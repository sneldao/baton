"""Laptop-side driver: starts a Vibe task over the baton WS protocol and
leaves permission requests for remote devices (phone / Telegram) to answer.

    uv run python tools/drive.py "create a file called hello.txt with 'hi'"
    uv run python tools/drive.py --auto-answer 8 "run ls"   # self-test
    BATON_WS=ws://localhost:8766/ws BATON_TOKEN=... uv run python tools/drive.py "..."

Prints every protocol event so you can watch the full loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import websockets

WS = os.environ.get("BATON_WS", "ws://localhost:8766/ws")
TOKEN = os.environ.get("BATON_TOKEN", "")
args, skip = [], False
for i, a in enumerate(sys.argv[1:], 1):
    if skip:
        skip = False
    elif a in ("--auto-answer", "--mode"):
        skip = True
    elif not a.startswith("--"):
        args.append(a)
PROMPT = " ".join(args) or "run ls"
AUTO_AFTER = None
MODE = "ask"
for i, a in enumerate(sys.argv):
    if a == "--auto-answer" and i + 1 < len(sys.argv):
        AUTO_AFTER = float(sys.argv[i + 1])
    elif a == "--mode" and i + 1 < len(sys.argv):
        MODE = sys.argv[i + 1]


async def main() -> None:
    async with websockets.connect(f"{WS}?token={TOKEN}", max_size=16 << 20) as ws:
        next_id = 0
        rpc_pending = {}
        session_id = None
        cwd = None

        async def rpc(method, params):
            nonlocal next_id
            next_id += 1
            fut = asyncio.get_event_loop().create_future()
            rpc_pending[next_id] = fut
            await ws.send(
                json.dumps(
                    {"type": "rpc", "id": next_id, "method": method, "params": params}
                )
            )
            return await fut

        async def answer_later(req_id, options, delay):
            await asyncio.sleep(delay)
            allow = next(
                (o for o in options if str(o.get("kind", "")).startswith("allow")),
                options[0] if options else None,
            )
            if not allow:
                return
            await ws.send(
                json.dumps(
                    {
                        "type": "agent_response",
                        "requestId": req_id,
                        "result": {
                            "outcome": {
                                "outcome": "selected",
                                "optionId": allow.get("optionId") or allow.get("id"),
                            }
                        },
                    }
                )
            )
            print(f"[driver] auto-answered request {req_id} → {allow.get('name')}")

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("id") in rpc_pending:
                rpc_pending.pop(msg["id"]).set_result(msg)
                continue
            mtype = msg.get("type")
            meth = msg.get("method")

            if meth == "bridge/status":
                cwd = msg["params"].get("cwd")
                print(f"[bridge] state={msg['params'].get('state')} cwd={cwd}")
                if session_id is None:
                    r = await rpc("session/new", {"cwd": cwd, "mcpServers": []})
                    session_id = (r.get("result") or {}).get("sessionId")
                    print(
                        f"[driver] session {str(session_id)[:8]} (mode={MODE}) — prompting: {PROMPT!r}"
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "notify",
                                "method": "session/set_mode",
                                "params": {"sessionId": session_id, "modeId": MODE},
                            }
                        )
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "type": "notify",
                                "method": "session/prompt",
                                "params": {
                                    "sessionId": session_id,
                                    "prompt": [{"type": "text", "text": PROMPT}],
                                },
                            }
                        )
                    )
            elif mtype == "agent_request":
                tc = msg.get("params", {}).get("toolCall", {})
                opts = msg.get("params", {}).get("options", [])
                print(
                    f"[PERMISSION] #{msg['requestId']} {tc.get('title')} — options: "
                    + ", ".join(o.get("name", "?") for o in opts)
                )
                if AUTO_AFTER:
                    asyncio.create_task(
                        answer_later(msg["requestId"], opts, AUTO_AFTER)
                    )
            elif mtype == "agent_resolved":
                print(
                    f"[resolved] #{msg.get('requestId')} answered on {msg.get('via', 'another device')}"
                )
            elif mtype == "auto_decided":
                print(
                    f"[policy] {msg.get('action')}: "
                    f"{(msg.get('params') or {}).get('toolCall', {}).get('title')}"
                )
            elif meth == "session/update":
                u = msg.get("params", {}).get("update", {})
                k = u.get("sessionUpdate")
                if k == "agent_message_chunk":
                    print((u.get("content") or {}).get("text", ""), end="", flush=True)
                elif k == "tool_call":
                    print(f"\n[tool] {u.get('title') or u.get('kind')}")
                elif k == "tool_call_update" and u.get("status") in (
                    "completed",
                    "failed",
                ):
                    print(f"[tool:{u.get('status')}] {u.get('title', '')}")


asyncio.run(main())
