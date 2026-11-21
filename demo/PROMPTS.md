# Canned demo prompts

Send these against **this directory** (`baton/demo/`).

**Set the mode to `ask` first** — new sessions default to `accept-edits`,
which silently auto-approves file edits and kills the demo's permission
beats. Bash commands still prompt in any mode; file edits only prompt
in `ask`.

Every prompt is short and output-bounded on purpose — long agent
monologues kill pacing. Rehearse each once; model behavior varies.

## Beat 1 — approve (the mundane task)

> Add a `--version` flag to main.py that prints the app name, then run
> the tests.

Produces: file read → edit permission (approve once) → bash permission
for `python -m unittest` (approve). Green tests, ~60s.

## Beat 2 — the walk-away approve

> Write a short CONTRIBUTING.md for this project — three bullet points max.

Produces: one file-write permission. Send it, walk away, approve from the
corridor over the tunnel URL.

## Beat 3 — the reject (safety beat)

> Clean up — delete the build/ directory and everything in it.

Produces: a destructive bash permission → red risk line → swipe left /
tap cancel. The agent adapts instead of crashing. "Reject isn't a crash,
it's a steering wheel."

## Beat 4 — filler / fallback (no permission)

> What does this project do? One sentence.

Pure streaming beat. Use if a prompt whiffs, or as a breather.

## The before/after (judge-requested format)

Mistral staff ask for "task X takes Y before, Z after." Ours:

- **Before**: in `ask` mode an unattended task waits as long as you're gone
  (minutes–hours); alternatives are babysitting or blanket auto-approve.
- **After**: approval latency ≈ notification latency (~10s, from anywhere);
  one-way doors still reach a human while low-risk calls flow through.

Time the corridor approval live — it's the Z in the sentence.

**Mistral Web contrast line**: "Mistral Web teleports the session to the
browser; baton is the reverse path — it teleports *you* back into the local
session, from any pocket device. They move the work; we move the authority."

## Reliability rules

- One session, three prompts — don't create sessions mid-demo.
- If a prompt completes without asking, don't retry live — next prompt.
- Fallback ladder: voice → tap. Tunnel → LAN URL. Live → recorded video.
