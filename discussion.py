"""课程讨论区主题列表组件：按热度排序 + 游标翻页（快照语义）。

仅依赖标准库 sqlite3。每个课程讨论板(board)一个讨论区。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id   INTEGER NOT NULL,
    author_id  INTEGER NOT NULL,
    body       TEXT    NOT NULL,
    hidden     INTEGER NOT NULL DEFAULT 0,
    pinned     INTEGER NOT NULL DEFAULT 0,
    pinned_seq INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_threads_board ON threads(board_id);

CREATE TABLE IF NOT EXISTS votes (
    thread_id INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    PRIMARY KEY (thread_id, user_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_items (
    snapshot_id INTEGER NOT NULL,
    position    INTEGER NOT NULL,
    thread_id   INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, position)
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class Thread:
    """列表页返回的单条主题。"""

    id: int
    board_id: int
    author_id: int
    body: str
    pinned: bool
    votes: int


@dataclass(frozen=True)
class Page:
    """一页结果：threads 为本页主题，next_cursor 为下一页游标（None 表示翻完）。"""

    threads: list[Thread]
    next_cursor: Optional[str]


class DiscussionBoard:
    """讨论区组件。传入 sqlite3 连接或数据库文件路径。"""

    def __init__(self, db: "sqlite3.Connection | str" = ":memory:") -> None:
        if isinstance(db, sqlite3.Connection):
            self._conn = db
        else:
            self._conn = sqlite3.connect(db)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _next_seq(self, name: str) -> int:
        """单调递增计数器，用于置顶时间等需要全序的场景。"""
        cur = self._conn.execute(
            "INSERT INTO counters(name, value) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET value = value + 1 "
            "RETURNING value",
            (name,),
        )
        seq = cur.fetchone()[0]
        self._conn.commit()
        return seq

    def create_thread(self, board_id: int, author_id: int, body: str) -> int:
        """发主题帖，返回主题 id。"""
        cur = self._conn.execute(
            "INSERT INTO threads(board_id, author_id, body) VALUES (?, ?, ?)",
            (board_id, author_id, body),
        )
        self._conn.commit()
        return cur.lastrowid

    def vote(self, thread_id: int, user_id: int, up: bool) -> None:
        """点赞/取消点赞；同一用户同一主题最多一票，重复操作幂等。"""
        if up:
            self._conn.execute(
                "INSERT OR IGNORE INTO votes(thread_id, user_id) VALUES (?, ?)",
                (thread_id, user_id),
            )
        else:
            self._conn.execute(
                "DELETE FROM votes WHERE thread_id = ? AND user_id = ?",
                (thread_id, user_id),
            )
        self._conn.commit()

    def set_pinned(self, thread_id: int, pinned: bool) -> None:
        """置顶或取消置顶。置顶时记录单调递增序号作为置顶时间。"""
        if pinned:
            seq = self._next_seq("pin")
            self._conn.execute(
                "UPDATE threads SET pinned = 1, pinned_seq = ? WHERE id = ?",
                (seq, thread_id),
            )
        else:
            self._conn.execute(
                "UPDATE threads SET pinned = 0, pinned_seq = 0 WHERE id = ?",
                (thread_id,),
            )
        self._conn.commit()

    def hide(self, thread_id: int) -> None:
        """管理员隐藏主题，立即生效。"""
        self._conn.execute("UPDATE threads SET hidden = 1 WHERE id = ?", (thread_id,))
        self._conn.commit()

    def list_threads(
        self, board_id: int, cursor: Optional[str] = None, limit: int = 20
    ) -> Page:
        """返回一页主题和下一页游标；cursor 为 None 表示从头开始（建立快照点）。

        快照语义：首次调用（cursor=None）把当前排序结果固化为一张有序表，
        之后的翻页沿该表推进；隐藏状态按读取时判断，快照后新建的主题不出现。
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if cursor is None:
            snapshot_id, position = self._create_snapshot(board_id), 0
        else:
            snapshot_id, position = self._parse_cursor(cursor)

        rows = self._conn.execute(
            """
            SELECT i.position, t.id, t.board_id, t.author_id, t.body, t.pinned,
                   (SELECT COUNT(*) FROM votes v WHERE v.thread_id = t.id) AS votes
            FROM snapshot_items i
            JOIN threads t ON t.id = i.thread_id
            WHERE i.snapshot_id = ? AND i.position >= ? AND t.hidden = 0
            ORDER BY i.position
            LIMIT ?
            """,
            (snapshot_id, position, limit + 1),
        ).fetchall()

        page_rows = rows[:limit]
        threads = [
            Thread(
                id=r[1], board_id=r[2], author_id=r[3],
                body=r[4], pinned=bool(r[5]), votes=r[6],
            )
            for r in page_rows
        ]
        if len(rows) > limit:
            next_cursor = self._format_cursor(snapshot_id, rows[limit][0])
        else:
            next_cursor = None
        return Page(threads=threads, next_cursor=next_cursor)

    def _create_snapshot(self, board_id: int) -> int:
        """按当前置顶状态与票数固化排序，返回快照 id。"""
        cur = self._conn.execute(
            "INSERT INTO snapshots(board_id) VALUES (?)", (board_id,)
        )
        snapshot_id = cur.lastrowid
        self._conn.execute(
            """
            INSERT INTO snapshot_items(snapshot_id, position, thread_id)
            SELECT ?, ROW_NUMBER() OVER (
                       ORDER BY t.pinned DESC, t.pinned_seq DESC,
                                votes DESC, t.id DESC
                   ) - 1, t.id
            FROM (
                SELECT t.*, (SELECT COUNT(*) FROM votes v
                             WHERE v.thread_id = t.id) AS votes
                FROM threads t
                WHERE t.board_id = ?
            ) t
            """,
            (snapshot_id, board_id),
        )
        self._conn.commit()
        return snapshot_id

    @staticmethod
    def _format_cursor(snapshot_id: int, position: int) -> str:
        return f"{snapshot_id}:{position}"

    @staticmethod
    def _parse_cursor(cursor: str) -> "tuple[int, int]":
        try:
            snapshot_id, position = cursor.split(":")
            return int(snapshot_id), int(position)
        except (ValueError, AttributeError):
            raise ValueError(f"invalid cursor: {cursor!r}") from None
