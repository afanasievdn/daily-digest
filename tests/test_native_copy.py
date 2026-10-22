"""Test Telegram native media copying without credentials or network."""
import unittest
from types import SimpleNamespace

from telegram.error import BadRequest, Forbidden
from media_support import copy_public_media


class NativeCopyTests(unittest.IsolatedAsyncioTestCase):
    async def test_copies_original_media_without_repeating_caption(self):
        class Bot:
            async def copy_message(self, **kwargs):
                self.last = kwargs
                return SimpleNamespace(message_id=44)
        bot = Bot()
        self.assertEqual(await copy_public_media(bot, 123, "public_channel", 456), 44)
        self.assertEqual(bot.last, {
            "chat_id": 123,
            "from_chat_id": "@public_channel",
            "message_id": 456,
            "caption": "",
        })

    async def test_bad_request_falls_back(self):
        class Bot:
            async def copy_message(self, **kwargs):
                raise BadRequest("chat not found")
        self.assertIsNone(await copy_public_media(Bot(), 123, "testchannel", 11))

    async def test_forbidden_falls_back(self):
        class Bot:
            async def copy_message(self, **kwargs):
                raise Forbidden("bot is not a member")
        self.assertIsNone(await copy_public_media(Bot(), 123, "testchannel", 11))


if __name__ == "__main__":
    unittest.main()
