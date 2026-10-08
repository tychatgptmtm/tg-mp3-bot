"""
Скачивание и конвертация в MP3: yt-dlp + FFmpeg.

Поддержка:
  • YouTube / YouTube Music (на серверных IP нужны cookies — YT_COOKIES — или PROXY_URL)
  • VK Видео
  • Музыка VK (vk.com/audio…, посты с музыкой, поиск) — нужен VK_TOKEN
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

YT_CLIENT_SETS = [
    None,
    ["android"],
    ["tv"],
    ["ios"],
    ["web_safari"],
    ["web_embedded"],
    ["mweb"],
]

YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be|music\.youtube\.com)", re.I)

# Регулярка для вытаскивания URL из текста сообщения (используется в bot.py)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)

RETRY_MARKERS = (
    "sign in", "not a bot", "confirm you", "login", "cookie",
    "http error 403", "http error 429", "http error 400",
    "no video formats", "requested format", "player response",
    "timeout", "timed out", "premiere", "live event",
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


# ------------------------------------------------------------------ VK audio/topic

VK_AUDIO_RE = re.compile(r"(?:vk\.com|vk\.ru|m\.vk\.com)/(audio|topic|wall)-?\d+_\d+", re.I)
_VK_AUDIO_ID_RE = re.compile(r"audio(-?\d+_\d+(?:_[0-9a-f]+)?)", re.I)
_VK_WALL_ID_RE = re.compile(r"wall(-?\d+_\d+)", re.I)

# VK_TOKEN — токен Kate Mobile (vkhost.github.io). Без него музыка VK недоступна.
VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
VK_API_VERSION = "5.131"
VK_UA = os.environ.get("VK_UA", "").strip() or (
    "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86; "
    "unknown Android SDK built for x86; en)")
VK_PREFIX = "vkaudio:"
# VK_PROXY — http(s)-прокси (лучше российский) для запросов к VK, если VK режет зарубежный сервер
VK_PROXY = os.environ.get("VK_PROXY", "").strip() or None

# VK_COOKIES — cookies.txt с vk.ru / vk.com из браузера, где выполнен вход.
# Бот сам получает по ним веб-токен VK (как это делает сайт vk.com) и обновляет его.
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
VK_WEB_APP_ID = "6287487"


def _parse_vk_cookies():
    raw = os.environ.get("VK_COOKIES", "").strip()
    if not raw:
        return []
    content, _, _ = _normalize_cookies(raw)
    out = []
    for line in content.splitlines():
        parts = line.split("\t")
        if len(parts) == 7 and not line.startswith("# "):
            domain = parts[0].replace("#HttpOnly_", "").lstrip(".").lower()
            if domain.endswith(("vk.com", "vk.ru")):
                out.append((domain, parts[5], parts[6]))
    return out


VK_COOKIES = _parse_vk_cookies()
_vk_web = {"token": None, "exp": 0.0, "ua": WEB_UA}

if VK_TOKEN:
    log.warning("VK_TOKEN: задан — музыка VK включена")
if VK_COOKIES:
    names = {n for _, n, _ in VK_COOKIES}
    doms = sorted({d for d, _, _ in VK_COOKIES})
    log.warning("VK_COOKIES: распознано %d cookies (%s), сессия: %s", len(VK_COOKIES),
                ", ".join(doms)[:200], "да" if names & {"remixsid", "remixnsid", "p", "l"} else "НЕТ")
if not VK_TOKEN and not VK_COOKIES:
    log.warning("VK: нет ни VK_TOKEN, ни VK_COOKIES — музыка VK выключена")


def _cookie_header(host: str) -> str:
    pairs = {}
    for domain, name, value in VK_COOKIES:
        if host == domain or host.endswith("." + domain):
            pairs[name] = value
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


async def _vk_web_token() -> str:
    """Веб-токен VK по cookies (кэшируется до истечения)."""
    import time as _time
    if _vk_web["token"] and _time.time() < _vk_web["exp"] - 60:
        return _vk_web["token"]
    last = "нет ответа"
    timeout = aiohttp.ClientTimeout(total=20)
    for base in ("vk.ru", "vk.com"):
        cookie = _cookie_header(f"login.{base}")
        if not cookie:
            continue
        headers = {"User-Agent": WEB_UA, "Origin": f"https://{base}", "Referer": f"https://{base}/",
                   "Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"}
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"https://login.{base}/?act=web_token", headers=headers,
                                        data={"version": "1", "app_id": VK_WEB_APP_ID},
                                        proxy=VK_PROXY) as resp:
                    data = await resp.json(content_type=None)
        except Exception as e:
            last = str(e)[:150]
            continue
        if isinstance(data, dict) and data.get("type") == "okay":
            d = data.get("data") or {}
            if d.get("access_token"):
                _vk_web["token"] = d["access_token"]
                _vk_web["exp"] = float(d.get("expires") or (_time.time() + 3600))
                log.warning("VK_COOKIES: веб-токен получен через login.%s (user_id=%s)", base, d.get("user_id"))
                return _vk_web["token"]
        last = str(data)[:200]
    log.warning("VK_COOKIES: не удалось получить веб-токен: %s", last)
    raise DownloadError("Cookies VK не подошли (сессия устарела или не та) — их нужно обновить 😔")


def _vk_enabled() -> bool:
    return bool(VK_TOKEN or VK_COOKIES)

VK_NO_TOKEN_MSG = (
    "Музыка VK пока не подключена 😔\n"
    "Напиши исполнителя и название — найду трек на YouTube или SoundCloud 🔎"
)


async def _vk_call(method: str, token: str, ua: str, params: dict):
    params = dict(params, access_token=token, v=VK_API_VERSION)
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": ua}, timeout=timeout) as session:
            async with session.post(f"https://api.vk.com/method/{method}", data=params,
                                    proxy=VK_PROXY) as resp:
                return await resp.json(content_type=None)
    except Exception as e:
        log.warning("VK %s: сеть: %s", method, str(e)[:200])
        raise DownloadError("VK не отвечает, попробуй ещё раз 😔")


async def _vk_api(method: str, **params):
    if not _vk_enabled():
        raise DownloadError(VK_NO_TOKEN_MSG)
    creds = []
    if VK_TOKEN:
        creds.append(("VK_TOKEN", lambda: VK_TOKEN, VK_UA))
    if VK_COOKIES:
        creds.append(("VK_COOKIES", _vk_web_token, WEB_UA))

    data, err_exc = None, None
    for i, (label, get_token, ua) in enumerate(creds):
        try:
            token = get_token()
            if asyncio.iscoroutine(token):
                token = await token
        except DownloadError as e:
            err_exc = e
            continue
        data = await _vk_call(method, token, ua, params)
        if "error" not in data:
            return data.get("response")
        code = data["error"].get("error_code")
        log.warning("VK %s [%s]: ошибка %s — %s", method, label, code, data["error"].get("error_msg", ""))
        if code == 5 and label == "VK_COOKIES":
            _vk_web["token"] = None
        if code in (5, 15, 3) and i + 1 < len(creds):
            continue  # пробуем следующий способ входа
        break
    if data is None or "error" not in data:
        raise err_exc or DownloadError(VK_NO_TOKEN_MSG)

    code = data["error"].get("error_code")
    if code == 5:
        raise DownloadError("Вход в VK устарел (токен или cookies) — их нужно обновить 😔")
    if code == 15:
        raise DownloadError("VK не даёт доступ к музыке с этим входом 😔")
    if code in (201, 203):
        raise DownloadError("VK не даёт доступ к этой аудиозаписи 😔")
    if code in (6, 9, 29):
        raise DownloadError("VK просит подождать — слишком много запросов, повтори через минуту 😔")
    if code == 14:
        raise DownloadError("VK просит капчу — повтори чуть позже 😔")
    raise DownloadError(f"VK вернул ошибку ({code}) 😔")


def _vk_full_id(a: dict) -> str:
    fid = f"{a['owner_id']}_{a['id']}"
    if a.get("access_key"):
        fid += f"_{a['access_key']}"
    return fid


def _vk_cover(a: dict):
    thumb = ((a.get("album") or {}).get("thumb") or {})
    for k in ("photo_600", "photo_300", "photo_270", "photo_135"):
        if thumb.get(k):
            return thumb[k]
    return None


async def vk_search(query: str, count: int = 30) -> list:
    if not _vk_enabled():
        return []
    try:
        resp = await _vk_api("audio.search", q=query, count=count, auto_complete=1, sort=2)
    except DownloadError as e:
        log.info("search vk: %s", e)
        return []
    out = []
    for a in (resp or {}).get("items") or []:
        if not a.get("title"):
            continue
        dur = int(a.get("duration") or 0)
        if dur > 2 * 60 * 60:
            continue
        out.append({"url": VK_PREFIX + _vk_full_id(a), "artist": (a.get("artist") or "").strip(),
                    "title": a["title"].strip(), "duration": dur, "source": "vk"})
    return out


async def _vk_resolve(url: str) -> dict:
    """Ссылка VK / vkaudio:<id> → объект аудиозаписи с прямым url."""
    if url.startswith(VK_PREFIX):
        ids = [url[len(VK_PREFIX):]]
    elif _VK_AUDIO_ID_RE.search(url):
        ids = [_VK_AUDIO_ID_RE.search(url).group(1)]
    elif _VK_WALL_ID_RE.search(url):
        posts = await _vk_api("wall.getById", posts=_VK_WALL_ID_RE.search(url).group(1))
        if isinstance(posts, dict):
            posts = posts.get("items")
        ids = []
        for post in posts or []:
            atts = list(post.get("attachments") or [])
            for rep in post.get("copy_history") or []:
                atts += rep.get("attachments") or []
            ids += [_vk_full_id(x["audio"]) for x in atts if x.get("type") == "audio"]
        if not ids:
            raise DownloadError("В этом посте нет аудиозаписей 😕")
    else:
        raise DownloadError("Не понял ссылку VK 😕 Пришли ссылку на аудиозапись или пост с музыкой.")
    items = await _vk_api("audio.getById", audios=ids[0])
    if not items:
        raise DownloadError("Аудиозапись VK не найдена или удалена 😔")
    a = items[0]
    url_ok = bool(a.get("url")) and "audio_api_unavailable" not in a.get("url", "")
    log.warning("VK трек %s: url=%s, content_restricted=%s, is_licensed=%s, поля=%s",
                _vk_full_id(a), ("есть" if url_ok else repr((a.get("url") or "")[:60])),
                a.get("content_restricted"), a.get("is_licensed"), ",".join(sorted(a.keys()))[:300])
    if not url_ok:
        cr = a.get("content_restricted")
        if cr == 2:
            raise DownloadError("VK не отдаёт этот трек серверу бота — ограничение по региону 😔")
        if cr == 5:
            raise DownloadError("Этот трек ещё не вышел в VK 😔")
        raise DownloadError("VK не отдал ссылку на трек 😔 (подробности в логах)")
    return a


async def _ffmpeg(*args) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        raise DownloadError("VK долго отдаёт трек, попробуй ещё раз 😔")
    if proc.returncode != 0:
        log.warning("ffmpeg: %s", (err or b"").decode(errors="ignore")[-400:])
        raise DownloadError("Не удалось скачать трек из VK 😔")


async def process_vk(url: str, preferred_quality: int = 192, progress_cb=None, meta: dict = None):
    a = await _vk_resolve(url)
    artist = (a.get("artist") or "").strip()
    title = (a.get("title") or "audio").strip()
    duration = int(a.get("duration") or 0)
    quality = _pick_quality(duration, preferred_quality) if duration else preferred_quality
    if duration and quality is None:
        raise DownloadError(f"Слишком длинный трек ({duration // 60} мин) — не влезет в 50 МБ 😔")
    if progress_cb:
        try:
            await progress_cb(f"⬇️ <i>Скачиваю «{html.escape(title)}» из VK…</i>")
        except Exception:
            pass

    tmp = tempfile.mkdtemp(prefix="mp3bot_")
    try:
        cover = None
        cover_url = _vk_cover(a)
        if cover_url:
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(cover_url) as resp:
                        if resp.status == 200:
                            cover = os.path.join(tmp, "cover.jpg")
                            with open(cover, "wb") as f:
                                f.write(await resp.read())
            except Exception:
                cover = None

        path = os.path.join(tmp, "track.mp3")
        args = (["-http_proxy", VK_PROXY] if VK_PROXY else []) + ["-user_agent", VK_UA,
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto,hls",
                "-i", a["url"]]
        if cover:
            args += ["-i", cover, "-map", "0:a", "-map", "1:v", "-c:v", "mjpeg",
                     "-disposition:v", "attached_pic",
                     "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
        else:
            args += ["-map", "0:a"]
        args += ["-c:a", "libmp3lame", "-b:a", f"{quality}k", "-id3v2_version", "3",
                 "-metadata", f"title={title}", "-metadata", f"artist={artist}", path]
        await _ffmpeg(*args)

        size_mb = os.path.getsize(path) / 1024 / 1024
        if size_mb > 49.5:
            raise DownloadError("Файл больше 50 МБ — Telegram не примет 😔")
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    return {
        "path": path,
        "tmpdir": tmp,
        "title": title,
        "performer": artist,
        "duration": duration or None,
        "quality": quality,
        "size_mb": round(size_mb, 1),
        "source_url": "https://vk.com/audio" + _vk_full_id(a),
    }



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
        return "Этот сайт не поддерживается 😕\nПопробуй YouTube, VK, SoundCloud или ссылку на трек Spotify."
    if "video unavailable" in msg or "not available" in msg or "removed" in msg:
        return "Контент недоступен — удалён или приватный 😔"
    if "private" in msg:
        return "Контент приватный, скачать не получится 😔"
    if "age" in msg and ("confirm" in msg or "restrict" in msg):
        return "Возрастное ограничение — скачивание недоступно 😔"
    if "sign in" in msg or "login" in msg or "cookies" in msg or "not a bot" in msg:
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
    """Поиск треков списком: VK (если есть VK_TOKEN) + YouTube + SoundCloud.
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

    vk, yt, sc = await asyncio.gather(vk_search(query, 30), one("ytsearch", 25, "yt"),
                                      one("scsearch", 30, "sc"))

    # чередуем VK, YouTube и SoundCloud, убираем дубли
    merged, seen = [], set()
    for i in range(max(len(vk), len(yt), len(sc))):
        for lst in (vk, yt, sc):
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


async def process_url(url: str, preferred_quality: int = 192, progress_cb=None, loop=None,
                      search_query: str = None, meta: dict = None):
    """
    Возвращает dict: path, tmpdir, title, performer, duration, quality, size_mb.
    Вызывающий обязан удалить tmpdir после отправки!
    """
    loop = loop or asyncio.get_running_loop()

    if search_query:
        candidates = [f"scsearch1:{search_query}", f"ytsearch1:{search_query}"]
    elif is_spotify(url):
        query = await resolve_spotify(url)
        log.info("spotify -> %s", query)
        candidates = [f"scsearch1:{query}", f"ytsearch1:{query}"]
    elif url.startswith(VK_PREFIX) or VK_AUDIO_RE.search(url):
        return await process_vk(url, preferred_quality, progress_cb, meta)
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
