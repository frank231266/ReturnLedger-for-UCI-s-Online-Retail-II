"""全量库验收：拿 data/build/return_system.db（1,044,848 行）真跑一遍界面接口。

为什么不并进 tools/webui_smoke.py
--------------------------------
`webui_smoke.py` 用 `sample_2000.csv` 建库，断言里写死了"2,000 行""q=489"这类切片事实，
好处是**任何机器、任何提交包**上都能跑（36 项，秒级）。
本文件是另一种问题：**全量库上有没有变慢、算错、撑不住**。没有 149 MB CSV 与 290 MB 库
就根本跑不了，所以它单独成一支，找不到全量库就明确跳过，绝不假装跑过。

它证明的是同一句话的另一半：
    切片上对，不等于全量上对（1M 行的聚合开销、前缀搜索、额度闭环、R-08 转人工）。

不动主库
--------
先把 `return_system.db` **复制**到 `_scratch/webui_full/full.db`，所有写操作只碰副本。
跑完全过就把副本删掉（失败才留着查）；主库、data/raw/、证据文件一个字节不碰。

用法
----
    python tools/webui_full_check.py           # 复制全量库后跑 30 项
    python tools/webui_full_check.py --keep     # 跑完保留副本
    python tools/webui_full_check.py --db 别的库.db
退出码 0 = 全过；1 = 有失败；2 = 找不到全量库（跳过，不算失败也不是通过）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import webui.server as server  # noqa: E402

FULL_DB = ROOT / "data" / "build" / "return_system.db"
SCRATCH = ROOT / "_scratch" / "webui_full"

BASE = ""
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok and detail:
        print(f"       └─ {detail[:400]}")
    return bool(ok)


# ---------------------------------------------------------------- HTTP 小工具


def get(path: str) -> tuple[int, str, float]:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(BASE + path, timeout=300) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), time.perf_counter() - started


def get_json(path: str) -> tuple[dict, float]:
    status, text, secs = get(path)
    try:
        return json.loads(text), secs
    except json.JSONDecodeError:
        return {"ok": False, "error": f"HTTP {status} 非 JSON：{text[:200]}"}, secs


def post_json(path: str, payload: dict) -> tuple[dict, float]:
    started = time.perf_counter()
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")), time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body), time.perf_counter() - started
        except json.JSONDecodeError:
            return {"ok": False, "error": f"HTTP {exc.code}：{body[:200]}"}, time.perf_counter() - started


# ---------------------------------------------------------------- 主流程


def prepare(explicit: str | None) -> Path | None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    if explicit:
        return Path(explicit)
    source = FULL_DB
    if not source.exists():
        return None
    target = SCRATCH / "full.db"
    for suffix in ("", "-wal", "-shm"):
        stale = target.with_name(target.name + suffix)
        if stale.exists():
            stale.unlink()
    started = time.perf_counter()
    shutil.copy2(source, target)
    print(f"已复制全量库 → {target.relative_to(ROOT)}（{time.perf_counter() - started:.1f}s）")
    return target


def main(argv: list[str] | None = None) -> int:
    global BASE
    parser = argparse.ArgumentParser(description="VC-R-01 界面全量库验收")
    parser.add_argument("--db", default=None, help="直接指定库（默认复制 data/build/return_system.db）")
    parser.add_argument("--keep", action="store_true", help="跑完保留副本，方便查")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    db_path = prepare(args.db)
    if db_path is None or not Path(db_path).exists():
        print(f"找不到全量库 {FULL_DB.relative_to(ROOT)}（或 --db 指定的文件）。")
        print("这是**跳过**，不是通过：本脚本只在有全量数据时才有意义。")
        print("  · 想验界面本身：python tools/webui_smoke.py（切片，36 项）")
        print("  · 想补全量数据：python src/prepare.py --no-db，再 python src/main.py import …")
        return 2

    server.STATE["db"] = Path(db_path).resolve()
    server.STATE["mode"] = "全量"
    server.STATE["note"] = "--db 指定的全量库副本（tools/webui_full_check.py）"
    server.Handler.log_message = lambda *a, **k: None      # 别刷屏

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"服务已起：{BASE}\n")

    try:
        run_checks(Path(db_path))
    finally:
        httpd.shutdown()
        httpd.server_close()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 60}\n  {passed}/{total} 项通过\n{'=' * 60}")
    if passed == total:
        if not args.keep and args.db is None:
            shutil.rmtree(SCRATCH, ignore_errors=True)
        return 0
    print(f"失败项留在 {SCRATCH.relative_to(ROOT)} 里，方便查")
    return 1


def run_checks(db_path: Path) -> None:  # noqa: C901（顺序就是剧本，不拆）
    # ---- 静态资源
    for path, needle in (("/", "VC-R-01"), ("/static/app.css", "--accent"), ("/static/app.js", "VC-R-01")):
        status, body, secs = get(path)
        check(f"GET {path} 200 且含关键字（{secs * 1000:.0f}ms）", status == 200 and needle in body, f"HTTP {status}")
    status, _, _ = get("/static/../server.py")
    check("静态目录不可穿越（/static/../server.py 被挡）", status in (403, 404), f"HTTP {status}")

    # ---- 概览：全表聚合，全量库上最慢的一个接口
    overview, secs = get_json("/api/overview")
    numbers = overview.get("data", {}).get("numbers", {})
    check("GET /api/overview 明细行数 = 1,044,848", numbers.get("lines") == 1044848,
          f"实际 {numbers.get('lines')}")
    check("概览的可退明细行 = 1,017,521（与 13 项不变量同一个口径）",
          numbers.get("retirable_rows") == 1017521, f"实际 {numbers.get('retirable_rows')}")
    check("概览的可退数量合计 = 11,465,120",
          numbers.get("retirable_sum") == 11465120, f"实际 {numbers.get('retirable_sum')}")
    check(f"概览耗时可接受（{secs:.2f}s < 3s）", secs < 3.0, f"{secs:.2f}s")

    # ---- 一张能心算的发票
    data = get_json("/api/invoice?q=536809")[0].get("data", {})
    check("GET /api/invoice?q=536809 命中", data.get("found"), str(data)[:200])
    line = (data.get("lines") or [{}])[0]
    check("536809|1 原始 1824 / 历史已退 4 / 可退 1820",
          line.get("returnable_qty") == 1824 and line.get("history_returned") == 4
          and line.get("retirable") == 1820,
          str({k: line.get(k) for k in ("returnable_qty", "history_returned", "active_qty", "retirable")}))

    # ---- 前缀搜索（前端 180ms 防抖会一直打这个接口）
    for term in ("5368", "5"):
        found, secs = get_json(f"/api/invoices?q={term}")
        count = len(found.get("data", {}).get("items", []))
        check(f"前缀搜索 q='{term}' 有结果且 < 1.5s（{secs * 1000:.0f}ms）",
              count > 0 and secs < 1.5, f"{count} 条 / {secs:.2f}s")

    miss, _ = get_json("/api/invoice?q=999999")
    check("不存在的发票明确返回未找到（不静默 200 空表）", miss.get("data", {}).get("found") is False,
          str(miss)[:160])

    plan, _ = get_json("/api/demo/plan")
    check("GET /api/demo/plan 在全量库上挑得到可演示明细", plan.get("data", {}).get("found"), str(plan)[:200])

    # ---- 退超量必须被拒
    over, _ = post_json("/api/apply", {"invoice": "536809", "seq": 1, "qty": 9999, "operator": "全量验收"})
    check("退 9999 件 → E_EXCEED / exit 2",
          over.get("cli", {}).get("result") == "E_EXCEED" and over["cli"]["exitCode"] == 2,
          str(over.get("cli", {}).get("lines"))[:200])

    # ---- 正常申请 → 额度下降 10
    applied, _ = post_json("/api/apply", {
        "invoice": "536809", "seq": 1, "qty": 10, "operator": "全量验收", "reason": "全量验收",
    })
    req_no = applied.get("reqNo") or ""
    check(f"申请 10 件 → OK 且拿到申请号（{req_no or '无'}）",
          applied.get("ok") and bool(req_no), "\n".join(applied.get("cli", {}).get("lines", []))[:300])
    after = (get_json("/api/invoice?q=536809")[0].get("data", {}).get("lines") or [{}])[0]
    check("申请后可退 1820 − 10 = 1810", after.get("retirable") == 1810, f"实际 {after.get('retirable')}")

    # ---- 状态机：跳级被拒 → 三跳 → 额度闭环
    jump, _ = post_json("/api/transition", {"req": req_no, "command": "close", "operator": "全量验收"})
    check("APPLIED 直接结案 → E_BAD_TRANSITION / exit 2",
          jump.get("cli", {}).get("result") == "E_BAD_TRANSITION" and jump["cli"]["exitCode"] == 2,
          str(jump.get("cli", {}).get("lines"))[:200])
    for command, expect in (("accept", "ACCEPTED"), ("receive", "RECEIVED"), ("close", "CLOSED")):
        out, _ = post_json("/api/transition", {"req": req_no, "command": command, "operator": "全量验收"})
        check(f"{command} → {expect}", out.get("ok") and out.get("request", {}).get("status") == expect,
              str(out.get("cli", {}).get("lines"))[:200])
    back = (get_json("/api/invoice?q=536809")[0].get("data", {}).get("lines") or [{}])[0]
    check("结案后可退闭环回到 1820", back.get("retirable") == 1820, f"实际 {back.get('retirable')}")

    # ---- R-08：缺客户号的明细（全量 235,287 行）从界面也能走通
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        guess = conn.execute(
            "SELECT invoice, seq FROM order_line"
            " WHERE cust_id IS NULL AND is_cancel = 0 AND is_non_product = 0"
            "   AND is_bad_debt = 0 AND returnable_qty > 0"
            "   AND NOT EXISTS (SELECT 1 FROM return_request r"
            "     WHERE r.line_key = order_line.line_key"
            "       AND r.status IN ('APPLIED','ACCEPTED','RECEIVED','PENDING_MANUAL'))"
            " LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if guess is None:
        check("缺客户号的明细可申请（R-08）", False, "全量库里没有「缺客户号且未结案」的明细")
    else:
        no_cust = get_json(f"/api/invoice?q={guess[0]}")[0].get("data", {})
        target = next(row for row in no_cust["lines"] if row["seq"] == guess[1])
        check("缺客户号的明细展示为「可申请（缺客户号→人工）」",
              target["code"] == "MANUAL" and target["needs_manual"] is True,
              str({k: target.get(k) for k in ("code", "text", "can_apply", "needs_manual")}))
        check("缺客户号的明细按钮可点（否则 R-08 从界面走不通）", target["can_apply"] is True,
              f"can_apply={target['can_apply']}")
        manual, _ = post_json("/api/apply", {"invoice": guess[0], "seq": guess[1], "qty": 1, "operator": "全量验收"})
        check("缺客户号提交 → E_MANUAL / exit 3，且带回申请号",
              manual.get("cli", {}).get("result") == "E_MANUAL" and manual["cli"]["exitCode"] == 3
              and manual.get("manual") is True and bool(manual.get("reqNo")),
              str(manual.get("cli", {}).get("lines"))[:200])
        pending = get_json(f"/api/request?req={manual.get('reqNo') or ''}")[0].get("data", {})
        check("转人工的单子落在 PENDING_MANUAL 且不占额度",
              pending.get("status") == "PENDING_MANUAL"
              and (pending.get("lineState") or {}).get("retirable") == target["retirable"],
              str({k: pending.get(k) for k in ("status", "reqNo")}))

    # ---- 证据页：13 项不变量 + 去重口径（铁律 7：两个口径分开摆）
    evidence = get_json("/api/evidence")[0].get("data", {})
    check("GET /api/evidence 13 项不变量全读出来", len(evidence.get("items") or []) == 13,
          f"items={len(evidence.get('items') or [])}")
    prov = evidence.get("provenance") or {}
    check("证据页去重口径 1,067,371 → 1,044,848 分得清",
          prov.get("去重前行数") == 1067371 and prov.get("去重后行数") == 1044848, str(prov))
    check("GET /api/rules 覆盖 R-01~R-09 共 9 条",
          len(get_json("/api/rules")[0].get("data", {}).get("table") or []) == 9)

    # ---- 非沙盒库上「复位」必须被拒（不能覆盖真库）
    reset, _ = post_json("/api/demo/reset", {})
    check("非沙盒库上「复位」被拒绝", reset.get("ok") is False, str(reset)[:200])


if __name__ == "__main__":
    raise SystemExit(main())
