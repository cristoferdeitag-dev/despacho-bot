import logging
import re
import time
from fastapi import FastAPI, HTTPException, Header
from app.config import settings
from app.models import ManychatWebhookPayload
from app.supabase_client import (
    get_or_create_customer,
    get_conversation_history,
    save_message,
    block_customer,
    is_blocked,
    get_user_messages_since_last_assistant,
    get_latest_user_message_id,
    has_assistant_reply_after,
    update_customer_phone,
    update_customer_stage,
    create_escalation,
    reset_customer_conversation,
)
from app.claude_client import generate_reply
from app.manychat_client import (
    set_bot_reply,
    notify_admin,
    set_conversation_ended,
    apply_tag,
    remove_tag,
    get_subscriber_tags,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("despacho-bot")

app = FastAPI(title="Despacho Contable Fiscal Bot", version="0.1.0")

# Segundos a esperar antes de procesar, para agregar mensajes fragmentados.
# El timeout duro de External Request en ManyChat es 10s. Con 6s + Claude
# Haiku (~1-2s) terminamos en ~7-8s — margen sano.
DEBOUNCE_SECONDS = 6.0

BLOCK_ACTION_PATTERN = re.compile(r"\[ACTION:(BLOCK_RUDE|BLOCK_CRISIS)\]")
ESCALATE_PATTERN = re.compile(r"\[ACTION:ESCALATE:(INTERESADO|REGULARIZACION|SEGUIMIENTO|NO_INTERESADO|CLIENTE)\]")
PHONE_SAVE_PATTERN = re.compile(r"\[SAVE:phone:([+0-9\s\-()]+)\]")
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

        pending = get_user_messages_since_last_assistant(customer["id"])
        if not pending:
            logger.warning(f"No pending messages for {customer['id']} — caso raro")
            return

        combined_text = "\n".join(m["content"] for m in pending)
        history = get_conversation_history(customer["id"], limit=40)
        history_clean = [
            h for h in history
            if h["role"] == "assistant" or h["content"] not in [m["content"] for m in pending]
        ]

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

        # Make sure conversation_ended is false for this turn so the Send
        # Message Condition in ManyChat lets our reply through. Costs one
        # extra ManyChat API call per turn but it's cheap and idempotent.
        try:
            set_conversation_ended(subscriber_id=payload.user_id, ended=False)
        except Exception as e:
            logger.warning(f"No se pudo limpiar conversation_ended en turn normal: {e}")
        set_bot_reply(subscriber_id=payload.user_id, text=reply_clean)
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
    x_webhook_secret: str | None = Header(default=None),
):
    """Webhook de ManyChat. Procesa el mensaje sincrónicamente con un debounce
    corto y deja el ai_response listo como custom field para que ManyChat lo
    muestre al usuario.

    Timeout de External Request en ManyChat: 10s hard-coded. Con DEBOUNCE_SECONDS=3
    y Claude (~3-5s) terminamos en ~8s.
    """
    if settings.WEBHOOK_SECRET and x_webhook_secret != settings.WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    logger.info(f"Mensaje de {payload.user_id}: {payload.text[:80]}")

    try:
        customer = get_or_create_customer(
            user_id=payload.user_id,
            phone=payload.phone,
            first_name=payload.first_name,
        )

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
            set_bot_reply(subscriber_id=payload.user_id, text=reply)
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

        # Silencio total para customers ya tageados — humano toma desde aquí.
        # El bot guarda el mensaje en historial (para que Soraida vea contexto),
        # marca ai_response vacío + conversation_ended=true. La automatización
        # de ManyChat debe tener el Condition de conversation_ended ANTES del
        # "Send Message" para que no se envíe nada.
        stage = (customer.get("stage") or "").strip()
        if stage.startswith("escalated_"):
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

        _process_user_turn(customer, payload, my_msg_id)
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
