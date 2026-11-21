# Native Vibe CLI approval demo

Status: opt-in demo path. Verified live once against the installed Vibe
3.1.4 TUI: a native `file_system.bash` approval was mirrored to Telegram,
the operator tapped **Allow once**, Vibe accepted the `callback/result`,
and the requested `touch` completed (`exit code 0`, file present).

`native.sh` launches the real installed Vibe TUI with `native_cli.py`
sitting in its app-server slot. The shim spawns the real
`vibe-app-server` underneath and relays JSON-RPC frames both ways. When
Vibe emits a tool `callback/call` approval request **and** a baton
client is connected, the shim acknowledges the callback delivery itself
(so the TUI never opens its own modal) and mirrors it as an ordinary
baton permission card. The operator's Allow once / Deny tap is delivered
back to Vibe as a `callback/result`.

- Phone-only approval: while the Telegram bridge is connected, the
  decision lands on the phone card. With **no** baton client connected,
  every frame passes through untouched and the TUI's native approval
  dialog works exactly as before.
- Approvals only. No remote prompts, no trust/connector/user-input
  callbacks, no permanent grants (permanent choices are never mirrored),
  no public tunnel. With `--approval-policy=accept-edits`, Vibe auto-runs
  file edits and sends shell calls to baton; baton auto-approves only
  commands it parses as read-only and cards everything else.
- The existing ACP + Telegram flow on its own port is unaffected; this
  is a second, loopback-only listener (default `127.0.0.1:8767`).

## Run it

Terminal 1 — pick a fresh token and launch the TUI:

```bash
export BATON_TOKEN=$(openssl rand -hex 32)
bash ./native.sh --trust --workdir /path/to/scratch --approval-policy=accept-edits
```

Terminal 2 — attach the existing Telegram bridge to the native port:

```bash
BATON_TOKEN=<same-secret> \
BATON_WS=ws://127.0.0.1:8767/ws \
BATON_HTTP=http://127.0.0.1:8767 \
./telegram.sh
```

Notes:

- Run exactly one Telegram poller per bot token.
- No `./tunnel.sh` — the listener is loopback-only by design.
- Do not run a second Vibe session against the same app server; prompts
  and session control stay in the TUI (the shim rejects remote
  `call`/`notify`). Use `--approval-policy=accept-edits` for the realistic
  demo: Vibe auto-runs file edits, every shell call reaches baton, and
  baton auto-approves only commands it parses as read-only (`ls`, `find`
  without `-delete`/`-exec`, `git status`, pipelines of those, and so on).
  Deletes, writes, network commands, git mutations, and anything outside
  that allowlist still produce a Telegram card. Use
  `--approval-policy=ask` only when you need a card for every call.
- Environment overrides: `BATON_NATIVE_PORT`, `BATON_NATIVE_APP_SERVER`,
  `BATON_VIBE_PYTHON`. The Telegram bot token stays in the telegram
  process; `native.sh` does not source `.env`.
