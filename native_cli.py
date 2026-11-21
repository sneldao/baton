"""Opt-in demo: route Vibe CLI tool approvals to baton permission cards.

Runs inside the Vibe app-server slot: `native.sh` launches the real Rust
TUI with this process as its app server, and this shim spawns the real
`vibe-app-server` underneath, relaying frames both ways. When a native
`callback/call` approval request arrives AND a baton client (the Telegram
bridge) is connected, the callback is consumed locally — the TUI never
shows its own modal — and mirrored as a normal baton permission card.
The operator's tap is delivered back to Vibe as a `callback/result`.

Scope: approvals only. No remote prompts, no trust/connector/user-input
callbacks, no permanent grants, no tunnel. Shell approvals are auto-answered
only when they parse as read-only; everything else becomes a baton card.
With no observer connected, every frame passes through untouched and the
TUI's own approval dialog works as usual.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import sys

from aiohttp import web

import server

log = logging.getLogger("baton.native")

SAFE_SHELL_COMMANDS = {
    "[",
    "ag",
    "cat",
    "cd",
    "cmp",
    "comm",
    "cut",
    "date",
    "df",
    "diff",
    "du",
    "echo",
    "egrep",
    "false",
    "fgrep",
    "file",
    "find",
    "grep",
    "head",
    "hostname",
    "id",
    "join",
    "jq",
    "ls",
    "lsof",
    "more",
    "paste",
    "printf",
    "ps",
    "pwd",
    "rg",
    "sort",
    "stat",
    "tail",
    "test",
    "tr",
    "true",
    "type",
    "uname",
    "uniq",
    "wc",
    "whereis",
    "which",
    "whoami",
}
GIT_READ_ONLY = {
    "blame",
    "cat-file",
    "describe",
    "diff",
    "log",
    "ls-files",
    "ls-tree",
    "name-rev",
    "rev-parse",
    "shortlog",
    "show",
    "status",
    "whatchanged",
}
FIND_UNSAFE_PREFIXES = (
    "-delete",
    "-exec",
    "-execdir",
    "-fls",
    "-fprint",
    "-ok",
    "-okdir",
)
_SAFE_REDIRECT = re.compile(
    r"(?:2\s*>\s*/dev/null|1\s*>\s*/dev/null|2\s*>&\s*1|&\s*>\s*/dev/null|>\s*/dev/null)"
)
_SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\|")
_FORBIDDEN_SEGMENT = ("`", "$(", "<", ">", "&", "\n")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def shell_command_safe(command: str) -> bool:
    command = _SAFE_REDIRECT.sub("", command)
    for raw_segment in _SEGMENT_SPLIT.split(command):
        segment = raw_segment.strip()
        if not segment:
            continue
        if any(mark in segment for mark in _FORBIDDEN_SEGMENT):
            return False
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            return False
        while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
            tokens.pop(0)
        if not tokens:
            return False
        name = tokens[0].rsplit("/", 1)[-1]
        args = tokens[1:]
        if name == "git":
            if len(args) < 1 or args[0].startswith("-") or args[0] not in GIT_READ_ONLY:
                return False
            if any(
                arg == "-o"
                or arg.startswith(("--output", "--exec="))
                or arg in {"--ext-diff", "--textconv", "--exec"}
                for arg in args[1:]
            ):
                return False
            continue
        if name == "find":
            if any(arg.startswith(FIND_UNSAFE_PREFIXES) for arg in args):
                return False
            continue
        if name not in SAFE_SHELL_COMMANDS:
            return False
    return True


def _trace_native(path: str, payload: dict) -> None:
    with open(path, "a") as stream:
        print(payload, file=stream)


class NativeBridge(server.AcpBridge):
    """AcpBridge whose "agent" is the real Vibe app-server over stdio."""

    def __init__(self, app_stdin: asyncio.StreamWriter) -> None:
        super().__init__()
        self.app_stdin = app_stdin
        self.initialized = True
        # opaque relay id -> (Vibe sessionId, callbackId)
        self.callbacks: dict[str, tuple[str, str]] = {}
        # outbound "baton:"-prefixed callback/result ids -> reply future
        self.answers: dict[str, asyncio.Future] = {}

    async def call(self, method, params=None):
        raise RuntimeError(
            "Native mode: start and manage sessions in the Vibe terminal"
        )

    async def notify(self, method, params=None):
        raise RuntimeError("Native mode: control the turn in the Vibe terminal")

    async def _policy_decide(self, method, params):
        if method != "session/request_permission":
            return None, {}
        tc = params.get("toolCall") or {}
        text = server.tool_call_text(tc).lower()
        if any(rule.lower() in text for rule in server.POLICY.get("deny") or []):
            return None, {}
        if server.ONE_WAY_DOORS.search(text):
            return None, {}
        raw = tc.get("rawInput")
        raw = raw if isinstance(raw, dict) else {}
        command = next(
            (
                raw[key]
                for key in ("command", "cmd", "shell")
                if isinstance(raw.get(key), str)
            ),
            None,
        )
        if command is None or not shell_command_safe(command):
            return None, {}
        opts = params.get("options") or []
        option = next(
            (o for o in opts if o.get("kind") == "allow_once"),
            None,
        ) or next(
            (
                o
                for o in opts
                if str(o.get("kind", "")).startswith("allow")
                and o.get("kind") != "allow_always"
            ),
            None,
        )
        if not option:
            return None, {}
        return (
            {
                "outcome": {
                    "outcome": "selected",
                    "optionId": option.get("optionId") or option.get("id"),
                }
            },
            {"action": "auto-approved", "rule": "native-safe-shell"},
        )

    async def forward_to_app(self, msg: dict) -> None:
        self.app_stdin.write(json.dumps(msg).encode() + b"\n")
        await self.app_stdin.drain()

    async def _send(self, msg: dict) -> None:
        session_id, callback_id = self.callbacks.pop(msg["id"])
        outcome = msg["result"]["outcome"]
        decision = "approve" if outcome.get("optionId") == "allow-once" else "deny"
        wire_id = "baton:" + secrets.token_hex(16)
        fut = asyncio.get_running_loop().create_future()
        self.answers[wire_id] = fut
        try:
            await self.forward_to_app(
                {
                    "jsonrpc": "2.0",
                    "id": wire_id,
                    "method": "callback/result",
                    "params": {
                        "sessionId": session_id,
                        "result": {
                            "callbackId": callback_id,
                            "output": {
                                "type": "approval",
                                "decision": {"type": decision},
                            },
                        },
                    },
                }
            )
            reply = await asyncio.wait_for(fut, 10)
            if (reply.get("result") or {}).get("accepted") is not True:
                raise RuntimeError("Vibe did not accept the approval result")
        finally:
            self.answers.pop(wire_id, None)

    async def handle_app_message(self, msg: dict) -> bool:
        """Return True when the frame is consumed rather than forwarded."""
        wire_id = msg.get("id")
        if (
            isinstance(wire_id, str)
            and wire_id.startswith("baton:")
            and "method" not in msg
        ):
            fut = self.answers.get(wire_id)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return True  # swallow late replies: Rust sees only its own responses

        params = msg.get("params")
        params = params if isinstance(params, dict) else {}
        if msg.get("method") == "turn/completed":
            session_id = params.get("sessionId")
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if session_id and turn_id:
                for rid, req in list(self.outstanding_requests.items()):
                    req_params = req.get("params") or {}
                    if (
                        req_params.get("sessionId") == session_id
                        and req_params.get("turnId") == turn_id
                    ):
                        entry = self.pending_agent_requests.pop(rid, None)
                        if entry is not None:
                            relay_id, _, timer = entry
                            timer.cancel()
                            self.callbacks.pop(relay_id, None)
                        self.outstanding_requests.pop(rid, None)
                        # no deny back to Vibe: the turn already stopped
                        await self.broadcast(
                            {
                                "type": "agent_resolved",
                                "requestId": rid,
                                "decision": "cancelled",
                                "source": "server",
                            }
                        )
            return False

        raw_callback = params.get("callback")
        callback = raw_callback if isinstance(raw_callback, dict) else {}
        raw_detail = callback.get("detail")
        detail = raw_detail if isinstance(raw_detail, dict) else {}
        raw_effect = detail.get("effect")
        effect = raw_effect if isinstance(raw_effect, dict) else {}
        if msg.get("method") == "callback/call" and (
            trace := os.environ.get("BATON_NATIVE_TRACE")
        ):
            await asyncio.to_thread(
                _trace_native,
                trace,
                {
                    "id_type": type(wire_id).__name__,
                    "callback_keys": sorted(callback),
                    "kind": detail.get("kind"),
                    "effect_kind": effect.get("kind"),
                    "choices": detail.get("choices") or [],
                    "clients": len(self.clients),
                },
            )
        if msg.get("method") != "callback/call" or not self.clients:
            return False
        raw_choices = detail.get("choices")
        choices = raw_choices if isinstance(raw_choices, list) else []
        sid, cid = callback.get("sessionId"), callback.get("callbackId")
        raw_display = effect.get("display")
        display = raw_display if isinstance(raw_display, dict) else {}
        title = (
            callback.get("title")
            or effect.get("tool_name")
            or effect.get("toolName")
            or display.get("summary")
            or display.get("status_text")
            or "Vibe tool"
        )
        if (
            wire_id is None
            or not isinstance(sid, str)
            or not sid
            or not isinstance(cid, str)
            or not cid
            or detail.get("kind") != "approval"
            or "approve" not in choices
            or "deny" not in choices
        ):
            return False
        if (sid, cid) in self.callbacks.values():
            await self.forward_to_app(
                {
                    "jsonrpc": "2.0",
                    "id": wire_id,
                    "result": {"callbackId": cid, "accepted": True},
                }
            )
            return True
        await self.forward_to_app(
            {
                "jsonrpc": "2.0",
                "id": wire_id,
                "result": {"callbackId": cid, "accepted": True},
            }
        )
        rid = secrets.token_hex(12)
        self.callbacks[rid] = (sid, cid)
        await self._handle_agent_request(
            {
                "id": rid,
                "method": "session/request_permission",
                "params": {
                    "sessionId": sid,
                    "turnId": callback.get("turnId"),
                    "toolCall": {
                        "title": title,
                        "rawInput": effect.get("input"),
                    },
                    "options": [
                        {
                            "optionId": "allow-once",
                            "name": "Allow once",
                            "kind": "allow_once",
                        },
                        {
                            "optionId": "deny",
                            "name": "Deny",
                            "kind": "reject_once",
                        },
                    ],
                },
            }
        )
        return True


def write_stdout(raw: bytes) -> None:
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


async def run() -> None:
    if not os.environ.get("BATON_TOKEN"):
        raise SystemExit("BATON_TOKEN must be set for the baton WebSocket")
    executable = os.environ.get("BATON_NATIVE_APP_SERVER") or shutil.which(
        "vibe-app-server"
    )
    if not executable:
        raise SystemExit(
            "vibe-app-server not found on PATH; set BATON_NATIVE_APP_SERVER "
            "to its absolute path"
        )

    proc = await asyncio.create_subprocess_exec(
        executable,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
        # stderr inherits: app-server logs land on the TUI console
    )
    bridge = NativeBridge(proc.stdin)
    server.bridge = bridge

    app = web.Application()
    app.router.add_get("/ws", server.websocket_handler)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
    port = int(os.environ.get("BATON_NATIVE_PORT", "8767"))
    reader = None
    transport = None
    tasks: list[asyncio.Task] = []
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        log.info("native approval relay on ws://127.0.0.1:%s/ws", port)

        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
        )

        async def pump_stdin() -> None:
            while raw := await reader.readline():
                proc.stdin.write(raw)
                await proc.stdin.drain()

        async def pump_stdout() -> None:
            while raw := await proc.stdout.readline():
                msg = json.loads(raw)
                if not await bridge.handle_app_message(msg):
                    await asyncio.to_thread(write_stdout, raw)

        tasks = [
            asyncio.create_task(pump_stdin()),
            asyncio.create_task(pump_stdout()),
        ]
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if transport is not None:
            transport.close()
        for _rid, _started, timer in bridge.pending_agent_requests.values():
            timer.cancel()
        await asyncio.gather(
            *(ws.close() for ws in tuple(bridge.clients)),
            return_exceptions=True,
        )
        await runner.cleanup()
        try:
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), 2)
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except TimeoutError:
                pass


if __name__ == "__main__":
    asyncio.run(run())
