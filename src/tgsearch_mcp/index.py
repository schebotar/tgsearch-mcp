from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .errors import TgSearchError
from .formatting import iso_from_ts, message_link
from .query import build_fts_query

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    username TEXT,
    type TEXT,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    date INTEGER NOT NULL,
    sender_id INTEGER,
    sender_name TEXT,
    text TEXT NOT NULL DEFAULT '',
    has_media INTEGER NOT NULL DEFAULT 0,
    topic_id INTEGER,
    reply_to_msg_id INTEGER,
    UNIQUE(chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS messages_chat_date ON messages(chat_id, date);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    content='messages',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TABLE IF NOT EXISTS index_state (
    chat_id INTEGER PRIMARY KEY,
    last_msg_id INTEGER NOT NULL DEFAULT 0,
    last_sync_ts INTEGER NOT NULL,
    total INTEGER NOT NULL DEFAULT 0
);
"""


class MessageIndex:
    """Local SQLite FTS5 index of Telegram messages."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.create_function(
            "pylower", 1, lambda value: (value or "").lower(), deterministic=True
        )
        for target, mode in ((self.path.parent, 0o700), (self.path, 0o600)):
            try:
                os.chmod(target, mode)
            except OSError:
                pass
        try:
            with self._lock:
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=NORMAL")
                self._db.execute("PRAGMA busy_timeout=5000")
                self._db.executescript(SCHEMA)
                self._migrate()
                self._db.commit()
        except sqlite3.OperationalError as exc:
            raise TgSearchError(
                "fts_unavailable",
                f"SQLite FTS5 is not available: {exc}",
                "Use a Python build with SQLite >= 3.9 and FTS5 enabled, or skip the local index tools.",
            ) from exc

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        """Bring an index created by an older version up to the current schema."""
        columns = {
            row[1] for row in self._db.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "reply_to_msg_id" not in columns:
            self._db.execute("ALTER TABLE messages ADD COLUMN reply_to_msg_id INTEGER")

    def upsert_chat(self, chat_id: int, title: str | None, username: str | None, kind: str | None) -> None:
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT INTO chats(chat_id, title, username, type, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    username = excluded.username,
                    type = excluded.type,
                    updated_at = excluded.updated_at
                """,
                (int(chat_id), title, username, kind, int(time.time())),
            )

    def upsert_messages(self, rows: Iterable[dict[str, Any]]) -> int:
        keys = (
            "chat_id",
            "msg_id",
            "date",
            "sender_id",
            "sender_name",
            "text",
            "has_media",
            "topic_id",
            "reply_to_msg_id",
        )
        prepared = [{key: row.get(key) for key in keys} for row in rows]
        if not prepared:
            return 0
        with self._lock, self._db:
            self._db.executemany(
                """
                INSERT INTO messages(chat_id, msg_id, date, sender_id, sender_name, text, has_media, topic_id, reply_to_msg_id)
                VALUES (:chat_id, :msg_id, :date, :sender_id, :sender_name, :text, :has_media, :topic_id, :reply_to_msg_id)
                ON CONFLICT(chat_id, msg_id) DO UPDATE SET
                    date = excluded.date,
                    sender_id = excluded.sender_id,
                    sender_name = excluded.sender_name,
                    text = excluded.text,
                    has_media = excluded.has_media,
                    topic_id = excluded.topic_id,
                    reply_to_msg_id = excluded.reply_to_msg_id
                """,
                prepared,
            )
        return len(prepared)

    def delete_chat(self, chat_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM messages WHERE chat_id = ?", (int(chat_id),))
            self._db.execute("DELETE FROM chats WHERE chat_id = ?", (int(chat_id),))
            self._db.execute("DELETE FROM index_state WHERE chat_id = ?", (int(chat_id),))

    def last_msg_id(self, chat_id: int) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(MAX(msg_id), 0) FROM messages WHERE chat_id = ?",
                (int(chat_id),),
            ).fetchone()
        return int(row[0] or 0)

    def count_chat(self, chat_id: int) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = ?", (int(chat_id),)
            ).fetchone()
        return int(row[0] or 0)

    def find_chat(self, ref: str) -> int | None:
        """Resolve a chat reference against the local index only (offline)."""
        text = str(ref).strip()
        if not text:
            return None
        if text.lstrip("-").isdigit():
            return int(text)
        username = text.lstrip("@").casefold()
        with self._lock:
            row = self._db.execute(
                "SELECT chat_id FROM chats WHERE LOWER(username) = ? LIMIT 1", (username,)
            ).fetchone()
            if row is not None:
                return int(row[0])
            row = self._db.execute(
                "SELECT chat_id FROM chats WHERE LOWER(title) = ? LIMIT 1", (text.casefold(),)
            ).fetchone()
        return int(row[0]) if row is not None else None

    def set_state(self, chat_id: int, last_msg_id: int) -> None:
        with self._lock, self._db:
            total = self._db.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = ?", (int(chat_id),)
            ).fetchone()[0]
            self._db.execute(
                """
                INSERT INTO index_state(chat_id, last_msg_id, last_sync_ts, total)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    last_msg_id = MAX(index_state.last_msg_id, excluded.last_msg_id),
                    last_sync_ts = excluded.last_sync_ts,
                    total = excluded.total
                """,
                (int(chat_id), int(last_msg_id), int(time.time()), int(total)),
            )

    def search(
        self,
        query: str,
        *,
        chat_id: int | None = None,
        chat_ids: list[int] | None = None,
        limit: int = 20,
        mode: str = "auto",
        min_ts: int | None = None,
        max_ts: int | None = None,
        sender: str | None = None,
        has_media: bool = False,
        topic_id: int | None = None,
        sort: str = "relevance",
    ) -> list[dict[str, Any]]:
        if sort not in {"relevance", "date"}:
            raise ValueError("sort must be 'relevance' or 'date'")
        match = build_fts_query(query, mode)
        sql = """
            SELECT m.chat_id, m.msg_id, m.date, m.sender_name, m.topic_id, m.has_media,
                   m.reply_to_msg_id, c.title AS chat_title, c.username AS chat_username,
                   snippet(messages_fts, 0, '**', '**', '…', 24) AS snippet,
                   bm25(messages_fts) AS score
            FROM messages_fts
            JOIN messages m ON m.rowid = messages_fts.rowid
            LEFT JOIN chats c ON c.chat_id = m.chat_id
            WHERE messages_fts MATCH ?
        """
        params: list[Any] = [match]
        if chat_id is not None:
            sql += " AND m.chat_id = ?"
            params.append(int(chat_id))
        elif chat_ids:
            placeholders = ", ".join("?" for _ in chat_ids)
            sql += f" AND m.chat_id IN ({placeholders})"
            params.extend(int(value) for value in chat_ids)
        if min_ts is not None:
            sql += " AND m.date >= ?"
            params.append(int(min_ts))
        if max_ts is not None:
            sql += " AND m.date <= ?"
            params.append(int(max_ts))
        if sender:
            text = str(sender).strip()
            if text.lstrip("-").isdigit():
                sql += " AND m.sender_id = ?"
                params.append(int(text))
            else:
                sql += " AND instr(pylower(m.sender_name), ?) > 0"
                params.append(text.lower())
        if has_media:
            sql += " AND m.has_media = 1"
        if topic_id is not None:
            sql += " AND m.topic_id = ?"
            params.append(int(topic_id))
        sql += " ORDER BY m.date DESC, m.msg_id DESC" if sort == "date" else " ORDER BY score"
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        results = []
        for row in rows:
            item: dict[str, Any] = {
                "chat_id": row["chat_id"],
                "chat_title": row["chat_title"],
                "msg_id": row["msg_id"],
                "date": iso_from_ts(row["date"]),
                "snippet": row["snippet"],
            }
            if row["sender_name"]:
                item["sender"] = row["sender_name"]
            if row["topic_id"]:
                item["topic_id"] = row["topic_id"]
            if row["has_media"]:
                item["has_media"] = True
            if row["reply_to_msg_id"]:
                item["reply_to"] = row["reply_to_msg_id"]
            link = message_link(row["chat_id"], row["chat_username"], row["msg_id"], row["topic_id"])
            if link:
                item["link"] = link
            results.append(item)
        return results

    def stats(self) -> dict[str, Any]:
        with self._lock:
            messages = self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            chats = self._db.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
        return {"path": str(self.path), "messages": int(messages), "chats": int(chats)}

    def chat_stats(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT c.chat_id, c.title, c.username, c.type,
                       COUNT(m.msg_id) AS messages, COALESCE(MAX(m.msg_id), 0) AS last_msg_id
                FROM chats c
                LEFT JOIN messages m ON m.chat_id = c.chat_id
                GROUP BY c.chat_id
                ORDER BY messages DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        out = []
        for row in rows:
            item = {
                "chat_id": row["chat_id"],
                "title": row["title"],
                "type": row["type"],
                "messages": int(row["messages"]),
                "last_msg_id": int(row["last_msg_id"]),
            }
            if row["username"]:
                item["username"] = row["username"]
            out.append(item)
        return out
