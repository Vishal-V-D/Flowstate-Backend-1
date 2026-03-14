"""
ws/session.py — FlowState (unified send queue, Windows-safe)

ARCHITECTURE:
  All sends to Gemini go through a single asyncio.Queue (gemini_q).
  One dedicated task (_gemini_sender) is the ONLY coroutine that calls
  live_session.send(). This eliminates send contention on Windows
  ProactorEventLoop which was causing 1006 keepalive failures.
"""
from __future__ import annotations
import asyncio, json, logging, traceback, time
from typing import Any
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from google import genai
from google.genai import types
import session_store
from config import GEMINI_API_KEY, GEMINI_MODEL, TAG_AUDIO, TAG_IMAGE, TAG_TEXT
from gemini_config import build_live_config

logger = logging.getLogger("flowstate.session")
router = APIRouter(tags=["AI Session"])

_SILENCE_SM = bytes(6400)    # 200ms @ 16kHz 16-bit mono
_SILENCE_LG = bytes(12800)   # 400ms @ 16kHz 16-bit mono

_client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options={"api_version": "v1alpha"},
)

async def _jx(ws: WebSocket, d: dict) -> None:
    try: await ws.send_text(json.dumps(d))
    except Exception: pass

async def _gemini_sender(live_session, gemini_q: asyncio.Queue,
                         stop: asyncio.Event) -> None:
    """THE only task that calls live_session.send(). Drains gemini_q serially."""
    while not stop.is_set():
        try:
            msg = await asyncio.wait_for(gemini_q.get(), timeout=0.3)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        kind = msg[0]
        try:
            if kind in ("silence", "audio"):
                await live_session.send(input=types.LiveClientRealtimeInput(
                    media_chunks=[types.Blob(data=msg[1], mime_type="audio/pcm;rate=16000")]
                ))
            elif kind == "text":
                await live_session.send(input=types.LiveClientContent(
                    turns=[types.Content(role="user",
                                         parts=[types.Part.from_text(text=msg[1])])],
                    turn_complete=True,
                ))
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"  [SEND] {kind} err: {e}")


async def _audio_out_sender(ws: WebSocket, audio_out_q: asyncio.Queue,
                             stop: asyncio.Event) -> None:
    """Sends AI audio bytes to browser. Isolated so receiver never blocks."""
    while not stop.is_set():
        try:
            data = await asyncio.wait_for(audio_out_q.get(), timeout=0.5)
            await asyncio.wait_for(ws.send_bytes(data), timeout=2.0)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def _silence_keeper(gemini_q: asyncio.Queue, stop: asyncio.Event,
                           ai_speaking: asyncio.Event) -> None:
    """Enqueues silence every 500ms. Never calls live_session directly."""
    ka_count = 0
    while not stop.is_set():
        try:
            await asyncio.sleep(0.5)
            if stop.is_set(): break
            chunk = _SILENCE_LG if ai_speaking.is_set() else _SILENCE_SM
            try: gemini_q.put_nowait(("silence", chunk))
            except asyncio.QueueFull: pass
            ka_count += 1
            if ka_count % 10 == 0:
                logger.info(f"  [KA] 🔇 x{ka_count} "
                            f"{'(AI SPEAKING)' if ai_speaking.is_set() else '(idle)'}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"  [KA] err: {e}")


async def _audio_sender(audio_q: asyncio.Queue, gemini_q: asyncio.Queue,
                         stop: asyncio.Event) -> None:
    """Forwards mic audio into the unified send queue."""
    while not stop.is_set():
        try:
            chunk = await asyncio.wait_for(audio_q.get(), timeout=0.5)
            try: gemini_q.put_nowait(("audio", chunk))
            except asyncio.QueueFull: pass
        except asyncio.TimeoutError: continue
        except asyncio.CancelledError: break
        except Exception as e: logger.warning(f"  [AUDIO] {e}")


async def _text_sender(text_q: asyncio.Queue, gemini_q: asyncio.Queue,
                       stop: asyncio.Event, turn_done: asyncio.Event) -> None:
    """Enqueues user text. Waits for turn_done (max 5s) then sends."""
    while not stop.is_set():
        try:
            text = await asyncio.wait_for(text_q.get(), timeout=0.5)
        except asyncio.TimeoutError: continue
        except asyncio.CancelledError: break

        try:
            await asyncio.wait_for(turn_done.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("  [TEXT] interrupting AI — sending now")
            turn_done.set()

        try:
            logger.info(f"  [TEXT] 💬 → Gemini: \"{text[:120]}\"")
            gemini_q.put_nowait(("text", text))
            turn_done.clear()
        except asyncio.QueueFull:
            logger.warning("  [TEXT] gemini_q full — dropping")
            turn_done.set()
        except Exception as e:
            logger.warning(f"  [TEXT] err: {e}")
            turn_done.set()


async def _demux(ws: WebSocket, audio_q: asyncio.Queue,
                 text_q: asyncio.Queue, stop: asyncio.Event) -> None:
    na = ni = 0
    try:
        while not stop.is_set():
            try: raw = await ws.receive()
            except (WebSocketDisconnect, RuntimeError):
                logger.info(f"  [DEMUX] gone  audio={na} img={ni}")
                stop.set(); return

            if "bytes" in raw and raw["bytes"]:
                data = raw["bytes"]
                if len(data) < 2: continue
                tag, payload = data[0], data[1:]
                if tag == TAG_AUDIO:
                    na += 1
                    try: audio_q.put_nowait(payload)
                    except asyncio.QueueFull: pass
                elif tag == TAG_IMAGE:
                    ni += 1
                elif tag == TAG_TEXT:
                    t = payload.decode("utf-8", errors="replace").strip()
                    if t:
                        logger.info(f"  [CLIENT] 💬 \"{t}\"")
                        try: text_q.put_nowait(t)
                        except asyncio.QueueFull: pass
            elif "text" in raw and raw["text"]:
                t = raw["text"].strip()
                if t and t != "ping":
                    try: text_q.put_nowait(t)
                    except asyncio.QueueFull: pass
    except Exception as e:
        logger.error(f"[DEMUX] {e}\n{traceback.format_exc()}")
        stop.set()


def _parse_node(line: str) -> dict | None:
    s = line.strip()
    if not s.startswith("NODE:"): return None
    try:
        d = json.loads(s[5:].strip())
        return {
            "action":       "ADD_NODE",
            "node_name":    (d.get("node_name")    or "Node").strip(),
            "node_type":    (d.get("node_type")    or "process").strip().lower(),
            "connected_to": (d.get("connected_to") or "").strip(),
            "placement":    (d.get("placement")    or "bottom").strip().lower(),
            "edge_label":   (d.get("edge_label")   or "").strip(),
        }
    except Exception: return None


def _strip_nodes(text: str) -> str:
    return "\n".join(
        l for l in text.splitlines()
        if not l.strip().startswith("NODE:")
    ).strip()


async def _receiver(ws: WebSocket, live_session,
                    session: session_store.SessionState,
                    stop: asyncio.Event, turn_done: asyncio.Event,
                    node_q: asyncio.Queue,
                    ai_speaking: asyncio.Event,
                    audio_out_q: asyncio.Queue) -> None:
    buf = ""; scan_pos = 0
    drawn: set[str] = set()
    try:
        while not stop.is_set():
            async for resp in live_session.receive():
                if stop.is_set(): break

                if resp.data:
                    ai_speaking.set()
                    try: audio_out_q.put_nowait(resp.data)
                    except asyncio.QueueFull: pass

                if resp.server_content:
                    sc = resp.server_content
                    ot = getattr(sc, "output_transcription", None)
                    if ot:
                        chunk = getattr(ot, "text", "") or ""
                        if chunk:
                            buf += chunk
                            logger.info(f"  [GEMINI] 📝 \"{chunk}\"")
                            new = buf[scan_pos:]
                            parts = new.split("\n")
                            for line in parts[:-1]:
                                node = _parse_node(line)
                                if node and node["node_name"] not in drawn:
                                    drawn.add(node["node_name"])
                                    session.add_ai_node(node)
                                    try: node_q.put_nowait(node)
                                    except asyncio.QueueFull: pass
                            scan_pos = len(buf) - len(parts[-1])
                            visible = _strip_nodes(buf)
                            if visible:
                                await _jx(ws, {"type":"transcript","role":"model","text":visible})

                    if getattr(sc, "turn_complete", False):
                        ai_speaking.clear()
                        logger.info("  [GEMINI] ✅ Turn complete")
                        if buf.strip():
                            for line in buf[scan_pos:].splitlines():
                                node = _parse_node(line)
                                if node and node["node_name"] not in drawn:
                                    drawn.add(node["node_name"])
                                    session.add_ai_node(node)
                                    try: node_q.put_nowait(node)
                                    except asyncio.QueueFull: pass
                            display = _strip_nodes(buf) or buf.strip()
                            session.add_turn("model", display)
                            logger.info(f"  [GEMINI] 💬 \"{display[:100]}\"")
                        buf = ""; scan_pos = 0
                        turn_done.set()
                        await _jx(ws, {"type":"status","status":"ai_done"})

                if resp.tool_call:
                    names = [f.name for f in resp.tool_call.function_calls]
                    logger.warning(f"  [GEMINI] ⚠️ tool_call: {names}")

    except asyncio.CancelledError: pass
    except Exception as e:
        logger.error(f"[RECV] {e}\n{traceback.format_exc()}")
    finally:
        turn_done.set()
        stop.set()


async def _drip(ws: WebSocket, node_q: asyncio.Queue, stop: asyncio.Event) -> None:
    count = 0
    while not stop.is_set():
        try: node = await asyncio.wait_for(node_q.get(), timeout=1.0)
        except asyncio.TimeoutError: continue
        except asyncio.CancelledError: break
        await _jx(ws, node)
        count += 1
        logger.info(f"  [CANVAS] 🎨 [{count}] '{node['node_name']}' ({node['node_type']})")
        try: await asyncio.sleep(1.5 if count <= 8 else 1.0)
        except asyncio.CancelledError: break


async def _heartbeat(ws: WebSocket, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try: await asyncio.sleep(20)
        except asyncio.CancelledError: break
        if not stop.is_set():
            await _jx(ws, {"type":"ping"})


async def _run(ws: WebSocket, live: Any, session: Any, stop: asyncio.Event,
               audio_q: asyncio.Queue, text_q: asyncio.Queue) -> None:
    turn_done    = asyncio.Event()
    turn_done.set()
    ai_speaking  = asyncio.Event()
    gemini_q:    asyncio.Queue = asyncio.Queue(maxsize=60)
    node_q:      asyncio.Queue = asyncio.Queue(maxsize=200)
    audio_out_q: asyncio.Queue = asyncio.Queue(maxsize=100)

    await _jx(ws, {"type":"status","status":"ai_ready"})
    logger.info("  [SESSION] ✅ Gemini Live OPEN")

    async def _welcome():
        await asyncio.sleep(0.4)
        history_len = len(session.history)
        node_count  = len(session.ai_nodes)
        import random
        if history_len == 0:
            greetings = [
                "Greet the user with exactly: 'FlowState online — what are we designing today?'",
                "Greet the user with exactly: 'Hey, FlowState here — give me a topic and I will bring it to life on your canvas.'",
                "Greet the user with exactly: 'FlowState ready — what would you like me to diagram?'",
                "Greet the user with exactly: 'Good to meet you, I am FlowState — tell me what to design and I will draw it instantly.'",
                "Greet the user with exactly: 'FlowState at your service — what system or concept should we visualise today?'",
            ]
            text_q.put_nowait(
                random.choice(greetings) +
                " Do NOT draw any nodes. Do NOT say anything else beyond this greeting."
            )
        else:
            last_nodes = [n.get("node_name","") for n in session.ai_nodes[-3:]] if node_count > 0 else []
            node_hint  = (
                f" The canvas has {node_count} nodes" +
                (f" including {', '.join(last_nodes)}." if last_nodes else ".")
            ) if node_count > 0 else ""
            text_q.put_nowait(
                f"Welcome back the user in ONE sentence.{node_hint} "
                f"The session has {history_len} previous exchanges. "
                "Offer to continue where they left off or start something new. "
                "Sound warm and natural. Do NOT draw nodes. One sentence only."
            )
        logger.info("  [SESSION] 👋 Welcome queued")

    asyncio.create_task(_welcome())

    tasks = [
        asyncio.ensure_future(_demux           (ws,        audio_q,    text_q,    stop)),
        asyncio.ensure_future(_gemini_sender   (live,      gemini_q,   stop)),
        asyncio.ensure_future(_audio_sender    (audio_q,   gemini_q,   stop)),
        asyncio.ensure_future(_text_sender     (text_q,    gemini_q,   stop, turn_done)),
        asyncio.ensure_future(_silence_keeper  (gemini_q,  stop,       ai_speaking)),
        asyncio.ensure_future(_receiver        (ws, live, session, stop, turn_done,
                                                node_q, ai_speaking, audio_out_q)),
        asyncio.ensure_future(_audio_out_sender(ws,        audio_out_q, stop)),
        asyncio.ensure_future(_drip            (ws,        node_q,     stop)),
        asyncio.ensure_future(_heartbeat       (ws,        stop)),
    ]
    try:
        await stop.wait()
    finally:
        for t in tasks:
            if not t.done(): t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("  [SESSION] 🛑 Session ended")


@router.websocket("/ws/session/{workspace_id}")
async def ai_session(websocket: WebSocket, workspace_id: str,
                     mode: str = Query(default="assisted")) -> None:
    t0 = time.monotonic()
    await websocket.accept()
    logger.info(f"\n{'='*58}")
    logger.info(f"  [SESSION] 🔌  workspace={workspace_id}  mode={mode}  model={GEMINI_MODEL}")
    logger.info(f"{'='*58}")
    session = session_store.get_or_create(workspace_id)
    await _jx(websocket, {"type":"status","status":"connecting"})
    audio_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=300)
    text_q:  asyncio.Queue[str]   = asyncio.Queue(maxsize=20)
    stop = asyncio.Event()

    backoff = getattr(session, "_connect_backoff", 0)
    if backoff > 0:
        logger.info(f"  [SESSION] ⏳ Network backoff {backoff}s...")
        await _jx(websocket, {"type":"status","status":"connecting","message":f"Retrying in {backoff}s…"})
        await asyncio.sleep(backoff)

    try:
        config = build_live_config(history_context=session.history_as_text())
        logger.info(f"  DEBUG |   [SESSION] 🧠 Connecting (model={GEMINI_MODEL}, api=v1alpha)")
        session._connect_backoff = 0
        async with _client.aio.live.connect(model=GEMINI_MODEL, config=config) as live:
            await _run(websocket, live, session, stop, audio_q, text_q)
    except (TimeoutError, OSError) as exc:
        delay = min(max(getattr(session, "_connect_backoff", 0), 1) * 2, 30)
        session._connect_backoff = delay
        msg = f"Network timeout connecting to Gemini. Will retry in {delay}s."
        logger.error(f"  [SESSION] ❌ {msg}")
        try: await _jx(websocket, {"type":"error","message":msg,"retry_after":delay})
        except Exception: pass
    except Exception as exc:
        session._connect_backoff = 0
        msg = str(exc)
        logger.error(f"  [SESSION] ❌ {msg}\n{traceback.format_exc()}")
        try: await _jx(websocket, {"type":"error","message":msg})
        except Exception: pass
    finally:
        stop.set()
        logger.info(f"  [SESSION] 🔴 Closed — {time.monotonic()-t0:.1f}s  "
                    f"nodes={len(session.ai_nodes)}  history={len(session.history)}")git add .