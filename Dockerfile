FROM python:3.12-slim

# FFmpeg — конвертация в MP3 и обложки; Deno — JS-рантайм, нужен yt-dlp для YouTube
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py downloader.py database.py ./

# База SQLite и временные файлы
ENV DB_PATH=/tmp/bot.db
EXPOSE 10000

CMD ["python", "bot.py"]
