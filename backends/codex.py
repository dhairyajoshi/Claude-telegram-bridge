"""Codex backend (official TypeScript SDK sidecar + JSONL).

Runs the official ``@openai/codex-sdk`` through a tiny Node sidecar for
each turn and translates its streamed events into the bridge's normalized
backend events. Codex persists thread history itself; we capture
``thread.started.thread_id`` and later continue with ``resumeThread()``.

The SDK currently wraps the local Codex CLI and streams structured JSONL.
There is no Claude-style Python permission callback to wire into Telegram,
so command safety is controlled by Codex's sandbox/approval options.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .base import (
    Backend,
    BackendSession,
    PermissionAsker,
    ResultEvent,
    TextEvent,
    ThinkingEvent,
    ToolUseEvent,
)


log = logging.getLogger("bridge.codex")

SDK_RUNNER = (
    Path(__file__).resolve().parents[1] / "scripts" / "codex_sdk_runner.mjs"
)


def _append_model(args: list[str], model: str) -> None:
    if model:
        args.extend(["-m", model])


def _append_common_args(args: list[str], model: str, sandbox: str) -> None:
    _append_model(args, model)
    if sandbox:
        args.extend(["-s", sandbox])


@dataclass
class CodexBackendSession:
    cwd: str
    model: str
    _ask_permission: PermissionAsker
    sandbox: str = "workspace-write"
    approval_policy: str = "never"
    use_sdk: bool = True
    resume_id: Optional[str] = None
    _proc: Optional[asyncio.subprocess.Process] = field(default=None, init=False)

    def _sdk_turn_args(self) -> list[str]:
        return ["node", str(SDK_RUNNER), "-"]

    def _sdk_request(self, prompt: str) -> dict:
        return {
            "cwd": self.cwd,
            "model": self.model,
            "sandbox": self.sandbox,
            "approvalPolicy": self.approval_policy,
            "resumeId": self.resume_id,
            "prompt": prompt,
            "skipGitRepoCheck": True,
        }

    def _new_turn_args(self, prompt: str) -> list[str]:
        args = ["codex", "exec", "--json", "--cd", self.cwd]
        _append_common_args(args, self.model, self.sandbox)
        args.append("--skip-git-repo-check")
        args.append(prompt)
        return args

    def _resume_turn_args(self, prompt: str) -> list[str]:
        # `codex exec resume` filters/selects by the process cwd rather than
        # accepting `--cd`, so subprocess cwd is set to `self.cwd`.
        args = ["codex", "exec", "resume", "--json"]
        _append_model(args, self.model)
        args.append("--skip-git-repo-check")
        args.extend([self.resume_id or "", prompt])
        return args

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> str:
        if proc.stderr is None:
            return ""
        try:
            data = await proc.stderr.read()
        except Exception:
            log.exception("codex stderr read failed")
            return ""
        return data.decode(errors="replace").strip()

    def _normalize_item(self, item: dict):
        item_type = item.get("type")
        if item_type == "agent_message":
            text = item.get("text") or ""
            if text.strip():
                yield TextEvent(text=text)
        elif item_type == "command_execution":
            command = item.get("command") or ""
            if command:
                yield ToolUseEvent(name="Bash", input={"command": command})
        elif item_type == "file_change":
            changes = item.get("changes") or []
            if changes:
                yield ToolUseEvent(name="Patch", input={"changes": changes})
        elif item_type == "mcp_tool_call":
            name = item.get("tool") or "mcp_tool_call"
            yield ToolUseEvent(
                name=name,
                input={
                    "server": item.get("server"),
                    "arguments": item.get("arguments"),
                },
            )
        elif item_type == "web_search":
            query = item.get("query") or ""
            yield ToolUseEvent(name="WebSearch", input={"query": query})
        elif item_type == "error":
            message = item.get("message") or ""
            if message:
                yield TextEvent(text=f"Codex error: {message}")
        elif item_type in {"reasoning", "agent_reasoning"}:
            yield ThinkingEvent()

    async def _query_process(
        self,
        args: list[str],
        *,
        stdin_payload: Optional[dict] = None,
    ):
        completed = False
        result_error = False
        result_message: Optional[str] = None
        emitted_items: set[str] = set()
        saw_event = False

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=self.cwd,
                stdin=(
                    asyncio.subprocess.PIPE
                    if stdin_payload is not None else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._proc = proc
        except Exception as e:
            log.exception("failed to start codex")
            yield ResultEvent(error=True, message=f"failed to start codex: {e}")
            return

        stderr_task = asyncio.create_task(
            self._read_stderr(proc), name="codex-stderr",
        )

        try:
            if stdin_payload is not None:
                assert proc.stdin is not None
                proc.stdin.write(json.dumps(stdin_payload).encode())
                await proc.stdin.drain()
                proc.stdin.close()

            assert proc.stdout is not None
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("non-json codex line: %r", line[:300])
                    continue

                saw_event = True
                etype = event.get("type")
                if etype == "thread.started":
                    thread_id = event.get("thread_id")
                    if thread_id:
                        self.resume_id = thread_id
                elif etype == "turn.started":
                    yield ThinkingEvent()
                elif etype in {"item.started", "item.completed"}:
                    item = event.get("item") or {}
                    item_id = item.get("id")
                    # Command items are emitted at start and completion.
                    # Show them once, when first observed.
                    if item_id and item_id in emitted_items:
                        continue
                    if item_id:
                        emitted_items.add(item_id)
                    for normalized in self._normalize_item(item):
                        yield normalized
                elif etype == "turn.completed":
                    completed = True
                    break
                elif etype == "turn.failed":
                    completed = True
                    result_error = True
                    result_message = str(
                        event.get("error")
                        or event.get("message")
                        or "turn failed"
                    )
                    break
                elif etype == "error":
                    completed = True
                    result_error = True
                    result_message = str(event.get("message") or "codex error")
                    break

            rc = await proc.wait()
            stderr = await stderr_task
            if completed:
                yield ResultEvent(error=result_error, message=result_message)
            else:
                msg = stderr or f"codex exited with status {rc}"
                yield ResultEvent(error=(rc != 0), message=msg if rc != 0 else None)
            if self.use_sdk and not saw_event and rc != 0:
                log.warning("codex sdk runner failed before events: %s", stderr)
        except asyncio.CancelledError:
            await self.interrupt()
            raise
        except Exception as e:
            log.exception("codex query failed")
            yield ResultEvent(error=True, message=str(e))
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
                try:
                    await stderr_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._proc = None

    async def query(self, prompt: str):
        if self.use_sdk:
            args = self._sdk_turn_args()
            request = self._sdk_request(prompt)
            async for ev in self._query_process(args, stdin_payload=request):
                yield ev
            return

        args = (
            self._resume_turn_args(prompt)
            if self.resume_id else self._new_turn_args(prompt)
        )
        async for ev in self._query_process(args):
            yield ev

    async def interrupt(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            return
        except Exception:
            log.exception("codex interrupt failed")

    async def disconnect(self) -> None:
        await self.interrupt()


@dataclass
class CodexBackend:
    name: str = "codex"
    default_model_id: str = ""
    sandbox: str = "workspace-write"
    approval_policy: str = "never"
    use_sdk: bool = True

    @classmethod
    def from_env(cls) -> "CodexBackend":
        return cls(
            default_model_id=os.environ.get("CODEX_BRIDGE_MODEL", ""),
            sandbox=os.environ.get("CODEX_BRIDGE_SANDBOX", "workspace-write"),
            approval_policy=os.environ.get(
                "CODEX_BRIDGE_APPROVAL_POLICY", "never",
            ),
            use_sdk=os.environ.get("CODEX_BRIDGE_TRANSPORT", "sdk") != "exec",
        )

    def default_model(self) -> str:
        return self.default_model_id

    async def open_session(
        self, *, cwd: str, model: str, ask_permission: PermissionAsker,
        resume_id: Optional[str] = None,
    ) -> BackendSession:
        return CodexBackendSession(
            cwd=cwd,
            model=model,
            _ask_permission=ask_permission,
            sandbox=self.sandbox,
            approval_policy=self.approval_policy,
            use_sdk=self.use_sdk,
            resume_id=resume_id,
        )
