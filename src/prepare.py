"""
prepare.py —— VC-R-01 数据准备管线：读取 → 清洗 → 切片 → 转换

对应课程要求（03-数据说明与准备）：
    "下载、读取、清洗、筛选、转换；说明为什么这样做"

设计要点
--------
1) 读取分两段：xlsx 用 openpyxl 只读流式读取一次，转成 CSV 缓存；
   之后所有清洗/统计都读 CSV。理由：1,067,371 行 xlsx 每次重读约 1 分钟，
   CSV 重读约 5 秒，改清洗规则时可以反复迭代。
2) 清洗的每一个动作都对应一条可写进报告的规则（R-01/R-02/R-03/R-06）。
3) 明细键 = 工作表 + 发票号 + 该发票内的记录序号（R-01）。
   序号按"发票内第几条记录"计，因为 发票号+商品编码 存在重复组合。
4) 原始文件只读，永不修改；所有产物落在 data/build/。

用法
----
    python src/prepare.py --xlsx data/raw/online_retail_II.xlsx
    python src/prepare.py --xlsx ... --rebuild-csv      # 强制重读 xlsx
    python src/prepare.py ... --sample-per-sheet 1000   # 额外产出测试切片
    python src/prepare.py ... --since 2010-12-01 --until 2011-12-09
    python src/prepare.py ... --cust 17850,14688        # 按客户切片
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------- 常量与配置

COLUMN_MAP = {
    # 原始列名 -> 规范列名。注意原文是 Invoice 而不是 InvoiceNo，Customer ID 带空格。
    "Invoice": "invoice",
    "StockCode": "stock_code",
    "Description": "description",
    "Quantity": "quantity",
    "InvoiceDate": "invoice_date",
    "Price": "price",
    "Customer ID": "cust_id",
    "Country": "country",
}

RAW_FIELDS = ["sheet"] + list(COLUMN_MAP.values())
OUT_FIELDS = RAW_FIELDS + [
    "seq",            # 该发票内的记录序号（R-01 明细键的一部分）
    "line_key",       # 明细键：sheet|invoice|seq
    "is_cancel",      # R-03：Invoice 以 C 开头
    "is_bad_debt",    # Invoice 以 A 开头（坏账调整）
    "is_non_product", # StockCode 不是商品编码（运费、手工调整、测试商品等）
    "returnable_qty", # 可退数量（本版 = 原始数量；已退数量由应用层维护）
]

# 商品编码形态：5 位数字，可带 1~2 位字母后缀（如 22423 / 85048B / 10002）
PRODUCT_CODE_RE = re.compile(r"^\d{5}[A-Za-z]{0,2}$")


# ---------------------------------------------------------------- 工具函数

def to_float(value):
    """把可能是字符串/空值的数字转成 float，失败返回 None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_iso(value):
    """InvoiceDate 统一成 'YYYY-MM-DD HH:MM:SS' 字符串，便于排序与比较。"""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    text = str(value).strip() if value is not None else ""
    return text or None


def norm_text(value):
    """文本归一：去首尾空格、压缩内部空白；空字符串视为 None。"""
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


# ---------------------------------------------------------------- 阶段 1：读取

def xlsx_to_csv(xlsx_path: Path, out_dir: Path, force: bool) -> list[Path]:
    """流式读取 xlsx 每个 sheet，原样落成 CSV（不做任何清洗）。"""
    import openpyxl

    targets = []
    workbook = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        for sheet_name in workbook.sheetnames:
            safe = sheet_name.replace(" ", "_").replace("/", "-")
            out_csv = out_dir / f"raw_{safe}.csv"
            targets.append(out_csv)
            if out_csv.exists() and not force:
                print(f"[读取] 已缓存，跳过：{out_csv.name} "
                      f"({out_csv.stat().st_size / 1024 / 1024:.1f} MB)")
                continue

            t0 = time.time()
            sheet = workbook[sheet_name]
            rows = sheet.iter_rows(values_only=True)
            header = [str(h).strip() for h in next(rows)]
            try:
                index = [header.index(raw) for raw in COLUMN_MAP]
            except ValueError as exc:
                raise SystemExit(f"列名不匹配，实际表头={header}") from exc

            written = 0
            with out_csv.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(RAW_FIELDS)
                for row in rows:
                    picked = [row[i] for i in index]
                    # 尾部空行：发票号和商品编码都为空则丢弃
                    if picked[0] is None and picked[1] is None:
                        continue
                    writer.writerow([sheet_name] + ["" if v is None else v for v in picked])
                    written += 1
            print(f"[读取] {sheet_name}: {written:,} 行 -> {out_csv.name} "
                  f"({time.time() - t0:.1f}s)")
    finally:
        workbook.close()
    return targets


# ---------------------------------------------------------------- 阶段 2：清洗

def clean_and_convert(raw_csvs: list[Path], out_csv: Path, stats: Counter):
    """
    清洗 + 去重（R-02）+ 生成明细键（R-01）+ 取消行标记（R-03）。
    一次顺序扫描，边读边写，内存只保留发票号集合与序号计数器。
    """
    seen_invoice: dict[str, str] = {}     # R-02：发票号 -> 首次出现的工作表
    seq_counter: Counter = Counter()      # R-01：发票内序号
    invoice_rows_per_sheet: dict[str, dict[str, int]] = {}
    non_product_codes: Counter = Counter()

    with out_csv.open("w", encoding="utf-8", newline="") as out_handle:
        writer = csv.DictWriter(out_handle, fieldnames=OUT_FIELDS)
        writer.writeheader()

        for raw_csv in raw_csvs:
            with raw_csv.open(encoding="utf-8", newline="") as in_handle:
                for row in csv.DictReader(in_handle):
                    stats["rows_read"] += 1
                    sheet = row["sheet"]
                    invoice = norm_text(row["invoice"])
                    stock_code = norm_text(row["stock_code"])

                    # --- 1. 主键缺失：无法定位，只能丢弃，必须计数上报
                    if not invoice or not stock_code:
                        stats["drop_missing_key"] += 1
                        continue

                    # --- 2. 跨表去重（R-02）：同一发票只在首次出现的表里保留，
                    #        同一张表内该发票的所有行都要保留（一张发票有多行明细）
                    per_sheet = invoice_rows_per_sheet.setdefault(sheet, {})
                    per_sheet[invoice] = per_sheet.get(invoice, 0) + 1
                    owner = seen_invoice.get(invoice)
                    if owner is None:
                        seen_invoice[invoice] = sheet
                    elif owner != sheet:
                        stats["drop_duplicate_invoice"] += 1
                        continue

                    # --- 3. 数值与日期归一
                    quantity = to_float(row["quantity"])
                    price = to_float(row["price"])
                    invoice_date = to_iso(row["invoice_date"])
                    if quantity is None:
                        stats["bad_quantity"] += 1
                        quantity = 0.0
                    if price is None:
                        stats["bad_price"] += 1
                        price = 0.0
                    if invoice_date is None:
                        stats["bad_date"] += 1

                    # --- 4. 取消行判定（R-03）：只看前缀，不看正负
                    upper = invoice.upper()
                    is_cancel = 1 if upper.startswith("C") else 0
                    is_bad_debt = 1 if upper.startswith("A") else 0

                    # --- 5. 非商品编码（运费、手工调整、测试商品等）
                    is_non_product = 0 if PRODUCT_CODE_RE.match(stock_code.upper()) else 1

                    # --- 6. 客户号缺失（R-06 的触发条件）
                    cust_id = norm_text(row["cust_id"])
                    if cust_id is None:
                        stats["missing_cust"] += 1
                    if norm_text(row["description"]) is None:
                        stats["missing_desc"] += 1

                    # --- 7. 明细键（R-01）
                    seq_counter[invoice] += 1
                    seq = seq_counter[invoice]
                    line_key = f"{sheet}|{invoice}|{seq}"

                    # --- 8. 可退数量：只有"商品行 + 普通销售 + 数量为正"才可退。
                    #        非商品编码（运费 POST、手工调整 M/D、银行费用等）不进退货流程。
                    returnable = 0.0
                    if not is_cancel and not is_bad_debt and not is_non_product and quantity > 0:
                        returnable = quantity
                        stats["returnable_rows"] += 1
                    elif is_non_product:
                        non_product_codes[stock_code.upper()] += 1

                    writer.writerow({
                        "sheet": sheet,
                        "invoice": invoice,
                        "stock_code": stock_code,
                        "description": norm_text(row["description"]) or "",
                        "quantity": quantity,
                        "invoice_date": invoice_date or "",
                        "price": price,
                        "cust_id": cust_id or "",
                        "country": norm_text(row["country"]) or "",
                        "seq": seq,
                        "line_key": line_key,
                        "is_cancel": is_cancel,
                        "is_bad_debt": is_bad_debt,
                        "is_non_product": is_non_product,
                        "returnable_qty": returnable,
                    })
                    stats["rows_written"] += 1
                    if is_cancel:
                        stats["cancel_rows"] += 1
                    if is_bad_debt:
                        stats["bad_debt_rows"] += 1
                    if is_non_product:
                        stats["non_product_rows"] += 1

    # --- 9. 重叠发票的校验：两张表里这些发票的行数是否一致
    names = list(invoice_rows_per_sheet)
    if len(names) >= 2:
        left, right = invoice_rows_per_sheet[names[0]], invoice_rows_per_sheet[names[1]]
        overlap = set(left) & set(right)
        stats["overlap_invoices"] = len(overlap)
        stats["overlap_rowcount_mismatch"] = sum(
            1 for inv in overlap if left[inv] != right[inv]
        )
    return non_product_codes


# ---------------------------------------------------------------- 阶段 3：转换

CREATE_SQL = """
CREATE TABLE order_line (
    line_key       TEXT PRIMARY KEY,
    sheet          TEXT NOT NULL,
    invoice        TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    stock_code     TEXT NOT NULL,
    description    TEXT,
    quantity       REAL NOT NULL,
    price          REAL NOT NULL,
    invoice_date   TEXT,
    cust_id        TEXT,
    country        TEXT,
    is_cancel      INTEGER NOT NULL DEFAULT 0,
    is_bad_debt    INTEGER NOT NULL DEFAULT 0,
    is_non_product INTEGER NOT NULL DEFAULT 0,
    returned_qty   REAL NOT NULL DEFAULT 0,
    returnable_qty REAL NOT NULL DEFAULT 0
);
CREATE INDEX idx_line_invoice ON order_line(invoice);
CREATE INDEX idx_line_cust    ON order_line(cust_id);
CREATE INDEX idx_line_stock   ON order_line(stock_code);
CREATE TABLE import_stat (k TEXT PRIMARY KEY, v TEXT);
"""

INSERT_SQL = (
    "INSERT INTO order_line (line_key, sheet, invoice, seq, stock_code, description,"
    " quantity, price, invoice_date, cust_id, country, is_cancel, is_bad_debt,"
    " is_non_product, returned_qty, returnable_qty)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)"
)


def csv_to_sqlite(clean_csv: Path, db_path: Path, stats: Counter):
    """清洗后的 CSV 落成 SQLite：给规则校验一个可索引、可事务的存储。"""
    t0 = time.time()
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(CREATE_SQL)
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")

        batch, total = [], 0
        with clean_csv.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                batch.append((
                    row["line_key"], row["sheet"], row["invoice"], int(row["seq"]),
                    row["stock_code"], row["description"],
                    float(row["quantity"]), float(row["price"]),
                    row["invoice_date"], row["cust_id"], row["country"],
                    int(row["is_cancel"]), int(row["is_bad_debt"]),
                    int(row["is_non_product"]), float(row["returnable_qty"]),
                ))
                if len(batch) >= 5000:
                    connection.executemany(INSERT_SQL, batch)
                    total += len(batch)
                    batch.clear()
            if batch:
                connection.executemany(INSERT_SQL, batch)
                total += len(batch)

        for key, value in stats.items():
            connection.execute(
                "INSERT OR REPLACE INTO import_stat (k, v) VALUES (?, ?)", (key, str(value))
            )
        connection.commit()
    finally:
        connection.close()
    stats["db_rows"] = total
    print(f"[转换] SQLite 写入 {total:,} 行 -> {db_path.name} "
          f"({db_path.stat().st_size / 1024 / 1024:.1f} MB, {time.time() - t0:.1f}s)")


# ---------------------------------------------------------------- 阶段 4：切片

def slice_csv(clean_csv: Path, out_dir: Path, sample_per_sheet: int,
              since: str | None, until: str | None, custs: set[str] | None):
    """按时间 / 客户 / 行数切片，产出可直接给测试与演示用的小文件。"""
    if since or until or custs:
        target = out_dir / "order_line_filtered.csv"
        kept = 0
        with clean_csv.open(encoding="utf-8", newline="") as in_handle, \
                target.open("w", encoding="utf-8", newline="") as out_handle:
            reader = csv.DictReader(in_handle)
            writer = csv.DictWriter(out_handle, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                day = (row["invoice_date"] or "")[:10]
                if since and day and day < since:
                    continue
                if until and day and day > until:
                    continue
                if custs and row["cust_id"] not in custs:
                    continue
                writer.writerow(row)
                kept += 1
        print(f"[切片] 条件筛选 -> {target.name}: {kept:,} 行")
        return target

    if sample_per_sheet > 0:
        # 每个 sheet 取前 N 行：够跑单测，又能覆盖到取消行与缺客户号行
        target = out_dir / f"sample_{sample_per_sheet * 2}.csv"
        counter: Counter = Counter()
        with clean_csv.open(encoding="utf-8", newline="") as in_handle, \
                target.open("w", encoding="utf-8", newline="") as out_handle:
            reader = csv.DictReader(in_handle)
            writer = csv.DictWriter(out_handle, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                if counter[row["sheet"]] >= sample_per_sheet:
                    continue
                counter[row["sheet"]] += 1
                writer.writerow(row)
        print(f"[切片] 每表前 {sample_per_sheet} 行 -> {target.name}: {sum(counter.values()):,} 行")
        return target
    return None


# ---------------------------------------------------------------- 汇总报告

def print_stats(stats: Counter, raw_csvs, clean_csv: Path, db_path: Path | None,
                non_product_codes: Counter | None = None):
    def line(label, value):
        print(f"  {label:<24}{value}")

    print("\n" + "=" * 62)
    print("清洗结果统计（可直接写进报告的『数据来源与准备过程』）")
    print("=" * 62)
    line("原始文件", "data/raw/online_retail_II.xlsx")
    line("工作表数", len(raw_csvs))
    line("读取行数", f"{stats['rows_read']:,}")
    line("丢弃：主键缺失", f"{stats['drop_missing_key']:,}")
    line("丢弃：跨表重复发票", f"{stats['drop_duplicate_invoice']:,}")
    line("两张表重叠发票", f"{stats['overlap_invoices']:,}")
    line("  其中行数不一致", f"{stats['overlap_rowcount_mismatch']:,}")
    line("写入明细行", f"{stats['rows_written']:,}")
    line("  C 开头取消行", f"{stats['cancel_rows']:,}")
    line("  A 开头坏账行", f"{stats['bad_debt_rows']:,}")
    line("  非商品编码行", f"{stats['non_product_rows']:,}")
    line("  可退明细行(>0)", f"{stats['returnable_rows']:,}")
    line("缺客户号行", f"{stats['missing_cust']:,}")
    line("缺商品名行", f"{stats['missing_desc']:,}")
    line("数量无法解析", f"{stats['bad_quantity']:,}")
    line("价格无法解析", f"{stats['bad_price']:,}")
    line("日期无法解析", f"{stats['bad_date']:,}")
    line("清洗后 CSV", f"{clean_csv.name}")
    if db_path and db_path.exists():
        line("SQLite", f"{db_path.name} ({db_path.stat().st_size/1024/1024:.1f} MB, {stats['db_rows']:,} 行)")
    if non_product_codes:
        top = ", ".join(f"{code}({n})" for code, n in non_product_codes.most_common(8))
        line("非商品编码 Top", top)
    print("=" * 62)


# ---------------------------------------------------------------- 主流程

def main():
    parser = argparse.ArgumentParser(description="VC-R-01 数据准备管线")
    parser.add_argument("--xlsx", default="data/raw/online_retail_II.xlsx")
    parser.add_argument("--out-dir", default="data/build")
    parser.add_argument("--rebuild-csv", action="store_true", help="强制重读 xlsx")
    parser.add_argument("--sample-per-sheet", type=int, default=0)
    parser.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--until", default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--cust", default=None, help="逗号分隔的客户号，如 17850,14688")
    parser.add_argument("--no-db", action="store_true", help="只出 CSV，不建 SQLite")
    args = parser.parse_args()

    xlsx = Path(args.xlsx)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not xlsx.exists():
        raise SystemExit(f"原始文件不存在：{xlsx}")

    t_start = time.time()
    stats: Counter = Counter()

    raw_csvs = xlsx_to_csv(xlsx, out_dir, args.rebuild_csv)
    clean_csv = out_dir / "order_line.csv"
    non_product_codes = clean_and_convert(raw_csvs, clean_csv, stats)

    db_path = out_dir / "order_line.db"
    if not args.no_db:
        csv_to_sqlite(clean_csv, db_path, stats)
    else:
        db_path = None

    custs = set(c.strip() for c in args.cust.split(",")) if args.cust else None
    slice_csv(clean_csv, out_dir, args.sample_per_sheet, args.since, args.until, custs)

    print_stats(stats, raw_csvs, clean_csv, db_path, non_product_codes)
    print(f"\n总耗时 {time.time() - t_start:.1f}s")

    # 注意：这里**不能**叫 实际运行结果.txt。
    # 那是本系统（main.py）的证据文件，由 tools/make_evidence.py 生成；
    # 数据准备阶段如果也用同名文件，会把系统证据覆盖掉，门禁立刻失败。
    with (out_dir / "prepare_stats.txt").open("w", encoding="utf-8") as handle:
        for key, value in stats.items():
            handle.write(f"{key}={value}\n")
    print(f"统计已保存：{out_dir / 'prepare_stats.txt'}")


if __name__ == "__main__":
    main()
