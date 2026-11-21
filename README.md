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
MISTRAL_API_KEY=… uv run python server.py
```

Then open the printed URL on your phone (or scan `/qr.png` on the host).

Config env vars: `PORT` (8765), `VIBE_ACP_CMD` (default: `vibe-acp` on PATH,
else `uv run` in a sibling `mistral-vibe` clone), `VIBE_REMOTE_TOKEN`
(default: random per launch).

## Demo story

Start a refactor at your desk → walk away → permission prompt vibrates on
your phone → say "yes, go ahead" → Voxtral transcribes → agent keeps working.
