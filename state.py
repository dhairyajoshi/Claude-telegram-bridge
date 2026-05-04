"""On-disk persistence for bridge state.

The bridge keeps everything in memory by default, which means a restart
wipes out all session metadata and the underlying agent's conversation
history (since the agent is keyed by a session id we never persisted).

This module saves the *minimum* needed to rebuild ``CHATS`` after a
restart:

- per-chat: active session id and a counter for the next short id
- per-session: short id, backend name, cwd, model, and the agent-level
  resume token (Claude session UUID, opencode session id, Codex thread id, ...)

We deliberately don't persist live backend client objects, queue
contents, or pending approvals — those are tied to the running process.
On restart, the bridge rebuilds ``BridgeSession`` records and lazily
re-opens the underlying agent client (with ``resume_id`` if present)
the first time a message arrives in that session.

The state file lives at ``~/.agent-telegram-bridge/state.json`` by
default; override with ``AGENT_BRIDGE_STATE_FILE``. The older
``CLAUDE_BRIDGE_STATE_FILE`` name is still accepted as a compatibility
fallback.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any


log = logging.getLogger("bridge.state")

VERSION = 1

DEFAULT_STATE_DIR = os.path.expanduser("~/.agent-telegram-bridge")
STATE_FILE = os.environ.get(
    "AGENT_BRIDGE_STATE_FILE",
    os.environ.get(
        "CLAUDE_BRIDGE_STATE_FILE",
        os.path.join(DEFAULT_STATE_DIR, "state.json"),
    ),
)


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "chats": {}}


def load() -> dict[str, Any]:
    """Read the state file. Returns an empty skeleton on any failure
    (missing file, corrupt JSON, version mismatch) — we'd rather start
    clean than refuse to boot."""
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _empty()
    except (json.JSONDecodeError, OSError) as e:
        log.warning("could not read state file %s: %s", STATE_FILE, e)
        return _empty()

    if not isinstance(data, dict) or data.get("version") != VERSION:
        log.warning(
            "state file version mismatch (%s != %s); ignoring",
            data.get("version") if isinstance(data, dict) else "?",
            VERSION,
        )
        return _empty()
    return data


def save(payload: dict[str, Any]) -> None:
    """Atomically write ``payload`` to the state file. Best-effort: any
    OS-level failure is logged and swallowed — losing one snapshot is
    far better than crashing a live conversation."""
    payload = {**payload, "version": VERSION}
    state_dir = os.path.dirname(STATE_FILE) or "."
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError as e:
        log.warning("could not create state dir %s: %s", state_dir, e)
        return

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=state_dir, prefix=".state.", suffix=".json",
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.replace(tmp_path, STATE_FILE)
    except OSError as e:
        log.warning("could not write state file %s: %s", STATE_FILE, e)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
