"""
Coding-agent <-> Telegram bridge.

Lets you drive an AI coding agent (Claude Code or opencode) on this Mac
from a Telegram chat. Each chat can hold multiple sessions (short numeric
IDs) and you switch between them with /switch. Read-only tools auto-allow
(Claude backend) or use opencode's own permission config; everything else
prompts with Allow/Deny buttons.

Setup
-----
1. @BotFather on Telegram -> /newbot -> save the token.
2. @userinfobot on Telegram -> save your numeric user id.
3. Export env vars:
       export TELEGRAM_BOT_TOKEN=...
       export ALLOWED_USER_IDS=12345              # comma-separated
       export CLAUDE_BRIDGE_CWD=$HOME/some/repo   # optional, default $HOME

   Backend selection (default: claude):
       export BRIDGE_BACKEND=claude               # or "opencode"
       export CLAUDE_BRIDGE_MODEL=claude-opus-4-7 # claude backend default
       # opencode backend (run `opencode serve` separately):
       export OPENCODE_BASE_URL=http://127.0.0.1:4096
       export OPENCODE_SERVER_PASSWORD=...        # if you set one
       export OPENCODE_BRIDGE_MODEL=anthropic/claude-sonnet-4-5  # provider/model

4. uv run python bridge.py

Commands in chat:
    /start                show active session
    /sessions             list sessions in this chat
    /switch <id>          switch active session
    /new [backend]        create a new session (backend: claude|opencode)
    /rm <id>              remove a session
    /stop                 interrupt the running task
    /cd <path>            change cwd of the active session
    /status               status of active session
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from backends import (
    Backend,
    BackendSession,
    ResultEvent,
    TextEvent,
    ThinkingEvent,
    ToolUseEvent,
    load_backend,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bridge")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if x.strip()
}
DEFAULT_CWD = os.environ.get("CLAUDE_BRIDGE_CWD") or os.path.expanduser("~")
DEFAULT_BACKEND = os.environ.get("BRIDGE_BACKEND", "claude")

KNOWN_BACKENDS = ("claude", "opencode")

# Resolved Backend instances, lazily populated. We keep them per-process
# (they're just factories) so two chats can share one OpencodeBackend.
_BACKENDS: dict[str, Backend] = {}


def get_backend(name: str) -> Backend:
    b = _BACKENDS.get(name)
    if b is None:
        b = load_backend(name)
        _BACKENDS[name] = b
    return b


MAX_MSG_LEN = 3500
APPROVAL_TIMEOUT_S = 600


# ---- Per-chat / per-session state -----------------------------------------

@dataclass
class BridgeSession:
    """One conversation slot in a Telegram chat. Wraps a BackendSession plus
    the user-visible metadata (cwd/model/backend) we render in /sessions."""
    sid: str
    backend_name: str
    cwd: str
    model: str
    backend_session: Optional[BackendSession] = None


@dataclass
class QueuedPrompt:
    """A user message waiting its turn. ``session`` is captured at enqueue
    time, so a message stays pinned to the session it was typed into even
    if the active session changes (via ``/switch``) before the worker gets
    to it."""
    session: BridgeSession
    text: str


@dataclass
class ChatState:
    chat_id: int
    sessions: dict[str, BridgeSession] = field(default_factory=dict)
    active_sid: Optional[str] = None
    # ``current_task`` is the in-flight ``run_query`` for the head of the
    # queue. ``worker_task`` is the long-lived drain loop that pulls items
    # off ``queue`` and runs them serially.
    current_task: Optional[asyncio.Task] = None
    running_sid: Optional[str] = None
    pending_approvals: dict[str, asyncio.Future] = field(default_factory=dict)
    queue: "asyncio.Queue[QueuedPrompt]" = field(default_factory=asyncio.Queue)
    worker_task: Optional[asyncio.Task] = None
    _next_id: int = 1

    def new_session(self, *, backend_name: Optional[str] = None,
                    cwd: Optional[str] = None,
                    model: Optional[str] = None) -> BridgeSession:
        bname = backend_name or DEFAULT_BACKEND
        backend = get_backend(bname)
        sid = str(self._next_id)
        self._next_id += 1
        s = BridgeSession(
            sid=sid,
            backend_name=bname,
            cwd=cwd or DEFAULT_CWD,
            model=model or backend.default_model(),
        )
        self.sessions[sid] = s
        self.active_sid = sid
        return s

    def active(self) -> Optional[BridgeSession]:
        if self.active_sid is None:
            return None
        return self.sessions.get(self.active_sid)


CHATS: dict[int, ChatState] = {}


def get_chat(chat_id: int) -> ChatState:
    c = CHATS.get(chat_id)
    if c is None:
        c = ChatState(chat_id=chat_id)
        CHATS[chat_id] = c
    return c


def get_or_create_active(chat_id: int) -> tuple[ChatState, BridgeSession]:
    c = get_chat(chat_id)
    s = c.active()
    if s is None:
        s = c.new_session()
    return c, s


# ---- auth -----------------------------------------------------------------

def is_authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def deny(update: Update) -> None:
    if update.effective_chat:
        await update.effective_chat.send_message("Not authorized.")
    log.warning("rejected user_id=%s",
                update.effective_user.id if update.effective_user else None)


# ---- telegram send helpers ------------------------------------------------

async def send_chunked(bot, chat_id: int, text: str, *,
                        parse_mode: Optional[str] = None) -> None:
    if not text:
        return
    while text:
        chunk, text = text[:MAX_MSG_LEN], text[MAX_MSG_LEN:]
        try:
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
        except BadRequest:
            await bot.send_message(chat_id=chat_id, text=chunk)


def render_tool_call(name: str, inp: dict) -> str:
    """Pretty-print a tool call. Tool names from claude_agent_sdk are
    PascalCase ("Bash", "Edit"); from opencode they're lowercase ("bash",
    "edit"). We match case-insensitively and display whatever name we got."""
    key = (name or "").lower()
    if key == "bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        body = f"<pre>{html.escape(cmd[:1500])}</pre>"
        if desc:
            body = f"<i>{html.escape(desc)}</i>\n{body}"
        return f"🔧 <b>{html.escape(name or 'bash')}</b>\n{body}"
    if key in ("write", "edit"):
        path = inp.get("file_path") or inp.get("filePath") or inp.get("path") or ""
        return f"✏️ <b>{html.escape(name)}</b> <code>{html.escape(path)}</code>"
    if key == "read":
        path = inp.get("file_path") or inp.get("filePath") or inp.get("path") or ""
        return f"📖 <b>{html.escape(name)}</b> <code>{html.escape(path)}</code>"
    if key == "glob":
        return (
            f"🔍 <b>{html.escape(name)}</b> "
            f"<code>{html.escape(inp.get('pattern', ''))}</code>"
        )
    if key == "grep":
        return (
            f"🔍 <b>{html.escape(name)}</b> "
            f"<code>{html.escape(inp.get('pattern', ''))}</code>"
        )
    blob = json.dumps(inp, indent=2, default=str)[:1200]
    return f"🔧 <b>{html.escape(name or 'tool')}</b>\n<pre>{html.escape(blob)}</pre>"


# ---- permission prompt (used by both backends) ----------------------------

def make_permission_asker(chat: ChatState, bot):
    async def ask_permission(tool_name: str, input_data: dict) -> bool:
        approval_id = uuid.uuid4().hex[:8]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        chat.pending_approvals[approval_id] = future

        prompt_text = (
            "⚠️ <b>Permission requested</b>\n\n"
            + render_tool_call(tool_name, input_data or {})
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Allow", callback_data=f"a:{approval_id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"d:{approval_id}"),
        ]])

        try:
            msg = await bot.send_message(
                chat_id=chat.chat_id, text=prompt_text,
                parse_mode=ParseMode.HTML, reply_markup=keyboard,
            )
        except Exception as e:
            chat.pending_approvals.pop(approval_id, None)
            log.exception("failed to send approval prompt")
            return False

        try:
            allowed = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            try:
                await bot.edit_message_text(
                    chat_id=chat.chat_id, message_id=msg.message_id,
                    text=prompt_text + "\n\n<i>⌛ Timed out — denied</i>",
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest:
                pass
            return False
        finally:
            chat.pending_approvals.pop(approval_id, None)

        try:
            await bot.edit_message_text(
                chat_id=chat.chat_id, message_id=msg.message_id,
                text=prompt_text + (
                    "\n\n<i>✅ Allowed</i>" if allowed else "\n\n<i>❌ Denied</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            pass

        return allowed

    return ask_permission


# ---- session lifecycle ----------------------------------------------------

async def ensure_backend(chat: ChatState, session: BridgeSession,
                          bot) -> BackendSession:
    if session.backend_session is not None:
        return session.backend_session
    backend = get_backend(session.backend_name)
    session.backend_session = await backend.open_session(
        cwd=session.cwd,
        model=session.model,
        ask_permission=make_permission_asker(chat, bot),
    )
    return session.backend_session


async def close_backend(session: BridgeSession) -> None:
    if session.backend_session is None:
        return
    try:
        await session.backend_session.disconnect()
    except Exception:
        log.exception("backend disconnect failed")
    session.backend_session = None


# ---- per-turn liveness indicator ------------------------------------------

# Telegram displays a "typing" chat-action for ~5 seconds after each call,
# so we refresh slightly under that to keep the indicator continuous.
HEARTBEAT_INTERVAL_S = 4.0


class TurnIndicator:
    """Per-turn liveness signal. Two layers:

    1. A repeating ``sendChatAction(TYPING)`` every ~4s so the chat header
       always shows "typing…" while the agent is busy. This is the
       cheapest, most idiomatic "still alive" hint Telegram offers.
    2. A transient text message ("💭 thinking… (Ns)") spawned the first
       time we see a :class:`ThinkingEvent` and updated by the heartbeat.
       Finalised to "💭 thought for Ns" when the next non-thinking event
       arrives, so the chat history shows that thinking happened without
       the message lingering as a stale "thinking…" pill.

    All Telegram calls are best-effort: any failure (rate limits, the
    indicator message being deleted, network blips) is logged at debug
    and the indicator continues. We never want this to take down a turn.
    """

    def __init__(self, bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._thinking_msg_id: Optional[int] = None
        self._thinking_started_at: Optional[float] = None
        self._last_thinking_text: str = ""

    async def start(self) -> None:
        # Kick off the indicator immediately so the user sees activity
        # before the first model token.
        try:
            await self.bot.send_chat_action(
                chat_id=self.chat_id, action=ChatAction.TYPING,
            )
        except Exception:
            log.debug("initial chat_action failed", exc_info=True)
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"heartbeat:{self.chat_id}",
        )

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                try:
                    await self.bot.send_chat_action(
                        chat_id=self.chat_id, action=ChatAction.TYPING,
                    )
                except Exception:
                    log.debug("heartbeat chat_action failed", exc_info=True)
                # If a thinking indicator is active, also tick its
                # elapsed-time counter so it doesn't look frozen.
                await self._refresh_thinking()
        except asyncio.CancelledError:
            return

    async def _refresh_thinking(self) -> None:
        if self._thinking_msg_id is None or self._thinking_started_at is None:
            return
        elapsed = int(time.time() - self._thinking_started_at)
        text = f"💭 thinking… ({elapsed}s)"
        if text == self._last_thinking_text:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self._thinking_msg_id,
                text=text,
            )
            self._last_thinking_text = text
        except BadRequest:
            # "message is not modified" or "message to edit not found" —
            # don't care, just stop trying to edit it.
            pass
        except Exception:
            log.debug("thinking edit failed", exc_info=True)

    async def thinking(self) -> None:
        """Called when a ThinkingEvent arrives. Idempotent within a single
        thinking burst — we only send the message once per burst."""
        if self._thinking_msg_id is not None:
            return
        try:
            msg = await self.bot.send_message(
                chat_id=self.chat_id, text="💭 thinking…",
            )
        except Exception:
            log.exception("send thinking message failed")
            return
        self._thinking_msg_id = msg.message_id
        self._thinking_started_at = time.time()
        self._last_thinking_text = "💭 thinking…"

    async def end_thinking(self) -> None:
        """Called when the first non-thinking event arrives, so the user
        sees the thinking burst as a closed past tense rather than an
        endlessly-spinning pill."""
        if self._thinking_msg_id is None:
            return
        elapsed = max(
            1, int(time.time() - (self._thinking_started_at or time.time())),
        )
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self._thinking_msg_id,
                text=f"💭 thought for {elapsed}s",
            )
        except BadRequest:
            pass
        except Exception:
            log.debug("thinking finalise failed", exc_info=True)
        self._thinking_msg_id = None
        self._thinking_started_at = None
        self._last_thinking_text = ""

    async def close(self) -> None:
        # Cancel the heartbeat first so it doesn't race with end_thinking.
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat_task = None
        await self.end_thinking()


# ---- driving a turn -------------------------------------------------------

async def run_query(chat: ChatState, session: BridgeSession, prompt: str,
                     bot) -> None:
    try:
        backend = await ensure_backend(chat, session, bot)
    except Exception as e:
        log.exception("ensure_backend failed")
        await bot.send_message(
            chat_id=chat.chat_id,
            text=f"❌ Failed to start session: {e}",
        )
        return

    indicator = TurnIndicator(bot, chat.chat_id)
    await indicator.start()

    try:
        async for ev in backend.query(prompt):
            if isinstance(ev, ThinkingEvent):
                await indicator.thinking()
            elif isinstance(ev, TextEvent):
                await indicator.end_thinking()
                if ev.text.strip():
                    await send_chunked(bot, chat.chat_id, ev.text)
            elif isinstance(ev, ToolUseEvent):
                await indicator.end_thinking()
                await bot.send_message(
                    chat_id=chat.chat_id,
                    text=render_tool_call(ev.name, ev.input),
                    parse_mode=ParseMode.HTML,
                )
            elif isinstance(ev, ResultEvent):
                await indicator.end_thinking()
                if ev.error:
                    suffix = f"\n{ev.message}" if ev.message else ""
                    await bot.send_message(
                        chat_id=chat.chat_id,
                        text=f"⚠️ {suffix or 'error'}",
                    )
    except asyncio.CancelledError:
        await bot.send_message(chat_id=chat.chat_id, text="⏸ Stopped.")
        raise
    except Exception as e:
        log.exception("run_query failed")
        await bot.send_message(chat_id=chat.chat_id, text=f"❌ Error: {e}")
    finally:
        await indicator.close()


# ---- message queue / worker -----------------------------------------------

async def chat_worker(chat: ChatState, bot) -> None:
    """Long-lived loop: pull queued prompts and run them serially. One per
    chat. Started lazily by ``ensure_worker`` on the first message.

    Why a single worker per chat (vs. per session): we want one task in
    flight at a time per chat — that matches the existing UX (and
    Telegram's read-the-output cadence). Multiple sessions in one chat
    therefore time-share the worker; their queue items just stay tagged
    with their target session."""
    while True:
        item = await chat.queue.get()
        try:
            # If the session was /rm'd between enqueue and now, drop the
            # message rather than crashing or running it against a
            # half-detached object.
            if item.session.sid not in chat.sessions:
                try:
                    await bot.send_message(
                        chat_id=chat.chat_id,
                        text=(
                            f"⚠️ Dropped queued message for removed "
                            f"session #{item.session.sid}."
                        ),
                    )
                except Exception:
                    log.exception("notify drop failed")
                continue

            chat.running_sid = item.session.sid
            task = asyncio.create_task(
                run_query(chat, item.session, item.text, bot),
                name=f"run_query:{chat.chat_id}:{item.session.sid}",
            )
            chat.current_task = task
            try:
                await task
            except asyncio.CancelledError:
                # /stop cancels the inner task; we swallow here so the
                # worker keeps living for the next item (which /stop will
                # already have drained, but be defensive).
                pass
            except Exception:
                log.exception("run_query crashed")
            finally:
                chat.current_task = None
                chat.running_sid = None
        except Exception:
            log.exception("chat_worker iteration failed")
        finally:
            chat.queue.task_done()


def ensure_worker(chat: ChatState, bot) -> None:
    if chat.worker_task is None or chat.worker_task.done():
        chat.worker_task = asyncio.create_task(
            chat_worker(chat, bot),
            name=f"chat-worker:{chat.chat_id}",
        )


def drain_queue(chat: ChatState, *,
                 only_sid: Optional[str] = None) -> int:
    """Pop everything off ``chat.queue`` (or just items for one session)
    and return how many were dropped. Used by /stop and /rm."""
    if only_sid is None:
        cleared = 0
        while True:
            try:
                chat.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            chat.queue.task_done()
            cleared += 1
        return cleared

    # Selective drain: rebuild the queue without the targeted session's
    # entries. asyncio.Queue has no native filter, so we shuffle through a
    # temp list.
    keep: list[QueuedPrompt] = []
    dropped = 0
    while True:
        try:
            item = chat.queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        chat.queue.task_done()
        if item.session.sid == only_sid:
            dropped += 1
        else:
            keep.append(item)
    for item in keep:
        chat.queue.put_nowait(item)
    return dropped


def queue_depth_by_session(chat: ChatState) -> dict[str, int]:
    """Snapshot per-session queue depth without disturbing ordering. We
    poke at ``_queue`` (a deque) directly — it's an implementation detail
    of asyncio.Queue but stable across CPython versions and only used
    here for read-only display."""
    counts: dict[str, int] = {}
    for item in list(chat.queue._queue):  # type: ignore[attr-defined]
        counts[item.session.sid] = counts.get(item.session.sid, 0) + 1
    return counts


# ---- telegram handlers ----------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    _, s = get_or_create_active(update.effective_chat.id)
    await update.message.reply_text(
        f"Connected (session #{s.sid}, backend={s.backend_name}).\n"
        f"cwd: {s.cwd}\n"
        f"model: {s.model or '(backend default)'}\n\n"
        "Send a message to start.\n"
        "Commands: /sessions /switch <id> /new [backend] /rm <id> "
        "/stop /cd <path> /status",
    )


async def cmd_sessions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    c = get_chat(update.effective_chat.id)
    if not c.sessions:
        await update.message.reply_text(
            "No sessions yet. Send a message or /new to create one."
        )
        return

    running_sid = c.running_sid if (
        c.current_task and not c.current_task.done()
    ) else None
    qdepth = queue_depth_by_session(c)

    lines = ["<b>Sessions</b>"]
    for sid, s in c.sessions.items():
        marker = "▶" if sid == c.active_sid else " "
        flags = []
        if s.backend_session is not None:
            flags.append("open")
        if sid == running_sid:
            flags.append("running")
        if qdepth.get(sid):
            flags.append(f"queued: {qdepth[sid]}")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        model = s.model or "(default)"
        lines.append(
            f"{marker} <b>#{sid}</b>  <code>{html.escape(s.backend_name)}</code>  "
            f"<code>{html.escape(s.cwd)}</code>  "
            f"<i>{html.escape(model)}</i>{flag_str}"
        )
    lines.append("")
    lines.append("Switch with <code>/switch &lt;id&gt;</code>")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def cmd_switch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    args = ctx.args or []
    c = get_chat(update.effective_chat.id)
    if not args:
        await update.message.reply_text("Usage: /switch <id>")
        return
    sid = args[0].lstrip("#")
    if sid not in c.sessions:
        ids = ", ".join(f"#{x}" for x in c.sessions) or "(none)"
        await update.message.reply_text(
            f"No session #{sid}. Available: {ids}"
        )
        return
    if c.current_task and not c.current_task.done():
        await update.message.reply_text("Stop the current task first (/stop).")
        return
    c.active_sid = sid
    s = c.sessions[sid]
    await update.message.reply_text(
        f"Switched to session #{sid} ({s.backend_name}).\n"
        f"cwd: {s.cwd}\n"
        f"model: {s.model or '(default)'}"
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    c = get_chat(update.effective_chat.id)
    if c.current_task and not c.current_task.done():
        await update.message.reply_text("Stop the current task first (/stop).")
        return
    args = ctx.args or []
    backend_name = None
    if args:
        cand = args[0].lower()
        if cand not in KNOWN_BACKENDS:
            await update.message.reply_text(
                f"Unknown backend {cand!r}. Known: {', '.join(KNOWN_BACKENDS)}"
            )
            return
        backend_name = cand
    try:
        s = c.new_session(backend_name=backend_name)
    except Exception as e:
        log.exception("new_session failed")
        await update.message.reply_text(f"❌ Could not create session: {e}")
        return
    await update.message.reply_text(
        f"🆕 New session #{s.sid} (backend={s.backend_name}, active).\n"
        f"cwd: {s.cwd}\n"
        f"model: {s.model or '(default)'}"
    )


async def cmd_rm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    args = ctx.args or []
    c = get_chat(update.effective_chat.id)
    if not args:
        await update.message.reply_text("Usage: /rm <id>")
        return
    sid = args[0].lstrip("#")
    if sid not in c.sessions:
        await update.message.reply_text(f"No session #{sid}.")
        return
    if c.current_task and not c.current_task.done() and c.running_sid == sid:
        await update.message.reply_text(
            f"Session #{sid} is running. /stop first."
        )
        return
    s = c.sessions.pop(sid)
    await close_backend(s)
    dropped = drain_queue(c, only_sid=sid)
    if c.active_sid == sid:
        c.active_sid = next(iter(c.sessions), None)
    suffix = f" Dropped {dropped} queued message(s)." if dropped else ""
    if c.active_sid:
        await update.message.reply_text(
            f"Removed #{sid}. Active is now #{c.active_sid}.{suffix}"
        )
    else:
        await update.message.reply_text(
            f"Removed #{sid}. No sessions left.{suffix}"
        )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    c = CHATS.get(update.effective_chat.id)
    is_running = bool(c and c.current_task and not c.current_task.done())
    has_queue = bool(c and not c.queue.empty())

    if not c or (not is_running and not has_queue):
        await update.message.reply_text("Nothing running.")
        return

    # Drain the queue first so newly-popped items can't sneak past the
    # interrupt below.
    cleared = drain_queue(c)

    if is_running:
        running = c.sessions.get(c.running_sid) if c.running_sid else None
        if running and running.backend_session:
            try:
                await running.backend_session.interrupt()
            except Exception:
                log.exception("interrupt failed")
        # The backend's interrupt unwinds the running query naturally; we
        # don't .cancel() the task because that races with the SDK's own
        # teardown and can leave dangling state.

    if cleared:
        await update.message.reply_text(
            f"🧹 Cleared {cleared} queued message(s)."
        )


async def cmd_cd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    args = ctx.args or []
    c, s = get_or_create_active(update.effective_chat.id)
    if not args:
        await update.message.reply_text(f"#{s.sid} cwd: {s.cwd}")
        return
    new_cwd = os.path.expanduser(" ".join(args))
    if not os.path.isdir(new_cwd):
        await update.message.reply_text(f"Not a directory: {new_cwd}")
        return
    if c.current_task and not c.current_task.done() and c.running_sid == s.sid:
        await update.message.reply_text("Stop the current task first (/stop).")
        return
    await close_backend(s)
    s.cwd = new_cwd
    await update.message.reply_text(
        f"#{s.sid} cwd → {new_cwd}\n(takes effect on next message)"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    c = CHATS.get(update.effective_chat.id)
    if not c or not c.sessions:
        await update.message.reply_text("No session.")
        return
    s = c.active()
    running = bool(c.current_task and not c.current_task.done())
    await update.message.reply_text(
        f"active: #{s.sid if s else '-'}\n"
        f"backend: {s.backend_name if s else '-'}\n"
        f"cwd: {s.cwd if s else '-'}\n"
        f"model: {(s.model if s else '-') or '(default)'}\n"
        f"client: {'open' if (s and s.backend_session) else 'idle'}\n"
        f"running: {running}{f' (#{c.running_sid})' if running else ''}\n"
        f"queued: {c.queue.qsize()}\n"
        f"sessions: {len(c.sessions)}\n"
        f"pending approvals: {len(c.pending_approvals)}"
    )


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    if not (update.message and update.message.text):
        return
    c, s = get_or_create_active(update.effective_chat.id)

    busy = c.current_task is not None and not c.current_task.done()
    ensure_worker(c, ctx.bot)
    await c.queue.put(QueuedPrompt(session=s, text=update.message.text))

    # Only acknowledge if we're actually queueing behind something. If the
    # worker is idle, the message will be picked up immediately and
    # producing a "Queued" reply would just be noise on top of the agent's
    # own output.
    if busy:
        # `qsize` after put = number of items still waiting. The currently
        # running turn isn't in the queue (the worker already popped it).
        position = c.queue.qsize()
        await update.message.reply_text(
            f"📥 Queued for #{s.sid} (position {position})."
        )


async def on_callback_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    if not cq:
        return
    if not is_authorized(update):
        await cq.answer("Not authorized.", show_alert=True)
        return
    data = cq.data or ""
    if ":" not in data:
        await cq.answer()
        return
    action, approval_id = data.split(":", 1)
    c = CHATS.get(update.effective_chat.id)
    if not c:
        await cq.answer("No session.")
        return
    fut = c.pending_approvals.get(approval_id)
    if fut is None or fut.done():
        await cq.answer("Already resolved.")
        return
    fut.set_result(action == "a")
    await cq.answer("Allowed." if action == "a" else "Denied.")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN")
    if not ALLOWED_USER_IDS:
        raise SystemExit("Set ALLOWED_USER_IDS (comma-separated Telegram user ids)")
    if not os.path.isdir(DEFAULT_CWD):
        raise SystemExit(f"CLAUDE_BRIDGE_CWD does not exist: {DEFAULT_CWD}")
    if DEFAULT_BACKEND not in KNOWN_BACKENDS:
        raise SystemExit(
            f"BRIDGE_BACKEND={DEFAULT_BACKEND!r} not in {KNOWN_BACKENDS}"
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(10)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(40)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(10)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("ls", cmd_sessions))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("rm", cmd_rm))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("bridge starting (cwd=%s, backend=%s, allowed=%s)",
             DEFAULT_CWD, DEFAULT_BACKEND, sorted(ALLOWED_USER_IDS))
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
