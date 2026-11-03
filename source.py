"""کلاینت سایت‌های منبع فیلم.

منابع به ترتیب اولویت:
1. www.myf2ms.top (rayanvand.top) — منبع اصلی
2. donyayeserial.com — اگر اثر در منبع اصلی پیدا نشد

جستجوی فیلم/سریال و استخراج لینک‌های دانلود از صفحهٔ اثر.
این ماژول بین ربات‌های تلگرام و روبیکا مشترک است و وابستگی
به هیچ‌کدام از پلتفرم‌ها ندارد.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlparse

import requests

BASE_URL = "https://www.myf2ms.top"
DY_BASE_URL = "https://donyayeserial.com"

SOURCE_MYF2MS = "myf2ms"
SOURCE_DONYAYE = "donyayeserial"

# نام فارسی منابع برای نمایش در پیام
SOURCE_LABELS = {
    SOURCE_MYF2MS: "فیلم ۲ مدیا",
    SOURCE_DONYAYE: "دنیای سریال",
}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT = 12  # ثانیه؛ کوتاه تا خطا سریع به منبع بعدی برسد

_local = threading.local()


def _session() -> requests.Session:
    """برای هر ترد یک Session جدا (ربات هم‌زمان چند کاربر را سرویس می‌دهد)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(
            {"User-Agent": USER_AGENT, "Accept-Language": "fa,en;q=0.8"}
        )
        _local.session = s
    return s


# کش کوتاه‌مدت: جستجو و صفحهٔ اثر ۱۰ دقیقه کش می‌شوند تا پاسخ‌های تکراری فوری باشند
CACHE_TTL = 600
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: str):
    with _cache_lock:
        item = _cache.get(key)
    if item is None:
        return None
    expiry, value = item
    if expiry < time.time():
        with _cache_lock:
            _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: object) -> None:
    now = time.time()
    with _cache_lock:
        if len(_cache) > 300:
            for k, (exp, _) in list(_cache.items()):
                if exp < now:
                    _cache.pop(k, None)
        _cache[key] = (now + CACHE_TTL, value)


class SourceError(RuntimeError):
    """خطا در ارتباط با سایت منبع."""


@dataclass
class SearchResult:
    id: int
    title: str
    link: str
    source: str = SOURCE_MYF2MS


@dataclass
class DownloadLink:
    quality: str
    url: str


@dataclass
class Movie:
    title: str
    link: str
    poster_url: str | None = None
    groups: list[tuple[str, list[DownloadLink]]] = field(default_factory=list)
    source: str = SOURCE_MYF2MS

    @property
    def all_links(self) -> list[DownloadLink]:
        return [lk for _, lks in self.groups for lk in lks]


def _get(url: str) -> requests.Response:
    try:
        resp = _session().get(url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SourceError(f"درخواست ناموفق: {url}") from exc
    return resp


def _json(resp: requests.Response) -> object:
    """پاسخ JSON سایت شامل BOM است؛ با utf-8-sig پارس می‌شود."""
    return json.loads(resp.content.decode("utf-8-sig", "replace"))


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def short_title(title: str, limit: int = 55) -> str:
    """عنوان کوتاه‌شده برای دکمه‌ها و پیام‌ها."""
    t = _clean(title)
    t = re.sub(r"^دانلود\s+(?:دوبله\s+فارسی\s+)?(?:فیلم|سریال|انیمیشن|انیمه)?\s*", "", t)
    t = re.sub(r"\s*بدون\s+سانسور.*$", "", t)
    t = re.sub(r"\s*با\s+زیرنویس\s+فارسی.*$", "", t)
    t = re.sub(r"\s*[|\-–—]\s*$", "", t).strip()
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t or _clean(title)


def extract_year(title: str) -> str | None:
    m = re.search(r"\b((?:19|20)\d{2})\b", _clean(title))
    return m.group(1) if m else None


def _norm(text: str) -> str:
    """نرمال‌سازی برای مقایسهٔ عنوان با عبارت جستجو (ی/ي، ک/ك، نیم‌فاصله)."""
    text = _clean(text)
    text = text.replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip().lower()


def _title_score(title: str, query: str) -> int:
    """امتیاز تطبیق عنوان با عبارت جستجو (نتایج عنوانی بالاتر می‌نشینند)."""
    t, q = _norm(title), _norm(query)
    if not q or not t:
        return 0
    if q in t:
        return 3
    tw = t.split()
    return sum(1 for w in q.split() if w in tw)


def _search_all(query: str) -> list[dict]:
    """جستجوی سراسری وردپرس: همه typeها (فیلم‌ها post، سریال‌ها series)."""
    url = (
        f"{BASE_URL}/wp-json/wp/v2/search"
        f"?search={quote(query)}&per_page=20&_fields=id,title,url"
    )
    data = _json(_get(url))
    if not isinstance(data, list):
        raise SourceError("پاسخ غیرمنتظره از سایت منبع.")
    return [
        {"id": it["id"], "title": it.get("title") or "", "url": it.get("url") or ""}
        for it in data
        if it.get("url")
    ]


def _search_posts(query: str) -> list[dict]:
    """جستجو در posts (پشتیبان وقتی جستجوی سراسری نتیجه ندهد)."""
    url = (
        f"{BASE_URL}/wp-json/wp/v2/posts"
        f"?search={quote(query)}&per_page=20&_fields=id,title,link"
    )
    data = _json(_get(url))
    if not isinstance(data, list):
        raise SourceError("پاسخ غیرمنتظره از سایت منبع.")
    return [
        {
            "id": p["id"],
            "title": (p.get("title") or {}).get("rendered") or "",
            "url": p.get("link") or "",
        }
        for p in data
        if p.get("link")
    ]


def _to_results(
    items: list[dict], query: str, limit: int, source_id: str
) -> list[SearchResult]:
    """مرتب‌سازی بر اساس تطبیق عنوان + حذف تکراری‌ها."""
    items.sort(key=lambda it: -_title_score(it["title"], query))
    results: list[SearchResult] = []
    seen: set[str] = set()
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        results.append(
            SearchResult(
                id=int(it["id"]),
                title=_clean(it["title"]),
                link=it["url"],
                source=source_id,
            )
        )
        if len(results) >= limit:
            break
    return results


def _search_primary(query: str, limit: int = 6) -> list[SearchResult]:
    """جستجو در سایت اصلی (فیلم ۲ مدیا) از REST API وردپرس.

    ابتدا جستجوی سراسری (همه typeها) و در نبود نتیجه، جستجو در posts.
    نتایج با امتیاز تطبیق عنوان مرتب می‌شوند، نه بر اساس تاریخ انتشار.
    """
    try:
        items = _search_all(query)
        if not items:
            items = _search_posts(query)
    except (SourceError, ValueError) as exc:
        raise SourceError("جستجو ناموفق بود.") from exc
    return _to_results(items, query, limit, SOURCE_MYF2MS)


def _dy_search(query: str, limit: int = 6) -> list[SearchResult]:
    """جستجو در دنیای سریال (REST API وردپرس: post/series/anime/collection)."""
    url = (
        f"{DY_BASE_URL}/wp-json/wp/v2/search"
        f"?search={quote(query)}&per_page=20&_fields=id,title,url"
    )
    try:
        data = _json(_get(url))
    except ValueError as exc:
        raise SourceError("پاسخ غیرمنتظره از دنیای سریال.") from exc
    if not isinstance(data, list):
        raise SourceError("پاسخ غیرمنتظره از دنیای سریال.")
    items = [
        {"id": it["id"], "title": it.get("title") or "", "url": it.get("url") or ""}
        for it in data
        if it.get("url")
    ]
    return _to_results(items, query, limit, SOURCE_DONYAYE)


def search(query: str, limit: int = 6) -> list[SearchResult]:
    """جستجو با ترتیب اولویت منابع: فیلم ۲ مدیا ← دنیای سریال."""
    query = query.strip()
    if not query:
        return []
    key = f"q:{_norm(query)}:{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    failed = 0
    for func, source_id in (
        (_search_primary, SOURCE_MYF2MS),
        (_dy_search, SOURCE_DONYAYE),
    ):
        try:
            results = func(query, limit)
        except (SourceError, ValueError):
            failed += 1
            continue
        if results:
            _cache_set(key, results)
            return results
    if failed == 2:
        raise SourceError("جستجو در هیچ‌کدام از سایت‌ها ممکن نشد.")
    _cache_set(key, [])
    return []


_SOURCE_SEARCHERS = {
    SOURCE_MYF2MS: _search_primary,
    SOURCE_DONYAYE: _dy_search,
}


def search_in(source_id: str, query: str, limit: int = 6) -> list[SearchResult]:
    """جستجوی مستقیم در یک منبع مشخص (برای تست و چکاپ)."""
    func = _SOURCE_SEARCHERS.get(source_id)
    if func is None:
        raise SourceError(f"منبع ناشناخته: {source_id}")
    return func(query.strip(), limit)


def _quality_from_url(url: str) -> str:
    name = unquote(url.rsplit("/", 1)[-1])
    m = re.search(
        r"(2160p|1080p|720p|480p|360p|WEB-?DL|BluRay|BDRip|HDTV|HDR|x265|x264|HEVC)",
        name,
        re.I,
    )
    return m.group(1) if m else "لینک دانلود"


def _is_image_url(url: str) -> bool:
    return bool(re.search(r"\.(?:jpe?g|png|webp|gif)(?:\?.*)?$", url, re.I))


def _featured_image(link: str) -> str | None:
    """پوستر رسمی پست از featured_media وردپرس (og:image این سایت خراب است)."""
    pid = re.search(r"/(\d+)/", link)
    if not pid:
        return None
    try:
        post = _json(_get(f"{BASE_URL}/wp-json/wp/v2/posts/{pid.group(1)}?_fields=featured_media"))
        media_id = post.get("featured_media")
        if not media_id:
            return None
        media = _json(_get(f"{BASE_URL}/wp-json/wp/v2/media/{media_id}?_fields=source_url"))
        url = media.get("source_url")
        return url if url and _is_image_url(url) else None
    except (SourceError, ValueError):
        return None


def fetch_movie(link: str, fallback_title: str = "") -> Movie:
    """دریافت صفحهٔ اثر از هر کدام از سایت‌ها و استخراج لینک‌های دانلود."""
    key = f"p:{link}"
    cached = _cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    host = urlparse(link).netloc.lower()
    if "donyayeserial" in host:
        movie = _dy_fetch(link, fallback_title)
    else:
        movie = _myf2_fetch(link, fallback_title)
    _cache_set(key, movie)
    return movie


def _page_title(page: str, fallback_title: str = "") -> str:
    """عنوان از og:title، بعد <h1>، بعد <title>."""
    for pattern in (
        r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"',
        r'<meta[^>]+content="([^"]*)"[^>]+property="og:title"',
    ):
        m = re.search(pattern, page)
        if m:
            return _clean(html_lib.unescape(m.group(1))).split("|")[0].strip()
    m = re.search(r"<h1[^>]*>(.*?)</h1>", page, flags=re.S)
    if m:
        return _clean(m.group(1))
    m = re.search(r"<title>(.*?)</title>", page, flags=re.S)
    if m:
        return _clean(m.group(1)).split("|")[0].strip()
    return fallback_title


def _og_image(page: str, base: str) -> str | None:
    m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page)
    if not m:
        m = re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', page)
    if not m:
        return None
    url = html_lib.unescape(m.group(1)).strip()
    if url.startswith("/"):
        url = base + url
    return url if url.startswith("http") and _is_image_url(url) else None


def _dy_fetch(link: str, fallback_title: str = "") -> Movie:
    """صفحهٔ دنیای سریال: هر بخش «dl-title-open» یک گروه (فصل یا دوبله/زیرنویس)."""
    page = _get(link).text
    title = _page_title(page, fallback_title)

    poster = None
    m = re.search(r'id="post-cover-img"[^>]*data-cover="([^"]+)"', page)
    if m:
        poster = html_lib.unescape(m.group(1)).strip()
    if not poster:
        m = re.search(r'class="[^"]*attachment-indexFilm[^"]*"[^>]*src="([^"]+)"', page)
        if m:
            poster = html_lib.unescape(m.group(1)).strip()
    if not poster:
        poster = _og_image(page, DY_BASE_URL)

    groups: list[tuple[str, list[DownloadLink]]] = []
    for seg in re.split(r'<div class="dl-title-open"', page)[1:]:
        # split فقط تا شروع تگ را جدا کرده؛ بقیهٔ ویژگی‌های تگ را دور می‌ریزیم
        seg = seg.split(">", 1)[1] if ">" in seg else seg
        head = re.split(r'<div class="dl-body', seg)[0]
        group_title = _clean(re.sub(r"<svg.*?</svg>", " ", head, flags=re.S))
        group_title = re.sub(r"^لینک\s*های\s*دانلود\s*", "", group_title).strip()

        links: list[DownloadLink] = []
        seen: set[str] = set()
        for row in re.split(r'<div class="dl-items', seg)[1:]:
            info = row.split('class="download-links"')[0]
            qm = re.search(
                r'<li class="dl-qa">.*?<strong[^>]*>(.*?)</strong>', info, flags=re.S
            )
            quality = _clean(qm.group(1)) if qm else ""
            if not quality:
                qm = re.search(r'کیفیت\s*:?\s*([^<]+)', _clean(re.sub(r"<svg.*?</svg>", " ", info, flags=re.S)))
                quality = qm.group(1).strip() if qm else ""
            em = re.search(r"<span[^>]*>\s*(قسمت[^<]*?)\s*</span>", info)
            episode = _clean(em.group(1)) if em else ""

            hrefs: list[str] = []
            for pattern in (
                r'<a\s[^>]*class="[^"]*dl-link-item[^"]*"[^>]*href="([^"]+)"',
                r'<a\s[^>]*href="([^"]+)"[^>]*class="[^"]*dl-link-item[^"]*"',
            ):
                hrefs.extend(re.findall(pattern, row))
            for href in hrefs:
                href = html_lib.unescape(href).strip()
                if not href.startswith("http") or href in seen:
                    continue
                seen.add(href)
                label = " · ".join(x for x in (episode, quality) if x)
                links.append(DownloadLink(quality=(label or _quality_from_url(href))[:80], url=href))
        if links:
            groups.append((group_title or "لینک‌های دانلود", links))

    return Movie(
        title=title or fallback_title or "نتیجهٔ جستجو",
        link=link,
        poster_url=poster,
        groups=groups,
        source=SOURCE_DONYAYE,
    )


def _myf2_fetch(link: str, fallback_title: str = "") -> Movie:
    """دریافت صفحهٔ اثر از سایت اصلی و استخراج لینک‌های دانلود (باکس دانلود)."""
    page = _get(link).text

    # پوستر از featured_media؛ سپس تصویر شاخص lazy صفحه (سریال‌ها)؛ سپس og:image
    poster = _featured_image(link)
    if not poster:
        mimg = re.search(r"<img[^>]*wp-post-image[^>]*>", page)
        if mimg:
            for attr in ("data-src", "data-lazy-src", "data-original", "data-lazy", "src"):
                am2 = re.search(attr + r'=\"([^\"]+)\"', mimg.group(0))
                if not am2:
                    continue
                cand = html_lib.unescape(am2.group(1)).strip()
                if cand.startswith("http") and _is_image_url(cand):
                    poster = cand
                    break
    if not poster:
        m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page)
        if not m:
            m = re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', page)
        if m:
            candidate = html_lib.unescape(m.group(1)).strip()
            if candidate.startswith("/"):
                candidate = BASE_URL + candidate
            if candidate.startswith("http") and _is_image_url(candidate):
                poster = candidate

    # عنوان از og:title (بدون پسوند نام سایت)
    title = fallback_title
    mt = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page)
    if mt:
        title = _clean(html_lib.unescape(mt.group(1))).split("|")[0].strip()
    if not title:
        mtt = re.search(r"<title>(.*?)</title>", page, flags=re.S)
        if mtt:
            title = _clean(mtt.group(1)).split("|")[0].strip()

    groups: list[tuple[str, list[DownloadLink]]] = []
    for cls, body in re.findall(
        r'<div class="download-list ([^"]*)">(.*?)</ul>', page, flags=re.S
    ):
        tm = re.search(r'<p class="title"><span>(.*?)</span>', body, flags=re.S)
        if tm:
            group_title = _clean(tm.group(1))
        else:
            group_title = "زیرنویس فارسی چسبیده" if cls == "hardsub" else "دوبله فارسی"

        links: list[DownloadLink] = []
        seen: set[str] = set()
        for row in re.findall(r"<li>(.*?)</li>", body, flags=re.S):
            href = None
            am = re.search(r'<a\s[^>]*href="([^"]+)"[^>]*>\s*دانلود\s*مستقیم', row)
            if am:
                href = html_lib.unescape(am.group(1)).strip()
            else:
                am = re.search(
                    r'<a\s[^>]*href="(https://[^"]*(?:abrtech|mehracdn)[^"]*)"', row
                )
                if am:
                    href = html_lib.unescape(am.group(1)).strip()
            if not href or href in seen:
                continue
            seen.add(href)
            qm = re.search(r'<span class="text" dir="ltr"\s*>(.*?)</span>', row, flags=re.S)
            quality = _clean(qm.group(1)) if qm else ""
            if not quality:
                quality = _quality_from_url(href)
            links.append(DownloadLink(quality=quality, url=href))

        # سریال: لینک هر قسمت (دکمهٔ btn-block در باکس دانلود)
        if not links:
            qm_block = re.search(
                r'<span class="text" dir="ltr"\s*>(.*?)</span>', body, flags=re.S
            )
            block_quality = _clean(qm_block.group(1)) if qm_block else ""
            for em in re.finditer(
                r'<a\s[^>]*href="(https://[^"]+)"[^>]*'
                r'class="btn btn-block btn-default"[^>]*>(.*?)</a>',
                body,
                flags=re.S,
            ):
                href = html_lib.unescape(em.group(1)).strip()
                if not re.search(r"abrtech|mehracdn", href) or href in seen:
                    continue
                seen.add(href)
                ep = _clean(re.sub(r"<[^>]+>", " ", em.group(2))) or "قسمت"
                q = _quality_from_url(href)
                if q == "لینک دانلود" and block_quality:
                    q = block_quality
                links.append(DownloadLink(quality=f"{ep} · {q}"[:80], url=href))
        if links:
            groups.append((group_title, links))

    return Movie(
        title=title or fallback_title or "نتیجهٔ جستجو",
        link=link,
        poster_url=poster,
        groups=groups,
        source=SOURCE_MYF2MS,
    )


def build_caption(movie: Movie) -> str:
    """متن پیام نتیجه (مشترک بین تلگرام و روبیکا) — بدون هیچ لینکی به سایت."""
    display_title = short_title(movie.title, limit=80)
    lines = [f"🎬 {display_title}"]
    year = extract_year(movie.title)
    if year:
        lines.append(f"📅 {year}")
    source_label = SOURCE_LABELS.get(movie.source)
    if source_label:
        lines.append(f"📡 منبع: {source_label}")
    n = len(movie.all_links)
    lines.append("")
    if n:
        lines.append(f"📥 {n} لینک دانلود موجود")
        lines.append("")
        lines.append("لینک‌های دانلود 👇")
    else:
        lines.append("⚠️ لینک دانلودی برای این اثر پیدا نشد.")
    return "\n".join(lines)
