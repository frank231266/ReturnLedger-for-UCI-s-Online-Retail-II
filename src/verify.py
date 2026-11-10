"""导入不变量：把"上游说的"变成"每次导入都复算、可失败"。

为什么需要这一层
--------------
本系统不自己清洗数据，直接读 order_line.csv。那么"CSV 对不对"就成了
一个必须每次回答的问题。老师的追问一定是这句：
    "你怎么知道这份 CSV 没算错？"
答案不是"我信 prepare.py"，而是"每次导入都复算 13 个不变量，
任何一个与冻结基线不符就整体回滚、退出码 4"。

复用方式
--------
Accumulator 与 db.insert_lines 共用同一个生成器，149 MB 只扫一遍。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from csv_loader import LineRaw
from rules import recheck_flags

_DIGITS_RE = re.compile(r"^[0-9]+$")


@dataclass(frozen=True)
class Invariants:
    """13 个可复算的数字。含义与报告第三节一一对应。"""

    rows: int = 0
    unique_keys: int = 0
    cancel_rows: int = 0
    cancel_pos_qty: int = 0
    bad_debt: int = 0
    non_product: int = 0
    missing_cust: int = 0
    neg_qty: int = 0
    retirable_rows: int = 0
    retirable_sum: int = 0
    flag_mismatch: int = 0
    qty_non_integer: int = 0
    cust_bad_format: int = 0


# 全量口径基线：v1.2 原始 xlsx 经 prepare.py 去重清洗后的结果（已实测复核）
# 改上游清洗规则时必须同步改这里，并在 过程记录.md 写一句"为什么变"
BASELINE: dict[str, int] = {
    "rows": 1_044_848,
    "unique_keys": 1_044_848,
    "cancel_rows": 19_165,
    "cancel_pos_qty": 1,
    "bad_debt": 6,
    "non_product": 5_992,
    "missing_cust": 235_287,
    "neg_qty": 22_557,
    "retirable_rows": 1_017_521,
    "retirable_sum": 11_465_120,
    "flag_mismatch": 0,
    "qty_non_integer": 0,
    "cust_bad_format": 0,
}

# 切片/测试数据无法与全量基线比较，这些项在切片时自动跳过
SAMPLE_IGNORED: tuple[str, ...] = (
    "rows",
    "unique_keys",
    "cancel_rows",
    "cancel_pos_qty",
    "bad_debt",
    "non_product",
    "missing_cust",
    "neg_qty",
    "retirable_rows",
    "retirable_sum",
)

# 去重口径：这些数字来自上游 prepare.py 的实测（1,067,371 − 22,523 = 1,044,848）
# 本系统不重做去重，但每次导入都要自检这三个数与基线行数是否自洽
RAW_ROWS = 1_067_371
OVERLAP_INVOICES = 1_088
OVERLAP_ROWS = 22_523


def provenance() -> dict[str, int]:
    return {
        "去重前行数": RAW_ROWS,
        "重叠发票数": OVERLAP_INVOICES,
        "去重丢弃行数": OVERLAP_ROWS,
        "去重后行数": BASELINE["rows"],
    }


def check_provenance() -> list[str]:
    """去重口径自洽性：去重前 − 丢弃 = 去重后。不自洽说明基线或常量被改坏了。"""
    if RAW_ROWS - OVERLAP_ROWS != BASELINE["rows"]:
        return [
            f"provenance: {RAW_ROWS} − {OVERLAP_ROWS} ≠ {BASELINE['rows']}，"
            "去重口径与基线不一致"
        ]
    return []


class Accumulator:
    """单遍累加器。与 db.insert_lines 串联使用，不重复读文件。"""

    __slots__ = ("_keys", "_counts")

    def __init__(self) -> None:
        self._keys: set[str] = set()
        self._counts = {name: 0 for name in Invariants.__dataclass_fields__}

    def add(self, row: LineRaw) -> None:
        c = self._counts
        c["rows"] += 1
        self._keys.add(row.line_key)

        if row.is_cancel:
            c["cancel_rows"] += 1
            if row.quantity >= 0:
                c["cancel_pos_qty"] += 1
        if row.is_bad_debt:
            c["bad_debt"] += 1
        if row.is_non_product:
            c["non_product"] += 1
        if row.cust_id is None:
            c["missing_cust"] += 1
        elif not _DIGITS_RE.match(row.cust_id):
            c["cust_bad_format"] += 1
        if row.quantity < 0:
            c["neg_qty"] += 1
        if row.returnable_qty > 0:
            c["retirable_rows"] += 1
            c["retirable_sum"] += row.returnable_qty

        expected = recheck_flags(invoice=row.invoice, stock_code=row.stock_code)
        actual = (row.is_cancel, row.is_bad_debt, row.is_non_product)
        if expected != actual:
            c["flag_mismatch"] += 1

    def result(self) -> Invariants:
        data = dict(self._counts)
        data["unique_keys"] = len(self._keys)
        return Invariants(**data)


def scan(rows: Iterable[LineRaw]) -> Invariants:
    acc = Accumulator()
    for row in rows:
        acc.add(row)
    return acc.result()


def compare(
    inv: Invariants,
    baseline: dict[str, int] | None = None,
    *,
    ignore: tuple[str, ...] = (),
) -> list[str]:
    """返回差异描述列表；空列表表示全部吻合。"""
    base = BASELINE if baseline is None else baseline
    diffs: list[str] = []
    for name, actual in asdict(inv).items():
        if name in ignore:
            continue
        expected = base.get(name)
        if expected is None:
            continue
        if expected != actual:
            diffs.append(f"{name}: 期望 {expected}，实际 {actual}")
    return diffs


def format_report(inv: Invariants, diffs: list[str], *, title: str = "导入不变量") -> str:
    lines = [f"{title}（基线比对 {len(asdict(inv)) - len(diffs)} 项吻合 / {len(diffs)} 项不符）"]
    for name, actual in asdict(inv).items():
        expected = BASELINE.get(name)
        mark = ""
        if expected is not None:
            mark = " ✓" if expected == actual else f" ✗ 期望 {expected}"
        lines.append(f"  {name:<16} {actual:>12}{mark}")
    if diffs:
        lines.append("不符项：")
        lines.extend(f"  - {item}" for item in diffs)
    return "\n".join(lines)
