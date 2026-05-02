# claude-telegram-bridge

Drive [Claude Code](https://docs.claude.com/en/docs/claude-code) on your
machine from a Telegram chat. Each chat is its own continuous Claude
session. Read-only tools auto-allow; anything that mutates state (`Bash`,
`Write`, `Edit`, ...) sends an inline **Allow / Deny** prompt to your chat.

The bridge runs **on your computer**. Telegram is just the front-end. Your
bot only takes orders from user IDs you whitelist, so nobody else can talk
to it.

---

## 1. Create the bot in Telegram

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`.
3. Pick a **display name** (anything, e.g. `My Claude Bridge`).
4. Pick a **username** ending in `bot` (e.g. `myclaudebridge_bot`). Must be
   globally unique.
5. BotFather replies with an **HTTP API token** that looks like:

   ```
   123456789:ABCdefGhIJKlmnOPQrstuvWXYz0123456789
   ```

   Save it. This is your `TELEGRAM_BOT_TOKEN`. Treat it like a password —
   anyone with this token can control the bot.

Optional but recommended:

- `/setprivacy` → select your bot → **Disable**. Lets the bot see all
  messages (only matters if you ever add it to a group; for 1:1 chats it
  doesn't matter).
- `/setcommands` → paste this so the slash menu shows up in chat:

  ```
  start - show active session info
  sessions - list all sessions in this chat
  switch - switch active session by id
  new - create a new session and switch to it
  rm - remove a session by id
  stop - interrupt the current task
  cd - change working directory
  status - show active session status
  ```

## 2. Find your Telegram user ID

The bridge only responds to whitelisted numeric user IDs.

1. Message [@userinfobot](https://t.me/userinfobot).
2. It replies with your numeric **Id** (e.g. `12345678`).
3. Save it. This is your `ALLOWED_USER_IDS` value. You can list multiple,
   comma-separated, if you want to share access.

## 3. Clone and install

```bash
git clone <this-repo-url> claude-telegram-bridge
cd claude-telegram-bridge

# uv handles the venv + Python 3.12+ automatically
uv sync
```

If you don't have `uv`, install it from
<https://docs.astral.sh/uv/getting-started/installation/>, or use plain
`pip` inside a Python 3.12+ venv:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install "claude-agent-sdk>=0.1.72" "python-telegram-bot>=21.0"
```

You also need the **Claude CLI** installed and authenticated on the same
machine — `claude-agent-sdk` shells out to it. See the
[Claude Agent SDK docs](https://docs.claude.com/en/api/agent-sdk/overview)
for installation. A quick sanity check:

```bash
claude --version
```

## 4. Configure environment variables

Required:

| Variable              | What it is                                             |
| --------------------- | ------------------------------------------------------ |
| `TELEGRAM_BOT_TOKEN`  | The token from BotFather                               |
| `ALLOWED_USER_IDS`    | Your numeric Telegram user ID (comma-separated for >1) |

Optional:

| Variable              | Default           | What it is                              |
| --------------------- | ----------------- | --------------------------------------- |
| `CLAUDE_BRIDGE_CWD`   | `$HOME`           | Default working directory for sessions  |
| `CLAUDE_BRIDGE_MODEL` | `claude-opus-4-7` | Claude model to use                     |

Export them in your shell:

```bash
export TELEGRAM_BOT_TOKEN="123456789:ABCdef..."
export ALLOWED_USER_IDS="12345678"
export CLAUDE_BRIDGE_CWD="$HOME/workspace/some-repo"
export CLAUDE_BRIDGE_MODEL="claude-opus-4-7"
```

Or drop them in a `.env` file and `source` it before running. Keep that
file out of git.

## 5. Run the bridge

```bash
uv run python bridge.py
```

You should see:

```
bridge starting (cwd=..., model=..., allowed=[12345678])
```

## 6. Talk to your bot

1. Open the bot's chat in Telegram (search by the username you picked, or
   click the `t.me/<username>` link BotFather gave you).
2. Press **Start** or send `/start`.
3. Send any message — the bridge runs it as a Claude Code prompt in
   `CLAUDE_BRIDGE_CWD`.

### Commands

| Command         | Effect                                                                  |
| --------------- | ----------------------------------------------------------------------- |
| `/start`        | Show active session (cwd / model / id)                                  |
| `/sessions`     | List all sessions in this chat (alias: `/ls`). Active is marked `▶`     |
| `/switch <id>`  | Switch the active session to the given short id (e.g. `/switch 2`)      |
| `/new`          | Create a new session and make it active. Old sessions stick around      |
| `/rm <id>`      | Remove a session and disconnect its client                              |
| `/stop`         | Interrupt the running task                                              |
| `/cd <path>`    | Change cwd of the active session (takes effect on next message)         |
| `/status`       | Show running state and pending approvals for the active session         |

### Multiple sessions

Each chat can hold multiple Claude sessions, each with its own conversation
history, working directory, and model. Sessions get short numeric IDs (`1`,
`2`, `3`, ...) so switching is just `/switch 2`.

```
You:  /sessions
Bot:  Sessions
      ▶ #1  /Users/me/repo-a   claude-opus-4-7  [open]
        #2  /Users/me/repo-b   claude-opus-4-7

You:  /switch 2
Bot:  Switched to session #2.
```

Only one task runs at a time per chat — `/stop` it before switching while
something is in flight.

### Tool permissions

- Read-only tools (`Read`, `Glob`, `Grep`, `WebFetch`, `WebSearch`,
  `TodoWrite`, `NotebookRead`) **auto-allow**.
- Anything else (`Bash`, `Write`, `Edit`, etc.) sends an inline
  **Allow / Deny** prompt to your chat. Approval times out after 10
  minutes and defaults to deny.

To change which tools auto-allow, edit `AUTO_ALLOW_TOOLS` in `bridge.py`.

## 7. Keep it running (optional)

The bridge is a foreground Python process. A few options to keep it up:

- **tmux/screen**: `tmux new -s bridge` then run the command.
- **launchd (macOS)**: drop a `.plist` in `~/Library/LaunchAgents/` that
  runs `uv run python /path/to/bridge.py` with the env vars.
- **systemd (Linux)**: a user service unit with the env vars in `Environment=`.

## Troubleshooting

- **`Set TELEGRAM_BOT_TOKEN` / `Set ALLOWED_USER_IDS`** — env vars not in
  the shell that started the bridge. `echo $TELEGRAM_BOT_TOKEN` to verify.
- **Bot replies "Not authorized."** — your user ID isn't in
  `ALLOWED_USER_IDS`, or you're messaging from a different account than
  the one @userinfobot showed you.
- **Bot never replies, no errors** — make sure only one instance of
  `bridge.py` is running. Telegram long-polling kicks the older one off.
- **`claude: command not found` in logs** — install and authenticate the
  Claude CLI on the host running the bridge.
- **Approval buttons do nothing** — the message must come from a
  whitelisted user. Callback queries from anyone else are rejected.

## Security notes

- The bot can run arbitrary `Bash` / `Write` / `Edit` on your machine when
  you tap **Allow**. Treat the chat like a root shell.
- Anyone with your `TELEGRAM_BOT_TOKEN` can impersonate the bot, but they
  still can't drive it without being in `ALLOWED_USER_IDS`.
- Don't commit your token. Use env vars or a gitignored `.env`.
