"""Synthetic HTML tests: no Telegram credentials or network required."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from bs4 import BeautifulSoup

from media_support import MediaItem, _cdn_url, deliver_media, extract_media


def read_media(markup):
    return extract_media(
        BeautifulSoup('<div data-post="channel/1">' + markup + "</div>", "html.parser")
        .select_one("div[data-post]")
    )


class HtmlMediaTests(unittest.TestCase):
    def test_photo_not_avatar_or_link_preview(self):
        items = read_media('''
          <img class="avatar" src="https://cdn1.telesco.pe/avatar.jpg">
          <a class="tgme_widget_message_photo_wrap"
             style="background-image:url('https://cdn2.telesco.pe/file/photo.jpg')"></a>
          <img class="link_preview" src="https://cdn2.telesco.pe/preview.jpg">''')
        self.assertEqual(items, (MediaItem("photo", "https://cdn2.telesco.pe/file/photo.jpg"),))

    def test_photo_album_and_deduplication(self):
        markup = "".join(
            '<a class="tgme_widget_message_photo_wrap" '
            'style="background-image:url(https://cdn1.telesco.pe/%s.jpg)"></a>' % i
            for i in (1, 2, 3, 2)
        )
        self.assertEqual(len(read_media(markup)), 3)

    def test_video(self):
        items = read_media('''
           <a class="tgme_widget_message_video_player">
             <video src="https://cdn1.telesco.pe/video.mp4"></video>
           </a>''')
        self.assertEqual(items, (MediaItem("video", "https://cdn1.telesco.pe/video.mp4"),))

    def test_video_thumbnail_if_source_is_hidden(self):
        items = read_media('''
           <a class="tgme_widget_message_video_player">
             <i class="tgme_widget_message_video_thumb"
                style="background-image: url('//cdn1.telesco.pe/thumbnail.jpg')"></i>
           </a>''')
        self.assertEqual(items, (MediaItem("preview", "https://cdn1.telesco.pe/thumbnail.jpg"),))

    def test_missing_media(self):
        self.assertEqual(read_media("<p>plain text</p>"), ())

    def test_untrusted_source_blocked(self):
        self.assertIsNone(_cdn_url("http://cdn1.telesco.pe/photo.jpg"))
        self.assertIsNone(_cdn_url("https://cdn1.telesco.pe.evil.invalid/photo.jpg"))
        self.assertIsNone(_cdn_url("https://localhost/photo.jpg"))
        self.assertEqual(read_media(
            '<a class="tgme_widget_message_photo_wrap" '
            'style="background-image:url(https://evil.invalid/photo.jpg)"></a>'
        ), ())


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_delivery_tracks_sent_id(self):
        class Bot:
            async def send_photo(self, **kwargs):
                self.photo = kwargs
                return SimpleNamespace(message_id=11)
        bot = Bot()
        with patch("media_support.download_media", return_value=b"image bytes"):
            ids = await deliver_media(bot, 123, (
                MediaItem("photo", "https://cdn1.telesco.pe/1.jpg"),))
        self.assertEqual(ids, [11])
        self.assertEqual(bot.photo["chat_id"], 123)

    async def test_album_is_grouped(self):
        class Bot:
            async def send_media_group(self, **kwargs):
                self.group = kwargs
                return [SimpleNamespace(message_id=21), SimpleNamespace(message_id=22)]
        bot = Bot()
        with patch("media_support.download_media", return_value=b"image bytes"):
            ids = await deliver_media(bot, 123, (
                MediaItem("photo", "https://cdn1.telesco.pe/1.jpg"),
                MediaItem("photo", "https://cdn1.telesco.pe/2.jpg"),
            ))
        self.assertEqual(ids, [21, 22])
        self.assertEqual(len(bot.group["media"]), 2)

    async def test_failed_image_does_not_block_others(self):
        class Bot:
            async def send_photo(self, **kwargs):
                return SimpleNamespace(message_id=33)
        def retrieve(item):
            if item.url.endswith("/1.jpg"):
                raise ValueError("expired media")
            return b"image bytes"
        with patch("media_support.download_media", side_effect=retrieve):
            ids = await deliver_media(Bot(), 123, (
                MediaItem("photo", "https://cdn1.telesco.pe/1.jpg"),
                MediaItem("photo", "https://cdn1.telesco.pe/2.jpg"),
            ))
        self.assertEqual(ids, [33])


if __name__ == "__main__":
    unittest.main()
