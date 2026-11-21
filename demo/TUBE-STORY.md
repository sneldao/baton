# The tube story (layered demo: baton + computer-use fork)

The full pitch in one scene: laptop stays at home, plugged in and awake. You
are on the tube. An email arrives that can only be handled on that laptop.
You trigger the task from your phone, the agent drives the Mac's GUI through
the teammate's computer-use MCP, and every risky step lands on your phone as
a baton card.

Baton repo + the `computer-use-mcp` branch of the vibe fork
(`luke-h-code/mistral-hackathon`, PR #2). No code merge: baton runs the
existing `../mistral-vibe` clone (the branch adds no `vibe-acp` changes);
the MCP registers at user level with `vibe mcp add`.

## Desk-time setup (do ALL of this before you leave the house)

- [ ] Fork checked out at `../mistral-hackathon` (shallow is fine: the
      branch only adds `scripts/computer-use/` — its `vibe-acp` is
      identical to upstream 3.1.4)
- [ ] **Don't `uv sync` the fork** on a space-tight Mac — building its Rust
      harness costs >1GB and buys nothing: baton runs the existing sibling
      `../mistral-vibe` (or the installed `vibe-acp`) and the MCP server is
      self-contained (`uv run --no-project --script`)
- [ ] Swift helper built (see *Enabling computer-use later* below) and the
      MCP server answers
      `--help`; register with `vibe mcp add computer-use …` and confirm
      with `/mcp` in a scratch Vibe session
- [ ] System Settings → Privacy & Security: **Screen & System Audio
      Recording** + **Accessibility** granted to the terminal that launches
      baton; relaunch it after granting
- [ ] `caffeinate -ims` in a spare terminal so the Mac never sleeps; the
      session must stay **unlocked** — computer-use cannot drive a locked
      screen
- [ ] Firefox open with the demo page (`scripts/computer-use/demo.html` in
      the fork) or Gmail already signed in
- [ ] `./run.sh` (sibling clone), then `./tunnel.sh` for the https URL;
      open the URL on the phone, pair via QR, stage view on `?stage`
- [ ] Mode set to `ask` (like the main demo: accept-edits silently kills
      permission beats)
- [ ] **Window mode, not display mode** for the live demo (see Safety) —
      register with `--window`/`--pid` pinned to the Firefox window

## Beats

1. **Walk away.** Stage view (`?stage`) shows the room: ticker, device
   counter. Phone in your pocket, laptop stays on the desk.
2. **The email.** Get a (staged) email on the phone: only-doable-on-the-Mac.
   For the strongest beat, the same email is sitting unread in Gmail in
   Firefox on the Mac — the agent will read the very email you just got.
3. **Trigger from the phone.** Voice-dictate the prompt over the tunnel URL:
   > Open Gmail in Firefox and read the newest email. Do what it asks —
   > submit the form it links to.

   Expect the first card ~10–30s in (`screenshot` is read-only but still
   asks in `ask` mode — good: it proves the phone sees everything).
4. **Approve the ladder.** Each `click` / `type_text` / `press_keys` call
   is a card with the Mistral risk line: approve 2–3 by tap, one by voice
   ("yes, go ahead"). Live ticker on stage shows auto-decisions vs. yours.
5. **The tunnel moment.** Mid-card, toggle airplane mode for 20s. Nothing is
   lost: the pending card replays on reconnect and the agent kept working.
   This is the beat judges remember — rehearse it once so you know it's
   true on the day.
6. **The reject.** The task's last step should be one genuinely risky call
   (e.g. the form's Delete button, or an outbound `curl`). Swipe deny; the
   agent adapts. "Reject isn't a crash, it's a steering wheel."
7. **Close on the before/after.** In `ask` mode this email waits until
   you're home; with blanket auto-approve the Mac does whatever it likes
   unsupervised. Baton: answered in ~10s from anywhere, one-way doors still
   escalate, deny rules still refuse.

## Safety (the judges' question you WANT)

Display mode lets the model click anything on screen — including the
terminal running Vibe and its own approval prompts, and in this demo nobody
is watching the desk. So the tube story runs the tight posture on purpose:

- **Window mode** pins computer-use to the Firefox window.
- **baton's trust policy** runs in front of every card: deny rules
  (`.env`, keys, secrets) never reach the phone; one-way doors escalate.
- The line: "remote approvals didn't loosen the guardrails — the phone is
  *where* the guardrails live now."

## Fallback ladder

- Voice flaky on the train → tap. Tunnel dead → LAN URL (if the room allows)
  → pre-recorded video of the same beats.
- A prompt completes without a card → don't retry live; next beat.
- Computer-use whiffs a click (screenshot coords) → it retries with a crop;
  let it. Only intervene if it loops.
- MCP missing from the session (`/mcp` empty) → Vibe caches tool lists 24h:
  run the server's `--check-cache` / `--clear` and restart the session.

## Enabling computer-use later (exact ladder)

The core baton demo works without any of this. To add the computer-use leg
on this Mac, in order:

1. **Fix Command Line Tools (needs your password, ~920MB download).** The
   current install cannot compile Swift at all: the compiler refuses every
   installed SDK's interfaces, and two conflicting `SwiftBridging`
   modulemaps ship together in the same include dir. Apple already has the
   fixed package staged:
   ```bash
   sudo softwareupdate --install "Command Line Tools for Xcode 26.6-26.6"
   ```
   Verify with `swiftc --version` before proceeding.
2. **Build the native helper** from the fork checkout (kept at
   `../mistral-hackathon`, shallow, ~76MB):
   ```bash
   cd ../mistral-hackathon && mkdir -p .native-build
   swiftc -parse-as-library -target "$(uname -m)-apple-macosx14.0" \
     scripts/computer-use/Logic.swift scripts/computer-use/WindowControl.swift \
     -o "$PWD/.native-build/window-control"
   ```
   Fast path: skip steps 1–2 entirely by copying the teammate's already
   built `window-control` binary (a standalone arm64 executable) and
   pointing `--helper` at it. Steps 3–5 still apply.
3. **Grant macOS privacy permissions** — System Settings → Privacy &
   Security: **Screen & System Audio Recording** + **Accessibility** to
   the terminal/app that launches Vibe, then relaunch it. This can only be
   done at the desk (by design; a feature beat for judges, not a bug).
4. **Register the MCP** at user level (no Vibe code changes, and do NOT
   `uv sync` the fork — the MCP server is self-contained):
   ```bash
   vibe mcp remove computer-use 2>/dev/null
   vibe mcp add computer-use --transport stdio \
     --command "$(command -v uv)" \
     --arg=run --arg=--no-project --arg=--script \
     --arg="$PWD/../mistral-hackathon/scripts/computer-use/server.py" \
     --arg=--helper --arg="$PWD/../mistral-hackathon/.native-build/window-control" \
     --arg=--window --arg=<window id> --arg=--pid --arg=<pid> \
     --tool-timeout-sec 25
   ```
   Window mode (pin to the Firefox window; get `<window id>`/`<pid>` from
   `.native-build/window-control windows`) is the recommended live posture
   — see *Safety*. Use `--arg=--display --arg=main` instead for
   full-display demos.
5. **Verify end to end** with `/mcp` in a scratch session, then open
   `scripts/computer-use/demo.html` in the browser and walk the tool list
   (click, type, drag, scroll, keys — the fork's
   `docs/computer-use-mcp.md` has the exact test prompt). If `/mcp` shows
   stale tools: Vibe caches MCP tool lists for 24h — run the server with
   `--check-cache` (and `--clear`) using the same registration args, then
   start a fresh session. Only after this works rehearse the tube story
   above — never first-run it in front of judges.

## Status notes

- Core baton demo (browser + cards + policy + stage view): fully proven and
  rehearsed — this needs nothing from the fork.
- Computer-use leg: MCP server verified standalone on this Mac; native
  helper NOT built here yet (step 1 above is the only machine blocker).
- Tube connectivity: station WiFi / 4G is enough for taps; don't script
  the voice beat as guaranteed.
