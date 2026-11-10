"""R-04 历史占用 FIFO 分摊的单测：纯内存，不碰数据库。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from csv_loader import LineRaw  # noqa: E402
from history import allocate  # noqa: E402

CUST = "13085"
STOCK = "85048"


def sale(key: str, qty: int, *, cust: str | None = CUST, stock: str = STOCK,
         date: str = "2024-01-01T00:00:00", returnable: int | None = None) -> LineRaw:
    return LineRaw(
        sheet="s", invoice="489434", stock_code=stock, description="d", quantity=qty,
        invoice_date=date, price=1.0, cust_id=cust, country="UK", seq=1, line_key=key,
        is_cancel=0, is_bad_debt=0, is_non_product=0,
        returnable_qty=qty if returnable is None else returnable,
    )


def cancel(key: str, qty: int, *, cust: str | None = CUST, stock: str = STOCK,
           date: str = "2024-02-01T00:00:00") -> LineRaw:
    return LineRaw(
        sheet="s", invoice="C489434", stock_code=stock, description="d", quantity=qty,
        invoice_date=date, price=1.0, cust_id=cust, country="UK", seq=1, line_key=key,
        is_cancel=1, is_bad_debt=0, is_non_product=0, returnable_qty=0,
    )


class TestFifoAllocation(unittest.TestCase):
    def test_partial_consumption_hits_earliest_line(self):
        updates, stats = allocate([sale("A", 6), sale("B", 2), cancel("C", -4)])
        self.assertEqual(dict(updates), {"A": 4})
        self.assertEqual(stats.assigned, 4)
        self.assertEqual(stats.over_returned, 0)
        self.assertEqual(stats.unmatched_cancel, 0)

    def test_cancel_spanning_two_lines(self):
        updates, stats = allocate([sale("A", 3), sale("B", 5), cancel("C", -6)])
        self.assertEqual(dict(updates), {"A": 3, "B": 3})
        self.assertEqual(stats.assigned, 6)

    def test_full_consumption(self):
        updates, stats = allocate([sale("A", 4), cancel("C", -4)])
        self.assertEqual(dict(updates), {"A": 4})
        self.assertEqual(stats.over_returned, 0)

    def test_over_returned_keeps_residual(self):
        updates, stats = allocate([sale("A", 3), cancel("C", -10)])
        self.assertEqual(dict(updates), {"A": 3})
        self.assertEqual(stats.over_returned, 1)
        self.assertEqual(stats.assigned, 3)

    def test_cancel_without_any_sale_is_unmatched(self):
        updates, stats = allocate([cancel("C", -2)])
        self.assertEqual(updates, [])
        self.assertEqual(stats.unmatched_cancel, 1)

    def test_guest_rows_are_skipped(self):
        updates, stats = allocate(
            [sale("A", 6, cust=None), cancel("C", -2, cust=None)]
        )
        self.assertEqual(updates, [])
        self.assertEqual(stats.guest_skipped, 1)
        self.assertEqual(stats.assigned, 0)

    def test_groups_do_not_mix_customers(self):
        updates, stats = allocate(
            [
                sale("A", 6, cust=CUST),
                sale("B", 6, cust="17850"),
                cancel("C", -2, cust="17850"),
            ]
        )
        self.assertEqual(dict(updates), {"B": 2})
        self.assertEqual(stats.groups, 2)

    def test_same_customer_different_stock_do_not_mix(self):
        updates, stats = allocate(
            [sale("A", 6, stock="85048"), sale("B", 6, stock="85123A"), cancel("C", -1, stock="85123A")]
        )
        self.assertEqual(dict(updates), {"B": 1})

    def test_cancel_with_positive_qty_is_not_a_return(self):
        updates, stats = allocate([sale("A", 5), cancel("C", 1)])
        self.assertEqual(updates, [])
        self.assertEqual(stats.cancel_pos_skipped, 1)

    def test_non_returnable_sale_enters_no_pool(self):
        updates, stats = allocate([sale("A", 5, returnable=0), cancel("C", -1)])
        self.assertEqual(updates, [])
        self.assertEqual(stats.unmatched_cancel, 1)


if __name__ == "__main__":
    unittest.main()
