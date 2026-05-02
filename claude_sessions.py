"""Read Claude CLI's on-disk session transcripts.

The Claude CLI stores every conversation as a JSONL file under
``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl``, where the
encoding is just ``cwd.replace('/', '-')``. Each line is a JSON object
describing one event in the conversation (user message, tool call,
permission update, ...).

The bridge uses this for "adopt-existing-session" UX in ``/resume``:

- :func:`list_sessions` returns the most recent transcripts in a given
  cwd, with a one-line preview pulled from the first user message, so
  the bridge can render a picker.
- :func:`find_by_prefix` resolves a user-typed short id (8-char prefix)
  to a unique session uuid in that cwd.

We never write to these files — we just hand the UUID back to the
``claude-agent-sdk`` via ``resume=...`` and let the SDK append on its
own. That way "continue in CLI" and "continue in bridge" interoperate
without any file-format coupling.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger("bridge.claude_sessions")

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

# How many lines into a transcript we'll scan before giving up looking
# for the first user prompt. Most sessions surface it within the first
# few lines; the cap stops a pathological JSONL from blocking the list.
_PROMPT_SCAN_LIMIT = 50


@dataclass
class CliSessionInfo:
    """One on-disk Claude CLI session, summarised for display."""
    session_id: str
    path: str
    mtime: float
    first_prompt: Optional[str]


def encode_cwd(cwd: str) -> str:
    """Mirror Claude CLI's path-to-directory encoding. We canonicalize
    the path first (``realpath``) so symlinks and trailing slashes
    don't make us miss a directory the CLI actually wrote to."""
    return os.path.realpath(cwd).replace("/", "-")


def project_dir(cwd: str) -> str:
    return os.path.join(PROJECTS_DIR, encode_cwd(cwd))


def list_sessions(cwd: str, limit: int = 10) -> list[CliSessionInfo]:
    """Return the ``limit`` most-recently-modified CLI sessions for
    ``cwd``, newest first. Empty list if the project directory doesn't
    exist or contains no transcripts. Never raises."""
    pdir = project_dir(cwd)
    try:
        names = os.listdir(pdir)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as e:
        log.warning("list_sessions(%s): %s", pdir, e)
        return []

    entries: list[tuple[str, str, float]] = []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(pdir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        sid = name[: -len(".jsonl")]
        entries.append((sid, path, st.st_mtime))

    entries.sort(key=lambda e: e[2], reverse=True)
    out: list[CliSessionInfo] = []
    for sid, path, mtime in entries[:limit]:
        out.append(CliSessionInfo(
            session_id=sid,
            path=path,
            mtime=mtime,
            first_prompt=_first_user_prompt(path),
        ))
    return out


def find_by_prefix(cwd: str, query: str) -> Optional[CliSessionInfo]:
    """Resolve a UUID or short prefix to a unique session in ``cwd``.

    ``None`` if there's no match or the prefix is ambiguous (matches
    more than one). The bridge surfaces "not found" either way — the
    user can re-run ``/resume`` to see the full list.
    """
    if not query:
        return None
    # Pull a generous slice; we expect a handful of sessions per project,
    # and bailing at 200 keeps the linear scan cheap on shared machines.
    sessions = list_sessions(cwd, limit=200)
    matches = [s for s in sessions if s.session_id.startswith(query)]
    if len(matches) != 1:
        return None
    return matches[0]


def _first_user_prompt(path: str) -> Optional[str]:
    """Pull the first user-typed message out of a transcript, for use
    as a one-line preview. ``None`` if we can't find one within the
    scan cap (e.g. session crashed before any user turn)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, raw in enumerate(f):
                if i >= _PROMPT_SCAN_LIMIT:
                    return None
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                msg = obj.get("message") or {}
                content = msg.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    # Multi-part message — concatenate text parts and
                    # ignore tool_result / image parts (which are noise
                    # for a preview).
                    parts = [
                        p.get("text") or ""
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    joined = "\n".join(p for p in parts if p)
                    return joined or None
                return None
    except OSError as e:
        log.debug("could not read %s: %s", path, e)
        return None
    return None
