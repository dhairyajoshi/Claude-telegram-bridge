"""Backend implementations for the Telegram bridge.

A backend is something that can drive an AI coding agent on this machine
(Claude Code, opencode, ...). The bridge talks to backends through the
``Backend`` / ``BackendSession`` protocols defined in :mod:`backends.base`.
"""
from __future__ import annotations

from .base import (
    Backend,
    BackendSession,
    BackendEvent,
    PermissionAsker,
    ResultEvent,
    TextEvent,
    ThinkingEvent,
    ToolUseEvent,
)

__all__ = [
    "Backend",
    "BackendSession",
    "BackendEvent",
    "PermissionAsker",
    "ResultEvent",
    "TextEvent",
    "ThinkingEvent",
    "ToolUseEvent",
    "load_backend",
]


def load_backend(name: str) -> Backend:
    """Return a Backend instance for the given short name."""
    name = name.lower()
    if name == "claude":
        from .claude import ClaudeBackend
        return ClaudeBackend()
    if name == "opencode":
        from .opencode import OpencodeBackend
        return OpencodeBackend.from_env()
    raise ValueError(f"unknown backend: {name!r} (expected: claude, opencode)")
