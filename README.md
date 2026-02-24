# FlowState AI — Backend

Real-time AI architecture collaboration. Your browser mic + screen → Gemini 2.5 Flash Live → voice response + draws nodes on your Excalidraw canvas.

---

## Prerequisites

- Python 3.10+  
- A Google AI Studio API key → [aistudio.google.com](https://aistudio.google.com)
- Node.js 18+ (for the `kira` frontend)

---

## 1 · Environment Setup

```bash
# Copy the example env file
cp .env.example .env
```

Open `.env` and set your key:
```env
GEMINI_API_KEY=YOUR_KEY_HERE
```

Get your key at → https://aistudio.google.com/app/apikey

---

## 2 · Install Dependencies

```bash
# (Optional but recommended) Create a virtual environment
python -m venv venv

# Activate it
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

# Install packages
pip install -r requirements.txt
```

---

## 3 · Run the Backend

```bash
uvicorn main:app --reload --port 8000
```

You'll see:
```
INFO  🚀 FlowState AI Backend starting…
INFO  Application startup complete.
INFO  Uvicorn running on http://127.0.0.1:8000
```

| URL | What it is |
|-----|-----------|
| http://localhost:8000/docs | **Swagger UI** — interactive API explorer |
| http://localhost:8000/redoc | ReDoc documentation |
| http://localhost:8000/health | Health check |

---

## 4 · Run the Frontend (kira)

In a **separate terminal**:

```bash
cd ..\kira          # adjust path as needed
npm install
npm run dev
```

Open http://localhost:3000

---

## 5 · Using FlowState AI

1. Click **New Workspace** on the library page
2. Choose **"With AI Assistance"**
   - Browser will ask for **Microphone** permission → Allow
   - Browser will ask for **Screen Share** permission → Share your whole screen
3. The AI connects and says hello
4. **Speak naturally** — *"Add a Redis cache between the API and the database"*
5. FlowState AI:
   - Speaks back explaining the decision
   - Automatically draws the node on your Excalidraw canvas

---

## Architecture

```
kira (Next.js)                         flowstate-backend (FastAPI)
──────────────                         ──────────────────────────────────────
Workspace page (mode=assisted)
  └─ useAISession.ts hook
       │
       │  Binary WS frame 0x01 ──────► _demux_client_frames()
       │  (mic PCM, 16kHz)              │
       │                                ▼
       │  Binary WS frame 0x02 ──────► _send_to_gemini()
       │  (screen JPEG, every 20s)      │
       │                                ▼
       │                        Gemini 2.5 Flash Live API
       │                          ┌─── AI voice (PCM 24kHz)
       │  ◄── Binary (raw PCM) ───┘
       │                          ├─── Text transcript
       │  ◄── JSON transcript ────┘
       │                          └─── Tool call: add_architecture_node()
       │  ◄── JSON ADD_NODE ──────
       │
       └─ handleAddNode()
          → Excalidraw insertElement()
          → Colour-coded node appears on canvas
```

---

## WebSocket Protocol

**Endpoint:** `ws://localhost:8000/ws/session/{workspace_id}?mode=assisted`

### Browser → Server (binary frames)
| Tag byte | Payload | Frequency |
|----------|---------|-----------|
| `0x01` | Raw PCM int16, 16000 Hz, mono | Continuous (mic) |
| `0x02` | Raw JPEG bytes (screenshot) | Every 20 seconds |
| `0x03` | UTF-8 text string | On demand |

### Server → Browser
| Frame type | Meaning |
|------------|---------|
| Binary | AI voice — raw PCM int16, 24000 Hz, play immediately |
| `{"type":"status","status":"ai_ready"}` | Gemini session established |
| `{"type":"transcript","text":"..."}` | AI text response |
| `{"action":"ADD_NODE","node_name":"Redis Cache","node_type":"cache","reasoning":"..."}` | Draw on canvas |
| `{"type":"ping"}` | Heartbeat (every 25s) |

---

## REST Endpoints

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/api/workspaces` | List all saved workspaces |
| `POST` | `/api/workspaces` | Create a workspace record |
| `GET` | `/api/workspaces/{id}` | Get workspace + canvas + AI nodes |
| `PUT` | `/api/workspaces/{id}` | Save canvas state |
| `DELETE` | `/api/workspaces/{id}` | Delete workspace |
| `POST` | `/api/workspaces/{id}/export` | Export to Terraform / Markdown / Mermaid |

### Export Usage

```bash
curl -X POST http://localhost:8000/api/workspaces/abc123/export \
  -H "Content-Type: application/json" \
  -d '{"format": "terraform"}'
```

Supported formats: `terraform` · `markdown` · `mermaid`

---

## Project Structure

```
flowstate-backend/
├── main.py                ← FastAPI entry point, Swagger UI
├── config.py              ← Constants (model, sample rates, frame tags)
├── gemini_config.py       ← LiveConnectConfig, system prompt, tool declarations
├── session_store.py       ← In-memory workspace sessions + chat history
├── ws/
│   ├── session.py        ← /ws/session/{id} — Core Gemini Live bridge ★
│   └── flowstate.py      ← Legacy bridge (kept for reference)
├── routers/
│   ├── workspaces.py     ← CRUD REST endpoints
│   └── export.py         ← Terraform / Markdown / Mermaid export
├── requirements.txt
├── .env.example
└── README.md
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `GEMINI_API_KEY not set` | Add key to `.env` file |
| `pip ResolutionImpossible` | Run `pip install -r requirements.txt --upgrade` |
| Browser blocks mic/screen | Use `https://` or `localhost` (not local IP) |
| `ImageCapture not defined` | Use Chrome or Edge (Firefox has limited ImageCapture support) |
| AI doesn't respond | Check backend logs — confirm `[SESSION] Gemini Live session open` appears |
