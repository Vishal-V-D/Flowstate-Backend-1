"""
ws/flowstate.py — Core real-time WebSocket bridge (Python 3.10 compatible).

Protocol (binary frames from frontend → backend):
  Byte 0: tag
    0x01 = raw PCM audio (int16, 16kHz, mono)
    0x02 = canvas JPEG snapshot (remainder is base64-encoded JPEG bytes)
    0x03 = UTF-8 text message
  Bytes 1..N: payload

Responses from backend → frontend:
  Binary: raw PCM audio bytes (24kHz) from Gemini TTS
  JSON text: {"type": "transcript", "text": "..."}
             {"action": "ADD_NODE", "type": "...", "name": "...", "reasoning": "..."}
             {"type": "error", "message": "..."}
             {"type": "status", "status": "connected" | "ai_ready" | "ai_speaking" | "ai_done"}
"""
from __future__ import annotations

import asyncio
import json
import logging
import traceback
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
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

logger = logging.getLogger("flowstate.ws")

router = APIRouter()

# Gemini async client (shared across connections)
_gemini_client = genai.Client(
    http_options={"api_version": "v1beta"},
    api_key=GEMINI_API_KEY,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _safe_send_bytes(ws: WebSocket, data: bytes) -> None:
    try:
        await ws.send_bytes(data)
    except Exception:
        pass


async def _safe_send_json(ws: WebSocket, payload: dict) -> None:
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        pass


# ── Main WebSocket handler ─────────────────────────────────────────────────────

@router.websocket("/ws/flowstate")
async def flowstate_ws(websocket: WebSocket, workspace_id: str = "default"):
    """
    Bidirectional real-time bridge between the FlowState frontend and Gemini Live.

    Query params:
      workspace_id — ties the connection to a session store entry
    """
    await websocket.accept()
    logger.info(f"[WS] Client connected — workspace_id={workspace_id}")

    session = session_store.get_or_create(workspace_id)
    history_ctx = session.history_as_text()

    await _safe_send_json(websocket, {"type": "status", "status": "connected"})

    to_gemini_q: asyncio.Queue = asyncio.Queue(maxsize=10)
    stop_event = asyncio.Event()

    # ── Task: receive frames from frontend ─────────────────────────────────────
    async def recv_from_client() -> None:
        try:
            while not stop_event.is_set():
                raw = await websocket.receive()

                if "bytes" in raw and raw["bytes"]:
                    data: bytes = raw["bytes"]
                    if len(data) < 1:
                        continue
                    tag = data[0]
                    payload = data[1:]

                    if tag == TAG_AUDIO:
                        msg = {"data": payload, "mime_type": "audio/pcm"}
                        try:
                            to_gemini_q.put_nowait(msg)
                        except asyncio.QueueFull:
                            pass

                    elif tag == TAG_IMAGE:
                        try:
                            import base64
                            image_b64 = payload.decode("utf-8")
                            image_bytes = base64.b64decode(image_b64)
                            msg = {"mime_type": "image/jpeg", "data": image_bytes}
                            to_gemini_q.put_nowait(msg)
                        except Exception as e:
                            logger.warning(f"[WS] Bad image frame: {e}")

                    elif tag == TAG_TEXT:
                        text = payload.decode("utf-8", errors="replace")
                        session.add_turn("user", text)
                        try:
                            to_gemini_q.put_nowait({"text": text})
                        except asyncio.QueueFull:
                            pass

                elif "text" in raw and raw["text"]:
                    text = raw["text"]
                    session.add_turn("user", text)
                    try:
                        to_gemini_q.put_nowait({"text": text})
                    except asyncio.QueueFull:
                        pass

        except WebSocketDisconnect:
            logger.info(f"[WS] Client disconnected — workspace_id={workspace_id}")
        except Exception as e:
            logger.error(f"[WS] recv_from_client error: {e}")
        finally:
            stop_event.set()
            try:
                to_gemini_q.put_nowait(None)  # unblock send_to_gemini
            except asyncio.QueueFull:
                pass

    # ── Task: drain queue → Gemini session ─────────────────────────────────────
    async def send_to_gemini(live_session) -> None:
        while not stop_event.is_set():
            try:
                msg = await asyncio.wait_for(to_gemini_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if msg is None:
                break
            try:
                if "data" in msg and "mime_type" in msg:
                    await live_session.send(
                        input=types.LiveClientRealtimeInput(
                            media_chunks=[
                                types.Blob(data=msg["data"], mime_type=msg["mime_type"])
                            ]
                        )
                    )
                elif "text" in msg:
                    await live_session.send(input=msg["text"], end_of_turn=True)
            except Exception as e:
                logger.warning(f"[WS] send_to_gemini error: {e}")

    # ── Task: receive Gemini responses → frontend ─────────────────────────────
    async def recv_from_gemini(live_session) -> None:
        try:
            while not stop_event.is_set():
                turn = live_session.receive()
                async for response in turn:
                    if stop_event.is_set():
                        break

                    # Audio bytes (TTS) → stream back as binary
                    if response.data:
                        await _safe_send_bytes(websocket, response.data)

                    # Text transcript
                    if response.text:
                        text = response.text
                        session.add_turn("model", text)
                        await _safe_send_json(websocket, {
                            "type": "transcript",
                            "text": text,
                        })

                    # Function / tool call
                    if response.tool_call:
                        for fn_call in response.tool_call.function_calls:
                            if fn_call.name == "add_architecture_node":
                                args = fn_call.args or {}
                                node_payload = {
                                    "action": "ADD_NODE",
                                    "type": args.get("node_type", "server"),
                                    "name": args.get("node_name", "Node"),
                                    "reasoning": args.get("reasoning", ""),
                                }
                                session.add_ai_node(node_payload)
                                logger.info(f"[TOOL] {node_payload}")

                                # Forward to frontend
                                await _safe_send_json(websocket, node_payload)

                                # Ack back to Gemini
                                try:
                                    await live_session.send(
                                        input=types.LiveClientToolResponse(
                                            function_responses=[
                                                types.FunctionResponse(
                                                    name=fn_call.name,
                                                    id=fn_call.id,
                                                    response={
                                                        "result": "Node added to canvas successfully."
                                                    },
                                                )
                                            ]
                                        )
                                    )
                                except Exception as e:
                                    logger.warning(f"[WS] tool response ack error: {e}")

                await _safe_send_json(websocket, {"type": "status", "status": "ai_done"})

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[WS] recv_from_gemini error: {e}\n{traceback.format_exc()}")
        finally:
            stop_event.set()

    # ── Task: heartbeat ────────────────────────────────────────────────────────
    async def heartbeat() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(20)
            try:
                await websocket.send_text(json.dumps({"type": "ping"}))
            except Exception:
                stop_event.set()
                break

    # ── Connect to Gemini Live and run all tasks ───────────────────────────────
    tasks = []
    try:
        live_config = build_live_config(history_context=history_ctx)
        async with _gemini_client.aio.live.connect(
            model=GEMINI_MODEL, config=live_config
        ) as live_session:
            await _safe_send_json(websocket, {"type": "status", "status": "ai_ready"})
            logger.info(f"[WS] Gemini Live opened — workspace_id={workspace_id}")

            # Python 3.10 compatible: gather tasks manually
            tasks = [
                asyncio.ensure_future(recv_from_client()),
                asyncio.ensure_future(send_to_gemini(live_session)),
                asyncio.ensure_future(recv_from_gemini(live_session)),
                asyncio.ensure_future(heartbeat()),
            ]

            # Wait until stop_event fires (disconnect or error)
            await stop_event.wait()

    except Exception as e:
        logger.error(f"[WS] Gemini connection error: {e}")
        await _safe_send_json(websocket, {"type": "error", "message": str(e)})
    finally:
        stop_event.set()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(f"[WS] Session closed — workspace_id={workspace_id}")
