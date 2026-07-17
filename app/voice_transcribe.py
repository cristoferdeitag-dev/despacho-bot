"""
Transcripción de notas de voz.

Motor primario: faster-whisper LOCAL en el VPS (gratis, mismo modelo que la
voz de Telegram de Web HTM), vía `/root/scripts/transcribe_audio_cli.py`.
Fallback: OpenAI Whisper API si hay OPENAI_API_KEY configurada.

El audio puede llegar de dos formas desde ManyChat:
1. Custom field `last_audio_url` mapeado como `audio_url` en el External
   Request (el camino "oficial").
2. La URL del archivo S3 (manybot-files) como TEXTO del mensaje — es lo que
   manda WhatsApp/ManyChat en la práctica cuando el campo no está mapeado.
   Para eso está `extract_audio_url()`.

Idioma: forzamos `language="es"` porque el bot vende en MX. Esto mejora
WER respecto a auto-detect.
"""
import logging
import re
import subprocess
import tempfile
import time
import httpx
from app.config import settings

logger = logging.getLogger("despacho-bot.voice")

WHISPER_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
LOCAL_WHISPER_CLI = "/root/scripts/transcribe_audio_cli.py"
DOWNLOAD_TIMEOUT = 15.0  # segundos para bajar el audio desde el CDN de ManyChat
TRANSCRIBE_TIMEOUT = 60.0  # segundos para que Whisper procese (largos ~1min audio)
LOCAL_TIMEOUT = 120.0  # el CLI local carga el modelo cada vez (~3-15s típico)
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Whisper hard limit: 25 MB

AUDIO_EXTS = (".ogg", ".mp3", ".m4a", ".wav", ".webm", ".oga")

# URL de audio pegada como texto del mensaje (CDN de ManyChat / WhatsApp)
AUDIO_URL_IN_TEXT = re.compile(
    r"https://\S+\.(?:ogg|oga|mp3|m4a|wav|webm)(?:\?\S*)?", re.IGNORECASE
)


def extract_audio_url(text: str) -> str | None:
    """Si el texto del mensaje contiene una URL de archivo de audio (caso
    típico: ManyChat manda la URL S3 de la nota de voz como last_input_text),
    la regresa. Si no, None."""
    if not text:
        return None
    m = AUDIO_URL_IN_TEXT.search(text)
    return m.group(0) if m else None


def _transcribe_local(audio_bytes: bytes, suffix: str) -> str | None:
    """Transcribe con faster-whisper local (subprocess al venv de whisper)."""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
            tmp.write(audio_bytes)
            tmp.flush()
            r = subprocess.run(
                [LOCAL_WHISPER_CLI, tmp.name],
                capture_output=True,
                text=True,
                timeout=LOCAL_TIMEOUT,
            )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        logger.warning(f"[voice] whisper local rc={r.returncode} err={r.stderr[:200]}")
        return None
    except Exception as e:
        logger.warning(f"[voice] whisper local error: {e}")
        return None


def transcribe_audio_url(audio_url: str, language: str = "es") -> str | None:
    """
    Descarga un audio desde una URL pública (CDN de WhatsApp / ManyChat) y lo
    transcribe usando OpenAI Whisper.

    Args:
        audio_url: URL pública del archivo de audio (ogg/mp3/m4a/wav).
        language: ISO-639-1 hint para Whisper (mejora precisión).

    Returns:
        Texto transcrito, o None si hubo cualquier error (caller decide
        cómo manejar — típicamente responde al usuario "no pude escuchar
        tu nota, ¿me lo puedes escribir?").
    """
    if not audio_url:
        return None

    t0 = time.time()
    try:
        with httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            r = client.get(audio_url)
            r.raise_for_status()
            audio_bytes = r.content
    except Exception as e:
        logger.error(f"[voice] download fallido url={audio_url[:80]} err={e}")
        return None

    if len(audio_bytes) > MAX_AUDIO_BYTES:
        logger.error(
            f"[voice] audio demasiado grande: {len(audio_bytes)} bytes > {MAX_AUDIO_BYTES}"
        )
        return None
    if len(audio_bytes) < 100:
        logger.warning(f"[voice] audio sospechosamente pequeño: {len(audio_bytes)} bytes")
        return None

    # Adivina nombre/MIME del archivo según la URL (Whisper requiere
    # extensión para inferir formato). WhatsApp usa .ogg, ManyChat puede
    # devolver .mp3 / .m4a / .wav.
    lower = audio_url.lower().split("?")[0]
    for ext in AUDIO_EXTS:
        if lower.endswith(ext):
            filename = f"voice{ext}"
            break
    else:
        filename = "voice.ogg"  # default WhatsApp

    # Motor primario: whisper local del VPS (gratis, sin dependencia externa)
    text = _transcribe_local(audio_bytes, suffix=filename[5:])
    if text:
        elapsed = time.time() - t0
        logger.info(
            f"[voice] transcribed(local) size={len(audio_bytes)//1024}KB "
            f"elapsed={elapsed:.1f}s chars={len(text)}"
        )
        return text

    # Fallback: OpenAI Whisper API (solo si hay llave configurada)
    if not settings.OPENAI_API_KEY:
        logger.error("[voice] whisper local falló y no hay OPENAI_API_KEY de respaldo")
        return None

    files = {"file": (filename, audio_bytes, "application/octet-stream")}
    data = {"model": settings.WHISPER_MODEL, "language": language}
    headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}

    try:
        with httpx.Client(timeout=TRANSCRIBE_TIMEOUT) as client:
            r = client.post(WHISPER_ENDPOINT, headers=headers, files=files, data=data)
        if r.status_code != 200:
            logger.error(
                f"[voice] Whisper HTTP {r.status_code}: {r.text[:300]}"
            )
            return None
        text = (r.json().get("text") or "").strip()
        elapsed = time.time() - t0
        size_kb = len(audio_bytes) // 1024
        logger.info(
            f"[voice] transcribed size={size_kb}KB elapsed={elapsed:.1f}s "
            f"chars={len(text)}"
        )
        return text or None
    except Exception as e:
        logger.exception(f"[voice] Whisper error: {e}")
        return None
