"""
ws/session.py — Production-grade Gemini Live AI session handler.

This is the CORE of FlowState AI. It bridges:
  - Browser mic audio (16kHz PCM) → Gemini Live
  - Browser screen screenshots (JPEG, every 20s) → Gemini Live as visual context
  - Gemini Live audio response → Browser (24kHz PCM, plays as speech)
  - Gemini function calls (add_architecture_node) → Browser as JSON commands

WebSocket: ws://localhost:8000/ws/session/{workspace_id}

Binary frame protocol (client → server):
  ┌──────────┬─────────────────────────────────────────┐
  │ Byte 0   │ Bytes 1..N                              │
  │ 0x01     │ Raw PCM int16, 16000Hz, mono            │ ← mic audio
  │ 0x02     │ Raw JPEG bytes (screenshot)             │ ← screen capture
  │ 0x03     │ UTF-8 text                              │ ← typed message
  └──────────┴─────────────────────────────────────────┘

Server → client:
  Binary        → Raw PCM int16 24000Hz (AI voice, play it)
  JSON text     → One of:
    {"type": "status",     "status": "connecting|ai_ready|ai_done|error"}
    {"type": "transcript", "text": "...", "role": "model"}
    {"type": "ping"}
    {"action": "ADD_NODE", "node_type": "...", "node_name": "...", "reasoning": "..."}
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import traceback
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from google import genai
from google.genai import types

import session_store
from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    TAG_AUDIO,
    TAG_IMAGE,
    TAG_TEXT,
)
from gemini_config import build_live_config

logger = logging.getLogger("flowstate.session")

router = APIRouter(tags=["AI Session"])

# ── Shared Gemini async client ─────────────────────────────────────────────────
_client = genai.Client(
    http_options={"api_version": "v1beta"},
    api_key=GEMINI_API_KEY,
)

def _debug_log(msg: str):
    """Failsafe logger that writes to a file AND echoing to terminal."""
    try:
        import datetime
        now = datetime.datetime.now().strftime("%H:%M:%S")
        # 1. Write to file
        with open("flowstate_debug.log", "a", encoding="utf-8") as f:
            f.write(f"[{now}] {msg}\n")
        # 2. Echo to terminal
        logger.info(f"DEBUG | {msg}")
    except:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _log_frame(tag: int, size: int) -> None:
    """Log incoming binary frames at DEBUG level with human-readable info."""
    if tag == TAG_AUDIO:
        logger.debug(f"  [FRAME] 🎙  audio   {size} bytes")
    elif tag == TAG_IMAGE:
        logger.debug(f"  [FRAME] 🖼  canvas  {size / 1024:.1f} KB")
    elif tag == TAG_TEXT:
        logger.debug(f"  [FRAME] 💬  text    {size} bytes")


async def _send_json(ws: WebSocket, payload: dict[str, Any]) -> None:
    """Fire-and-forget JSON frame. Never raises."""
    try:
        _debug_log(f"  [OUT] JSON: {json.dumps(payload)[:100]}...")
        await ws.send_text(json.dumps(payload))
    except Exception:
        pass


async def _send_bytes(ws: WebSocket, data: bytes) -> None:
    """Fire-and-forget binary frame. Never raises."""
    try:
        await ws.send_bytes(data)
    except Exception:
        pass



# ─────────────────────────────────────────────────────────────────────────────
# Frame demultiplexer — runs as a coroutine reading forever from the WS
# ─────────────────────────────────────────────────────────────────────────────

async def _demux_client_frames(
    ws: WebSocket,
    audio_q: asyncio.Queue,
    image_q: asyncio.Queue,
    text_q: asyncio.Queue,
    stop: asyncio.Event,
    wid: str = "?",
) -> None:
    audio_chunks = 0
    image_chunks = 0
    try:
        while not stop.is_set():
            raw = await ws.receive()

            # ── Binary frame ─────────────────────────────────────────────────
            if "bytes" in raw and raw["bytes"]:
                data: bytes = raw["bytes"]
                if len(data) < 2:
                    continue

                tag = data[0]
                payload = data[1:]
                _log_frame(tag, len(payload))

                if tag == TAG_AUDIO:
                    try:
                        audio_q.put_nowait(payload)
                        audio_chunks += 1
                    except asyncio.QueueFull:
                        logger.debug("  [DEMUX] audio queue full, dropping chunk")

                elif tag == TAG_IMAGE:
                    try:
                        image_q.put_nowait(payload)
                        image_chunks += 1
                        logger.info(f"  [CANVAS] 🖼️  snapshot #{image_chunks}  →  {len(payload)/1024:.1f} KB  (ws={wid})")
                    except asyncio.QueueFull:
                        logger.warning("  [CANVAS] image queue full, skipping snapshot")

                elif tag == TAG_TEXT:
                    text = payload.decode("utf-8", errors="replace").strip()
                    if text:
                        logger.info(f"  [CLIENT] 💬 Received typed text: \"{text}\"")
                        _debug_log(f"  [TEXT IN] 💬 Client typed: \"{text}\"")
                        text_q.put_nowait(text)

            # ── Plain text frame (debug clients) ────────────────────────────
            elif "text" in raw and raw["text"]:
                text = raw["text"].strip()
                if text and text != "ping":
                    text_q.put_nowait(text)
                    logger.info(f"  [TEXT]  💬  text frame: \"{text[:80]}\"")
                    _debug_log(f"  [TEXT IN] 💬 Client typed (plain text): \"{text}\"")

    except WebSocketDisconnect:
        logger.info(f"  [DEMUX] 🔌  client disconnected  (ws={wid}, audio={audio_chunks}, screenshots={image_chunks})")
    except Exception as exc:
        _debug_log(f"  [CLIENT] ❌ Error demuxing frames: {exc}")
        logger.error(f"[DEMUX] error: {exc}\n{traceback.format_exc()}")
        stop.set()


# ─────────────────────────────────────────────────────────────────────────────
# Gemini sender — drains all queues and forwards to the Live session
# ─────────────────────────────────────────────────────────────────────────────

async def _send_to_gemini(
    live_session,
    audio_q: asyncio.Queue,
    image_q: asyncio.Queue,
    text_q: asyncio.Queue,
    stop: asyncio.Event,
) -> None:
    audio_sent = 0
    images_sent = 0
    while not stop.is_set():
        flushed_any = False

        # 1. Drain audio
        try:
            while True:
                chunk = audio_q.get_nowait()
                await live_session.send(
                    input=types.LiveClientRealtimeInput(
                        media_chunks=[types.Blob(data=chunk, mime_type="audio/pcm")]
                    )
                )
                audio_sent += 1
                flushed_any = True
        except asyncio.QueueEmpty:
            pass
        except Exception as e:
            logger.warning(f"  [SEND] audio error: {e}")
            _debug_log(f"  [SEND] ❌ Audio error: {e}")

        # 2. Send one screenshot per loop
        try:
            jpeg_bytes = image_q.get_nowait()
            await live_session.send(
                input=types.LiveClientRealtimeInput(
                    media_chunks=[types.Blob(data=jpeg_bytes, mime_type="image/jpeg")]
                )
            )
            images_sent += 1
            logger.info(f"  [SEND] 🖼️  canvas snapshot → Gemini  ({len(jpeg_bytes)//1024} KB, total sent={images_sent})")
            flushed_any = True
        except asyncio.QueueEmpty:
            pass
        except Exception as e:
            logger.warning(f"  [SEND] image error: {e}")
            _debug_log(f"  [SEND] ❌ Image error: {e}")

        # 3. Send text with turn-end
        try:
            text = text_q.get_nowait()
            logger.info(f"  [GEMINI] 📤 Converting text to TTS audio: \"{text}\"")
            _debug_log(f"  [TEXT OUT] 📤 To AI (TTS): \"{text}\"")
            
            try:
                from tts_util import generate_pcm_16k
                loop = asyncio.get_running_loop()
                # Run TTS in an executor to avoid blocking the WebSocket event loop
                pcm_bytes = await loop.run_in_executor(None, generate_pcm_16k, text)
                
                # Send the generated audio to the model just like mic input
                await live_session.send(
                    input=types.LiveClientRealtimeInput(
                        media_chunks=[types.Blob(data=pcm_bytes, mime_type="audio/pcm")]
                    ),
                    end_of_turn=True
                )
            except Exception as e:
                _debug_log(f"  [SEND] ⚠️ Text TTS failed: {e}")
                logger.warning(f"  [SEND] Text TTS failed: {e}")
            
            flushed_any = True
        except asyncio.QueueEmpty:
            pass
        except Exception as e:
            _debug_log(f"  [SEND] ❌ Text queue error: {e}")
            logger.warning(f"  [SEND] text queue error: {e}")

        if not flushed_any:
            await asyncio.sleep(0.02)


# ─────────────────────────────────────────────────────────────────────────────
# Gemini receiver — reads responses and forwards to the browser
# ─────────────────────────────────────────────────────────────────────────────

async def _recv_from_gemini(
    ws: WebSocket,
    live_session,
    session: session_store.SessionState,
    stop: asyncio.Event,
) -> None:
    """
    Receive server-sent events from Gemini Live and relay them to the browser:

    • audio data   → raw PCM bytes (browser plays them in AudioContext)
    • text         → {"type":"transcript","text":"...","role":"model"}
    • tool_call    → handle add_architecture_node → {"action":"ADD_NODE",...}
    • turn_complete → {"type":"status","status":"ai_done"}
    """
    audio_buffer = bytearray()
    has_transcript = False

    try:
        while not stop.is_set():
            async for response in live_session.receive():
                if stop.is_set():
                    break

                # ── AI voice response (raw PCM int16 at 24kHz) ───────────────
                if response.data:
                    logger.debug(f"  [GEMINI] 📥 Received {len(response.data)} bytes of audio data")
                    audio_buffer.extend(response.data)
                    await _send_bytes(ws, response.data)

                # ── AI text / transcript ─────────────────────────────────────
                if response.text:
                    has_transcript = True
                    logger.info(f"  [GEMINI] 📥 Received transcript: \"{response.text}\"")
                    _debug_log(f"  [TEXT IN] 🤖 AI typed: \"{response.text}\"")
                    session.add_turn("model", response.text)
                    await _send_json(ws, {
                        "type": "transcript",
                        "role": "model",
                        "text": response.text,
                    })

                # ── Function / tool call ─────────────────────────────────────
                if response.tool_call:
                    logger.info(f"  [GEMINI] 📥 Received tool call: {response.tool_call}")
                    for fn in response.tool_call.function_calls:
                        await _handle_tool_call(ws, live_session, session, fn)

                # ── Turn complete ────────────────────────────────────────────
                if getattr(response, "server_content", None):
                    sc = response.server_content
                    if getattr(sc, "turn_complete", False):
                        logger.info("  [GEMINI] 📥 Turn complete")
                        
                        # Fallback for transcription if Gemini was silent in text
                        if not has_transcript and len(audio_buffer) > 0:
                            logger.info("  [STT] 🎙️ Missing transcript, starting fallback...")
                            asyncio.create_task(_transcribe_audio_fallback(ws, session, bytes(audio_buffer)))
                        
                        # Reset turn state
                        audio_buffer = bytearray()
                        has_transcript = False
                        await _send_json(ws, {"type": "status", "status": "ai_done"})

    except asyncio.CancelledError:
        pass
    except Exception as exc:
        _debug_log(f"  [RECV] ❌ Error in receiver loop: {exc}")
        logger.error(f"[RECV] error: {exc}\n{traceback.format_exc()}")
    finally:
        stop.set()


async def _transcribe_audio_fallback(ws: WebSocket, session: session_store.SessionState, pcm_bytes: bytes):
    """Fallback: Transcribes AI audio if Live modality failed to provide text."""
    try:
        # Convert raw PCM int16 24kHz to WAV so Gemini can digest it easier
        # Native Audio model should handle this perfectly
        import io, wave
        wav_io = io.BytesIO()
        with wave.open(wav_io, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(pcm_bytes)
        
        wav_bytes = wav_io.getvalue()
        
        prompt = "Transcribe the following AI voice response exactly as spoken. Keep it brief."
        # Use 2.0-flash for fallback STT because native-audio model doesn't support generate_content (404)
        res = await _client.aio.models.generate_content(
            model="gemini-2.0-flash",
            contents=[prompt, types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav")]
        )
        
        text = res.text.strip()
        if text:
            _debug_log(f"  [TEXT IN] 🎙️ AI Voice transcribed: \"{text}\"")
            session.add_turn("model", text)
            await _send_json(ws, {
                "type": "transcript",
                "role": "model",
                "text": text,
            })
    except Exception as e:
        logger.warning(f"  [STT] ❌ Fallback failed: {e}")
        _debug_log(f"  [STT] ❌ Fallback failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# Tool call handler
# ─────────────────────────────────────────────────────────────────────────────

async def _handle_tool_call(ws, live_session, session, fn_call) -> None:
    args = dict(fn_call.args) if fn_call.args else {}

    if fn_call.name == "add_architecture_node":
        node = {
            "action":    "ADD_NODE",
            "node_name": args.get("node_name", "Node"),
            "node_type": args.get("node_type", "server"),
            "reasoning": args.get("reasoning", ""),
            "connected_to": args.get("connected_to", ""),
            "placement": args.get("placement", ""),
        }
        session.add_ai_node(node)
        logger.info(
            f"  [TOOL]  ✨  ADD_NODE  →  name='{node['node_name']}'  "
            f"type='{node['node_type']}'  reason='{node['reasoning'][:60]}'"
        )

        # 1. Push to frontend canvas
        await _send_json(ws, node)

        # 2. Ack back to Gemini
        try:
            await live_session.send(
                input=types.LiveClientToolResponse(
                    function_responses=[
                        types.FunctionResponse(
                            name=fn_call.name,
                            id=fn_call.id,
                            response={"result": f"'{node['node_name']}' added to canvas."},
                        )
                    ]
                )
            )
        except Exception as e:
            logger.warning(f"  [TOOL] ack error: {e}")
            _debug_log(f"  [TOOL] ❌ Ack error: {e}")
    else:
        logger.warning(f"  [TOOL] Unknown function called by Gemini: {fn_call.name}")


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat — keeps the WS alive and lets the client detect drops
# ─────────────────────────────────────────────────────────────────────────────

async def _heartbeat(ws: WebSocket, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await asyncio.sleep(25)
        await _send_json(ws, {"type": "ping"})


# ─────────────────────────────────────────────────────────────────────────────
# MAIN WEBSOCKET ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

async def _run_session_loop(websocket: WebSocket, live: Any, session: Any, stop: asyncio.Event, audio_q: asyncio.Queue, image_q: asyncio.Queue, text_q: asyncio.Queue, workspace_id: str):
    """Main session loop managing concurrent I/O tasks."""
    await _send_json(websocket, {"type": "status", "status": "ai_ready"})
    _debug_log("  [SESSION] ✅ Gemini Live OPEN")
    logger.info(f"  [SESSION] ✅ Gemini Live OPEN")

    # Proactive Welcome Message (AI-driven)
    async def _send_welcome():
        await asyncio.sleep(2.0)
        # We send a "hidden" trigger prompt so Gemini introduces itself naturally
        trigger_prompt = (
            "Greet the user warmly as FlowState, your Senior Cloud Architect. "
            "Ask what we're designing today. Keep it human and concise."
        )
        text_q.put_nowait(trigger_prompt)
        logger.info(f"  [SESSION] 👋 AI Welcome trigger sent")

    asyncio.create_task(_send_welcome())

    tasks = [
        asyncio.ensure_future(_demux_client_frames(websocket, audio_q, image_q, text_q, stop, workspace_id)),
        asyncio.ensure_future(_send_to_gemini(live, audio_q, image_q, text_q, stop)),
        asyncio.ensure_future(_recv_from_gemini(websocket, live, session, stop)),
        asyncio.ensure_future(_heartbeat(websocket, stop)),
    ]
    
    try:
        await stop.wait()
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        _debug_log("  [SESSION] 🛑 Session loop finished")

@router.websocket("/ws/session/{workspace_id}")
async def ai_session(
    websocket: WebSocket,
    workspace_id: str,
    mode: str = Query(default="assisted", description="'personal' or 'assisted'"),
):
    """
    ## FlowState AI Real-Time Session

    Connects the browser to a live Gemini 2.5 Flash session for a specific workspace.
    The browser streams **mic audio (PCM)** and **canvas JPEG snapshots** (every 20s, no screen share).
    Gemini responds with voice and issues `ADD_NODE` commands to draw on the Excalidraw canvas.
    """
    import time
    connect_time = time.time()

    await websocket.accept()
    logger.info(f"\n{'='*58}")
    logger.info(f"  [SESSION] 🔌  New client connected")
    logger.info(f"  [SESSION]     workspace  = {workspace_id}")
    logger.info(f"  [SESSION]     mode       = {mode}")
    logger.info(f"  [SESSION]     client     = {websocket.client}")
    logger.info(f"{'='*58}")

    session = session_store.get_or_create(workspace_id)
    history_ctx = session.history_as_text()
    if session.history:
        logger.info(f"  [SESSION] 📜  Prior history loaded — {len(session.history)} turns")
    else:
        logger.info(f"  [SESSION] 🔆  Fresh session for workspace '{workspace_id}'")

    await _send_json(websocket, {"type": "status", "status": "connecting"})

    audio_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
    image_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=5)
    text_q:  asyncio.Queue[str]   = asyncio.Queue(maxsize=20)
    stop = asyncio.Event()

    try:
        config = build_live_config(history_context=history_ctx)
        _debug_log(f"  [SESSION] 🧠 Opening Gemini Live session (model={GEMINI_MODEL})")
        
        try:
            # First attempt: Prefer combined AUDIO + TEXT if possible
            async with _client.aio.live.connect(model=GEMINI_MODEL, config=config) as live:
                await _run_session_loop(websocket, live, session, stop, audio_q, image_q, text_q, workspace_id)
        except Exception as e:
            err_str = str(e).lower()
            if "invalid argument" in err_str or "1007" in err_str:
                _debug_log(f"  [SESSION] ⚠️ Combined modality failed: {e}. Falling back to AUDIO-only...")
                # Fallback attempt: Standard AUDIO modality
                config.response_modalities = ["AUDIO"]
                async with _client.aio.live.connect(model=GEMINI_MODEL, config=config) as live:
                    await _run_session_loop(websocket, live, session, stop, audio_q, image_q, text_q, workspace_id)
            else:
                _debug_log(f"  [SESSION] ❌ Connection error: {e}")
                raise e

    except Exception as exc:
        msg = str(exc)
        _debug_log(f"  [SESSION] ❌ Fatal error: {msg}")
        logger.error(f"  [SESSION] ❌  Fatal: {msg}")
        try:
            await _send_json(websocket, {"type": "error", "message": msg})
        except:
            pass

    finally:
        stop.set()
        duration = time.time() - connect_time
        logger.info(f"\n{'='*58}")
        logger.info(f"  [SESSION] 🔴  Session closed")
        logger.info(f"  [SESSION]     workspace  = {workspace_id}")
        logger.info(f"  [SESSION]     duration   = {duration:.1f}s")
        logger.info(f"  [SESSION]     ai_nodes   = {len(session.ai_nodes)}")
        logger.info(f"  [SESSION]     history    = {len(session.history)} turns")
        logger.info(f"{'='*58}\n")
