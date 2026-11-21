"""baton <-> Telegram bridge: approve Vibe permission requests from one chat.

The bridge speaks the same WebSocket protocol as the phone client — it is
literally just another baton device. Permission requests arrive as live
Telegram cards (the message itself is the race clock — it edits in place,
then becomes the verdict stamp); inline buttons approve once or deny.
Exactly one operator is authorized: the private chat named by
TELEGRAM_CHAT_ID. Every message and callback is checked against it, and
buttons are bound to the chat/message the card was issued to.

Run:
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... BATON_TOKEN=... \
        uv run python tools/telegram_bridge.py

    BATON_WS      ws://localhost:8765/ws    (default)
    BATON_HTTP    http://localhost:8765     (default, for /api/* calls)
    TELEGRAM_CHAT_ID  positive numeric ID of the authorized private chat —
                      for a private chat this equals the operator's user ID

Get a bot token from @BotFather in Telegram, then /start your bot from the
operator account to confirm the link. /start confirms access; it cannot
grant it — only TELEGRAM_CHAT_ID does.
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

MAX_TG = 3900  # telegram text limit is 4096; leave margin
TICK = 2.5  # card clock refresh, seconds — comfortably under edit limits


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clock(t0: float) -> str:
    el = time.monotonic() - t0
    return f"{int(el // 60)}:{el % 60:04.1f}"


class Telegram:
    def __init__(self, client: httpx.AsyncClient, chat_id: int) -> None:
        self.http = client
        self.offset = 0
        self.chat_id = chat_id

    def accepts(self, message: dict, sender: dict) -> bool:
        chat = message.get("chat") or {}
        return (
            chat.get("type") == "private"
            and chat.get("id") == self.chat_id
            and sender.get("id") == self.chat_id
            and not sender.get("is_bot")
        )

    async def call(self, method: str, **payload):
        try:
            r = await self.http.post(f"{TG_API}/{method}", json=payload)
            data = r.json()
            if not data.get("ok"):
                log.warning("tg %s failed: %s", method, data.get("description"))
            return data.get("result")
        except Exception as exc:
            log.warning("tg %s error: %s", method, exc)
            return None

    async def send(
        self,
        chat_id: int,
        text: str,
        markup: dict | None = None,
        reply_to: int | None = None,
        silent: bool = False,
    ):
        return await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text[:MAX_TG],
            parse_mode="HTML",
            disable_web_page_preview=True,
            disable_notification=silent,
            **({"reply_markup": markup} if markup else {}),
            **({"reply_to_message_id": reply_to} if reply_to else {}),
        )

    async def edit(
        self, chat_id: int, message_id: int, text: str, markup: dict | None = None
    ):
        return await self.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text[:MAX_TG],
            parse_mode="HTML",
            reply_markup=markup or {"inline_keyboard": []},
        )

    async def broadcast(
        self, text: str, markup: dict | None = None, silent: bool = False
    ) -> list:
        r = await self.send(self.chat_id, text, markup, silent=silent)
        return [(self.chat_id, r.get("message_id"))] if r else []

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
        self.mode = None
        self.clients = 0
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
        if msg.get("type") == "bridge/clients":
            self.clients = msg.get("count", 0)
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

    async def rpc(self, method: str, params: dict | None = None, timeout: float = 60):
        self.next_id += 1
        rid = self.next_id
        fut = asyncio.get_event_loop().create_future()
        self.rpc_pending[rid] = fut
        await self.send(
            {"type": "rpc", "id": rid, "method": method, "params": params or {}}
        )
        return await asyncio.wait_for(fut, timeout=timeout)

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

    try:
        chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", ""))
    except ValueError:
        chat_id = 0
    if chat_id <= 0:
        raise SystemExit(
            "TELEGRAM_CHAT_ID must be the positive numeric ID of the authorized private chat"
        )

    tg = Telegram(httpx.AsyncClient(timeout=45), chat_id)
    bws = BatonWs()

    # requestId -> pending card state
    pending: dict[int, dict] = {}
    agent_text = ""
    flush_task: asyncio.Task | None = None

    await tg.call(
        "setMyCommands",
        commands=[
            {"command": "start", "description": "pair this chat with baton"},
            {"command": "new", "description": "fresh Vibe session"},
            {"command": "sessions", "description": "resume a recent session"},
            {"command": "stop", "description": "cancel the current turn"},
            {"command": "policy", "description": "show trust rules"},
            {"command": "status", "description": "bridge + session state"},
        ],
    )

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

    def card_text(entry: dict, verdict: str | None = None) -> str:
        risk = f"\nmistral · {esc(entry['risk'])}\n" if entry.get("risk") else ""
        if verdict:
            return entry["body"] + risk + f"\n<b>{verdict}</b>"
        return (
            entry["body"]
            + risk
            + f"\n⏱ <code>{clock(entry['t0'])}</code> — waiting on you"
        )

    def card_keyboard(req_id: int, options: list) -> dict:
        row = [
            {"text": "Allow once", "callback_data": f"a:{req_id}:{i}"}
            for i, opt in enumerate(options)
            if opt.get("kind") == "allow_once" and opt.get("optionId")
        ][:1]
        row.append({"text": "Deny", "callback_data": f"d:{req_id}"})
        return {"inline_keyboard": [row]}

    async def tick_card(req_id: int):
        """The card is the race clock — edit in place until resolved."""
        entry = pending.get(req_id)
        while entry and not entry.get("answered"):
            await asyncio.sleep(TICK)
            for cid, mid in entry["msgs"]:
                if mid:
                    await tg.edit(cid, mid, card_text(entry), entry["markup"])

    async def annotate(req_id: int, tool_call: dict):
        """Fetch Mistral's risk verdict and edit it into the card."""
        try:
            r = await tg.http.post(
                f"{BATON_HTTP}/api/summarize",
                params={"token": BATON_TOKEN},
                json={"toolCall": tool_call},
                timeout=20,
            )
            data = r.json()
        except Exception:
            return
        entry = pending.get(req_id)
        if not entry or entry.get("answered"):
            return
        summary, risk = data.get("summary") or "", data.get("risk") or ""
        if not summary and not risk:
            return
        icon = {"low": "🟢", "medium": "🟠", "high": "🔴"}.get(risk, "⚖")
        entry["risk"] = f"{icon} {summary}" if summary else f"{icon} {risk} risk"
        for cid, mid in entry["msgs"]:
            if mid:
                await tg.edit(cid, mid, card_text(entry), entry["markup"])

    async def stamp(req_id: int, verdict: str):
        """The card becomes the receipt — buttons off, verdict on."""
        entry = pending.pop(req_id, None)
        if not entry:
            return
        entry["answered"] = True
        for cid, mid in entry["msgs"]:
            if mid:
                await tg.edit(cid, mid, card_text(entry, verdict))

    async def answer(req_id: int, option_id, via: str):
        entry = pending.get(req_id)
        if not entry or entry.get("answered"):
            return False
        entry["answered"] = via
        result = (
            {"outcome": {"outcome": "selected", "optionId": option_id}}
            if option_id
            else {"outcome": {"outcome": "cancelled"}}
        )
        await bws.answer(req_id, result)
        return True

    async def on_baton(msg: dict):
        nonlocal agent_text
        mtype = msg.get("type")

        if mtype == "agent_request":
            if msg.get("method") != "session/request_permission":
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
            tc = msg.get("params", {}).get("toolCall") or {}
            options = msg.get("params", {}).get("options") or []
            title = tc.get("title") or "tool call"
            raw = json.dumps(tc.get("rawInput") or {}, ensure_ascii=False)[:1200]
            body = (
                "⚠️ <b>VIBE NEEDS A DECISION</b>\n\n"
                f"<b>{esc(title)}</b>\n"
                f"<blockquote expandable>{esc(raw)}</blockquote>"
            )
            markup = card_keyboard(rid, options)
            entry = {
                "options": options,
                "t0": time.monotonic(),
                "body": body,
                "markup": markup,
                "msgs": [],
            }
            pending[rid] = entry
            entry["msgs"] = await tg.broadcast(card_text(entry), markup)
            asyncio.create_task(tick_card(rid))
            asyncio.create_task(annotate(rid, tc))
            return

        if mtype == "agent_resolved":
            rid = msg.get("requestId")
            if pending.get(rid):
                await stamp(rid, "Closed by baton")
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
                    note += f"\nmistral · {esc(msg['summary'])}"
            await flush_agent_text()
            await tg.broadcast(note)
            return

        if mtype == "bridge_down":
            await tg.broadcast("🔌 baton bridge disconnected — reconnecting…")
            return

        if msg.get("method") == "session/update":
            u = (msg.get("params") or {}).get("update") or {}
            kind = u.get("sessionUpdate")
            if kind == "agent_message_chunk":
                agent_text += (u.get("content") or {}).get("text", "")
                schedule_flush()
            elif kind == "tool_call":
                await flush_agent_text()
                await tg.broadcast(
                    f"⚙ {esc(u.get('title') or u.get('kind') or 'tool call')}",
                    silent=True,
                )
            elif kind == "current_mode_update":
                bws.mode = u.get("currentModeId")
            elif kind == "turn_end":
                await flush_agent_text()

    async def typing_loop(cid: int, stop: asyncio.Event):
        """Keep the bot 'typing' while the agent works — it thinks with Vibe."""
        while not stop.is_set():
            await tg.call("sendChatAction", chat_id=cid, action="typing")
            try:
                await asyncio.wait_for(stop.wait(), timeout=4)
            except asyncio.TimeoutError:
                pass

    async def run_prompt(cid: int, text: str, reply_to: int | None):
        if not bws.session_id:
            try:
                r = await bws.rpc("session/new", {"cwd": bws.cwd, "mcpServers": []})
            except Exception as exc:
                await tg.send(cid, f"couldn't start a session: {exc}")
                return
            bws.session_id = r.get("sessionId")
            bws.mode = (r.get("modes") or {}).get("currentModeId")
        stop = asyncio.Event()
        typing = asyncio.create_task(typing_loop(cid, stop))
        try:
            r = await bws.rpc(
                "session/prompt",
                {
                    "sessionId": bws.session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=300,
            )
            reason = (r or {}).get("stopReason") or "done"
            await flush_agent_text()
            await tg.send(
                cid,
                f"✓ turn complete <code>{esc(str(reason))}</code>",
                reply_to=reply_to,
                silent=True,
            )
        except asyncio.TimeoutError:
            await tg.send(
                cid, "turn still running — cards will land here if it needs you"
            )
        except Exception as exc:
            await tg.send(cid, f"prompt failed: {esc(str(exc))[:200]}")
        finally:
            stop.set()
            typing.cancel()

    async def on_telegram(upd: dict):
        if "callback_query" in upd:
            cb = upd["callback_query"]
            message = cb.get("message") or {}
            if not tg.accepts(message, cb.get("from") or {}):
                return
            data = cb.get("data") or ""
            await tg.call("answerCallbackQuery", callback_query_id=cb["id"])
            act, _, rest = data.partition(":")
            if act == "d":
                try:
                    rid = int(rest)
                except ValueError:
                    return
                entry = pending.get(rid)
                if (
                    not entry
                    or (tg.chat_id, message.get("message_id")) not in entry["msgs"]
                ):
                    return
                await answer(rid, None, "tap")
            elif act == "a":
                rid_s, _, idx_s = rest.partition(":")
                try:
                    rid, idx = int(rid_s), int(idx_s)
                except ValueError:
                    return
                entry = pending.get(rid)
                if (
                    not entry
                    or (tg.chat_id, message.get("message_id")) not in entry["msgs"]
                ):
                    return
                if not 0 <= idx < len(entry["options"]):
                    return
                opt = entry["options"][idx]
                if not (opt.get("kind") == "allow_once" and opt.get("optionId")):
                    return
                await answer(rid, opt["optionId"], "tap")
            elif act == "l":
                sid = rest
                try:
                    r = await bws.rpc(
                        "session/load",
                        {"sessionId": sid, "cwd": bws.cwd, "mcpServers": []},
                    )
                except Exception as exc:
                    await tg.send(tg.chat_id, f"load failed: {exc}")
                    return
                bws.session_id = sid
                bws.mode = (r.get("modes") or {}).get("currentModeId")
                await tg.send(
                    tg.chat_id,
                    f"▶ resumed <code>{esc(sid[:8])}</code>"
                    + (f" · mode <code>{esc(bws.mode)}</code>" if bws.mode else ""),
                )
            return

        m = upd.get("message") or {}
        if not tg.accepts(m, m.get("from") or {}):
            return
        cid = tg.chat_id
        text = (m.get("text") or "").strip()
        if not text:
            return
        cmd, _, arg = text.partition(" ")
        if cmd == "/start":
            await tg.send(
                cid,
                "<b>baton linked to this private chat.</b>\n\n"
                "You are the authorized operator. When Vibe needs a decision, "
                "a card lands here — Allow once or Deny.\n\n"
                "/new session · /sessions resume · /stop turn\n"
                "/policy rules · /status bridge state\n\n"
                "Send any other text as a prompt for the current session.",
            )
        elif cmd == "/new":
            r = await bws.rpc("session/new", {"cwd": bws.cwd, "mcpServers": []})
            bws.session_id = r.get("sessionId")
            bws.mode = (r.get("modes") or {}).get("currentModeId")
            await tg.send(
                cid,
                f"🆕 session <code>{esc(str(bws.session_id)[:8])}</code>"
                + (f" · mode <code>{esc(bws.mode)}</code>" if bws.mode else "")
                + "\nidle — send text to give it a task",
            )
        elif cmd == "/sessions":
            r = await bws.rpc("session/list", {"cwd": bws.cwd})
            sessions = (r.get("sessions") or [])[:8]
            if not sessions:
                await tg.send(cid, "no saved sessions yet")
                return
            rows = [
                [
                    {
                        "text": f"{'▶ ' if s.get('sessionId') == bws.session_id else ''}{(s.get('title') or s.get('sessionId', '?'))[:34]}",
                        "callback_data": f"l:{s.get('sessionId')}",
                    }
                ]
                for s in sessions
            ]
            await tg.send(
                cid, "📂 <b>tap to resume:</b>", markup={"inline_keyboard": rows}
            )
        elif cmd == "/stop":
            if bws.session_id:
                await bws.notify("session/cancel", {"sessionId": bws.session_id})
                await tg.send(cid, "✋ cancel sent")
        elif cmd == "/status":
            await tg.send(
                cid,
                f"bridge: <b>{'connected' if bws.ws else 'down'}</b> · "
                f"{bws.clients} device(s) watching\n"
                f"session <code>{esc(str(bws.session_id)[:8]) if bws.session_id else '—'}</code>"
                + (f" · mode <code>{esc(bws.mode)}</code>" if bws.mode else "")
                + f"\n{len(pending)} decision(s) awaiting you",
            )
        elif cmd == "/policy":
            r = await tg.http.get(
                f"{BATON_HTTP}/api/policy", params={"token": BATON_TOKEN}
            )
            await tg.send(cid, f"<code>{esc(json.dumps(r.json(), indent=1))}</code>")
        elif not cmd.startswith("/"):
            asyncio.create_task(run_prompt(cid, text, m.get("message_id")))
        else:
            await tg.send(cid, "Unknown command. Use /start for help.")

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
