# baton 🏃

Pass the baton: drive **Mistral Vibe** from your phone. Native remote mode for
[mistral-vibe#1535](https://github.com/mistralai/mistral-vibe/issues/1535) —
built on `vibe-acp` (Agent Client Protocol), voice-native via **Voxtral**.

```
phone browser (PWA)  ──WebSocket──▶  baton  ──stdio/JSON-RPC──▶  vibe-acp
```

## Features

- List + resume sessions (`session/list` / `session/load`)
- Stream agent turns live (`session/update`)
- **Remote permission approvals** (`session/request_permission`) — tap, or
  *speak* your answer: phone mic → Voxtral → approve/reject
- Voice dictation for prompts (Voxtral)
- QR pairing over LAN, bearer-token auth
- Stop a running turn (`session/cancel`)

## Run

```bash
uv sync
MISTRAL_API_KEY=… ./run.sh                      # stock vibe-acp
MISTRAL_API_KEY=… ./run.sh /path/to/vibe/fork   # the team's fork
```

Then open the printed URL on your phone (or scan `/qr.png` on the host).

Config env vars: `PORT` (8765), `VIBE_ACP_CMD` (any command, see `run.sh`),
`BATON_TOKEN` (default: random per launch).

## Integration contract (for teammates)

Baton needs **nothing** from the vibe fork beyond a working `vibe-acp`.
Whatever your branch adds surfaces automatically:

| Your feature | How it shows up in baton |
| --- | --- |
| New tools / tool calls | `session/update` → rendered as cards in the stream |
| New permission triggers | `session/request_permission` → phone approve/reject card |
| New modes | `session/set_mode` → mode picker, populated from `session/new` |
| Anything over MCP | configure on the session as usual (`mcpServers`) |

Test your branch against baton: `./run.sh /path/to/your/branch`, then:

- [ ] `session/new` returns a sessionId without errors
- [ ] a prompt produces streamed output on the phone
- [ ] your feature's permission prompt (if any) reaches the phone and
      approve/reject unblocks the agent
- [ ] `session/list` + resume still works

If it works over ACP, it works in baton. If your branch changes ACP methods
themselves, tell me — that's the one place we can conflict.

## Development

```bash
brew install gitleaks   # secrets scan runs as a local pre-commit hook
pre-commit install
pre-commit run --all-files
```

Hooks: ruff lint/format, yaml/toml/json hygiene, `detect-private-key`,
gitleaks. Never commit `MISTRAL_API_KEY` or `.env` — they stay in the
environment or `~/.vibe/.env`.

## Demo story

Start a refactor at your desk → walk away → permission prompt vibrates on
your phone → say "yes, go ahead" → Voxtral transcribes → agent keeps working.
