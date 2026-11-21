"""End-to-end rehearsal against a fake Telegram endpoint and a fake ACP.

Covers the still-pending item in docs/telegram-integration-review.md:
"fake Telegram + fake ACP scenario tests beyond the boundary contract
(including a rejected message payload)". No real Telegram, no vibe-acp,
no Mistral, no network beyond loopback. The live servers, Telegram poller
and public tunnel stay off.

Flow under test:

    fake ACP permission request
      -> baton /ws broadcast (agent_request)
      -> telegram_bridge main() sends a Telegram card (sendMessage)
      -> operator callback_query tap (Allow once / Deny) via getUpdates
      -> bridge sends agent_response over the baton WS
      -> fake ACP receives the JSON-RPC result, all clients get agent_resolved
      -> card edited in place into the verdict stamp (buttons removed)

Rejected payloads must change nothing: callbacks on a message that is not
the issued card, out-of-range option indexes, malformed callback data,
messages from any other chat / a group / a bot, and an agent_response the
server refuses all leave the request pending and the card unanswered.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

# Must be set before telegram_bridge is imported (module-level env reads).
os.environ["BATON_TOKEN"] = "e2e-token"
os.environ["TELEGRAM_BOT_TOKEN"] = "fakebot"
os.environ["TELEGRAM_CHAT_ID"] = "4242"
os.environ["BATON_NO_AGENT"] = "1"
os.environ.pop("MISTRAL_API_KEY", None)  # keep risk adjudication offline

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import server  # noqa: E402
import telegram_bridge as tb  # noqa: E402

CHAT = 4242
TOKEN = "e2e-token"
BOT = "fakebot"

PERMISSION_PARAMS = {
    "sessionId": "s",
    "toolCall": {"title": "write file", "rawInput": {"path": "/tmp/x"}},
    "options": [
        {"optionId": "once", "kind": "allow_once"},
        {"optionId": "no", "kind": "reject_once"},
    ],
}


class FakeTelegram:
    """aiohttp stand-in for api.telegram.org.

    Records every call; operator input is injected through `queue` and
    served by getUpdates.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.sent_payloads: dict[int, dict] = {}  # message_id -> sendMessage
        self.queue: asyncio.Queue = asyncio.Queue()
        self._next_mid = 1000

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get(f"/bot{BOT}/getUpdates", self.get_updates)
        app.router.add_post(f"/bot{BOT}/{{method}}", self.post)
        return app

    async def get_updates(self, request: web.Request) -> web.Response:
        try:
            upd = await asyncio.wait_for(self.queue.get(), timeout=0.2)
            return web.json_response({"ok": True, "result": [upd]})
        except asyncio.TimeoutError:
            return web.json_response({"ok": True, "result": []})

    async def post(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        payload = await request.json()
        self.calls.append((method, payload))
        if method == "sendMessage":
            self._next_mid += 1
            self.sent_payloads[self._next_mid] = payload
            return web.json_response(
                {
                    "ok": True,
                    "result": {
                        "message_id": self._next_mid,
                        "chat": {"id": payload.get("chat_id"), "type": "private"},
                    },
                }
            )
        if method == "editMessageText":
            return web.json_response(
                {"ok": True, "result": {"message_id": payload.get("message_id")}}
            )
        return web.json_response({"ok": True, "result": True})

    def sent_messages(self) -> list[dict]:
        return [p for m, p in self.calls if m == "sendMessage"]

    def edits(self) -> list[dict]:
        return [p for m, p in self.calls if m == "editMessageText"]

    def card(self):
        """The pending permission card: (payload, buttons, message_id)."""
        for mid, p in self.sent_payloads.items():
            kb = (p.get("reply_markup") or {}).get("inline_keyboard") or []
            buttons = [b for row in kb for b in row]
            if any(str(b.get("callback_data", "")).startswith("a:") for b in buttons):
                return p, buttons, mid
        return None, [], None


class FakeTelegramE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent: list[dict] = []  # payloads written to the fake ACP stdin

        # Fake ACP: the bridge records instead of spawning vibe-acp.
        self.bridge = server.AcpBridge()

        async def fake_send(payload: dict) -> None:
            self.sent.append(payload)

        self.bridge._send = fake_send

        # Baton server: only the routes the telegram bridge uses.
        app = web.Application()
        app.router.add_get("/ws", server.websocket_handler)
        app.router.add_get("/api/summarize", server.summarize)
        app.router.add_post("/api/summarize", server.summarize)
        for p in (
            patch.object(server, "bridge", self.bridge),
            patch.object(server, "TOKEN", TOKEN),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.baton_client = TestClient(TestServer(app))
        await self.baton_client.start_server()
        self.addCleanup(self.baton_client.close)
        base = str(self.baton_client.make_url("/")).rstrip("/")
        ws_url = base.replace("http://", "ws://") + "/ws"

        # Fake Telegram API.
        self.tg = FakeTelegram()
        self.tg_client = TestClient(TestServer(self.tg.app()))
        await self.tg_client.start_server()
        self.addCleanup(self.tg_client.close)
        tg_base = str(self.tg_client.make_url("/")).rstrip("/")

        for p in (
            patch.object(tb, "TG_API", f"{tg_base}/bot{BOT}"),
            patch.object(tb, "TG_TOKEN", BOT),
            patch.object(tb, "BATON_WS", ws_url),
            patch.object(tb, "BATON_HTTP", base),
            patch.object(tb, "BATON_TOKEN", TOKEN),
        ):
            p.start()
            self.addCleanup(p.stop)

        # Run the real bridge main loop (tg_loop + ws_loop) against both fakes.
        self.main_task = asyncio.create_task(tb.main())

        async def stop_main():
            self.main_task.cancel()
            await asyncio.gather(self.main_task, return_exceptions=True)

        self.addAsyncCleanup(stop_main)

    # -- helpers -----------------------------------------------------------

    async def wait_for(self, pred, what: str, timeout: float = 10):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if pred():
                return
            await asyncio.sleep(0.02)
        self.fail(f"timed out waiting for {what}")

    async def trigger_permission(self, acp_id: int):
        await self.bridge._handle_agent_request(
            {
                "jsonrpc": "2.0",
                "id": acp_id,
                "method": "session/request_permission",
                "params": dict(PERMISSION_PARAMS),
            }
        )

    async def wait_card(self):
        await self.wait_for(lambda: self.tg.card()[0] is not None, "telegram card")
        payload, buttons, mid = self.tg.card()
        rid = next(
            b["callback_data"][2:].split(":")[0]
            for b in buttons
            if str(b.get("callback_data", "")).startswith("a:")
        )
        return rid, mid, payload, buttons

    def callback(
        self,
        data: str,
        message_id: int,
        upd_id: int,
        chat=CHAT,
        sender=CHAT,
        is_bot=False,
    ):
        return {
            "update_id": upd_id,
            "callback_query": {
                "id": f"cb{upd_id}",
                "from": {"id": sender, "is_bot": is_bot},
                "message": {
                    "message_id": message_id,
                    "chat": {"type": "private", "id": chat},
                },
                "data": data,
            },
        }

    def text_message(
        self,
        upd_id: int,
        chat_id: int,
        sender_id: int,
        text: str,
        chat_type="private",
        is_bot=False,
    ):
        return {
            "update_id": upd_id,
            "message": {
                "message_id": upd_id,
                "chat": {"type": chat_type, "id": chat_id},
                "from": {"id": sender_id, "is_bot": is_bot},
                "text": text,
            },
        }

    def acp_result(self, acp_id: int):
        return next(
            (
                p.get("result")
                for p in self.sent
                if p.get("id") == acp_id and "result" in p
            ),
            None,
        )

    def verdict_stamp(self, label: str, source: str):
        for e in self.tg.edits():
            text = e.get("text") or ""
            kb = (e.get("reply_markup") or {}).get("inline_keyboard") or []
            if label in text and source in text and not kb:
                return e
        return None

    # -- tests -------------------------------------------------------------

    async def test_approval_flow_end_to_end(self):
        """Allow once tapped on the card -> fake ACP gets selected/once and
        the card becomes the receipt."""
        await self.trigger_permission(99)
        rid, mid, payload, buttons = await self.wait_card()

        # The card is bound to this request and offers exactly one allow_once
        # plus Deny.
        self.assertEqual(
            sorted(b["callback_data"] for b in buttons), [f"a:{rid}:0", f"d:{rid}"]
        )
        self.assertIn("Approval needed", payload["text"])
        self.assertNotIn("rawInput", payload["text"])
        self.assertEqual(payload["chat_id"], CHAT)

        await self.tg.queue.put(self.callback(f"a:{rid}:0", mid, 1))

        await self.wait_for(lambda: self.acp_result(99) is not None, "ACP result")
        self.assertEqual(
            self.acp_result(99),
            {"outcome": {"outcome": "selected", "optionId": "once"}},
        )
        self.assertNotIn(rid, self.bridge.outstanding_requests)

        # The tap was acknowledged and the card stamped with the verdict from
        # the telegram source, buttons removed.
        self.assertTrue(any(m == "answerCallbackQuery" for m, _ in self.tg.calls))
        await self.wait_for(
            lambda: self.verdict_stamp("Approved", "telegram") is not None,
            "verdict stamp",
        )

    async def test_deny_flow_end_to_end(self):
        await self.trigger_permission(100)
        rid, mid, _, buttons = await self.wait_card()
        self.assertIn(f"d:{rid}", [b["callback_data"] for b in buttons])

        await self.tg.queue.put(self.callback(f"d:{rid}", mid, 1))

        await self.wait_for(lambda: self.acp_result(100) is not None, "ACP result")
        self.assertEqual(self.acp_result(100), {"outcome": {"outcome": "cancelled"}})
        await self.wait_for(
            lambda: self.verdict_stamp("Denied", "telegram") is not None,
            "deny stamp",
        )

    async def test_rejected_telegram_payloads_change_nothing(self):
        await self.trigger_permission(101)
        rid, mid, _, _ = await self.wait_card()

        # Callback aimed at a message that is not the issued card.
        await self.tg.queue.put(self.callback(f"a:{rid}:0", mid + 500, 1))
        # Out-of-range option index on the real card.
        await self.tg.queue.put(self.callback(f"a:{rid}:9", mid, 2))
        # Malformed callback data.
        await self.tg.queue.put(self.callback("a:oops", mid, 3))
        # Messages from another chat, a group, and a bot sender.
        await self.tg.queue.put(self.text_message(4, 999, 999, "/start"))
        await self.tg.queue.put(
            self.text_message(5, CHAT, CHAT, "/start", chat_type="group")
        )
        await self.tg.queue.put(self.text_message(6, CHAT, CHAT, "/start", is_bot=True))

        await asyncio.sleep(1.0)
        self.assertIsNone(self.acp_result(101))
        self.assertEqual([p for p in self.sent if p.get("id") == 101], [])
        self.assertIn(rid, self.bridge.outstanding_requests)
        self.assertFalse(
            any("baton linked" in m.get("text", "") for m in self.tg.sent_messages())
        )
        self.assertIsNone(self.verdict_stamp("Approved", "telegram"))

        # The bridge survived the rejected payloads: a valid tap still works.
        await self.tg.queue.put(self.callback(f"a:{rid}:0", mid, 7))
        await self.wait_for(
            lambda: self.acp_result(101) is not None,
            "answer after rejected payloads",
        )
        self.assertEqual(self.acp_result(101)["outcome"]["optionId"], "once")

    async def test_server_rejects_invalid_answer_payload(self):
        """A browser client's malformed agent_response is refused with an
        error frame and leaves the request pending for a valid answer."""
        await self.trigger_permission(102)
        rid, _, _, _ = await self.wait_card()

        ws = await self.baton_client.ws_connect(
            "/ws", params={"token": TOKEN, "client": "browser"}
        )
        self.addAsyncCleanup(ws.close)

        snap = await ws.receive_json(timeout=2)
        self.assertEqual(snap["type"], "permission_snapshot")
        self.assertIn(rid, snap["requestIds"])

        async def until(pred, what):
            for _ in range(20):
                m = await ws.receive_json(timeout=2)
                if pred(m):
                    return m
            self.fail(f"never saw {what}")

        await until(lambda m: m.get("type") == "agent_request", "replayed card")

        # Unknown optionId.
        await ws.send_json(
            {
                "type": "agent_response",
                "requestId": rid,
                "result": {"outcome": {"outcome": "selected", "optionId": "bogus"}},
            }
        )
        err = await until(lambda m: m.get("type") == "error", "error frame")
        self.assertIn("rejected", err["message"].lower())
        self.assertIn(rid, self.bridge.outstanding_requests)

        # Malformed result (not an outcome object).
        await ws.send_json(
            {
                "type": "agent_response",
                "requestId": rid,
                "result": "yes",
            }
        )
        await until(lambda m: m.get("type") == "error", "second error frame")
        self.assertIn(rid, self.bridge.outstanding_requests)

        # A valid answer is still accepted afterwards, and the telegram card
        # is stamped with the browser as the source.
        await ws.send_json(
            {
                "type": "agent_response",
                "requestId": rid,
                "result": {"outcome": {"outcome": "selected", "optionId": "once"}},
            }
        )
        resolved = await until(
            lambda m: m.get("type") == "agent_resolved", "agent_resolved"
        )
        self.assertEqual(resolved["decision"], "approved")
        self.assertEqual(resolved["source"], "browser")
        self.assertEqual(
            self.acp_result(102),
            {"outcome": {"outcome": "selected", "optionId": "once"}},
        )
        await self.wait_for(
            lambda: self.verdict_stamp("Approved", "browser") is not None,
            "browser-source stamp",
        )


if __name__ == "__main__":
    unittest.main()
