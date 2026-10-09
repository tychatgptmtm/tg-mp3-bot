"""
Тексты песен: Genius (основной источник) + LRCLIB (запасной, если Genius недоступен).

Переменные окружения:
  GENIUS_TOKEN  — Client Access Token с https://genius.com/api-clients (рекомендуется:
                  без него поиск Genius с серверных IP часто блокируется)
  GENIUS_PROXY  — опционально: прокси для запросов к genius.com (иначе берётся PROXY_URL)
  GENIUS_MIRRORS — опционально: зеркала Genius через запятую (dumb / intellectual)

Genius блокирует IP датацентров (Render и т.п.) — тогда тексты Genius берутся через
открытые зеркала-фронтенды (dumb, intellectual), которые отдают те же страницы Genius.
"""
import logging
import os
import re
import time
from html import unescape
from html.parser import HTMLParser

import aiohttp

log = logging.getLogger("lyrics")

GENIUS_TOKEN = os.environ.get("GENIUS_TOKEN", "").strip()
GENIUS_PROXY = (os.environ.get("GENIUS_PROXY", "").strip()
                or os.environ.get("PROXY_URL", "").strip() or None)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_TIMEOUT = aiohttp.ClientTimeout(total=12)
GENIUS_MIRRORS = [m.strip().rstrip("/") for m in (
    os.environ.get("GENIUS_MIRRORS", "").strip()
    or "https://dumb.bloat.cat,https://dumb.canine.tools,https://genius.fsky.io,"
       "https://dumb.artemislena.eu,https://intellectual.insprill.net"
).split(",") if m.strip()]
_genius_blocked = {"until": 0.0}   # genius.com отвечает 403 — не дёргаем его какое-то время


class LyricsError(Exception):
    pass


# ------------------------------------------------------------------ нормализация запроса

_JUNK_RE = re.compile(
    r"[\(\[][^\)\]]*(official|video|audio|lyrics?|клип|текст|премьера|prod\.?|hd|hq|4k|"
    r"mv|visuali[sz]er|remaster(ed)?)[^\)\]]*[\)\]]", re.I)


def clean_query(artist: str, title: str) -> str:
    t = _JUNK_RE.sub("", title or "")
    t = re.sub(r"\s+(ft\.?|feat\.?)\s.*$", "", t, flags=re.I)
    q = f"{artist or ''} {t}".strip()
    return re.sub(r"\s+", " ", q)[:150]


def _norm(s: str) -> str:
    return re.sub(r"\W+", "", (s or "").lower())


# ------------------------------------------------------------------ Genius

async def _genius_search(session: aiohttp.ClientSession, query: str):
    """Возвращает список хитов [{title, artist, url}]."""
    if GENIUS_TOKEN:
        async with session.get("https://api.genius.com/search", params={"q": query},
                               headers={"Authorization": f"Bearer {GENIUS_TOKEN}"}) as r:
            if r.status != 200:
                raise LyricsError(f"genius api {r.status}")
            data = await r.json(content_type=None)
        hits = data.get("response", {}).get("hits", [])
    else:
        async with session.get("https://genius.com/api/search/multi", params={"q": query},
                               proxy=GENIUS_PROXY) as r:
            if r.status != 200:
                raise LyricsError(f"genius search {r.status}")
            data = await r.json(content_type=None)
        hits = []
        for sec in data.get("response", {}).get("sections", []):
            if sec.get("type") in ("top_hit", "song"):
                hits += [h for h in sec.get("hits", []) if h.get("type") == "song"]
    out = []
    for h in hits:
        res = h.get("result") or {}
        if res.get("url") and res.get("title"):
            out.append({"title": res["title"],
                        "artist": (res.get("primary_artist") or {}).get("name") or res.get("artist_names", ""),
                        "url": res["url"]})
    return out


class _LyricsParser(HTMLParser):
    """Собирает текст из <div data-lyrics-container="true">, <br> → перенос строки."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0          # глубина внутри контейнера с текстом
        self.skip = 0           # глубина внутри исключённого блока (шапка, «Contributors» и т.п.)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.depth == 0:
            if tag == "div" and a.get("data-lyrics-container") == "true":
                self.depth = 1
                if self.parts:
                    self.parts.append("\n")
            return
        if tag in ("br",):
            if not self.skip:
                self.parts.append("\n")
            return
        if tag in ("img", "input", "meta", "link", "hr"):
            return
        self.depth += 1
        if self.skip:
            self.skip += 1
        elif a.get("data-exclude-from-selection") == "true":
            self.skip = 1

    def handle_endtag(self, tag):
        if self.depth == 0 or tag in ("br", "img", "input", "meta", "link", "hr"):
            return
        self.depth -= 1
        if self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.depth and not self.skip:
            self.parts.append(data)


def _parse_genius_page(page: str) -> str:
    p = _LyricsParser()
    p.feed(page)
    text = unescape("".join(p.parts))
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


async def _genius_lyrics(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, proxy=GENIUS_PROXY) as r:
        if r.status != 200:
            raise LyricsError(f"genius page {r.status}")
        page = await r.text()
    return _parse_genius_page(page)


# ------------------------------------------------------------------ зеркала Genius

_DUMB_ITEM_RE = re.compile(
    r'<a id="search-item" href="(/[^"?#]+-lyrics)"><img[^>]*><div><span>(.*?)</span><h3>(.*?)</h3>', re.S)
_INTEL_ITEM_RE = re.compile(r'href="(/[^"?#]+-lyrics)\?id=\d+"')


def _strip_tags(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


async def _mirror_search(session: aiohttp.ClientSession, query: str):
    """Ищет песню на зеркалах. Возвращает список путей вида /Artist-song-lyrics."""
    fallback = []
    for base in GENIUS_MIRRORS:
        try:
            async with session.get(f"{base}/search", params={"q": query}) as r:
                if r.status != 200:
                    log.info("mirror %s: search %s", base, r.status)
                    continue
                page = await r.text()
        except Exception as e:
            log.info("mirror %s: %s", base, str(e)[:120])
            continue
        paths = [m.group(1) for m in _DUMB_ITEM_RE.finditer(page)]
        paths += [p for p in _INTEL_ITEM_RE.findall(page) if p not in paths]
        # переводы/романизации («Genius Romanizations», «Genius Translations») — в конец
        own = [p for p in paths if not p.lower().startswith("/genius-")]
        if own:
            return own + [p for p in paths if p not in own]
        if paths:
            fallback = fallback or paths
    return fallback


class _DumbParser(HTMLParser):
    """Страница dumb: <div id="lyrics">…</div>, артист в <h2>, название в <h1> внутри #metadata-info."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts = []
        self.meta = None
        self.title = ""
        self.artist = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.depth:
            if tag == "br":
                self.parts.append("\n")
            elif tag not in ("img", "hr"):
                self.depth += 1
            return
        if tag == "div" and a.get("id") == "lyrics":
            self.depth = 1
        elif tag in ("h1", "h2") and not (self.title if tag == "h1" else self.artist):
            self.meta = tag

    def handle_endtag(self, tag):
        if self.depth and tag not in ("br", "img", "hr"):
            self.depth -= 1
        if tag == self.meta:
            self.meta = None

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)
        elif self.meta == "h1":
            self.title += data
        elif self.meta == "h2":
            self.artist += data


def _parse_dumb(page: str):
    p = _DumbParser()
    p.feed(page)
    text = re.sub(r"[ \t]+\n", "\n", "".join(p.parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return p.artist.strip(), p.title.strip(), text


async def _mirror_lyrics(session: aiohttp.ClientSession, path: str):
    for base in GENIUS_MIRRORS:
        if "intellectual" in base:
            continue
        try:
            async with session.get(base + path) as r:
                if r.status != 200:
                    log.info("mirror %s%s: %s", base, path, r.status)
                    continue
                page = await r.text()
        except Exception as e:
            log.info("mirror %s: %s", base, str(e)[:120])
            continue
        artist, title, text = _parse_dumb(page)
        if text:
            return artist, title, text
    return None


# ------------------------------------------------------------------ LRCLIB (запасной)

async def _lrclib(session: aiohttp.ClientSession, query: str):
    async with session.get("https://lrclib.net/api/search", params={"q": query}) as r:
        if r.status != 200:
            return None
        data = await r.json(content_type=None)
    items = [it for it in (data or []) if it.get("plainLyrics")]
    if re.search(r"[а-яё]", query, re.I):  # кириллический запрос — сначала тексты на кириллице
        items.sort(key=lambda it: not re.search(r"[а-яё]", it["plainLyrics"], re.I))
    for it in items:
        if it.get("plainLyrics"):
            return {"title": it.get("trackName") or "", "artist": it.get("artistName") or "",
                    "text": it["plainLyrics"].strip()}
    return None


# ------------------------------------------------------------------ публичная функция

async def find_lyrics(query: str) -> dict:
    """Ищет текст песни. Возвращает dict(title, artist, text, url, source)."""
    query = re.sub(r"\s+", " ", query or "").strip()[:150]
    if len(query) < 2:
        raise LyricsError("Слишком короткий запрос 😕 Напиши исполнителя и название.")

    genius_url, title, artist = None, "", ""
    async with aiohttp.ClientSession(timeout=_TIMEOUT, headers={"User-Agent": UA}) as session:
        # 1) напрямую через Genius (работает с токеном / прокси / не с датацентра)
        direct = GENIUS_TOKEN or GENIUS_PROXY or time.time() > _genius_blocked["until"]
        if direct:
            try:
                hits = await _genius_search(session, query)
                if hits:
                    qn = _norm(query)
                    best = next((h for h in hits if _norm(h["artist"]) and _norm(h["artist"]) in qn), hits[0])
                    genius_url, title, artist = best["url"], best["title"], best["artist"]
                    text = await _genius_lyrics(session, genius_url)
                    if text:
                        return {"title": title, "artist": artist, "text": text,
                                "url": genius_url, "source": "Genius"}
            except Exception as e:
                log.warning("Genius напрямую: %s", str(e)[:200])
                if "403" in str(e):
                    _genius_blocked["until"] = time.time() + 6 * 3600

        # 2) Genius через зеркала
        try:
            if genius_url:
                paths = ["/" + genius_url.split("genius.com/", 1)[-1].lstrip("/")]
            else:
                paths = await _mirror_search(session, query)
            for path in paths[:2]:
                got = await _mirror_lyrics(session, path)
                if got:
                    a, t, text = got
                    return {"title": t or title, "artist": a or artist, "text": text,
                            "url": "https://genius.com" + path, "source": "Genius"}
            if paths and not genius_url:
                genius_url = "https://genius.com" + paths[0]
        except Exception as e:
            log.warning("Genius через зеркала: %s", str(e)[:200])

        # 3) запасной источник
        try:
            lr = await _lrclib(session, f"{artist} {title}".strip() if title else query)
            if not lr and title:
                lr = await _lrclib(session, query)
        except Exception as e:
            log.warning("LRCLIB: %s", str(e)[:200])
            lr = None
        if lr:
            return dict(lr, url=genius_url, source="LRCLIB")

    if genius_url:
        raise LyricsError("Не смог вытащить текст 😔", genius_url)
    raise LyricsError("Текст не нашёлся 😕 Попробуй: /lyrics исполнитель — название")
