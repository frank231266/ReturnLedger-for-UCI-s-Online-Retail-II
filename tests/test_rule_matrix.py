"""R-01 ~ R-09 逐条对应的单元测试矩阵。

命名约定：一个规则一个 `TestR0x*` 类，`unittest -v` 的输出里直接能看到规则编号。
`TestCoverageMatrix` 会自检"九条规则每条都有测试类且至少一个 test_ 方法"，
防止以后删测试导致某条规则裸奔。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _support import LATER, NOW, OVERLAP, SAMPLE, DbCase, run_cli

import csv_loader
import db
import main
import rules
import verify


# ---------------------------------------------------------------- R-01 明细键


class TestR01DetailKey(DbCase):
    def test_key_is_sheet_invoice_seq(self):
        self.do_import()
        conn = self.conn()
        try:
            line = db.get_line(conn, "Year 2009-2010|489434|1")
            self.assertIsNotNone(line)
            parts = line.line_key.split("|")
            self.assertEqual(len(parts), 3)
            self.assertEqual(parts[0], line.sheet)
            self.assertEqual(parts[1], line.invoice)
            self.assertEqual(int(parts[2]), line.seq)
        finally:
            conn.close()

    def test_same_invoice_same_stock_get_two_distinct_lines(self):
        """只用 发票号+商品编码 会把这 2 行并成 1 行，可退数量就算错。"""
        self.do_import()
        conn = self.conn()
        try:
            lines = db.get_lines_by_invoice(conn, "489434")
            same_stock = [line for line in lines if line.stock_code == "85048"]
            self.assertEqual(len(same_stock), 2)
            self.assertEqual(sorted(line.seq for line in same_stock), [1, 2])
            self.assertEqual(sorted(line.returnable_qty for line in same_stock), [2, 6])
            keys = [line.line_key for line in lines]
            self.assertEqual(len(keys), len(set(keys)))
        finally:
            conn.close()


# ---------------------------------------------------------------- R-02 两表重叠


class TestR02Overlap(DbCase):
    def test_provenance_adds_up(self):
        self.assertEqual(verify.RAW_ROWS - verify.OVERLAP_ROWS, verify.BASELINE["rows"])
        self.assertEqual(verify.check_provenance(), [])

    def test_strict_import_rolls_back_un_deduped_overlap(self):
        """没去重的数据在 strict 模式下必须整体回滚，一行都不许进库。"""
        code, out = run_cli(
            "import", "--csv", OVERLAP, "--db", self.db_path, "--out", self.out,
            "--baseline", "strict",
        )
        self.assertEqual(code, main.EXIT_BASELINE, out)
        self.assertIn("已整体回滚", out)
        conn = self.conn()
        try:
            self.assertEqual(db.count_lines(conn), 0)
        finally:
            conn.close()

    def test_without_dedupe_same_invoice_counts_twice(self):
        """证明"不去重会怎样"：同一张发票在两张表里各留一行。"""
        code, out = self.do_import(OVERLAP)
        self.assertEqual(code, 0, out)
        conn = self.conn()
        try:
            lines = db.get_lines_by_invoice(conn, "536365")
            self.assertEqual(len(lines), 2)
            self.assertEqual({line.sheet for line in lines},
                             {"Year 2009-2010", "Year 2010-2011"})
        finally:
            conn.close()


# ---------------------------------------------------------------- R-03 取消只看前缀


class TestR03CancelPrefix(DbCase):
    def test_uppercase_prefix_decides_type(self):
        self.assertEqual(rules.recheck_flags(invoice="c536379", stock_code="85048"), (1, 0, 0))
        self.assertEqual(rules.recheck_flags(invoice="A563185", stock_code="12000"), (0, 1, 0))

    def test_negative_quantity_without_c_is_not_cancel(self):
        self.do_import()
        conn = self.conn()
        try:
            row = db.get_line(conn, "Year 2009-2010|489434|6")
            self.assertEqual(row.quantity, -5)
            self.assertEqual(row.is_cancel, 0)          # 负数量 ≠ 取消
            self.assertEqual(row.returnable_qty, 0)
        finally:
            conn.close()

    def test_cancel_line_cannot_be_returned_again(self):
        self.do_import()
        code, out = self.apply("--invoice", "C489434", "--seq", "1", "--qty", "1")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("RESULT=E_NON_RETURNABLE", out)
        self.assertIn("取消行", out)


# ---------------------------------------------------------------- R-04 可退数量


class TestR04Retirable(DbCase):
    def test_formula_and_zero_floor(self):
        self.assertEqual(rules.compute_retirable(10, 3, 2), 5)
        self.assertEqual(rules.compute_retirable(2, 5, 0), 0)

    def test_history_is_allocated_to_earliest_line(self):
        """取消 4 件 → 落在 seq1（6 件）上，seq2（2 件）不受影响。"""
        self.do_import()
        conn = self.conn()
        try:
            first = db.get_line(conn, "Year 2009-2010|489434|1")
            second = db.get_line(conn, "Year 2009-2010|489434|2")
            self.assertEqual((first.returnable_qty, first.history_returned), (6, 4))
            self.assertEqual((second.returnable_qty, second.history_returned), (2, 0))
            self.assertEqual(
                rules.compute_retirable(first.returnable_qty, first.history_returned, 0), 2
            )
        finally:
            conn.close()


# ---------------------------------------------------------------- R-05 非商品 / 零价


class TestR05NonProduct(DbCase):
    def test_non_product_codes(self):
        for code in ("POST", "D", "M", "BANK CHARGES", "DOT"):
            self.assertEqual(
                rules.recheck_flags(invoice="489434", stock_code=code)[2], 1, code
            )
        self.assertEqual(rules.recheck_flags(invoice="489434", stock_code="85123A")[2], 0)

    def test_postage_line_rejected(self):
        self.do_import()
        code, out = self.apply("--invoice", "489434", "--seq", "4", "--qty", "1")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("非商品", out)

    def test_zero_price_product_line_is_still_retirable(self):
        """2026-10 的决定：单价 0 的赠品/样品行仍可退，只显示提示，不清零额度。"""
        self.do_import()
        conn = self.conn()
        try:
            line = db.get_line(conn, "Year 2009-2010|489434|5")
            self.assertEqual(line.price, 0.0)
            self.assertEqual(line.is_non_product, 0)
            self.assertEqual(line.returnable_qty, 4)
        finally:
            conn.close()
        code, out = self.apply("--invoice", "489434", "--seq", "5", "--qty", "4")
        self.assertEqual(code, 0, out)


# ---------------------------------------------------------------- R-06 重复与幂等


class TestR06Duplicate(DbCase):
    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_second_submit_rejected_without_new_record(self):
        first_code, first_out = self.apply("--invoice", "489434", "--seq", "2", "--qty", "2")
        self.assertEqual(first_code, 0, first_out)
        conn = self.conn()
        before = db.count_requests(conn)
        conn.close()

        code, out = self.apply("--invoice", "489434", "--seq", "2", "--qty", "1")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("RESULT=E_DUPLICATE", out)
        self.assertIn("本次未新增记录", out)
        conn = self.conn()
        try:
            self.assertEqual(db.count_requests(conn), before)
        finally:
            conn.close()

    def test_same_request_id_replays_previous_result(self):
        args = ("--invoice", "489434", "--seq", "5", "--qty", "1", "--request-id", "fix-01")
        self.apply(*args)
        code, out = self.apply(*args)
        self.assertEqual(code, 0, out)
        self.assertIn("重复提交同一请求", out)
        conn = self.conn()
        try:
            self.assertEqual(db.count_requests(conn), 1)
        finally:
            conn.close()


# ---------------------------------------------------------------- R-07 数量校验


class TestR07Quantity(DbCase):
    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_zero_and_negative_rejected(self):
        for qty in ("0", "-1", "-99"):
            code, out = self.apply("--invoice", "489434", "--seq", "1", "--qty", qty)
            self.assertEqual(code, main.EXIT_REJECT, out)
            self.assertIn("RESULT=E_INVALID_QTY", out)

    def test_exceed_shows_retirable_and_breakdown(self):
        code, out = self.apply("--invoice", "489434", "--seq", "1", "--qty", "3")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("可退 2", out)
        self.assertIn("申请 3", out)
        self.assertIn("history_returned=4", out)


# ---------------------------------------------------------------- R-08 异常队列


class TestR08ManualQueue(DbCase):
    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_missing_customer_goes_pending_and_keeps_quota(self):
        code, out = self.apply("--invoice", "489434", "--seq", "7", "--qty", "1")
        self.assertEqual(code, main.EXIT_MANUAL, out)
        self.assertIn("RESULT=E_MANUAL", out)
        self.assertIn("PENDING_MANUAL", out)
        conn = self.conn()
        try:
            row = conn.execute(
                "SELECT status, anomaly_kind FROM return_request WHERE line_key = ?",
                ("Year 2009-2010|489434|7",),
            ).fetchone()
            self.assertEqual(row["status"], "PENDING_MANUAL")
            self.assertEqual(row["anomaly_kind"], "missing_cust")
            self.assertEqual(db.sum_active_qty(conn, "Year 2009-2010|489434|7"), 0)
        finally:
            conn.close()

    def test_request_date_before_invoice_rejected_as_anomaly(self):
        result = rules.check_not_in_future("2026-01-02 00:00:00", NOW)
        self.assertEqual(result.code, "E_ANOMALY")


# ---------------------------------------------------------------- R-09 状态机与释放


class TestR09StateMachine(DbCase):
    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_applied_then_auto_released_after_seven_days(self):
        code, out = self.apply("--invoice", "489434", "--seq", "3", "--qty", "1")
        self.assertEqual(code, 0, out)
        conn = self.conn()
        try:
            self.assertEqual(db.sum_active_qty(conn, "Year 2009-2010|489434|3"), 1)
        finally:
            conn.close()

        code, out = run_cli(
            "query", "--db", self.db_path, "--invoice", "489434", "--now", LATER, "--log"
        )
        self.assertEqual(code, 0, out)
        conn = self.conn()
        try:
            self.assertEqual(db.sum_active_qty(conn, "Year 2009-2010|489434|3"), 0)
            status = conn.execute("SELECT status FROM return_request").fetchone()["status"]
            self.assertEqual(status, "REJECTED")
            notes = [row["note"] for row in db.get_status_log(conn, "R-0001")]
            self.assertTrue(any("AUTO_RELEASED" in note for note in notes), notes)
        finally:
            conn.close()

    def test_every_transition_is_logged(self):
        self.apply("--invoice", "489434", "--seq", "3", "--qty", "1")
        conn = self.conn()
        try:
            logs = db.get_status_log(conn, "R-0001")
            self.assertEqual(len(logs), 1)
            self.assertIsNone(logs[0]["from_status"])
            self.assertEqual(logs[0]["to_status"], "APPLIED")
            self.assertEqual(logs[0]["operator"], "售后-未署名")
        finally:
            conn.close()


# ---------------------------------------------------------------- 元测试


class TestCoverageMatrix(unittest.TestCase):
    """防回归：删掉某条规则的测试类，这里必须红。"""

    def test_every_rule_has_a_test_class_with_tests(self):
        module = sys.modules[__name__]
        missing: list[str] = []
        for number in range(1, 10):
            prefix = f"TestR{number:02d}"
            hits = [
                name for name, obj in vars(module).items()
                if name.startswith(prefix) and isinstance(obj, type)
            ]
            if not hits:
                missing.append(f"{prefix}（无测试类）")
                continue
            methods = [
                m for m in dir(getattr(module, hits[0]))
                if m.startswith("test_")
            ]
            if not methods:
                missing.append(f"{prefix}（类里没有 test_ 方法）")
        self.assertEqual(missing, [], f"以下规则没有测试：{missing}")

    def test_fixtures_are_present(self):
        self.assertTrue(csv_loader.check_header(SAMPLE))
        self.assertTrue(csv_loader.check_header(OVERLAP))


if __name__ == "__main__":
    unittest.main()
