# claude-telegram-bridge

Drive an AI coding agent on your machine from a Telegram chat. Supports
two backends:

- **claude** — [Claude Code](https://docs.claude.com/en/docs/claude-code)
  via `claude-agent-sdk` (the default).
- **opencode** — [opencode](https://opencode.ai) via its local HTTP
  server (`opencode serve`). Experimental.

Each chat can hold multiple sessions, each with its own backend, working
directory, and conversation history. Read-only tools auto-allow (claude
backend) or follow opencode's permission config; anything else (`Bash`,
`Write`, `Edit`, ...) sends an inline **Allow / Deny** prompt to your chat.

While the agent is working you'll see a live "typing…" indicator in the
chat header and a transient **💭 thinking…** message that ticks elapsed
time and finalises to **💭 thought for Ns** — so idle gaps (slow bash,
model warm-up, extended reasoning) don't make the chat feel stuck.

Sessions persist across restarts. The bridge keeps a snapshot of every
chat's sessions at `~/.claude-telegram-bridge/state.json` and reconnects
to the underlying agent (Claude SDK / opencode) with the original
session id on the next message — your conversation history survives a
reboot. You can also `/resume` an existing Claude CLI transcript from
`~/.claude/projects` and continue it through the bot.

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
  new - create a new session (optional backend arg)
  rm - remove a session by id
  stop - interrupt the current task
  cd - change working directory
  resume - adopt a Claude CLI transcript from ~/.claude/projects
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
pip install "claude-agent-sdk>=0.1.72" "python-telegram-bot>=21.0" "httpx>=0.27" "httpx-sse>=0.4"
```

### Backend prerequisites

For the **claude** backend (default), you need the **Claude CLI** installed
and authenticated on the same machine — `claude-agent-sdk` shells out to
it. See the [Claude Agent SDK docs](https://docs.claude.com/en/api/agent-sdk/overview)
for installation. Quick sanity check: `claude --version`.

For the **opencode** backend you need a running `opencode serve` on the
same machine (or any reachable host). The bridge does **not** spawn it for
you. Quick start:

```bash
# install: https://opencode.ai/docs/install
export OPENCODE_SERVER_PASSWORD="$(openssl rand -hex 16)"
opencode serve --port 4096 --hostname 127.0.0.1
```

Leave that running in another terminal. The bridge will pick up the same
`OPENCODE_SERVER_PASSWORD` from its env. If you skip the password the
server is unauthenticated — fine for `127.0.0.1` only, never expose it.

## 4. Configure environment variables

Required:

| Variable              | What it is                                             |
| --------------------- | ------------------------------------------------------ |
| `TELEGRAM_BOT_TOKEN`  | The token from BotFather                               |
| `ALLOWED_USER_IDS`    | Your numeric Telegram user ID (comma-separated for >1) |

Optional (general):

| Variable                    | Default                                  | What it is                                                          |
| --------------------------- | ---------------------------------------- | ------------------------------------------------------------------- |
| `CLAUDE_BRIDGE_CWD`         | `$HOME`                                  | Default working directory for new sessions                          |
| `BRIDGE_BACKEND`            | `claude`                                 | Default backend: `claude` or `opencode`                             |
| `CLAUDE_BRIDGE_STATE_FILE`  | `~/.claude-telegram-bridge/state.json`   | Where the bridge persists its session snapshot for restart recovery |

Optional (claude backend):

| Variable              | Default           | What it is                  |
| --------------------- | ----------------- | --------------------------- |
| `CLAUDE_BRIDGE_MODEL` | `claude-opus-4-7` | Claude model to use         |

Optional (opencode backend):

| Variable                    | Default                  | What it is                                                                |
| --------------------------- | ------------------------ | ------------------------------------------------------------------------- |
| `OPENCODE_BASE_URL`         | `http://127.0.0.1:4096`  | URL where `opencode serve` is listening                                   |
| `OPENCODE_SERVER_USERNAME`  | `opencode`               | HTTP Basic auth username (only used if password is set)                   |
| `OPENCODE_SERVER_PASSWORD`  | _(unset)_                | HTTP Basic auth password — must match what `opencode serve` was given     |
| `OPENCODE_BRIDGE_MODEL`     | _(unset)_                | Model in `<provider>/<id>` form, e.g. `anthropic/claude-sonnet-4-5`       |
| `OPENCODE_BRIDGE_AGENT`     | _(unset)_                | Default opencode agent (e.g. `build`)                                     |

Export them in your shell:

```bash
export TELEGRAM_BOT_TOKEN="123456789:ABCdef..."
export ALLOWED_USER_IDS="12345678"
export CLAUDE_BRIDGE_CWD="$HOME/workspace/some-repo"
export CLAUDE_BRIDGE_MODEL="claude-opus-4-7"

# only if you want opencode:
export BRIDGE_BACKEND="opencode"
export OPENCODE_SERVER_PASSWORD="..."
export OPENCODE_BRIDGE_MODEL="anthropic/claude-sonnet-4-5"
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
| `/new [backend]`| Create a new session and make it active. Backend is `claude` or `opencode`; defaults to `BRIDGE_BACKEND`. Old sessions stick around |
| `/rm <id>`      | Remove a session and disconnect its client                              |
| `/stop`         | Interrupt the running task                                              |
| `/cd <path>`    | Change cwd of the active session (takes effect on next message)         |
| `/resume [id\|prefix]` | Adopt a Claude CLI transcript from `~/.claude/projects` as a new bridge session. With no args, lists the most recent transcripts in the active session's cwd as inline buttons; tap one to adopt it. With an id/prefix, adopts directly without the picker |
| `/status`       | Show running state and pending approvals for the active session         |

### Multiple sessions

Each chat can hold multiple sessions, each with its own backend,
conversation history, working directory, and model. Sessions get short
numeric IDs (`1`, `2`, `3`, ...) so switching is just `/switch 2`. You
can mix backends in one chat:

```
You:  /sessions
Bot:  Sessions
      ▶ #1  claude    /Users/me/repo-a   claude-opus-4-7  [open]
        #2  opencode  /Users/me/repo-b   anthropic/claude-sonnet-4-5

You:  /new opencode
Bot:  🆕 New session #3 (backend=opencode, active).

You:  /switch 1
Bot:  Switched to session #1 (claude).
```

Only one task runs at a time per chat — `/stop` it before switching while
something is in flight.

### Persistence and `/resume`

The bridge writes a snapshot of every chat's sessions to
`~/.claude-telegram-bridge/state.json` (override with
`CLAUDE_BRIDGE_STATE_FILE`) after every meaningful change — new session,
cwd update, end-of-turn, etc. On startup it rebuilds `CHATS` and lazily
reconnects to the underlying agent client with the original session id
the next time you send a message. Live state (running tasks, queued
messages, pending approvals) is intentionally **not** persisted — those
die with the process.

`/resume` plugs into the **Claude CLI's** transcript store
(`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`). With no args
it lists the most recent transcripts for the active session's cwd as
inline buttons; tap one to spawn a new bridge session that resumes that
transcript. With an id/prefix arg (`/resume a1b2c3d4`) it adopts
directly. Because the SDK and CLI append to the same JSONL file, a
conversation started in `claude` continues seamlessly in Telegram —
and vice versa.

```
You:  /resume
Bot:  Claude CLI sessions in /Users/me/repo-a
      • a1b2c3d4 · 12m ago
         refactor the auth middleware
      • 9f8e7d6c · 2h ago
         add tests for the order pipeline
      [tap to adopt]

You:  (taps first row)
Bot:  📥 Adopted a1b2c3d4 as session #4. Send a message to continue.
```

### Live activity indicator

While a turn is running the bridge keeps Telegram's "typing…" indicator
refreshed every ~4s, so the chat header always shows the bot is busy.
The first time the agent enters a thinking burst it sends a transient
**💭 thinking…** message which ticks elapsed time; once thinking ends
it's replaced with **💭 thought for Ns**. Both Claude (`ThinkingBlock`)
and opencode (reasoning parts) are handled. The indicator is torn down
in a `finally`, so `/stop` and backend crashes won't leave a stuck
heartbeat.

### Tool permissions

**claude backend.** Read-only tools (`Read`, `Glob`, `Grep`, `WebFetch`,
`WebSearch`, `TodoWrite`, `NotebookRead`) **auto-allow**. Anything else
(`Bash`, `Write`, `Edit`, ...) sends an inline **Allow / Deny** prompt.
To change which tools auto-allow, edit `AUTO_ALLOW_TOOLS` in
`backends/claude.py`.

**opencode backend.** Permission policy lives in opencode itself
(typically `~/.config/opencode/opencode.json` or repo-local
`opencode.json`). Whatever opencode marks as `"ask"` triggers a Telegram
prompt. Example:

```json
{
  "permission": {
    "read": "allow",
    "list": "allow",
    "grep": "allow",
    "bash": "ask",
    "write": "ask",
    "edit": "ask"
  }
}
```

Approval times out after 10 minutes on either backend and defaults to deny.

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
- **opencode `failed to start session: ConnectError`** — `opencode serve`
  isn't running, or `OPENCODE_BASE_URL` doesn't match its `--port`.
- **opencode `401 Unauthorized`** — `OPENCODE_SERVER_PASSWORD` mismatch
  between the bridge and the server, or set on one but not the other.
- **Approval buttons do nothing** — the message must come from a
  whitelisted user. Callback queries from anyone else are rejected.
- **`/resume` says "No Claude CLI sessions found"** — the bridge looks
  in `~/.claude/projects/<encoded-cwd>/`. Make sure the active session's
  cwd matches the directory you used `claude` in. The error message
  prints the exact path it checked.
- **Restart didn't restore my sessions** — check that the bridge can
  write to `~/.claude-telegram-bridge/`. Look for a "could not write
  state file" warning in the logs. Set `CLAUDE_BRIDGE_STATE_FILE` to
  point somewhere writable if the default isn't.
- **Stuck "typing…" or "💭 thinking…" message** — should clear on the
  next turn boundary; if it doesn't, `/stop` and try again. The
  heartbeat is wrapped in a `finally`, so the most likely cause is the
  backend hanging, not the indicator.

## Security notes

- The bot can run arbitrary `Bash` / `Write` / `Edit` on your machine when
  you tap **Allow**. Treat the chat like a root shell.
- Anyone with your `TELEGRAM_BOT_TOKEN` can impersonate the bot, but they
  still can't drive it without being in `ALLOWED_USER_IDS`.
- Don't commit your token. Use env vars or a gitignored `.env`.
