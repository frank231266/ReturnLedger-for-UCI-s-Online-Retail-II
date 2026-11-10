"""抽 20 张发票，把原始 xlsx 与 order_line.csv 逐张对照。

为什么需要它
------------
报告里写了成功标准"抽 20 张发票，明细行数与数量合计与 Excel 手算一致"。
这句话如果只是嘴上说，老师一问"哪 20 张？表呢？"就答不上来。
本脚本把这件事做成可重跑的对照表：

    xlsx（原始件，只读）  ──按 R-02 去重──┐
                                          ├─ 逐张比 行数 / 数量合计
    order_line.csv（系统唯一数据源） ─────┘

抽样怎么定
----------
不用随机数（换个时间跑结果就变了），而是"从头到尾均匀取 18 张 + 重叠期取 2 张"：
重叠期那 2 张专治 R-02 —— 它们同时出现在两个 sheet 里，只有去重规则写对，
行数和数量合计才对得上。

口径
----
R-02 实测是"保留首现表"：重叠期 22,523 行全部落在 Year 2009-2010。
本脚本按同一规则算 xlsx 侧，所以对照的不是"我手算的数字"，而是"同一规则下
两份数据是否一致"。

用法
----
    python 核对脚本/04-二十张发票对照.py
    python 核对脚本/04-二十张发票对照.py --out data/build/发票对照20张.txt
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
XLSX = ROOT / "data" / "raw" / "online_retail_II.xlsx"
CSV_PATH = ROOT / "data" / "build" / "order_line.csv"
OVERLAP_LO = "2010-12-01"
OVERLAP_HI = "2010-12-10"   # 左闭右开
SAMPLE_SIZE = 20
OVERLAP_SAMPLE = 2


def norm_invoice(value: object) -> str:
    """xlsx 里发票号可能是 int / float / str，统一成不带 .0 的字符串。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.upper()


def num(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def pick_invoices(csv_path: Path) -> tuple[list[str], dict[str, str]]:
    """均匀抽 18 张 + 重叠期抽 2 张，返回（发票列表, 发票→来源表）。"""
    order: list[str] = []
    seen: set[str] = set()
    overlap: list[str] = []
    overlap_seen: set[str] = set()

    with csv_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            invoice = row["invoice"]
            if invoice not in seen:
                seen.add(invoice)
                order.append(invoice)
            if (
                row["invoice_date"] >= OVERLAP_LO
                and row["invoice_date"] < OVERLAP_HI
                and invoice not in overlap_seen
            ):
                overlap_seen.add(invoice)
                overlap.append(invoice)

    step = max(1, len(order) // SAMPLE_SIZE)
    spread = [order[i * step] for i in range(SAMPLE_SIZE) if i * step < len(order)]
    chosen: list[str] = []
    for invoice in spread[: SAMPLE_SIZE - OVERLAP_SAMPLE]:
        if invoice not in chosen:
            chosen.append(invoice)
    for invoice in overlap[:OVERLAP_SAMPLE]:
        if invoice not in chosen:
            chosen.append(invoice)

    # 补足到 20 张（极端情况下重叠期发票已被均匀抽样挑走）
    for invoice in order:
        if len(chosen) >= SAMPLE_SIZE:
            break
        if invoice not in chosen:
            chosen.append(invoice)

    return chosen[:SAMPLE_SIZE], {invoice: "" for invoice in chosen}


def scan_xlsx(xlsx_path: Path, wanted: set[str]) -> dict[str, dict[str, object]]:
    """按 R-02 去重规则统计 xlsx 侧：发票只算它首次出现的那张表。"""
    stats: dict[str, dict[str, object]] = {
        invoice: {"rows": 0, "qty": 0.0, "sheet": ""} for invoice in wanted
    }
    owner: dict[str, str] = {}
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        for sheet in wb.sheetnames:          # 与 prepare.py 相同的表顺序
            ws = wb[sheet]
            rows = ws.iter_rows(values_only=True)
            header = [str(h).strip() for h in next(rows)]
            idx = {name: i for i, name in enumerate(header)}
            col_inv, col_qty = idx["Invoice"], idx["Quantity"]
            for record in rows:
                invoice = norm_invoice(record[col_inv])
                if invoice not in wanted:
                    continue
                if owner.setdefault(invoice, sheet) != sheet:
                    continue                 # 跨表去重：非首现表，丢弃
                stats[invoice]["rows"] = int(stats[invoice]["rows"]) + 1
                stats[invoice]["qty"] = float(stats[invoice]["qty"]) + num(record[col_qty])
                stats[invoice]["sheet"] = sheet
    finally:
        wb.close()
    return stats


def scan_csv(csv_path: Path, wanted: set[str]) -> dict[str, dict[str, object]]:
    stats: dict[str, dict[str, object]] = {
        invoice: {"rows": 0, "qty": 0.0, "sheet": ""} for invoice in wanted
    }
    with csv_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            invoice = row["invoice"]
            if invoice not in wanted:
                continue
            stats[invoice]["rows"] = int(stats[invoice]["rows"]) + 1
            stats[invoice]["qty"] = float(stats[invoice]["qty"]) + num(row["quantity"])
            stats[invoice]["sheet"] = row["sheet"]
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="20 张发票：xlsx 与 order_line.csv 对照")
    parser.add_argument("--xlsx", default=str(XLSX))
    parser.add_argument("--csv", default=str(CSV_PATH))
    parser.add_argument("--out", default=None, help="同时把结果写成文件（证据）")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    for path in (Path(args.xlsx), Path(args.csv)):
        if not path.exists():
            print(f"错误：找不到 {path}")
            return 2

    invoices, _ = pick_invoices(Path(args.csv))
    wanted = set(invoices)
    print(f"抽样：{len(invoices)} 张发票（均匀抽 {SAMPLE_SIZE - OVERLAP_SAMPLE} 张 + "
          f"重叠期 {OVERLAP_SAMPLE} 张）\n")

    xlsx_stats = scan_xlsx(Path(args.xlsx), wanted)
    csv_stats = scan_csv(Path(args.csv), wanted)

    header = (
        f"{'发票号':<10}{'来源表':<16}{'xlsx行数':>9}{'CSV行数':>9}"
        f"{'xlsx数量':>11}{'CSV数量':>11}  结果"
    )
    lines = [
        "20 张发票对照：原始 xlsx（按 R-02 保留首现表） vs order_line.csv（系统唯一数据源）",
        "口径：数量合计 = Quantity 列直接求和（含取消行负数量），不做可退过滤",
        "",
        header,
        "-" * len(header),
    ]

    bad = 0
    for invoice in invoices:
        xs, cs = xlsx_stats[invoice], csv_stats[invoice]
        same = (
            int(xs["rows"]) == int(cs["rows"])
            and abs(float(xs["qty"]) - float(cs["qty"])) < 1e-6
            and xs["rows"] != 0
        )
        if not same:
            bad += 1
        lines.append(
            f"{invoice:<10}{str(xs['sheet'] or cs['sheet']):<16}"
            f"{int(xs['rows']):>9}{int(cs['rows']):>9}"
            f"{fmt(float(xs['qty'])):>11}{fmt(float(cs['qty'])):>11}  "
            f"{'一致' if same else '不一致'}"
        )

    lines.append("-" * len(header))
    lines.append(f"结论：{len(invoices)} 张中 {len(invoices) - bad} 张一致，{bad} 张不一致")
    lines.append(
        "能证明什么：这两份数据在抽样发票上逐行对得上，包括重叠期（R-02 去重生效）。"
    )
    lines.append(
        "不能证明什么：只证明「搬得对」，不证明业务规则对；也不等于全量核对"
        "（全量由导入时 13 项不变量负责）。"
    )

    text = "\n".join(lines)
    print(text)

    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        print(f"\n已写入 {target}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
