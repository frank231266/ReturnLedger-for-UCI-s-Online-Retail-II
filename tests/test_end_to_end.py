"""端到端：import → apply → query 三条路径，全部走 CLI，断言退出码与库内状态。

用 tests/fixtures/tiny.csv（10 行手写样本，专测边界）与 data/build/sample_2000.csv
（真实数据切片）两个数据源。
"""

from __future__ import annotations

import gc
import io
import shutil
import sys
import time
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

import db  # noqa: E402
import main  # noqa: E402

TINY = str(_ROOT / "tests" / "fixtures" / "tiny.csv")
SAMPLE = str(_ROOT / "data" / "build" / "sample_2000.csv")

INVOICE = "489434"


def tearDownModule() -> None:
    tmp = _ROOT / "tests" / ".tmp"
    for _ in range(5):
        gc.collect()
        shutil.rmtree(tmp, ignore_errors=True)
        if not tmp.exists():
            return
        time.sleep(0.05)


class CliCase(unittest.TestCase):
    NOW = "2026-01-01T00:00:00"

    def setUp(self) -> None:
        # 不用 tempfile.TemporaryDirectory：受限沙箱下它的 chmod 清理会报 PermissionError
        workdir = _ROOT / "tests" / ".tmp"
        workdir.mkdir(parents=True, exist_ok=True)
        self.dir = workdir / uuid.uuid4().hex[:8]
        self.dir.mkdir()
        self.db_path = str(self.dir / "rs.db")
        self.out = str(self.dir)

    def tearDown(self) -> None:
        # Windows 上 sqlite 关闭后句柄释放有延迟，删除目录要重试
        for _ in range(5):
            gc.collect()
            shutil.rmtree(self.dir, ignore_errors=True)
            if not self.dir.exists():
                return
            time.sleep(0.05)

    def run_cli(self, *args: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main.main(list(args))
        return code, buffer.getvalue()

    def do_import(self, csv_path: str = TINY) -> tuple[int, str]:
        return self.run_cli(
            "import", "--csv", csv_path, "--db", self.db_path,
            "--out", self.out, "--baseline", "report",
        )

    def apply(self, *extra: str) -> tuple[int, str]:
        return self.run_cli("apply", "--db", self.db_path, "--now", self.NOW, *extra)

    def query(self, invoice: str = INVOICE, *extra: str) -> tuple[int, str]:
        # 固定时钟：否则运行时的--now 与真实时间差超过 7 天，R-09 会把申请释放掉
        return self.run_cli(
            "query", "--db", self.db_path, "--invoice", invoice, "--now", self.NOW, *extra
        )

    def conn(self):
        return db.connect(self.db_path)


class TestImport(CliCase):
    def test_import_creates_lines_and_history(self):
        code, out = self.do_import()
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT=OK", out)
        conn = self.conn()
        try:
            self.assertEqual(db.count_lines(conn), 10)
            lines = {line.line_key: line for line in db.get_lines_by_invoice(conn, INVOICE)}
            self.assertEqual(len(lines), 7)
            # 取消 4 件全部落在最早那条 6 件的行上
            self.assertEqual(lines["Year 2009-2010|489434|1"].history_returned, 4)
            self.assertEqual(lines["Year 2009-2010|489434|2"].history_returned, 0)
            self.assertEqual(lines["Year 2009-2010|489434|3"].history_returned, 1)
            self.assertEqual(db.count_history_hit(conn), 2)
        finally:
            conn.close()

    def test_import_writes_invariant_file(self):
        self.do_import()
        report = Path(self.out) / "import_invariants.txt"
        self.assertTrue(report.exists())
        text = report.read_text(encoding="utf-8")
        self.assertIn("flag_mismatch", text)
        self.assertIn("历史占用", text)

    def test_second_import_requires_rebuild(self):
        self.do_import()
        code, out = self.do_import()
        self.assertEqual(code, main.EXIT_IO)
        self.assertIn("--rebuild", out)

    def test_rebuild_keeps_requests(self):
        self.do_import()
        self.apply("--invoice", INVOICE, "--seq", "2", "--qty", "2")
        conn = self.conn()
        before = db.count_requests(conn)
        conn.close()
        code, out = self.run_cli(
            "import", "--csv", TINY, "--db", self.db_path, "--out", self.out,
            "--baseline", "report", "--rebuild",
        )
        self.assertEqual(code, 0, out)
        conn = self.conn()
        try:
            self.assertEqual(db.count_requests(conn), before)
        finally:
            conn.close()


class TestApply(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_happy_path(self):
        code, out = self.apply("--invoice", INVOICE, "--seq", "2", "--qty", "2", "--reason", "尺寸不符")
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT=OK", out)
        self.assertIn("R-0001", out)

    def test_exceed_reports_retirable(self):
        code, out = self.apply("--invoice", INVOICE, "--seq", "1", "--qty", "3")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("RESULT=E_EXCEED", out)
        self.assertIn("可退 2", out)

    def test_duplicate_does_not_increase_records(self):
        self.apply("--invoice", INVOICE, "--seq", "2", "--qty", "2")
        conn = self.conn()
        before = db.count_requests(conn)
        conn.close()
        code, out = self.apply("--invoice", INVOICE, "--seq", "2", "--qty", "1")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("RESULT=E_DUPLICATE", out)
        conn = self.conn()
        try:
            self.assertEqual(db.count_requests(conn), before)
        finally:
            conn.close()

    def test_same_request_id_is_idempotent(self):
        args = ("--invoice", INVOICE, "--seq", "5", "--qty", "1", "--request-id", "fixed-001")
        first_code, first_out = self.apply(*args)
        second_code, second_out = self.apply(*args)
        self.assertEqual(first_code, 0, first_out)
        self.assertEqual(second_code, 0, second_out)
        self.assertIn("重复提交同一请求", second_out)
        conn = self.conn()
        try:
            self.assertEqual(db.count_requests(conn), 1)
        finally:
            conn.close()

    def test_invalid_qty(self):
        for qty in ("0", "-1"):
            code, out = self.apply("--invoice", INVOICE, "--seq", "1", "--qty", qty)
            self.assertEqual(code, main.EXIT_REJECT, out)
            self.assertIn("RESULT=E_INVALID_QTY", out)

    def test_non_retirable_kinds(self):
        cases = {
            "4": "非商品行",       # POST
            "6": "可退数量为",      # 负数量但非 C 开头
        }
        for seq, keyword in cases.items():
            code, out = self.apply("--invoice", INVOICE, "--seq", seq, "--qty", "1")
            self.assertEqual(code, main.EXIT_REJECT, out)
            self.assertIn("RESULT=E_NON_RETURNABLE", out)
            self.assertIn(keyword, out)

    def test_bad_debt_line_has_its_own_message(self):
        code, out = self.apply("--invoice", "A489434", "--seq", "1", "--qty", "1")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("坏账调整", out)

    def test_missing_customer_goes_to_manual_queue(self):
        code, out = self.apply("--invoice", INVOICE, "--seq", "7", "--qty", "1")
        self.assertEqual(code, main.EXIT_MANUAL, out)
        self.assertIn("RESULT=E_MANUAL", out)
        self.assertIn("PENDING_MANUAL", out)
        conn = self.conn()
        try:
            self.assertEqual(db.count_requests(conn), 1)
            row = conn.execute("SELECT status, anomaly_kind FROM return_request").fetchone()
            self.assertEqual(row["status"], "PENDING_MANUAL")
            self.assertEqual(row["anomaly_kind"], "missing_cust")
            # 异常单不占额度
            self.assertEqual(db.sum_active_qty(conn, "Year 2009-2010|489434|7"), 0)
        finally:
            conn.close()

    def test_unknown_line(self):
        code, out = self.apply("--invoice", "999999", "--seq", "1", "--qty", "1")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("RESULT=E_NOT_FOUND", out)

    def test_zero_price_line_is_still_returnable(self):
        code, out = self.apply("--invoice", INVOICE, "--seq", "5", "--qty", "4")
        self.assertEqual(code, 0, out)

    def test_expired_applied_releases_quota(self):
        code, out = self.apply("--invoice", INVOICE, "--seq", "3", "--qty", "1")
        self.assertEqual(code, 0, out)
        conn = self.conn()
        try:
            self.assertEqual(db.sum_active_qty(conn, "Year 2009-2010|489434|3"), 1)
        finally:
            conn.close()

        code, out = self.run_cli(
            "query", "--db", self.db_path, "--invoice", INVOICE, "--now", "2026-01-09T00:00:00"
        )
        self.assertEqual(code, 0, out)
        conn = self.conn()
        try:
            self.assertEqual(db.sum_active_qty(conn, "Year 2009-2010|489434|3"), 0)
            row = conn.execute("SELECT status FROM return_request").fetchone()
            self.assertEqual(row["status"], "REJECTED")
            log = conn.execute(
                "SELECT note FROM status_log WHERE to_status = 'REJECTED'"
            ).fetchone()
            self.assertIn("AUTO_RELEASED", log["note"])
        finally:
            conn.close()


class TestQuery(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_query_lists_all_lines_with_retirable(self):
        code, out = self.query()
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT=OK", out)
        self.assertIn("SAME STOCK FIRST LINE", out)
        self.assertIn("不可退(非商品)", out)          # POST 行
        self.assertIn("不可退(无可退数量)", out)      # 负数量但非 C 开头
        self.assertIn("可申请(缺客户号→人工)", out)   # 散客行
        self.assertIn("本发票可退合计 15 件", out)     # 2+2+4+0+4+0+3

    def test_query_shows_active_request(self):
        self.apply("--invoice", INVOICE, "--seq", "2", "--qty", "2")
        code, out = self.query()
        self.assertEqual(code, 0, out)
        self.assertIn("APPLIED(R-0001", out)

    def test_query_with_log(self):
        self.apply("--invoice", INVOICE, "--seq", "2", "--qty", "2")
        code, out = self.query(INVOICE, "--log")
        self.assertEqual(code, 0, out)
        self.assertIn("[R-0001]", out)
        self.assertIn("首次提交", out)

    def test_query_unknown_invoice(self):
        code, out = self.query("000000")
        self.assertEqual(code, main.EXIT_REJECT, out)
        self.assertIn("RESULT=E_NOT_FOUND", out)


class TestRealSlice(CliCase):
    """真实数据切片冒烟：不是手写样本，跑一次确认全链路通。"""

    def test_sample_2000_imports_and_is_queryable(self):
        if not Path(SAMPLE).exists():
            self.skipTest(f"缺少真实切片 {SAMPLE}（先跑 python src/prepare.py）")
        code, out = self.do_import(SAMPLE)
        self.assertEqual(code, 0, out)
        conn = self.conn()
        try:
            self.assertEqual(db.count_lines(conn), 2000)
            # 切片只有前 2000 行，取消行可能配不到原单，这里不断言命中数
            self.assertGreaterEqual(db.count_history_hit(conn), 0)
            report = Path(self.out) / "import_invariants.txt"
            self.assertTrue(report.exists())
            self.assertIn("历史占用", report.read_text(encoding="utf-8"))
            first = conn.execute(
                "SELECT invoice FROM order_line WHERE is_cancel = 0 LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        code, out = self.query(first["invoice"])
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT=OK", out)


class TestRejectStats(CliCase):
    """各校验拒绝计数：每次被拒/转人工都按错误码累加，并落成 reject_stat.txt。"""

    def setUp(self) -> None:
        super().setUp()
        self.do_import()

    def test_counters_accumulate_per_code(self):
        self.apply("--invoice", INVOICE, "--seq", "1", "--qty", "3")       # E_EXCEED
        self.apply("--invoice", INVOICE, "--seq", "1", "--qty", "0")       # E_INVALID_QTY
        self.apply("--invoice", INVOICE, "--seq", "4", "--qty", "1")       # E_NON_RETURNABLE
        self.apply("--invoice", "999999", "--seq", "1", "--qty", "1")      # E_NOT_FOUND
        code, _ = self.apply("--invoice", INVOICE, "--seq", "7", "--qty", "1")   # E_MANUAL
        self.assertEqual(code, main.EXIT_MANUAL)

        conn = self.conn()
        try:
            stats = db.get_reject_stats(conn)
        finally:
            conn.close()
        self.assertEqual(
            stats,
            {
                "E_EXCEED": 1,
                "E_INVALID_QTY": 1,
                "E_NON_RETURNABLE": 1,
                "E_NOT_FOUND": 1,
                "E_MANUAL": 1,
            },
        )
        report = Path(self.out) / "reject_stat.txt"
        self.assertTrue(report.exists())
        text = report.read_text(encoding="utf-8")
        self.assertIn("E_EXCEED", text)
        self.assertIn("合计", text)

    def test_successful_apply_is_not_counted_as_reject(self):
        self.apply("--invoice", INVOICE, "--seq", "5", "--qty", "1")
        conn = self.conn()
        try:
            self.assertEqual(db.get_reject_stats(conn), {})
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
