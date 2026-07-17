from pydantic import BaseModel
from typing import Optional


class ManychatUserProfile(BaseModel):
    id: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    status: Optional[str] = None


class ManychatWebhookPayload(BaseModel):
    """
    Payload enviado por ManyChat cuando el usuario manda un mensaje.
    En ManyChat configuras los campos a enviar en 'External Request'.
    Recomendado mínimo: user_id, text, first_name, phone, channel.
    """
    user_id: str
    text: str = ""  # puede venir vacío si solo hay nota de voz
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    channel: Optional[str] = "whatsapp"  # whatsapp | instagram | messenger
    # URL de nota de voz (custom field last_audio_url de ManyChat). En la
    # práctica la URL suele llegar dentro de `text` — ver extract_audio_url().
    audio_url: Optional[str] = None


class BotReply(BaseModel):
    """
    Respuesta que le devolvemos a ManyChat para que la mande al usuario.
    Estructura compatible con ManyChat Dynamic Content.
    """
    version: str = "v2"
    content: dict
