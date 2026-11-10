"""R-09 状态机：四个动词子命令 + 合法流转校验 + 额度语义。

报告里承诺的流程是 `APPLIED → ACCEPTED → (RECEIVED → CLOSED) | REJECTED`，
本文件把这条流程的**每一跳**都用真 CLI 走一遍，并锁死三件容易做错的事：

1. 不许跳步（APPLIED 不能直接 CLOSED）；
2. 终态不许复活（CLOSED / REJECTED 之后任何改动都要被拒）；
3. 转人工的单子放行前必须**再核一次额度**（它当初没占额度，期间可能被占掉）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _support import NOW, DbCase, run_cli

import db
import main
import rules


class StateCase(DbCase):
    """公用底座：导入 tiny.csv，提供"挪一步"和"看一眼状态"的短方法。"""

    LINE = "Year 2009-2010|489434|3"   # 客户 17850，可退 5
    GUEST = "Year 2009-2010|489434|7"  # 缺客户号，可退 3（走人工队列）

    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def move(self, verb: str, *extra: str, req: str = "R-0001") -> tuple[int, str]:
        return run_cli(verb, "--db", self.db_path, "--req", req, "--now", NOW, *extra)

    def status_of(self, req: str = "R-0001") -> str:
        conn = self.conn()
        try:
            row = db.get_request_by_no(conn, req)
            return "" if row is None else row.status
        finally:
            conn.close()

    def active_qty(self, line_key: str | None = None) -> int:
        conn = self.conn()
        try:
            return db.sum_active_qty(conn, line_key or self.LINE)
        finally:
            conn.close()

    def log_edges(self, req: str = "R-0001") -> list[tuple[str | None, str]]:
        conn = self.conn()
        try:
            return [(r["from_status"], r["to_status"]) for r in db.get_status_log(conn, req)]
        finally:
            conn.close()


# ---------------------------------------------------------------- 正常流转


class TestStateHappyPath(StateCase):
    def test_accept_moves_applied_to_accepted(self):
        code, out = self.apply("--invoice", "489434", "--seq", "3", "--qty", "1")
        self.assertEqual(code, 0, out)
        code, out = self.move("accept")
        self.assertEqual(code, 0, out)
        self.assertIn("已受理", out)
        self.assertEqual(self.status_of(), "ACCEPTED")
        self.assertEqual(self.active_qty(), 1)

    def test_full_chain_to_closed(self):
        self.apply("--invoice", "489434", "--seq", "3", "--qty", "1")
        for verb in ("accept", "receive", "close"):
            code, out = self.move(verb)
            self.assertEqual(code, 0, f"{verb} 失败：{out}")
        self.assertEqual(self.status_of(), "CLOSED")
        self.assertEqual(
            self.log_edges(),
            [
                (None, "APPLIED"),
                ("APPLIED", "ACCEPTED"),
                ("ACCEPTED", "RECEIVED"),
                ("RECEIVED", "CLOSED"),
            ],
        )

    def test_close_frees_quota_and_allows_reapply(self):
        # 这行原始可退 5，但 R-04 FIFO 已经把 1 件算作历史已退，实际可退 4
        self.apply("--invoice", "489434", "--seq", "3", "--qty", "4")
        for verb in ("accept", "receive", "close"):
            self.move(verb)
        self.assertEqual(self.active_qty(), 0)
        # R-06 例外：之前那条已 CLOSED → 可以再次申请
        code, out = self.apply("--invoice", "489434", "--seq", "3", "--qty", "2")
        self.assertEqual(code, 0, out)
        self.assertIn("R-0002", out)
        self.assertEqual(self.active_qty(), 2)

    def test_reject_frees_quota_and_allows_reapply(self):
        self.apply("--invoice", "489434", "--seq", "3", "--qty", "4")
        self.assertEqual(self.active_qty(), 4)
        code, out = self.move("reject", "--note", "客户取消寄回")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.status_of(), "REJECTED")
        self.assertEqual(self.active_qty(), 0)
        code, out = self.apply("--invoice", "489434", "--seq", "3", "--qty", "1")
        self.assertEqual(code, 0, out)

    def test_operator_and_note_are_recorded(self):
        self.apply("--invoice", "489434", "--seq", "3", "--qty", "1")
        self.move("accept", "--operator", "客服主管-李", "--note", "电话核实后受理")
        conn = self.conn()
        try:
            last = db.get_status_log(conn, "R-0001")[-1]
        finally:
            conn.close()
        self.assertEqual(last["operator"], "客服主管-李")
        self.assertIn("电话核实", last["note"])


# ---------------------------------------------------------------- 非法流转


class TestStateIllegal(StateCase):
    def test_cannot_skip_accept_to_close(self):
        self.apply("--invoice", "489434", "--seq", "3", "--qty", "1")
        code, out = self.move("close")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("E_BAD_TRANSITION", out)
        self.assertIn("下一步只能是", out)
        self.assertEqual(self.status_of(), "APPLIED")
        self.assertEqual(len(self.log_edges()), 1)

    def test_cannot_receive_before_accept(self):
        self.apply("--invoice", "489434", "--seq", "3", "--qty", "1")
        code, out = self.move("receive")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertEqual(self.status_of(), "APPLIED")

    def test_closed_is_terminal(self):
        self.apply("--invoice", "489434", "--seq", "3", "--qty", "1")
        for verb in ("accept", "receive", "close"):
            self.move(verb)
        for verb in ("accept", "receive", "close", "reject"):
            code, out = self.move(verb)
            self.assertEqual(code, main.EXIT_REJECT, f"{verb} 竟然通过了：{out}")
            self.assertIn("终态", out)
        self.assertEqual(self.status_of(), "CLOSED")

    def test_unknown_request_number(self):
        code, out = self.move("accept", req="R-9999")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("E_NOT_FOUND", out)


# ---------------------------------------------------------------- 人工队列 R-08


class TestStateManualQueue(StateCase):
    def test_manual_accept_consumes_quota(self):
        code, out = self.apply("--invoice", "489434", "--seq", "7", "--qty", "3")
        self.assertEqual(code, main.EXIT_MANUAL, out)
        self.assertEqual(self.active_qty(self.GUEST), 0)
        code, out = self.move("accept", "--note", "客户电话核实，人工放行")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.status_of(), "ACCEPTED")
        self.assertEqual(self.active_qty(self.GUEST), 3)

    def test_manual_reject_frees_nothing_but_closes_case(self):
        self.apply("--invoice", "489434", "--seq", "7", "--qty", "3")
        code, out = self.move("reject", "--note", "无法核实归属")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.status_of(), "REJECTED")
        self.assertEqual(self.active_qty(self.GUEST), 0)

    def test_manual_accept_rechecks_quota(self):
        """防超卖：两条转人工单合计 5 件，但这条明细只有 3 件可退。"""
        self.apply("--invoice", "489434", "--seq", "7", "--qty", "3")
        self.apply("--invoice", "489434", "--seq", "7", "--qty", "2")
        code, out = self.move("accept", req="R-0001")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.active_qty(self.GUEST), 3)
        code, out = self.move("accept", req="R-0002")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("E_EXCEED", out)
        self.assertIn("保持 PENDING_MANUAL", out)
        self.assertEqual(self.status_of("R-0002"), "PENDING_MANUAL")


# ---------------------------------------------------------------- 查询页显示


class TestQueryShowsState(StateCase):
    """查询页的"状态"列必须与库里的真相一致，不能说反话。"""

    def test_query_shows_pending_manual_not_applicable(self):
        self.apply("--invoice", "489434", "--seq", "7", "--qty", "1")
        code, out = self.query("489434")
        self.assertEqual(code, 0, out)
        self.assertIn("PENDING_MANUAL(R-0001 1件)", out)
        # 修 bug 前这里会写"可申请(缺客户号→人工)"，与异常队列自相矛盾
        self.assertNotIn("可申请(缺客户号→人工)", out)

    def test_query_shows_accepted_with_occupancy(self):
        self.apply("--invoice", "489434", "--seq", "3", "--qty", "2")
        self.move("accept")
        code, out = self.query("489434")
        self.assertEqual(code, 0, out)
        self.assertIn("ACCEPTED(R-0001 2件)", out)

    def test_closed_line_becomes_applicable_again(self):
        self.apply("--invoice", "489434", "--seq", "3", "--qty", "2")
        for verb in ("accept", "receive", "close"):
            self.move(verb)
        code, out = self.query("489434")
        self.assertEqual(code, 0, out)
        self.assertIn("可申请", out)
        self.assertNotIn("ACCEPTED", out)


# ---------------------------------------------------------------- 纯函数层


class TestTransitionTable(unittest.TestCase):
    """不碰数据库：流转表单测，保证报告里那张表就是代码里那张表。"""

    def test_legal_edges(self):
        for src, dst in (
            ("APPLIED", "ACCEPTED"),
            ("APPLIED", "REJECTED"),
            ("PENDING_MANUAL", "ACCEPTED"),
            ("PENDING_MANUAL", "REJECTED"),
            ("ACCEPTED", "RECEIVED"),
            ("RECEIVED", "CLOSED"),
        ):
            self.assertTrue(rules.check_transition(src, dst).ok, f"{src}→{dst} 应当合法")

    def test_illegal_edges(self):
        for src, dst in (
            ("APPLIED", "CLOSED"),
            ("APPLIED", "RECEIVED"),
            ("ACCEPTED", "CLOSED"),
            ("CLOSED", "ACCEPTED"),
            ("REJECTED", "ACCEPTED"),
            ("ACCEPTED", "ACCEPTED"),
        ):
            result = rules.check_transition(src, dst)
            self.assertFalse(result.ok, f"{src}→{dst} 应当被拒")
            self.assertEqual(result.code, "E_BAD_TRANSITION")

    def test_unknown_status_is_reported(self):
        result = rules.check_transition("WHATEVER", "ACCEPTED")
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "E_BAD_STATUS")

    def test_every_status_has_a_chinese_label(self):
        for status in rules.TRANSITIONS:
            self.assertIn(status, rules.STATUS_LABELS)

    def test_previous_apply_still_works(self):
        """历史状态机里前置状态没被改坏：PENDING_MANUAL 与 APPLIED 都只能往两处走。"""
        self.assertEqual(rules.next_statuses("APPLIED"), ("ACCEPTED", "REJECTED"))
        self.assertEqual(rules.next_statuses("PENDING_MANUAL"), ("ACCEPTED", "REJECTED"))
        self.assertEqual(rules.next_statuses("CLOSED"), ())


if __name__ == "__main__":
    unittest.main()
