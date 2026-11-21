# Native Vibe CLI approval demo

Status: opt-in demo path. Verified live against the installed Vibe 3.1.4
TUI: a native tool approval was mirrored to Telegram, the operator tapped
**Allow once**, Vibe accepted the `callback/result`, and the requested
command completed (`exit code 0`, file present).

## What it does

`native.sh` launches the real installed Vibe TUI with `native_cli.py`
sitting in its app-server slot. The shim spawns the real
`vibe-app-server` underneath and relays JSON-RPC frames both ways. When
Vibe emits a tool `callback/call` approval request **and** a baton client
is connected, the shim acknowledges callback delivery itself (so the TUI
never opens its own modal) and mirrors it as an ordinary baton permission
card. The operator's Allow once / Deny tap is delivered back to Vibe as a
`callback/result`.

With `--approval-policy=accept-edits`, Vibe auto-runs file edits and sends
shell calls to baton. Baton auto-approves only commands it parses as
read-only (`ls`, `cat`, `grep`, `find` without `-delete`/`-exec`,
read-only `git` such as `status`/`diff`/`log`, and pipelines of those).
Deletes, writes, network commands, git mutations, sensitive paths, and
anything outside that allowlist still produce a Telegram card.
`--approval-policy=ask` makes Vibe ask for every call; use it only when
you need a card for every tool.

- Phone-only approval: while the Telegram bridge is connected, the
  decision lands on the phone card. With **no** baton client connected,
  every frame passes through untouched and the TUI's native approval
  dialog works exactly as before.
- Approvals only. No remote prompts, no trust/connector/user-input
  callbacks, no permanent grants (permanent choices are never mirrored),
  no public tunnel.
- Loopback-only listener (default `127.0.0.1:8767`); the ACP + Telegram
  flow on its own port is unaffected.

## Prerequisites

- baton repo checked out; run `uv sync` once so `uv run --no-sync` has an
  environment.
- `vibe` and `vibe-app-server` on `PATH`, plus `uv`. `native.sh` resolves
  the Python interpreter from the `vibe` wrapper.
- A Telegram bot + authorized private chat: follow
  [telegram-setup.md](telegram-setup.md). You need `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID` in `.env` or the environment for `telegram.sh`.
- `BATON_TOKEN`: any shared secret. `native.sh` requires it and
  `telegram.sh` must use the same value.

## Run it

Terminal 1 — launch the TUI:

```bash
cd /path/to/baton
export BATON_TOKEN=$(openssl rand -hex 32)   # or reuse an existing demo token
bash ./native.sh --trust --workdir /path/to/scratch --approval-policy=accept-edits
```

Terminal 2 — attach the Telegram bridge to the native listener:

```bash
BATON_TOKEN=<same-secret> \
BATON_WS=ws://127.0.0.1:8767/ws \
BATON_HTTP=http://127.0.0.1:8767 \
./telegram.sh
```

`telegram.sh` loads `.env`; `native.sh` does not, so export `BATON_TOKEN`
in the TUI shell.

## Preflight before you prompt

```bash
lsof -nP -iTCP:8767 -sTCP:LISTEN   # expect python / native_cli.py
tail -5 /tmp/telegram-native.log  # or your log path; expect "connected to ws://127.0.0.1:8767/ws"
```

In Telegram send `/status`; the bridge should be connected. Only then
submit the prompt.

## Demo prompt

```text
Create demo.txt containing hello, run ls, then delete /tmp/baton-delete-target.txt.
```

Expected: the file write auto-passes under `accept-edits`, `ls`
auto-approves silently, and the `rm` produces the Telegram card. Tap
**Allow once**; Vibe continues without a local keystroke.

## Troubleshooting

- **Connection refused on 8767 / Telegram reconnect loop:** `native.sh` is
  not running or exited. Relaunch it and wait for the listener before
  prompting.
- **No card for `ls` or `git status`:** expected — baton auto-approved
  read-only shell. Use `rm`, a write outside the workdir, a network
  command, or `--approval-policy=ask` for a guaranteed card.
- **Card went to the local TUI instead of Telegram:** no baton client was
  connected when the callback fired. Recheck `/status` and the listener
  before prompting.
- **Do not run a second Vibe session against the same app server** —
  prompts and session control stay in the TUI (the shim rejects remote
  `call`/`notify`).
- Environment overrides: `BATON_NATIVE_PORT`, `BATON_NATIVE_APP_SERVER`,
  `BATON_VIBE_PYTHON`. The Telegram bot token stays in the telegram
  process; `native.sh` does not source `.env`.
