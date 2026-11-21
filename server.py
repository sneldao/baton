"""baton: drive Mistral Vibe sessions from a phone/browser.

Spawns `vibe-acp` (Agent Client Protocol, JSON-RPC over stdio) and bridges it
to browsers over WebSocket. Browsers get live session updates, can prompt,
resume sessions, and answer permission requests — including by voice via
Voxtral transcription.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shlex
import socket
from pathlib import Path

import httpx
from aiohttp import web

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("baton")

STATIC = Path(__file__).parent / "static"
PORT = int(os.environ.get("PORT", "8765"))
TOKEN = os.environ.get("BATON_TOKEN") or secrets.token_urlsafe(12)


# How to launch vibe-acp. Defaults: binary on PATH, else `uv run` in a sibling
# clone of mistralai/mistral-vibe.
def _default_acp_cmd() -> str:
    import shutil

    if shutil.which("vibe-acp"):
        return "vibe-acp"
    sibling = Path(__file__).parent.parent / "mistral-vibe"
    return f"uv run --directory {sibling} vibe-acp"


VIBE_ACP_CMD = os.environ.get("VIBE_ACP_CMD") or _default_acp_cmd()

AGENT_REQUEST_TIMEOUT = 600  # seconds to wait for a browser to answer

CLIENT_CAPABILITIES = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class AcpBridge:
    """Owns the vibe-acp subprocess and fans its messages out to WS clients."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.pending_agent_requests: dict[int, asyncio.Task] = {}
        # Outstanding agent->client requests, replayed to clients that connect
        # or reconnect so a phone refresh never loses a pending approval.
        self.outstanding_requests: dict[int, dict] = {}
        self.clients: set[web.WebSocketResponse] = set()
        self.initialized = False
        self.auth_methods: list = []
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None

    async def start(self) -> None:
        cmd = shlex.split(VIBE_ACP_CMD)
        log.info("spawning: %s", " ".join(cmd))
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            result = await self.call(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": CLIENT_CAPABILITIES,
                    "clientInfo": {"name": "baton", "version": "0.1.0"},
                },
            )
            self.initialized = True
            self.auth_methods = result.get("authMethods", [])
            log.info(
                "ACP initialized: agent=%s authMethods=%s",
                result.get("agentInfo"),
                self.auth_methods,
            )
            await self.broadcast(
                {"method": "bridge/status", "params": {"state": "ready"}}
            )
        except Exception as exc:
            log.error("ACP initialize failed: %s", exc)
            await self.broadcast(
                {
                    "method": "bridge/status",
                    "params": {"state": "error", "message": str(exc)},
                }
            )

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        async for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("non-JSON from agent: %r", line[:200])
                continue
            await self._dispatch(msg)

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for line in self.proc.stderr:
            log.debug("vibe-acp[stderr] %s", line.decode(errors="replace").rstrip())

    async def _dispatch(self, msg: dict) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self.pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(json.dumps(msg["error"])))
                else:
                    fut.set_result(msg["result"])
            return
        if "id" in msg and "method" in msg:
            # Agent -> client request (e.g. session/request_permission).
            await self._handle_agent_request(msg)
            return
        # Notification -> broadcast verbatim.
        await self.broadcast(msg)

    async def _handle_agent_request(self, msg: dict) -> None:
        req_id = msg["id"]
        method = msg["method"]
        log.info("agent request %s (id=%s)", method, req_id)

        async def timeout_answer() -> None:
            await asyncio.sleep(AGENT_REQUEST_TIMEOUT)
            if req_id in self.pending_agent_requests:
                self.pending_agent_requests.pop(req_id)
                self.outstanding_requests.pop(req_id, None)
                await self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": "client did not answer"},
                    }
                )
                await self.broadcast({"type": "agent_resolved", "requestId": req_id})

        self.pending_agent_requests[req_id] = asyncio.create_task(timeout_answer())
        forwarded = {
            "type": "agent_request",
            "requestId": req_id,
            "method": method,
            "params": msg.get("params", {}),
        }
        self.outstanding_requests[req_id] = forwarded
        # trust dial: low-risk tool calls may auto-approve; one-way doors never do
        decision, meta = await self._policy_decide(method, msg.get("params", {}))
        if decision is not None:
            await self._resolve(req_id)
            await self._send({"jsonrpc": "2.0", "id": req_id, "result": decision})
            await self.broadcast(
                {
                    "type": "auto_decided",
                    "requestId": req_id,
                    "method": method,
                    "params": msg.get("params", {}),
                    **meta,
                }
            )
            return
        await self.broadcast(forwarded)

    async def _policy_decide(
        self, method: str, params: dict
    ) -> tuple[dict | None, dict]:
        """Three-tier trust: deny rules refuse outright, allow/low-risk auto-pass,
        one-way doors always escalate to a human."""
        if method != "session/request_permission":
            return None, {}
        tc = params.get("toolCall") or {}
        text = tool_call_text(tc).lower()

        for rule in POLICY.get("deny") or []:
            if rule.lower() in text:
                return {"outcome": {"outcome": "cancelled"}}, {
                    "action": "denied",
                    "rule": rule,
                }

        if ONE_WAY_DOORS.search(text):
            return None, {}

        opts = params.get("options") or []

        def allow_once():
            o = next((x for x in opts if x.get("kind") == "allow_once"), None) or next(
                (
                    x
                    for x in opts
                    if str(x.get("kind", "")).startswith("allow")
                    and x.get("kind") != "allow_always"
                ),
                None,
            )
            if not o:
                return None
            return {
                "outcome": {
                    "outcome": "selected",
                    "optionId": o.get("optionId") or o.get("id"),
                }
            }

        for rule in POLICY.get("allow") or []:
            if rule.lower() in text:
                r = allow_once()
                if r:
                    return r, {"action": "allowed", "rule": rule}

        if not POLICY["auto_low"]:
            return None, {}
        risk = await risk_annotate(tc)
        if risk.get("risk") != "low":
            return None, {}
        r = allow_once()
        if not r:
            return None, {}
        return r, {
            "action": "auto-approved",
            "summary": risk.get("summary", ""),
            "risk": "low",
        }

    async def _resolve(self, req_id: int) -> asyncio.Task | None:
        self.outstanding_requests.pop(req_id, None)
        task = self.pending_agent_requests.pop(req_id, None)
        if task is None:
            return None
        task.cancel()
        await self.broadcast({"type": "agent_resolved", "requestId": req_id})
        return task

    async def answer_agent_request(self, req_id: int, result: dict) -> bool:
        if await self._resolve(req_id) is None:
            return False
        await self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
        return True

    async def error_agent_request(self, req_id: int, error: dict | str) -> bool:
        if await self._resolve(req_id) is None:
            return False
        err = error if isinstance(error, dict) else {"code": -32601, "message": error}
        await self._send({"jsonrpc": "2.0", "id": req_id, "error": err})
        return True

    async def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        rid = self.next_id
        fut = asyncio.get_event_loop().create_future()
        self.pending[rid] = fut
        await self._send(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        )
        return await asyncio.wait_for(fut, timeout=120)

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _send(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        await self.proc.stdin.drain()

    async def broadcast(self, msg: dict) -> None:
        dead = []
        for ws in self.clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def broadcast_client_count(self) -> None:
        await self.broadcast({"type": "bridge/clients", "count": len(self.clients)})

    async def shutdown(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()


bridge = AcpBridge()


def check_token(request: web.Request) -> bool:
    return (
        request.query.get("token") == TOKEN
        or request.headers.get("X-Vibe-Token") == TOKEN
    )


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC / "index.html")


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    if not check_token(request):
        return web.Response(status=401, text="bad token")
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    bridge.clients.add(ws)
    await bridge.broadcast_client_count()
    await ws.send_json(
        {
            "method": "bridge/status",
            "params": {
                "state": "ready" if bridge.initialized else "starting",
                "authMethods": bridge.auth_methods,
                "cwd": os.getcwd(),
            },
        }
    )
    # Replay requests still awaiting an answer (covers refresh/reconnect).
    for req in bridge.outstanding_requests.values():
        await ws.send_json(req)
    try:
        async for raw in ws:
            if raw.type != web.WSMsgType.TEXT:
                continue
            try:
                msg = json.loads(raw.data)
            except json.JSONDecodeError:
                continue
            await handle_client_msg(ws, msg)
    finally:
        bridge.clients.discard(ws)
        await bridge.broadcast_client_count()
    return ws


async def handle_client_msg(ws: web.WebSocketResponse, msg: dict) -> None:
    mtype = msg.get("type", "rpc")
    try:
        if mtype == "rpc":  # client -> agent JSON-RPC
            result = await bridge.call(msg["method"], msg.get("params"))
            await ws.send_json({"id": msg.get("id"), "result": result})
        elif mtype == "notify":  # client -> agent notification
            await bridge.notify(msg["method"], msg.get("params"))
        elif mtype == "agent_response":  # answer to agent -> client request
            if "error" in msg:
                ok = await bridge.error_agent_request(msg["requestId"], msg["error"])
            else:
                ok = await bridge.answer_agent_request(
                    msg["requestId"], msg.get("result", {})
                )
            if not ok:
                await ws.send_json(
                    {"type": "error", "message": "request already resolved"}
                )
    except Exception as exc:
        await ws.send_json({"id": msg.get("id"), "error": {"message": str(exc)}})


async def voice(request: web.Request) -> web.Response:
    """Phone mic audio -> Voxtral transcription (Mistral audio/transcriptions)."""
    if not check_token(request):
        return web.Response(status=401, text="bad token")
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        return web.json_response(
            {"error": "MISTRAL_API_KEY not set on bridge"}, status=503
        )
    audio = await request.read()
    if not audio:
        return web.json_response({"error": "empty audio"}, status=400)
    ctype = request.headers.get("Content-Type", "audio/webm")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.mistral.ai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={
                "file": (f"audio.{ctype.split('/')[-1].split(';')[0]}", audio, ctype)
            },
            data={"model": "voxtral-mini-latest"},
        )
    if resp.status_code != 200:
        return web.json_response(
            {"error": f"mistral {resp.status_code}: {resp.text[:300]}"}, status=502
        )
    return web.json_response(resp.json())


# Claude Code-style allow/ask/deny policy. Precedence:
#   deny rules → one-way doors (always ask) → allow rules → low-risk (auto_low) → ask
POLICY = {
    "auto_low": False,
    "deny": [".env", "id_rsa", ".aws/credentials", "secret", "api_key"],
    "allow": [],
}

# one-way doors: destructive/irreversible actions always reach a human,
# regardless of how confident the risk classifier is
ONE_WAY_DOORS = re.compile(
    r"\b(rm\s+(-\w+\s+)*-?rf?\b|git\s+push\b[^|;&]*(--force|-f\b)|"
    r"git\s+reset\s+--hard|drop\s+(table|database)|mkfs\b|dd\s+if=|"
    r"chmod\s+-R\s+777|npm\s+publish|pip\s+uninstall|brew\s+uninstall|"
    r"shutdown\b|reboot\b|killall\b|pkill\b)",
    re.IGNORECASE,
)


def tool_call_text(tc: dict) -> str:
    return (tc.get("title") or "") + " " + json.dumps(tc.get("rawInput") or tc)


RISK_PROMPT = (
    "You annotate permission requests for a coding agent's remote approval "
    "console. Given a tool call, reply with EXACTLY one JSON object: "
    '{"summary": "one short sentence a non-expert understands", '
    '"risk": "low|medium|high"}. No markdown, no extra text.'
)


async def risk_annotate(tc: dict) -> dict:
    """One-line risk annotation for a tool call, via Mistral."""
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        return {"summary": "", "risk": ""}
    user = f"Tool call title: {tc.get('title', '?')}\nInput: {json.dumps(tc.get('rawInput'))[:800]}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": os.environ.get("BATON_RISK_MODEL", "mistral-small-latest"),
                    "messages": [
                        {"role": "system", "content": RISK_PROMPT},
                        {"role": "user", "content": user},
                    ],
                    "response_format": {"type": "json_object"},
                    "max_tokens": 120,
                },
            )
        if resp.status_code != 200:
            return {"summary": "", "risk": ""}
        content = resp.json()["choices"][0]["message"]["content"]
        out = json.loads(content)
        return {"summary": out.get("summary", ""), "risk": out.get("risk", "")}
    except Exception:
        return {"summary": "", "risk": ""}


async def summarize(request: web.Request) -> web.Response:
    """One-line risk annotation for a permission request, via Mistral."""
    if not check_token(request):
        return web.Response(status=401, text="bad token")
    if not os.environ.get("MISTRAL_API_KEY"):
        return web.json_response(
            {"error": "MISTRAL_API_KEY not set on bridge"}, status=503
        )
    body = await request.json()
    return web.json_response(await risk_annotate(body.get("toolCall", {})))


async def policy(request: web.Request) -> web.Response:
    """Trust dial: get/set the auto-approval policy."""
    if not check_token(request):
        return web.Response(status=401, text="bad token")
    if request.method == "POST":
        body = await request.json()
        if "auto_low" in body:
            POLICY["auto_low"] = bool(body["auto_low"])
        if "deny" in body:
            POLICY["deny"] = list(body["deny"])
        if "allow" in body:
            POLICY["allow"] = list(body["allow"])
        await bridge.broadcast({"type": "policy", **POLICY})
    return web.json_response(POLICY)


async def qr_png(request: web.Request) -> web.Response:
    import io

    import qrcode

    url = f"http://{lan_ip()}:{PORT}/?token={TOKEN}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return web.Response(body=buf.getvalue(), content_type="image/png")


async def on_startup(app: web.Application) -> None:
    asyncio.create_task(bridge.start())


async def on_shutdown(app: web.Application) -> None:
    await bridge.shutdown()


def main() -> None:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_post("/api/voice", voice)
    app.router.add_post("/api/summarize", summarize)
    app.router.add_get("/api/policy", policy)
    app.router.add_post("/api/policy", policy)
    app.router.add_get("/qr.png", qr_png)
    app.router.add_static("/static", STATIC)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    base = f"http://{lan_ip()}:{PORT}"
    print(
        f"\n  baton listening"
        f"\n  pair your phone: {base}/?token={TOKEN}"
        f"\n  QR code: {base}/qr.png  (open on the host screen)\n"
    )
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
