"""Offline boundary tests: QR auth on the server, operator auth in the bridge,
and the WS/permission contract against a fake ACP (mocked `_send`).

No network beyond aiohttp loopback, no Telegram, no vibe-acp.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import server  # noqa: E402
from telegram_bridge import BatonWs, Telegram  # noqa: E402

PERMISSION_PARAMS = {
    "sessionId": "s",
    "toolCall": {"title": "x"},
    "options": [
        {"optionId": "once", "kind": "allow_once"},
        {"optionId": "no", "kind": "reject_once"},
    ],
}


class TestQrAuth(unittest.IsolatedAsyncioTestCase):
    async def test_qr_png_rejects_without_token(self):
        response = await server.qr_png(make_mocked_request("GET", "/qr.png"))
        self.assertEqual(response.status, 401)

    async def test_qr_png_serves_with_token(self):
        with patch.object(server, "TOKEN", "offline-test-token"):
            response = await server.qr_png(
                make_mocked_request(
                    "GET",
                    "/qr.png",
                    headers={"X-Vibe-Token": "offline-test-token"},
                )
            )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(response.body.startswith(b"\x89PNG\r\n\x1a\n"))


def msg(chat_type, chat_id):
    return {"chat": {"type": chat_type, "id": chat_id}}


def sender(user_id, is_bot=False):
    return {"id": user_id, "is_bot": is_bot}


class TestTelegramAccepts(unittest.TestCase):
    def setUp(self):
        self.bridge = Telegram(None, 123)

    def test_accepts_operator_private_chat(self):
        self.assertTrue(self.bridge.accepts(msg("private", 123), sender(123)))

    def test_rejects_wrong_chat(self):
        self.assertFalse(self.bridge.accepts(msg("private", 321), sender(321)))

    def test_rejects_wrong_sender(self):
        self.assertFalse(self.bridge.accepts(msg("private", 123), sender(321)))

    def test_rejects_group_chat(self):
        self.assertFalse(self.bridge.accepts(msg("group", 123), sender(123)))

    def test_rejects_missing_sender(self):
        self.assertFalse(self.bridge.accepts(msg("private", 123), {}))

    def test_rejects_bot_sender(self):
        self.assertFalse(self.bridge.accepts(msg("private", 123), sender(123, True)))


def permission_msg(acp_id, params=None):
    return {
        "id": acp_id,
        "method": "session/request_permission",
        "params": params or PERMISSION_PARAMS,
    }


class WsCase(unittest.IsolatedAsyncioTestCase):
    """aiohttp loopback client against only the /ws route and a fake ACP."""

    async def connect(self, bridge, client_label="telegram"):
        app = web.Application()
        app.router.add_get("/ws", server.websocket_handler)
        self._patch = patch.object(server, "bridge", bridge)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addCleanup(client.close)
        ws = await client.ws_connect(
            "/ws", params={"token": server.TOKEN, "client": client_label}
        )
        self.addCleanup(ws.close)
        return ws

    async def next_type(self, ws, want, tries=10):
        for _ in range(tries):
            m = await ws.receive_json(timeout=2)
            if m.get("type") == want or m.get("method") == want:
                return m
        self.fail(f"never saw {want}")

    def fake_acp(self, bridge, sent):
        """_send replacement: emits the permission request mid-prompt and
        completes the prompt once the agent request is answered."""
        state = {}

        async def fake_send(payload):
            sent.append(payload)
            if payload.get("method") == "session/prompt":
                state["prompt_id"] = payload["id"]
                asyncio.get_event_loop().create_task(
                    bridge._handle_agent_request(permission_msg(99))
                )
            elif payload.get("id") == 99 and "result" in payload:
                await bridge._dispatch(
                    {
                        "id": state["prompt_id"],
                        "result": {"stopReason": "end_turn"},
                    }
                )

        return fake_send


class TestPermissionContract(WsCase):
    async def test_prompt_can_be_answered_on_same_socket(self):
        bridge = server.AcpBridge()
        sent = []
        bridge._send = self.fake_acp(bridge, sent)
        ws = await self.connect(bridge)

        first = await ws.receive_json(timeout=2)
        self.assertEqual(first["type"], "permission_snapshot")
        self.assertEqual(first["requestIds"], [])

        await ws.send_json(
            {
                "type": "rpc",
                "id": 7,
                "method": "session/prompt",
                "params": {"sessionId": "s", "prompt": []},
            }
        )
        card = await self.next_type(ws, "agent_request")
        self.assertIsInstance(card["requestId"], str)
        self.assertNotEqual(card["requestId"], 99)

        await ws.send_json(
            {
                "type": "agent_response",
                "requestId": card["requestId"],
                "result": {"outcome": {"outcome": "selected", "optionId": "once"}},
            }
        )

        seen = []
        for _ in range(10):
            m = await ws.receive_json(timeout=2)
            seen.append(m)
            if any(x.get("type") == "agent_resolved" for x in seen) and any(
                x.get("id") == 7 and "result" in x for x in seen
            ):
                break

        acp_replies = [m for m in sent if m.get("id") == 99]
        self.assertEqual(len(acp_replies), 1)
        self.assertEqual(
            acp_replies[0]["result"],
            {"outcome": {"outcome": "selected", "optionId": "once"}},
        )
        resolved = [m for m in seen if m.get("type") == "agent_resolved"]
        self.assertEqual(resolved[0]["decision"], "approved")
        self.assertEqual(resolved[0]["source"], "telegram")
        self.assertEqual(
            [m for m in seen if m.get("id") == 7][0]["result"],
            {"stopReason": "end_turn"},
        )

    async def test_first_valid_answer_wins(self):
        bridge = server.AcpBridge()
        bridge._send = AsyncMock()
        bridge.broadcast = AsyncMock()
        await bridge._handle_agent_request(permission_msg(1))
        rid = next(iter(bridge.outstanding_requests))

        bad = await bridge.answer_agent_request(
            rid,
            {"outcome": {"outcome": "selected", "optionId": "not-offered"}},
            "telegram",
        )
        self.assertFalse(bad)
        self.assertIn(rid, bridge.outstanding_requests)

        results = await asyncio.gather(
            bridge.answer_agent_request(
                rid,
                {"outcome": {"outcome": "selected", "optionId": "once"}},
                "telegram",
            ),
            bridge.answer_agent_request(
                rid, {"outcome": {"outcome": "cancelled"}}, "browser"
            ),
        )
        self.assertEqual(results, [True, False])
        bridge._send.assert_awaited_once()

        resolved = [
            c.args[0]
            for c in bridge.broadcast.await_args_list
            if c.args[0].get("type") == "agent_resolved"
        ]
        self.assertEqual(resolved[0]["decision"], "approved")
        self.assertEqual(resolved[0]["source"], "telegram")
        self.assertEqual(resolved[0]["optionId"], "once")

    async def test_snapshot_replays_only_pending(self):
        bridge = server.AcpBridge()
        bridge._send = AsyncMock()
        bridge.broadcast = AsyncMock()
        await bridge._handle_agent_request(permission_msg(91))
        await bridge._handle_agent_request(permission_msg(92))
        rid91, rid92 = list(bridge.outstanding_requests)
        await bridge.answer_agent_request(
            rid91, {"outcome": {"outcome": "cancelled"}}, "server"
        )
        self.addAsyncCleanup(
            bridge.answer_agent_request,
            rid92,
            {"outcome": {"outcome": "cancelled"}},
            "server",
        )

        ws = await self.connect(bridge)
        first = await ws.receive_json(timeout=2)
        self.assertEqual(first["type"], "permission_snapshot")
        self.assertEqual(first["requestIds"], [rid92])
        replay = await self.next_type(ws, "agent_request")
        self.assertEqual(replay["requestId"], rid92)

    async def test_cancel_reaches_agent_while_prompt_in_flight(self):
        bridge = server.AcpBridge()
        sent = []
        state = {}

        async def fake_send(payload):
            sent.append(payload)
            if payload.get("method") == "session/prompt":
                state["prompt_id"] = payload["id"]
                asyncio.get_event_loop().create_task(
                    bridge._handle_agent_request(permission_msg(99))
                )
            elif payload.get("id") == 99 and "result" in payload:
                await bridge._dispatch(
                    {
                        "id": state["prompt_id"],
                        "result": {"stopReason": "cancelled"},
                    }
                )

        bridge._send = fake_send
        ws = await self.connect(bridge)
        await ws.receive_json(timeout=2)  # snapshot
        await ws.send_json(
            {
                "type": "rpc",
                "id": 7,
                "method": "session/prompt",
                "params": {"sessionId": "s", "prompt": []},
            }
        )
        card = await self.next_type(ws, "agent_request")
        await ws.send_json(
            {
                "type": "notify",
                "method": "session/cancel",
                "params": {"sessionId": "s"},
            }
        )

        seen = []
        for _ in range(10):
            m = await ws.receive_json(timeout=2)
            seen.append(m)
            if any(x.get("type") == "agent_resolved" for x in seen) and any(
                x.get("id") == 7 and "result" in x for x in seen
            ):
                break

        notifies = [m for m in sent if m.get("method") == "session/cancel"]
        self.assertEqual(notifies[0]["params"]["sessionId"], "s")
        acp_reply = [m for m in sent if m.get("id") == 99][0]
        self.assertEqual(acp_reply["result"], {"outcome": {"outcome": "cancelled"}})
        resolved = [m for m in seen if m.get("type") == "agent_resolved"]
        self.assertEqual(resolved[0]["decision"], "cancelled")
        self.assertEqual(
            [m for m in seen if m.get("id") == 7][0]["result"],
            {"stopReason": "cancelled"},
        )
        self.assertNotIn(card["requestId"], bridge.outstanding_requests)

    async def test_timed_out_rpc_does_not_break_reader(self):
        bws = BatonWs()
        bws.send = AsyncMock()
        with self.assertRaises(TimeoutError):
            await bws.rpc("session/list", timeout=0)
        self.assertEqual(bws.rpc_pending, {})
        await bws._dispatch({"id": 1, "result": {}})
        self.assertTrue(bws.queue.empty())
        await bws._dispatch(
            {
                "method": "session/update",
                "params": {"sessionId": "other", "update": {}},
            }
        )
        self.assertIsNone(bws.session_id)
        self.assertFalse(bws.watches({"sessionId": "other"}))
        bws.session_id = "s"
        self.assertFalse(bws.watches({"sessionId": "other"}))
        self.assertFalse(bws.watches({}))
        self.assertTrue(bws.watches({"sessionId": "s"}))

    async def test_telegram_only_queues_permission_events(self):
        bws = BatonWs()
        bws.session_id = "s"
        for update in (
            {"sessionUpdate": "tool_call", "title": "Reading output from process"},
            {"sessionUpdate": "agent_message_chunk", "content": {"text": "partial"}},
            {"sessionUpdate": "current_mode_update", "currentModeId": "ask"},
        ):
            await bws._dispatch(
                {
                    "method": "session/update",
                    "params": {"sessionId": "s", "update": update},
                }
            )
        await bws._dispatch({"type": "auto_decided", "action": "denied"})
        self.assertTrue(bws.queue.empty())
        self.assertEqual(bws.mode, "ask")
        for event in (
            {"type": "agent_request", "requestId": "demo"},
            {"type": "agent_resolved", "requestId": "demo"},
        ):
            await bws._dispatch(event)
            self.assertEqual(bws.queue.get_nowait(), event)

    async def test_permission_expires_server_side(self):
        bridge = server.AcpBridge()
        bridge._send = AsyncMock()
        bridge.broadcast = AsyncMock()
        with patch.object(server, "AGENT_REQUEST_TIMEOUT", 0):
            await bridge._handle_agent_request(permission_msg(99))
            rid = next(iter(bridge.outstanding_requests))
            timer = bridge.pending_agent_requests[rid][2]
            await asyncio.wait_for(timer, 2)
        self.assertNotIn(rid, bridge.outstanding_requests)
        bridge._send.assert_awaited_once_with(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "result": {"outcome": {"outcome": "cancelled"}},
            }
        )
        resolved = [
            c.args[0]
            for c in bridge.broadcast.await_args_list
            if c.args[0].get("type") == "agent_resolved"
        ]
        self.assertEqual(resolved[-1]["decision"], "expired")
        self.assertEqual(resolved[-1]["source"], "server")

        # a client reconnecting afterwards gets an empty snapshot, no revival
        ws = await self.connect(bridge, client_label="browser")
        first = await ws.receive_json(timeout=2)
        self.assertEqual(first["type"], "permission_snapshot")
        self.assertEqual(first["requestIds"], [])


if __name__ == "__main__":
    unittest.main()
