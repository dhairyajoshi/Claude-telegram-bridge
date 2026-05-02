"""Claude Code backend (claude_agent_sdk).

Wraps :class:`claude_agent_sdk.ClaudeSDKClient` behind the
:class:`backends.base.Backend` protocol. The translation is mostly
mechanical: we convert the SDK's ``can_use_tool`` callback into our generic
:data:`PermissionAsker`, and demux the SDK's message stream into
``TextEvent`` / ``ToolUseEvent`` / ``ResultEvent``.

Read-only tools (Read, Glob, Grep, ...) are auto-approved on the bridge
side so they don't generate Telegram noise.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from .base import (
    Backend,
    BackendSession,
    PermissionAsker,
    ResultEvent,
    TextEvent,
    ThinkingEvent,
    ToolUseEvent,
)


log = logging.getLogger("bridge.claude")


# Tools we always allow without prompting. These are read-only or
# bookkeeping; the user clicking "Allow" 50 times per turn is worse than
# the threat model justifies.
AUTO_ALLOW_TOOLS = {
    "Read", "Glob", "Grep", "WebFetch", "WebSearch",
    "TodoWrite", "NotebookRead",
}


@dataclass
class ClaudeBackendSession:
    cwd: str
    model: str
    _ask_permission: PermissionAsker
    client: Optional[ClaudeSDKClient] = field(default=None)

    async def _ensure_client(self) -> ClaudeSDKClient:
        if self.client is not None:
            return self.client

        async def can_use_tool(tool_name, input_data, context):
            if tool_name in AUTO_ALLOW_TOOLS:
                return PermissionResultAllow()
            try:
                allowed = await self._ask_permission(tool_name, input_data)
            except Exception as e:
                log.exception("permission asker raised")
                return PermissionResultDeny(message=f"bridge error: {e}")
            return (
                PermissionResultAllow() if allowed
                else PermissionResultDeny(message="user denied")
            )

        options = ClaudeAgentOptions(
            model=self.model,
            cwd=self.cwd,
            permission_mode="default",
            can_use_tool=can_use_tool,
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        self.client = client
        return client

    async def query(self, prompt: str):
        try:
            client = await self._ensure_client()
        except Exception as e:
            log.exception("ensure_client failed")
            yield ResultEvent(error=True, message=f"failed to start session: {e}")
            return

        try:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            if block.text.strip():
                                yield TextEvent(text=block.text)
                        elif isinstance(block, ToolUseBlock):
                            yield ToolUseEvent(name=block.name, input=block.input)
                        elif isinstance(block, ThinkingBlock):
                            # Surface as a heartbeat; the bridge renders a
                            # transient "thinking" indicator and discards
                            # the raw reasoning text.
                            yield ThinkingEvent()
                elif isinstance(message, ResultMessage):
                    error = message.is_error or message.subtype != "success"
                    msg = message.result or message.subtype if error else None
                    yield ResultEvent(error=error, message=msg)
                    return
        except Exception as e:
            log.exception("claude query failed")
            yield ResultEvent(error=True, message=str(e))

    async def interrupt(self) -> None:
        if self.client is None:
            return
        try:
            await self.client.interrupt()
        except Exception:
            log.exception("interrupt failed")

    async def disconnect(self) -> None:
        if self.client is None:
            return
        try:
            await self.client.disconnect()
        except Exception:
            log.exception("disconnect failed")
        self.client = None


class ClaudeBackend:
    name = "claude"

    def default_model(self) -> str:
        return os.environ.get("CLAUDE_BRIDGE_MODEL", "claude-opus-4-7")

    async def open_session(
        self, *, cwd: str, model: str, ask_permission: PermissionAsker,
    ) -> BackendSession:
        return ClaudeBackendSession(
            cwd=cwd, model=model, _ask_permission=ask_permission,
        )
