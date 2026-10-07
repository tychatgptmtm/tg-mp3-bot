"""Хранилище данных бота (SQLite): пользователи, качество, баны, статистика."""
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
                """
            )
            self._conn.commit()

    # ---------------------------------------------------------- пользователи

    def add_user(self, user) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO users (id, username, first_name, joined)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       username = excluded.username,
                       first_name = excluded.first_name""",
                (user.id, user.username, user.first_name, int(time.time())),
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

    def all_user_ids(self, include_banned: bool = False):
        q = "SELECT id FROM users" + ("" if include_banned else " WHERE banned = 0")
        with self._lock:
            return [r["id"] for r in self._conn.execute(q).fetchall()]

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
            top = self._conn.execute(
                "SELECT first_name, username, downloads FROM users "
                "ORDER BY downloads DESC LIMIT 5"
            ).fetchall()
        return {
            "users": agg["c"],
            "downloads": (total["value"] if total else 0) or agg["d"],
            "banned": banned["c"],
            "top": [dict(r) for r in top],
        }


db = Database(os.environ.get("DB_PATH", "data/bot.db"))
