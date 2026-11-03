"""آمار استفاده از ربات: چند کاربر، چند جستجو، وضعیت امروز.

این فایل‌ها روی هاست موقت (GitHub Actions) با هر اجرا از بین می‌روند، پس آمار در یک
فایل JSON در ریشهٔ پروژه نگه داشته می‌شود و همراه بقیهٔ وضعیت در ریپو کامیت می‌شود.

حریم خصوصی: شناسهٔ کاربران هرگز ذخیره نمی‌شود؛ فقط هش کوتاه آن (SHA-256) نگه داشته
می‌شود تا هم شمارش یکتا ممکن باشد و هم چیزی از کاربران در ریپوی عمومی لو نرود.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone

STATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot_stats.json")

_lock = threading.Lock()
_data: dict | None = None
_last_write = 0.0
WRITE_EVERY = 30.0  # ثانیه؛ برای اینکه هر پیام یک نوشتن دیسک نسازد


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _key(user_id: str) -> str:
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:12]


def _load() -> dict:
    global _data
    if _data is not None:
        return _data
    raw: dict = {}
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            raw = loaded
    except (OSError, ValueError):
        raw = {}
    raw.setdefault("users", {})
    raw.setdefault("searches", 0)
    raw.setdefault("days", {})
    raw.setdefault("started", _day(time.time()))
    _data = raw
    return _data


def save(force: bool = False) -> None:
    """آمار را روی دیسک می‌نویسد (حداکثر هر WRITE_EVERY ثانیه، مگر force)."""
    global _last_write
    if _data is None:
        return
    now = time.time()
    if not force and now - _last_write < WRITE_EVERY:
        return
    _last_write = now
    tmp = STATS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, STATS_FILE)
    except OSError:
        pass


def _bump_day(day: str, field: str, amount: int = 1) -> None:
    days = _data["days"]
    bucket = days.setdefault(day, {})
    bucket[field] = int(bucket.get(field, 0)) + amount


def track_user(user_id: str) -> bool:
    """هر کاربری که با ربات کار می‌کند را ثبت می‌کند. True یعنی کاربر تازه است."""
    if not user_id:
        return False
    with _lock:
        data = _load()
        key = _key(user_id)
        now = time.time()
        entry = data["users"].get(key)
        is_new = entry is None
        if is_new:
            entry = {"first": int(now), "last": int(now), "n": 0}
            data["users"][key] = entry
            _bump_day(_day(now), "new_users")
            _bump_day(_day(now), "active")
        else:
            if _day(entry.get("last", 0)) != _day(now):
                _bump_day(_day(now), "active")
            entry["last"] = int(now)
        entry["n"] = int(entry.get("n", 0)) + 1
        save()
        return is_new


def track_search() -> None:
    """هر جستجوی موفق/شروع‌شده را می‌شمارد."""
    with _lock:
        data = _load()
        now = time.time()
        data["searches"] = int(data.get("searches", 0)) + 1
        _bump_day(_day(now), "searches")
        save()


def summary() -> dict:
    with _lock:
        data = _load()
        today = _today()
        bucket = data["days"].get(today, {})
        return {
            "started": data.get("started", today),
            "users_total": len(data["users"]),
            "users_new_today": int(bucket.get("new_users", 0)),
            "users_active_today": int(bucket.get("active", 0)),
            "searches_total": int(data.get("searches", 0)),
            "searches_today": int(bucket.get("searches", 0)),
        }


def report_text() -> str:
    """گزارش فارسی برای فرستادن به مدیر."""
    s = summary()
    return (
        "📊 آمار ربات\n\n"
        f"👥 کل کاربران: {s['users_total']}\n"
        f"🆕 کاربر تازه امروز: {s['users_new_today']}\n"
        f"👤 فعال امروز: {s['users_active_today']}\n\n"
        f"🔍 کل جستجوها: {s['searches_total']}\n"
        f"📅 جستجوهای امروز: {s['searches_today']}\n\n"
        f"🗓 شروع شمارش: {s['started']}\n"
        "ℹ️ آمار هر ۳۰ ثانیه یک بار ذخیره می‌شود و پایان هر اجرا در ریپو کامیت می‌شود."
    )
