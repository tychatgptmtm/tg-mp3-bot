"""
MP3 Downloader Bot 🎧
Telegram-бот: пришлите ссылку (VK / YouTube / Spotify / SoundCloud) — получите MP3.

Переменные окружения:
  BOT_TOKEN  — токен от @BotFather (обязателен)
  ADMIN_IDS  — ID администраторов через запятую
  DB_PATH    — путь к базе SQLite (по умолчанию data/bot.db)
  PORT       — порт health-check сервера (по умолчанию 10000)
  PROXY_URL  — опционально: прокси для исходящих запросов yt-dlp
"""
import asyncio
import html
import logging
import os
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database import db
from downloader import DownloadError, URL_RE, process_url, search_tracks

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ADMIN_IDS", "")) if x.strip()}

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
    "Привет, <b>{name}</b>! Я превращаю ссылки в готовые MP3-файлы.\n\n"
    "<b>Откуда умею качать:</b>\n"
    "• YouTube / YouTube Music\n"
    "• Музыка VK и VK Видео\n"
    "• SoundCloud\n"
    "• Spotify (нахожу и качаю лучшую версию трека)\n"
    "• Поиск по названию 🔎\n\n"
    "Пришли ссылку — или просто напиши <i>исполнитель — название</i>, найду сам 🔎\n"
    "🎚 Текущее качество: <b>{q} kbps</b>"
)

HELP_TEXT = (
    "📥 <b>Как скачать</b>\n\n"
    "1️⃣ Скопируй ссылку на трек или видео:\n"
    "• youtube.com, youtu.be, music.youtube.com\n"
    "• vk.com/audio…, посты VK с музыкой, vkvideo.ru\n"
    "• soundcloud.com\n"
    "• open.spotify.com/track/…\n\n"
    "2️⃣ Отправь ссылку мне сообщением\n"
    "   …или просто напиши <i>исполнитель — название</i> (например: tryavoid криминальное чтиво 2) — "
    "пришлю список, выбери нужный трек\n"
    "3️⃣ Через несколько секунд получишь MP3 с обложкой и тегами 🎵\n\n"
    "⚠️ Лимит Telegram — 50 МБ на файл. Для длинных видео качество "
    "автоматически понижается, чтобы файл влез.\n"
    "⚠️ Для ссылок на плейлисты качаю первый трек.\n"
    "⚠️ YouTube иногда блокирует серверные запросы — тогда пробую обход, "
    "а если совсем не пускает, повтори через минуту."
)

ABOUT_TEXT = (
    "ℹ️ <b>О боте</b>\n\n"
    "MP3 Downloader скачивает аудио и видео из интернета и отдаёт "
    "готовый MP3 с обложкой и тегами исполнителя.\n\n"
    "⚙️ Движок: yt-dlp + FFmpeg\n"
    "🤖 Каркас: aiogram 3\n"
    "🔒 Без регистрации и лишних данных — только ссылка и файл."
)

ADMIN_HELP = (
    "🛠 <b>Команды администратора</b>\n\n"
    "/stats — статистика бота\n"
    "/ban &lt;id&gt; — забанить пользователя\n"
    "/unban &lt;id&gt; — разбанить\n"
    "/broadcast &lt;текст&gt; — рассылка всем пользователям"
)


# ------------------------------------------------------------------ клавиатуры

def main_menu(quality: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Как скачать", callback_data="help")],
        [InlineKeyboardButton(text=f"⚙️ Качество: {quality} kbps", callback_data="settings")],
        [InlineKeyboardButton(text="ℹ️ О боте", callback_data="about")],
    ])


def quality_menu(current: int) -> InlineKeyboardMarkup:
    rows = []
    for q, label in QUALITY_LABELS.items():
        mark = "✅ " if q == current else ""
        rows.append([InlineKeyboardButton(text=mark + label, callback_data=f"setq:{q}")])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
    ])


# ------------------------------------------------------------------ утилиты

async def _safe_edit(msg: Message, text: str, reply_markup=None) -> None:
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except Exception:
        pass


def fmt_stats() -> str:
    s = db.stats()
    lines = [
        "📊 <b>Статистика бота</b>",
        "",
        f"👥 Пользователей: <b>{s['users']}</b>",
        f"🚫 Забанено: {s['banned']}",
        f"📥 Скачиваний всего: <b>{s['downloads']}</b>",
    ]
    top = s.get("top") or []
    if top:
        lines.append("")
        lines.append("🏆 <b>Топ-5 по скачиваниям:</b>")
        for i, u in enumerate(top, 1):
            name = html.escape(str(u["first_name"] or u["username"] or "—"))
            lines.append(f"{i}. {name} — {u['downloads']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ пользовательские хендлеры

@router.message(CommandStart())
async def cmd_start(m: Message):
    if m.from_user is None:
        return
    if db.is_banned(m.from_user.id):
        await m.answer("🚫 Доступ к боту закрыт.")
        return
    db.add_user(m.from_user)
    user = db.get_user(m.from_user.id) or {}
    q = user.get("quality", 192)
    name = html.escape(m.from_user.first_name or "друг")
    await m.answer(WELCOME_TPL.format(name=name, q=q), reply_markup=main_menu(q))


@router.message(Command("help"))
async def cmd_help(m: Message):
    if m.from_user and db.is_banned(m.from_user.id):
        return
    await m.answer(HELP_TEXT)


@router.message(Command("quality"))
async def cmd_quality(m: Message):
    if m.from_user is None:
        return
    if db.is_banned(m.from_user.id):
        return
    db.add_user(m.from_user)
    user = db.get_user(m.from_user.id) or {}
    await m.answer("⚙️ <b>Качество звука</b>\n\nЧем выше — тем лучше звук, но больше файл.",
                   reply_markup=quality_menu(user.get("quality", 192)))


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    if c.from_user is None or c.message is None:
        return await c.answer()
    if db.is_banned(c.from_user.id):
        return await c.answer()
    user = db.get_user(c.from_user.id) or {}
    q = user.get("quality", 192)
    name = html.escape(c.from_user.first_name or "друг")
    await _safe_edit(c.message, WELCOME_TPL.format(name=name, q=q), main_menu(q))
    await c.answer()


@router.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    if c.message is None:
        return await c.answer()
    await _safe_edit(c.message, HELP_TEXT,
                     InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]]))
    await c.answer()


@router.callback_query(F.data == "about")
async def cb_about(c: CallbackQuery):
    if c.message is None:
        return await c.answer()
    await _safe_edit(c.message, ABOUT_TEXT,
                     InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]]))
    await c.answer()


@router.callback_query(F.data == "settings")
async def cb_settings(c: CallbackQuery):
    if c.from_user is None or c.message is None:
        return await c.answer()
    user = db.get_user(c.from_user.id) or {}
    await _safe_edit(c.message, "⚙️ <b>Качество звука</b>\n\nЧем выше — тем лучше звук, но больше файл.",
                     quality_menu(user.get("quality", 192)))
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
    await c.answer(f"✅ Качество: {q} kbps", show_alert=False)
    await _safe_edit(c.message, "⚙️ <b>Качество звука</b>\n\nЧем выше — тем лучше звук, но больше файл.",
                     quality_menu(q))


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


def _fmt_dur(sec: int) -> str:
    if not sec:
        return "—:—"
    h, rem = divmod(int(sec), 3600)
    mnt, s = divmod(rem, 60)
    return f"{h}:{mnt:02d}:{s:02d}" if h else f"{mnt}:{s:02d}"


SOURCE_ICONS = {"vk": "🟦", "yt": "🟥", "sc": "🟧"}
SOURCE_NAMES = {"yt": "YouTube", "sc": "SoundCloud", "vk": "VK", "spotify": "Spotify"}


def _source_of(url: str) -> str:
    u = (url or "").lower()
    if u.startswith("vkaudio:"):
        return "vk"
    if "youtube.com" in u or "youtu.be" in u:
        return "yt"
    if "soundcloud.com" in u:
        return "sc"
    if "vk.com" in u or "vkvideo.ru" in u or "vk.ru" in u:
        return "vk"
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
    legend = " · ".join(f"{SOURCE_ICONS[k]} {SOURCE_NAMES[k]}" for k in ("vk", "yt", "sc") if k in used)
    text = (f"🎶 Аудиозаписи по запросу «<b>{html.escape(data['q'])}</b>»\n"
            f"<i>Нажми на трек — пришлю MP3</i>\n{legend}")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


_bot_username = {"v": None}


async def _caption_footer(bot: Bot) -> str:
    if _bot_username["v"] is None:
        try:
            _bot_username["v"] = (await bot.me()).username or ""
        except Exception:
            return ""
    u = _bot_username["v"]
    return f'\n🔎 <a href="https://t.me/{u}">Найти другую песню</a>' if u else ""


async def deliver(m: Message, user_id: int, url: str, meta: dict = None) -> None:
    """Скачивает трек по ссылке и отправляет MP3 в чат сообщения m."""
    stored = db.get_user(user_id) or {}
    quality = stored.get("quality", 192)
    status = await m.answer("⏳ <i>Скачиваю…</i>" if meta else "🔎 <i>Ищу трек…</i>")
    loop = asyncio.get_running_loop()
    last_progress = {"t": 0.0}

    async def progress(text: str) -> None:
        if time.monotonic() - last_progress["t"] < 3.5:
            return
        last_progress["t"] = time.monotonic()
        try:
            await status.edit_text(text)
        except Exception:
            pass

    result = None
    try:
        result = await process_url(url, quality, progress_cb=progress, loop=loop, meta=meta)
        await _safe_edit(status, "🎛 <i>Конвертирую в MP3 и вшиваю теги с обложкой…</i>")

        src = SOURCE_NAMES.get(_source_of(result.get("source_url") or url), "")
        caption = (f"🎚 {result['quality']} kbps · {result['size_mb']} МБ"
                   + (f"\n📡 Найдено: {src}" if src else "")
                   + await _caption_footer(m.bot))
        await m.answer_audio(
            FSInputFile(result["path"]),
            title=(result["title"] or "audio")[:64],
            performer=(result["performer"] or None),
            duration=result["duration"],
            caption=caption,
        )
        db.add_download(user_id)
        await status.delete()
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
        if result:
            shutil.rmtree(result["tmpdir"], ignore_errors=True)


# ------------------------------------------------------------------ приём ссылок и запросов

@router.message(F.text & ~F.text.startswith("/"))
async def on_any_text(m: Message):
    text = (m.text or "").strip()
    if m.from_user is None:
        return
    if db.is_banned(m.from_user.id):
        return
    db.add_user(m.from_user)

    url_match = URL_RE.search(text)
    if url_match:
        await deliver(m, m.from_user.id, url_match.group(0).rstrip(").,;"))
        return

    query = re.sub(r"\s+", " ", text)[:200]
    if len(query) < 2:
        await m.reply("🤔 Пришли ссылку на трек или напиши исполнителя и название.")
        return
    status = await m.answer("🔎 <i>Ищу треки…</i>")
    try:
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


@router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


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
    if c.from_user is None or c.message is None:
        return await c.answer()
    if db.is_banned(c.from_user.id):
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
    await m.answer(fmt_stats())


@admin_router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(c: CallbackQuery):
    if c.message is None:
        return await c.answer()
    await _safe_edit(c.message, fmt_stats(), admin_menu())
    await c.answer()


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
    if not text:
        return await m.answer("Использование: /broadcast &lt;текст сообщения&gt;")
    ids = db.all_user_ids()
    status = await m.answer(f"📢 Рассылка запущена: {len(ids)} получателей…")
    sent = failed = 0
    for uid in ids:
        try:
            await m.bot.send_message(uid, text)
            sent += 1
        except TelegramForbiddenError:
            db.set_ban(uid, True)
            failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await _safe_edit(status, f"✅ Рассылка завершена.\n📬 Доставлено: {sent}\n⚠️ Ошибок: {failed}")


# ------------------------------------------------------------------ запуск

async def set_commands(bot: Bot) -> None:
    base = [
        BotCommand(command="start", description="🏠 Главное меню"),
        BotCommand(command="help", description="📥 Как скачать"),
        BotCommand(command="quality", description="⚙️ Качество звука"),
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
