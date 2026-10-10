"""Хранилище данных бота (SQLite): пользователи, треки-кэш, избранное, история, статистика."""
import os
import sqlite3
import threading
import time


class Database:
    def __init__(self, path: str):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    quality INTEGER NOT NULL DEFAULT 192,
                    downloads INTEGER NOT NULL DEFAULT 0,
                    banned INTEGER NOT NULL DEFAULT 0,
                    joined INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stats (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO stats (key, value) VALUES ('downloads', 0);

                -- кэш готовых MP3: один раз скачали — дальше отдаём по file_id мгновенно
                CREATE TABLE IF NOT EXISTS tracks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL,
                    file_id TEXT NOT NULL,
                    title TEXT,
                    performer TEXT,
                    duration INTEGER,
                    quality INTEGER,
                    size_mb REAL,
                    source_url TEXT,
                    hits INTEGER NOT NULL DEFAULT 1,
                    created INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    user_id INTEGER NOT NULL,
                    track_id INTEGER NOT NULL,
                    added INTEGER NOT NULL,
                    PRIMARY KEY (user_id, track_id)
                );
                CREATE TABLE IF NOT EXISTS history (
                    user_id INTEGER NOT NULL,
                    track_id INTEGER NOT NULL,
                    ts INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts);
                """
            )
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(users)")}
            if "blocked" not in cols:  # пользователь заблокировал бота (не путать с баном)
                self._conn.execute("ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
            if "last_seen" not in cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN last_seen INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()

    # ---------------------------------------------------------- пользователи

    def add_user(self, user) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """INSERT INTO users (id, username, first_name, joined, last_seen)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       username = excluded.username,
                       first_name = excluded.first_name,
                       last_seen = excluded.last_seen,
                       blocked = 0""",
                (user.id, user.username, user.first_name, now, now),
            )
            self._conn.commit()

    def get_user(self, uid: int):
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        return dict(row) if row else None

    def is_banned(self, uid: int) -> bool:
        u = self.get_user(uid)
        return bool(u and u["banned"])

    def set_quality(self, uid: int, quality: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO users (id, joined) VALUES (?, ?)",
                (uid, int(time.time())),
            )
            self._conn.execute("UPDATE users SET quality = ? WHERE id = ?", (quality, uid))
            self._conn.commit()

    def set_ban(self, uid: int, banned: bool) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET banned = ? WHERE id = ?", (int(banned), uid))
            self._conn.commit()

    def set_blocked(self, uid: int, blocked: bool = True) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET blocked = ? WHERE id = ?", (int(blocked), uid))
            self._conn.commit()

    def all_user_ids(self, include_banned: bool = False):
        q = "SELECT id FROM users WHERE blocked = 0" + ("" if include_banned else " AND banned = 0")
        with self._lock:
            return [r["id"] for r in self._conn.execute(q).fetchall()]

    def user_rank(self, uid: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) + 1 AS r FROM users WHERE downloads > "
                "(SELECT COALESCE(downloads, 0) FROM users WHERE id = ?)", (uid,)).fetchone()
        return row["r"]

    # ---------------------------------------------------------- кэш треков

    def get_track_by_key(self, key: str):
        with self._lock:
            row = self._conn.execute("SELECT * FROM tracks WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def get_track(self, track_id: int):
        with self._lock:
            row = self._conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return dict(row) if row else None

    def save_track(self, key: str, file_id: str, title: str, performer: str, duration,
                   quality: int, size_mb: float, source_url: str) -> int:
        with self._lock:
            self._conn.execute(
                """INSERT INTO tracks (key, file_id, title, performer, duration, quality,
                                       size_mb, source_url, created)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET file_id = excluded.file_id""",
                (key, file_id, title, performer, duration, quality, size_mb, source_url,
                 int(time.time())),
            )
            row = self._conn.execute("SELECT id FROM tracks WHERE key = ?", (key,)).fetchone()
            self._conn.commit()
        return row["id"]

    def bump_track(self, track_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE tracks SET hits = hits + 1 WHERE id = ?", (track_id,))
            self._conn.commit()

    def drop_track(self, track_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
            self._conn.commit()

    def chart(self, days: int = 7, limit: int = 10):
        """Самые популярные треки за последние N дней (по истории скачиваний)."""
        since = int(time.time()) - days * 86400
        with self._lock:
            rows = self._conn.execute(
                """SELECT t.*, COUNT(h.track_id) AS plays FROM history h
                   JOIN tracks t ON t.id = h.track_id
                   WHERE h.ts >= ? GROUP BY t.id ORDER BY plays DESC, t.hits DESC LIMIT ?""",
                (since, limit)).fetchall()
            if not rows:
                rows = self._conn.execute(
                    "SELECT t.*, t.hits AS plays FROM tracks t ORDER BY hits DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    def random_track(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tracks WHERE hits >= 1 ORDER BY RANDOM() LIMIT 1").fetchone()
        return dict(row) if row else None

    def search_cached(self, query: str, limit: int = 20):
        like = f"%{query.strip()}%"
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM tracks WHERE title LIKE ? OR performer LIKE ?
                   OR (COALESCE(performer, '') || ' ' || COALESCE(title, '')) LIKE ?
                   ORDER BY hits DESC LIMIT ?""", (like, like, like, limit)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- избранное и история

    def toggle_favorite(self, uid: int, track_id: int) -> bool:
        """Возвращает True, если трек теперь в избранном."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM favorites WHERE user_id = ? AND track_id = ?", (uid, track_id))
            if cur.rowcount:
                self._conn.commit()
                return False
            self._conn.execute(
                "INSERT INTO favorites (user_id, track_id, added) VALUES (?, ?, ?)",
                (uid, track_id, int(time.time())))
            self._conn.commit()
        return True

    def is_favorite(self, uid: int, track_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM favorites WHERE user_id = ? AND track_id = ?", (uid, track_id)).fetchone()
        return bool(row)

    def favorites(self, uid: int):
        with self._lock:
            rows = self._conn.execute(
                """SELECT t.* FROM favorites f JOIN tracks t ON t.id = f.track_id
                   WHERE f.user_id = ? ORDER BY f.added DESC""", (uid,)).fetchall()
        return [dict(r) for r in rows]

    def count_favorites(self, uid: int) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) AS c FROM favorites WHERE user_id = ?", (uid,)).fetchone()["c"]

    def add_history(self, uid: int, track_id: int) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO history (user_id, track_id, ts) VALUES (?, ?, ?)",
                               (uid, track_id, int(time.time())))
            self._conn.commit()

    def history(self, uid: int, limit: int = 30):
        with self._lock:
            rows = self._conn.execute(
                """SELECT t.*, MAX(h.ts) AS last_ts FROM history h JOIN tracks t ON t.id = h.track_id
                   WHERE h.user_id = ? GROUP BY t.id ORDER BY last_ts DESC LIMIT ?""",
                (uid, limit)).fetchall()
        return [dict(r) for r in rows]

    def favorite_artist(self, uid: int):
        with self._lock:
            row = self._conn.execute(
                """SELECT t.performer AS p, COUNT(*) AS c FROM history h JOIN tracks t ON t.id = h.track_id
                   WHERE h.user_id = ? AND COALESCE(t.performer, '') != ''
                   GROUP BY lower(t.performer) ORDER BY c DESC LIMIT 1""", (uid,)).fetchone()
        return (row["p"], row["c"]) if row else (None, 0)

    # ---------------------------------------------------------- статистика

    def add_download(self, uid: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET downloads = downloads + 1 WHERE id = ?", (uid,)
            )
            self._conn.execute(
                """INSERT INTO stats (key, value) VALUES ('downloads', 1)
                   ON CONFLICT(key) DO UPDATE SET value = value + 1"""
            )
            self._conn.commit()

    def stats(self) -> dict:
        day_ago = int(time.time()) - 86400
        with self._lock:
            agg = self._conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(downloads), 0) AS d FROM users"
            ).fetchone()
            total = self._conn.execute(
                "SELECT value FROM stats WHERE key = 'downloads'"
            ).fetchone()
            banned = self._conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE banned = 1"
            ).fetchone()
            blocked = self._conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE blocked = 1"
            ).fetchone()
            active = self._conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE last_seen >= ?", (day_ago,)
            ).fetchone()
            new = self._conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE joined >= ?", (day_ago,)
            ).fetchone()
            today = self._conn.execute(
                "SELECT COUNT(*) AS c FROM history WHERE ts >= ?", (day_ago,)
            ).fetchone()
            cached = self._conn.execute("SELECT COUNT(*) AS c FROM tracks").fetchone()
            top = self._conn.execute(
                "SELECT first_name, username, downloads FROM users "
                "ORDER BY downloads DESC LIMIT 5"
            ).fetchall()
        return {
            "users": agg["c"],
            "downloads": (total["value"] if total else 0) or agg["d"],
            "banned": banned["c"],
            "blocked": blocked["c"],
            "active_24h": active["c"],
            "new_24h": new["c"],
            "downloads_24h": today["c"],
            "cached": cached["c"],
            "top": [dict(r) for r in top],
        }


db = Database(os.environ.get("DB_PATH", "data/bot.db"))
