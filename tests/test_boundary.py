"""边界用例单独成组：=0、负数、超量、重复提交两次、缺客户号、两表重叠、大小写/空格。

每组一个 `TestBoundary*` 类，和 test_rule_matrix.py 的"一条规则一个类"互补：
矩阵回答"每条规则有没有被测到"，这里回答"边界有没有被单独测到"。
"""

from __future__ import annotations

import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_HERE))

from _support import LATER, NOW, OVERLAP, SAMPLE, DbCase, run_cli

import csv_loader
import db
import main
import rules
import verify


class TestBoundaryZero(DbCase):
    """= 0：申请数量 0、可退已为 0、单价 0。"""

    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_apply_qty_zero_rejected(self):
        code, out = self.apply("--invoice", "489434", "--seq", "1", "--qty", "0")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("RESULT=E_INVALID_QTY", out)
        self.assertIn("必须为正整数", out)

    def test_retirable_already_zero_then_apply_rejected(self):
        # 先把这一行退满：可退 2 → 0
        code, out = self.apply("--invoice", "489434", "--seq", "2", "--qty", "2")
        self.assertEqual(code, 0, out)
        # 再申请：先命中"同一明细已有生效申请"，不会走到超量判断
        code, out = self.apply("--invoice", "489434", "--seq", "2", "--qty", "1")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("RESULT=E_DUPLICATE", out)
        # 可退为 0 时纯校验的报错文案（E_EXCEED 会带出"可退 0"）
        result = rules.check_qty(1, 0)
        self.assertEqual(result.code, "E_EXCEED")
        self.assertIn("可退 0", result.message)

    def test_zero_returnable_line_is_not_retirable(self):
        result = rules.check_retirable_line(
            returnable_qty=0, is_cancel=0, is_bad_debt=0, is_non_product=0
        )
        self.assertEqual(result.code, "E_NON_RETURNABLE")
        self.assertEqual(rules.compute_retirable(0, 0, 0), 0)


class TestBoundaryNegative(DbCase):
    """负数：申请数量为负、负数量但非 C 开头的行。"""

    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_apply_negative_qty_rejected(self):
        for qty in ("-1", "-100"):
            code, out = self.apply("--invoice", "489434", "--seq", "1", "--qty", qty)
            self.assertEqual(code, main.EXIT_REJECT, out)
            self.assertIn("RESULT=E_INVALID_QTY", out)

    def test_negative_quantity_line_not_retirable(self):
        code, out = self.apply("--invoice", "489434", "--seq", "6", "--qty", "1")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("RESULT=E_NON_RETURNABLE", out)
        self.assertIn("负数量", out)


class TestBoundaryExceed(DbCase):
    """超量：可退 2 申请 3、申请 99999。"""

    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_slightly_over(self):
        code, out = self.apply("--invoice", "489434", "--seq", "1", "--qty", "3")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("RESULT=E_EXCEED", out)
        self.assertIn("可退 2", out)

    def test_far_over_does_not_create_record(self):
        code, out = self.apply("--invoice", "489434", "--seq", "1", "--qty", "99999")
        self.assertEqual(code, main.EXIT_REJECT, out)
        conn = self.conn()
        try:
            self.assertEqual(db.count_requests(conn), 0)
        finally:
            conn.close()


class TestBoundaryDuplicateTwice(DbCase):
    """重复提交两次：同请求号一次落库、不同请求号被拦。"""

    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_same_request_id_twice_only_one_record(self):
        args = ("--invoice", "489434", "--seq", "2", "--qty", "1", "--request-id", "dup-01")
        self.apply(*args)
        code, out = self.apply(*args)
        self.assertEqual(code, 0, out)
        self.assertIn("重复提交同一请求", out)
        conn = self.conn()
        try:
            self.assertEqual(db.count_requests(conn), 1)
        finally:
            conn.close()

    def test_different_request_id_same_line_is_duplicate(self):
        self.apply("--invoice", "489434", "--seq", "2", "--qty", "1", "--request-id", "a-01")
        code, out = self.apply(
            "--invoice", "489434", "--seq", "2", "--qty", "1", "--request-id", "b-02"
        )
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("RESULT=E_DUPLICATE", out)

    def test_rejected_attempt_does_not_block_later_valid_apply(self):
        code, _ = self.apply("--invoice", "489434", "--seq", "2", "--qty", "99")
        self.assertEqual(code, main.EXIT_REJECT)
        code, out = self.apply("--invoice", "489434", "--seq", "2", "--qty", "2")
        self.assertEqual(code, 0, out)


class TestBoundaryMissingCustomer(DbCase):
    """缺客户号：进异常队列、不占额度、人工放行后才占。"""

    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_goes_to_pending_manual(self):
        code, out = self.apply("--invoice", "489434", "--seq", "7", "--qty", "1")
        self.assertEqual(code, main.EXIT_MANUAL, out)
        self.assertIn("PENDING_MANUAL", out)
        conn = self.conn()
        try:
            self.assertEqual(db.sum_active_qty(conn, "Year 2009-2010|489434|7"), 0)
        finally:
            conn.close()

    def test_manual_approval_then_quota_consumed(self):
        self.apply("--invoice", "489434", "--seq", "7", "--qty", "1")
        conn = self.conn()
        try:
            db.update_request_status(
                conn, req_no="R-0001", to_status="ACCEPTED",
                operator="客服主管-李", note="客户电话核实后放行", now_iso=NOW,
            )
            conn.commit()
            self.assertEqual(db.sum_active_qty(conn, "Year 2009-2010|489434|7"), 1)
            self.assertEqual(
                rules.compute_retirable(3, 0, 1), 2
            )
        finally:
            conn.close()


class TestBoundaryTwoSheetOverlap(DbCase):
    """两表重叠：同一张发票出现在两个 sheet 里。"""

    def test_strict_mode_blocks_un_deduped_data(self):
        code, out = run_cli(
            "import", "--csv", OVERLAP, "--db", self.db_path, "--out", self.out,
            "--baseline", "strict",
        )
        self.assertEqual(code, main.EXIT_BASELINE, out)
        conn = self.conn()
        try:
            self.assertEqual(db.count_lines(conn), 0)
        finally:
            conn.close()

    def test_report_mode_shows_double_counting_hazard(self):
        code, out = self.do_import(OVERLAP)
        self.assertEqual(code, 0, out)
        conn = self.conn()
        try:
            lines = db.get_lines_by_invoice(conn, "536365")
            self.assertEqual(len(lines), 2)          # 不去重 → 同一发票算两次
        finally:
            conn.close()

    def test_provenance_is_self_consistent(self):
        self.assertEqual(verify.RAW_ROWS - verify.OVERLAP_ROWS, verify.BASELINE["rows"])
        self.assertEqual(verify.check_provenance(), [])
        self.assertEqual(verify.OVERLAP_INVOICES, 1088)


class TestBoundaryStockCodeCaseAndSpace(DbCase):
    """StockCode / Invoice 的大小写与空格。"""

    def test_recheck_flags_is_case_and_space_insensitive(self):
        self.assertEqual(
            rules.recheck_flags(invoice=" 489434 ", stock_code=" 85123a "),
            rules.recheck_flags(invoice="489434", stock_code="85123A"),
        )
        self.assertEqual(rules.recheck_flags(invoice=" 489434 ", stock_code=" 85123a "), (0, 0, 0))

    def test_lowercase_non_product_still_detected(self):
        self.assertEqual(rules.recheck_flags(invoice="489434", stock_code="post")[2], 1)
        self.assertEqual(rules.recheck_flags(invoice="489434", stock_code="bank charges")[2], 1)

    def test_invoice_lowercase_c_still_cancel(self):
        self.assertEqual(rules.recheck_flags(invoice="c489434", stock_code="85048")[0], 1)

    def test_real_slice_has_no_un_normalized_stock_code(self):
        """数据断言：上游已把 StockCode 归一，本系统不再重复处理。"""
        if not Path(SAMPLE).exists():
            self.skipTest(f"缺少真实切片 {SAMPLE}")
        dirty = [
            row.stock_code for row in csv_loader.iter_lines(SAMPLE)
            if row.stock_code != row.stock_code.strip().upper()
        ]
        self.assertEqual(dirty, [])


class TestBoundaryCrossDay(DbCase):
    """跨年/跨天的时钟边界（R-08 的"申请早于开票"）。"""

    def test_invoice_after_request_is_anomaly(self):
        result = rules.check_not_in_future("2026-01-02 00:00:00", NOW)
        self.assertEqual(result.code, "E_ANOMALY")

    def test_same_day_space_vs_t_separator(self):
        self.assertTrue(rules.check_not_in_future("2026-01-01 00:00:00", NOW).ok)
        self.assertEqual(
            rules.check_not_in_future("2026-01-01 23:59:59", "2026-01-01T00:00:00").code,
            "E_ANOMALY",
        )

    def test_expired_release_at_day_eight(self):
        self.do_import()
        code, out = self.apply("--invoice", "489434", "--seq", "2", "--qty", "1")
        self.assertEqual(code, 0, out)
        code, out = run_cli(
            "query", "--db", self.db_path, "--invoice", "489434", "--now", LATER
        )
        self.assertEqual(code, 0, out)
        self.assertIn("已自动释放超期申请", out)


if __name__ == "__main__":
    unittest.main()
