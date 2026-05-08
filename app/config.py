import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
    MANYCHAT_API_TOKEN = os.getenv("MANYCHAT_API_TOKEN", "")
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
    LLM_MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001")
    PORT = int(os.getenv("PORT", "8000"))

    # IDs específicos del workspace de ManyChat del Despacho. Se llenan después
    # de crear el flow + custom fields (ver README).
    MANYCHAT_AI_RESPONSE_FIELD_ID = os.getenv("MANYCHAT_AI_RESPONSE_FIELD_ID", "")
    MANYCHAT_CONVERSATION_ENDED_FIELD_ID = os.getenv("MANYCHAT_CONVERSATION_ENDED_FIELD_ID", "")
    MANYCHAT_ADMIN_SUBSCRIBER_ID = os.getenv("MANYCHAT_ADMIN_SUBSCRIBER_ID", "")


settings = Settings()
