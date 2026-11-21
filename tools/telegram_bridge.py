"""baton <-> Telegram bridge: approve Vibe permission requests from a chat.

The bridge speaks the same WebSocket protocol as the phone client — it is
literally just another baton device. Permission requests arrive as Telegram
messages with inline Approve/Deny buttons; first answer wins across all
devices (phone, stage, Telegram).

Run:
    TELEGRAM_BOT_TOKEN=... BATON_TOKEN=... uv run python tools/telegram_bridge.py

    BATON_WS     ws://localhost:8765/ws   (default)
    BATON_HTTP   http://localhost:8765    (default, for /api/policy)
    TELEGRAM_CHAT_ID  pin the bridge to one chat; otherwise any chat that
                      sends /start becomes the control chat

Get a bot token from @BotFather in Telegram, then /start your bot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx
import websockets

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # URLs carry the bot token
log = logging.getLogger("baton.tg")

BATON_WS = os.environ.get("BATON_WS", "ws://localhost:8765/ws")
BATON_HTTP = os.environ.get("BATON_HTTP", "http://localhost:8765")
BATON_TOKEN = os.environ.get("BATON_TOKEN", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
PINNED_CHAT = os.environ.get("TELEGRAM_CHAT_ID")

MAX_TG = 3900  # telegram text limit is 4096; leave margin

VIA = "telegram"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Telegram:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.http = client
        self.offset = 0
        self.chats: set[int] = set()
        if PINNED_CHAT:
            self.chats.add(int(PINNED_CHAT))

    async def call(self, method: str, **payload):
        try:
            r = await self.http.post(f"{TG_API}/{method}", json=payload)
            data = r.json()
            if not data.get("ok"):
                log.warning("tg %s failed: %s", method, data)
            return data.get("result")
        except Exception as exc:
            log.warning("tg %s error: %s", method, exc)
            return None

    async def send(self, chat_id: int, text: str, markup: dict | None = None):
        return await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text[:MAX_TG],
            parse_mode="HTML",
            disable_web_page_preview=True,
            **({"reply_markup": markup} if markup else {}),
        )

    async def edit(self, chat_id: int, message_id: int, text: str):
        return await self.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text[:MAX_TG],
            parse_mode="HTML",
        )

    async def broadcast(self, text: str, markup: dict | None = None) -> list:
        out = []
        for cid in list(self.chats):
            r = await self.send(cid, text, markup)
            if r:
                out.append((cid, r.get("message_id")))
        return out

    async def updates(self) -> list:
        r = await self.http.get(
            f"{TG_API}/getUpdates",
            params={"offset": self.offset, "timeout": 30},
            timeout=40,
        )
        return r.json().get("result", [])


class BatonWs:
    """Minimal baton WS client: mirrors what static/index.html sends."""

    def __init__(self) -> None:
        self.ws = None
        self.next_id = 0
        self.rpc_pending: dict[int, asyncio.Future] = {}
        self.cwd = None
        self.session_id = None
        self.queue: asyncio.Queue = asyncio.Queue()

    async def connect(self):
        ws = await websockets.connect(
            f"{BATON_WS}?token={BATON_TOKEN}", max_size=16 * 1024 * 1024
        )
        self.ws = ws
        asyncio.create_task(self._reader())
        return ws

    async def _reader(self):
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(msg)
        finally:
            await self.queue.put({"type": "bridge_down"})
            for fut in self.rpc_pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("bridge disconnected"))
            self.rpc_pending.clear()

    async def _dispatch(self, msg: dict):
        if msg.get("id") is not None and msg["id"] in self.rpc_pending:
            fut = self.rpc_pending.pop(msg["id"])
            if "error" in msg:
                fut.set_exception(RuntimeError(str(msg["error"])))
            else:
                fut.set_result(msg.get("result"))
            return
        if msg.get("method") == "bridge/status":
            p = msg.get("params", {})
            self.cwd = p.get("cwd") or self.cwd
            log.info("bridge status: %s cwd=%s", p.get("state"), self.cwd)
            return
        # learn the active session id from stream traffic
        sid = (msg.get("params") or {}).get("sessionId")
        if sid:
            self.session_id = sid
        await self.queue.put(msg)

    async def send(self, msg: dict):
        if self.ws:
            await self.ws.send(json.dumps(msg))

    async def rpc(self, method: str, params: dict | None = None):
        self.next_id += 1
        rid = self.next_id
        fut = asyncio.get_event_loop().create_future()
        self.rpc_pending[rid] = fut
        await self.send(
            {"type": "rpc", "id": rid, "method": method, "params": params or {}}
        )
        return await asyncio.wait_for(fut, timeout=60)

    async def notify(self, method: str, params: dict | None = None):
        await self.send({"type": "notify", "method": method, "params": params or {}})

    async def answer(self, request_id: int, result: dict):
        await self.send(
            {"type": "agent_response", "requestId": request_id, "result": result}
        )


async def main() -> None:
    if not TG_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required (talk to @BotFather)")
    if not BATON_TOKEN:
        raise SystemExit("BATON_TOKEN is required (printed by server.py)")

    tg = Telegram(httpx.AsyncClient(timeout=45))
    bws = BatonWs()

    # requestId -> {"options": [...], "msgs": [(chat_id, message_id)], "t0": ts,
    #               "title": str, "answered": via|None}
    pending: dict[int, dict] = {}
    agent_text = ""
    flush_task: asyncio.Task | None = None

    async def flush_agent_text():
        nonlocal agent_text
        if not agent_text.strip():
            return
        text, agent_text = agent_text.strip(), ""
        await tg.broadcast("🤖 " + esc(text))

    async def flush_later(delay: float):
        try:
            await asyncio.sleep(delay)
            await flush_agent_text()
        except asyncio.CancelledError:
            pass

    def schedule_flush():
        nonlocal flush_task
        if flush_task:
            flush_task.cancel()
        flush_task = asyncio.create_task(flush_later(2.5))

    def perm_card(req_id: int, params: dict) -> str:
        tc = params.get("toolCall") or {}
        title = tc.get("title") or "tool call"
        raw = json.dumps(tc.get("rawInput") or {}, ensure_ascii=False)[:400]
        return (
            f"⚠️ <b>VIBE NEEDS A DECISION</b>\n\n"
            f"<b>{esc(title)}</b>\n"
            f"<code>{esc(raw)}</code>\n\n"
            f"request #{req_id}"
        )

    def perm_keyboard(req_id: int, options: list) -> dict:
        rows = []
        for i, o in enumerate(options):
            name = o.get("name") or o.get("optionId") or o.get("id") or f"option {i}"
            kind = str(o.get("kind", ""))
            icon = "✅" if kind.startswith("allow") else "⛔"
            rows.append(
                [{"text": f"{icon} {name}", "callback_data": f"a:{req_id}:{i}"}]
            )
        rows.append([{"text": "✕ Deny", "callback_data": f"d:{req_id}"}])
        return {"inline_keyboard": rows}

    async def resolve_note(req_id: int, note: str):
        entry = pending.pop(req_id, None)
        if not entry:
            return
        for cid, mid in entry["msgs"]:
            if mid:
                await tg.edit(cid, mid, entry["card"] + "\n\n" + note)

    async def on_baton(msg: dict):
        nonlocal agent_text
        mtype = msg.get("type")

        if mtype == "agent_request":
            if msg.get("method") != "session/request_permission":
                # unsupported client request — decline politely
                await bws.send(
                    {
                        "type": "agent_response",
                        "requestId": msg["requestId"],
                        "error": {
                            "code": -32601,
                            "message": "unsupported by telegram bridge",
                        },
                    }
                )
                return
            await flush_agent_text()
            rid = msg["requestId"]
            params = msg.get("params", {})
            card = perm_card(rid, params)
            markup = perm_keyboard(rid, params.get("options") or [])
            msgs = await tg.broadcast(card, markup)
            pending[rid] = {
                "options": params.get("options") or [],
                "msgs": msgs,
                "t0": time.monotonic(),
                "card": card,
            }
            return

        if mtype == "agent_resolved":
            rid = msg.get("requestId")
            entry = pending.get(rid)
            if entry and not entry.get("answered"):
                await resolve_note(rid, "<i>resolved on another device</i>")
            else:
                pending.pop(rid, None)
            return

        if mtype == "auto_decided":
            title = ((msg.get("params") or {}).get("toolCall") or {}).get(
                "title", "tool call"
            )
            if msg.get("action") == "denied":
                note = f"⛔ <b>denied by rule</b> <code>{esc(str(msg.get('rule')))}</code>: {esc(title)}"
            else:
                note = f"⚡ <b>auto-approved</b>: {esc(title)}"
                if msg.get("summary"):
                    note += f"\n<i>{esc(msg['summary'])}</i>"
            await flush_agent_text()
            await tg.broadcast(note)
            return

        if mtype == "bridge_down":
            await tg.broadcast("🔌 baton bridge disconnected — reconnecting…")
            return

        # ACP session/update notifications
        if msg.get("method") == "session/update":
            u = (msg.get("params") or {}).get("update") or {}
            kind = u.get("sessionUpdate")
            if kind == "agent_message_chunk":
                agent_text += (u.get("content") or {}).get("text", "")
                schedule_flush()
            elif kind == "tool_call":
                await flush_agent_text()
                await tg.broadcast(
                    f"⚙ {esc(u.get('title') or u.get('kind') or 'tool call')}"
                )
            elif kind == "turn_end":
                await flush_agent_text()

    async def on_telegram(upd: dict):
        if "message" in upd:
            m = upd["message"]
            cid = m["chat"]["id"]
            text = (m.get("text") or "").strip()
            if not text:
                return
            cmd, _, arg = text.partition(" ")
            if cmd in ("/start", "/pair"):
                tg.chats.add(cid)
                await tg.send(
                    cid,
                    "🏃 <b>baton paired.</b>\n\n"
                    "Permission requests will land here. Commands:\n"
                    "/new — fresh session · /stop — cancel turn\n"
                    "/auto — toggle low-risk auto-approve · /policy — show rules\n"
                    "anything else — sent to Vibe as a prompt",
                )
            elif cmd == "/new":
                r = await bws.rpc("session/new", {"cwd": bws.cwd, "mcpServers": []})
                bws.session_id = r.get("sessionId")
                await tg.send(
                    cid, f"🆕 session <code>{esc(str(bws.session_id)[:8])}</code>"
                )
            elif cmd == "/stop":
                if bws.session_id:
                    await bws.notify("session/cancel", {"sessionId": bws.session_id})
                    await tg.send(cid, "✋ cancel sent")
            elif cmd == "/policy":
                r = await tg.http.get(
                    f"{BATON_HTTP}/api/policy", params={"token": BATON_TOKEN}
                )
                await tg.send(
                    cid, f"<code>{esc(json.dumps(r.json(), indent=1))}</code>"
                )
            elif cmd == "/auto":
                p = (
                    await tg.http.get(
                        f"{BATON_HTTP}/api/policy", params={"token": BATON_TOKEN}
                    )
                ).json()
                n = (
                    await tg.http.post(
                        f"{BATON_HTTP}/api/policy",
                        params={"token": BATON_TOKEN},
                        json={"auto_low": not p.get("auto_low")},
                    )
                ).json()
                await tg.send(
                    cid,
                    f"🛡 auto-approve low-risk: <b>{'ON' if n.get('auto_low') else 'OFF'}</b>\n"
                    "one-way doors still escalate",
                )
            elif cid in tg.chats:
                if not bws.session_id:
                    r = await bws.rpc("session/new", {"cwd": bws.cwd, "mcpServers": []})
                    bws.session_id = r.get("sessionId")
                await bws.notify(
                    "session/prompt",
                    {
                        "sessionId": bws.session_id,
                        "prompt": [{"type": "text", "text": text}],
                    },
                )
                await tg.send(cid, f"📨 sent: {esc(text[:80])}")
        elif "callback_query" in upd:
            cb = upd["callback_query"]
            data = cb.get("data") or ""
            cid = cb["message"]["chat"]["id"]
            await tg.call("answerCallbackQuery", callback_query_id=cb["id"])
            act, _, rest = data.partition(":")
            if act == "d":
                rid = int(rest)
                entry = pending.get(rid)
                if not entry or entry.get("answered"):
                    return
                entry["answered"] = VIA
                await bws.answer(rid, {"outcome": {"outcome": "cancelled"}})
                el = time.monotonic() - entry["t0"]
                await resolve_note(rid, f"✕ <b>rejected</b> · {el:.1f}s · via Telegram")
            elif act == "a":
                rid_s, _, idx_s = rest.partition(":")
                rid, idx = int(rid_s), int(idx_s)
                entry = pending.get(rid)
                if not entry or entry.get("answered"):
                    return
                opt = (
                    (entry["options"] or [None])[idx]
                    if idx < len(entry["options"])
                    else None
                )
                if not opt:
                    return
                entry["answered"] = VIA
                oid = opt.get("optionId") or opt.get("id")
                await bws.answer(
                    rid,
                    {"outcome": {"outcome": "selected", "optionId": oid}},
                )
                el = time.monotonic() - entry["t0"]
                name = opt.get("name") or oid
                await resolve_note(
                    rid, f"✅ <b>{esc(str(name))}</b> · {el:.1f}s · via Telegram"
                )

    async def tg_loop():
        while True:
            try:
                for upd in await tg.updates():
                    tg.offset = max(tg.offset, upd["update_id"] + 1)
                    await on_telegram(upd)
            except Exception as exc:
                log.warning("tg poll: %s", exc)
                await asyncio.sleep(3)

    async def ws_loop():
        backoff = 1
        while True:
            try:
                await bws.connect()
                backoff = 1
                log.info("connected to %s", BATON_WS)
                while True:
                    msg = await bws.queue.get()
                    await on_baton(msg)
            except Exception as exc:
                log.warning("bridge conn: %s — retry in %ss", exc, backoff)
                await tg.broadcast("🔌 baton bridge unreachable — retrying…")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    await asyncio.gather(tg_loop(), ws_loop())


if __name__ == "__main__":
    asyncio.run(main())
