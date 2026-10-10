"""
Скачивание и конвертация в MP3: yt-dlp + FFmpeg.

Поддержка:
  • YouTube / YouTube Music (на серверных IP нужны cookies — YT_COOKIES — или PROXY_URL)
  • SoundCloud
  • Spotify — берём «исполнитель — название» и ищем на YouTube / SoundCloud
"""
import asyncio
import html
import json
import logging
import os
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import yt_dlp

log = logging.getLogger("downloader")

MAX_FILE_MB = 48
MAX_BYTES = MAX_FILE_MB * 1024 * 1024

PROXY = os.environ.get("PROXY_URL", "").strip() or None


_COOKIE_RE = re.compile(
    r"(?P<domain>(?:#HttpOnly_)?[A-Za-z0-9.-]+\.[A-Za-z]{2,})\s+"
    r"(?P<flag>TRUE|FALSE)\s+"
    r"(?P<path>/\S*)\s+"
    r"(?P<secure>TRUE|FALSE)\s+"
    r"(?P<expiry>-?\d+)\s+"
    r"(?P<name>[^\s=;]+)\s+"
    r"(?P<value>\S*?)(?=\s|$|(?:#HttpOnly_)?\.(?:youtube|google)\.com\s)",
)
_LOGIN_COOKIES = ("LOGIN_INFO", "SAPISID", "__Secure-3PSID", "SID")


def _normalize_cookies(raw: str):
    """Чинит cookies, вставленные с телефона: табуляции → пробелы,
    пропавшие переносы строк, буквальные \\n / \\t. Каждая запись
    разбирается по 7 полям и собирается обратно в формат Netscape."""
    text = raw.replace("\\t", "\t").replace("\\n", "\n").replace("\r", "\n")
    # убираем комментарии (# Netscape HTTP Cookie File и т.п.), но не #HttpOnly_
    text = re.sub(
        r"#(?!HttpOnly_)[^\n]*?(?=(?:#HttpOnly_)?\.?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
        r"\.[A-Za-z]{2,}\s+(?:TRUE|FALSE)\s|\n|$)",
        " ", text)
    cookies = []
    for m in _COOKIE_RE.finditer(text):
        cookies.append("\t".join(m.group("domain", "flag", "path", "secure",
                                          "expiry", "name", "value")))
    names = {c.split("\t")[5] for c in cookies}
    logged_in = any(n in names for n in _LOGIN_COOKIES)
    out = "# Netscape HTTP Cookie File\n" + "\n".join(cookies) + "\n"
    return out, len(cookies), logged_in


def _prepare_cookies():
    """YT_COOKIES — содержимое cookies.txt (формат Netscape) из браузера,
    где выполнен вход в YouTube. Пишем во временный файл для yt-dlp."""
    raw = os.environ.get("YT_COOKIES", "").strip()
    if not raw:
        log.warning("YT_COOKIES: не задано — YouTube на Render может не качаться")
        return None
    content, count, logged_in = _normalize_cookies(raw)
    log.warning("YT_COOKIES: распознано %d cookies, вход в аккаунт: %s",
                count, "да" if logged_in else "НЕТ")
    if not count:
        return None
    path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


COOKIES_FILE = _prepare_cookies()


FALLBACK_QUALITIES = [320, 256, 192, 128, 96, 64]
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ytdl")

# Наборы клиентов YouTube. android/ios не принимают cookies и почти всегда
# требуют PO-токен, поэтому с cookies используем только «веб/ТВ»-клиенты.
if COOKIES_FILE:
    YT_CLIENT_SETS = [None, ["tv"], ["web_safari"], ["mweb"], ["tv_downgraded"], ["web"]]
else:
    YT_CLIENT_SETS = [None, ["tv_simply"], ["android_vr"], ["web_embedded"], ["tv"], ["mweb"]]

YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be|music\.youtube\.com)", re.I)

# Регулярка для вытаскивания URL из текста сообщения (используется в bot.py)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)

RETRY_MARKERS = (
    "sign in", "not a bot", "confirm you", "login", "cookie",
    "http error 403", "http error 429", "http error 400",
    "no video formats", "requested format", "player response",
    "timeout", "timed out", "premiere", "live event",
    "http error 401", "unable to download api page", "only images are available",
    "fragment", "unable to extract", "did not get any data",
)


class DownloadError(Exception):
    """Ошибка с человекочитаемым сообщением для пользователя."""


# ------------------------------------------------------------------ Spotify

def is_spotify(url: str) -> bool:
    return "spotify.com" in url or url.startswith("spotify:")


async def resolve_spotify(url: str) -> str:
    m = re.search(r"track[/:]([A-Za-z0-9]{22})", url)
    if not m:
        raise DownloadError(
            "Это ссылка на альбом или плейлист Spotify 😕\n"
            "Пришли ссылку на конкретный трек: <code>open.spotify.com/track/…</code>"
        )
    track_id = m.group(1)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        # 1) embed-страница: там есть и название, и исполнители
        try:
            async with session.get(f"https://open.spotify.com/embed/track/{track_id}") as resp:
                page = await resp.text()
            nd = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page, re.S)
            entity = json.loads(nd.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
            name = entity.get("name") or entity.get("title")
            artists = ", ".join(a["name"] for a in entity.get("artists") or [] if a.get("name"))
            if name:
                return f"{artists} - {name}" if artists else name
        except Exception as e:
            log.info("spotify embed: %s", str(e)[:120])
        # 2) запасной вариант — oEmbed (только название)
        try:
            async with session.get(
                "https://open.spotify.com/oembed",
                params={"url": f"https://open.spotify.com/track/{track_id}"},
            ) as resp:
                data = await resp.json(content_type=None)
            if data and data.get("title"):
                return data["title"]
        except Exception:
            pass
    raise DownloadError("Не удалось получить данные трека из Spotify 😔")



# ------------------------------------------------------------------ утилиты

def _is_youtube(url: str) -> bool:
    return url.startswith("ytsearch") or bool(YOUTUBE_RE.search(url or ""))


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in RETRY_MARKERS)


def _youtube_id(url: str):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def _pick_quality(duration: int, preferred: int):
    for q in sorted(set(FALLBACK_QUALITIES + [preferred]), reverse=True):
        if q > preferred:
            continue
        if int(duration) * q * 1000 // 8 <= MAX_BYTES:
            return q
    return None


# ------------------------------------------------------------------ yt-dlp

def _base_opts() -> dict:
    opts = {
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "nocheckcertificate": True,
        "retries": 3,
        "fragment_retries": 3,
        # JS-рантайм для YouTube (решение n/sig-челленджей). Deno ставится в Dockerfile.
        "js_runtimes": {"deno": {}, "node": {}},
    }
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    if PROXY:
        opts["proxy"] = PROXY
    return opts


def _apply_clients(opts: dict, clients) -> None:
    if clients:
        opts["extractor_args"] = {"youtube": {"player_client": clients}}


def _probe_sync(url: str, clients=None):
    opts = _base_opts()
    opts.update({"skip_download": True, "extract_flat": "in_playlist"})
    _apply_clients(opts, clients)
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _download_sync(url: str, tmp: str, quality: int, hook, clients=None):
    opts = _base_opts()
    opts.update({
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
        "writethumbnail": True,
        "convert_thumbnails": "jpg",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(quality)},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "progress_hooks": [hook] if hook else [],
    })
    _apply_clients(opts, clients)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


async def _run(func, *args):
    return await asyncio.get_running_loop().run_in_executor(_executor, func, *args)


async def _probe_with_fallback(url: str):
    attempts = YT_CLIENT_SETS if _is_youtube(url) else [None]
    err = None
    for clients in attempts:
        try:
            info = await _run(_probe_sync, url, clients)
            return info, clients
        except Exception as e:
            err = e
            log.info("probe %s: %s -> %s", url[:80], clients, str(e)[:120])
            if not _is_retryable(e):
                raise
    raise err


async def _download_with_fallback(url, tmp, quality, hook, clients) -> None:
    attempts = YT_CLIENT_SETS if _is_youtube(url) else [None]
    order = [clients] + [a for a in attempts if a != clients]
    err = None
    for c in order:
        try:
            await _run(_download_sync, url, tmp, quality, hook, c)
            return
        except Exception as e:
            err = e
            log.info("download %s: %s -> %s", url[:80], c, str(e)[:120])
            for f in os.listdir(tmp):
                try:
                    os.remove(os.path.join(tmp, f))
                except OSError:
                    pass
            if not _is_retryable(e):
                raise
    raise err


def _make_hook(loop, cb, title: str):
    state = {"last": -100.0}

    def hook(d):
        if cb is None or d.get("status") != "downloading":
            return
        raw = str(d.get("_percent_str", "")).strip().rstrip("%")
        try:
            p = float(raw)
        except ValueError:
            return
        if p - state["last"] < 15:
            return
        state["last"] = p
        try:
            asyncio.run_coroutine_threadsafe(
                cb(f"⬇️ <i>Скачиваю «{title}» — {int(p)}%</i>"), loop
            )
        except Exception:
            pass

    return hook


# ------------------------------------------------------------------ ошибки

def _friendly_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "unsupported url" in msg:
        return "Этот сайт не поддерживается 😕\nПопробуй YouTube, SoundCloud или ссылку на трек Spotify."
    if "video unavailable" in msg or "not available" in msg or "removed" in msg:
        return "Контент недоступен — удалён или приватный 😔"
    if "private" in msg:
        return "Контент приватный, скачать не получится 😔"
    if "age" in msg and ("confirm" in msg or "restrict" in msg):
        return "Возрастное ограничение — скачивание недоступно 😔"
    if ("sign in" in msg or "login" in msg or "cookies" in msg or "not a bot" in msg
            or "http error 403" in msg or "requested format" in msg
            or "only images are available" in msg):
        return ("YouTube блокирует сервер бота 😔 Пришли трек ссылкой на SoundCloud или Spotify."
                if not COOKIES_FILE else
                "YouTube не принял cookies — их нужно обновить 😔")
    if "timeout" in msg or "timed out" in msg:
        return "Источник долго не отвечает, попробуй ещё раз 😔"
    log.warning("Ошибка: %s", str(exc)[:300])
    return "Не удалось скачать 😔 Попробуй другую ссылку или повтори позже."


# ------------------------------------------------------------------ поиск списком

_SEP_RE = re.compile(r"\s+(?:-{1,2}|–|—)\s+")


def _clean_name(name: str) -> str:
    return re.sub(r"\s*-\s*Topic$", "", (name or "").strip(), flags=re.I).strip()


def _entry_label(e: dict):
    """Возвращает (исполнитель, название) для результата поиска."""
    title = re.sub(r"\s+", " ", (e.get("title") or "").strip())
    author = _clean_name(e.get("artist") or e.get("uploader") or e.get("channel") or "")
    parts = _SEP_RE.split(title, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0].strip(), parts[1].strip()
    return author, title


def _search_sync(query: str):
    opts = _base_opts()
    opts.update({"skip_download": True, "extract_flat": "in_playlist", "noplaylist": False})
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(query, download=False)


async def search_tracks(query: str, limit: int = 50) -> list:
    """Поиск треков списком: YouTube + SoundCloud.
    Возвращает list[dict(url, artist, title, duration, source)]."""
    query = re.sub(r"\s+", " ", query).strip()[:200]
    if len(query) < 2:
        raise DownloadError("Слишком короткий запрос 😕 Напиши исполнителя и название трека.")

    async def one(prefix: str, n: int, source: str):
        try:
            info = await _run(_search_sync, f"{prefix}{n}:{query}")
        except Exception as e:
            log.info("search %s: %s", source, str(e)[:160])
            return []
        out = []
        for e in info.get("entries") or []:
            if not e:
                continue
            url = e.get("url") or e.get("webpage_url")
            if not url:
                continue
            if not str(url).startswith("http"):
                url = f"https://www.youtube.com/watch?v={url}"
            dur = int(e.get("duration") or 0)
            if dur and dur > 2 * 60 * 60:  # часовые миксы не влезут в 50 МБ
                continue
            artist, title = _entry_label(e)
            if not title:
                continue
            out.append({"url": url, "artist": artist, "title": title,
                        "duration": dur, "source": source})
        return out

    yt, sc = await asyncio.gather(one("ytsearch", 25, "yt"), one("scsearch", 30, "sc"))

    # чередуем YouTube и SoundCloud, убираем дубли
    merged, seen = [], set()
    for i in range(max(len(yt), len(sc))):
        for lst in (yt, sc):
            if i < len(lst):
                it = lst[i]
                key = (re.sub(r"\W+", "", (it["artist"] + it["title"]).lower()), it["duration"] // 3)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(it)
    if not merged:
        raise DownloadError("Ничего не нашлось 😕 Попробуй уточнить: исполнитель — название.")
    return merged[:limit]


# ------------------------------------------------------------------ основной пайплайн

async def process_query(query: str, preferred_quality: int = 192, progress_cb=None, loop=None):
    """Поиск по тексту «исполнитель — название»: SoundCloud, затем YouTube."""
    query = re.sub(r"\s+", " ", query).strip()[:200]
    if len(query) < 2:
        raise DownloadError("Слишком короткий запрос 😕 Напиши исполнителя и название трека.")
    return await process_url("", preferred_quality, progress_cb, loop, search_query=query)


async def _youtube_fallback_query(url: str, meta: dict = None):
    """Строка поиска для запасного скачивания с SoundCloud, если YouTube не отдал трек."""
    if meta and meta.get("title"):
        return f"{meta.get('artist') or ''} {meta['title']}".strip()
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout,
                                         headers={"User-Agent": "Mozilla/5.0"}) as session:
            async with session.get(_OEMBED["youtube"], params={"url": url, "format": "json"}) as r:
                if r.status != 200:
                    return None
                d = await r.json(content_type=None)
    except Exception as e:
        log.info("oembed fallback %s: %s", url[:60], str(e)[:120])
        return None
    title = (d.get("title") or "").strip()
    author = _clean_name(d.get("author_name") or "")
    if not title:
        return None
    if _SEP_RE.search(title) or (author and author.lower() in title.lower()):
        return title
    return f"{author} {title}".strip()


async def process_url(url: str, preferred_quality: int = 192, progress_cb=None, loop=None,
                      search_query: str = None, meta: dict = None):
    """
    Возвращает dict: path, tmpdir, title, performer, duration, quality, size_mb.
    Вызывающий обязан удалить tmpdir после отправки!
    Если YouTube не отдаёт трек (блок сервера, нет cookies и т.п.) — ищем
    тот же трек на SoundCloud и качаем оттуда.
    """
    try:
        return await _process_url(url, preferred_quality, progress_cb, loop, search_query, meta)
    except DownloadError as e:
        if search_query or not url or is_spotify(url) or not YOUTUBE_RE.search(url):
            raise
        query = await _youtube_fallback_query(url, meta)
        if not query:
            raise
        log.warning("YouTube не отдал %s (%s) — пробую SoundCloud: %s", url[:80], str(e)[:80], query)
        if progress_cb:
            try:
                await progress_cb("🔁 <i>YouTube не отдаёт трек, ищу его на SoundCloud…</i>")
            except Exception:
                pass
        try:
            return await _process_url("", preferred_quality, progress_cb, loop,
                                      search_query=query, meta=meta, sources=("sc",))
        except DownloadError:
            raise e


async def _process_url(url: str, preferred_quality: int = 192, progress_cb=None, loop=None,
                       search_query: str = None, meta: dict = None, sources=("sc", "yt")):
    loop = loop or asyncio.get_running_loop()
    prefixes = {"sc": "scsearch1:", "yt": "ytsearch1:"}

    if search_query:
        candidates = [f"{prefixes[s]}{search_query}" for s in sources]
    elif is_spotify(url):
        query = await resolve_spotify(url)
        log.info("spotify -> %s", query)
        candidates = [f"scsearch1:{query}", f"ytsearch1:{query}"]
    else:
        candidates = [url]

    info, clients, last_err = None, None, None
    for cand in candidates:
        try:
            probed, probed_clients = await _probe_with_fallback(cand)
        except Exception as e:
            last_err = e
            log.info("candidate %s: %s", cand[:60], str(e)[:160])
            continue
        # плейлист / поиск → первый трек; пустой результат = пробуем следующий кандидат
        if probed.get("_type") == "playlist" or "entries" in probed:
            entries = [e for e in (probed.get("entries") or []) if e]
            if not entries:
                log.info("candidate %s: пустой результат", cand[:60])
                continue
            first = entries[0]
            nested = str(first.get("url") or first.get("webpage_url") or first.get("id"))
            if not nested.startswith("http"):
                nested = f"https://www.youtube.com/watch?v={nested}"
            try:
                probed, probed_clients = await _probe_with_fallback(nested)
                cand = nested
            except Exception as e:
                last_err = e
                log.info("candidate %s: %s", nested[:60], str(e)[:160])
                continue
        info, clients, url = probed, probed_clients, cand
        break

    if info is None:
        if last_err is None:
            raise DownloadError("Ничего не нашлось 😕 Попробуй уточнить: исполнитель — название.")
        raise DownloadError(_friendly_error(last_err))

    duration = int(info.get("duration") or 0)
    quality = _pick_quality(duration, preferred_quality) if duration else preferred_quality
    if duration and quality is None:
        raise DownloadError(f"Слишком длинный трек ({duration // 60} мин) — не влезет в 50 МБ 😔")

    title = info.get("title") or "audio"
    hook = _make_hook(loop, progress_cb, html.escape(title))
    tmp = tempfile.mkdtemp(prefix="mp3bot_")
    try:
        await _download_with_fallback(url, tmp, quality, hook, clients)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise DownloadError(_friendly_error(e))

    files = [f for f in os.listdir(tmp) if f.lower().endswith(".mp3")]
    if not files:
        shutil.rmtree(tmp, ignore_errors=True)
        raise DownloadError("Не удалось сконвертировать файл 😔")
    path = os.path.join(tmp, files[0])
    size_mb = os.path.getsize(path) / 1024 / 1024
    if size_mb > 49.5:
        shutil.rmtree(tmp, ignore_errors=True)
        raise DownloadError("Файл больше 50 МБ — Telegram не примет 😔")

    performer = info.get("artist") or _clean_name(info.get("uploader") or info.get("channel") or "")
    if meta and meta.get("title"):
        title, performer = meta["title"], meta.get("artist") or performer
    return {
        "path": path,
        "tmpdir": tmp,
        "title": title,
        "performer": performer,
        "source_url": info.get("webpage_url") or url,
        "duration": duration or None,
        "quality": quality,
        "size_mb": round(size_mb, 1),
    }


# ------------------------------------------------------------------ название трека по ссылке (для текстов)

_OEMBED = {
    "youtube": "https://www.youtube.com/oembed",
    "soundcloud": "https://soundcloud.com/oembed",
}


async def track_name_from_url(url: str) -> str:
    """Ссылка YouTube / SoundCloud / Spotify / др. → строка «исполнитель название» для поиска текста."""
    if is_spotify(url):
        return await resolve_spotify(url)
    kind = "youtube" if YOUTUBE_RE.search(url) else ("soundcloud" if "soundcloud.com" in url.lower() else None)
    if kind:
        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession(timeout=timeout,
                                             headers={"User-Agent": "Mozilla/5.0"}) as session:
                async with session.get(_OEMBED[kind], params={"url": url, "format": "json"}) as r:
                    if r.status == 200:
                        d = await r.json(content_type=None)
                        title = (d.get("title") or "").strip()
                        author = _clean_name(d.get("author_name") or "")
                        if title:
                            if _SEP_RE.search(title) or (author and author.lower() in title.lower()):
                                return title
                            return f"{author} {title}".strip()
        except Exception as e:
            log.info("oembed %s: %s", url[:60], str(e)[:120])
    try:
        info, _ = await _probe_with_fallback(url)
    except Exception as e:
        raise DownloadError(_friendly_error(e))
    if info.get("entries"):
        info = next((e for e in info["entries"] if e), {}) or {}
    title = info.get("track") or info.get("title") or ""
    artist = info.get("artist") or _clean_name(info.get("uploader") or info.get("channel") or "")
    if not title:
        raise DownloadError("Не смог понять, что за трек по этой ссылке 😕 Напиши название.")
    return title if _SEP_RE.search(title) else f"{artist} {title}".strip()
