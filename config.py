"""
config.py — Centralised settings for FlowState backend.
Loads environment variables and exposes typed constants.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Gemini ──────────────────────────────────────────────────────────────────
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL: str = "gemini-2.5-flash-native-audio-preview-12-2025"

# ── Audio ────────────────────────────────────────────────────────────────────
SEND_SAMPLE_RATE: int = 16_000   # mic → Gemini
RECEIVE_SAMPLE_RATE: int = 24_000  # Gemini → speakers
AUDIO_CHANNELS: int = 1
CHUNK_SIZE: int = 1_024

# ── Vision ────────────────────────────────────────────────────────────────────
CANVAS_SNAPSHOT_INTERVAL: float = 15.0  # matches frontend logic

# ── WebSocket frame tags ──────────────────────────────────────────────────────
TAG_AUDIO: int = 0x01   # raw PCM int16 from mic
TAG_IMAGE: int = 0x02   # canvas JPEG snapshot (base64-encoded bytes after tag)
TAG_TEXT: int  = 0x03   # UTF-8 text message

# ── Session ───────────────────────────────────────────────────────────────────
MAX_HISTORY_TURNS: int = 20   # sliding window for chat history

# ── CORS ──────────────────────────────────────────────────────────────────────
ALLOWED_ORIGINS: list[str] = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
