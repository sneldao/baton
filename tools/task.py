"""Send a prompt to an existing baton/Vibe session and watch events.

    BATON_TOKEN=... uv run python -u tools/task.py <sessionId> "run ls"

Leaves permission requests for remote devices (phone / Telegram) to answer.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import websockets

WS = os.environ.get("BATON_WS", "ws://localhost:8766/ws")
TOKEN = os.environ.get("BATON_TOKEN", "")
SESSION_ID = sys.argv[1]
PROMPT = " ".join(sys.argv[2:]) or "run the shell command: ls"
MODE = os.environ.get("MODE", "ask")


async def main() -> None:
    async with websockets.connect(f"{WS}?token={TOKEN}", max_size=16 << 20) as ws:
        sent = False
        async for raw in ws:
            msg = json.loads(raw)
            meth, mtype = msg.get("method"), msg.get("type")
            if meth == "bridge/status" and not sent:
                sent = True
                await ws.send(
                    json.dumps(
                        {
                            "type": "notify",
                            "method": "session/set_mode",
                            "params": {"sessionId": SESSION_ID, "modeId": MODE},
                        }
                    )
                )
                await ws.send(
                    json.dumps(
                        {
                            "type": "notify",
                            "method": "session/prompt",
                            "params": {
                                "sessionId": SESSION_ID,
                                "prompt": [{"type": "text", "text": PROMPT}],
                            },
                        }
                    )
                )
                print(
                    f"[driver] mode={MODE} prompt={PROMPT!r} → session {SESSION_ID[:8]}",
                    flush=True,
                )
            elif mtype == "agent_request":
                tc = msg.get("params", {}).get("toolCall", {})
                opts = msg.get("params", {}).get("options", [])
                print(
                    f"[PERMISSION] #{msg['requestId']} {tc.get('title')} — options: "
                    + ", ".join(o.get("name", "?") for o in opts),
                    flush=True,
                )
            elif mtype == "agent_resolved":
                print(f"[resolved] #{msg.get('requestId')}", flush=True)
            elif mtype == "auto_decided":
                print(
                    f"[policy] {msg.get('action')}: "
                    f"{(msg.get('params') or {}).get('toolCall', {}).get('title')}",
                    flush=True,
                )
            elif meth == "session/update":
                u = msg.get("params", {}).get("update", {})
                k = u.get("sessionUpdate")
                if k == "agent_message_chunk":
                    print((u.get("content") or {}).get("text", ""), end="", flush=True)
                elif k == "tool_call":
                    print(f"\n[tool] {u.get('title') or u.get('kind')}", flush=True)
                elif k == "tool_call_update" and u.get("status") in (
                    "completed",
                    "failed",
                ):
                    print(f"[tool:{u.get('status')}] {u.get('title', '')}", flush=True)


asyncio.run(main())
