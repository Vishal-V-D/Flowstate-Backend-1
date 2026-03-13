"""
main.py — FlowState AI Backend

Run:
    uvicorn main:app --reload --port 8000

Swagger UI: http://localhost:8000/docs
WebSocket:  ws://localhost:8000/ws/session/{workspace_id}?mode=assisted
"""
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import ALLOWED_ORIGINS, GEMINI_API_KEY, GEMINI_MODEL
from routers.workspaces import router as workspace_router
from routers.export import router as export_router
from ws.session import router as session_router

# ── Logging setup ─────────────────────────────────────────────────────────────
class FlushHandler(logging.FileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  —  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        FlushHandler("flowstate.log", mode="a", encoding="utf-8")
    ],
)
logger = logging.getLogger("flowstate.main")


def _validate_startup() -> None:
    """Run pre-flight checks and log the results clearly."""

    logger.info("=" * 60)
    logger.info("  FlowState AI Backend  —  Starting up")
    logger.info("=" * 60)

    # ── API Key check ──────────────────────────────────────────────
    if not GEMINI_API_KEY:
        logger.error("❌  GEMINI_API_KEY is missing!")
        logger.error("    Create a .env file with: GEMINI_API_KEY=your_key")
        logger.error("    Get a key at: https://aistudio.google.com/app/apikey")
    elif len(GEMINI_API_KEY) < 20:
        logger.warning("⚠️   GEMINI_API_KEY looks too short — double-check it")
    else:
        masked = GEMINI_API_KEY[:6] + "..." + GEMINI_API_KEY[-4:]
        logger.info(f"✅  GEMINI_API_KEY detected  →  {masked}")

    # ── Model ──────────────────────────────────────────────────────
    logger.info(f"🤖  Gemini model          →  {GEMINI_MODEL}")

    # ── Routes ────────────────────────────────────────────────────
    logger.info("🔌  WebSocket endpoint    →  /ws/session/{{workspace_id}}?mode=assisted")
    logger.info("📡  REST API              →  /api/workspaces")
    logger.info("📖  Swagger UI            →  http://localhost:8000/docs")
    logger.info("📖  ReDoc                 →  http://localhost:8000/redoc")
    logger.info("🌐  CORS allowed origins  →  " + ", ".join(ALLOWED_ORIGINS))
    logger.info("=" * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_startup()
    logger.info("🚀  Application ready — waiting for connections")
    yield
    logger.info("🛑  FlowState AI Backend shutting down — bye!")


app = FastAPI(
    title="FlowState AI Backend",
    description="""
Real-time AI architecture collaboration backend.

## Core Feature
Connect to `/ws/session/{workspace_id}?mode=assisted` to start a **Gemini 2.5 Flash Live** session.
The browser streams **mic audio (PCM)** and **Excalidraw canvas JPEG snapshots** (every 20 seconds, no screen share needed).
Gemini responds with voice and issues `ADD_NODE` commands to draw on the canvas.

## Binary Frame Protocol (browser → server)
| Tag byte | Content |
|----------|---------|
| `0x01`   | Mic audio — raw PCM int16, 16 000 Hz, mono |
| `0x02`   | Canvas screenshot — raw JPEG bytes |
| `0x03`   | Typed text — UTF-8 string |

## Server → Browser
| Frame | Meaning |
|-------|---------|
| Binary | AI voice — raw PCM int16, 24 000 Hz |
| `{"type":"status","status":"ai_ready"}` | Gemini session open |
| `{"type":"transcript","text":"..."}` | AI text |
| `{"action":"ADD_NODE",...}` | Draw node on canvas |
| `{"type":"ping"}` | Heartbeat every 25s |
    """,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(session_router)
app.include_router(workspace_router)
app.include_router(export_router)


@app.get("/health", tags=["System"])
def health():
    key_ok = bool(GEMINI_API_KEY and len(GEMINI_API_KEY) >= 20)
    return {
        "status": "ok",
        "version": "1.0.0",
        "gemini_key_set": key_ok,
        "model": GEMINI_MODEL,
    }


@app.get("/", tags=["System"])
def root():
    return {
        "service": "FlowState AI Backend ✨",
        "docs": "/docs",
        "websocket": "ws://localhost:8000/ws/session/{workspace_id}?mode=assisted",
    }