"""R-03~R-08 纯函数单测：不碰数据库、不读文件，整套秒级跑完。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

import rules  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_invoice_keeps_c_prefix(self):
        self.assertEqual(rules.normalize_invoice("c536379"), "C536379")
        self.assertEqual(rules.normalize_invoice(" 536365 "), "536365")
        self.assertEqual(rules.normalize_invoice(None), "")

    def test_customer_id_float_like(self):
        self.assertEqual(rules.normalize_customer_id(13085.0), "13085")
        self.assertEqual(rules.normalize_customer_id(" 13085 "), "13085")
        self.assertIsNone(rules.normalize_customer_id(""))
        self.assertIsNone(rules.normalize_customer_id(None))

    def test_timestamp_space_and_t_are_equivalent(self):
        self.assertEqual(
            rules.normalize_ts("2009-12-01 07:45:00"), rules.normalize_ts("2009-12-01T07:45:00")
        )


class TestRecheckFlags(unittest.TestCase):
    def test_product_code_matches_upstream_flag(self):
        self.assertEqual(rules.recheck_flags(invoice="489434", stock_code="85048"), (0, 0, 0))
        self.assertEqual(rules.recheck_flags(invoice="489434", stock_code="79323P"), (0, 0, 0))

    def test_non_product_codes(self):
        for code in ("POST", "D", "M", "BANK CHARGES", "DOT", "AMAZONFEE"):
            self.assertEqual(rules.recheck_flags(invoice="489434", stock_code=code)[2], 1, code)

    def test_cancel_and_bad_debt_prefix(self):
        self.assertEqual(rules.recheck_flags(invoice="c536379", stock_code="85048"), (1, 0, 0))
        self.assertEqual(rules.recheck_flags(invoice="A563185", stock_code="12000"), (0, 1, 0))


class TestComputeRetirable(unittest.TestCase):
    def test_subtract_both_occupancies(self):
        self.assertEqual(rules.compute_retirable(10, 3, 2), 5)

    def test_never_negative(self):
        self.assertEqual(rules.compute_retirable(2, 5, 0), 0)


class TestChecks(unittest.TestCase):
    def test_found(self):
        self.assertTrue(rules.check_found(True, "k").ok)
        self.assertEqual(rules.check_found(False, "k").code, "E_NOT_FOUND")

    def test_non_retirable_reasons(self):
        cases = {
            "bad_debt": dict(returnable_qty=0, is_cancel=0, is_bad_debt=1, is_non_product=0),
            "non_product": dict(returnable_qty=0, is_cancel=0, is_bad_debt=0, is_non_product=1),
            "cancel": dict(returnable_qty=0, is_cancel=1, is_bad_debt=0, is_non_product=0),
            "no_qty": dict(returnable_qty=0, is_cancel=0, is_bad_debt=0, is_non_product=0),
        }
        for reason, kwargs in cases.items():
            result = rules.check_retirable_line(**kwargs)
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "E_NON_RETURNABLE")
            self.assertEqual(result.data["reason"], reason)
        self.assertTrue(
            rules.check_retirable_line(
                returnable_qty=6, is_cancel=0, is_bad_debt=0, is_non_product=0
            ).ok
        )

    def test_qty_bounds(self):
        self.assertEqual(rules.check_qty(0, 6).code, "E_INVALID_QTY")
        self.assertEqual(rules.check_qty(-1, 6).code, "E_INVALID_QTY")
        exceeded = rules.check_qty(7, 6)
        self.assertEqual(exceeded.code, "E_EXCEED")
        self.assertIn("可退 6", exceeded.message)
        self.assertIn("申请 7", exceeded.message)
        self.assertTrue(rules.check_qty(6, 6).ok)

    def test_duplicate(self):
        self.assertTrue(rules.check_duplicate(None).ok)
        result = rules.check_duplicate("R-0007")
        self.assertEqual(result.code, "E_DUPLICATE")
        self.assertIn("R-0007", result.message)

    def test_future_date_uses_normalized_timestamp(self):
        self.assertTrue(
            rules.check_not_in_future("2009-12-01 07:45:00", "2026-01-01T00:00:00").ok
        )
        same_day = rules.check_not_in_future("2026-01-01 23:00:00", "2026-01-01T00:00:00")
        self.assertEqual(same_day.code, "E_ANOMALY")

    def test_anomaly_missing_customer(self):
        result = rules.check_anomaly(
            cust_id=None, found=True, invoice_date_iso="2009-12-01 07:45:00", now_iso="2026-01-01T00:00:00"
        )
        self.assertEqual(result.code, "E_MANUAL")
        self.assertEqual(result.data["reason"], "missing_cust")


class TestDecideOrder(unittest.TestCase):
    def _base(self, **overrides):
        payload = dict(
            line_key="Year 2009-2010|489434|1",
            found=True,
            returnable_qty=6,
            is_cancel=0,
            is_bad_debt=0,
            is_non_product=0,
            cust_id="13085",
            invoice_date_iso="2009-12-01 07:45:00",
            history_returned=0,
            active_qty=0,
            active_request_no=None,
            now_iso="2026-01-01T00:00:00",
            qty_requested=2,
        )
        payload.update(overrides)
        return rules.decide(**payload)

    def test_happy_path(self):
        result = self._base()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["retirable"], 6)
        self.assertEqual(result.data["remaining"], 4)

    def test_not_found_beats_everything(self):
        result = self._base(found=False, qty_requested=0, is_non_product=1, active_request_no="R-1")
        self.assertEqual(result.code, "E_NOT_FOUND")

    def test_non_retirable_beats_duplicate(self):
        result = self._base(is_non_product=1, active_request_no="R-1", qty_requested=99)
        self.assertEqual(result.code, "E_NON_RETURNABLE")

    def test_duplicate_beats_invalid_qty(self):
        result = self._base(active_request_no="R-1", qty_requested=0)
        self.assertEqual(result.code, "E_DUPLICATE")

    def test_invalid_qty_beats_exceed(self):
        result = self._base(qty_requested=-3)
        self.assertEqual(result.code, "E_INVALID_QTY")

    def test_exceed_beats_anomaly(self):
        result = self._base(cust_id=None, qty_requested=99)
        self.assertEqual(result.code, "E_EXCEED")

    def test_anomaly_after_qty(self):
        result = self._base(cust_id=None, qty_requested=2)
        self.assertEqual(result.code, "E_MANUAL")

    def test_exceed_message_carries_breakdown(self):
        result = self._base(history_returned=3, active_qty=1, qty_requested=3)
        self.assertEqual(result.code, "E_EXCEED")
        self.assertEqual(result.data["retirable"], 2)
        self.assertEqual(result.data["history_returned"], 3)


class TestMessagesNeverSilent(unittest.TestCase):
    def test_every_reject_has_code_and_chinese_message(self):
        samples = [
            rules.check_found(False, "k"),
            rules.check_retirable_line(
                returnable_qty=0, is_cancel=1, is_bad_debt=0, is_non_product=0
            ),
            rules.check_duplicate("R-0001"),
            rules.check_qty(0, 1),
            rules.check_qty(2, 1),
            rules.check_not_in_future("2026-02-01 00:00:00", "2026-01-01T00:00:00"),
            rules.check_anomaly(
                cust_id=None, found=True, invoice_date_iso="2009-12-01 00:00:00",
                now_iso="2026-01-01T00:00:00",
            ),
        ]
        for result in samples:
            self.assertFalse(result.ok)
            self.assertTrue(result.code.startswith("E_"), result)
            self.assertTrue(result.message.strip(), result.code)
            self.assertRegex(result.message, r"[\u4e00-\u9fff]")


if __name__ == "__main__":
    unittest.main()
