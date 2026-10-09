"""Extract and forward media from Telegram's public channel HTML preview.

The preview is not an official API: unavailable, expired or oversized media
are skipped while the digest still retains its text and original post link.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlsplit

import requests
from bs4 import Tag
from telegram import InputFile, InputMediaPhoto, InputMediaVideo
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

LOG = logging.getLogger("daily-digest.media")
CSS_URL = re.compile(r"background-image\s*:\s*url\(\s*['\"]?([^'\")]+)", re.I)
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 45 * 1024 * 1024
MAX_POST_BYTES = 90 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/128.0 Safari/537.36"
)


@dataclass(frozen=True)
class MediaItem:
    kind: str  # photo, video, or preview (thumbnail when video is inaccessible)
    url: str


def _cdn_url(raw: str | None) -> str | None:
    """Only retrieve media from official Telegram CDN hosts; no arbitrary URLs."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or not (host.endswith(".telesco.pe") or host.endswith(".telegram-cdn.org"))
    ):
        return None
    return raw


def _background_url(element: Tag | None) -> str | None:
    if element is None:
        return None
    match = CSS_URL.search(element.get("style", ""))
    return _cdn_url(match.group(1)) if match else None


def extract_media(node: Tag) -> tuple[MediaItem, ...]:
    """Parse only post attachments, excluding author avatars and link previews."""
    items: list[MediaItem] = []
    seen: set[str] = set()
    selectors = ".tgme_widget_message_photo_wrap, .tgme_widget_message_video_player"
    for element in node.select(selectors):
        classes = element.get("class", [])
        if "tgme_widget_message_photo_wrap" in classes:
            url = _background_url(element)
            kind = "photo"
        else:
            video = element.select_one("video.tgme_widget_message_video, video[src], video source[src]")
            url = _cdn_url(video.get("src")) if video else None
            kind = "video"
            if not url:
                url = _background_url(element.select_one(".tgme_widget_message_video_thumb"))
                kind = "preview"
        if url and url not in seen:
            items.append(MediaItem(kind, url))
            seen.add(url)
    return tuple(items)


def download_media(item: MediaItem) -> bytes:
    """Bound memory use; do not follow redirects to untrusted hosts."""
    maximum = MAX_VIDEO_BYTES if item.kind == "video" else MAX_PHOTO_BYTES
    with requests.get(
        item.url,
        headers={"User-Agent": USER_AGENT, "Referer": "https://t.me/"},
        timeout=(10, 35),
        stream=True,
        allow_redirects=False,
    ) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > maximum:
            raise ValueError("media exceeds Telegram size limit")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type.startswith("text/") or content_type == "application/json":
            raise ValueError("media URL returned text, not a file")
        buffer = BytesIO()
        for part in response.iter_content(chunk_size=128 * 1024):
            buffer.write(part)
            if buffer.tell() > maximum:
                raise ValueError("media exceeds Telegram size limit")
        if not buffer.tell():
            raise ValueError("empty media")
        return buffer.getvalue()


def _media_object(item: MediaItem, content: bytes):
    if item.kind == "video":
        return InputMediaVideo(media=InputFile(content, filename="video.mp4"))
    return InputMediaPhoto(media=InputFile(content, filename="photo.jpg"))


async def _send_with_retry(callback):
    for attempt in range(3):
        try:
            return await callback()
        except RetryAfter as exc:
            if attempt == 2:
                LOG.warning("Media rate limit: %s", exc)
                return None
            wait = exc.retry_after
            seconds = wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
            await asyncio.sleep(min(seconds, 60) + 0.5)
        except (NetworkError, TimedOut) as exc:
            if attempt == 2:
                LOG.warning("Media delivery failed after retries: %s", exc)
                return None
            await asyncio.sleep(2 ** (attempt + 1))
        except BadRequest as exc:
            LOG.warning("Telegram rejected media: %s", exc)
            return None


async def deliver_media(bot, chat_id: int, items: tuple[MediaItem, ...]) -> list[int]:
    """Send actual files, never only auto-generated link previews.

    Albums are sent as groups of at most 10. A failed group gets individual
    attempts so one incompatible attachment does not hide the rest.
    """
    loaded: list[tuple[MediaItem, bytes]] = []
    used = 0
    for item in items:
        if len(loaded) >= 20:
            LOG.warning("Post has too many attachments; remaining items skipped")
            break
        try:
            data = await asyncio.to_thread(download_media, item)
            if used + len(data) > MAX_POST_BYTES:
                LOG.warning("Post media budget exceeded; item skipped")
                continue
            loaded.append((item, data))
            used += len(data)
        except (requests.RequestException, ValueError, OSError) as exc:
            LOG.warning("Could not retrieve %s media: %s", item.kind, exc)

    message_ids: list[int] = []

    async def send_one(item: MediaItem, content: bytes) -> None:
        if item.kind == "video":
            async def action():
                return await bot.send_video(chat_id=chat_id, video=InputFile(content, filename="video.mp4"))
        else:
            async def action():
                return await bot.send_photo(chat_id=chat_id, photo=InputFile(content, filename="photo.jpg"))
        message = await _send_with_retry(action)
        if message:
            message_ids.append(message.message_id)

    for start in range(0, len(loaded), 10):
        chunk = loaded[start : start + 10]
        if len(chunk) == 1:
            await send_one(*chunk[0])
            continue

        async def send_group():
            return await bot.send_media_group(
                chat_id=chat_id,
                media=[_media_object(item, data) for item, data in chunk],
            )

        group = await _send_with_retry(send_group)
        if group:
            message_ids.extend(message.message_id for message in group)
        else:
            for item, content in chunk:
                await send_one(item, content)
    return message_ids
