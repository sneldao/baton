"""Offline boundary tests: QR auth on the server, operator auth in the bridge.

No network, no Telegram, no vibe-acp — stdlib unittest only.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import make_mocked_request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import server  # noqa: E402
from telegram_bridge import Telegram  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
