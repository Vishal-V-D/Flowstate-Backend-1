"""
config.py — Centralised settings for FlowState backend.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Gemini ──────────────────────────────────────────────────────────────────
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")

# ✅ Keep the native audio model (best voice quality + supports tools)
# ✅ API version changed to v1alpha in session.py (v1beta doesn't support bidiGenerateContent)
# gemini-2.5-flash-native-audio-preview-12-2025: higher quality but 20-30s thinking = 1006 timeouts
GEMINI_MODEL: str = "gemini-2.5-flash-native-audio-preview-12-2025"

# ── Audio ────────────────────────────────────────────────────────────────────
SEND_SAMPLE_RATE: int = 16_000
RECEIVE_SAMPLE_RATE: int = 24_000
AUDIO_CHANNELS: int = 1
CHUNK_SIZE: int = 1_024

# ── Vision ────────────────────────────────────────────────────────────────────
CANVAS_SNAPSHOT_INTERVAL: float = 15.0

# ── WebSocket frame tags ──────────────────────────────────────────────────────
TAG_AUDIO: int = 0x01
TAG_IMAGE: int = 0x02
TAG_TEXT:  int = 0x03

# ── Session ───────────────────────────────────────────────────────────────────
MAX_HISTORY_TURNS: int = 20

# ── CORS ──────────────────────────────────────────────────────────────────────
ALLOWED_ORIGINS: list[str] = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
