"""ربات روبیکا: جستجوی فیلم/سریال و دریافت لینک دانلود از سایت منبع.

از Bot API رسمی روبیکا استفاده می‌کند:
    POST https://botapi.rubika.ir/v3/{token}/{method}

بدون وابستگی به SDK شخص ثالث؛ فقط requests.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time

from dotenv import load_dotenv
import requests

import source
import stats

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("rubika_bot")

API_BASE = "https://botapi.rubika.ir/v3"
MAX_RESULTS = 6
MAX_TEXT = 4300  # سقف طول یک پیام (محدودیت روبیکا: ۵۰۰۰ کاراکتر و ۱۲۸ خط)
MAX_LINES = 120
POLL_INTERVAL = 0.4  # فاصلهٔ بین درخواست‌های getUpdates (هرچه کمتر، پاسخ سریع‌تر)
THROTTLE = 0.1  # تأخیر قبل از هر ارسال پیام
API_TIMEOUT = 15
MAX_ATTEMPTS = 3
# پیام‌های قدیمی‌تر از این مقدار (ثانیه) جواب داده نمی‌شوند؛ جلوی ارسال انبوهِ
# پیام‌های تکراری بعد از خاموشیِ ربات یا اجرای تازه روی هاست را می‌گیرد.
MAX_UPDATE_AGE = int(os.getenv("MAX_UPDATE_AGE_SECONDS", "600") or 0)


WELCOME = (
    "سلام! 👋\n"
    "من ربات جستجوی فیلم و سریال هستم.\n\n"
    "📌 کافیه نام فیلم یا سریال رو برام بفرستی (فارسی یا انگلیسی)،\n"
    "تا لینک‌های دانلودش رو پیدا کنم.\n\n"
    "مثال: ماتریکس یا Interstellar\n\n"
    "📝 برای انتقاد یا پیشنهاد، دکمهٔ پایین چت رو بزن.\n\n"
    "🛠 دستورات:\n"
    "/start — شروع\n"
    "/help — راهنما\n"
    "/feedback — انتقاد و پیشنهاد"
)

# --------------------------- عضویت اجباری در کانال ------------------------- #
CHANNEL_USERNAME = os.getenv("RUBIKA_CHANNEL", "Pari_Movies").lstrip("@")
CHANNEL_LINK = f"https://rubika.ir/{CHANNEL_USERNAME}"
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
MEMBER_TTL = 600  # هر ۱۰ دقیقه عضویت دوباره چک می‌شود
_MEMBER_STATUSES_OK = {"Creator", "Owner", "Administrator", "Admin", "Member"}

_home_keypad = {
    "rows": [
        {
            "buttons": [
                {"id": "fb", "type": "Simple", "button_text": "📝 انتقاد و پیشنهاد"},
                {"id": "check", "type": "Simple", "button_text": "✅ عضو شدم"},
            ]
        }
    ],
    "resize_keyboard": True,
}

# کش وضعیت عضویت: user_id -> (زمان چک، True/False)
_member_cache: dict[str, tuple[float, bool]] = {}
# چت‌هایی که منتظر متن انتقاد/پیشنهاد هستند
_awaiting_feedback: dict[str, bool] = {}
_channel_id: str | None = None


def channel_id(api: "RubikaAPI") -> str:
    global _channel_id
    if _channel_id:
        return _channel_id
    try:
        info = api._call("getChat", {"chat_id": f"@{CHANNEL_USERNAME}"})
        _channel_id = ((info or {}).get("chat") or {}).get("chat_id") or ""
    except RubikaError:
        _channel_id = ""
    if not _channel_id:
        log.warning("channel chat_id not resolved for @%s", CHANNEL_USERNAME)
    return _channel_id


def is_member(api: "RubikaAPI", user_id: str) -> bool:
    """آیا کاربر عضو کانال است؟ نتیجه ۱۰ دقیقه کش می‌شود."""
    if not user_id:
        return True
    now = time.time()
    hit = _member_cache.get(user_id)
    if hit and now - hit[0] < MEMBER_TTL:
        return hit[1]
    cid = channel_id(api)
    if not cid:
        return True  # اگر کانال قابل بررسی نبود، کاربر را بلاک نکن
    try:
        res = api._call(
            "getChatMember", {"chat_id": cid, "user_id": user_id}, quiet=True
        )
    except RubikaError:
        return True
    status = ((res or {}).get("chat_member") or {}).get("status", "")
    ok = status in _MEMBER_STATUSES_OK
    if not status:
        ok = False  # INVALID_INPUT یعنی عضو نیست
    _member_cache[user_id] = (now, ok)
    return ok


def send_join_prompt(api: "RubikaAPI", chat_id: str) -> None:
    api.send_message(
        chat_id,
        "🔐 برای استفاده از ربات اول باید عضو کانال ما باشی:\n\n"
        f"{CHANNEL_LINK}\n\n"
        "بعد از عضو شدن، دکمهٔ «✅ عضو شدم» رو بزن.",
        chat_rows=_home_keypad["rows"],
    )

# نگاشت «آخرین نتایج جستجوی هر چت» برای پاسخ به دکمه‌ها (در حافظه)
_search_by_id: dict[str, dict[str, str]] = {}
_search_by_title: dict[str, dict[str, str]] = {}
_search_order: dict[str, list[str]] = {}

# ذخیرهٔ offset تا با هر بار اجرا دوباره پیام‌های قدیمی پردازش نشوند
OFFSET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".rubika_offset")


def _load_offset() -> str | None:
    try:
        with open(OFFSET_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _save_offset(offset: str | None) -> None:
    if not offset:
        return
    try:
        with open(OFFSET_FILE, "w", encoding="utf-8") as f:
            f.write(offset)
    except OSError:
        pass


# ضربان قلب: برای اینکه چکاپ روزانه بفهمد ربات واقعاً زنده است
HEARTBEAT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".rubika_heartbeat"
)
_last_beat = 0.0


def heartbeat() -> None:
    global _last_beat
    now = time.time()
    if now - _last_beat < 30:
        return
    _last_beat = now
    try:
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(now)))
    except OSError:
        pass


class RubikaError(RuntimeError):
    """خطا در ارتباط با Bot API روبیکا."""


class RubikaAPI:
    def __init__(self, token: str):
        # توکن در مسیر URL است؛ هرگز URL کامل را لاگ نکن
        self._base = f"{API_BASE}/{token}"
        self._local = threading.local()

    @property
    def _session(self) -> requests.Session:
        """هر ترد Session خودش را دارد؛ Session بین تردها امن نیست."""
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            self._local.session = s
        return s

    def _call(self, method: str, payload: dict, quiet: bool = False) -> dict:
        last_exc: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = self._session.post(
                    f"{self._base}/{method}", json=payload, timeout=API_TIMEOUT
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") == "OK" or "data" in data:
                    return data.get("data", data)
                if not quiet:
                    log.warning("Rubika API error on %s: %s", method, data)
                return data
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                log.warning("retry %s after %s", method, type(exc).__name__)
                time.sleep(1.5 * (attempt + 1))
        raise RubikaError(f"API call failed: {method}") from last_exc

    # ------------------------------------------------------------------ #
    def send_message(
        self,
        chat_id: str,
        text: str,
        rows: list | None = None,
        chat_rows: list | None = None,
    ) -> dict:
        payload: dict = {"chat_id": chat_id, "text": text}
        if rows:
            payload["inline_keypad"] = {"rows": rows}
        if chat_rows:
            # دکمه‌های پایین چت: کلیکشان به‌صورت NewMessage می‌آید (مسیر polling اثبات‌شده)
            payload["chat_keypad"] = {"rows": chat_rows, "resize_keyboard": True}
            payload["chat_keypad_type"] = "New"
        time.sleep(THROTTLE)
        return self._call("sendMessage", payload)

    def get_updates(self, offset: str | None) -> dict:
        payload: dict = {"limit": 50}
        if offset:
            payload["offset_id"] = offset
        return self._call("getUpdates", payload)


# ------------------------------ کیبوردها --------------------------------- #
def result_rows(results: list[source.SearchResult]) -> list:
    return [
        {
            "buttons": [
                {
                    "id": f"m:{r.id}",
                    "type": "Simple",
                    "button_text": source.short_title(r.title, 48),
                }
            ]
        }
        for r in results
    ] + [
        {"buttons": [{"id": "fb", "type": "Simple", "button_text": "📝 انتقاد و پیشنهاد"}]}
    ]


# ------------------------------ منطق ربات -------------------------------- #
def handle_search(api: RubikaAPI, chat_id: str, query: str) -> None:
    log.info("search chat=%s query=%r", chat_id, query[:60])
    try:
        results = source.search(query, MAX_RESULTS)
    except source.SourceError:
        api.send_message(
            chat_id, "⚠️ ارتباط با سایت منبع برقرار نشد؛ لطفاً کمی بعد دوباره تلاش کن."
        )
        return
    if not results:
        api.send_message(
            chat_id,
            f"😕 نتیجه‌ای برای «{query}» پیدا نشد.\nعنوان را ساده‌تر یا انگلیسی بنویس.",
        )
        return
    _search_by_id[chat_id] = {str(r.id): r.link for r in results}
    _search_by_title[chat_id] = {
        source.short_title(r.title, 48).casefold(): r.link for r in results
    }
    _search_order[chat_id] = [str(r.id) for r in results]
    label = source.SOURCE_LABELS.get(results[0].source, "")
    where = f" (منبع: {label})" if label else ""
    api.send_message(
        chat_id,
        f"🔍 نتایج برای «{query}»{where}:\nیکی رو از دکمه‌های پایین انتخاب کن"
        f" یا شماره‌اش رو بفرست (۱ تا {len(results)}):",
        chat_rows=result_rows(results),
    )


def links_chunks(header: str, movie: source.Movie) -> list[str]:
    """همهٔ لینک‌ها را به‌صورت متن (لمس‌کردنی) می‌فرستد؛ در صورت طولانی بودن چند تکه.

    تکه‌ها هم سقف کاراکتر و هم سقف خط روبیکا را رعایت می‌کنند؛ تعداد کمتر تکه
    یعنی ارسال سریع‌تر (هر پیام حدود یک ثانیه طول می‌کشد).
    """
    chunks: list[str] = []
    buf: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        if buf:
            chunks.append("\n".join(buf))
        buf = []
        size = 0

    def add_line(line: str) -> None:
        nonlocal size
        if buf and (size + len(line) + 2 > MAX_TEXT or len(buf) + 1 > MAX_LINES):
            flush()
        buf.append(line)
        size += len(line) + 1

    for line in header.split("\n"):
        add_line(line)
    for name, links in movie.groups:
        add_line("")
        add_line(f"▪️ {name}")
        prev_quality = None
        for link in links:
            # کیفیت تکراری فقط یک‌بار نوشته می‌شود تا پیام‌ها کوتاه‌تر و ارسال سریع‌تر شود
            if link.quality != prev_quality:
                add_line(f"• {link.quality}")
                prev_quality = link.quality
            add_line(link.url)
    flush()
    return chunks


def send_movie(api: RubikaAPI, chat_id: str, link: str) -> None:
    try:
        movie = source.fetch_movie(link)
    except source.SourceError:
        api.send_message(chat_id, "⚠️ خطا در دریافت صفحهٔ فیلم؛ دوباره تلاش کن.")
        return
    header = source.build_caption(movie)
    if not movie.all_links:
        api.send_message(chat_id, header)
        return
    chunks = links_chunks(header, movie)
    for chunk in chunks:
        api.send_message(chat_id, chunk)
    log.info("sent links chat=%s links=%d messages=%d", chat_id, len(movie.all_links), len(chunks))


def handle_selection(
    api: RubikaAPI, chat_id: str, post_id: str, hint: str = ""
) -> None:
    link = _search_by_id.get(chat_id, {}).get(post_id)
    if not link:
        log.info("selection miss chat=%s post_id=%s hint=%r", chat_id, post_id, hint[:40])
        link = _resolve_stale(chat_id, post_id, hint)
    if not link:
        api.send_message(
            chat_id, "⌛ این جستجو منقضی شده؛ دوباره نام فیلم رو بفرست."
        )
        return
    log.info("selection ok chat=%s post_id=%s", chat_id, post_id)
    send_movie(api, chat_id, link)


def _resolve_stale(chat_id: str, post_id: str, hint: str) -> str | None:
    """اگر ربات ری‌استارت شده باشد، با متن دکمه دوباره جستجو می‌کند."""
    hint = (hint or "").strip()
    if not hint:
        return None
    try:
        results = source.search(hint, MAX_RESULTS)
    except source.SourceError:
        return None
    for r in results:
        if str(r.id) == post_id:
            return r.link
    key = source.short_title(hint, 48).casefold()
    for r in results:
        if source.short_title(r.title, 48).casefold() == key:
            log.info("stale resolved by title chat=%s post_id=%s", chat_id, post_id)
            return r.link
    return results[0].link if results else None


def handle_feedback(api: RubikaAPI, chat_id: str, sender_id: str, text: str) -> None:
    log.info("feedback chat=%s user=%s len=%d", chat_id, sender_id, len(text))
    if ADMIN_CHAT_ID:
        try:
            api.send_message(
                ADMIN_CHAT_ID,
                f"📝 انتقاد / پیشنهاد جدید\n👤 کاربر: {sender_id or 'نامشخص'}\n\n{text[:3000]}",
            )
        except RubikaError:
            log.warning("feedback forward failed")
    else:
        log.warning("ADMIN_CHAT_ID is not set; feedback not forwarded")
    api.send_message(
        chat_id,
        "✅ پیامت رسید، ممنون از وقتی که گذاشتی 🙏",
        chat_rows=_home_keypad["rows"],
    )


def handle_update(api: RubikaAPI, update: dict) -> None:
    # لاگ خام برای دیدن شکل واقعی به‌روزرسانی‌ها (به‌خصوص کلیک دکمه)
    log.info(
        "raw: %s", json.dumps(update, ensure_ascii=False, default=str)[:500]
    )

    utype = update.get("type")
    chat_id = update.get("chat_id")

    # کلیک دکمهٔ inline: آبجکت مستقل inline_message (بدون فیلد type)
    if "inline_message" in update:
        im = update.get("inline_message") or {}
        chat_id = im.get("chat_id") or chat_id
        aux = im.get("aux_data") or {}
        button_id = aux.get("button_id") or ""
        text = (im.get("text") or "").strip()
        if not chat_id:
            return
        if button_id.startswith("m:"):
            handle_selection(api, chat_id, button_id[2:], text)
            return
        link = _search_by_title.get(chat_id, {}).get(text.casefold())
        if link:
            log.info("inline selection via text chat=%s text=%r", chat_id, text[:40])
            send_movie(api, chat_id, link)
            return
        log.info("inline click unhandled: button_id=%r text=%r", button_id, text[:40])
        return

    if utype == "StartedBot" and chat_id:
        api.send_message(chat_id, WELCOME, chat_rows=_home_keypad["rows"])
        return
    if utype != "NewMessage":
        return

    msg = update.get("new_message") or {}
    chat_id = msg.get("chat_id") or chat_id
    if not chat_id:
        return

    # شناسهٔ دکمه ممکن است داخل new_message یا روی خود update باشد
    aux = msg.get("aux_data") or update.get("aux_data") or {}
    button_id = aux.get("button_id") or ""
    text = (msg.get("text") or "").strip()

    sender_id = msg.get("sender_id") or ""

    if button_id:
        # کلیک دکمه — بدون توجه به sender_type (برخی کلیک‌ها فرستنده ندارند)
        if not is_member(api, sender_id):
            send_join_prompt(api, chat_id)
            return
        if button_id == "fb":
            _awaiting_feedback[chat_id] = True
            api.send_message(
                chat_id,
                "📝 انتقاد یا پیشنهادت رو بنویس و بفرست؛ مستقیم به مدیر می‌رسه.",
            )
        elif button_id == "check":
            _member_cache.pop(sender_id, None)
            if is_member(api, sender_id):
                api.send_message(
                    chat_id, "✅ عضویتت تأیید شد!\n\n" + WELCOME, chat_rows=_home_keypad["rows"]
                )
            else:
                send_join_prompt(api, chat_id)
        elif button_id.startswith("m:"):
            handle_selection(api, chat_id, button_id[2:], text)
        else:
            log.info("ignored button_id=%r chat=%s", button_id, chat_id)
        return

    if msg.get("sender_type") != "User":
        return  # پیام‌های خود ربات را نادیده بگیر
    if not text:
        return

    # گیت عضویت (قبل از هر عملیاتی)
    if not is_member(api, sender_id):
        log.info("blocked non-member user=%s", sender_id)
        send_join_prompt(api, chat_id)
        return

    stats.track_user(sender_id)

    if text in ("/start", "/help"):
        api.send_message(chat_id, WELCOME, chat_rows=_home_keypad["rows"])
        return

    # آمار فقط برای مدیر (در چت خصوصی خودش با ربات)
    if text in ("/stats", "آمار") and ADMIN_CHAT_ID and chat_id == ADMIN_CHAT_ID:
        api.send_message(chat_id, stats.report_text(), chat_rows=_home_keypad["rows"])
        return

    if text == "/feedback" or _awaiting_feedback.pop(chat_id, False):
        if text == "/feedback":
            _awaiting_feedback[chat_id] = True
            api.send_message(
                chat_id, "📝 انتقاد یا پیشنهادت رو بنویس و بفرست؛ مستقیم به مدیر می‌رسه."
            )
            return
        handle_feedback(api, chat_id, sender_id, text)
        return

    # پاسخ شماره‌ای (۱ تا N) به‌جای دکمه
    num = text.translate(
        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    )
    if num.isdigit():
        ids = _search_order.get(chat_id, [])
        if 1 <= int(num) <= len(ids):
            handle_selection(api, chat_id, ids[int(num) - 1])
            return

    # اگر روبیکا کلیک دکمه را به‌صورت متن ساده فرستاد:
    link = _search_by_title.get(chat_id, {}).get(text.casefold())
    if link:
        log.info("selection via text chat=%s text=%r", chat_id, text[:40])
        send_movie(api, chat_id, link)
        return

    stats.track_search()
    handle_search(api, chat_id, text)


# --------------------------- صف کار هر چت --------------------------- #
# هر چت یک کارگر خودش دارد: جستجوی سنگین یک کاربر، بقیه را معطل نمی‌کند
# و ترتیب پیام‌های هر کاربر هم حفظ می‌شود.
_chat_queues: dict[str, queue.Queue] = {}
_queues_lock = threading.Lock()


def _chat_worker(api: RubikaAPI, chat_key: str, q: "queue.Queue") -> None:
    while True:
        update = q.get()
        started = time.time()
        try:
            handle_update(api, update)
        except Exception:
            log.exception("error handling update")
        finally:
            elapsed = time.time() - started
            if elapsed >= 3:
                log.info("slow handler chat=%s %.1fs", chat_key, elapsed)
            q.task_done()


def enqueue(api: RubikaAPI, update: dict) -> None:
    chat_key = (
        update.get("chat_id")
        or (update.get("inline_message") or {}).get("chat_id")
        or "unknown"
    )
    with _queues_lock:
        q = _chat_queues.get(chat_key)
        if q is None:
            q = queue.Queue()
            _chat_queues[chat_key] = q
            threading.Thread(
                target=_chat_worker, args=(api, chat_key, q), daemon=True
            ).start()
    q.put(update)


def main() -> None:
    token = os.getenv("RUBIKA_BOT_TOKEN", "").strip()
    if not token:
        print("خطا: RUBIKA_BOT_TOKEN در فایل .env تنظیم نشده است.", file=sys.stderr)
        sys.exit(1)
    token = token
    api = RubikaAPI(token)
    offset: str | None = _load_offset()
    # روی هاست‌های موقت (مثل GitHub Actions) بعد از این مدت خودش تمیز خارج می‌شود
    max_runtime = float(os.getenv("MAX_RUNTIME_SECONDS", "0") or 0)
    started = time.time()
    log.info("Rubika bot started (polling)…")
    heartbeat()
    while True:
        if max_runtime and time.time() - started >= max_runtime:
            log.info("max runtime reached; exiting cleanly")
            break
        try:
            data = api.get_updates(offset)
        except RubikaError:
            time.sleep(3)
            continue
        heartbeat()
        for update in data.get("updates", []):
            age = _update_age(update)
            if MAX_UPDATE_AGE and age is not None and age > MAX_UPDATE_AGE:
                log.info("skip stale update age=%.0fs", age)
                continue
            enqueue(api, update)
        offset = data.get("next_offset_id") or offset
        _save_offset(offset)
        time.sleep(POLL_INTERVAL)
    _drain(timeout=120)
    stats.save(force=True)
    log.info("bot stopped")


def _update_age(update: dict) -> float | None:
    ts = update.get("update_time") or (update.get("new_message") or {}).get("time")
    try:
        return time.time() - float(ts)
    except (TypeError, ValueError):
        return None


def _drain(timeout: float = 60) -> None:
    """صبر می‌کند کارهای در صف تمام شوند تا پیام کسی نصفه نماند."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _queues_lock:
            busy = [q for q in _chat_queues.values() if q.unfinished_tasks]
        if not busy:
            return
        time.sleep(0.2)


if __name__ == "__main__":
    main()
