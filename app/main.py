import logging
import re
import time
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from app.config import settings
from app.models import ManychatWebhookPayload
from app.supabase_client import (
    get_or_create_customer,
    maybe_set_source_from_text,
    get_conversation_history,
    save_message,
    block_customer,
    is_blocked,
    get_user_messages_since_last_assistant,
    get_latest_user_message_id,
    get_previous_user_message,
    has_assistant_reply_after,
    update_customer_phone,
    update_customer_stage,
    create_escalation,
    reset_customer_conversation,
)
from app.claude_client import generate_reply, classify_inquiry
from app.manychat_client import (
    set_bot_reply,
    notify_admin,
    set_conversation_ended,
    apply_tag,
    remove_tag,
    get_subscriber_tags,
    send_flow,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("despacho-bot")

app = FastAPI(title="Despacho Contable Fiscal Bot", version="0.1.0")

# Segundos a esperar antes de procesar, para agregar mensajes fragmentados.
# 4s con Haiku (~1-2s) total ~5-6s — la latencia más baja que Claude no
# se sienta lento. Reduce las veces que el cliente reenvía por impaciencia.
DEBOUNCE_SECONDS = 8.0

# Variables de ManyChat sin renderizar (p.ej. "{{last_input_text}}"): llegan
# así cuando una automatización (secuencia/broadcast) dispara el External
# Request en un contexto donde la variable no existe. NO son mensajes del
# cliente. Ráfagas vistas 2026-07-01 y 2026-07-08 (varios leads en minutos).
UNRENDERED_TEMPLATE_PATTERN = re.compile(r"^\s*(\{\{[\w\s.|]+\}\}\s*)+$")

BLOCK_ACTION_PATTERN = re.compile(r"\[ACTION:(BLOCK_RUDE|BLOCK_CRISIS)\]")
ESCALATE_PATTERN = re.compile(r"\[ACTION:ESCALATE:(INTERESADO|REGULARIZACION|SEGUIMIENTO|NO_INTERESADO|CLIENTE)\]")
PHONE_SAVE_PATTERN = re.compile(r"\[SAVE:phone:([+0-9\s\-()]+)\]")
SENDFLOW_PATTERN = re.compile(r"\[ACTION:SENDFLOW:([A-Z_]+)\]")

# Router de contenido: mapea una intención (key que el bot emite con
# [ACTION:SENDFLOW:KEY]) al flow_ns del flujo de ManyChat que YA contiene el
# audio/video/guión correcto del despacho. ns verificados del workspace
# 110240088419870 (getFlows, 2026-06-14). Si una key no está aquí, se ignora.
CONTENT_FLOWS = {
    "DECLARACION_ANUAL": "content20260209192728_479506",  # Presentar Declaración Anual
    "ADEUDO": "content20260209193357_611926",             # Declaración / Adeudo Fiscal
    "RECHAZADA": "content20260209193236_888524",          # Declaración Rechazada
    "ALTA_RFC": "content20260209232027_167152",           # Nueva alta en el RFC (video e.firma)
    "MENSUAL_PF": "content20260209185953_202899",         # Física - Servicio Contable Mensual
    "MENSUAL_PM": "content20260209185428_206642",         # Moral - Servicio Contable Mensual
    "REGULARIZACION_PF": "content20260203181752_033158",  # Física - Audio Regularización
    "REGULARIZACION_PM": "content20260209185103_386754",  # Moral - Audio Regularización
    "CUENTA_BANCARIA": "content20260209211955_250160",    # Cuenta bancaria
    "MENU": "content20260209213603_730337",               # Menú Principal 2.0
}
BOLD_MARKDOWN_PATTERN = re.compile(r"\*\*(.+?)\*\*")
WHATSAPP_BOLD_PATTERN = re.compile(r"(?<!\S)\*([^\n*]+?)\*(?!\S)")

# Mapping de categoría de escalation → tags de ManyChat (los tags reales del workspace 110240088419870)
ESCALATION_TAGS = {
    "INTERESADO": ["Interesado", "ANÁLISIS FISCAL PENDIENTE"],
    "REGULARIZACION": ["Interesado", "REGULARIZACIÓN", "ANÁLISIS FISCAL PENDIENTE"],
    "SEGUIMIENTO": ["Seguimiento"],
    "NO_INTERESADO": ["No Interesado"],
    "CLIENTE": ["CLIENTE", "IMPORTANTE"],
}

# Categorías que disparan notificación al equipo del despacho (vía notify_admin)
ESCALATION_NOTIFY = {"INTERESADO", "REGULARIZACION", "CLIENTE"}

# Stages donde un HUMANO toma la conversación → el bot se calla (silent handoff).
# SEGUIMIENTO (necesita sacar e.firma/cita y volver) y NO_INTERESADO NO van aquí:
# en esos casos el bot debe SEGUIR atendiendo si el cliente sigue escribiendo.
HUMAN_HANDOFF_STAGES = {
    "escalated_interesado",
    "escalated_regularizacion",
    "escalated_cliente",
    "escalated_defensa",
}

# --- Remarketing (Fase 2) ---
# Etiqueta-interruptor que mete al lead a la secuencia "Remarketing" de ManyChat
# (vía una Regla: "se agrega etiqueta remarketing" → suscribir a la secuencia).
# El bot la PONE mientras el lead siga siendo un prospecto sin cerrar, y la QUITA
# cuando pasa con la contadora (INTERESADO/REGULARIZACION/CLIENTE), no le interesa
# (NO_INTERESADO), está en SEGUIMIENTO o se bloquea. Las plantillas R1/R2 de la
# secuencia llevan condición de envío "tiene etiqueta remarketing", así que un lead
# ya convertido deja de recibir recordatorios en cuanto le quitamos la etiqueta.
#
# SEGUIMIENTO (necesita ir al SAT a sacar e.firma/RFC/contraseña y volver — puede
# tardar SEMANAS o un mes) NO va al remarketing agresivo (R1 20h / R2 2d / borrado
# 4d): se le borraría un lead bueno por tardar en su papeleo. En su lugar recibe la
# etiqueta "Seguimiento" (vía ESCALATION_TAGS) → una secuencia SUAVE aparte en
# ManyChat (recordatorio 1sem/2sem/1mes, SIN borrado).
REMARKETING_TAG = "remarketing"
REMARKETING_STOP_CATEGORIES = {"INTERESADO", "REGULARIZACION", "CLIENTE", "NO_INTERESADO", "SEGUIMIENTO"}

# Keyword exacto que un usuario puede mandar para resetear su conversación
# entera en pruebas. No usar palabras que un cliente real podría escribir.
RESET_KEYWORD = "Reset"


def whatsapp_format(text: str) -> str:
    """Convierte Markdown bold (**x**) a WhatsApp bold (*x*)."""
    return BOLD_MARKDOWN_PATTERN.sub(r"*\1*", text)


def strip_asterisks_for_ig(text: str) -> str:
    """Para Instagram/Messenger, quita asteriscos de negritas (se verían literalmente)."""
    text = BOLD_MARKDOWN_PATTERN.sub(r"\1", text)
    text = WHATSAPP_BOLD_PATTERN.sub(r"\1", text)
    return text


def extract_phone_save(reply_text: str) -> tuple[str, str | None]:
    match = PHONE_SAVE_PATTERN.search(reply_text)
    if not match:
        return reply_text, None
    clean = PHONE_SAVE_PATTERN.sub("", reply_text).strip()
    raw_phone = match.group(1).strip()
    normalized = "+" + "".join(c for c in raw_phone if c.isdigit()) if "+" in raw_phone \
                 else "".join(c for c in raw_phone if c.isdigit())
    return clean, normalized


def extract_block_action(reply_text: str) -> tuple[str, str | None]:
    match = BLOCK_ACTION_PATTERN.search(reply_text)
    action = match.group(1) if match else None
    clean_text = BLOCK_ACTION_PATTERN.sub("", reply_text).strip()
    return clean_text, action


def extract_escalation(reply_text: str) -> tuple[str, str | None]:
    match = ESCALATE_PATTERN.search(reply_text)
    category = match.group(1) if match else None
    clean_text = ESCALATE_PATTERN.sub("", reply_text).strip()
    return clean_text, category


def extract_sendflow(reply_text: str) -> tuple[str, str | None]:
    """Saca el marcador [ACTION:SENDFLOW:KEY] del texto. Devuelve (texto_limpio,
    flow_ns) donde flow_ns es el ns del flujo de ManyChat a disparar, o None si
    no hay marcador o la key no está en el catálogo."""
    match = SENDFLOW_PATTERN.search(reply_text)
    clean_text = SENDFLOW_PATTERN.sub("", reply_text).strip()
    if not match:
        return clean_text, None
    flow_ns = CONTENT_FLOWS.get(match.group(1))
    if not flow_ns:
        logger.warning(f"SENDFLOW key desconocida ignorada: {match.group(1)}")
    return clean_text, flow_ns


def handle_escalation(customer: dict, category: str, payload: ManychatWebhookPayload) -> None:
    """Aplica los tags correspondientes en ManyChat, registra la escalation
    en Supabase y, si aplica, notifica al equipo del despacho.
    """
    try:
        tags = ESCALATION_TAGS.get(category, [])
        for tag in tags:
            try:
                apply_tag(payload.user_id, tag)
            except Exception as e:
                logger.warning(f"No se pudo aplicar tag {tag!r} en ManyChat: {e}")

        try:
            create_escalation(customer["id"], reason=category.lower())
        except Exception as e:
            logger.warning(f"No se pudo registrar escalation en Supabase: {e}")

        try:
            update_customer_stage(customer["id"], f"escalated_{category.lower()}")
        except Exception as e:
            logger.warning(f"No se pudo actualizar stage: {e}")

        if category in ESCALATION_NOTIFY:
            admin_msg = (
                f"📋 Nuevo prospecto Despacho ({category})\n\n"
                f"👤 {customer.get('first_name') or 'Sin nombre'}\n"
                f"📞 {customer.get('phone') or 'Sin teléfono'}\n"
                f"🆔 ManyChat user: {payload.user_id}\n"
                f"🏷 Tags aplicados: {', '.join(tags)}"
            )
            try:
                notify_admin(admin_msg)
            except Exception as e:
                logger.warning(f"No se pudo notificar al admin: {e}")
    except Exception as e:
        logger.exception(f"Error en handle_escalation: {e}")


@app.get("/")
def root():
    return {"service": "despacho-bot", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/api/test/chat")
def test_chat(payload: ManychatWebhookPayload, x_webhook_secret: str | None = Header(default=None)):
    """Endpoint de prueba — corre la misma lógica del bot pero NO envía la
    respuesta por ManyChat. La devuelve directo en el JSON. Útil para iterar
    el prompt sin tocar el flow real.
    """
    if settings.WEBHOOK_SECRET and x_webhook_secret != settings.WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    customer = get_or_create_customer(
        user_id=payload.user_id,
        phone=payload.phone,
        first_name=payload.first_name,
    )

    if payload.text.strip() == RESET_KEYWORD:
        deleted = reset_customer_conversation(customer["id"])
        return {"status": "reset", "deleted_messages": deleted, "customer_id": customer["id"]}

    if is_blocked(customer):
        return {"status": "blocked", "reason": customer.get("block_reason")}

    save_message(customer["id"], "user", payload.text)

    history = get_conversation_history(customer["id"], limit=40)
    history_clean = [h for h in history if h["content"] != payload.text or h["role"] != "user"]

    channel = payload.channel or "whatsapp"
    reply_raw = generate_reply(
        history_clean, payload.text,
        user_name=customer.get("first_name"),
        channel=channel,
        phone=customer.get("phone"),
        customer_stage=customer.get("stage"),
    )
    reply_clean, block_action = extract_block_action(reply_raw)
    reply_clean, escalation_category = extract_escalation(reply_clean)
    reply_clean, phone_collected = extract_phone_save(reply_clean)
    reply_clean, sendflow_ns = extract_sendflow(reply_clean)

    if channel == "whatsapp":
        reply_clean = whatsapp_format(reply_clean)
    else:
        reply_clean = strip_asterisks_for_ig(reply_clean)

    save_message(customer["id"], "assistant", reply_clean)

    return {
        "reply": reply_clean,
        "block_action": block_action,
        "escalation": escalation_category,
        "phone_collected": phone_collected,
        "sendflow_ns": sendflow_ns,
        "customer_id": customer["id"],
    }


def _process_user_turn(customer: dict, payload: ManychatWebhookPayload, my_msg_id: str | None) -> None:
    """Se ejecuta sincrónicamente dentro del request del webhook con un debounce corto
    para agregar mensajes fragmentados, hacer idempotency checks y llamar a Claude.
    """
    try:
        time.sleep(DEBOUNCE_SECONDS)

        latest_id = get_latest_user_message_id(customer["id"])
        if latest_id and my_msg_id and latest_id != my_msg_id:
            logger.info(
                f"Debounce: skip {customer['id']} — mensaje más reciente ({latest_id}) procesará"
            )
            return

        if my_msg_id and has_assistant_reply_after(customer["id"], my_msg_id):
            logger.info(
                f"Idempotency: skip {customer['id']} — ya existe respuesta después de {my_msg_id}"
            )
            return

        # 🔒 Candado WhatsApp (aquí, DESPUÉS del debounce → el tag lead-nuevo ya
        # aterrizó): el bot SOLO atiende contactos con etiqueta `lead-nuevo`.
        # Clientes viejos / conversaciones previas no la tienen → el bot se queda
        # callado y NUNCA les escribe. Fail-safe: si falla leer tags, silencio.
        if (payload.channel or "whatsapp") == "whatsapp":
            try:
                wa_tags = get_subscriber_tags(payload.user_id)
            except Exception as e:
                logger.warning(f"No pude leer tags de {payload.user_id}: {e}")
                wa_tags = []
            if "lead-nuevo" not in wa_tags:
                logger.info(
                    f"WhatsApp gate (bg): {customer['id']} sin lead-nuevo → bot en silencio"
                )
                return
            # Remarketing (Fase 2): el lead ACABA de escribir → quítale la etiqueta
            # "remarketing" para CANCELAR cualquier recordatorio/borrado pendiente
            # (Regla "CANCELAR" lo desuscribe de la secuencia). Si al final NO cerró,
            # se la reponemos abajo → re-arma el contador desde ESTE mensaje. Así el
            # borrado automático SOLO alcanza a quien de verdad nunca contestó.
            try:
                remove_tag(payload.user_id, REMARKETING_TAG)
            except Exception as e:
                logger.warning(f"No se pudo limpiar remarketing al entrar: {e}")

        pending = get_user_messages_since_last_assistant(customer["id"])
        if not pending:
            logger.warning(f"No pending messages for {customer['id']} — caso raro")
            return

        combined_text = "\n".join(m["content"] for m in pending)
        history = get_conversation_history(customer["id"], limit=40)
        history_clean = [
            h for h in history
            # Fuera del contexto del modelo cualquier mensaje con plantillas
            # {{...}} sin renderizar: tanto la basura que entró de ManyChat como
            # las respuestas "modo configuración" que el bot dio por culpa de esa
            # basura (p.ej. "Perfecto, {{first_name}}..."). Limpia el veneno ya
            # guardado sin tocar la base.
            if "{{" not in (h["content"] or "")
            and (h["role"] == "assistant" or h["content"] not in [m["content"] for m in pending])
        ]

        # Non-service triage — only for brand-new leads (no prior conversation).
        # If the lead is asking for prácticas, ofreciendo servicios, buscando
        # empleo, etc., we don't engage the LLM. Single fixed reply, tag, end
        # conversation. Returning users (anyone with prior assistant replies)
        # skip this — they already passed the gate or were classified before.
        stage = (customer.get("stage") or "lead_new").strip() or "lead_new"
        has_prior_assistant = any(h["role"] == "assistant" for h in history_clean)
        if stage == "lead_new" and not has_prior_assistant:
            category = classify_inquiry(combined_text)
            if category != "service":
                logger.info(f"Non-service inquiry para {customer['id']}: {category}")
                fixed_reply = "En unos momentos se atenderá su petición."
                save_message(customer["id"], "assistant", fixed_reply)
                try:
                    apply_tag(payload.user_id, category)
                except Exception as e:
                    logger.warning(f"No se pudo aplicar tag {category}: {e}")
                # End the conversation: future messages from this lead won't
                # reach Claude. ManyChat's Condition on conversation_ended
                # blocks subsequent Send Message nodes.
                try:
                    set_conversation_ended(subscriber_id=payload.user_id, ended=True)
                except Exception as e:
                    logger.warning(f"No se pudo set conversation_ended en non-service: {e}")
                set_bot_reply(subscriber_id=payload.user_id, text=fixed_reply, channel=(payload.channel or "whatsapp"))
                return

        channel = payload.channel or "whatsapp"
        reply_raw = generate_reply(
            history_clean, combined_text,
            user_name=customer.get("first_name"),
            channel=channel,
            phone=customer.get("phone"),
            customer_stage=customer.get("stage"),
        )
        reply_clean, block_action = extract_block_action(reply_raw)
        reply_clean, escalation_category = extract_escalation(reply_clean)
        reply_clean, phone_collected = extract_phone_save(reply_clean)
        reply_clean, sendflow_ns = extract_sendflow(reply_clean)

        if channel == "whatsapp":
            reply_clean = whatsapp_format(reply_clean)
        else:
            reply_clean = strip_asterisks_for_ig(reply_clean)

        if my_msg_id and has_assistant_reply_after(customer["id"], my_msg_id):
            logger.info(
                f"Late idempotency: skip {customer['id']} — respuesta concurrente detectada"
            )
            return

        save_message(customer["id"], "assistant", reply_clean)

        if phone_collected:
            logger.info(f"Teléfono capturado para {customer['id']}: {phone_collected}")
            update_customer_phone(customer["id"], phone_collected)
            customer["phone"] = phone_collected

        if block_action == "BLOCK_RUDE":
            logger.info(f"Blocking {customer['id']} por grosería/spam (7 días)")
            block_customer(customer["id"], reason="rude", days=7)
        elif block_action == "BLOCK_CRISIS":
            logger.info(f"Blocking {customer['id']} por crisis (7 días)")
            block_customer(customer["id"], reason="crisis", days=7)

        if escalation_category:
            logger.info(f"Escalation {escalation_category} para {customer['id']}")
            handle_escalation(customer, escalation_category, payload)
            # Don't set conversation_ended=true here — that would block the
            # Send Message Condition in ManyChat for THIS turn, meaning the
            # user never sees the "Le aviso al equipo" confirmation. The
            # customer's stage is now escalated_*; the NEXT message will hit
            # the silent-handoff branch in the webhook entry, which sets
            # conversation_ended=true at that point. The handoff message
            # itself still reaches the user.

        # Remarketing (Fase 2): re-arma el drip. Ya quitamos "remarketing" al
        # entrar el mensaje (cancela lo pendiente). Aquí, SOLO si el lead sigue
        # siendo prospecto activo (no cerró con la contadora, no es no-interesado,
        # no se bloqueó), le REPONEMOS la etiqueta → la Regla "SUSCRIBIR" lo mete a
        # la secuencia con el contador reiniciado desde este mensaje. Si cerró, NO
        # la reponemos → se queda fuera del drip y del borrado (lo atiende Soraida).
        if channel == "whatsapp":
            try:
                if escalation_category in REMARKETING_STOP_CATEGORIES or block_action:
                    logger.info(f"Remarketing OFF para {customer['id']} ({escalation_category or block_action})")
                else:
                    apply_tag(payload.user_id, REMARKETING_TAG)
                    logger.info(f"Remarketing ON (re-armado) para {customer['id']}")
            except Exception as e:
                logger.warning(f"No se pudo gestionar etiqueta remarketing: {e}")

        # Make sure conversation_ended is false for this turn so the Send
        # Message Condition in ManyChat lets our reply through. Costs one
        # extra ManyChat API call per turn but it's cheap and idempotent.
        try:
            set_conversation_ended(subscriber_id=payload.user_id, ended=False)
        except Exception as e:
            logger.warning(f"No se pudo limpiar conversation_ended en turn normal: {e}")
        set_bot_reply(subscriber_id=payload.user_id, text=reply_clean, channel=channel)

        # Router de contenido: si el bot decidió compartir un recurso del
        # despacho (audio/video/guión), dispara el flujo de ManyChat después
        # del texto. Best-effort, no rompe el turno si falla.
        if sendflow_ns:
            try:
                send_flow(payload.user_id, sendflow_ns)
                logger.info(f"Router: flujo {sendflow_ns} disparado para {customer['id']}")
            except Exception as e:
                logger.warning(f"No se pudo disparar flujo {sendflow_ns}: {e}")

        logger.info(f"Respondido a {customer['id']}: {len(pending)} msg(s) agregados")
    except Exception as e:
        logger.exception(f"Error procesando mensaje en background: {e}")
        try:
            set_bot_reply(
                subscriber_id=payload.user_id,
                text="Hola, en un momento un ejecutivo del despacho te contacta. Gracias por tu paciencia 🙏",
            )
        except Exception:
            pass


@app.post("/api/webhook/manychat")
def manychat_webhook(
    payload: ManychatWebhookPayload,
    background_tasks: BackgroundTasks,
    x_webhook_secret: str | None = Header(default=None),
):
    """Webhook de ManyChat. Responde 200 al INSTANTE y procesa el mensaje en
    segundo plano (Claude tarda ~8-12s).

    Antes procesábamos sincrónicamente, pero el External Request de ManyChat
    tiene timeout de 10s: si el bot tardaba más, ManyChat seguía de largo y el
    flujo de WhatsApp llegaba al "Enviar mensaje" SIN la respuesta lista → no
    entregaba nada. Ahora la Solicitud externa retorna de inmediato y el flujo
    debe tener una PAUSA después de ella (≈15s) para esperar a que el bot deje
    listo {{ai_response}} + conversation_ended antes del "Enviar mensaje".
    En Messenger no importa la pausa (ahí el bot entrega directo por send_direct).
    """
    if settings.WEBHOOK_SECRET and x_webhook_secret != settings.WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    logger.info(f"Mensaje de {payload.user_id}: {payload.text[:80]}")

    # 🛡️ Vacuna anti-plantilla: si el "mensaje" es una variable de ManyChat sin
    # renderizar, se ignora por completo — ni se guarda (envenenaba el historial
    # y el modelo entraba en "modo configuración") ni se responde. Se silencia
    # el flow igual que el silent handoff para que no reenvíe un ai_response viejo.
    if UNRENDERED_TEMPLATE_PATTERN.match(payload.text or ""):
        logger.warning(f"Template sin renderizar de {payload.user_id} ({payload.text[:40]!r}) — ignorado")
        try:
            set_bot_reply(subscriber_id=payload.user_id, text="")
            set_conversation_ended(subscriber_id=payload.user_id, ended=True)
        except Exception as e:
            logger.warning(f"No se pudo silenciar flow tras template: {e}")
        return {"status": "ignored_unrendered_template"}

    try:
        customer = get_or_create_customer(
            user_id=payload.user_id,
            phone=payload.phone,
            first_name=payload.first_name,
        )
        maybe_set_source_from_text(customer, payload.text or "")

        if payload.text.strip() == RESET_KEYWORD:
            deleted = reset_customer_conversation(customer["id"])
            # Quita todos los tags del bot en ManyChat también — si no, el
            # cliente queda como "Interesado" en la lista de Soraida aunque
            # internamente sea lead_new.
            BOT_MANAGED_TAGS = [
                "Interesado",
                "ANÁLISIS FISCAL PENDIENTE",
                "REGULARIZACIÓN",
                "Seguimiento",
                "No Interesado",
                "CLIENTE",
                "IMPORTANTE",
                "remarketing",
                "No respondió",
            ]
            current_tags = get_subscriber_tags(payload.user_id)
            removed_tags = []
            for tag in BOT_MANAGED_TAGS:
                if tag in current_tags:
                    if remove_tag(payload.user_id, tag):
                        removed_tags.append(tag)
            reply = "🔄 Conversación reiniciada. Empezamos de cero — escríbeme un mensaje."
            try:
                # Clear conversation_ended FIRST so the ManyChat Condition lets
                # the Send Message run for this Reset confirmation. The
                # silent-handoff path leaves the field at true; without this
                # unset, the user stays in silent mode forever even after Reset.
                set_conversation_ended(subscriber_id=payload.user_id, ended=False)
            except Exception as e:
                logger.warning(f"No se pudo limpiar conversation_ended en Reset: {e}")
            set_bot_reply(subscriber_id=payload.user_id, text=reply, channel=(payload.channel or "whatsapp"))
            logger.info(
                f"Reset manual de {customer['id']} — {deleted} mensajes borrados, "
                f"tags quitados: {removed_tags or 'ninguno'}"
            )
            return {"status": "reset", "deleted_messages": deleted, "removed_tags": removed_tags}

        if is_blocked(customer):
            logger.info(
                f"Customer {customer['id']} está bloqueado ({customer.get('block_reason')}) — ignorando mensaje"
            )
            save_message(customer["id"], "user", payload.text)
            return {"status": "blocked"}

        # NOTA: el 🔒 candado WhatsApp (solo atender contactos con etiqueta
        # `lead-nuevo`) se movió a _process_user_turn, DESPUÉS del debounce —
        # así el tag `lead-nuevo` (que pone la automatización "contacto nuevo")
        # ya aterrizó y no perdemos el PRIMER mensaje de un lead nuevo por race.
        # Esto permite quitar la Pausa inteligente y la Condición del flow de
        # ManyChat (que perdían mensajes en ráfaga). Clientes viejos = sin
        # lead-nuevo → el bot se queda callado igual (fail-safe).

        # Silencio total para customers ya tageados — humano toma desde aquí.
        # El bot guarda el mensaje en historial (para que Soraida vea contexto),
        # marca ai_response vacío + conversation_ended=true. La automatización
        # de ManyChat debe tener el Condition de conversation_ended ANTES del
        # "Send Message" para que no se envíe nada.
        stage = (customer.get("stage") or "").strip()
        if stage in HUMAN_HANDOFF_STAGES:
            save_message(customer["id"], "user", payload.text)
            try:
                set_bot_reply(subscriber_id=payload.user_id, text="")
                set_conversation_ended(subscriber_id=payload.user_id, ended=True)
            except Exception as e:
                logger.warning(f"No se pudo silenciar bot para {customer['id']}: {e}")
            logger.info(f"Silent handoff para {customer['id']} (stage={stage})")
            return {"status": "handed_off", "stage": stage}

        saved = save_message(customer["id"], "user", payload.text)
        my_msg_id = saved[0]["id"] if saved else None

        # NOTA: antes había aquí un "dedup" que, si el user reenviaba el mismo
        # texto en <30s, seteaba conversation_ended=true y silenciaba. BUG: con el
        # debounce "latest wins", el mensaje original se saltaba (hay uno más
        # reciente) y el reenvío se deduplicaba → NADIE respondía y el bot quedaba
        # mudo (clientes mandando 2 veces lo mismo). Se ELIMINÓ. La de-duplicación
        # real ya la hacen el debounce (procesa solo el último y agrega todos los
        # pendientes) + la idempotencia (has_assistant_reply_after) en
        # _process_user_turn, sin silenciar nunca.

        # Procesa en segundo plano y responde 200 de inmediato (evita el timeout
        # de 10s del External Request de ManyChat). El flujo espera con su Pausa.
        background_tasks.add_task(_process_user_turn, customer, payload, my_msg_id)
        return {"status": "ok", "msg_id": my_msg_id}

    except Exception as e:
        logger.exception(f"Error procesando mensaje: {e}")
        try:
            set_bot_reply(
                subscriber_id=payload.user_id,
                text="Hola, en un momento un ejecutivo del despacho te contacta. Gracias por tu paciencia 🙏",
            )
        except Exception:
            pass
        return {"status": "error"}
