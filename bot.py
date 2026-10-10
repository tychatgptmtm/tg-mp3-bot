"""
MP3 Downloader Bot 🎧
Telegram-бот: пришлите ссылку (YouTube / Spotify / SoundCloud) или название — получите MP3 и текст песни.

Переменные окружения:
  BOT_TOKEN      — токен от @BotFather (обязателен)
  ADMIN_IDS      — ID администраторов через запятую
  DB_PATH        — путь к базе SQLite (по умолчанию data/bot.db)
  PORT           — порт health-check сервера (по умолчанию 10000)
  PROXY_URL      — опционально: прокси для исходящих запросов yt-dlp
  GENIUS_TOKEN   — опционально: токен Genius API для текстов песен
  DOWNLOAD_SLOTS — сколько треков качать одновременно (по умолчанию 3)
"""
import asyncio
import html
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultCachedAudio,
    InlineQueryResultsButton,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.chat_action import ChatActionSender

from database import db
from downloader import DownloadError, URL_RE, process_url, search_tracks, track_name_from_url
from lyrics import LyricsError, clean_query, find_lyrics

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ADMIN_IDS", "")) if x.strip()}
DOWNLOAD_SLOTS = max(1, int(os.environ.get("DOWNLOAD_SLOTS", "3") or 3))

router = Router()
admin_router = Router()
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

# ------------------------------------------------------------------ тексты

QUALITY_LABELS = {
    320: "⚡ 320 kbps — максимум",
    192: "🎧 192 kbps — баланс",
    128: "📱 128 kbps — экономно",
}

WELCOME_TPL = (
    "🎧 <b>MP3 Downloader</b>\n\n"
    "Привет, <b>{name}</b>! Я превращаю ссылки и названия в готовые MP3 🎶\n\n"
    "🔗 <b>Пришли ссылку</b> — YouTube, YouTube Music, SoundCloud, Spotify\n"
    "🔎 <b>Или просто напиши</b> <i>исполнитель — название</i>\n\n"
    "✨ <b>Ещё умею:</b>\n"
    "📝 тексты песен с Genius\n"
    "❤️ избранное и 🕘 историю — все твои треки под рукой\n"
    "🔥 чарт самых популярных треков бота\n"
    "⚡ мгновенную отправку уже скачанных треков\n"
    "💬 inline-режим: напиши <code>@{bot} название</code> в любом чате\n\n"
    "🎚 Текущее качество: <b>{q} kbps</b>"
)

HELP_TEXT = (
    "📥 <b>Как скачать</b>\n\n"
    "1️⃣ Скопируй ссылку на трек или видео:\n"
    "• youtube.com, youtu.be, music.youtube.com\n"
    "• soundcloud.com\n"
    "• open.spotify.com/track/…\n\n"
    "2️⃣ Отправь ссылку мне\n"
    "   …или напиши <i>исполнитель — название</i> — пришлю список, выбери трек\n"
    "3️⃣ Через несколько секунд получишь MP3 с обложкой и тегами 🎵\n\n"
    "<b>Кнопки под треком:</b>\n"
    "📝 — текст песни · 🤍 — в избранное · 🔗 — оригинал\n\n"
    "<b>Меню внизу:</b>\n"
    "🔥 Чарт · ❤️ Избранное · 🕘 История · 👤 Профиль · 🎲 Случайный трек\n\n"
    "⚠️ Лимит Telegram — 50 МБ на файл, для длинных видео качество понижается автоматически.\n"
    "⚠️ Для ссылок на плейлисты качаю первый трек."
)

ABOUT_TEXT = (
    "ℹ️ <b>О боте</b>\n\n"
    "MP3 Downloader скачивает аудио из интернета и отдаёт "
    "готовый MP3 с обложкой и тегами исполнителя.\n\n"
    "⚙️ Движок: yt-dlp + FFmpeg\n"
    "📝 Тексты: Genius (запасной — LRCLIB)\n"
    "⚡ Кэш: однажды скачанный трек приходит мгновенно\n"
    "🤖 Каркас: aiogram 3\n"
    "🔒 Без регистрации и лишних данных — только ссылка и файл."
)

ADMIN_HELP = (
    "🛠 <b>Команды администратора</b>\n\n"
    "/stats — статистика бота\n"
    "/ban &lt;id&gt; — забанить пользователя\n"
    "/unban &lt;id&gt; — разбанить\n"
    "/broadcast &lt;текст&gt; — рассылка текста\n"
    "↩️ /broadcast ответом на любое сообщение — разослать его как есть (фото, видео, аудио…)"
)

QUALITY_TEXT = "⚙️ <b>Качество звука</b>\n\nЧем выше — тем лучше звук, но больше файл."

# ------------------------------------------------------------------ клавиатуры

BTN_CHART = "🔥 Чарт"
BTN_FAVS = "❤️ Избранное"
BTN_HISTORY = "🕘 История"
BTN_PROFILE = "👤 Профиль"
BTN_LYRICS = "📝 Текст песни"
BTN_RANDOM = "🎲 Случайный"

reply_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_CHART), KeyboardButton(text=BTN_FAVS)],
        [KeyboardButton(text=BTN_HISTORY), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_LYRICS), KeyboardButton(text=BTN_RANDOM)],
    ],
    resize_keyboard=True, is_persistent=True,
    input_field_placeholder="Ссылка или исполнитель — название",
)
BACK_BTN = InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")
BACK_KB = InlineKeyboardMarkup(inline_keyboard=[[BACK_BTN]])
CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="lyrics_cancel")]])
LYRICS_ASK_TEXT = ("📝 <b>Текст песни</b>\n\n"
                   "Напиши <i>исполнитель — название</i> или пришли ссылку на трек "
                   "(YouTube, SoundCloud, Spotify) — найду слова на Genius.")
_await_lyrics: dict = {}          # user_id -> время, когда бот попросил название
AWAIT_TTL = 10 * 60


def main_menu(quality: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Чарт", callback_data="chart"),
         InlineKeyboardButton(text="❤️ Избранное", callback_data="favs:0")],
        [InlineKeyboardButton(text="🕘 История", callback_data="hist:0"),
         InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="📝 Текст песни", callback_data="lyrics_ask"),
         InlineKeyboardButton(text="🎲 Случайный", callback_data="random")],
        [InlineKeyboardButton(text=f"⚙️ Качество: {quality} kbps", callback_data="settings")],
        [InlineKeyboardButton(text="📥 Как скачать", callback_data="help"),
         InlineKeyboardButton(text="ℹ️ О боте", callback_data="about")],
        [InlineKeyboardButton(text="💬 Искать в любом чате", switch_inline_query="")],
    ])


def quality_menu(current: int) -> InlineKeyboardMarkup:
    rows = []
    for q, label in QUALITY_LABELS.items():
        mark = "✅ " if q == current else ""
        rows.append([InlineKeyboardButton(text=mark + label, callback_data=f"setq:{q}")])
    rows.append([BACK_BTN])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить статистику", callback_data="admin:stats")],
    ])


def track_kb(track_id: int, user_id: int, source_url: str = None) -> InlineKeyboardMarkup:
    fav = db.is_favorite(user_id, track_id)
    row = [
        InlineKeyboardButton(text="📝 Текст", callback_data=f"lt:{track_id}"),
        InlineKeyboardButton(text="❤️" if fav else "🤍", callback_data=f"fav:{track_id}"),
    ]
    if source_url and source_url.startswith("http"):
        row.append(InlineKeyboardButton(text="🔗", url=source_url))
    return InlineKeyboardMarkup(inline_keyboard=[row])


# ------------------------------------------------------------------ утилиты

async def _safe_edit(msg: Message, text: str, reply_markup=None) -> None:
    try:
        await msg.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception:
        pass


def _fmt_dur(sec) -> str:
    if not sec:
        return "—:—"
    h, rem = divmod(int(sec), 3600)
    mnt, s = divmod(rem, 60)
    return f"{h}:{mnt:02d}:{s:02d}" if h else f"{mnt}:{s:02d}"


def _track_name(t: dict) -> str:
    return f"{t['performer']} — {t['title']}" if t.get("performer") else (t.get("title") or "audio")


def _plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


_bot_username = {"v": None}


async def _get_username(bot: Bot) -> str:
    if _bot_username["v"] is None:
        try:
            _bot_username["v"] = (await bot.me()).username or ""
        except Exception:
            return ""
    return _bot_username["v"]


async def _caption_footer(bot: Bot) -> str:
    u = await _get_username(bot)
    return f'\n🔎 <a href="https://t.me/{u}">Найти другую песню</a>' if u else ""


def fmt_stats() -> str:
    s = db.stats()
    lines = [
        "📊 <b>Статистика бота</b>",
        "",
        f"👥 Пользователей: <b>{s['users']}</b> (+{s['new_24h']} за сутки)",
        f"🟢 Активны за 24 ч: <b>{s['active_24h']}</b>",
        f"📥 Скачиваний всего: <b>{s['downloads']}</b> (за сутки: {s['downloads_24h']})",
        f"⚡ Треков в кэше: {s['cached']}",
        f"🚫 Забанено: {s['banned']} · 🔕 Заблокировали бота: {s['blocked']}",
    ]
    top = s.get("top") or []
    if top:
        lines += ["", "🏆 <b>Топ-5 меломанов:</b>"]
        medals = ["🥇", "🥈", "🥉", "4.", "5."]
        for i, u in enumerate(top):
            name = html.escape(str(u["first_name"] or u["username"] or "—"))
            lines.append(f"{medals[i]} {name} — {u['downloads']}")
    return "\n".join(lines)


def _allowed(user) -> bool:
    return user is not None and not db.is_banned(user.id)


# ------------------------------------------------------------------ отправка трека из кэша

async def send_cached(m: Message, user_id: int, track: dict, note: str = "⚡ Из кэша — мгновенно") -> bool:
    """Отправляет трек по file_id. False — file_id протух, нужно качать заново."""
    caption = (f"🎚 {track['quality']} kbps · {track['size_mb']} МБ\n{note}"
               + await _caption_footer(m.bot))
    try:
        await m.answer_audio(track["file_id"], caption=caption,
                             reply_markup=track_kb(track["id"], user_id, track.get("source_url")))
    except TelegramBadRequest as e:
        log.warning("file_id трека %s не принят: %s", track["id"], e)
        db.drop_track(track["id"])
        return False
    db.bump_track(track["id"])
    db.add_history(user_id, track["id"])
    db.add_download(user_id)
    return True


# ------------------------------------------------------------------ пользовательские хендлеры

async def show_menu(m: Message, user, edit: bool = False) -> None:
    db.add_user(user)
    q = (db.get_user(user.id) or {}).get("quality", 192)
    text = WELCOME_TPL.format(name=html.escape(user.first_name or "друг"), q=q,
                              bot=await _get_username(m.bot) or "bot")
    if edit:
        await _safe_edit(m, text, main_menu(q))
    else:
        await m.answer(text, reply_markup=main_menu(q))


@router.message(CommandStart())
async def cmd_start(m: Message):
    if m.from_user is None:
        return
    if db.is_banned(m.from_user.id):
        await m.answer("🚫 Доступ к боту закрыт.")
        return
    db.add_user(m.from_user)
    await m.answer("🎛 Меню всегда внизу 👇", reply_markup=reply_kb)
    await show_menu(m, m.from_user)


@router.message(Command("help"))
async def cmd_help(m: Message):
    if _allowed(m.from_user):
        await m.answer(HELP_TEXT, reply_markup=BACK_KB)


@router.message(Command("quality"))
async def cmd_quality(m: Message):
    if not _allowed(m.from_user):
        return
    db.add_user(m.from_user)
    user = db.get_user(m.from_user.id) or {}
    await m.answer(QUALITY_TEXT, reply_markup=quality_menu(user.get("quality", 192)))


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    if _allowed(c.from_user) and c.message is not None:
        await show_menu(c.message, c.from_user, edit=True)
    await c.answer()


@router.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    if c.message is not None:
        await _safe_edit(c.message, HELP_TEXT, BACK_KB)
    await c.answer()


@router.callback_query(F.data == "about")
async def cb_about(c: CallbackQuery):
    if c.message is not None:
        await _safe_edit(c.message, ABOUT_TEXT, BACK_KB)
    await c.answer()


@router.callback_query(F.data == "settings")
async def cb_settings(c: CallbackQuery):
    if c.from_user is None or c.message is None:
        return await c.answer()
    user = db.get_user(c.from_user.id) or {}
    await _safe_edit(c.message, QUALITY_TEXT, quality_menu(user.get("quality", 192)))
    await c.answer()


@router.callback_query(F.data.startswith("setq:"))
async def cb_set_quality(c: CallbackQuery):
    if c.from_user is None or c.message is None:
        return await c.answer()
    try:
        q = int((c.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        return await c.answer()
    if q not in QUALITY_LABELS:
        return await c.answer()
    db.set_quality(c.from_user.id, q)
    await c.answer(f"✅ Качество: {q} kbps")
    await _safe_edit(c.message, QUALITY_TEXT, quality_menu(q))


@router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


# ------------------------------------------------------------------ профиль

def profile_text(user) -> str:
    u = db.get_user(user.id) or {}
    downloads = u.get("downloads", 0)
    favs = db.count_favorites(user.id)
    artist, plays = db.favorite_artist(user.id)
    joined = datetime.fromtimestamp(u.get("joined") or time.time()).strftime("%d.%m.%Y")
    level = next(lvl for need, lvl in [(500, "👑 Легенда"), (200, "💎 Меломан"), (50, "🎸 Знаток"),
                                       (10, "🎧 Слушатель"), (0, "🌱 Новичок")] if downloads >= need)
    lines = [
        f"👤 <b>{html.escape(user.first_name or 'Профиль')}</b>",
        f"🏅 Уровень: <b>{level}</b>",
        "",
        f"📥 Скачано: <b>{downloads}</b> {_plural(downloads, 'трек', 'трека', 'треков')}",
        f"🏆 Место в рейтинге: <b>#{db.user_rank(user.id)}</b>",
        f"❤️ В избранном: <b>{favs}</b>",
    ]
    if artist:
        lines.append(f"🎤 Любимый исполнитель: <b>{html.escape(artist)}</b> ({plays})")
    lines += [f"🎚 Качество: <b>{u.get('quality', 192)} kbps</b>", f"📅 С нами с {joined}"]
    return "\n".join(lines)


def profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️ Избранное", callback_data="favs:0"),
         InlineKeyboardButton(text="🕘 История", callback_data="hist:0")],
        [InlineKeyboardButton(text="⚙️ Качество", callback_data="settings"), BACK_BTN],
    ])


@router.message(Command("me", "profile"))
@router.message(F.text == BTN_PROFILE)
async def msg_profile(m: Message):
    if _allowed(m.from_user):
        db.add_user(m.from_user)
        await m.answer(profile_text(m.from_user), reply_markup=profile_kb())


@router.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery):
    if _allowed(c.from_user) and c.message is not None:
        await _safe_edit(c.message, profile_text(c.from_user), profile_kb())
    await c.answer()


# ------------------------------------------------------------------ списки треков: избранное, история, чарт

LIST_PAGE = 8


def tracks_list(title: str, tracks: list, kind: str, page: int, empty: str):
    if not tracks:
        return f"{title}\n\n{empty}", BACK_KB
    pages = max(1, (len(tracks) + LIST_PAGE - 1) // LIST_PAGE)
    page = max(0, min(page, pages - 1))
    rows = []
    for t in tracks[page * LIST_PAGE:(page + 1) * LIST_PAGE]:
        label = f"🎵 {_fmt_dur(t.get('duration'))} {_track_name(t)}"[:60]
        rows.append([InlineKeyboardButton(text=label, callback_data=f"pl:{t['id']}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="‹‹", callback_data=f"{kind}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="››", callback_data=f"{kind}:{page + 1}"))
        rows.append(nav)
    rows.append([BACK_BTN])
    return f"{title}\n<i>Нажми на трек — пришлю мгновенно ⚡</i>", InlineKeyboardMarkup(inline_keyboard=rows)


def favs_view(uid: int, page: int = 0):
    favs = db.favorites(uid)
    return tracks_list(f"❤️ <b>Избранное</b> · {len(favs)}", favs, "favs", page,
                       "Пока пусто. Жми 🤍 под любым треком, чтобы сохранить его сюда.")


def hist_view(uid: int, page: int = 0):
    return tracks_list("🕘 <b>Недавно скачанные</b>", db.history(uid), "hist", page,
                       "Ты ещё ничего не скачал — пришли ссылку или название трека 🎶")


def chart_view():
    tracks = db.chart(days=7, limit=10)
    if not tracks:
        return "🔥 <b>Чарт недели</b>\n\nЧарт пока пуст — стань первым, кто что-нибудь скачает 🎶", BACK_KB
    medals = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]
    rows = []
    for i, t in enumerate(tracks):
        rows.append([InlineKeyboardButton(text=f"{medals[i]} {_track_name(t)}"[:60], callback_data=f"pl:{t['id']}")])
    rows.append([InlineKeyboardButton(text="🎲 Случайный трек", callback_data="random"), BACK_BTN])
    return ("🔥 <b>Чарт недели</b>\n<i>Самые скачиваемые треки бота за 7 дней</i>",
            InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Command("favorites", "fav"))
@router.message(F.text == BTN_FAVS)
async def msg_favs(m: Message):
    if _allowed(m.from_user):
        text, kb = favs_view(m.from_user.id)
        await m.answer(text, reply_markup=kb)


@router.message(Command("history"))
@router.message(F.text == BTN_HISTORY)
async def msg_history(m: Message):
    if _allowed(m.from_user):
        text, kb = hist_view(m.from_user.id)
        await m.answer(text, reply_markup=kb)


@router.message(Command("top", "chart"))
@router.message(F.text == BTN_CHART)
async def msg_chart(m: Message):
    if _allowed(m.from_user):
        text, kb = chart_view()
        await m.answer(text, reply_markup=kb)


@router.callback_query(F.data.regexp(r"^(favs|hist):\d+$"))
async def cb_lists(c: CallbackQuery):
    if not _allowed(c.from_user) or c.message is None:
        return await c.answer()
    kind, page = (c.data or "").split(":")
    view = favs_view if kind == "favs" else hist_view
    text, kb = view(c.from_user.id, int(page))
    await _safe_edit(c.message, text, kb)
    await c.answer()


@router.callback_query(F.data == "chart")
async def cb_chart(c: CallbackQuery):
    if c.message is not None:
        text, kb = chart_view()
        await _safe_edit(c.message, text, kb)
    await c.answer()


@router.callback_query(F.data.startswith("pl:"))
async def cb_play(c: CallbackQuery):
    if not _allowed(c.from_user) or c.message is None:
        return await c.answer()
    try:
        track = db.get_track(int((c.data or "").split(":", 1)[1]))
    except ValueError:
        track = None
    if not track:
        return await c.answer("Трек пропал из кэша 😕 Найди его заново", show_alert=True)
    await c.answer("⚡ Отправляю")
    if not await send_cached(c.message, c.from_user.id, track, note="🎵 " + html.escape(_track_name(track))):
        await deliver(c.message, c.from_user.id, track["source_url"],
                      meta={"artist": track["performer"], "title": track["title"]})


@router.callback_query(F.data.startswith("fav:"))
async def cb_fav(c: CallbackQuery):
    if not _allowed(c.from_user):
        return await c.answer()
    try:
        tid = int((c.data or "").split(":", 1)[1])
    except ValueError:
        return await c.answer()
    track = db.get_track(tid)
    if not track:
        return await c.answer("Трек уже не в кэше 😕", show_alert=True)
    added = db.toggle_favorite(c.from_user.id, tid)
    await c.answer("❤️ Добавлено в избранное" if added else "💔 Убрано из избранного")
    if c.message is not None:
        try:
            await c.message.edit_reply_markup(reply_markup=track_kb(tid, c.from_user.id, track.get("source_url")))
        except Exception:
            pass


async def send_random(m: Message, user_id: int) -> None:
    track = db.random_track()
    if not track:
        await m.answer("🎲 Пока нечего выбирать — кэш пуст. Скачай что-нибудь первым!")
        return
    if not await send_cached(m, user_id, track, note="🎲 Случайный трек из коллекции бота"):
        await m.answer("🎲 Не повезло — трек устарел. Жми ещё раз!")


@router.message(Command("random"))
@router.message(F.text == BTN_RANDOM)
async def msg_random(m: Message):
    if _allowed(m.from_user):
        db.add_user(m.from_user)
        await send_random(m, m.from_user.id)


@router.callback_query(F.data == "random")
async def cb_random(c: CallbackQuery):
    if not _allowed(c.from_user) or c.message is None:
        return await c.answer()
    await c.answer("🎲 Кручу барабан…")
    await send_random(c.message, c.from_user.id)


# ------------------------------------------------------------------ поиск списком

PAGE_SIZE = 10
SEARCH_TTL = 6 * 60 * 60          # результаты поиска живут 6 часов
SEARCH_MAX = 1000                 # сколько поисков держим в памяти
_searches: dict = {}              # sid -> {"q", "items", "uid", "t"}
_sid_counter = {"n": 0}


def _new_sid() -> str:
    _sid_counter["n"] += 1
    n, chars, out = _sid_counter["n"], "0123456789abcdefghijklmnopqrstuvwxyz", ""
    while n:
        n, r = divmod(n, 36)
        out = chars[r] + out
    return out


def _prune_searches() -> None:
    now = time.time()
    for sid in [k for k, v in _searches.items() if now - v["t"] > SEARCH_TTL]:
        _searches.pop(sid, None)
    while len(_searches) > SEARCH_MAX:
        _searches.pop(next(iter(_searches)))


SOURCE_ICONS = {"yt": "🟥", "sc": "🟧"}
SOURCE_NAMES = {"yt": "YouTube", "sc": "SoundCloud", "spotify": "Spotify"}


def _source_of(url: str) -> str:
    u = (url or "").lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "yt"
    if "soundcloud.com" in u:
        return "sc"
    if "spotify" in u:
        return "spotify"
    return ""


def _item_text(it: dict) -> str:
    name = f"{it['artist']} - {it['title']}" if it["artist"] else it["title"]
    icon = SOURCE_ICONS.get(it.get("source"), "")
    return f"{icon} {_fmt_dur(it['duration'])} {name}".strip()[:60]


def search_page(sid: str, page: int):
    data = _searches[sid]
    items = data["items"]
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    rows = []
    for i in range(page * PAGE_SIZE, min(len(items), (page + 1) * PAGE_SIZE)):
        rows.append([InlineKeyboardButton(text=_item_text(items[i]), callback_data=f"t:{sid}:{i}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="‹‹", callback_data=f"p:{sid}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1} / {pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="››", callback_data=f"p:{sid}:{page + 1}"))
        rows.append(nav)
    used = {it.get("source") for it in items}
    legend = " · ".join(f"{SOURCE_ICONS[k]} {SOURCE_NAMES[k]}" for k in ("yt", "sc") if k in used)
    text = (f"🎶 Нашёл <b>{len(items)}</b> по запросу «<b>{html.escape(data['q'])}</b>»\n"
            f"<i>Нажми на трек — пришлю MP3</i>\n{legend}")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------------------------------------------------------ тексты песен

LYRICS_MAX = 2000
_lyrics_q: dict = {}              # lid -> поисковый запрос (для старых кнопок «ly:»)
TG_LIMIT = 4000


def _split_text(text: str, limit: int = TG_LIMIT) -> list:
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


async def send_lyrics(m: Message, query: str) -> None:
    status = await m.answer("📝 <i>Ищу текст…</i>")
    try:
        r = await find_lyrics(query)
    except LyricsError as e:
        msg = f"❌ {html.escape(str(e.args[0]))}"
        if len(e.args) > 1 and e.args[1]:
            msg += f'\n🔗 <a href="{html.escape(e.args[1])}">Открыть на Genius</a>'
        await _safe_edit(status, msg)
        return
    except Exception:
        log.exception("Ошибка поиска текста %s", query)
        await _safe_edit(status, "❌ Поиск текстов сейчас не работает, попробуй позже.")
        return
    head = f"📝 <b>{html.escape(r['artist'])} — {html.escape(r['title'])}</b>\n\n"
    foot = f"\n\n🔗 <a href=\"{html.escape(r['url'])}\">Genius</a>" if r.get("url") else ""
    if r["source"] != "Genius":
        foot += f"\n<i>Источник: {html.escape(r['source'])}</i>"
    # секции вида [Припев] / [Куплет 1] — выделяем жирным
    body = re.sub(r"(?m)^(\[[^\]\n]{1,60}\])$", r"<b>\1</b>", html.escape(r["text"]))
    parts = _split_text(body, TG_LIMIT - len(head) - len(foot))
    try:
        await status.delete()
    except Exception:
        pass
    for i, part in enumerate(parts):
        await m.answer((head if i == 0 else "") + part + (foot if i == len(parts) - 1 else ""),
                       disable_web_page_preview=True)


@router.message(Command("lyrics", "text"))
async def cmd_lyrics(m: Message, command: CommandObject):
    if not _allowed(m.from_user):
        return
    q = (command.args or "").strip()
    if not q:
        await ask_lyrics(m, m.from_user.id)
        return
    await lyrics_from_input(m, q)


async def ask_lyrics(m: Message, user_id: int) -> None:
    _await_lyrics[user_id] = time.time()
    await m.answer(LYRICS_ASK_TEXT, reply_markup=CANCEL_KB)


async def lyrics_from_input(m: Message, text: str) -> None:
    """Текст пользователя (название или ссылка) → текст песни."""
    url_match = URL_RE.search(text)
    if url_match:
        status = await m.answer("🔗 <i>Смотрю, что за трек…</i>")
        try:
            query = await track_name_from_url(url_match.group(0).rstrip(").,;"))
        except DownloadError as e:
            await _safe_edit(status, f"❌ {e}")
            return
        except Exception:
            log.exception("Не удалось определить трек по ссылке %s", text)
            await _safe_edit(status, "❌ Не смог понять трек по ссылке 😕 Напиши исполнителя и название.")
            return
        try:
            await status.delete()
        except Exception:
            pass
        await send_lyrics(m, clean_query("", query) or query)
        return
    await send_lyrics(m, re.sub(r"\s+", " ", text)[:150])


@router.message(F.text == BTN_LYRICS)
async def on_lyrics_button(m: Message):
    if _allowed(m.from_user):
        db.add_user(m.from_user)
        await ask_lyrics(m, m.from_user.id)


@router.callback_query(F.data == "lyrics_ask")
async def cb_lyrics_ask(c: CallbackQuery):
    if not _allowed(c.from_user) or c.message is None:
        return await c.answer()
    await c.answer()
    await ask_lyrics(c.message, c.from_user.id)


@router.callback_query(F.data == "lyrics_cancel")
async def cb_lyrics_cancel(c: CallbackQuery):
    if c.from_user is not None:
        _await_lyrics.pop(c.from_user.id, None)
    if c.message is not None:
        await _safe_edit(c.message, "✖️ Отменено. Можешь прислать ссылку или название — скачаю MP3 🎧")
    await c.answer()


@router.callback_query(F.data.startswith("lt:"))
async def cb_track_lyrics(c: CallbackQuery):
    if not _allowed(c.from_user) or c.message is None:
        return await c.answer()
    try:
        track = db.get_track(int((c.data or "").split(":", 1)[1]))
    except ValueError:
        track = None
    q = clean_query(track.get("performer") or "", track.get("title") or "") if track else ""
    if len(q) < 2:
        return await c.answer("Не знаю, что за трек 😕 Напиши /lyrics исполнитель — название", show_alert=True)
    await c.answer("📝 Ищу текст…")
    await send_lyrics(c.message, q)


@router.callback_query(F.data.startswith("ly:"))
async def cb_lyrics_legacy(c: CallbackQuery):
    """Кнопки «📝 Текст» под треками, отправленными старой версией бота."""
    if not _allowed(c.from_user) or c.message is None:
        return await c.answer()
    q = _lyrics_q.get((c.data or "").split(":", 1)[1])
    if not q and c.message.audio:
        q = clean_query(c.message.audio.performer or "", c.message.audio.title or "")
    if not q:
        return await c.answer("Кнопка устарела — напиши /lyrics исполнитель — название", show_alert=True)
    await c.answer("📝 Ищу текст…")
    await send_lyrics(c.message, q)


# ------------------------------------------------------------------ скачивание

_slots = asyncio.Semaphore(DOWNLOAD_SLOTS)
_busy_users: set = set()


def _cache_key(url: str, quality: int) -> str:
    url = (url or "").strip()
    yt = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", url)
    if yt:
        url = f"yt:{yt.group(1)}"
    else:
        url = re.sub(r"[?#].*$", "", url).rstrip("/").lower()
    return f"{url}|{quality}"


async def deliver(m: Message, user_id: int, url: str, meta: dict = None) -> None:
    """Скачивает трек по ссылке и отправляет MP3 в чат сообщения m."""
    stored = db.get_user(user_id) or {}
    quality = stored.get("quality", 192)

    key = _cache_key(url, quality)
    cached = db.get_track_by_key(key)
    if cached and await send_cached(m, user_id, cached):
        return

    if user_id in _busy_users:
        await m.answer("⏳ Уже качаю твой предыдущий трек — дождись его, и пришли следующий 🙏")
        return
    _busy_users.add(user_id)

    queued = _slots.locked()
    status = await m.answer("🕐 <i>В очереди, сейчас все слоты заняты…</i>" if queued else
                            ("⏳ <i>Скачиваю…</i>" if meta else "🔎 <i>Ищу трек…</i>"))
    loop = asyncio.get_running_loop()
    last_progress = {"t": 0.0}

    async def progress(text: str) -> None:
        if time.monotonic() - last_progress["t"] < 3:
            return
        last_progress["t"] = time.monotonic()
        try:
            await status.edit_text(text)
        except Exception:
            pass

    result = None
    try:
        async with _slots:
            if queued:
                await _safe_edit(status, "⏳ <i>Твоя очередь! Скачиваю…</i>")
            async with ChatActionSender.upload_voice(bot=m.bot, chat_id=m.chat.id):
                result = await process_url(url, quality, progress_cb=progress, loop=loop, meta=meta)
                await _safe_edit(status, "🎛 <i>Вшиваю теги и обложку, отправляю…</i>")

                src = SOURCE_NAMES.get(_source_of(result.get("source_url") or url), "")
                caption = (f"🎚 {result['quality']} kbps · {result['size_mb']} МБ"
                           + (f" · 📡 {src}" if src else "")
                           + await _caption_footer(m.bot))
                title = (result["title"] or "audio")[:64]
                performer = result["performer"] or None
                sent = await m.answer_audio(
                    FSInputFile(result["path"]),
                    title=title,
                    performer=performer,
                    duration=result["duration"],
                    caption=caption,
                )
        source_url = result.get("source_url") or url
        tid = db.save_track(key, sent.audio.file_id, title, performer or "", result["duration"],
                            result["quality"], result["size_mb"], source_url)
        db.add_history(user_id, tid)
        db.add_download(user_id)
        try:
            await sent.edit_reply_markup(reply_markup=track_kb(tid, user_id, source_url))
        except Exception:
            pass
        try:
            await status.delete()
        except Exception:
            pass
    except DownloadError as e:
        await _safe_edit(status, f"❌ {e}")
    except TelegramBadRequest as e:
        log.warning("Telegram отклонил файл: %s", e)
        if "big" in str(e).lower():
            await _safe_edit(status, "❌ Файл больше 50 МБ — Telegram не принимает такие 😔")
        else:
            await _safe_edit(status, "❌ Ошибка Telegram, попробуй ещё раз.")
    except Exception:
        log.exception("Неожиданная ошибка при обработке %s", url)
        await _safe_edit(status, "❌ Что-то пошло не так. Попробуй ещё раз.")
    finally:
        _busy_users.discard(user_id)
        if result:
            shutil.rmtree(result["tmpdir"], ignore_errors=True)


# ------------------------------------------------------------------ inline-режим (@bot запрос в любом чате)

@router.inline_query()
async def on_inline(q: InlineQuery):
    if db.is_banned(q.from_user.id):
        return await q.answer([], cache_time=60, is_personal=True)
    text = (q.query or "").strip()
    tracks = db.search_cached(text) if len(text) >= 2 else (db.favorites(q.from_user.id)[:20] or db.chart(limit=20))
    results = [
        InlineQueryResultCachedAudio(id=str(t["id"]), audio_file_id=t["file_id"])
        for t in tracks[:20]
    ]
    button = InlineQueryResultsButton(text="🔎 Нет нужного? Найти и скачать в боте", start_parameter="inline")
    await q.answer(results, cache_time=30, is_personal=True, button=button)


# ------------------------------------------------------------------ приём ссылок и запросов

@router.message(F.text & ~F.text.startswith("/"))
async def on_any_text(m: Message):
    text = (m.text or "").strip()
    if not _allowed(m.from_user):
        return
    db.add_user(m.from_user)

    asked = _await_lyrics.pop(m.from_user.id, None)
    if asked and time.time() - asked < AWAIT_TTL:
        await lyrics_from_input(m, text)
        return

    url_match = URL_RE.search(text)
    if url_match and re.search(r"(^|[/.])(vk\.com|vk\.ru|vkvideo\.ru)", url_match.group(0), re.I):
        await m.reply("😔 VK больше не поддерживается. Пришли ссылку на YouTube, SoundCloud или Spotify "
                      "— или просто напиши исполнителя и название.")
        return
    if url_match:
        await deliver(m, m.from_user.id, url_match.group(0).rstrip(").,;"))
        return

    query = re.sub(r"\s+", " ", text)[:200]
    if len(query) < 2:
        await m.reply("🤔 Пришли ссылку на трек или напиши исполнителя и название.")
        return
    status = await m.answer(f"🔎 <i>Ищу «{html.escape(query)}» на YouTube и SoundCloud…</i>")
    try:
        async with ChatActionSender.typing(bot=m.bot, chat_id=m.chat.id):
            items = await search_tracks(query)
    except DownloadError as e:
        await _safe_edit(status, f"❌ {e}")
        return
    except Exception:
        log.exception("Ошибка поиска %s", query)
        await _safe_edit(status, "❌ Поиск сейчас не работает, попробуй ещё раз.")
        return
    _prune_searches()
    sid = _new_sid()
    _searches[sid] = {"q": query, "items": items, "uid": m.from_user.id, "t": time.time()}
    text, kb = search_page(sid, 0)
    await _safe_edit(status, text, kb)


@router.callback_query(F.data.startswith("p:"))
async def cb_search_page(c: CallbackQuery):
    try:
        _, sid, page = (c.data or "").split(":")
        page = int(page)
    except ValueError:
        return await c.answer()
    if sid not in _searches or c.message is None:
        return await c.answer("Поиск устарел — отправь запрос заново 🔎", show_alert=True)
    text, kb = search_page(sid, page)
    await _safe_edit(c.message, text, kb)
    await c.answer()


@router.callback_query(F.data.startswith("t:"))
async def cb_search_pick(c: CallbackQuery):
    if not _allowed(c.from_user) or c.message is None:
        return await c.answer()
    try:
        _, sid, idx = (c.data or "").split(":")
        item = _searches[sid]["items"][int(idx)]
    except (ValueError, KeyError, IndexError):
        return await c.answer("Поиск устарел — отправь запрос заново 🔎", show_alert=True)
    await c.answer(f"⏳ {item['title'][:150]}")
    db.add_user(c.from_user)
    await deliver(c.message, c.from_user.id, item["url"],
                  meta={"artist": item["artist"], "title": item["title"]})


# ------------------------------------------------------------------ админ-хендлеры

@admin_router.message(Command("admin"))
async def cmd_admin(m: Message):
    await m.answer(fmt_stats() + "\n\n" + ADMIN_HELP, reply_markup=admin_menu())


@admin_router.message(Command("stats"))
async def cmd_stats(m: Message):
    await m.answer(fmt_stats(), reply_markup=admin_menu())


@admin_router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(c: CallbackQuery):
    if c.message is not None:
        await _safe_edit(c.message, fmt_stats(), admin_menu())
    await c.answer("🔄 Обновлено")


@admin_router.message(Command("ban"))
async def cmd_ban(m: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        return await m.answer("Использование: /ban &lt;user_id&gt;")
    uid = int(command.args.strip())
    if uid in ADMIN_IDS:
        return await m.answer("Нельзя забанить администратора 🙂")
    db.set_ban(uid, True)
    await m.answer(f"🚫 Пользователь <code>{uid}</code> забанен.")


@admin_router.message(Command("unban"))
async def cmd_unban(m: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        return await m.answer("Использование: /unban &lt;user_id&gt;")
    uid = int(command.args.strip())
    db.set_ban(uid, False)
    await m.answer(f"✅ Пользователь <code>{uid}</code> разбанен.")


@admin_router.message(Command("broadcast"))
async def cmd_broadcast(m: Message, command: CommandObject):
    text = (command.args or "").strip()
    source = m.reply_to_message
    if not text and source is None:
        return await m.answer("Использование: /broadcast &lt;текст&gt;\n"
                              "…или ответь командой /broadcast на сообщение, которое нужно разослать.")
    ids = db.all_user_ids()
    status = await m.answer(f"📢 Рассылка запущена: {len(ids)} получателей…")
    sent = failed = blocked = 0
    for n, uid in enumerate(ids, 1):
        for _ in range(2):
            try:
                if source is not None:
                    await m.bot.copy_message(uid, source.chat.id, source.message_id)
                else:
                    await m.bot.send_message(uid, text)
                sent += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                continue
            except TelegramForbiddenError:
                db.set_blocked(uid, True)   # пользователь заблокировал бота — не баним, просто помечаем
                blocked += 1
            except Exception:
                failed += 1
            break
        if n % 25 == 0:
            await _safe_edit(status, f"📢 Рассылка… {n}/{len(ids)}")
        await asyncio.sleep(0.05)
    await _safe_edit(status, f"✅ Рассылка завершена.\n📬 Доставлено: {sent}\n"
                             f"🔕 Заблокировали бота: {blocked}\n⚠️ Ошибок: {failed}")


# ------------------------------------------------------------------ запуск

async def set_commands(bot: Bot) -> None:
    base = [
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="top", description="🔥 Чарт недели"),
        BotCommand(command="favorites", description="❤️ Избранное"),
        BotCommand(command="history", description="🕘 История"),
        BotCommand(command="me", description="👤 Профиль"),
        BotCommand(command="random", description="🎲 Случайный трек"),
        BotCommand(command="lyrics", description="📝 Текст песни"),
        BotCommand(command="quality", description="⚙️ Качество звука"),
        BotCommand(command="help", description="📥 Как скачать"),
    ]
    await bot.set_my_commands(base)
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                base + [BotCommand(command="admin", description="🛠 Панель админа")],
                BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            pass  # админ ещё ни разу не писал боту — скоуп недоступен


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # тише в логах
        pass


def start_health_server() -> None:
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    log.info("Health-check: http://0.0.0.0:%s/", port)


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("❌ Не задана переменная окружения BOT_TOKEN (токен от @BotFather)")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_routers(admin_router, router)
    await set_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен, админы: %s", sorted(ADMIN_IDS) or "нет")
    await dp.start_polling(bot)


if __name__ == "__main__":
    start_health_server()
    asyncio.run(main())
