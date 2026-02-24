"""
gemini_config.py — Builds the LiveConnectConfig for FlowState AI.
The system prompt positions Gemini as a Senior Cloud Architect that speaks
back verbally AND calls tools to draw nodes on the Excalidraw canvas.
"""
from google.genai import types

from config import GEMINI_MODEL


# ── Tool definition — add_architecture_node ───────────────────────────────────
ADD_NODE_DECLARATION = types.FunctionDeclaration(
    name="add_architecture_node",
    description=(
        "Add a new infrastructure or architecture component node to the user's "
        "system design canvas. Call this AFTER you have explained your reasoning "
        "verbally so the user understands why you are adding the component."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "node_name": types.Schema(
                type=types.Type.STRING,
                description="Human-readable label shown on the node, e.g. 'Redis Cache', 'API Gateway'.",
            ),
            "node_type": types.Schema(
                type=types.Type.STRING,
                description=(
                    "Category of the node. One of: "
                    "client, server, database, cache, queue, gateway, cdn, "
                    "loadbalancer, auth, storage, microservice, external_api, firewall, dns"
                ),
            ),
            "reasoning": types.Schema(
                type=types.Type.STRING,
                description="Brief reason why this node is being added.",
            ),
            "connected_to": types.Schema(
                type=types.Type.STRING,
                description="Optional. The exact name of an existing node on the canvas this new node should connect to.",
            ),
            "placement": types.Schema(
                type=types.Type.STRING,
                description="Optional. Where to place this node relative to the connected node: 'top', 'bottom', 'left', or 'right'.",
            ),
        },
        required=["node_name", "node_type", "reasoning"],
    ),
)

FLOWSTATE_TOOLS = types.Tool(function_declarations=[ADD_NODE_DECLARATION])

SYSTEM_INSTRUCTION = """\
You are FlowState — an AI Senior Cloud Architect collaborating in real-time \
with an engineer on a system design whiteboard.

WHAT YOU RECEIVE:
- The engineer's voice via PCM audio (continuous, 16kHz mic stream)
- A JPEG screenshot of their Excalidraw whiteboard every 20 seconds so you can \
  see exactly what is currently drawn

YOUR ROLE:
- Be the engineer's JARVIS — proactive, concise, technically brilliant
- When you see the whiteboard, immediately understand the architecture at a glance
- When the engineer asks you to add a component (e.g. "add a db", "add Redis"), do THREE things:
    1. Immediately analyze the most recent canvas snapshot to understand the current architecture.
    2. Speak your reasoning in 1-2 short sentences, explicitly explaining WHY it is needed based on the current canvas.
    3. Call `add_architecture_node` to draw it on their canvas.
- Never just describe adding something — always call the tool.
- If you spot an architectural issue in the screenshot (SPOF, missing auth, \
  no caching layer), proactively mention it unprompted.
- Keep responses under 30 words unless the engineer asks for detail.
- Use natural, confident, senior-engineer language.

TOOL AVAILABLE:
- `add_architecture_node(node_name, node_type, reasoning, connected_to, placement)` — draws a labelled node \
  on the canvas. connected_to and placement are optional. Call this AFTER your verbal explanation.

node_type must be one of: \
client, server, database, cache, queue, gateway, cdn, loadbalancer, \
auth, storage, microservice, external_api, firewall, dns
"""


def build_live_config(history_context: str = "") -> types.LiveConnectConfig:
    """
    Build a fresh LiveConnectConfig. Inject prior session history if available.
    """
    system_text = SYSTEM_INSTRUCTION
    if history_context and history_context != "No prior conversation.":
        system_text += f"\n\n--- Prior conversation history ---\n{history_context}\n---"

    return types.LiveConnectConfig(
        # gemini-live-2.5-flash-native-audio only supports AUDIO modality
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name="Zephyr",
                )
            )
        ),
        system_instruction=types.Content(
            parts=[types.Part.from_text(text=system_text)],
            role="user",
        ),
        tools=[FLOWSTATE_TOOLS],
    )
