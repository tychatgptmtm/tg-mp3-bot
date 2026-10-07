"""
Скачивание и конвертация в MP3: yt-dlp + FFmpeg.

Поддержка:
  • YouTube / YouTube Music — напрямую + обход блокировки датацентров
  • VK Видео (vk.com/video, vkvideo.ru) — напрямую
  • SoundCloud — напрямую
  • Spotify — ссылка не содержит аудио: берём название трека через
    Spotify oEmbed и ищем лучшую версию (YouTube, затем SoundCloud).

Обход блокировки YouTube («Sign in to confirm you're not a bot»):
  1. Пробуем разные клиенты YouTube (android / tv / ios / web_safari …)
     — у них разные требования к PO-токенам, какой-нибудь обычно проходит.
  2. Если ничего не прошло — качаем аудиопоток через публичные Piped API.
  3. Опционально можно задать PROXY_URL (прокси для исходящих запросов).
"""
import asyncio
import logging
import os
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import yt_dlp

log = logging.getLogger("downloader")

MAX_FILE_MB = 48                 # запас до лимита Telegram 50 МБ
MAX_BYTES = MAX_FILE_MB * 1024 * 1024
URL_RE = re.compile(r"https?://[^\s<>\"']+")

PROXY = os.environ.get("PROXY_URL", "").strip() or None

FALLBACK_QUALITIES = [320, 256, 192, 128, 96, 64]
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ytdl")

# --- Обход блокировки YouTube: наборы клиентов (у них разные требования) ---
YT_CLIENT_SETS = [
    None,                      # клиенты по умолчанию текущей версии yt-dlp
    ["android"],               # android-клиент чаще всего проходит без PO-токена
    ["tv"],                    # Smart TV клиент
    ["ios"],
    ["web_safari"],
    ["web_embedded"],
    ["mweb"],
]

# --- Резерв: публичные Piped API (отдают прямой аудиопоток) ---
PIPED_API = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.private.coffee",
    "https://pipedapi.drgns.space",
]

YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be|music\.youtube\.com)", re.I)

# ошибки, при которых имеет смысл попробовать другой клиент / другой путь
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
    """Spotify отдаёт только метаданные: берём название трека для поиска."""
    if not re.search(r"(open\.spotify\.com|spotify:)/(?:intl-[a-z-]+/)?track/", url):
        raise DownloadError(
            "Это ссылка на альбом или плейлист Spotify 😕\n"
            "Пришли ссылку на конкретный трек: <code>open.spotify.com/track/…</code>"
        )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://open.spotify.com/oembed",
                params={"url": url},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except Exception:
        raise DownloadError("Не удалось получить данные трека из Spotify 😔 Попробуй ещё раз через минуту.")
    title = (data or {}).get("title")
    if not title:
        raise DownloadError("Не удалось распознать этот трек Spotify 😔")
    return title


# ------------------------------------------------------------------ утилиты

def _is_youtube(url: str) -> bool:
    return url.startswith("ytsearch") or bool(YOUTUBE_RE.search(url or ""))


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in RETRY_MARKERS)


def _youtube_id(url: str):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def _pick_quality(duration: int, preferred: int):
    """Максимальное качество, при котором файл уложится в лимит Telegram."""
    for q in sorted(set(FALLBACK_QUALITIES + [preferred]), reverse=True):
        if q > preferred:
            continue
        if int(duration) * q * 1000 // 8 <= MAX_BYTES:
            return q
    return None


# ------------------------------------------------------------------ yt-dlp (в executor)

def _base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "nocheckcertificate": True,
    }
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
    """Пробуем YouTube с разными клиентами, пока какой-нибудь не пройдёт."""
    attempts = YT_CLIENT_SETS if _is_youtube(url) else [None]
    err = None
    for clients in attempts:
        try:
            info = await _run(_probe_sync, url, clients)
            return info, clients
        except Exception as e:
            err = e
            log.info("probe %s: клиенты %s не сработали: %s",
                     url[:80], clients, str(e)[:120])
            if not _is_retryable(e):
                raise
    raise err


async def _download_with_fallback(url, tmp, quality, hook, clients) -> None:
    """Скачивание с ротацией клиентов YouTube (начинаем с удачного при probe)."""
    attempts = YT_CLIENT_SETS if _is_youtube(url) else [None]
    order = [clients] + [a for a in attempts if a != clients]
    err = None
    for c in order:
        try:
            await _run(_download_sync, url, tmp, quality, hook, c)
            return
        except Exception as e:
            err = e
            log.info("download %s: клиенты %s не сработали: %s",
                     url[:80], c, str(e)[:120])
            for f in os.listdir(tmp):          # чистим недокачанное
                try:
                    os.remove(os.path.join(tmp, f))
                except OSError:
                    pass
            if not _is_retryable(e):
                raise
    raise err


def _make_hook(loop: asyncio.AbstractEventLoop, cb, title: str):
    """Прогресс скачивания → редкие правки статусного сообщения."""
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


# ------------------------------------------------------------------ резерв: Piped API

async def _http_download(url: str, dest: str, progress_cb=None, label: str = "") -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=180)) as resp:
                if resp.status != 200:
                    return False
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                last_pct = -100
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        f.write(chunk)
                        done += len(chunk)
                        if progress_cb and total:
                            pct = done * 100 // total
                            if pct - last_pct >= 20:
                                last_pct = pct
                                try:
                                    await progress_cb(f"⬇️ <i>Скачиваю {label} — {pct}%</i>")
                                except Exception:
                                    pass
        return True
    except Exception as e:
        log.info("http download %s: %s", url[:80], str(e)[:120])
        return False


async def _ffmpeg_mp3(raw: str, cover, quality: int, title: str, performer: str):
    """Конвертация сырого потока в MP3 с тегами и обложкой."""
    out = raw + ".mp3"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", raw]
    if cover and os.path.exists(cover):
        cmd += ["-i", cover, "-map", "0:a", "-map", "1:v", "-c:v", "mjpeg"]
    else:
        cmd += ["-map", "0:a"]
    cmd += ["-c:a", "libmp3lame", "-b:a", f"{quality}k", "-id3v2_version", "3"]
    if title:
        cmd += ["-metadata", f"title={title}"]
    if performer:
        cmd += ["-metadata", f"artist={performer}"]
    cmd += [out]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await proc.wait() != 0:
            return None
    except Exception:
        return None
    return out if os.path.exists(out) else None


async def _piped_download(video_id, tmp: str, quality: int, progress_cb=None):
    """Резервный путь: аудиопоток через публичные Piped API. None = не вышло."""
    if not video_id:
        return None
    for base in PIPED_API:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{base}/streams/{video_id}",
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
            if not isinstance(data, dict) or data.get("error"):
                continue
            streams = [s for s in data.get("audioStreams") or [] if s.get("url")]
            if not streams:
                continue
            best = max(streams, key=lambda s: int(s.get("bitrate") or 0))
            title = data.get("title") or "audio"
            performer = data.get("uploader") or ""
            duration = int(data.get("duration") or 0)
            thumb = data.get("thumbnailUrl")

            raw = os.path.join(tmp, f"{video_id}_raw")
            if not await _http_download(best["url"], raw, progress_cb, "«" + title[:30] + "»"):
                continue
            cover = None
            if thumb:
                cover = os.path.join(tmp, "cover.jpg")
                if not await _http_download(thumb, cover):
                    cover = None
            out = await _ffmpeg_mp3(raw, cover, quality, title, performer)
            if not out:
                continue
            try:
                os.remove(raw)
            except OSError:
                pass
            return {
                "path": out, "title": title, "performer": performer,
                "duration": duration or None, "quality": quality,
            }
        except Exception as e:
            log.info("piped %s: %s", base, str(e)[:120])
            continue
    return None


# ------------------------------------------------------------------ человекочитаемые ошибки

def _friendly_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "unsupported url" in msg:
        return ("Этот сайт не поддерживается 😕\n"
                "Попробуй YouTube, VK Видео, SoundCloud или ссылку на трек Spotify.")
    if "video unavailable" in msg or "not available" in msg or "removed" in msg:
        return "Контент недоступен — удалён или приватный 😔"
    if "private" in msg:
        return "Контент приватный, скачать не получится 😔"
    if "age" in msg and ("confirm" in msg or "restrict" in msg):
        return "Возрастное ограничение — скачивание недоступно 😔"
    if "geo" in msg and "restrict" in msg:
        return "Контент недоступен в этом регионе 😔"
    if "sign in" in msg or "login" in msg or "cookies" in msg or "not a bot" in msg:
        return ("YouTube сейчас блокирует запросы с сервера 😔\n"
                "Нажми «повторить» через минуту — обычно проходит. "
                "Или пришли этот трек из VK / SoundCloud.")
    if "timeout" in msg or "timed out" in msg:
        return "Источник долго не отвечает, попробуй ещё раз 😔"
    if "no video" in msg or "no audio" in msg or "no media" in msg:
        return "Не нашёл аудио/видео по этой ссылке 😕"
    log.warning("Ошибка yt-dlp: %s", str(exc)[:300])
    return "Не удалось скачать 😔 Попробуй другую ссылку или повтори позже."


# ------------------------------------------------------------------ основной пайплайн

async def process_url(url: str, preferred_quality: int = 192, progress_cb=None, loop=None):
    """
    Возвращает dict: path, tmpdir, title, performer, duration, quality, size_mb.
    Вызывающий обязан удалить tmpdir после отправки файла!
    """
    loop = loop or asyncio.get_running_loop()

    # --- определяем, что качать ---
    if is_spotify(url):
        search_title = await resolve_spotify(url)
        candidates = [
            f"ytsearch1:{search_title}",
            f"scsearch1:{search_title}",     # запасной поиск в SoundCloud
        ]
    else:
        candidates = [url]

    info = None
    clients = None
    last_err = None
    for cand in candidates:
        try:
            info, clients = await _probe_with_fallback(cand)
            url = cand
            break
        except Exception as e:
            last_err = e
            log.info("candidate %s не сработал: %s", cand[:60], str(e)[:120])
            if not _is_retryable(e):
                raise DownloadError(_friendly_error(e))
    if info is None:
        raise DownloadError(_friendly_error(last_err or Exception("empty")))

    # --- плейлист → первый трек ---
    if info.get("_type") == "playlist" or info.get("entries"):
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise DownloadError("По этой ссылке нет доступных треков 😕")
        first = entries[0]
        nested = first.get("url") or first.get("webpage_url") or first.get("id")
        if not str(nested).startswith("http"):
            nested = f"https://www.youtube.com/watch?v={nested}"
        try:
            info, clients = await _probe_with_fallback(str(nested))
            url = str(nested)
        except Exception as e:
            raise DownloadError(_friendly_error(e))

    duration = int(info.get("duration") or 0)
    quality = _pick_quality(duration, preferred_quality) if duration else preferred_quality
    if duration and quality is None:
        minutes = duration // 60
        raise DownloadError(
            f"Слишком длинный трек ({minutes} мин) — MP3 не влезет в лимит Telegram 50 МБ 😔"
        )

    title = info.get("title") or "audio"
    hook = _make_hook(loop, progress_cb, title)
    tmp = tempfile.mkdtemp(prefix="mp3bot_")
    meta = None

    # --- основной путь: yt-dlp с ротацией клиентов ---
    try:
        await _download_with_fallback(url, tmp, quality, hook, clients)
    except Exception as e:
        # --- последний резерв: Piped API (только YouTube) ---
        meta = None
        if _is_youtube(url) or is_spotify(url) or url.startswith("ytsearch"):
            video_id = _youtube_id(url) or (info.get("id") if re.fullmatch(r"[A-Za-z0-9_-]{11}", str(info.get("id") or "")) else None)
            if video_id:
                if progress_cb:
                    try:
                        await progress_cb("🛟 <i>Прямой путь заблокирован — иду в обход…</i>")
                    except Exception:
                        pass
                meta = await _piped_download(video_id, tmp, quality, progress_cb)
        if meta is None:
            shutil.rmtree(tmp, ignore_errors=True)
            raise DownloadError(_friendly_error(e))

    # --- собираем результат ---
    files = [f for f in os.listdir(tmp) if f.lower().endswith(".mp3")]
    if not files:
        shutil.rmtree(tmp, ignore_errors=True)
        raise DownloadError("Не удалось сконвертировать файл 😔 Попробуй другую ссылку.")

    path = os.path.join(tmp, files[0])
    size_mb = os.path.getsize(path) / 1024 / 1024
    if size_mb > 49.5:
        shutil.rmtree(tmp, ignore_errors=True)
        raise DownloadError("Файл получился больше 50 МБ — Telegram его не примет 😔")

    if meta is None:
        meta = {
            "title": title,
            "performer": info.get("artist") or info.get("uploader") or info.get("channel") or "",
            "duration": duration or None,
            "quality": quality,
        }

    return {
        "path": path,
        "tmpdir": tmp,
        "title": meta["title"],
        "performer": meta["performer"],
        "duration": meta["duration"],
        "quality": meta["quality"],
        "size_mb": round(size_mb, 1),
    }
