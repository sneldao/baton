# Connect your own Telegram bot

The Telegram bridge turns one private chat into another baton device: permission
requests arrive as cards with inline **Allow once** / **Deny** buttons, and a
few commands drive the current session. It is "bring your own bot" — you create
the bot, you authorize exactly one chat (yours), and nothing about your bot is
shared with anyone else's baton instance.

> **Status: paused.** The integration is gated by
> [telegram-integration-review.md](telegram-integration-review.md). The offline
> suite (fake Telegram + fake ACP, 28 tests) passes, but the live phone
> rehearsal and scoped pairing credentials are still pending — do not run this
> against a real bot/chat you care about yet. Everything below is how it *will*
> be turned on, and works today for testing.

## What you need

- baton running on the machine that runs Vibe (`./run.sh`)
- a Telegram account on your phone
- no public tunnel, no HTTPS — Telegram polls outbound, nothing listens inbound

## Step 1 — Create the bot and get a token

1. In Telegram, open a chat with **@BotFather** (verified, blue check).
2. Send `/newbot`. Pick a display name (anything), then a username ending in
   `bot` (must be globally unique, e.g. `udinges-baton-bot`).
3. BotFather replies with a token like
   `1234567890:AAEhBOweik6ad9r_QXMENQjcrGbqCr5GpD0o`. This token **is** full
   control of the bot — treat it like a password. If it ever leaks, send
   `/revoke` to BotFather to invalidate it.

You now have `TELEGRAM_BOT_TOKEN`.

## Step 2 — Find your chat ID

The bridge authorizes exactly one private chat by its numeric ID. For a private
chat that ID equals your Telegram **user ID** — a positive number like
`123456789`. Two ways to find it:

**Via your own bot (no third parties involved):**

1. Open a chat with your new bot and send it any message (e.g. `hi`).
2. Fetch the updates it received:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```
3. In the JSON, find `"message": { "chat": { "id": 123456789, ... } }`.
   That number is your `TELEGRAM_CHAT_ID`.

If you get `{"ok":true,"result":[]}`, message the bot first and retry — a bot
only sees messages sent after it existed. Note the response contains your
message text, which is why you shouldn't paste `getUpdates` output anywhere.

**Via a lookup bot:** message **@userinfobot** and it replies with your numeric
user ID. Convenient, but it's a third party — the getUpdates method above never
leaves Telegram's own API.

Group chats do **not** work: the bridge accepts private chats only, and checks
that the chat ID, the sender ID, and the configured `TELEGRAM_CHAT_ID` all
match. Bots are rejected too.

## Step 3 — Configure `.env`

In the repo root (`.env` is gitignored; never commit it):

```
TELEGRAM_BOT_TOKEN=1234567890:AAE...
TELEGRAM_CHAT_ID=123456789
```

## Step 4 — Start baton and the bridge

```bash
./run.sh                    # terminal 1 — prints BATON_TOKEN=<token> ...
BATON_TOKEN=<token> ./telegram.sh   # terminal 2
```

`telegram.sh` loads `.env`, checks both variables are set, and runs
`tools/telegram_bridge.py`. `BATON_WS` / `BATON_HTTP` override the default
`localhost:8765` endpoints if baton runs elsewhere.

## Step 5 — Link the chat

From the operator account, open your bot and send `/start`. You should get the
linked message back. **`/start` cannot grant access** — it only confirms that
the chat named by `TELEGRAM_CHAT_ID` is reachable. Anyone else who finds the
bot and sends `/start` gets no card and no access; their messages are ignored
before any command runs.

## Commands

| Command | What it does |
| --- | --- |
| `/start` | help text + confirms the link |
| `/new` | start a new Vibe session |
| `/sessions` | list saved sessions, tap to resume |
| `/stop` | cancel the running turn |
| `/policy` | show the current trust rules |
| `/status` | bridge state, session, pending decisions |
| any other text | sent as a prompt to the current session |

Permission cards arrive with **Allow once** / **Deny** buttons. First answer
wins — if you tap on the phone first, the Telegram card closes itself, and vice
versa. There is deliberately no "always allow" from Telegram, and voice answers
are disabled there (they remain in the browser client).

## Security model (why it is this restrictive)

- One operator, one chat, configured by you at launch. No enrollment, no
  broadcast set, no self-authorization.
- Answers are bound to the exact card message the bridge issued — a forwarded
  or copied card can't be answered.
- Only `allow_once` options are approvable from Telegram.
- `.env`, keys, and secrets are refused by the trust policy before any card is
  issued to any client.

See [telegram-integration-review.md](telegram-integration-review.md) for the
full review, known pending items, and the release gate.

## Privacy line

Vibe execution stays on the laptop. Telegram receives permission-card details
(command text, file paths) and any prompts you type in the chat. Mistral
receives tool-call inputs for risk summaries. Treat both channels as shared.

## Troubleshooting

- **`set TELEGRAM_BOT_TOKEN in .env`** — `telegram.sh` didn't find `.env` in
  the repo root, or the var isn't in it.
- **`set BATON_TOKEN to the token server.py printed`** — start `./run.sh`
  first and copy the token from its output (it's random per launch unless you
  set `BATON_TOKEN` yourself).
- **`TELEGRAM_CHAT_ID must be the positive numeric ID…`** — you set a
  `@username` or negative (group) ID. Use the positive number from
  `getUpdates`.
- **Nothing arrives, `/status` says connected** — the agent hasn't hit a
  permission prompt. Commands already in `~/.vibe` allowlists (`ls`, `cat`,
  `git status`…) never produce a card; that's the feature, not a bug.
- **Bridge logs `tg poll: …`** — usually a revoked/typo'd token or no network.
  Check the token with
  `curl -s "https://api.telegram.org/bot<TOKEN>/getMe"`.
