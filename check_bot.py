"""چکاپ روزانهٔ ربات: منابع، پارس لینک‌ها، روبیکا، کانال، زنده‌بودن ربات و لاگ خطاها.

اجرا:
    .venv/bin/python check_bot.py             # گزارش در ترمینال
    .venv/bin/python check_bot.py --notify    # گزارش هم به چت مدیر در روبیکا برود

کد خروج ۱ اگر هر تستی رد شود (برای کرون یا Task Scheduler).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

import source

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "rubika_bot.log")
HEARTBEAT_FILE = os.path.join(BASE_DIR, ".rubika_heartbeat")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

TEST_QUERY = "matrix"  # عنوانی که در هر دو سایت قطعاً وجود دارد
HEARTBEAT_MAX_AGE = 180  # ثانیه

checks: list[tuple[bool, str, str]] = []


def add(ok: bool, name: str, detail: str = "") -> None:
    checks.append((ok, name, detail))


def _check_source(source_id: str, label: str) -> None:
    try:
        results = source.search_in(source_id, TEST_QUERY, 1)
    except source.SourceError as exc:
        add(False, label, f"خطای جستجو: {exc}")
        return
    if not results:
        add(False, label, f"برای «{TEST_QUERY}» نتیجه‌ای برنگشت")
        return
    try:
        movie = source.fetch_movie(results[0].link)
    except source.SourceError as exc:
        add(False, label, f"خطا در خواندن صفحه: {exc}")
        return
    n = len(movie.all_links)
    add(
        bool(n),
        label,
        f"{n} لینک از «{source.short_title(movie.title, 35)}»"
        if n
        else "صفحه باز شد ولی لینکی پارس نشد",
    )


def _check_api():
    """توکن روبیکا را تست می‌کند و آبجکت API را برمی‌گرداند."""
    token = os.getenv("RUBIKA_BOT_TOKEN", "").strip()
    if not token:
        add(False, "توکن روبیکا", "RUBIKA_BOT_TOKEN در .env نیست")
        return None
    import rubika_bot as rb

    api = rb.RubikaAPI(token)
    try:
        res = api._call("getMe", {}, quiet=True)
    except Exception as exc:  # noqa: BLE001 - هر خطایی یعنی وضعیت خراب است
        add(False, "ربات روبیکا", f"getMe ناموفق: {exc}")
        return None
    bot = (res or {}).get("bot") or {}
    if not bot.get("username"):
        add(False, "ربات روبیکا", "پاسخ getMe نامعتبر بود")
        return None
    add(True, "ربات روبیکا", f"@{bot['username']}")
    return api


def _check_channel(api) -> None:
    if api is None:
        add(False, "کانال اجباری", "بدون اتصال به روبیکا قابل تست نیست")
        return
    import rubika_bot as rb

    cid = rb.channel_id(api)
    if cid:
        add(True, "کانال اجباری", f"@{rb.CHANNEL_USERNAME} شناسایی شد")
    else:
        add(False, "کانال اجباری", "کانال پیدا نشد یا ربات ادمین نیست")


def _check_heartbeat() -> None:
    try:
        with open(HEARTBEAT_FILE, encoding="utf-8") as f:
            beat = int(f.read().strip())
    except (OSError, ValueError):
        add(False, "زنده‌بودن ربات", "ضربان قلب ثبت نشده — ربات اجرا نشده است")
        return
    age = int(time.time()) - beat
    if age <= HEARTBEAT_MAX_AGE:
        add(True, "زنده‌بودن ربات", f"آخرین فعالیت {age} ثانیه پیش")
    else:
        add(
            False,
            "زنده‌بودن ربات",
            f"از {age // 60} دقیقه پیش پاسخ نداده — احتمالاً خاموش است",
        )


def _check_log() -> None:
    if not os.path.exists(LOG_FILE):
        add(True, "خطاهای لاگ", "لاگی وجود ندارد")
        return
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as exc:
        add(False, "خطاهای لاگ", f"خواندن لاگ ناموفق: {exc}")
        return
    errors = len(re.findall(r"Traceback|error handling update", text))
    if errors:
        add(False, "خطاهای لاگ", f"{errors} مورد خطا در rubika_bot.log")
    else:
        add(True, "خطاهای لاگ", "بدون Traceback")


def _report() -> tuple[bool, str]:
    passed = sum(1 for ok, _, _ in checks if ok)
    total = len(checks)
    lines = [f"🩺 چکاپ ربات — {datetime.now():%Y-%m-%d %H:%M}", ""]
    for ok, name, detail in checks:
        mark = "✅" if ok else "❌"
        lines.append(f"{mark} {name}" + (f": {detail}" if detail else ""))
    lines.append("")
    lines.append(f"نتیجه: {passed} از {total} تست موفق")
    return passed == total, "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="چکاپ روزانهٔ ربات")
    parser.add_argument(
        "--notify", action="store_true", help="ارسال گزارش به چت مدیر در روبیکا"
    )
    args = parser.parse_args()

    _check_source(source.SOURCE_MYF2MS, "منبع اصلی (فیلم ۲ مدیا)")
    _check_source(source.SOURCE_DONYAYE, "منبع دوم (دنیای سریال)")
    api = _check_api()
    _check_channel(api)
    _check_heartbeat()
    _check_log()

    ok, text = _report()
    print(text)

    if args.notify and api is not None and ADMIN_CHAT_ID:
        try:
            api.send_message(ADMIN_CHAT_ID, text)
        except Exception as exc:  # noqa: BLE001
            print(f"ارسال گزارش به مدیر ناموفق بود: {exc}", file=sys.stderr)
            ok = False

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
