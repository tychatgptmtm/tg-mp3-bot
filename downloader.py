"""
Скачивание и конвертация в MP3: yt-dlp + FFmpeg.

Поддержка:
  • YouTube / YouTube Music — напрямую
  • VK Видео (vk.com/video, vkvideo.ru) — напрямую
  • SoundCloud — напрямую
  • Spotify — ссылки не содержат аудио, поэтому бот берёт название трека
    через Spotify oEmbed и ищет лучшую версию на YouTube.
"""
import asyncio
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import yt_dlp

MAX_FILE_MB = 48                 # запас до лимита Telegram 50 МБ
MAX_BYTES = MAX_FILE_MB * 1024 * 1024
URL_RE = re.compile(r"https?://[^\s<>\"']+")

FALLBACK_QUALITIES = [320, 256, 192, 128, 96, 64]
_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ytdl")


class DownloadError(Exception):
    """Ошибка с человекочитаемым сообщением для пользователя."""


# ------------------------------------------------------------------ Spotify

def is_spotify(url: str) -> bool:
    return "spotify.com" in url or url.startswith("spotify:")


async def resolve_spotify(url: str) -> str:
    """Spotify отдаёт только метаданные: берём название трека и ищем его на YouTube."""
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
    return f"ytsearch1:{title}"


# ------------------------------------------------------------------ качество под лимит

def _pick_quality(duration: int, preferred: int):
    """Максимальное качество, при котором файл уложится в лимит Telegram."""
    for q in sorted(set(FALLBACK_QUALITIES + [preferred]), reverse=True):
        if q > preferred:
            continue
        if int(duration) * q * 1000 // 8 <= MAX_BYTES:
            return q
    return None


# ------------------------------------------------------------------ yt-dlp (в executor)

def _probe_sync(url: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 25,
        "nocheckcertificate": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _download_sync(url: str, tmp: str, quality: int, hook):
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 25,
        "nocheckcertificate": True,
        "writethumbnail": True,
        "convert_thumbnails": "jpg",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(quality)},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "progress_hooks": [hook] if hook else [],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


async def _run(func, *args):
    return await asyncio.get_running_loop().run_in_executor(_executor, func, *args)


def _make_hook(loop: asyncio.AbstractEventLoop, cb, title: str):
    """Прогресс скачивания → редкие правки статусного сообщения (раз в ~3.5 с)."""
    state = {"last": -100.0, "fired": 0}

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
        state["fired"] += 1
        try:
            asyncio.run_coroutine_threadsafe(
                cb(f"⬇️ <i>Скачиваю «{title}» — {int(p)}%</i>"), loop
            )
        except Exception:
            pass

    return hook


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
    if "sign in" in msg or "login" in msg or "cookies" in msg:
        return "Источник требует авторизацию, скачать не получится 😔"
    if "timeout" in msg or "timed out" in msg:
        return "Источник долго не отвечает, попробуй ещё раз 😔"
    if "no video" in msg or "no audio" in msg or "no media" in msg:
        return "Не нашёл аудио/видео по этой ссылке 😕"
    log = __import__("logging").getLogger("downloader")
    log.warning("Ошибка yt-dlp: %s", str(exc)[:300])
    return "Не удалось скачать 😔 Попробуй другую ссылку или повтори позже."


# ------------------------------------------------------------------ основной пайплайн

async def process_url(url: str, preferred_quality: int = 192, progress_cb=None, loop=None):
    """
    Возвращает dict: path, tmpdir, title, performer, duration, quality, size_mb.
    Вызывающий обязан удалить tmpdir после отправки файла!
    """
    loop = loop or asyncio.get_running_loop()

    if is_spotify(url):
        url = await resolve_spotify(url)

    try:
        info = await _run(_probe_sync, url)
    except Exception as e:
        raise DownloadError(_friendly_error(e))

    # Плейлист → первый трек
    if info.get("_type") == "playlist" or info.get("entries"):
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise DownloadError("По этой ссылке нет доступных треков 😕")
        first = entries[0]
        nested = first.get("url") or first.get("webpage_url") or first.get("id")
        if not str(nested).startswith("http"):
            nested = f"https://www.youtube.com/watch?v={nested}"
        try:
            info = await _run(_probe_sync, str(nested))
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

    try:
        await _run(_download_sync, url, tmp, quality, hook)
    except Exception as e:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        raise DownloadError(_friendly_error(e))

    files = [f for f in os.listdir(tmp) if f.lower().endswith(".mp3")]
    if not files:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        raise DownloadError("Не удалось сконвертировать файл 😔 Попробуй другую ссылку.")

    path = os.path.join(tmp, files[0])
    size_mb = os.path.getsize(path) / 1024 / 1024
    if size_mb > 49.5:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        raise DownloadError("Файл получился больше 50 МБ — Telegram его не примет 😔")

    return {
        "path": path,
        "tmpdir": tmp,
        "title": title,
        "performer": info.get("artist") or info.get("uploader") or info.get("channel") or "",
        "duration": duration or None,
        "quality": quality,
        "size_mb": round(size_mb, 1),
    }
