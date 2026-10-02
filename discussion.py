"""课程讨论区主题列表组件：热度排序 + 游标翻页（快照一致性）。

快照语义：不带游标的 list_threads 在快照表中固化当时的排序（置顶状态、
置顶时间、票数、主题 id），游标只记录 (snapshot_id, offset)。翻页时按
固化顺序取行，再按读取时的 hidden 标志过滤，因此满足：
- 任何主题至多出现一次；
- 各页拼接顺序与快照点排序一致；
- 快照时存在、读取时未隐藏的主题必然出现，已隐藏的不出现；
- 快照之后新建的主题不出现。
"""

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    hidden INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    pinned_seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS votes (
    thread_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (thread_id, user_id)
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    board_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_items (
    snapshot_id INTEGER NOT NULL,
    pos INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, pos)
);
"""


def connect(path=":memory:"):
    """创建数据库连接并初始化 schema。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


def _next_seq(conn, name):
    conn.execute(
        "INSERT INTO counters (name, value) VALUES (?, 1) "
        "ON CONFLICT(name) DO UPDATE SET value = value + 1",
        (name,),
    )
    return conn.execute(
        "SELECT value FROM counters WHERE name = ?", (name,)
    ).fetchone()[0]


def create_thread(conn, board_id, author_id, body):
    """发主题帖，返回主题 id。"""
    cur = conn.execute(
        "INSERT INTO threads (board_id, author_id, body) VALUES (?, ?, ?)",
        (board_id, author_id, body),
    )
    conn.commit()
    return cur.lastrowid


def vote(conn, thread_id, user_id, up):
    """点赞或取消点赞；同一用户最多一票，重复操作幂等。"""
    if up:
        conn.execute(
            "INSERT OR IGNORE INTO votes (thread_id, user_id) VALUES (?, ?)",
            (thread_id, user_id),
        )
    else:
        conn.execute(
            "DELETE FROM votes WHERE thread_id = ? AND user_id = ?",
            (thread_id, user_id),
        )
    conn.commit()


def set_pinned(conn, thread_id, pinned):
    """置顶或取消置顶。置顶时间用全局递增序号记录，新的置顶排更前。"""
    if pinned:
        seq = _next_seq(conn, "pin")
        conn.execute(
            "UPDATE threads SET pinned = 1, pinned_seq = ? WHERE id = ?",
            (seq, thread_id),
        )
    else:
        conn.execute(
            "UPDATE threads SET pinned = 0, pinned_seq = 0 WHERE id = ?",
            (thread_id,),
        )
    conn.commit()


def hide(conn, thread_id):
    """管理员隐藏主题，立即生效。"""
    conn.execute("UPDATE threads SET hidden = 1 WHERE id = ?", (thread_id,))
    conn.commit()


def _create_snapshot(conn, board_id):
    """固化当前 board 的排序，返回 snapshot id。"""
    cur = conn.execute("INSERT INTO snapshots (board_id) VALUES (?)", (board_id,))
    sid = cur.lastrowid
    conn.execute(
        """
        INSERT INTO snapshot_items (snapshot_id, pos, thread_id)
        SELECT ?, ROW_NUMBER() OVER (
                   ORDER BY t.pinned DESC, t.pinned_seq DESC,
                            votes.cnt DESC, t.id DESC
               ) - 1, t.id
        FROM threads t
        LEFT JOIN (
            SELECT thread_id, COUNT(*) AS cnt FROM votes GROUP BY thread_id
        ) AS votes ON votes.thread_id = t.id
        WHERE t.board_id = ?
        """,
        (sid, board_id),
    )
    conn.commit()
    return sid


def _parse_cursor(cursor):
    sid_s, off_s = cursor.split(":", 1)
    return int(sid_s), int(off_s)


def list_threads(conn, board_id, cursor=None, limit=10):
    """返回 (threads, next_cursor)。cursor 为 None 表示从头开始翻。

    threads 为 dict 列表，含 id/board_id/author_id/body/pinned/hidden/votes。
    next_cursor 为 None 表示已翻完。
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if cursor is None:
        sid = _create_snapshot(conn, board_id)
        offset = 0
    else:
        sid, offset = _parse_cursor(cursor)

    rows = conn.execute(
        "SELECT thread_id FROM snapshot_items "
        "WHERE snapshot_id = ? AND pos >= ? ORDER BY pos LIMIT ?",
        (sid, offset, limit + 1),
    ).fetchall()

    page = rows[:limit]
    if len(rows) > limit:
        next_cursor = "%d:%d" % (sid, offset + limit)
    else:
        next_cursor = None

    threads = []
    for (tid,) in page:
        row = conn.execute(
            "SELECT t.id, t.board_id, t.author_id, t.body, t.pinned, t.hidden, "
            "       COALESCE(v.cnt, 0) "
            "FROM threads t "
            "LEFT JOIN (SELECT thread_id, COUNT(*) AS cnt FROM votes "
            "           GROUP BY thread_id) v ON v.thread_id = t.id "
            "WHERE t.id = ?",
            (tid,),
        ).fetchone()
        if row is None or row[5]:
            continue  # 读取时已被隐藏（或已删除）的主题不出现
        threads.append(
            {
                "id": row[0],
                "board_id": row[1],
                "author_id": row[2],
                "body": row[3],
                "pinned": bool(row[4]),
                "hidden": False,
                "votes": row[6],
            }
        )
    return threads, next_cursor
