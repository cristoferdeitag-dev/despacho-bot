import re

from supabase import create_client, Client
from app.config import settings


def get_supabase() -> Client:
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "Faltan SUPABASE_URL o SUPABASE_SERVICE_KEY en el .env"
        )
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)


def get_or_create_customer(user_id: str, phone: str | None = None, first_name: str | None = None) -> dict:
    sb = get_supabase()
    existing = sb.table("customers").select("*").eq("manychat_user_id", user_id).limit(1).execute()
    if existing.data:
        customer = existing.data[0]
        # Actualizar campos si ahora tenemos info que antes estaba vacía
        updates = {}
        if first_name and not customer.get("first_name"):
            updates["first_name"] = first_name
        if phone and not customer.get("phone"):
            updates["phone"] = phone
        if updates:
            sb.table("customers").update(updates).eq("id", customer["id"]).execute()
            customer.update(updates)
        return customer

    insert = sb.table("customers").insert({
        "manychat_user_id": user_id,
        "phone": phone,
        "first_name": first_name,
        "stage": "lead_new",
    }).execute()
    return insert.data[0]


# Atribución de fuente: la web añade una línea tipo "Los encontré en Google" al
# mensaje precargado de WhatsApp (src/lib/tracking.js en despacho-web); aquí la
# detectamos y llenamos first_source (solo la primera vez) + last_source/detail.
SOURCE_PATTERNS = [
    (re.compile(r"los encontr[eé] en google|vengo de google", re.I), "google_ads"),
    (re.compile(r"los encontr[eé] en ([a-z0-9_.-]+)", re.I), None),  # grupo 1 = fuente
    (re.compile(r"vi sus videos", re.I), "web_videos"),
]


def maybe_set_source_from_text(customer: dict, text: str) -> None:
    if not text:
        return
    source = detail = None
    for pat, fixed in SOURCE_PATTERNS:
        m = pat.search(text)
        if m:
            source = (fixed or m.group(1)).strip("_.-").lower()
            detail = m.group(0)
            break
    if not source or customer.get("last_source") == source:
        return
    updates = {"last_source": source, "source_detail": detail}
    if not customer.get("first_source"):
        updates["first_source"] = source
    try:
        get_supabase().table("customers").update(updates).eq("id", customer["id"]).execute()
        customer.update(updates)
    except Exception:
        pass  # la atribución jamás debe tumbar el turno del bot


def get_conversation_history(customer_id: str, limit: int = 40) -> list[dict]:
    """Get the most recent `limit` messages, returned in chronological order
    (oldest first) so Claude sees them in conversation order.

    Previously used ORDER BY created_at ASC LIMIT 40, which for long-term
    customers returned the oldest 40 messages (from weeks ago) and cut off
    the recent turn. That caused the bot to answer to stale context
    (e.g. replying about age when the user just said "Hola").
    """
    sb = get_supabase()
    result = (
        sb.table("messages")
        .select("role, content, created_at")
        .eq("customer_id", customer_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    rows = result.data or []
    rows.reverse()  # back to chronological order
    return rows


def save_message(customer_id: str, role: str, content: str) -> list[dict]:
    sb = get_supabase()
    result = sb.table("messages").insert({
        "customer_id": customer_id,
        "role": role,
        "content": content,
    }).execute()
    return result.data or []


def update_customer_stage(customer_id: str, stage: str) -> None:
    sb = get_supabase()
    sb.table("customers").update({"stage": stage}).eq("id", customer_id).execute()


def update_customer_phone(customer_id: str, phone: str) -> None:
    """Actualiza el teléfono del customer (usado cuando se colecta por IG/Messenger)."""
    sb = get_supabase()
    sb.table("customers").update({"phone": phone}).eq("id", customer_id).execute()


def block_customer(customer_id: str, reason: str, days: int = 7) -> None:
    """Marca al cliente como bloqueado por N días (por grosería, crisis, etc.)."""
    from datetime import datetime, timezone, timedelta
    blocked_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    sb = get_supabase()
    sb.table("customers").update({
        "blocked_until": blocked_until,
        "block_reason": reason,
        "stage": f"blocked_{reason}",
    }).eq("id", customer_id).execute()


def get_user_messages_since_last_assistant(customer_id: str) -> list[dict]:
    """
    Devuelve todos los mensajes del usuario posteriores al último mensaje del asistente.
    Sirve para agregar fragmentos de mensajes que el usuario envió en sucesión rápida.
    Si no hay mensajes del asistente, devuelve todos los mensajes del usuario.
    """
    sb = get_supabase()
    # Último mensaje assistant
    last_assistant = (
        sb.table("messages")
        .select("created_at")
        .eq("customer_id", customer_id)
        .eq("role", "assistant")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if last_assistant.data:
        cutoff = last_assistant.data[0]["created_at"]
        q = (
            sb.table("messages")
            .select("id, content, created_at")
            .eq("customer_id", customer_id)
            .eq("role", "user")
            .gt("created_at", cutoff)
            .order("created_at", desc=False)
        )
    else:
        q = (
            sb.table("messages")
            .select("id, content, created_at")
            .eq("customer_id", customer_id)
            .eq("role", "user")
            .order("created_at", desc=False)
        )
    return q.execute().data or []


def create_escalation(customer_id: str, reason: str) -> dict:
    """Registra una escalation cuando el bot pasa el prospecto a humano o lo clasifica.
    `reason` típicamente: 'interesado', 'regularizacion', 'seguimiento', 'no_interesado', 'cliente'.
    """
    sb = get_supabase()
    result = sb.table("escalations").insert({
        "customer_id": customer_id,
        "reason": reason,
    }).execute()
    return result.data[0] if result.data else {}


def get_previous_user_message(customer_id: str, exclude_id: str | None = None) -> dict | None:
    """Regresa el mensaje user inmediatamente anterior a `exclude_id` (o el
    último de todos si no se da). Útil para detectar duplicados literales
    cuando el cliente reenvía el mismo texto por impaciencia."""
    sb = get_supabase()
    q = (
        sb.table("messages")
        .select("id, content, created_at")
        .eq("customer_id", customer_id)
        .eq("role", "user")
        .order("created_at", desc=True)
        .limit(2 if exclude_id else 1)
    )
    res = q.execute()
    rows = res.data or []
    if exclude_id:
        rows = [r for r in rows if r["id"] != exclude_id]
    return rows[0] if rows else None


def get_latest_user_message_id(customer_id: str) -> str | None:
    sb = get_supabase()
    res = (
        sb.table("messages")
        .select("id")
        .eq("customer_id", customer_id)
        .eq("role", "user")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0]["id"] if res.data else None


def has_assistant_reply_after(customer_id: str, user_msg_id: str) -> bool:
    """Idempotency check: True si ya existe un mensaje del asistente posterior al
    user_msg dado. ManyChat reintenta el webhook si nuestro servidor tarda >10s,
    así que este check evita que dos workers generen respuestas duplicadas.
    """
    sb = get_supabase()
    me = sb.table("messages").select("created_at").eq("id", user_msg_id).limit(1).execute()
    if not me.data:
        return False
    my_ts = me.data[0]["created_at"]
    res = (
        sb.table("messages")
        .select("id")
        .eq("customer_id", customer_id)
        .eq("role", "assistant")
        .gt("created_at", my_ts)
        .limit(1)
        .execute()
    )
    return bool(res.data)


def reset_customer_conversation(customer_id: str) -> int:
    """Borra todo el historial de mensajes del customer y resetea su stage a
    lead_new. Usado por el comando de pruebas `Reset` para empezar de cero
    sin tener que crear un contacto nuevo en ManyChat.
    """
    sb = get_supabase()
    deleted = sb.table("messages").delete().eq("customer_id", customer_id).execute()
    sb.table("customers").update({"stage": "lead_new", "notes": None}).eq("id", customer_id).execute()
    return len(deleted.data or [])


def is_blocked(customer: dict) -> bool:
    """Revisa si el cliente está bloqueado (blocked_until futuro)."""
    from datetime import datetime, timezone
    blocked_until = customer.get("blocked_until")
    if not blocked_until:
        return False
    try:
        # Supabase devuelve ISO string con timezone
        if isinstance(blocked_until, str):
            # Parse ISO
            blocked_until = blocked_until.replace("Z", "+00:00")
            bu = datetime.fromisoformat(blocked_until)
        else:
            bu = blocked_until
        return bu > datetime.now(timezone.utc)
    except Exception:
        return False
