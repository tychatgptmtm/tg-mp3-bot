# 🎧 MP3 Downloader Bot

Telegram-бот: отправляешь ссылку на трек или видео — получаешь готовый **MP3** с обложкой и тегами.

## Возможности

- **YouTube / YouTube Music** — напрямую + обход блокировки датацентров
- **VK Видео** (vk.com/video, vkvideo.ru) — напрямую
- **SoundCloud** — напрямую
- **Spotify** — трек находится по метаданным (поиск на YouTube, запасной поиск на SoundCloud)
- **Поиск по названию** — просто напиши «исполнитель — название», бот найдёт трек на SoundCloud / YouTube
- Любые другие сайты, поддерживаемые [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- Красивое меню на inline-кнопках, прогресс скачивания, выбор качества (128 / 192 / 320 kbps)
- Админ-панель: `/stats`, `/ban`, `/unban`, `/broadcast` (рассылка)
- Автоматическое понижение качества для длинных треков, чтобы уложиться в лимит Telegram 50 МБ

## 🔓 Обход блокировки YouTube на хостингах

YouTube часто блокирует запросы с IP датацентров («Sign in to confirm you're not a bot»). Бот обходит это сразу несколькими способами:

1. **Ротация клиентов** — YouTube пробуется с разными player_client (android, tv, ios, web_safari, web_embedded, mweb): у них разные требования к токенам, какой-нибудь обычно проходит.
2. **Авто-обновление yt-dlp** при каждом старте бота (в фоне) — свежая версия актуальна всегда.
3. **Резерв через Piped API** — если прямые способы не прошли, аудиопоток берётся через публичные зеркала Piped и конвертируется в MP3 с FFmpeg.
4. **Spotify fallback** — если YouTube недоступен, трек ищется в SoundCloud.

Дополнительно можно задать переменную `PROXY_URL` (например `socks5://user:pass@host:port`) — тогда все запросы yt-dlp пойдут через прокси.

## Переменные окружения

| Переменная | Обязательно | Описание |
|---|---|---|
| `BOT_TOKEN` | ✅ | токен от [@BotFather](https://t.me/BotFather) |
| `ADMIN_IDS` | — | ID админов через запятую (узнать: [@userinfobot](https://t.me/userinfobot)) |
| `DB_PATH` | — | путь к SQLite (по умолчанию `data/bot.db`) |
| `PORT` | — | порт health-check сервера (Render выставляет сам) |
| `PROXY_URL` | — | прокси для yt-dlp, если YouTube жёстко блокирует регион датацентра |
| `VK_TOKEN` | — | токен Kate Mobile (vkhost.github.io) — включает поиск и скачивание музыки VK |
| `VK_COOKIES` | — | cookies.txt с vk.ru/vk.com (вместо VK_TOKEN): бот сам получает веб-токен |
| `VK_PROXY` | — | http-прокси для запросов к VK (если VK ограничивает зарубежный сервер) |
| `VK_UA` | Kate Mobile | User-Agent для VK API (менять, только если токен от другого приложения) |
| `YT_COOKIES` | — | содержимое `cookies.txt` с youtube.com (без него YouTube на Render не качается) |

## Локальный запуск

```bash
pip install -r requirements.txt        # нужен ещё ffmpeg: apt install ffmpeg / brew install ffmpeg
BOT_TOKEN=123:ABC ADMIN_IDS=123456789 python bot.py
```

## Деплой на Render

1. На [render.com](https://render.com) → **New → Web Service** → подключи этот репозиторий.
   - **Runtime:** Docker (Render сам найдёт `Dockerfile`; можно использовать и `render.yaml` — Blueprint).
   - **Health Check Path:** `/`
   - **Instance type:** Free.
2. В **Environment** добавь переменные:
   - `BOT_TOKEN` — токен бота
   - `ADMIN_IDS` — твой Telegram ID
3. Deploy. В логах появится `Бот запущен` — пиши боту `/start` 🚀

### ⚠️ Важно про бесплатный тариф Render

Бесплатный Web Service засыпает через ~15 минут без входящих запросов, и бот перестаёт отвечать. Решение — внешний пинг:

1. Возьми URL сервиса `https://tg-mp3-bot-xxxx.onrender.com`.
2. Заведи бесплатный монитор в [UptimeRobot](https://uptimerobot.com) с интервалом 5 минут на этот URL.
3. Теперь бот будет работать круглосуточно.

### Ограничения

- Telegram Bot API принимает файлы до **50 МБ** — для очень длинных видео бот вернёт ошибку.
- Spotify не отдаёт сами файлы, поэтому трек ищется по названию (качество как правило 320 kbps с YouTube).
- Ссылки на плейлисты: скачивается первый трек.
- Piped API — публичные зеркала, они периодически перегружены: если и они не помогли, просто повтори запрос через минуту.

## Структура

```
bot.py         — логика бота (aiogram 3): меню, ссылки, админка, health-check, self-update yt-dlp
downloader.py  — yt-dlp + FFmpeg: поиск, ротация клиентов YouTube, Piped-фолбэк, конвертация в MP3
database.py    — SQLite: пользователи, качество, баны, статистика
Dockerfile     — образ с FFmpeg для Render
render.yaml    — blueprint для деплоя
```
