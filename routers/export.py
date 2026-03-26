"""
routers/export.py — Export workspace architecture to Terraform or Markdown.

Uses a standard Gemini Pro text call (NOT Live) to generate the export.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from google import genai

import session_store
from config import GEMINI_API_KEY

logger = logging.getLogger("flowstate.export")

router = APIRouter(prefix="/api/workspaces", tags=["Export"])

_client = None
try:
    _client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    logger.error(f"Failed to initialize Gemini export client: {e}")
    import traceback
    traceback.print_exc()

EXPORT_MODEL = "models/gemini-2.0-flash"  # fast text model for export


class ExportRequest(BaseModel):
    format: str = "terraform"   # "terraform" | "markdown" | "mermaid"


def _build_export_prompt(format: str, nodes: list[dict], canvas: dict) -> str:
    node_list = "\n".join(
        f"- {n.get('name', 'Unknown')} (type: {n.get('type', 'unknown')}): {n.get('reasoning', '')}"
        for n in nodes
    ) or "No AI-added nodes yet."

    excalidraw_elements = canvas.get("elements", [])
    elem_summary = f"{len(excalidraw_elements)} elements on canvas" if excalidraw_elements else "empty canvas"

    prompts = {
        "terraform": f"""\
You are a Senior Cloud Architect. Based on the following system architecture, \
generate a clean, production-ready Terraform HCL configuration. Use AWS resources \
by default unless the node names imply otherwise. Include comments explaining each resource.

Architecture nodes:
{node_list}

Canvas state: {elem_summary}

Output ONLY valid Terraform HCL code with comments. No markdown fences.""",

        "markdown": f"""\
You are a Senior Cloud Architect. Based on the following system architecture, \
generate a comprehensive architecture decision record (ADR) in Markdown format. \
Include sections: Overview, Components, Data Flow, Scalability Considerations, \
Security Notes, and Next Steps.

Architecture nodes:
{node_list}

Canvas state: {elem_summary}

Output clean Markdown only.""",

        "mermaid": f"""\
You are a Senior Cloud Architect. Based on the following system architecture, \
generate a Mermaid diagram definition that represents the system.

Architecture nodes:
{node_list}

Canvas state: {elem_summary}

Output ONLY a valid Mermaid diagram (graph TD or C4Context). No markdown fences.""",
    }

    return prompts.get(format, prompts["markdown"])


@router.post(
    "/{workspace_id}/export",
    response_class=PlainTextResponse,
    summary="Export architecture to Terraform / Markdown / Mermaid",
    description=(
        "Triggers a Gemini Pro text call that analyzes the current canvas state "
        "and AI-added nodes, then generates a downloadable artifact. "
        "Supported formats: `terraform`, `markdown`, `mermaid`."
    ),
)
async def export_workspace(workspace_id: str, body: ExportRequest):
    s = session_store.get(workspace_id)
    if not s:
        raise HTTPException(status_code=404, detail="Workspace not found")

    valid_formats = {"terraform", "markdown", "mermaid"}
    if body.format not in valid_formats:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid format. Choose one of: {', '.join(valid_formats)}",
        )

    prompt = _build_export_prompt(body.format, s.ai_nodes, s.canvas_state)

    try:
        response = _client.models.generate_content(
            model=EXPORT_MODEL,
            contents=prompt,
        )
        text = response.text or ""
        logger.info(f"[EXPORT] workspace={workspace_id} format={body.format} chars={len(text)}")
        return PlainTextResponse(content=text, media_type="text/plain")
    except Exception as e:
        logger.error(f"[EXPORT] Error: {e}")
        raise HTTPException(status_code=500, detail=f"Gemini export failed: {e}")
