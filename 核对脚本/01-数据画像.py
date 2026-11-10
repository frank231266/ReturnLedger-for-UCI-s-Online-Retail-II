import openpyxl
from collections import Counter
from datetime import datetime
from pathlib import Path

# 路径从本文件反推仓库根，换台机器/换个目录名都不用改
ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "data" / "raw" / "online_retail_II.xlsx"
wb = openpyxl.load_workbook(path, read_only=True, data_only=True)

for sheet in wb.sheetnames:
    ws = wb[sheet]
    rows = ws.iter_rows(values_only=True)
    header = [str(h).strip() for h in next(rows)]
    idx = {h: i for i, h in enumerate(header)}
    print("=" * 70)
    print("SHEET:", sheet, "HEADER:", header)
    n = 0
    c_rows = 0
    a_rows = 0
    neg_qty = 0
    zero_qty = 0
    missing_cust = 0
    missing_desc = 0
    min_d = max_d = None
    inv_set = set()
    pair_counter = Counter()
    cust_set = set()
    c_invoices = set()
    neg_price = 0
    for r in rows:
        if r[idx["Invoice"]] is None and r[idx["StockCode"]] is None:
            continue
        n += 1
        invs = str(r[idx["Invoice"]]).strip()
        sku = str(r[idx["StockCode"]]).strip()
        qty = r[idx["Quantity"]]
        dt = r[idx["InvoiceDate"]]
        price = r[idx["Price"]]
        cust = r[idx["Customer ID"]]
        desc = r[idx["Description"]]
        if invs[:1] == "C":
            c_rows += 1
            c_invoices.add(invs)
        elif invs[:1] == "A":
            a_rows += 1
        inv_set.add(invs)
        try:
            q = float(qty)
            if q < 0:
                neg_qty += 1
            elif q == 0:
                zero_qty += 1
        except (TypeError, ValueError):
            pass
        try:
            if float(price) < 0:
                neg_price += 1
        except (TypeError, ValueError):
            pass
        if cust is None:
            missing_cust += 1
        else:
            cust_set.add(str(cust).strip())
        if desc is None or str(desc).strip() == "":
            missing_desc += 1
        if isinstance(dt, datetime):
            min_d = dt if min_d is None or dt < min_d else min_d
            max_d = dt if max_d is None or dt > max_d else max_d
        pair_counter[(invs, sku)] += 1
    dup_pairs = sum(1 for k, v in pair_counter.items() if v > 1)
    dup_rows = sum(v for v in pair_counter.values() if v > 1)
    print(f"rows={n} unique_invoices={len(inv_set)} C_rows={c_rows} C_invoices={len(c_invoices)} A_rows={a_rows}")
    print(f"neg_qty={neg_qty} zero_qty={zero_qty} neg_price={neg_price}")
    print(f"missing_CustomerID_rows={missing_cust} unique_customers={len(cust_set)} missing_desc={missing_desc}")
    print(f"date_range={min_d} .. {max_d}")
    print(f"dup(Invoice,StockCode)_pairs={dup_pairs} rows_in_dup_pairs={dup_rows}")
wb.close()
