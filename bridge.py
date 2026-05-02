"""
Claude Code <-> Telegram bridge.

Lets you drive Claude Code on this Mac from a Telegram chat. Each chat is its
own continuous Claude session. Read-only tools auto-allow; Bash/Write/Edit
prompt with Allow/Deny buttons.

Setup
-----
1. @BotFather on Telegram -> /newbot -> save the token.
2. @userinfobot on Telegram -> save your numeric user id.
3. Export env vars:
       export TELEGRAM_BOT_TOKEN=...
       export ALLOWED_USER_IDS=12345              # comma-separated
       export CLAUDE_BRIDGE_CWD=$HOME/some/repo   # optional, default $HOME
       export CLAUDE_BRIDGE_MODEL=claude-opus-4-7 # optional
4. uv run python bridge.py

Commands in chat: /start /new /stop /cd <path> /status
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import uuid
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
DEFAULT_MODEL = os.environ.get("CLAUDE_BRIDGE_MODEL", "claude-opus-4-7")

AUTO_ALLOW_TOOLS = {
    "Read", "Glob", "Grep", "WebFetch", "WebSearch",
    "TodoWrite", "NotebookRead",
}

MAX_MSG_LEN = 3500
APPROVAL_TIMEOUT_S = 600


@dataclass
class ChatSession:
    chat_id: int
    cwd: str
    model: str
    client: Optional[ClaudeSDKClient] = None
    current_task: Optional[asyncio.Task] = None
    pending_approvals: dict[str, asyncio.Future] = field(default_factory=dict)


SESSIONS: dict[int, ChatSession] = {}


# ---------- auth ----------

def is_authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def deny(update: Update) -> None:
    if update.effective_chat:
        await update.effective_chat.send_message("Not authorized.")
    log.warning("rejected user_id=%s",
                update.effective_user.id if update.effective_user else None)


# ---------- telegram send helpers ----------

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
    if name == "Bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        body = f"<pre>{html.escape(cmd[:1500])}</pre>"
        if desc:
            body = f"<i>{html.escape(desc)}</i>\n{body}"
        return f"🔧 <b>Bash</b>\n{body}"
    if name in ("Write", "Edit"):
        path = inp.get("file_path", "")
        return f"✏️ <b>{name}</b> <code>{html.escape(path)}</code>"
    if name == "Read":
        return f"📖 <b>Read</b> <code>{html.escape(inp.get('file_path', ''))}</code>"
    if name == "Glob":
        return f"🔍 <b>Glob</b> <code>{html.escape(inp.get('pattern', ''))}</code>"
    if name == "Grep":
        return f"🔍 <b>Grep</b> <code>{html.escape(inp.get('pattern', ''))}</code>"
    blob = json.dumps(inp, indent=2, default=str)[:1200]
    return f"🔧 <b>{html.escape(name)}</b>\n<pre>{html.escape(blob)}</pre>"


# ---------- permission flow ----------

def make_can_use_tool(session: ChatSession, bot):
    async def can_use_tool(tool_name, input_data, context):
        if tool_name in AUTO_ALLOW_TOOLS:
            return PermissionResultAllow()

        approval_id = uuid.uuid4().hex[:8]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        session.pending_approvals[approval_id] = future

        prompt_text = (
            "⚠️ <b>Permission requested</b>\n\n"
            + render_tool_call(tool_name, input_data)
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Allow", callback_data=f"a:{approval_id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"d:{approval_id}"),
        ]])

        try:
            msg = await bot.send_message(
                chat_id=session.chat_id, text=prompt_text,
                parse_mode=ParseMode.HTML, reply_markup=keyboard,
            )
        except Exception as e:
            session.pending_approvals.pop(approval_id, None)
            log.exception("failed to send approval prompt")
            return PermissionResultDeny(message=f"bridge error: {e}")

        try:
            allowed = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            try:
                await bot.edit_message_text(
                    chat_id=session.chat_id, message_id=msg.message_id,
                    text=prompt_text + "\n\n<i>⌛ Timed out — denied</i>",
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest:
                pass
            return PermissionResultDeny(message="approval timed out")
        finally:
            session.pending_approvals.pop(approval_id, None)

        try:
            await bot.edit_message_text(
                chat_id=session.chat_id, message_id=msg.message_id,
                text=prompt_text + (
                    "\n\n<i>✅ Allowed</i>" if allowed else "\n\n<i>❌ Denied</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            pass

        return PermissionResultAllow() if allowed else PermissionResultDeny(
            message="user denied"
        )

    return can_use_tool


# ---------- session lifecycle ----------

async def get_session(chat_id: int) -> ChatSession:
    s = SESSIONS.get(chat_id)
    if s is None:
        s = ChatSession(chat_id=chat_id, cwd=DEFAULT_CWD, model=DEFAULT_MODEL)
        SESSIONS[chat_id] = s
    return s


async def ensure_client(session: ChatSession, bot) -> ClaudeSDKClient:
    if session.client is not None:
        return session.client
    options = ClaudeAgentOptions(
        model=session.model,
        cwd=session.cwd,
        permission_mode="default",
        can_use_tool=make_can_use_tool(session, bot),
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    session.client = client
    return client


async def close_client(session: ChatSession) -> None:
    if session.client is None:
        return
    try:
        await session.client.disconnect()
    except Exception:
        log.exception("disconnect failed")
    session.client = None


# ---------- driving a turn ----------

async def run_query(session: ChatSession, prompt: str, bot) -> None:
    try:
        client = await ensure_client(session, bot)
    except Exception as e:
        log.exception("ensure_client failed")
        await bot.send_message(
            chat_id=session.chat_id,
            text=f"❌ Failed to start session: {e}",
        )
        return

    try:
        await bot.send_chat_action(chat_id=session.chat_id, action=ChatAction.TYPING)
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        if block.text.strip():
                            await send_chunked(bot, session.chat_id, block.text)
                    elif isinstance(block, ToolUseBlock):
                        await bot.send_message(
                            chat_id=session.chat_id,
                            text=render_tool_call(block.name, block.input),
                            parse_mode=ParseMode.HTML,
                        )
                    elif isinstance(block, ThinkingBlock):
                        pass
            elif isinstance(message, ResultMessage):
                if message.is_error or message.subtype != "success":
                    suffix = f"\n{message.result}" if message.result else ""
                    await bot.send_message(
                        chat_id=session.chat_id,
                        text=f"⚠️ {message.subtype}{suffix}",
                    )
    except asyncio.CancelledError:
        await bot.send_message(chat_id=session.chat_id, text="⏸ Stopped.")
        raise
    except Exception as e:
        log.exception("run_query failed")
        await bot.send_message(chat_id=session.chat_id, text=f"❌ Error: {e}")


# ---------- telegram handlers ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    s = await get_session(update.effective_chat.id)
    await update.message.reply_text(
        "Connected to Claude Code.\n"
        f"cwd: {s.cwd}\n"
        f"model: {s.model}\n\n"
        "Send a message to start. Commands: /new /stop /cd <path> /status",
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    s = await get_session(update.effective_chat.id)
    if s.current_task and not s.current_task.done():
        if s.client:
            try:
                await s.client.interrupt()
            except Exception:
                pass
        s.current_task.cancel()
    await close_client(s)
    await update.message.reply_text("🆕 Fresh session.")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    s = SESSIONS.get(update.effective_chat.id)
    if not s or not s.current_task or s.current_task.done():
        await update.message.reply_text("Nothing running.")
        return
    if s.client:
        try:
            await s.client.interrupt()
        except Exception:
            log.exception("interrupt failed")


async def cmd_cd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    args = ctx.args or []
    s = await get_session(update.effective_chat.id)
    if not args:
        await update.message.reply_text(f"cwd: {s.cwd}")
        return
    new_cwd = os.path.expanduser(" ".join(args))
    if not os.path.isdir(new_cwd):
        await update.message.reply_text(f"Not a directory: {new_cwd}")
        return
    if s.current_task and not s.current_task.done():
        await update.message.reply_text("Stop the current task first (/stop).")
        return
    await close_client(s)
    s.cwd = new_cwd
    await update.message.reply_text(f"cwd → {new_cwd}\n(takes effect on next message)")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    s = SESSIONS.get(update.effective_chat.id)
    if not s:
        await update.message.reply_text("No session.")
        return
    running = bool(s.current_task and not s.current_task.done())
    await update.message.reply_text(
        f"cwd: {s.cwd}\n"
        f"model: {s.model}\n"
        f"client: {'open' if s.client else 'idle'}\n"
        f"running: {running}\n"
        f"pending approvals: {len(s.pending_approvals)}"
    )


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return await deny(update)
    if not (update.message and update.message.text):
        return
    s = await get_session(update.effective_chat.id)
    if s.current_task and not s.current_task.done():
        await update.message.reply_text("Already running. /stop to interrupt.")
        return
    s.current_task = asyncio.create_task(
        run_query(s, update.message.text, ctx.bot),
        name=f"run_query:{s.chat_id}",
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
    s = SESSIONS.get(update.effective_chat.id)
    if not s:
        await cq.answer("No session.")
        return
    fut = s.pending_approvals.get(approval_id)
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
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("bridge starting (cwd=%s, model=%s, allowed=%s)",
             DEFAULT_CWD, DEFAULT_MODEL, sorted(ALLOWED_USER_IDS))
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
