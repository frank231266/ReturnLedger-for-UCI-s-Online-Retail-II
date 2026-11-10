import openpyxl
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "data" / "raw" / "online_retail_II.xlsx"
wb = openpyxl.load_workbook(path, read_only=True, data_only=True)

for sheet in wb.sheetnames:
    ws = wb[sheet]
    rows = ws.iter_rows(values_only=True)
    header = [str(h).strip() for h in next(rows)]
    idx = {h: i for i, h in enumerate(header)}
    pos = defaultdict(float)      # (cust, sku) -> total positive qty
    pos_orders = defaultdict(set)
    crow_list = []
    for r in rows:
        if r[idx["Invoice"]] is None:
            continue
        invs = str(r[idx["Invoice"]]).strip()
        sku = str(r[idx["StockCode"]]).strip()
        cust = r[idx["Customer ID"]]
        cust = "" if cust is None else str(cust).strip()
        try:
            q = float(r[idx["Quantity"]])
        except (TypeError, ValueError):
            continue
        key = (cust, sku)
        if invs[:1] == "C":
            crow_list.append((key, abs(q), invs))
        else:
            if q > 0:
                pos[key] += q
                pos_orders[key].add(invs)
    enough = over = no_orig = no_cust = 0
    for key, need, invs in crow_list:
        if key[0] == "":
            no_cust += 1
            continue
        avail = pos.get(key, 0.0)
        if avail <= 0:
            no_orig += 1
        elif avail >= need:
            enough += 1
        else:
            over += 1
    print("=" * 70)
    print("SHEET:", sheet)
    print(f"C_rows={len(crow_list)}  no_CustomerID={no_cust}  no_original_found={no_orig}  qty_within_original={enough}  qty_exceeds_original={over}")
wb.close()
