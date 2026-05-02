"""opencode backend (HTTP + SSE).

Talks to a running ``opencode serve`` instance over HTTP. We never spawn
the server ourselves on this branch — the user runs ``opencode serve``
separately and points the bridge at it via ``OPENCODE_BASE_URL``.

Wire format (v1, derived from packages/sdk/openapi.json in sst/opencode)
-----------------------------------------------------------------------
- Auth: HTTP Basic ``opencode:$OPENCODE_SERVER_PASSWORD`` (username overridable
  via ``OPENCODE_SERVER_USERNAME``). Server is unauthenticated if no password.
- ``POST /session?directory=<cwd>`` body ``{}`` -> ``{id, ...}``.
- ``POST /session/{id}/message`` body
  ``{ parts: [{type:"text", text}], model?: {providerID, modelID}, agent? }``.
- ``GET /event`` SSE: each line is JSON ``{type, properties}``. We watch:
    - ``message.part.updated`` with ``properties.part`` carrying TextPart /
      ToolPart snapshots,
    - ``permission.updated`` with the full Permission record (we approve via
      ``POST /session/{id}/permissions/{permissionID}`` body
      ``{response: "once"|"always"|"reject"}``),
    - ``session.idle`` with ``{sessionID}`` — the turn-complete signal,
    - ``session.error`` — surfaced as a ResultEvent error.
- ``POST /session/{id}/abort`` — no body, returns ``true``.

Model selection
---------------
opencode wants ``{providerID, modelID}``. Users pass models as
``"<providerID>/<modelID>"`` (e.g. ``anthropic/claude-sonnet-4-5``); if no
slash is present we leave ``model`` unset and let opencode use its config
default.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import httpx
from httpx_sse import aconnect_sse

from .base import (
    Backend,
    BackendSession,
    PermissionAsker,
    ResultEvent,
    TextEvent,
    ThinkingEvent,
    ToolUseEvent,
)


log = logging.getLogger("bridge.opencode")


def _parse_model(model: str) -> Optional[dict]:
    """``"anthropic/claude-sonnet-4-5"`` -> ``{providerID, modelID}``.
    Returns None if the model string isn't in provider/model form, so the
    caller knows to omit the field entirely (let opencode use its default)."""
    if not model or "/" not in model:
        return None
    provider, _, model_id = model.partition("/")
    if not provider or not model_id:
        return None
    return {"providerID": provider, "modelID": model_id}


@dataclass
class OpencodeBackendSession:
    cwd: str
    model: str
    _ask_permission: PermissionAsker
    _http: httpx.AsyncClient
    _agent: Optional[str] = None
    session_id: Optional[str] = None
    # part IDs we've already emitted an event for this turn (tool calls,
    # reasoning bursts) so we don't spam the chat as parts transition
    # through pending/running/completed snapshots.
    _emitted_part_ids: set = field(default_factory=set)
    _pending_perm_tasks: set = field(default_factory=set)

    async def _ensure_session(self) -> str:
        if self.session_id is not None:
            return self.session_id
        r = await self._http.post(
            "/session", params={"directory": self.cwd}, json={},
        )
        r.raise_for_status()
        data = r.json()
        self.session_id = data["id"]
        log.info("created opencode session %s in %s", self.session_id, self.cwd)
        return self.session_id

    async def _post_message(self, prompt: str) -> None:
        body: dict = {"parts": [{"type": "text", "text": prompt}]}
        model = _parse_model(self.model)
        if model is not None:
            body["model"] = model
        if self._agent:
            body["agent"] = self._agent
        # Fire-and-forget from our perspective: opencode processes
        # asynchronously and emits SSE events. Don't block on the response
        # body beyond status check.
        r = await self._http.post(
            f"/session/{self.session_id}/message", json=body,
        )
        r.raise_for_status()

    async def _respond_to_permission(self, perm_id: str, allow: bool) -> None:
        try:
            r = await self._http.post(
                f"/session/{self.session_id}/permissions/{perm_id}",
                json={"response": "once" if allow else "reject"},
            )
            r.raise_for_status()
        except Exception:
            log.exception("failed to submit permission response")

    def _spawn_perm_handler(self, perm: dict) -> None:
        """Permission events arrive on the SSE stream; we have to ask the
        user *and* keep iterating. So fan out the prompt to a task and let
        the main loop keep reading."""
        perm_id = perm.get("id")
        tool_name = perm.get("type") or "tool"
        # opencode's permission record carries the call payload under
        # `metadata`; some versions also stash a `pattern`. We pass
        # whatever's there straight through to the renderer.
        meta = perm.get("metadata") or {}
        if "pattern" in perm and "pattern" not in meta:
            meta = {**meta, "pattern": perm["pattern"]}

        async def handle():
            try:
                allowed = await self._ask_permission(tool_name, meta)
            except Exception:
                log.exception("ask_permission raised")
                allowed = False
            await self._respond_to_permission(perm_id, allowed)

        task = asyncio.create_task(handle(), name=f"perm:{perm_id}")
        self._pending_perm_tasks.add(task)
        task.add_done_callback(self._pending_perm_tasks.discard)

    def _normalize_part(self, part: dict):
        """Turn an opencode Part snapshot into our event vocabulary.
        Yields zero or one events; the caller flushes them."""
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text") or ""
            # Only emit on terminal-ish state: full text snapshot. Streaming
            # deltas come via message.part.delta which we deliberately ignore
            # to avoid Telegram rate limits.
            if text.strip():
                yield TextEvent(text=text)
        elif ptype == "tool":
            part_id = part.get("id")
            if part_id and part_id in self._emitted_part_ids:
                return
            state = part.get("state") or {}
            status = state.get("status")
            # Wait until we have the full input — `pending` may not have
            # populated it yet.
            if status not in ("running", "completed", "error"):
                return
            name = part.get("name") or "tool"
            # v1 carries top-level `input`; v2-style nests under state.
            inp = part.get("input")
            if inp is None:
                inp = state.get("input") or {}
            if part_id:
                self._emitted_part_ids.add(part_id)
            yield ToolUseEvent(name=name, input=inp or {})
        elif ptype == "reasoning":
            # Reasoning parts get updated repeatedly as the model thinks;
            # the bridge only needs the first ping to start its indicator.
            part_id = part.get("id")
            if part_id and part_id in self._emitted_part_ids:
                return
            if part_id:
                self._emitted_part_ids.add(part_id)
            yield ThinkingEvent()

    async def query(self, prompt: str):
        # Fresh part-dedup state per turn (covers tools + reasoning bursts).
        self._emitted_part_ids = set()

        try:
            await self._ensure_session()
        except Exception as e:
            log.exception("opencode session create failed")
            yield ResultEvent(error=True, message=f"failed to start session: {e}")
            return

        # Open the SSE stream BEFORE sending the message so we don't race on
        # early events.
        try:
            async with aconnect_sse(
                self._http, "GET", "/event",
                timeout=httpx.Timeout(connect=10, read=None, write=10, pool=10),
            ) as event_source:
                try:
                    await self._post_message(prompt)
                except httpx.HTTPStatusError as e:
                    body = ""
                    try:
                        body = e.response.text
                    except Exception:
                        pass
                    yield ResultEvent(
                        error=True,
                        message=f"message POST failed: {e}\n{body[:500]}",
                    )
                    return
                except Exception as e:
                    log.exception("post message failed")
                    yield ResultEvent(error=True, message=f"post failed: {e}")
                    return

                async for sse in event_source.aiter_sse():
                    if not sse.data:
                        continue
                    try:
                        ev = sse.json()
                    except ValueError:
                        log.warning("non-json SSE: %r", sse.data[:200])
                        continue

                    etype = ev.get("type")
                    props = ev.get("properties") or {}

                    # All payloads we care about either carry sessionID
                    # directly or wrap it inside .info / .part. Bail on
                    # events for other sessions so two parallel /new'd chats
                    # don't cross-talk.
                    sid = (
                        props.get("sessionID")
                        or (props.get("info") or {}).get("id")
                        or (props.get("part") or {}).get("sessionID")
                    )
                    if sid and sid != self.session_id:
                        continue

                    if etype == "message.part.updated":
                        part = props.get("part") or {}
                        for normalized in self._normalize_part(part):
                            yield normalized
                    elif etype == "permission.updated":
                        # The permission record IS the properties payload
                        # (per the spec — Permission shape, not wrapped).
                        self._spawn_perm_handler(props)
                    elif etype == "session.error":
                        err = props.get("error") or "unknown error"
                        yield ResultEvent(error=True, message=str(err))
                        return
                    elif etype == "session.idle":
                        yield ResultEvent(error=False)
                        return
                    # Everything else (file.edited, lsp.*, todo.*, ...) we
                    # quietly ignore — they're useful for a TUI but the
                    # bridge doesn't surface them.
        except httpx.HTTPError as e:
            log.exception("opencode SSE failed")
            yield ResultEvent(error=True, message=f"SSE error: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("opencode query failed")
            yield ResultEvent(error=True, message=str(e))

    async def interrupt(self) -> None:
        if self.session_id is None:
            return
        try:
            r = await self._http.post(f"/session/{self.session_id}/abort")
            r.raise_for_status()
        except Exception:
            log.exception("opencode abort failed")

    async def disconnect(self) -> None:
        # Cancel any in-flight permission prompts so they don't outlive the
        # session.
        for t in list(self._pending_perm_tasks):
            t.cancel()
        self._pending_perm_tasks.clear()
        try:
            await self._http.aclose()
        except Exception:
            log.exception("http close failed")


@dataclass
class OpencodeBackend:
    name: str = "opencode"
    base_url: str = "http://127.0.0.1:4096"
    username: str = "opencode"
    password: Optional[str] = None
    default_model_id: str = ""
    default_agent: Optional[str] = None

    @classmethod
    def from_env(cls) -> "OpencodeBackend":
        return cls(
            base_url=os.environ.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096"),
            username=os.environ.get("OPENCODE_SERVER_USERNAME", "opencode"),
            password=os.environ.get("OPENCODE_SERVER_PASSWORD") or None,
            default_model_id=os.environ.get("OPENCODE_BRIDGE_MODEL", ""),
            default_agent=os.environ.get("OPENCODE_BRIDGE_AGENT") or None,
        )

    def default_model(self) -> str:
        return self.default_model_id

    async def open_session(
        self, *, cwd: str, model: str, ask_permission: PermissionAsker,
    ) -> BackendSession:
        auth = (
            httpx.BasicAuth(self.username, self.password)
            if self.password else None
        )
        # No global read timeout — SSE streams are long-lived.
        client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=auth,
            timeout=httpx.Timeout(connect=10, read=None, write=30, pool=10),
        )
        return OpencodeBackendSession(
            cwd=cwd,
            model=model,
            _ask_permission=ask_permission,
            _http=client,
            _agent=self.default_agent,
        )
