import asyncio
import html
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, time as clock_time, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
LOG = logging.getLogger("daily-digest")
# HTTP client request URLs contain the bot token. Never write them to logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
OWNER_CHAT_ID = int(os.environ["OWNER_CHAT_ID"])
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "20"))
DIGEST_MINUTE = int(os.getenv("DIGEST_MINUTE", "30"))
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "digest.sqlite3"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/128.0 Safari/537.36"
)


@dataclass(frozen=True)
class Post:
    channel: str
    message_id: int
    published_at: datetime
    text: str

    @property
    def url(self) -> str:
        return f"https://t.me/{self.channel}/{self.message_id}"


def db_connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS channels (
                username TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS digest_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );

            INSERT OR IGNORE INTO chat_messages(chat_id, message_id)
            SELECT chat_id, message_id FROM digest_messages;
            """
        )


def normalize_username(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^https?://(?:www\.)?t\.me/(?:s/)?", "", value, flags=re.I)
    value = value.split("?", 1)[0].split("/", 1)[0].lstrip("@")
    if not USERNAME_RE.fullmatch(value):
        raise ValueError("Некорректное имя публичного канала")
    return value


def list_channels() -> list[str]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT username FROM channels WHERE enabled = 1 ORDER BY added_at, username"
        ).fetchall()
    return [row["username"] for row in rows]


def add_channel(username: str) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO channels(username, enabled, added_at)
            VALUES (?, 1, ?)
            ON CONFLICT(username) DO UPDATE SET enabled = 1
            """,
            (username, datetime.now(timezone.utc).isoformat()),
        )


def disable_channel(username: str) -> bool:
    with db_connect() as conn:
        result = conn.execute(
            "UPDATE channels SET enabled = 0 WHERE username = ? AND enabled = 1",
            (username,),
        )
    return result.rowcount > 0


def previous_message_ids(chat_id: int) -> list[int]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT message_id FROM digest_messages WHERE chat_id = ? ORDER BY message_id",
            (chat_id,),
        ).fetchall()
    return [row["message_id"] for row in rows]


def replace_message_ids(chat_id: int, message_ids: Iterable[int]) -> None:
    with db_connect() as conn:
        conn.execute("DELETE FROM digest_messages WHERE chat_id = ?", (chat_id,))
        conn.executemany(
            "INSERT INTO digest_messages(chat_id, message_id) VALUES (?, ?)",
            [(chat_id, message_id) for message_id in message_ids],
        )



def remember_chat_message(chat_id: int, message_id: int) -> None:
    if int(chat_id) != OWNER_CHAT_ID:
        return
    with db_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO chat_messages(chat_id, message_id) VALUES (?, ?)",
            (int(chat_id), int(message_id)),
        )


def tracked_chat_message_ids(chat_id: int) -> list[int]:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT message_id FROM chat_messages WHERE chat_id = ? ORDER BY message_id",
            (chat_id,),
        ).fetchall()
    return [row["message_id"] for row in rows]


def forget_chat_message(chat_id: int, message_id: int) -> None:
    with db_connect() as conn:
        conn.execute(
            "DELETE FROM chat_messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )


async def reply_text_tracked(message, *args, **kwargs):
    result = await message.reply_text(*args, **kwargs)
    remember_chat_message(result.chat_id, result.message_id)
    return result


async def track_owner_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return
    if message.chat_id != OWNER_CHAT_ID:
        return
    remember_chat_message(message.chat_id, message.message_id)


async def delete_all_tracked_messages(application: Application) -> None:
    message_ids = tracked_chat_message_ids(OWNER_CHAT_ID)

    if not message_ids:
        LOG.info("Full chat cleanup: nothing to delete")
        return

    LOG.info("Full chat cleanup: %s messages", len(message_ids))

    deleted = 0

    # Сначала новые сообщения, потом старые.
    for message_id in reversed(message_ids):
        handled = False

        for attempt in range(1, 4):
            try:
                await application.bot.delete_message(
                    chat_id=OWNER_CHAT_ID,
                    message_id=message_id,
                )
                handled = True
                deleted += 1
                break

            except BadRequest as exc:
                # Уже удалено / слишком старое / недоступно.
                # Повторять такой ID в будущем бессмысленно.
                LOG.info(
                    "Could not delete chat message %s: %s",
                    message_id,
                    exc,
                )
                handled = True
                break

            except (TimedOut, NetworkError) as exc:
                LOG.warning(
                    "Delete %s failed attempt %s/3: %s",
                    message_id,
                    attempt,
                    exc,
                )
                if attempt < 3:
                    await asyncio.sleep(attempt * 2)

        if handled:
            forget_chat_message(OWNER_CHAT_ID, message_id)

        await asyncio.sleep(0.08)

    LOG.info(
        "Full chat cleanup finished: deleted %s of %s tracked messages",
        deleted,
        len(message_ids),
    )


def fetch_page(username: str, before: int | None = None) -> BeautifulSoup:
    url = f"https://t.me/s/{quote(username)}"
    params = {"before": before} if before else None
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
        timeout=20,
    )
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def scrape_channel(username: str, since: datetime, max_pages: int = 12) -> list[Post]:
    posts: dict[int, Post] = {}
    before: int | None = None

    for _ in range(max_pages):
        soup = fetch_page(username, before)
        page_ids: list[int] = []
        page_dates: list[datetime] = []

        for node in soup.select("div.tgme_widget_message[data-post]"):
            data_post = node.get("data-post", "")
            try:
                _, raw_id = data_post.rsplit("/", 1)
                message_id = int(raw_id)
            except (ValueError, TypeError):
                continue

            time_node = node.select_one("time[datetime]")
            if not time_node or not time_node.get("datetime"):
                continue
            try:
                published = datetime.fromisoformat(
                    time_node["datetime"].replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except ValueError:
                continue

            text_node = node.select_one(".tgme_widget_message_text")
            text = text_node.get_text("\n", strip=True) if text_node else ""
            if not text:
                text = "Медиа без текстовой подписи"

            page_ids.append(message_id)
            page_dates.append(published)
            if published >= since:
                posts[message_id] = Post(username, message_id, published, text)

        if not page_ids or not page_dates or min(page_dates) < since:
            break
        next_before = min(page_ids)
        if before == next_before:
            break
        before = next_before

    return sorted(posts.values(), key=lambda post: (post.published_at, post.message_id))


def channel_exists(username: str) -> bool:
    soup = fetch_page(username)
    return bool(
        soup.select_one(".tgme_channel_info_header_title")
        or soup.select_one("div.tgme_widget_message[data-post]")
    )


def is_owner(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.id == OWNER_CHAT_ID)


async def reject_non_owner(update: Update) -> None:
    if update.effective_message:
        await reply_text_tracked(update.effective_message,"Этот бот настроен как личный.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_non_owner(update)
        return
    await reply_text_tracked(update.effective_message,
        "Daily Digest запущен.\n\n"
        "/add @channel — добавить публичный канал\n"
        "/list — показать каналы\n"
        "/remove @channel — убрать канал\n"
        "/digest — получить выпуск сейчас\n"
        "/status — проверить расписание"
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_non_owner(update)
        return
    if not context.args:
        await reply_text_tracked(update.effective_message,"Пример: /add @channel_name")
        return
    try:
        username = normalize_username(context.args[0])
        exists = await asyncio.to_thread(channel_exists, username)
    except (ValueError, requests.RequestException) as exc:
        LOG.warning("Channel validation failed: %s", exc)
        await reply_text_tracked(update.effective_message,
            "Не удалось открыть канал. Проверь, что он публичный и ссылка верная."
        )
        return
    if not exists:
        await reply_text_tracked(update.effective_message,
            "Публичный канал не найден. Проверь его имя."
        )
        return
    add_channel(username)
    await reply_text_tracked(update.effective_message,f"Добавлен: @{username}")


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_non_owner(update)
        return
    channels = list_channels()
    if not channels:
        await reply_text_tracked(update.effective_message,"Список пока пуст. Используй /add @channel")
        return
    await reply_text_tracked(update.effective_message,
        "Каналы:\n" + "\n".join(f"• @{name}" for name in channels)
    )


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_non_owner(update)
        return
    if not context.args:
        await reply_text_tracked(update.effective_message,"Пример: /remove @channel_name")
        return
    try:
        username = normalize_username(context.args[0])
    except ValueError:
        await reply_text_tracked(update.effective_message,"Некорректное имя канала.")
        return
    removed = disable_channel(username)
    await reply_text_tracked(update.effective_message,
        f"Канал @{username} отключён." if removed else "Такого активного канала нет."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_non_owner(update)
        return
    await reply_text_tracked(update.effective_message,
        f"Работаю. Каналов: {len(list_channels())}.\n"
        f"Ежедневный выпуск: {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d} ({TZ.key}).\n"
        f"Окно выпуска: последние {LOOKBACK_HOURS} ч."
    )


async def delete_previous_digest(application: Application) -> None:
    for message_id in previous_message_ids(OWNER_CHAT_ID):
        try:
            await application.bot.delete_message(OWNER_CHAT_ID, message_id)
        except BadRequest as exc:
            LOG.info("Could not delete old message %s: %s", message_id, exc)
    replace_message_ids(OWNER_CHAT_ID, [])


def format_post(post: Post) -> str:
    local_time = post.published_at.astimezone(TZ).strftime("%H:%M")
    body = html.escape(post.text)
    if len(body) > 3000:
        body = body[:2997] + "…"
    return f"<b>{local_time}</b>\n{body}\n<a href=\"{post.url}\">Открыть пост</a>"



async def send_message_retry(bot, *args, retries: int = 4, **kwargs):
    """Send a Telegram message with retries on temporary network failures."""
    delay = 2

    for attempt in range(1, retries + 1):
        try:
            message = await bot.send_message(*args, **kwargs)
            remember_chat_message(message.chat_id, message.message_id)
            return message
        except (TimedOut, NetworkError) as exc:
            if attempt == retries:
                LOG.error(
                    "Telegram message failed after %s attempts: %s",
                    retries,
                    exc,
                )
                return None

            LOG.warning(
                "Telegram send failed attempt %s/%s: %s. Retry in %ss",
                attempt,
                retries,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
            delay *= 2


async def run_digest(application: Application, lookback_hours: int | None = None) -> None:
    channels = list_channels()
    if not channels:
        await send_message_retry(application.bot,
            OWNER_CHAT_ID, "Список каналов пуст. Добавь первый командой /add @channel"
        )
        return

    sent_ids: list[int] = []
    now = datetime.now(timezone.utc)
    hours = lookback_hours or LOOKBACK_HOURS
    since = now - timedelta(hours=hours)
    intro = await send_message_retry(application.bot,
        OWNER_CHAT_ID,
        f"🗞 Дайджест за последние {hours} ч.\n"
        f"{now.astimezone(TZ).strftime('%d.%m.%Y %H:%M')}",
    )
    if intro is not None:
        sent_ids.append(intro.message_id)

    for username in channels:
        try:
            posts = await asyncio.to_thread(scrape_channel, username, since)
        except Exception as exc:
            LOG.exception("Failed to scrape @%s", username)
            message = await send_message_retry(application.bot,
                OWNER_CHAT_ID, f"⚠️ Не удалось прочитать @{username}: {exc}"
            )
            if message is not None:
                sent_ids.append(message.message_id)
            continue

        header = await send_message_retry(application.bot,
            OWNER_CHAT_ID,
            f"<b>Канал @{html.escape(username)}</b> — публикаций: {len(posts)}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        if header is not None:
            sent_ids.append(header.message_id)

        for post in posts:
            message = await send_message_retry(application.bot,
                OWNER_CHAT_ID,
                format_post(post),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            if message is not None:
                sent_ids.append(message.message_id)

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚫 Отписаться", callback_data=f"unsub:{username}")]]
        )
        footer = await send_message_retry(application.bot,
            OWNER_CHAT_ID,
            f"Конец блока @{username}",
            reply_markup=keyboard,
        )
        if footer is not None:
            sent_ids.append(footer.message_id)
        await asyncio.sleep(0.4)

    replace_message_ids(OWNER_CHAT_ID, sent_ids)


async def digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await reject_non_owner(update)
        return
    await reply_text_tracked(update.effective_message,"Собираю свежий выпуск…")
    await run_digest(context.application)


async def unsubscribe_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query:
        return
    if query.from_user.id != OWNER_CHAT_ID:
        await query.answer("Недоступно", show_alert=True)
        return
    await query.answer()
    raw = query.data or ""
    try:
        username = normalize_username(raw.removeprefix("unsub:"))
    except ValueError:
        return
    removed = disable_channel(username)
    if removed:
        await query.edit_message_text(f"Канал @{username} отключён.")
    else:
        await query.edit_message_text(f"Канал @{username} уже отключён.")


async def scheduled_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    await delete_all_tracked_messages(context.application)
    await run_digest(context.application)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOG.exception("Unhandled update error", exc_info=context.error)


async def post_init(application: Application) -> None:
    application.job_queue.run_daily(
        scheduled_digest,
        time=clock_time(DIGEST_HOUR, DIGEST_MINUTE, tzinfo=TZ),
        name="daily-digest",
    )
    bot = await application.bot.get_me()
    LOG.info(
        "Started @%s for owner %s; schedule %02d:%02d %s",
        bot.username,
        OWNER_CHAT_ID,
        DIGEST_HOUR,
        DIGEST_MINUTE,
        TZ.key,
    )


def main() -> None:
    init_db()
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .post_init(post_init)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("digest", digest_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CallbackQueryHandler(unsubscribe_callback, pattern=r"^unsub:"))
    application.add_handler(MessageHandler(filters.ALL, track_owner_message), group=1)
    application.add_error_handler(error_handler)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
