"""Offline tests for the native CLI approval relay (native_cli.py)."""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import native_cli

FRAME = {
    "jsonrpc": "2.0",
    "id": 17,
    "method": "callback/call",
    "params": {
        "callback": {
            "sessionId": "native",
            "callbackId": "c1",
            "turnId": "t1",
            "title": "touch demo.txt",
            "detail": {
                "kind": "approval",
                "choices": ["approve", "approve_permanently", "deny"],
                "effect": {
                    "kind": "tool",
                    "toolName": "bash",
                    "input": {"command": "touch demo.txt"},
                },
            },
        }
    },
}


def make_bridge():
    writer = Mock()
    writer.drain = AsyncMock()
    bridge = native_cli.NativeBridge(writer)
    ws = Mock()
    ws.send_json = AsyncMock()
    bridge.clients.add(ws)
    return bridge, writer, ws


def writes(writer):
    return [json.loads(c.args[0]) for c in writer.write.call_args_list]


class TestNativeApprovalRelay(unittest.IsolatedAsyncioTestCase):
    async def test_approval_card_and_callback_result(self):
        bridge, writer, ws = make_bridge()
        frame = json.loads(json.dumps(FRAME))
        self.assertTrue(await bridge.handle_app_message(frame))

        sent = writes(writer)
        self.assertEqual(
            sent[0],
            {
                "jsonrpc": "2.0",
                "id": 17,
                "result": {"callbackId": "c1", "accepted": True},
            },
        )
        self.assertEqual(len(sent), 1)  # no semantic result before the tap

        rid = next(iter(bridge.outstanding_requests))
        kinds = [
            o["kind"] for o in bridge.outstanding_requests[rid]["params"]["options"]
        ]
        self.assertEqual(kinds, ["allow_once", "reject_once"])

        task = asyncio.create_task(
            bridge.answer_agent_request(
                rid,
                {"outcome": {"outcome": "selected", "optionId": "allow-once"}},
                "telegram",
            )
        )
        await asyncio.sleep(0)
        last = writes(writer)[-1]
        self.assertEqual(last["method"], "callback/result")
        self.assertEqual(last["params"]["sessionId"], "native")
        self.assertEqual(last["params"]["result"]["callbackId"], "c1")
        self.assertEqual(
            last["params"]["result"]["output"],
            {"type": "approval", "decision": {"type": "approve"}},
        )
        wire_id = last["id"]
        self.assertTrue(wire_id.startswith("baton:"))
        # no verdict stamp until Vibe confirms delivery
        self.assertFalse(
            any(
                c.args[0].get("type") == "agent_resolved"
                for c in ws.send_json.call_args_list
            )
        )

        self.assertTrue(
            await bridge.handle_app_message(
                {"id": wire_id, "result": {"accepted": True}}
            )
        )
        self.assertTrue(await asyncio.wait_for(task, 1))
        resolved = [
            c.args[0]
            for c in ws.send_json.call_args_list
            if c.args[0].get("type") == "agent_resolved"
        ]
        self.assertEqual(resolved[-1]["decision"], "approved")
        self.assertEqual(resolved[-1]["source"], "telegram")

    async def test_rejected_delivery_stamps_unavailable(self):
        bridge, writer, ws = make_bridge()
        frame = json.loads(json.dumps(FRAME))
        self.assertTrue(await bridge.handle_app_message(frame))
        rid = next(iter(bridge.outstanding_requests))

        task = asyncio.create_task(
            bridge.answer_agent_request(
                rid,
                {"outcome": {"outcome": "cancelled"}},
                "telegram",
            )
        )
        await asyncio.sleep(0)
        wire_id = writes(writer)[-1]["id"]
        self.assertTrue(
            await bridge.handle_app_message(
                {"id": wire_id, "error": {"message": "rejected"}}
            )
        )
        with self.assertLogs("baton", level="ERROR"):
            self.assertTrue(await asyncio.wait_for(task, 1))
        resolved = [
            c.args[0]
            for c in ws.send_json.call_args_list
            if c.args[0].get("type") == "agent_resolved"
        ]
        self.assertEqual(resolved[-1]["decision"], "unavailable")
        self.assertNotEqual(resolved[-1]["decision"], "approved")

    async def test_passthrough_without_observer_or_for_non_approval(self):
        writer = Mock()
        writer.drain = AsyncMock()
        bridge = native_cli.NativeBridge(writer)
        frame = json.loads(json.dumps(FRAME))
        self.assertFalse(await bridge.handle_app_message(frame))
        writer.write.assert_not_called()
        self.assertFalse(bridge.outstanding_requests)

        ws = Mock()
        ws.send_json = AsyncMock()
        bridge.clients.add(ws)
        frame["params"]["callback"]["detail"]["kind"] = "user_input"
        self.assertFalse(await bridge.handle_app_message(frame))
        writer.write.assert_not_called()
        self.assertFalse(bridge.outstanding_requests)

    async def test_turn_completed_cancels_mirrored_card(self):
        bridge, writer, ws = make_bridge()
        frame = json.loads(json.dumps(FRAME))
        self.assertTrue(await bridge.handle_app_message(frame))
        self.assertTrue(bridge.outstanding_requests)
        writes_before = writer.write.call_count

        consumed = await bridge.handle_app_message(
            {
                "method": "turn/completed",
                "params": {"sessionId": "native", "turn": {"id": "t1"}},
            }
        )
        self.assertFalse(consumed)  # turn notification still reaches the TUI
        self.assertFalse(bridge.outstanding_requests)
        self.assertFalse(bridge.pending_agent_requests)
        self.assertFalse(bridge.callbacks)
        self.assertEqual(writer.write.call_count, writes_before)
        resolved = [
            c.args[0]
            for c in ws.send_json.call_args_list
            if c.args[0].get("type") == "agent_resolved"
        ]
        self.assertEqual(resolved[-1]["decision"], "cancelled")
        self.assertEqual(resolved[-1]["source"], "server")

    def approval_frame(self, command):
        frame = json.loads(json.dumps(FRAME))
        frame["params"]["callback"]["detail"]["effect"]["input"] = {"command": command}
        return frame

    async def test_safe_shell_auto_approves_without_card(self):
        bridge, writer, ws = make_bridge()
        task = asyncio.create_task(
            bridge.handle_app_message(self.approval_frame("ls -la | head"))
        )
        for _ in range(5):
            await asyncio.sleep(0)
        sent = writes(writer)
        self.assertEqual(sent[0]["id"], 17)
        self.assertTrue(sent[1]["id"].startswith("baton:"))
        self.assertEqual(sent[1]["method"], "callback/result")
        self.assertEqual(
            sent[1]["params"]["result"]["output"],
            {"type": "approval", "decision": {"type": "approve"}},
        )
        self.assertFalse(bridge.outstanding_requests)

        self.assertTrue(
            await bridge.handle_app_message(
                {"id": sent[1]["id"], "result": {"accepted": True}}
            )
        )
        self.assertTrue(await asyncio.wait_for(task, 1))
        resolved = [
            c.args[0]
            for c in ws.send_json.call_args_list
            if c.args[0].get("type") == "agent_resolved"
        ]
        self.assertFalse(resolved)
        auto = [
            c.args[0]
            for c in ws.send_json.call_args_list
            if c.args[0].get("type") == "auto_decided"
        ]
        self.assertEqual(auto[-1]["rule"], "native-safe-shell")

    async def test_destructive_and_sensitive_shell_still_card(self):
        for command in ("rm -rf /tmp/x", "cat ~/.ssh/id_rsa"):
            bridge, writer, _ws = make_bridge()
            frame = self.approval_frame(command)
            self.assertTrue(await bridge.handle_app_message(frame))
            self.assertEqual(len(writes(writer)), 1)  # ack only, no result yet
            self.assertEqual(len(bridge.outstanding_requests), 1)
            rid = next(iter(bridge.outstanding_requests))
            self.assertEqual(
                [
                    o["optionId"]
                    for o in bridge.outstanding_requests[rid]["params"]["options"]
                ],
                ["allow-once", "deny"],
            )


if __name__ == "__main__":
    unittest.main()
