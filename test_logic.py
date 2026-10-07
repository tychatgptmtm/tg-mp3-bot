from downloader import _pick_quality, _is_retryable, _is_youtube, _youtube_id, is_spotify

# 1. качество под лимит Telegram
assert _pick_quality(180, 320) == 320, _pick_quality(180, 320)
assert _pick_quality(3600, 320) < 320, _pick_quality(3600, 320)
assert _pick_quality(3600, 128) == 128

# 2. retryable-ошибки (в них смысл пробовать другой клиент YouTube)
assert _is_retryable(Exception("Sign in to confirm you're not a bot"))
assert _is_retryable(Exception("HTTP Error 403: Forbidden"))
assert not _is_retryable(Exception("Unsupported URL"))

# 3. распознавание YouTube-ссылок
assert _is_youtube("https://www.youtube.com/watch?v=abc")
assert _is_youtube("https://youtu.be/abc12345678")
assert not _is_youtube("https://vk.com/video-1_2")

# 4. извлечение video_id
assert _youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
assert _youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
assert _youtube_id("https://youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

# 5. Spotify
assert is_spotify("https://open.spotify.com/track/123")
assert not is_spotify("https://youtube.com/watch?v=x")

print("все проверки пройдены")
