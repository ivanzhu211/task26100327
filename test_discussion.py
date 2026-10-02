import unittest

from discussion import DiscussionBoard


class OrderingTest(unittest.TestCase):
    def setUp(self):
        self.b = DiscussionBoard()

    def test_pinned_first_then_votes_then_id_desc(self):
        t = [self.b.create_thread(1, 10, f"p{i}") for i in range(6)]
        # 票数: t1:2, t2:2, t3:1, t4:0, t5:0, t6:0
        for u in (1, 2):
            self.b.vote(t[0], u, True)
            self.b.vote(t[1], u, True)
        self.b.vote(t[2], 1, True)
        # 置顶 t5 再置顶 t6：置顶时间新的在前
        self.b.set_pinned(t[4], True)
        self.b.set_pinned(t[5], True)
        page = self.b.list_threads(1, None, 20)
        self.assertEqual(
            [x.id for x in page.threads],
            [t[5], t[4], t[1], t[0], t[2], t[3]],
        )
        self.assertIsNone(page.next_cursor)

    def test_unpin_falls_back_to_hot_rank(self):
        t1 = self.b.create_thread(1, 10, "a")
        t2 = self.b.create_thread(1, 10, "b")
        self.b.set_pinned(t1, True)
        self.b.vote(t2, 1, True)
        self.b.set_pinned(t1, False)
        page = self.b.list_threads(1, None, 20)
        self.assertEqual([x.id for x in page.threads], [t2, t1])

    def test_boards_are_isolated(self):
        a = self.b.create_thread(1, 10, "board1")
        self.b.create_thread(2, 10, "board2")
        page = self.b.list_threads(1, None, 20)
        self.assertEqual([x.id for x in page.threads], [a])


class MutationTest(unittest.TestCase):
    def setUp(self):
        self.b = DiscussionBoard()

    def test_vote_idempotent(self):
        t = self.b.create_thread(1, 10, "x")
        self.b.vote(t, 1, True)
        self.b.vote(t, 1, True)
        self.b.vote(t, 1, True)
        page = self.b.list_threads(1, None, 20)
        self.assertEqual(page.threads[0].votes, 1)
        self.b.vote(t, 1, False)
        self.b.vote(t, 1, False)  # 重复取消也幂等
        page = self.b.list_threads(1, None, 20)
        self.assertEqual(page.threads[0].votes, 0)

    def test_hide_immediate(self):
        t = self.b.create_thread(1, 10, "x")
        self.b.hide(t)
        page = self.b.list_threads(1, None, 20)
        self.assertEqual(page.threads, [])


class PaginationTest(unittest.TestCase):
    def test_walk_to_end(self):
        b = DiscussionBoard()
        ids = [b.create_thread(1, 10, f"p{i}") for i in range(7)]
        seen, cursor = [], None
        while True:
            page = b.list_threads(1, cursor, 3)
            seen.extend(x.id for x in page.threads)
            cursor = page.next_cursor
            if cursor is None:
                break
        self.assertEqual(seen, list(reversed(ids)))  # 全 0 票，按 id 从大到小

    def test_empty_board(self):
        b = DiscussionBoard()
        page = b.list_threads(99, None, 10)
        self.assertEqual(page.threads, [])
        self.assertIsNone(page.next_cursor)

    def test_invalid_cursor_rejected(self):
        b = DiscussionBoard()
        with self.assertRaises(ValueError):
            b.list_threads(1, "not-a-cursor", 10)


class SnapshotSessionTest(unittest.TestCase):
    """翻页会话期间并发发生投票/置顶/隐藏/发帖，会话仍满足快照语义。"""

    def test_session_invariants_under_mutation(self):
        b = DiscussionBoard()
        # 8 个主题，初始票数: t0:5 t1:4 t2:3 t3:2 t4:1 t5:0 t6:0 t7:0
        t = [b.create_thread(1, 10, f"p{i}") for i in range(8)]
        for i in range(5):
            for u in range(5 - i):
                b.vote(t[i], 1000 + u, True)
        # 快照点排序: t0 t1 t2 t3 t4 t5 t6 t7（t5/t6/t7 同 0 票按 id 倒序 -> t7 t6 t5）
        snapshot_order = [t[0], t[1], t[2], t[3], t[4], t[7], t[6], t[5]]

        page1 = b.list_threads(1, None, 3)
        self.assertEqual([x.id for x in page1.threads], snapshot_order[:3])

        # ---- 两页之间的并发变更 ----
        # 游标之后的 t5 被点赞顶到游标之前
        for u in range(10):
            b.vote(t[5], 2000 + u, True)
        # 游标之前的 t0 被取消点赞掉到游标之后
        for u in range(5):
            b.vote(t[0], 1000 + u, False)
        # t6 被置顶，t0 先置顶又取消置顶
        b.set_pinned(t[6], True)
        b.set_pinned(t[0], True)
        b.set_pinned(t[0], False)
        # t3 被隐藏
        b.hide(t[3])
        # 新主题发布
        b.create_thread(1, 10, "latecomer")

        # ---- 继续沿旧游标翻完 ----
        rest, cursor = [], page1.next_cursor
        while cursor is not None:
            page = b.list_threads(1, cursor, 3)
            rest.extend(x.id for x in page.threads)
            cursor = page.next_cursor

        session = [x.id for x in page1.threads] + rest
        # 快照后新建的主题不出现
        self.assertNotIn(9, session)
        # 任何主题至多出现一次
        self.assertEqual(len(session), len(set(session)))
        # 已隐藏的 t3 不出现；快照时存在且未隐藏的全部出现
        self.assertNotIn(t[3], session)
        expected = [x for x in snapshot_order if x != t[3]]
        self.assertEqual(session, expected)

    def test_new_snapshot_sees_mutations(self):
        """同一份数据，cursor=None 重新开会话则按新状态排序。"""
        b = DiscussionBoard()
        t1 = b.create_thread(1, 10, "a")
        t2 = b.create_thread(1, 10, "b")
        page = b.list_threads(1, None, 10)
        self.assertEqual([x.id for x in page.threads], [t2, t1])
        b.vote(t1, 1, True)
        page = b.list_threads(1, None, 10)
        self.assertEqual([x.id for x in page.threads], [t1, t2])

    def test_hidden_between_pages_disappears(self):
        b = DiscussionBoard()
        ids = [b.create_thread(1, 10, f"p{i}") for i in range(5)]
        page1 = b.list_threads(1, None, 2)
        b.hide(ids[4])  # 快照第一位，尚未读到
        b.hide(ids[0])  # 快照最后一位
        rest, cursor = [], page1.next_cursor
        while cursor is not None:
            page = b.list_threads(1, cursor, 2)
            rest.extend(x.id for x in page.threads)
            cursor = page.next_cursor
        session = [x.id for x in page1.threads] + rest
        # ids[4] 在隐藏前已被第一页读出；ids[0] 读到时已隐藏，不出现
        self.assertEqual(session, [ids[4], ids[3], ids[2], ids[1]])


if __name__ == "__main__":
    unittest.main()
