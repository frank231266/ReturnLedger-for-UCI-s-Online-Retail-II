import openpyxl
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "data" / "raw" / "online_retail_II.xlsx"
wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
sets = {}
for sheet in wb.sheetnames:
    ws = wb[sheet]
    rows = ws.iter_rows(values_only=True)
    header = [str(h).strip() for h in next(rows)]
    idx = {h: i for i, h in enumerate(header)}
    s = set()
    for r in rows:
        if r[idx["Invoice"]] is None:
            continue
        s.add(str(r[idx["Invoice"]]).strip())
    sets[sheet] = s
a, b = list(sets.values())
print("sheet sizes:", {k: len(v) for k, v in sets.items()})
inter = a & b
print("invoices in both sheets:", len(inter))
print("sample:", sorted(inter)[:10])
wb.close()
