"""Digest notice regression tests. No Telegram account or network required."""
import importlib
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from media_support import DeliveryResult, MediaItem

# app.py reads configuration at import time, but tests must not use real credentials.
with patch.dict(os.environ, {"BOT_TOKEN": "testing-only", "OWNER_CHAT_ID": "123"}):
    app = importlib.import_module("app")


def post_with_media(*items):
    return app.Post(
        channel="world_playstation",
        message_id=55856,
        published_at=datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc),
        text="Example video & information",
        media=tuple(items),
    )


class FormatPostTests(TestCase):
    def test_unavailable_video_notice_preserves_post_and_source(self):
        post = post_with_media(MediaItem("preview", ""))
        result = app.format_post(post, video_unavailable=True)
        self.assertIn("Example video &amp; information", result)
        self.assertIn("Видео недоступно", result)
        self.assertIn("https://t.me/world_playstation/55856", result)

    def test_unavailable_photo_notice_keeps_post_and_source(self):
        post = post_with_media(MediaItem("photo", "https://cdn1.telesco.pe/a.jpg"))
        result = app.format_post(post, photo_unavailable=True)
        self.assertIn("Фото недоступно", result)
        self.assertIn("Example video &amp; information", result)
        self.assertIn("https://t.me/world_playstation/55856", result)

    def test_regular_media_has_no_notice(self):
        self.assertNotIn("Видео недоступно", app.format_post(post_with_media()))


class DigestVideoTests(IsolatedAsyncioTestCase):
    async def _run(self, post, delivery, *, copy_result=None):
        message_ids = [1, 2, 3, 4]
        send = AsyncMock(side_effect=[
            SimpleNamespace(message_id=i, chat_id=123) for i in message_ids
        ])
        bot = SimpleNamespace(edit_message_text=AsyncMock())
        application = SimpleNamespace(bot=bot)
        with patch.object(app, "list_channels", return_value=["world_playstation"]), \
             patch.object(app, "scrape_channel", return_value=[post]), \
             patch.object(app, "recover_embedded_videos", side_effect=lambda c, m, items: items), \
             patch.object(app, "recover_embedded_media", side_effect=lambda c, m, items, **kw: items), \
             patch.object(app, "deliver_media", new_callable=AsyncMock, return_value=delivery) as deliver, \
             patch.object(app, "copy_public_media", new_callable=AsyncMock, return_value=copy_result) as copy, \
             patch.object(app, "send_message_retry", send), \
             patch.object(app, "remember_chat_message"), \
             patch.object(app, "replace_message_ids"), \
             patch.object(app.asyncio, "sleep", new_callable=AsyncMock):
            await app.run_digest(application)
        return send, deliver, bot, copy

    async def test_missing_photo_in_html_has_visible_notice(self):
        post = app.Post(
            channel="world_playstation",
            message_id=55856,
            published_at=datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc),
            text="Image expected",
            media=(),
            has_photo=True,
        )
        send, deliver, bot, copy = await self._run(post, DeliveryResult([], 0))
        self.assertNotIn("Фото недоступно", send.call_args_list[2].args[2])
        deliver.assert_not_awaited()
        copy.assert_awaited_once()
        self.assertIn("Фото недоступно", bot.edit_message_text.await_args.kwargs["text"])

    async def test_failed_photo_upload_adds_notice(self):
        post = post_with_media(MediaItem("photo", "https://cdn1.telesco.pe/a.jpg"))
        _, _, bot, _ = await self._run(post, DeliveryResult([], 0, 1))
        bot.edit_message_text.assert_awaited_once()
        self.assertIn("Фото недоступно", bot.edit_message_text.await_args.kwargs["text"])

    async def test_preview_has_notice_and_no_false_image(self):
        post = post_with_media(MediaItem("preview", "https://cdn1.telesco.pe/thumb.jpg"))
        send, deliver, bot, copy = await self._run(post, DeliveryResult([], 1))
        self.assertNotIn("Видео недоступно", send.call_args_list[2].args[2])
        self.assertEqual(deliver.await_count, 1)
        self.assertIn("Видео недоступно", bot.edit_message_text.await_args.kwargs["text"])

    async def test_failed_video_upload_adds_notice_afterwards(self):
        post = post_with_media(MediaItem("video", "https://cdn1.telesco.pe/clip.mp4"))
        send, _, bot, _ = await self._run(post, DeliveryResult([], 1))
        self.assertNotIn("Видео недоступно", send.call_args_list[2].args[2])
        bot.edit_message_text.assert_awaited_once()
        self.assertIn("Видео недоступно", bot.edit_message_text.await_args.kwargs["text"])


    async def test_native_copy_restores_photo_even_without_cdn_url(self):
        post = app.Post(
            channel="world_playstation",
            message_id=55856,
            published_at=datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc),
            text="Photo expected",
            media=(),
            has_photo=True,
        )
        send, deliver, bot, copy = await self._run(
            post, DeliveryResult([], 0), copy_result=99
        )
        copy.assert_awaited_once()
        deliver.assert_not_awaited()
        bot.edit_message_text.assert_not_awaited()
        self.assertNotIn("Фото недоступно", send.call_args_list[2].args[2])

    async def test_native_copy_restores_playable_video_from_thumbnail(self):
        post = post_with_media(MediaItem("preview", "https://cdn1.telesco.pe/thumb.jpg"))
        send, deliver, bot, copy = await self._run(
            post, DeliveryResult([], 1), copy_result=100
        )
        copy.assert_awaited_once()
        deliver.assert_not_awaited()
        bot.edit_message_text.assert_not_awaited()
        self.assertNotIn("Видео недоступно", send.call_args_list[2].args[2])

    async def test_successful_video_does_not_show_false_notice(self):
        post = post_with_media(MediaItem("video", "https://cdn1.telesco.pe/clip.mp4"))
        send, _, bot, _ = await self._run(post, DeliveryResult([42], 0))
        self.assertNotIn("Видео недоступно", send.call_args_list[2].args[2])
        bot.edit_message_text.assert_not_awaited()
