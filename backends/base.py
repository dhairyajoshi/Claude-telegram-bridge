"""Backend protocol — the shape every coding-agent backend must implement.

The bridge consumes a backend through three things:

1. :class:`Backend` – a factory that creates sessions.
2. :class:`BackendSession` – a single in-flight conversation, with ``query()``
   yielding a stream of :data:`BackendEvent`\\ s, plus ``interrupt()`` and
   ``disconnect()``.
3. A :data:`PermissionAsker` callback the bridge supplies at session-open
   time. The backend awaits it whenever the underlying agent wants to run a
   tool that needs human approval (Bash, Write, Edit, ...). The callback
   receives ``(tool_name, input_dict)`` and returns ``True`` to allow.

The point of this layer is to keep ``bridge.py`` agnostic: it just plugs in
a Telegram-flavoured permission prompt and renders normalized events.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import (
    Awaitable,
    Callable,
    Optional,
    Protocol,
    Union,
    runtime_checkable,
)


# ---- Normalized event types ------------------------------------------------

@dataclass
class TextEvent:
    """A chunk of assistant-visible text. May be a full message or a fragment.
    Backends are free to coalesce or split; the consumer just renders it."""
    text: str


@dataclass
class ToolUseEvent:
    """The agent decided to invoke a tool. Emitted once per tool call (when
    the call is first observed). Approval, if needed, is handled separately
    via the :data:`PermissionAsker` callback inside the backend."""
    name: str
    input: dict


@dataclass
class ResultEvent:
    """Turn finished. ``error`` indicates the agent itself failed (vs. the
    user got a normal answer). ``message`` is an optional human-readable
    detail to surface."""
    error: bool = False
    message: Optional[str] = None


BackendEvent = Union[TextEvent, ToolUseEvent, ResultEvent]


# ---- Permission callback ---------------------------------------------------

# (tool_name, input_dict) -> awaitable[bool]; True = allow, False = deny.
PermissionAsker = Callable[[str, dict], Awaitable[bool]]


# ---- Backend protocols -----------------------------------------------------

@runtime_checkable
class BackendSession(Protocol):
    """One conversation with the agent. Holds cwd/model and the active
    underlying client (SDK client, HTTP session id, etc.)."""

    cwd: str
    model: str

    async def query(self, prompt: str):
        """Send ``prompt`` and asynchronously yield :data:`BackendEvent`\\ s
        until the turn completes. Implementations should yield a final
        :class:`ResultEvent` (success or error) and then stop."""
        ...

    async def interrupt(self) -> None:
        """Best-effort: cancel the in-flight turn. May be a no-op."""
        ...

    async def disconnect(self) -> None:
        """Tear down the underlying client/connection. Idempotent."""
        ...


class Backend(Protocol):
    """Factory for sessions. One instance per backend implementation;
    ``open_session`` is called once per chat session."""

    name: str

    async def open_session(
        self,
        *,
        cwd: str,
        model: str,
        ask_permission: PermissionAsker,
    ) -> BackendSession:
        ...

    def default_model(self) -> str:
        """Default model identifier when the user didn't pick one."""
        ...
