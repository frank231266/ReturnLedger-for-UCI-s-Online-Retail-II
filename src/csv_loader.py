"""读取 data/build/order_line.csv（上游 prepare.py 的产物）。

职责边界
--------
本模块只做"把 CSV 变成 LineRaw"，不做任何业务判断：
- 不判取消行（用上游已算好的 is_cancel 标记位）
- 不判非商品（用 is_non_product）
- 不生成明细键（用上游的 line_key）
- 不分配历史占用（见 history.py）

为什么严格校验表头顺序
--------------------
列顺序是"数据契约"的一部分。上游 prepare.py 的 OUT_FIELDS 一旦调整，
按位置取值会静默错位（例如把 price 读成 quantity），这比直接报错难查得多。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from typing import NamedTuple

# 数据契约：与 src/prepare.py 的 OUT_FIELDS 一致，顺序敏感
HEADER: tuple[str, ...] = (
    "sheet",
    "invoice",
    "stock_code",
    "description",
    "quantity",
    "invoice_date",
    "price",
    "cust_id",
    "country",
    "seq",
    "line_key",
    "is_cancel",
    "is_bad_debt",
    "is_non_product",
    "returnable_qty",
)

_FIELD_COUNT = len(HEADER)


class CsvFormatError(ValueError):
    """表头或字段不可解析。message 必须带列名或行号，禁止静默跳过。"""


class LineRaw(NamedTuple):
    """一条明细的全部字段，与 CSV 一列对一列。"""

    sheet: str
    invoice: str
    stock_code: str
    description: str
    quantity: int
    invoice_date: str
    price: float
    cust_id: str | None
    country: str
    seq: int
    line_key: str
    is_cancel: int
    is_bad_debt: int
    is_non_product: int
    returnable_qty: int


def read_header(csv_path: str) -> tuple[str, ...]:
    """只读表头，用于快速自检与报错。"""
    try:
        handle = io.open(csv_path, "r", encoding="utf-8", newline="")
    except OSError as exc:
        raise CsvFormatError(f"无法打开 CSV：{csv_path}（{exc.strerror or exc}）") from exc
    try:
        reader = csv.reader(handle)
        try:
            first = next(reader)
        except StopIteration:
            raise CsvFormatError(f"CSV 是空文件：{csv_path}") from None
    finally:
        handle.close()
    return tuple(cell.strip().lstrip("\ufeff") for cell in first)


def check_header(csv_path: str) -> tuple[str, ...]:
    """严格校验表头：缺列、多列、顺序不符都直接报错。"""
    actual = read_header(csv_path)
    if actual != HEADER:
        missing = [name for name in HEADER if name not in actual]
        extra = [name for name in actual if name not in HEADER]
        hint = ""
        if not missing and not extra:
            hint = "（列名齐全但顺序不同）"
        raise CsvFormatError(
            f"表头不符{hint}：缺少 {missing}，多出 {extra}\n"
            f"  期望 {list(HEADER)}\n  实际 {list(actual)}"
        )
    return actual


def to_float(text: str, field: str, row_no: int) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        raise CsvFormatError(f"第 {row_no} 行 {field} 不是数字：{text!r}") from None


def to_int(text: str, field: str, row_no: int) -> int:
    """CSV 里数量写作 '12.0'，必须容忍；但 '12.5' 属于数据异常，直接报错不四舍五入。"""
    value = to_float(text, field, row_no)
    if value != int(value):
        raise CsvFormatError(f"第 {row_no} 行 {field} 不是整数：{text!r}")
    return int(value)


def to_cust(text: str) -> str | None:
    """空字符串与纯空白都视为"无客户号"，返回 None；否则返回去空格的数字串。"""
    cleaned = (text or "").strip()
    return cleaned or None


def iter_lines(csv_path: str, *, limit: int | None = None) -> Iterator[LineRaw]:
    """流式产出 LineRaw。

    limit 仅用于测试切片（取前 N 条数据行）；用于统计时必须为 None。
    """
    check_header(csv_path)
    handle = io.open(csv_path, "r", encoding="utf-8", newline="")
    try:
        reader = csv.reader(handle)
        next(reader)  # 跳过表头（已校验）
        data_rows = 0
        for row_no, row in enumerate(reader, start=2):
            if len(row) != _FIELD_COUNT:
                raise CsvFormatError(
                    f"第 {row_no} 行有 {len(row)} 个字段，期望 {_FIELD_COUNT} 个：{row!r}"
                )
            if limit is not None and data_rows >= limit:
                return
            data_rows += 1
            yield LineRaw(
                sheet=row[0],
                invoice=row[1].strip(),
                stock_code=row[2].strip(),
                description=row[3],
                quantity=to_int(row[4], "quantity", row_no),
                invoice_date=row[5].strip(),
                price=to_float(row[6], "price", row_no),
                cust_id=to_cust(row[7]),
                country=row[8],
                seq=to_int(row[9], "seq", row_no),
                line_key=row[10].strip(),
                is_cancel=to_int(row[11], "is_cancel", row_no),
                is_bad_debt=to_int(row[12], "is_bad_debt", row_no),
                is_non_product=to_int(row[13], "is_non_product", row_no),
                returnable_qty=to_int(row[14], "returnable_qty", row_no),
            )
    finally:
        handle.close()
