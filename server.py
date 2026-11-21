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

    # Prefer the sibling repo clone: PATH-installed vibe-acp boots its
    # app server for minutes on some machines (verified slower live).
    sibling = Path(__file__).parent.parent / "mistral-vibe"
    if sibling.is_dir():
        return f"uv run --directory {sibling} vibe-acp"
    if shutil.which("vibe-acp"):
        return "vibe-acp"
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
        # public request id -> (ACP id, start monotonic, expiry timer)
        self.pending_agent_requests: dict[
            str, tuple[int | str, float, asyncio.Task]
        ] = {}
        # Outstanding agent->client requests, replayed to clients that connect
        # or reconnect so a phone refresh never loses a pending approval.
        # Keyed by the public opaque id — ACP ids never reach the wire.
        self.outstanding_requests: dict[str, dict] = {}
        self.clients: set[web.WebSocketResponse] = set()
        self.initialized = False
        self.auth_methods: list = []
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self.broadcast_lock = asyncio.Lock()

    async def start(self) -> None:
        cmd = shlex.split(VIBE_ACP_CMD)
        log.info("spawning: %s", " ".join(cmd))
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
            # vibe-acp emits single JSON lines >64KB; default limit kills reader
            limit=16 * 1024 * 1024,
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
        try:
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
        finally:
            # synchronous only: fail local RPC futures; the agent may still be
            # running turns, so don't announce anything to clients here
            self.initialized = False
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("ACP connection closed"))

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
        acp_id = msg["id"]
        method = msg["method"]
        log.info("agent request %s (id=%s)", method, acp_id)

        if method != "session/request_permission":
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": acp_id,
                    "error": {
                        "code": -32601,
                        "message": "unsupported ACP client request",
                    },
                }
            )
            return

        # trust dial: policy decides before anything reaches a human card;
        # low-risk tool calls may auto-approve, one-way doors never do
        decision, meta = await self._policy_decide(method, msg.get("params", {}))
        if decision is not None:
            await self._send({"jsonrpc": "2.0", "id": acp_id, "result": decision})
            await self.broadcast(
                {
                    "type": "auto_decided",
                    "requestId": secrets.token_hex(12),
                    "method": method,
                    "params": msg.get("params", {}),
                    **meta,
                }
            )
            return

        request_id = secrets.token_hex(12)
        started_at = asyncio.get_running_loop().time()
        timer = asyncio.create_task(self._expire_permission(request_id))
        self.pending_agent_requests[request_id] = (acp_id, started_at, timer)
        forwarded = {
            "type": "agent_request",
            "requestId": request_id,
            "method": method,
            "params": msg.get("params", {}),
        }
        self.outstanding_requests[request_id] = forwarded
        await self.broadcast(forwarded)

    async def _expire_permission(self, request_id: str) -> None:
        await asyncio.sleep(AGENT_REQUEST_TIMEOUT)
        await self._finish_permission(
            request_id,
            {"outcome": {"outcome": "cancelled"}},
            "expired",
            "server",
        )

    async def _finish_permission(
        self, request_id: str, result: dict, decision: str, source: str
    ) -> bool:
        entry = self.pending_agent_requests.pop(request_id, None)
        if entry is None:
            return False
        self.outstanding_requests.pop(request_id, None)
        acp_id, started_at, timer = entry
        if timer is not asyncio.current_task():
            timer.cancel()
        option_id = result["outcome"].get("optionId")
        try:
            await self._send({"jsonrpc": "2.0", "id": acp_id, "result": result})
        except Exception:
            log.exception("failed to deliver answer for request %s", request_id)
            decision = "unavailable"
        await self.broadcast(
            {
                "type": "agent_resolved",
                "requestId": request_id,
                "decision": decision,
                "source": source,
                "optionId": option_id,
                "elapsedMs": round(
                    (asyncio.get_running_loop().time() - started_at) * 1000
                ),
            }
        )
        return True

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

    async def answer_agent_request(
        self, request_id: str, result: dict, source: str
    ) -> bool:
        """Validate a client answer against the offered options, then resolve.

        Unknown or malformed answers leave the request pending.
        """
        if not isinstance(request_id, str):
            return False
        forwarded = self.outstanding_requests.get(request_id)
        if forwarded is None:
            return False
        outcome = result.get("outcome") if isinstance(result, dict) else None
        if not isinstance(outcome, dict):
            return False
        kind = outcome.get("outcome")
        if kind == "cancelled":
            return await self._finish_permission(
                request_id,
                {"outcome": {"outcome": "cancelled"}},
                "denied",
                source,
            )
        if kind != "selected":
            return False
        option_id = outcome.get("optionId")
        if not isinstance(option_id, str) or not option_id:
            return False
        options = (forwarded.get("params") or {}).get("options") or []
        opt = next(
            (o for o in options if (o.get("optionId") or o.get("id")) == option_id),
            None,
        )
        if opt is None:
            return False
        opt_kind = str(opt.get("kind") or "")
        if not (opt_kind.startswith("allow") or opt_kind.startswith("reject")):
            return False
        decision = "approved" if opt_kind.startswith("allow") else "denied"
        return await self._finish_permission(
            request_id,
            {"outcome": {"outcome": "selected", "optionId": option_id}},
            decision,
            source,
        )

    async def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        rid = self.next_id
        fut = asyncio.get_event_loop().create_future()
        self.pending[rid] = fut
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            )
            return await asyncio.wait_for(
                fut, timeout=None if method == "session/prompt" else 120
            )
        finally:
            self.pending.pop(rid, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _send(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        await self.proc.stdin.drain()

    async def broadcast(self, msg: dict) -> None:
        async with self.broadcast_lock:
            dead = []
            for ws in tuple(self.clients):
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


async def _safe_send(ws: web.WebSocketResponse, msg: dict) -> None:
    try:
        await ws.send_json(msg)
    except (ConnectionError, RuntimeError):
        pass


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    if not check_token(request):
        return web.Response(status=401, text="bad token")
    source = request.query.get("client", "remote")
    if source not in {"telegram", "browser"}:
        source = "remote"
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    bridge.clients.add(ws)
    # Snapshot synchronously: the snapshot frame plus any replayed request
    # still present in outstanding_requests is a consistent view.
    requests = list(bridge.outstanding_requests.values())
    prompts: set[asyncio.Task] = set()
    try:
        await ws.send_json(
            {
                "type": "permission_snapshot",
                "requestIds": [r["requestId"] for r in requests],
            }
        )
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
        for req in requests:
            if req["requestId"] in bridge.outstanding_requests:
                await ws.send_json(req)
        async for raw in ws:
            if raw.type != web.WSMsgType.TEXT:
                continue
            try:
                msg = json.loads(raw.data)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            # prompts run concurrently so control messages (answers,
            # cancels) are processed while a turn is in flight
            if (
                msg.get("type", "rpc") == "rpc"
                and msg.get("method") == "session/prompt"
            ):
                task = asyncio.create_task(handle_client_msg(ws, msg, source))
                prompts.add(task)
                task.add_done_callback(prompts.discard)
            else:
                await handle_client_msg(ws, msg, source)
    finally:
        for task in prompts:
            task.cancel()
        if prompts:
            await asyncio.gather(*prompts, return_exceptions=True)
        bridge.clients.discard(ws)
        await bridge.broadcast_client_count()
    return ws


async def handle_client_msg(
    ws: web.WebSocketResponse, msg: dict, source: str = "remote"
) -> None:
    mtype = msg.get("type", "rpc")
    try:
        if mtype == "rpc":  # client -> agent JSON-RPC
            result = await bridge.call(msg["method"], msg.get("params"))
            await _safe_send(ws, {"id": msg.get("id"), "result": result})
        elif mtype == "notify":  # client -> agent notification
            await bridge.notify(msg["method"], msg.get("params"))
            if msg.get("method") == "session/cancel":
                sid = (msg.get("params") or {}).get("sessionId")
                if isinstance(sid, str) and sid:
                    for rid, req in list(bridge.outstanding_requests.items()):
                        if (req.get("params") or {}).get("sessionId") == sid:
                            await bridge._finish_permission(
                                rid,
                                {"outcome": {"outcome": "cancelled"}},
                                "cancelled",
                                source,
                            )
        elif mtype == "agent_response":  # answer to agent -> client request
            ok = False
            if "error" not in msg:
                ok = await bridge.answer_agent_request(
                    msg.get("requestId"), msg.get("result"), source
                )
            if not ok:
                await _safe_send(
                    ws,
                    {
                        "type": "error",
                        "requestId": msg.get("requestId"),
                        "message": "Permission answer rejected: stale or invalid",
                    },
                )
    except Exception as exc:
        if not ws.closed:
            await _safe_send(ws, {"id": msg.get("id"), "error": {"message": str(exc)}})


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


RISK_AGENT_ID: str | None = None
# mistral-small: reasoning models burn tokens thinking on destructive inputs
# (~6s vs ~0.5s) — latency matters on the approval hot path.
RISK_MODEL = os.environ.get("BATON_RISK_MODEL", "mistral-small-latest")


async def _risk_agent_id(client: httpx.AsyncClient, api_key: str) -> str | None:
    """Create (once) the Mistral Agent that adjudicates tool-call risk."""
    global RISK_AGENT_ID
    if RISK_AGENT_ID or os.environ.get("BATON_NO_AGENT"):
        return RISK_AGENT_ID
    try:
        resp = await client.post(
            "https://api.mistral.ai/v1/agents",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": RISK_MODEL,
                "name": "baton-risk-assessor",
                "description": "Judges remote permission requests for a coding agent",
                "instructions": RISK_PROMPT,
            },
        )
        if resp.status_code in (200, 201):
            RISK_AGENT_ID = resp.json().get("id")
    except Exception:
        pass
    return RISK_AGENT_ID


def _conversation_text(payload: dict) -> str:
    """Pull the assistant text out of a Conversations API response."""
    for item in payload.get("outputs", []):
        if item.get("type") in ("message.output", "message"):
            content = item.get("content", "")
            if isinstance(content, str):
                return content
            for part in content:
                if part.get("type") in ("text", "output_text"):
                    return part.get("text", "")
    raise ValueError("no assistant text in conversation output")


def _chat_text(message: dict) -> str:
    """Extract answer text; reasoning models return content as typed parts."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    for part in content or []:
        if part.get("type") == "text":
            return part.get("text", "")
    return ""


async def risk_annotate(tc: dict) -> dict:
    """Risk adjudication by a Mistral Agent (Le Chonk); falls back to chat."""
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        return {"summary": "", "risk": ""}
    user = f"Tool call title: {tc.get('title', '?')}\nInput: {json.dumps(tc.get('rawInput'))[:800]}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = {"Authorization": f"Bearer {api_key}"}
            agent_id = await _risk_agent_id(client, api_key)
            if agent_id:
                resp = await client.post(
                    "https://api.mistral.ai/v1/conversations",
                    headers=headers,
                    json={
                        "agent_id": agent_id,
                        "inputs": [{"role": "user", "content": user}],
                    },
                )
                if resp.status_code in (200, 201):
                    try:
                        out = json.loads(_conversation_text(resp.json()))
                        return {
                            "summary": out.get("summary", ""),
                            "risk": out.get("risk", ""),
                            "via": "agent",
                        }
                    except (ValueError, json.JSONDecodeError):
                        pass
            resp = await client.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers=headers,
                json={
                    "model": RISK_MODEL,
                    "messages": [
                        {"role": "system", "content": RISK_PROMPT},
                        {"role": "user", "content": user},
                    ],
                    "response_format": {"type": "json_object"},
                    "max_tokens": 512,
                },
            )
        if resp.status_code != 200:
            return {"summary": "", "risk": ""}
        content = _chat_text(resp.json()["choices"][0]["message"])
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
    if not check_token(request):
        return web.Response(status=401, text="bad token")
    import io

    import qrcode

    url = f"http://{lan_ip()}:{PORT}/?token={TOKEN}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return web.Response(
        body=buf.getvalue(),
        content_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


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
        f"\n  QR code: use Pair on the authenticated page (trusted LAN only)\n"
    )
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    main()
