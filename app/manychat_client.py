"""
Cliente para la ManyChat API.

Patrón general:
- Después de procesar un mensaje, escribimos la respuesta de Claude al custom field
  `ai_response` (ID configurado en MANYCHAT_AI_RESPONSE_FIELD_ID). El flow de ManyChat
  lo lee con {{ai_response}} en el siguiente Send Message.
- Cuando hay que terminar la conversación, seteamos `conversation_ended` (boolean)
  para que el flow corte el loop de re-pregunta.
- Para clasificar al prospecto, aplicamos tags por nombre vía addTagByName.
"""
import logging
import httpx
from app.config import settings

logger = logging.getLogger("despacho-bot.manychat")

MANYCHAT_API_BASE = "https://api.manychat.com"
SET_CUSTOM_FIELD_ENDPOINT = f"{MANYCHAT_API_BASE}/fb/subscriber/setCustomField"
SEND_FLOW_ENDPOINT = f"{MANYCHAT_API_BASE}/fb/sending/sendContent"
SEND_FLOW_NS_ENDPOINT = f"{MANYCHAT_API_BASE}/fb/sending/sendFlow"
ADD_TAG_BY_NAME_ENDPOINT = f"{MANYCHAT_API_BASE}/fb/subscriber/addTagByName"
REMOVE_TAG_BY_NAME_ENDPOINT = f"{MANYCHAT_API_BASE}/fb/subscriber/removeTagByName"
GET_INFO_ENDPOINT = f"{MANYCHAT_API_BASE}/fb/subscriber/getInfo"


def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.MANYCHAT_API_TOKEN}",
        "Content-Type": "application/json",
    }


def send_direct_message(subscriber_id: str, text: str) -> bool:
    """Envía el texto DIRECTAMENTE al subscriber vía ManyChat sendContent.

    NO depende del bloque "Enviar mensaje" del flow visual. Esto evita el race
    condition de los custom fields globales (cuando 2 flows del mismo user corren
    en paralelo, ambos leen {{ai_response}} y mandan doble). Con el envío directo,
    el bot entrega UNA sola vez por turno (controlado por su debounce/dedup).
    Requiere que el user haya escrito en las últimas 24h (messaging window).
    """
    if not settings.MANYCHAT_API_TOKEN:
        logger.error("MANYCHAT_API_TOKEN no configurado — no puedo enviar directo")
        return False
    payload = {
        "subscriber_id": subscriber_id,
        "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text}]}},
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(SEND_FLOW_ENDPOINT, json=payload, headers=_auth_headers())
        if response.status_code == 200 and response.json().get("status") == "success":
            logger.info(f"Mensaje directo enviado a {subscriber_id}")
            return True
        logger.error(f"Error en send_direct_message: {response.status_code} — {response.text[:300]}")
        return False
    except Exception as e:
        logger.exception(f"Error inesperado en send_direct_message: {e}")
        return False


def send_flow(subscriber_id: str, flow_ns: str) -> bool:
    """Dispara un flujo EXISTENTE de ManyChat (por su flow_ns) al subscriber.

    Sirve para que el bot actúe como "router de contenido": detecta la intención
    y manda el flujo del despacho que ya contiene el audio/video/guión correcto
    (declaración anual, regularización, video e.firma, cuenta bancaria, etc.).
    Requiere ventana de 24h abierta (el cliente escribió primero) porque los
    flujos llevan contenido libre. Best-effort: si falla, se loguea y seguimos.
    """
    if not settings.MANYCHAT_API_TOKEN:
        logger.error("MANYCHAT_API_TOKEN no configurado — no puedo enviar flujo")
        return False
    if not flow_ns:
        return False
    payload = {"subscriber_id": subscriber_id, "flow_ns": flow_ns}
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(SEND_FLOW_NS_ENDPOINT, json=payload, headers=_auth_headers())
        if response.status_code == 200 and response.json().get("status") == "success":
            logger.info(f"Flujo {flow_ns} enviado a {subscriber_id}")
            return True
        logger.error(f"Error en send_flow {flow_ns}: {response.status_code} — {response.text[:300]}")
        return False
    except Exception as e:
        logger.exception(f"Error inesperado en send_flow {flow_ns}: {e}")
        return False


def set_bot_reply(subscriber_id: str, text: str, channel: str = "messenger") -> bool:
    """Entrega la respuesta del bot.

    1) La escribe en el custom field `ai_response` (lo lee el bloque "Enviar mensaje"
       del flow con {{ai_response}}).
    2) Entrega:
       - En MESSENGER/IG: la manda DIRECTO vía send_direct_message (sendContent), que
         evita el race del custom field global y entrega una sola vez. (En estos
         canales el flow ya NO tiene bloque "Enviar mensaje".)
       - En WHATSAPP: NO se manda directo. La API de WhatsApp rechaza sendContent
         fuera de la ventana de sesión (error 3011 "without a message tag"). La
         entrega la hace el bloque "Enviar mensaje {{ai_response}}" del flow, que sí
         corre dentro de la sesión. Aquí solo dejamos ai_response seteado.
       Texto vacío = silencio/handoff → no se envía nada.
    """
    if not settings.MANYCHAT_API_TOKEN:
        logger.error("MANYCHAT_API_TOKEN no configurado — no puedo guardar respuesta")
        return False

    ok_field = False
    # Solo seteamos ai_response cuando hay texto real. ManyChat rechaza el string
    # vacío con "Invalid field value" (HTTP 400); en silencio/handoff no tocamos
    # el campo — la entrega la corta la Condición conversation_ended del flow.
    if settings.MANYCHAT_AI_RESPONSE_FIELD_ID and text and text.strip():
        payload = {
            "subscriber_id": subscriber_id,
            "field_id": int(settings.MANYCHAT_AI_RESPONSE_FIELD_ID),
            "field_value": text,
        }
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(SET_CUSTOM_FIELD_ENDPOINT, json=payload, headers=_auth_headers())
            if response.status_code == 200 and response.json().get("status") == "success":
                logger.info(f"ai_response actualizado para {subscriber_id}")
                ok_field = True
            else:
                logger.error(
                    f"Error al guardar ai_response: HTTP {response.status_code} — {response.text[:300]}"
                )
        except httpx.TimeoutException:
            logger.error(f"Timeout al actualizar ai_response para {subscriber_id}")
        except Exception as e:
            logger.exception(f"Error inesperado al actualizar custom field: {e}")

    # Entrega directa SOLO en Messenger/IG. En WhatsApp la API rechaza el envío
    # fuera de sesión (3011); ahí entrega el bloque "Enviar mensaje {{ai_response}}"
    # del flow. Texto vacío = silencio/handoff → no se envía nada.
    if text and text.strip() and channel != "whatsapp":
        send_direct_message(subscriber_id, text)
    return ok_field


send_text_message = set_bot_reply  # alias retrocompat


def set_conversation_ended(subscriber_id: str, ended: bool = True) -> bool:
    """Marca la conversación como terminada seteando el campo `conversation_ended`."""
    if not settings.MANYCHAT_API_TOKEN or not settings.MANYCHAT_CONVERSATION_ENDED_FIELD_ID:
        logger.error("Falta MANYCHAT_API_TOKEN o MANYCHAT_CONVERSATION_ENDED_FIELD_ID")
        return False

    payload = {
        "subscriber_id": subscriber_id,
        "field_id": int(settings.MANYCHAT_CONVERSATION_ENDED_FIELD_ID),
        # field is Text in this workspace (created via UI as Text by default).
        # Send "true"/"false" string so the flow's Condition can compare on it.
        "field_value": "true" if ended else "false",
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(SET_CUSTOM_FIELD_ENDPOINT, json=payload, headers=_auth_headers())
        if response.status_code == 200 and response.json().get("status") == "success":
            logger.info(f"conversation_ended={ended} para {subscriber_id}")
            return True
        logger.error(f"Error seteando conversation_ended: {response.status_code} — {response.text[:300]}")
        return False
    except Exception as e:
        logger.exception(f"Error inesperado seteando conversation_ended: {e}")
        return False


def apply_tag(subscriber_id: str, tag_name: str) -> bool:
    """Aplica un tag al suscriptor por nombre. Usa el endpoint addTagByName que
    no requiere conocer el tag_id."""
    if not settings.MANYCHAT_API_TOKEN:
        logger.error("MANYCHAT_API_TOKEN no configurado — no puedo aplicar tag")
        return False

    payload = {"subscriber_id": subscriber_id, "tag_name": tag_name}
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(ADD_TAG_BY_NAME_ENDPOINT, json=payload, headers=_auth_headers())
        if response.status_code == 200 and response.json().get("status") == "success":
            logger.info(f"Tag {tag_name!r} aplicado a {subscriber_id}")
            return True
        logger.error(f"Error aplicando tag {tag_name!r}: {response.status_code} — {response.text[:300]}")
        return False
    except Exception as e:
        logger.exception(f"Error inesperado aplicando tag {tag_name!r}: {e}")
        return False


def remove_tag(subscriber_id: str, tag_name: str) -> bool:
    """Quita un tag del suscriptor por nombre."""
    if not settings.MANYCHAT_API_TOKEN:
        logger.error("MANYCHAT_API_TOKEN no configurado — no puedo quitar tag")
        return False

    payload = {"subscriber_id": subscriber_id, "tag_name": tag_name}
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(REMOVE_TAG_BY_NAME_ENDPOINT, json=payload, headers=_auth_headers())
        if response.status_code == 200 and response.json().get("status") == "success":
            logger.info(f"Tag {tag_name!r} quitado de {subscriber_id}")
            return True
        logger.error(f"Error quitando tag {tag_name!r}: {response.status_code} — {response.text[:300]}")
        return False
    except Exception as e:
        logger.exception(f"Error inesperado quitando tag {tag_name!r}: {e}")
        return False


def get_subscriber_tags(subscriber_id: str) -> list[str]:
    """Regresa los nombres de tags actualmente aplicados al suscriptor."""
    if not settings.MANYCHAT_API_TOKEN:
        return []
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                GET_INFO_ENDPOINT,
                params={"subscriber_id": subscriber_id},
                headers=_auth_headers(),
            )
        if response.status_code == 200 and response.json().get("status") == "success":
            data = response.json().get("data") or {}
            return [t.get("name", "") for t in (data.get("tags") or []) if t.get("name")]
        logger.error(f"Error consultando tags de {subscriber_id}: {response.status_code}")
        return []
    except Exception as e:
        logger.exception(f"Error inesperado consultando tags: {e}")
        return []


def notify_admin(text: str) -> bool:
    """Manda mensaje de WhatsApp al admin del despacho (Soraida) cuando hay
    un prospecto INTERESADO/REGULARIZACION/CLIENTE que requiere atención humana.
    """
    if not settings.MANYCHAT_API_TOKEN or not settings.MANYCHAT_ADMIN_SUBSCRIBER_ID:
        logger.error("Falta MANYCHAT_API_TOKEN o MANYCHAT_ADMIN_SUBSCRIBER_ID — no puedo notificar")
        return False

    payload = {
        "subscriber_id": settings.MANYCHAT_ADMIN_SUBSCRIBER_ID,
        "data": {
            "version": "v2",
            "content": {
                "messages": [
                    {"type": "text", "text": text}
                ]
            }
        },
        "message_tag": "ACCOUNT_UPDATE",
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(SEND_FLOW_ENDPOINT, json=payload, headers=_auth_headers())
        if response.status_code == 200 and response.json().get("status") == "success":
            logger.info("Admin notificado correctamente")
            return True
        logger.error(f"Error al notificar admin: {response.status_code} — {response.text[:300]}")
        return False
    except Exception as e:
        logger.exception(f"Error inesperado notificando admin: {e}")
        return False
