# Telegram integration review

Status: Phase 1 hardening and the core Phase 2 protocol fixes have passed offline checks. The live integration remains paused; pushing source changes does not clear the release gate or authorize restarting the servers, Telegram poller, or public tunnel.

Baton moves authority, not execution. Only an explicitly authorized operator can act, an answer must target one request and one offered action, and receipts must reflect the server's result.

## Findings at the start of the review

- **Public QR leaked the shared bearer.** `/qr.png` was served unauthenticated; anyone who could reach the port got a QR encoding the shared access token.
- **`/start` self-enrolled callers and the chat pin was additive.** Any Telegram user who found the bot could `/start` and be added to the broadcast set; `TELEGRAM_CHAT_ID` only *added* a chat, it did not exclude others.
- **WebSocket read loop blocked on prompt.** The Baton server awaited `session/prompt` inside the read path, so it could not consume control messages on the same connection while the turn was active.
- **Optimistic receipts and racing card edits.** Telegram stamped "approved/rejected" as soon as it sent a tap, before the server resolved the request. Timer and annotation edits could race the final Telegram edit and restore buttons.
- **Unbound substring-matched voice could select any allow.** A voice note containing "yes" could pick the first `allow*` option, including `allow_always` if offered first. The pending request was picked after transcription, rather than bound when the note was received.
- **Session selection followed arbitrary traffic and reconnect did not reconnect.** The bridge adopted whatever `sessionId` appeared in stream traffic, and the reconnect path did not restore bridge state.
- **No checked-in integration tests** covering the authorization boundary.
- The existing policy engine only sees ACP permission requests; it cannot guarantee all destructive actions are intercepted.

## Phase 1 (done)

- Guard `/qr.png` behind the token and mark it `Cache-Control: no-store`; stop printing an unauthenticated QR URL at startup.
- Require exactly one explicit operator: `TELEGRAM_CHAT_ID` must name the authorized private chat; every message and callback is verified against it (private chat, matching chat id, matching sender id, not a bot). `/start` confirms access but cannot grant it.
- Bind callbacks to the issued chat/message: an answer is only accepted from the card message that offered it, and only exact `allow_once` options are approvable — plus Deny.
- Remove the Telegram control surface that could widen authority: voice answers, permanent (`allow_always`) grants, the Mini App button, `/auto` policy mutation, and additive chat enrollment.

## Phase 2

Implemented (core, offline-verified):

- The server processes control messages while a prompt RPC is active (prompts run as concurrent tasks on the same socket), resolves each request once after validating the answer against the offered options, and emits the authoritative `agent_resolved` with decision, source, optionId and elapsed time. Public opaque request ids replace raw ACP ids on the wire; a `permission_snapshot` frame precedes replay on connect; session cancels close that session's pending cards; expiry and disconnect are handled without optimistic receipts.
- Telegram and browser cards reconcile on reconnect via the snapshot; the selected session is explicit and no longer follows stream traffic; Telegram answers bind the issued chat/message and a per-card lock prevents late edits from resurrecting buttons.

Still pending:

- End-to-end rehearsal: fake Telegram + fake ACP scenario tests beyond the boundary contract (including a rejected message payload) are now checked in (`tests/test_telegram_e2e_fake.py`, run with `uv run --with pytest pytest tests/`); one live pass on the real phone is still pending.
- Make Mistral summaries advisory for the demo — not model-authorized auto passes.
- Replace the shared bearer with scoped, expiring pairing credentials before any public rollout.

## Privacy

Vibe execution stays local to the laptop. Telegram receives card details and carries prompts you type there. Mistral receives tool-call inputs for risk summaries. Treat both channels as shared.

## Release gate

Prove that an unpaired user can't read or act; that a turn started in Telegram can be approved, denied, or stopped there without a browser connected; that racing a phone tap against a Telegram tap yields one outcome and matching receipts; and that reconnect restores a pending card without reviving an expired one. Automate those against fake Telegram and ACP endpoints, including a rejected message payload, then rehearse the exact flow once on the real phone.
