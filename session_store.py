"""
session_store.py — In-memory store for workspace sessions and chat history.
Each workspace gets a SessionState bucket that survives reconnects within
the same process lifetime.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from config import MAX_HISTORY_TURNS


@dataclass
class ChatTurn:
    role: str          # "user" | "model"
    content: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class SessionState:
    workspace_id: str
    title: str = "Untitled"
    subtitle: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # Chat / turn history (sliding window)
    history: list[ChatTurn] = field(default_factory=list)

    # Canvas JSON saved from Excalidraw
    canvas_state: dict[str, Any] = field(default_factory=dict)

    # Nodes that have been AI-added to the canvas
    ai_nodes: list[dict[str, Any]] = field(default_factory=list)

    def add_turn(self, role: str, content: str) -> None:
        """Append a turn and enforce the sliding window."""
        self.history.append(ChatTurn(role=role, content=content))
        if len(self.history) > MAX_HISTORY_TURNS:
            # Drop oldest turns beyond max
            self.history = self.history[-MAX_HISTORY_TURNS:]
        self.updated_at = time.time()

    def history_as_text(self) -> str:
        """Return history as a readable string to inject into the system prompt."""
        if not self.history:
            return "No prior conversation."
        lines: list[str] = []
        for turn in self.history:
            prefix = "User" if turn.role == "user" else "FlowState"
            lines.append(f"[{prefix}]: {turn.content}")
        return "\n".join(lines)

    def add_ai_node(self, node: dict[str, Any]) -> None:
        self.ai_nodes.append(node)
        self.updated_at = time.time()


# ── Global store ──────────────────────────────────────────────────────────────
# dict[workspace_id → SessionState]
_store: dict[str, SessionState] = {}


def get_or_create(workspace_id: str, title: str = "Untitled", subtitle: str = "") -> SessionState:
    if workspace_id not in _store:
        _store[workspace_id] = SessionState(
            workspace_id=workspace_id,
            title=title,
            subtitle=subtitle,
        )
    return _store[workspace_id]


def get(workspace_id: str) -> SessionState | None:
    return _store.get(workspace_id)


def list_all() -> list[SessionState]:
    return list(_store.values())


def delete(workspace_id: str) -> bool:
    if workspace_id in _store:
        del _store[workspace_id]
        return True
    return False