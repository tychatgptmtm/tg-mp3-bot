"""
Скачивание и конвертация в MP3: yt-dlp + FFmpeg + Cobalt API (обход блокировок).

Поддержка:
  • YouTube / YouTube Music
  • VK (vk.com/video, vkvideo.ru, vk.com/audio, vk.ru)
  • SoundCloud
  • Spotify — берём название через oEmbed и ищем на YouTube / SoundCloud / VK

Обход блокировки YouTube с серверных IP (Render и т.п.):
  1. yt-dlp с ротацией клиентов (android / tv / ios / web_safari / mweb)
  2. Cobalt API — внешний сервис качает за нас (наш IP не участвует)
  3. Piped API — прямой аудиопоток с публичных зеркал
"""
import asyncio
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

RETRY_MARKERS = (
    "sign in", "not a bot", "confirm you", "login", "cookie",
    "http error 403", "http error 429", "http error 400",
    "no video formats", "requested format", "player response",
    "timeout", "timed out", "premiere", "live event",
)


class DownloadError(Exception):
    """Ошибка с человекочитаемым сообщением для пользователя."""


# ------------------------------------------------------------------ Cobalt API

# Публичные инстансы Cobalt (качают сами, отдают нам ссылку на файл)
COBALT_INSTANCES = [
    "https://cobalt-api.kwiatekmiki.com",
    "https://cobalt-backend.canine.tools",
    "https://capi.oak.li",
    "https://cobalt.api.timelessnesses.me",
    "https://downloadapi.stuff.solutions",
    "https://cobalt.api.meowing.de",
    "https://api.dl.ixhby.dev",
    "https://capi.3kh0.net",
    "https://cobalt-api.ayo.tf",
    "https://api.cobalt.best",
]

_cobalt_bases: list = []  # кэш рабочих инстансов


async def _refresh_cobalt_instances():
    """Подтягиваем актуальный список инстансов Cobalt из их официального реестра."""
    global _cobalt_bases
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://instances.cobalt.best/api/instances.json",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json(content_type=None)
        fresh = []
        for inst in data if isinstance(data, list) else []:
            if not isinstance(inst, dict):
                continue
            api = inst.get("api") or ""
            services = inst.get("services") or {}
            if api and inst.get("api_online", True) and services.get("youtube", True):
                fresh.append(api.rstrip("/"))
        if fresh:
            _cobalt_bases = fresh
            log.info("Cobalt: загружено %d инстансов из реестра", len(fresh))
    except Exception as e:
        log.info("Cobalt: не удалось обновить список инстансов: %s", e)


def _cobalt_candidates() -> list:
    seen, out = set(), []
    for base in _cobalt_bases + COBALT_INSTANCES:
        b = base.rstrip("/")
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


async def _cobalt_download(url: str, tmp: str, progress_cb=None):
    """Качаем через Cobalt: POST / {url, downloadMode: audio, audioFormat: mp3}."""
    payload = {
        "url": url,
        "downloadMode": "audio",
        "audioFormat": "mp3",
        "audioBitrate": "320",
        "filenameStyle": "basic",
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    for base in _cobalt_candidates():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    base + "/", json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
            status = data.get("status")
            file_url = None
            if status in ("tunnel", "redirect"):
                file_url = data.get("url")
            elif status == "picker":
                picks = data.get("picker") or []
                audio = [p for p in picks if p.get("type") == "audio"]
                file_url = (audio[0] if audio else picks[0]).get("url") if picks else None
            if not file_url:
                continue

            dest = os.path.join(tmp, "cobalt.mp3")
            ok = await _http_download(file_url, dest, progress_cb, "через Cobalt")
            if not ok or not os.path.exists(dest) or os.path.getsize(dest) < 50_000:
                continue

            title = (data.get("filename") or "audio").rsplit(".", 1)[0]
            return {
                "path": dest,
                "title": title,
                "performer": "",
                "duration": None,
                "quality": 320,
            }
        except Exception as e:
            log.info("cobalt %s: %s", base, str(e)[:120])
            continue
    return None


# ------------------------------------------------------------------ Spotify

def is_spotify(url: str) -> bool:
    return "spotify.com" in url or url.startswith("spotify:")


async def resolve_spotify(url: str) -> str:
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
        raise DownloadError("Не удалось получить данные трека из Spotify 😔")
    title = (data or {}).get("title")
    if not title:
        raise DownloadError("Не удалось распознать этот трек Spotify 😔")
    return title


# ------------------------------------------------------------------ VK audio/topic

VK_AUDIO_RE = re.compile(r"(?:vk\.com|vk\.ru|m\.vk\.com)/(audio|topic|wall)-?\d+_\d+", re.I)


async def resolve_vk_title(url: str) -> str:
    """VK oEmbed возвращает название трека/топика — используем для поиска."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://vk.com/oembed.php",
                params={"url": url, "format": "json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json(content_type=None)
        title = (data or {}).get("title") or ""
        # «Zavet — Родная, пой» → чистим лишнее
        title = re.sub(r"\s*[|•]\s*ВКонтакте.*$", "", title).strip()
        return title
    except Exception:
        return ""


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
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "nocheckcertificate": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Mobile Safari/537.36"
            )
        },
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


# ------------------------------------------------------------------ HTTP + ffmpeg

async def _http_download(url: str, dest: str, progress_cb=None, label: str = "") -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    return False
                total = int(resp.headers.get("Content-Length") or 0)
                done, last_pct = 0, -100
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
        log.info("http download: %s", str(e)[:120])
        return False


async def _ffmpeg_mp3(raw: str, cover, quality: int, title: str, performer: str):
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
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        if await proc.wait() != 0:
            return None
    except Exception:
        return None
    return out if os.path.exists(out) else None


# ------------------------------------------------------------------ Piped API

PIPED_API = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.private.coffee",
    "https://pipedapi.drgns.space",
]


async def _piped_download(video_id, tmp: str, quality: int, progress_cb=None):
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
            if not await _http_download(best["url"], raw, progress_cb, f"«{title[:30]}»"):
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
        return "Источник временно блокирует сервер 😔 Попробуй через минуту или пришли трек из другого сервиса."
    if "timeout" in msg or "timed out" in msg:
        return "Источник долго не отвечает, попробуй ещё раз 😔"
    log.warning("Ошибка: %s", str(exc)[:300])
    return "Не удалось скачать 😔 Попробуй другую ссылку или повтори позже."


# ------------------------------------------------------------------ основной пайплайн

async def process_url(url: str, preferred_quality: int = 192, progress_cb=None, loop=None):
    """
    Возвращает dict: path, tmpdir, title, performer, duration, quality, size_mb.
    Вызывающий обязан удалить tmpdir после отправки!
    """
    loop = loop or asyncio.get_running_loop()
    await _refresh_cobalt_instances()

    # ---- Spotify: название -> поиск на YouTube / SoundCloud / VK ----
    if is_spotify(url):
        search_title = await resolve_spotify(url)
        candidates = [
            f"ytsearch1:{search_title}",
            f"scsearch1:{search_title}",
            f"https://vk.com/video?q={search_title}",
        ]
    # ---- VK audio/topic: резолвим название и ищем ----
    elif VK_AUDIO_RE.search(url):
        if progress_cb:
            try:
                await progress_cb("🔍 <i>Распознаю трек VK…</i>")
            except Exception:
                pass
        title = await resolve_vk_title(url)
        if not title:
            raise DownloadError("Не удалось распознать трек VK 😔 Попробуй ссылку на видео или другой сервис.")
        candidates = [
            f"ytsearch1:{title}",
            f"scsearch1:{title}",
            f"https://vk.com/video?q={title}",
        ]
    else:
        candidates = [url]

    info, clients, last_err = None, None, None
    for cand in candidates:
        try:
            info, clients = await _probe_with_fallback(cand)
            url = cand
            break
        except Exception as e:
            last_err = e
            log.info("candidate %s: %s", cand[:60], str(e)[:120])
            if not _is_retryable(e):
                break

    # ---- если probe упал по блокировке — Cobalt сразу с исходной ссылкой ----
    if info is None and not is_spotify(url) and not VK_AUDIO_RE.search(url):
        if progress_cb:
            try:
                await progress_cb("🛟 <i>Прямой путь заблокирован — иду через Cobalt…</i>")
            except Exception:
                pass
        tmp = tempfile.mkdtemp(prefix="mp3bot_")
        meta = await _cobalt_download(url, tmp, progress_cb)
        if meta:
            return _finalize(meta, tmp)
        shutil.rmtree(tmp, ignore_errors=True)
        raise DownloadError(_friendly_error(last_err or Exception("empty")))

    if info is None:
        raise DownloadError(_friendly_error(last_err or Exception("empty")))

    # ---- плейлист → первый трек ----
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
        raise DownloadError(f"Слишком длинный трек ({minutes} мин) — не влезет в 50 МБ 😔")

    title = info.get("title") or "audio"
    hook = _make_hook(loop, progress_cb, title)
    tmp = tempfile.mkdtemp(prefix="mp3bot_")

    # ---- 1. yt-dlp ----
    try:
        await _download_with_fallback(url, tmp, quality, hook, clients)
    except Exception as e:
        log.info("yt-dlp failed: %s", str(e)[:200])

        # ---- 2. Cobalt ----
        meta = None
        if progress_cb:
            try:
                await progress_cb("🛟 <i>Прямой путь заблокирован — иду через Cobalt…</i>")
            except Exception:
                pass
        meta = await _cobalt_download(url, tmp, progress_cb)

        # ---- 3. Piped (только YouTube) ----
        if meta is None and (_is_youtube(url) or url.startswith("ytsearch")):
            video_id = _youtube_id(url) or (
                info.get("id") if re.fullmatch(r"[A-Za-z0-9_-]{11}", str(info.get("id") or "")) else None
            )
            if video_id:
                if progress_cb:
                    try:
                        await progress_cb("🛟 <i>Иду через Piped…</i>")
                    except Exception:
                        pass
                meta = await _piped_download(video_id, tmp, quality, progress_cb)

        if meta is None:
            shutil.rmtree(tmp, ignore_errors=True)
            raise DownloadError(_friendly_error(e))
        return _finalize(meta, tmp)

    # ---- результат yt-dlp ----
    files = [f for f in os.listdir(tmp) if f.lower().endswith(".mp3")]
    if not files:
        shutil.rmtree(tmp, ignore_errors=True)
        raise DownloadError("Не удалось сконвертировать файл 😔")
    path = os.path.join(tmp, files[0])
    size_mb = os.path.getsize(path) / 1024 / 1024
    if size_mb > 49.5:
        shutil.rmtree(tmp, ignore_errors=True)
        raise DownloadError("Файл больше 50 МБ — Telegram не примет 😔")

    return {
        "path": path,
        "tmpdir": tmp,
        "title": title,
        "performer": info.get("artist") or info.get("uploader") or info.get("channel") or "",
        "duration": duration or None,
        "quality": quality,
        "size_mb": round(size_mb, 1),
    }


def _finalize(meta: dict, tmp: str) -> dict:
    """Общая сборка результата для Cobalt/Piped."""
    size_mb = os.path.getsize(meta["path"]) / 1024 / 1024
    return {
        "path": meta["path"],
        "tmpdir": tmp,
        "title": meta["title"],
        "performer": meta.get("performer", ""),
        "duration": meta.get("duration"),
        "quality": meta.get("quality", 320),
        "size_mb": round(size_mb, 1),
    }
