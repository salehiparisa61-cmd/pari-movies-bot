"""ربات تلگرام: جستجوی فیلم/سریال و دریافت لینک دانلود از سایت منبع."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import source

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("telegram_bot")

MAX_RESULTS = 6
MAX_LINK_BUTTONS = 60  # سقف دکمه‌های inline تلگرام ۱۰۰ است

WELCOME = (
    "سلام! 👋\n"
    "من ربات جستجوی فیلم و سریال هستم.\n\n"
    "📌 کافیه نام فیلم یا سریال رو برام بفرستی (فارسی یا انگلیسی)،\n"
    "تا لینک‌های دانلودش رو پیدا کنم.\n\n"
    "مثال: ماتریکس یا Interstellar\n\n"
    "🛠 دستورات:\n"
    "/start — شروع\n"
    "/help — راهنما"
)


def _download_markup(movie: source.Movie) -> InlineKeyboardMarkup | None:
    buttons: list[list[InlineKeyboardButton]] = []
    for link in movie.all_links[:MAX_LINK_BUTTONS]:
        label = f"⬇️ {link.quality}"[:64]
        buttons.append([InlineKeyboardButton(label, url=link.url)])
    return InlineKeyboardMarkup(buttons) if buttons else None


def _caption(movie: source.Movie) -> str:
    return source.build_caption(movie)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = (update.effective_message.text or "").strip()
    if not query:
        return
    status = await update.effective_message.reply_text("🔍 دارم جستجو می‌کنم…")
    try:
        results = await asyncio.to_thread(source.search, query, MAX_RESULTS)
    except source.SourceError:
        await status.edit_text(
            "⚠️ ارتباط با سایت منبع برقرار نشد؛ لطفاً کمی بعد دوباره تلاش کن."
        )
        return
    if not results:
        await status.edit_text(
            f"😕 نتیجه‌ای برای «{query}» پیدا نشد.\nعنوان را ساده‌تر یا انگلیسی بنویس."
        )
        return
    context.user_data["results"] = {str(r.id): r.link for r in results}
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(source.short_title(r.title), callback_data=f"m:{r.id}")]
            for r in results
        ]
    )
    label = source.SOURCE_LABELS.get(results[0].source, "")
    where = f" (منبع: {label})" if label else ""
    await status.edit_text(
        f"🔍 نتایج برای «{query}»{where}:\nیکی رو انتخاب کن:",
        reply_markup=keyboard,
    )


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    post_id = query.data.split(":", 1)[1]
    link = context.user_data.get("results", {}).get(post_id)
    msg = query.message
    if not link:
        await msg.edit_text("⌛ این جستجو منقضی شده؛ دوباره نام فیلم رو بفرست.")
        return
    await msg.edit_text("⏳ در حال دریافت لینک‌های دانلود…")
    try:
        movie = await asyncio.to_thread(source.fetch_movie, link, msg.text or "")
    except source.SourceError:
        await msg.edit_text("⚠️ خطا در دریافت صفحهٔ فیلم؛ دوباره تلاش کن.")
        return
    caption = _caption(movie)
    markup = _download_markup(movie)
    sent = False
    if movie.poster_url:
        try:
            await msg.reply_photo(
                photo=movie.poster_url, caption=caption, reply_markup=markup
            )
            await msg.delete()
            sent = True
        except Exception:
            log.warning("ارسال پوستر ناموفق بود", exc_info=True)
    if not sent:
        await msg.edit_text(caption, reply_markup=markup)

    # باقی لینک‌ها (بیش از ظرفیت دکمه‌ها) به‌صورت متن؛ هیچ لینکی به سایت نمی‌رود
    rest = movie.all_links[MAX_LINK_BUTTONS:]
    if rest:
        text = "\n\n".join(f"• {lk.quality}\n{lk.url}" for lk in rest)
        for i in range(0, len(text), 4000):
            await update.effective_chat.send_message(text[i : i + 4000])


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("خطا: TELEGRAM_BOT_TOKEN در فایل .env تنظیم نشده است.", file=sys.stderr)
        sys.exit(1)
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^m:\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("Telegram bot started (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
