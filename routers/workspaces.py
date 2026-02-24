"""
routers/workspaces.py — REST CRUD for FlowState workspaces.

All data lives in-memory via session_store. Swap out for a DB layer later.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import session_store

router = APIRouter(prefix="/api/workspaces", tags=["Workspaces"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class WorkspaceCreate(BaseModel):
    title: str = "Untitled"
    subtitle: str = ""


class WorkspaceUpdate(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    canvas_state: dict[str, Any] | None = None


class WorkspaceOut(BaseModel):
    workspace_id: str
    title: str
    subtitle: str
    created_at: float
    updated_at: float
    node_count: int
    ai_node_count: int


class WorkspaceDetail(WorkspaceOut):
    canvas_state: dict[str, Any]
    ai_nodes: list[dict[str, Any]]
    history: list[dict[str, Any]]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get(
    "",
    response_model=list[WorkspaceOut],
    summary="List all workspaces",
    description="Returns all workspace stubs sorted by last edit (newest first).",
)
def list_workspaces():
    sessions = session_store.list_all()
    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    return [
        WorkspaceOut(
            workspace_id=s.workspace_id,
            title=s.title,
            subtitle=s.subtitle,
            created_at=s.created_at,
            updated_at=s.updated_at,
            node_count=len(s.canvas_state.get("elements", [])),
            ai_node_count=len(s.ai_nodes),
        )
        for s in sessions
    ]


@router.post(
    "",
    response_model=WorkspaceOut,
    status_code=201,
    summary="Create a new workspace",
)
def create_workspace(body: WorkspaceCreate):
    import uuid
    ws_id = str(uuid.uuid4())[:8]
    s = session_store.get_or_create(ws_id, title=body.title, subtitle=body.subtitle)
    return WorkspaceOut(
        workspace_id=s.workspace_id,
        title=s.title,
        subtitle=s.subtitle,
        created_at=s.created_at,
        updated_at=s.updated_at,
        node_count=0,
        ai_node_count=0,
    )


@router.get(
    "/{workspace_id}",
    response_model=WorkspaceDetail,
    summary="Get full workspace state",
)
def get_workspace(workspace_id: str):
    s = session_store.get(workspace_id)
    if not s:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return WorkspaceDetail(
        workspace_id=s.workspace_id,
        title=s.title,
        subtitle=s.subtitle,
        created_at=s.created_at,
        updated_at=s.updated_at,
        node_count=len(s.canvas_state.get("elements", [])),
        ai_node_count=len(s.ai_nodes),
        canvas_state=s.canvas_state,
        ai_nodes=s.ai_nodes,
        history=[
            {"role": t.role, "content": t.content, "timestamp": t.timestamp}
            for t in s.history
        ],
    )


@router.put(
    "/{workspace_id}",
    response_model=WorkspaceOut,
    summary="Save canvas state",
    description="Called by the frontend when the user saves or when auto-save triggers.",
)
def update_workspace(workspace_id: str, body: WorkspaceUpdate):
    s = session_store.get(workspace_id)
    if not s:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if body.title is not None:
        s.title = body.title
    if body.subtitle is not None:
        s.subtitle = body.subtitle
    if body.canvas_state is not None:
        s.canvas_state = body.canvas_state
    s.updated_at = time.time()
    return WorkspaceOut(
        workspace_id=s.workspace_id,
        title=s.title,
        subtitle=s.subtitle,
        created_at=s.created_at,
        updated_at=s.updated_at,
        node_count=len(s.canvas_state.get("elements", [])),
        ai_node_count=len(s.ai_nodes),
    )


@router.delete(
    "/{workspace_id}",
    status_code=204,
    summary="Delete workspace",
)
def delete_workspace(workspace_id: str):
    ok = session_store.delete(workspace_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Workspace not found")
