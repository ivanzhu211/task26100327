"""discussion 库的单元测试。"""

import unittest

import discussion


def collect_all(conn, board_id, limit):
    """从头翻完一个会话，返回 (逐页 id 列表, 扁平 id 列表)。"""
    pages = []
    cursor = None
    while True:
        threads, cursor = discussion.list_threads(conn, board_id, cursor, limit)
        pages.append([t["id"] for t in threads])
        if cursor is None:
            break
    return pages, [tid for page in pages for tid in page]


class TestBasicOps(unittest.TestCase):
    def setUp(self):
        self.conn = discussion.connect()

    def tearDown(self):
        self.conn.close()

    def test_create_thread_returns_id(self):
        t1 = discussion.create_thread(self.conn, 1, 100, "hello")
        t2 = discussion.create_thread(self.conn, 1, 100, "world")
        self.assertIsInstance(t1, int)
        self.assertNotEqual(t1, t2)

    def test_vote_idempotent(self):
        t = discussion.create_thread(self.conn, 1, 100, "x")
        discussion.vote(self.conn, t, 1, True)
        discussion.vote(self.conn, t, 1, True)  # 重复点赞仍是一票
        discussion.vote(self.conn, t, 2, True)
        threads, _ = discussion.list_threads(self.conn, 1)
        self.assertEqual(threads[0]["votes"], 2)
        discussion.vote(self.conn, t, 1, False)
        discussion.vote(self.conn, t, 1, False)  # 重复取消幂等
        discussion.vote(self.conn, t, 3, False)  # 没投过也幂等
        threads, _ = discussion.list_threads(self.conn, 1)
        self.assertEqual(threads[0]["votes"], 1)

    def test_ordering_votes_then_id_desc(self):
        t1 = discussion.create_thread(self.conn, 1, 100, "a")
        t2 = discussion.create_thread(self.conn, 1, 100, "b")
        t3 = discussion.create_thread(self.conn, 1, 100, "c")
        discussion.vote(self.conn, t1, 1, True)
        discussion.vote(self.conn, t3, 1, True)
        discussion.vote(self.conn, t3, 2, True)
        threads, _ = discussion.list_threads(self.conn, 1)
        # t3 两票最前；t1 一票其次；t2 零票最后
        self.assertEqual([t["id"] for t in threads], [t3, t1, t2])

    def test_pinned_first_newest_pin_first(self):
        t1 = discussion.create_thread(self.conn, 1, 100, "a")
        t2 = discussion.create_thread(self.conn, 1, 100, "b")
        t3 = discussion.create_thread(self.conn, 1, 100, "c")
        discussion.vote(self.conn, t3, 1, True)
        discussion.set_pinned(self.conn, t1, True)
        discussion.set_pinned(self.conn, t2, True)  # 更晚置顶，排更前
        threads, _ = discussion.list_threads(self.conn, 1)
        self.assertEqual([t["id"] for t in threads], [t2, t1, t3])
        discussion.set_pinned(self.conn, t2, False)
        threads, _ = discussion.list_threads(self.conn, 1)
        self.assertEqual([t["id"] for t in threads], [t1, t3, t2])

    def test_hide_takes_effect_immediately(self):
        t1 = discussion.create_thread(self.conn, 1, 100, "a")
        discussion.create_thread(self.conn, 1, 100, "b")
        discussion.hide(self.conn, t1)
        threads, _ = discussion.list_threads(self.conn, 1)
        self.assertNotIn(t1, [t["id"] for t in threads])

    def test_boards_are_isolated(self):
        discussion.create_thread(self.conn, 1, 100, "board1")
        discussion.create_thread(self.conn, 2, 100, "board2")
        threads, _ = discussion.list_threads(self.conn, 1)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["body"], "board1")

    def test_empty_board(self):
        threads, cursor = discussion.list_threads(self.conn, 99)
        self.assertEqual(threads, [])
        self.assertIsNone(cursor)


class TestPagination(unittest.TestCase):
    def setUp(self):
        self.conn = discussion.connect()

    def tearDown(self):
        self.conn.close()

    def test_multi_page_no_mutation(self):
        ids = [discussion.create_thread(self.conn, 1, 100, "t%d" % i) for i in range(5)]
        pages, flat = collect_all(self.conn, 1, 2)
        self.assertEqual([len(p) for p in pages], [2, 2, 1])
        # 全部零票，按 id 从大到小
        self.assertEqual(flat, list(reversed(ids)))

    def test_limit_boundary_exact_multiple(self):
        for i in range(4):
            discussion.create_thread(self.conn, 1, 100, "t%d" % i)
        pages, flat = collect_all(self.conn, 1, 2)
        self.assertEqual([len(p) for p in pages], [2, 2])
        self.assertEqual(len(flat), 4)


class TestSnapshotConsistency(unittest.TestCase):
    def setUp(self):
        self.conn = discussion.connect()

    def tearDown(self):
        self.conn.close()

    def test_mutations_between_pages(self):
        # 快照前：t5、t3 置顶（t5 更新）；非置顶按票数 t2(3) t4(2) t1(1) t6(0)
        t1 = discussion.create_thread(self.conn, 1, 100, "t1")
        t2 = discussion.create_thread(self.conn, 1, 100, "t2")
        t3 = discussion.create_thread(self.conn, 1, 100, "t3")
        t4 = discussion.create_thread(self.conn, 1, 100, "t4")
        t5 = discussion.create_thread(self.conn, 1, 100, "t5")
        t6 = discussion.create_thread(self.conn, 1, 100, "t6")
        for u in (1, 2, 3):
            discussion.vote(self.conn, t2, u, True)
        for u in (1, 2):
            discussion.vote(self.conn, t4, u, True)
        discussion.vote(self.conn, t1, 1, True)
        discussion.set_pinned(self.conn, t3, True)
        discussion.set_pinned(self.conn, t5, True)

        # 快照点排序：t5 t3 | t2 t4 | t1 t6
        page1, cursor = discussion.list_threads(self.conn, 1, None, 2)
        self.assertEqual([t["id"] for t in page1], [t5, t3])
        self.assertIsNotNone(cursor)

        # 翻页间隙的并发修改：
        for u in (10, 11, 12, 13, 14):
            discussion.vote(self.conn, t6, u, True)   # 游标后的 t6 被顶到游标前
        for u in (1, 2, 3):
            discussion.vote(self.conn, t2, u, False)  # 游标前的 t2 掉到游标后
        discussion.set_pinned(self.conn, t6, True)    # 新置顶
        discussion.set_pinned(self.conn, t5, False)   # 取消置顶
        discussion.hide(self.conn, t4)                # 隐藏
        t7 = discussion.create_thread(self.conn, 1, 100, "t7")  # 新主题

        page2, cursor = discussion.list_threads(self.conn, 1, cursor, 2)
        page3, cursor = discussion.list_threads(self.conn, 1, cursor, 2)
        self.assertIsNone(cursor)

        pages = [[t["id"] for t in page1],
                 [t["id"] for t in page2],
                 [t["id"] for t in page3]]
        flat = [tid for p in pages for tid in p]

        # 任何主题至多出现一次
        self.assertEqual(len(flat), len(set(flat)))
        # 顺序与快照点排序一致（去掉读取时已隐藏的 t4）
        self.assertEqual(flat, [t5, t3, t2, t1, t6])
        # 被隐藏的 t4 不出现
        self.assertNotIn(t4, flat)
        # 快照后新建的 t7 不出现
        self.assertNotIn(t7, flat)
        # 快照时存在且未被隐藏的全部出现
        self.assertEqual(set(flat), {t1, t2, t3, t5, t6})

    def test_new_thread_visible_in_fresh_session(self):
        t1 = discussion.create_thread(self.conn, 1, 100, "old")
        _, cursor = discussion.list_threads(self.conn, 1, None, 10)
        t2 = discussion.create_thread(self.conn, 1, 100, "new")
        # 旧会话看不到新主题
        threads, cursor = discussion.list_threads(self.conn, 1, cursor, 10)
        self.assertIsNone(cursor)
        # 新会话能看到
        threads, _ = discussion.list_threads(self.conn, 1, None, 10)
        self.assertEqual([t["id"] for t in threads], [t2, t1])


if __name__ == "__main__":
    unittest.main()
