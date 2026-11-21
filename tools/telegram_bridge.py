"""baton <-> Telegram bridge: approve Vibe permission requests from one chat.

The bridge speaks the same WebSocket protocol as the phone client — it is
literally just another baton device. Permission requests arrive as
Telegram cards that edit in place and become the verdict stamp once the
server resolves the request; inline buttons approve once or deny.
The bridge is passive about decisions: no streamed text, tool
activity, auto-decisions, or connection notices reach the chat —
only permission cards and direct command replies.
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


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clip(text: str, limit: int = 1200) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def scalar_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def tool_detail(raw) -> tuple[str, str]:
    if raw is None:
        return "", ""
    if isinstance(raw, dict):
        command = next(
            (
                value.strip()
                for key in ("command", "cmd", "shell", "script")
                if isinstance((value := raw.get(key)), str) and value.strip()
            ),
            "",
        )
        if command:
            return "Shell command", clip(command)
        lines = []
        size = 0
        for key, value in raw.items():
            if value in (None, "", [], {}):
                continue
            line = f"{key}: {scalar_text(value)}"
            lines.append(line)
            size += len(line) + 1
            if size > 1200:
                break
        if lines:
            return "", clip("\n".join(lines))
        return ("", clip(json.dumps(raw, ensure_ascii=False))) if raw else ("", "")
    if isinstance(raw, str):
        return ("", clip(raw)) if raw.strip() else ("", "")
    return "", clip(json.dumps(raw, ensure_ascii=False))


def clean_title(title: str) -> str:
    text = (title or "").strip()
    for prefix in ("approve ", "allow ", "run "):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            break
    return text or "tool call"


def permission_body(tool_call: dict, session_id) -> str:
    title = str(tool_call.get("title") or "")
    low = title.lower()
    label, detail = tool_detail(tool_call.get("rawInput"))
    if not label:
        label = (
            "Shell command"
            if "command" in low or "bash" in low or "shell" in low
            else clean_title(title)
        )
    lines = ["⚠️ <b>Approval needed</b>", "", f"<b>{esc(label)}</b>"]
    if detail:
        lines.append(f"<pre>{esc(detail)}</pre>")
    if session_id:
        lines += ["", f"<code>{esc(str(session_id)[:8])}</code>"]
    return "\n".join(lines)


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

    async def broadcast(self, text: str, markup: dict | None = None) -> list:
        r = await self.send(self.chat_id, text, markup)
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

    def watches(self, params: dict) -> bool:
        return (
            self.session_id is not None and params.get("sessionId") == self.session_id
        )

    async def connect(self):
        ws = await websockets.connect(
            f"{BATON_WS}?token={BATON_TOKEN}&client=telegram",
            max_size=16 * 1024 * 1024,
        )
        self.ws = ws
        return ws

    async def _reader(self, ws):
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(msg)
        finally:
            if self.ws is ws:
                self.ws = None
                self.clients = 0
                for fut in self.rpc_pending.values():
                    if not fut.done():
                        fut.set_exception(RuntimeError("bridge disconnected"))
                self.rpc_pending.clear()
            await self.queue.put({"type": "bridge_down"})

    async def _dispatch(self, msg: dict):
        if msg.get("id") is not None and ("result" in msg or "error" in msg):
            fut = self.rpc_pending.pop(msg["id"], None)
            if fut is None or fut.done():
                return
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
        if msg.get("method") == "session/update":
            params = msg.get("params") or {}
            update = params.get("update") or {}
            if (
                self.watches(params)
                and update.get("sessionUpdate") == "current_mode_update"
            ):
                self.mode = update.get("currentModeId")
            return
        if msg.get("type") in {
            "permission_snapshot",
            "agent_request",
            "agent_resolved",
            "error",
        }:
            await self.queue.put(msg)

    async def send(self, msg: dict):
        if self.ws is None:
            raise RuntimeError("bridge disconnected")
        await self.ws.send(json.dumps(msg))

    async def rpc(
        self, method: str, params: dict | None = None, timeout: float | None = 60
    ):
        self.next_id += 1
        rid = self.next_id
        fut = asyncio.get_event_loop().create_future()
        self.rpc_pending[rid] = fut
        try:
            await self.send(
                {"type": "rpc", "id": rid, "method": method, "params": params or {}}
            )
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self.rpc_pending.pop(rid, None)

    async def resume_session(self, session_id: str) -> None:
        await self.rpc(
            "session/resume",
            {"sessionId": session_id, "cwd": self.cwd, "mcpServers": []},
        )
        if self.session_id != session_id:
            self.mode = None
        self.session_id = session_id

    async def notify(self, method: str, params: dict | None = None):
        await self.send({"type": "notify", "method": method, "params": params or {}})

    async def answer(self, request_id: str, result: dict):
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
    prompt_task: asyncio.Task | None = None
    prompt_session: str | None = None

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

    def card_text(entry: dict, verdict: str | None = None) -> str:
        parts = [entry["body"]]
        if entry.get("risk"):
            parts.append(esc(entry["risk"]))
        parts.append(f"<b>{verdict}</b>" if verdict else "<i>waiting</i>")
        return "\n\n".join(parts)

    def card_keyboard(req_id: str, options: list) -> dict:
        row = [
            {"text": "Allow once", "callback_data": f"a:{req_id}:{i}"}
            for i, opt in enumerate(options)
            if opt.get("kind") == "allow_once" and opt.get("optionId")
        ][:1]
        row.append({"text": "Deny", "callback_data": f"d:{req_id}"})
        return {"inline_keyboard": [row]}

    async def annotate(req_id: str, tool_call: dict):
        """Fetch Mistral's risk verdict and edit it into the card."""
        entry = pending.get(req_id)
        if not entry:
            return
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
        summary, risk = data.get("summary") or "", data.get("risk") or ""
        if not summary and not risk:
            return
        icon = {"low": "🟢", "medium": "🟠", "high": "🔴"}.get(risk, "⚖")
        async with entry["lock"]:
            if pending.get(req_id) is not entry or entry.get("answered"):
                return
            entry["risk"] = f"{icon} {summary}" if summary else f"{icon} {risk} risk"
            for cid, mid in entry["msgs"]:
                if mid:
                    await tg.edit(cid, mid, card_text(entry), entry["markup"])

    async def stamp(req_id: str, verdict: str):
        """The card becomes the receipt — buttons off, verdict on."""
        entry = pending.pop(req_id, None)
        if not entry:
            return
        entry["answered"] = True
        async with entry["lock"]:
            for cid, mid in entry["msgs"]:
                if mid:
                    await tg.edit(cid, mid, card_text(entry, verdict))

    async def answer(req_id: str, option_id, via: str):
        entry = pending.get(req_id)
        if not entry or entry.get("answered"):
            return False
        entry["answered"] = via
        result = (
            {"outcome": {"outcome": "selected", "optionId": option_id}}
            if option_id
            else {"outcome": {"outcome": "cancelled"}}
        )
        try:
            await bws.answer(req_id, result)
        except Exception:
            if pending.get(req_id) is entry:
                entry["answered"] = None
                await tg.send(
                    tg.chat_id,
                    "Couldn't reach baton — tap again to retry.",
                )
            return False
        return True

    async def on_baton(msg: dict):
        mtype = msg.get("type")

        if mtype == "permission_snapshot":
            live = set(msg.get("requestIds") or [])
            for rid in list(pending):
                if rid in live:
                    pending[rid]["answered"] = None
                else:
                    await stamp(rid, "Closed while disconnected")
            return

        if mtype == "agent_request":
            if msg.get("method") != "session/request_permission":
                return
            rid = msg["requestId"]
            entry = pending.get(rid)
            if entry and entry["msgs"]:
                return  # card already issued for this request
            tc = msg.get("params", {}).get("toolCall") or {}
            options = msg.get("params", {}).get("options") or []
            sid = msg.get("params", {}).get("sessionId")
            body = permission_body(tc, sid)
            markup = card_keyboard(rid, options)
            if not entry:
                entry = {
                    "options": options,
                    "body": body,
                    "markup": markup,
                    "msgs": [],
                    "lock": asyncio.Lock(),
                }
                pending[rid] = entry
            entry["msgs"] = await tg.broadcast(card_text(entry), markup)
            asyncio.create_task(annotate(rid, tc))
            return

        if mtype == "agent_resolved":
            rid = msg.get("requestId")
            if not pending.get(rid):
                return
            label = {
                "approved": "✅ Approved",
                "denied": "❌ Denied",
                "expired": "⌛ Expired",
                "cancelled": "✖ Cancelled",
                "unavailable": "⚠️ Unavailable",
            }.get(msg.get("decision"), "Closed")
            elapsed = msg.get("elapsedMs") or 0
            source = esc(str(msg.get("source") or "remote"))
            await stamp(rid, f"{label} · {elapsed / 1000:.1f}s · {source}")
            return

        if mtype == "error":
            rid = msg.get("requestId")
            entry = pending.get(rid)
            if entry:
                entry["answered"] = None
                await tg.send(
                    tg.chat_id,
                    "Answer not accepted; this card may already be closed.",
                )
            return

    async def typing_loop(cid: int, stop: asyncio.Event):
        """Keep the bot 'typing' while the agent works — it thinks with Vibe."""
        while not stop.is_set():
            await tg.call("sendChatAction", chat_id=cid, action="typing")
            try:
                await asyncio.wait_for(stop.wait(), timeout=4)
            except asyncio.TimeoutError:
                pass

    async def run_prompt(cid: int, text: str, reply_to: int | None):
        session_id = bws.session_id
        if not session_id:
            await tg.send(
                cid,
                "Choose a session with /sessions or create one with /new first.",
                reply_to=reply_to,
            )
            return
        stop = asyncio.Event()
        typing = asyncio.create_task(typing_loop(cid, stop))
        try:
            r = await bws.rpc(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=None,
            )
            reason = (r or {}).get("stopReason") or "done"
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
            await tg.send(
                cid,
                f"prompt failed: {esc(str(exc))[:160]}\n"
                "if the bridge disconnected, the turn may still be running",
            )
        finally:
            stop.set()
            typing.cancel()

    async def on_telegram(upd: dict):
        nonlocal prompt_task, prompt_session
        if "callback_query" in upd:
            cb = upd["callback_query"]
            message = cb.get("message") or {}
            if not tg.accepts(message, cb.get("from") or {}):
                return
            data = cb.get("data") or ""
            await tg.call("answerCallbackQuery", callback_query_id=cb["id"])
            act, _, rest = data.partition(":")
            if act == "d":
                rid = rest
                entry = pending.get(rid)
                if (
                    not entry
                    or (tg.chat_id, message.get("message_id")) not in entry["msgs"]
                ):
                    return
                await answer(rid, None, "tap")
            elif act == "a":
                rid, _, idx_s = rest.partition(":")
                try:
                    idx = int(idx_s)
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
                if prompt_task is not None and not prompt_task.done():
                    await tg.send(
                        tg.chat_id,
                        "Stop the current turn before changing sessions.",
                    )
                    return
                try:
                    await bws.resume_session(sid)
                except Exception as exc:
                    if f"Session is already open: {sid}" in str(exc):
                        await tg.send(
                            tg.chat_id,
                            "That session is open in another Vibe process. "
                            "Close it there and try again, or use /new here.",
                        )
                    else:
                        await tg.send(
                            tg.chat_id, f"Couldn't resume: {esc(str(exc)[:200])}"
                        )
                    return
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
            if prompt_task is not None and not prompt_task.done():
                await tg.send(cid, "Stop the current turn before changing sessions.")
                return
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
            target = (
                prompt_session
                if prompt_task is not None and not prompt_task.done()
                else bws.session_id
            )
            if target:
                await bws.notify("session/cancel", {"sessionId": target})
                await tg.send(cid, "✋ cancel sent")
            else:
                await tg.send(cid, "no session selected")
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
            if prompt_task is not None and not prompt_task.done():
                await tg.send(
                    cid,
                    "A turn is already active. Use /stop to cancel it first.",
                )
            else:
                prompt_session = bws.session_id
                prompt_task = asyncio.create_task(
                    run_prompt(cid, text, m.get("message_id"))
                )
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
            reader = None
            ws = None
            try:
                ws = await bws.connect()
                backoff = 1
                log.info("connected to %s", BATON_WS)
                reader = asyncio.create_task(bws._reader(ws))
                while True:
                    msg = await bws.queue.get()
                    if msg.get("type") == "bridge_down":
                        break
                    await on_baton(msg)
            except Exception as exc:
                log.warning("bridge conn: %s — retry in %ss", exc, backoff)
            finally:
                if reader is not None:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                if bws.ws is ws:
                    bws.ws = None
                # fresh queue per connection: drop stale buffered frames
                bws.queue = asyncio.Queue()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    await asyncio.gather(tg_loop(), ws_loop())


if __name__ == "__main__":
    asyncio.run(main())
