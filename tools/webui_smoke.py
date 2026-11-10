"""界面冒烟检查：起一个真 HTTP 服务，把每个接口都打一遍。

为什么不放进 tests/
------------------
`tests/` 那 120 条是"规则总账"，文档、验证台、讲稿都按这个数对账。
界面是展示层，用独立脚本验证更合适：它跑的是**真 HTTP**（起服务、发请求、看 JSON），
比单元测试更接近"用户在里面点按钮"。

验证的是一个闭环：
    查明细 → 退超量被拒(E_EXCEED) → 正常申请 → 重复申请被拒(E_DUPLICATE)
    → 跳级结案被拒(E_BAD_TRANSITION) → 受理 → 收货 → 结案 → 额度回到起点
另有一条支线：缺客户号的明细 → 按钮可点 → 提交后 E_MANUAL(exit 3) → 转人工不占额度（R-08）。

用的是 _scratch/ 下的临时库，不碰 data/build/demo/，也不碰证据文件。

用法
----
    python tools/webui_smoke.py
退出码 0 表示全过。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import webui.server as server  # noqa: E402

SCRATCH = ROOT / "_scratch" / "webui"
SAMPLE = ROOT / "data" / "build" / "sample_2000.csv"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok and detail:
        print(f"       └─ {detail}")
    return bool(ok)


# ---------------------------------------------------------------- HTTP 小工具


def get(path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(BASE + path, timeout=120) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def get_json(path: str) -> dict:
    status, text = get(path)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"HTTP {status} 非 JSON：{text[:200]}"}


def post_json(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"HTTP {exc.code}：{body[:200]}"}


# ---------------------------------------------------------------- 准备


def prepare_db() -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    db_path = SCRATCH / "smoke.db"
    for suffix in ("", "-wal", "-shm"):
        stale = db_path.with_name(db_path.name + suffix)
        if stale.exists():
            stale.unlink()
    if not SAMPLE.exists():
        print(f"缺少 {SAMPLE}，无法建冒烟库")
        raise SystemExit(1)
    out = server.run_cli(
        [
            "import", "--csv", str(SAMPLE), "--db", str(db_path),
            "--out", str(SCRATCH), "--baseline", "report",
        ]
    )
    if out["exitCode"] != 0:
        print("建冒烟库失败：")
        print("\n".join(out["lines"][-10:]))
        raise SystemExit(1)
    print(f"冒烟库已建好：{db_path.relative_to(ROOT)}")
    return db_path


# ---------------------------------------------------------------- 主流程


def main() -> int:
    global BASE

    db_path = prepare_db()
    server.STATE["db"] = db_path
    server.STATE["mode"] = "切片"
    server.STATE["note"] = "冒烟测试临时库（tools/webui_smoke.py）"
    server.Handler.log_message = lambda *args, **kwargs: None  # 别刷屏

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    BASE = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"服务已起：{BASE}\n")

    try:
        run_checks()
    finally:
        httpd.shutdown()
        httpd.server_close()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 58}")
    print(f"  {passed}/{total} 项通过")
    print(f"{'=' * 58}")
    if passed == total:
        shutil.rmtree(SCRATCH, ignore_errors=True)
        return 0
    print(f"失败项留在 {SCRATCH.relative_to(ROOT)} 里，方便查")
    return 1


def run_checks() -> None:  # noqa: C901（顺序就是剧本，不拆）
    # ---- 静态页
    status, html = get("/")
    check("首页能打开（GET /）", status == 200 and "VC-R-01" in html, f"HTTP {status}")
    status, css = get("/static/app.css")
    check("样式表能取到（GET /static/app.css）", status == 200 and "--accent" in css, f"HTTP {status}")
    status, js = get("/static/app.js")
    check(
        "脚本能取到，且不引用外部 CDN（断网也能用）",
        status == 200 and "VC-R-01" in js and "http://" not in js and "https://" not in js,
        f"HTTP {status}",
    )
    check(
        "首页引用的两个静态文件都对得上",
        "/static/app.css" in html and "/static/app.js" in html,
        "index.html 里的引用与实际文件不一致",
    )
    status, _ = get("/static/../server.py")
    check("静态目录不可穿越（/static/../server.py 被挡）", status in (403, 404), f"HTTP {status}")

    # ---- 只读接口
    meta = get_json("/api/meta")
    check("GET /api/meta", meta.get("ok") and meta["data"]["dbExists"])
    transitions = meta["data"]["transitions"]
    check(
        "状态机来自 rules.TRANSITIONS（不在前端硬编码）",
        transitions["CLOSED"] == [] and "RECEIVED" in transitions["ACCEPTED"],
        str(transitions),
    )

    overview = get_json("/api/overview")["data"]
    lines = overview["numbers"]["lines"]
    check("GET /api/overview 明细行数 = 2,000", lines == 2000, f"实际 {lines}")

    search = get_json("/api/invoices?q=489")["data"]
    check("GET /api/invoices?q=489 有结果", len(search["items"]) > 0)

    plan = get_json("/api/demo/plan")["data"]
    check("GET /api/demo/plan 挑到可演示明细", plan.get("found"), str(plan)[:200])

    invoice_no = plan["invoice"]
    seq = plan["seq"]
    invoice = get_json(f"/api/invoice?q={invoice_no}")["data"]
    check(f"GET /api/invoice?q={invoice_no}", invoice.get("found") and invoice["totals"]["lines"] > 0)
    first = next(row for row in invoice["lines"] if row["seq"] == seq)
    retirable = first["retirable"]
    check(
        "展示值与公式一致（原始 − 历史 − 占用）",
        retirable == first["returnable_qty"] - first["history_returned"] - first["active_qty"],
        f"{first}",
    )

    # ---- 退超量：应当被规则拒（界面不能悄悄放行）
    over = post_json("/api/apply", {
        "invoice": invoice_no, "seq": seq, "qty": retirable + 5, "operator": "冒烟",
    })
    check(
        "申请超过可退 → E_EXCEED / exit 2",
        over.get("cli", {}).get("result") == "E_EXCEED" and over["cli"]["exitCode"] == 2,
        str(over.get("cli", {}).get("lines"))[:200],
    )

    qty = min(3, max(1, retirable))
    applied = post_json("/api/apply", {
        "invoice": invoice_no, "seq": seq, "qty": qty, "operator": "冒烟", "reason": "冒烟测试",
    })
    text = "\n".join(applied.get("cli", {}).get("lines", []))
    req_no = (re.search(r"申请号 (R-\d+)", text) or [None, ""])[1]
    check(f"正常申请 → OK 且拿到申请号（{req_no}）", applied.get("ok") and bool(req_no), text[:200])

    line_after_apply = get_json(f"/api/line?invoice={invoice_no}&seq={seq}")["data"]["line"]
    check(
        f"申请后剩余可退 = {retirable} − {qty} = {retirable - qty}",
        line_after_apply["retirable"] == retirable - qty,
        f"实际 {line_after_apply['retirable']}",
    )

    again = post_json("/api/apply", {
        "invoice": invoice_no, "seq": seq, "qty": 1, "operator": "冒烟",
    })
    check(
        "同明细再申请 → E_DUPLICATE / exit 2",
        again.get("cli", {}).get("result") == "E_DUPLICATE" and again["cli"]["exitCode"] == 2,
        str(again.get("cli", {}).get("lines"))[:200],
    )

    zero = post_json("/api/apply", {"invoice": invoice_no, "seq": seq, "qty": 0, "operator": "冒烟"})
    # 校验顺序写死成 E_DUPLICATE 在 E_INVALID_QTY 前面，所以这一条先撞 DUPLICATE。
    # 这不是 bug，是 rules.decide 的既定顺序 —— 顺手在这里把它钉住。
    check(
        "校验顺序：已有生效申请时，先报 E_DUPLICATE 而不是 E_INVALID_QTY",
        zero.get("cli", {}).get("result") == "E_DUPLICATE",
        str(zero.get("cli", {}).get("lines"))[:200],
    )

    fresh = get_json(f"/api/invoice?q={invoice_no}")["data"]
    other = next(
        (row for row in fresh["lines"] if row["can_apply"] and row["seq"] != seq), None
    )
    if other is not None:
        invalid = post_json("/api/apply", {
            "invoice": invoice_no, "seq": other["seq"], "qty": 0, "operator": "冒烟",
        })
        check(
            "换一条没有生效申请的明细，数量 0 → E_INVALID_QTY",
            invalid.get("cli", {}).get("result") == "E_INVALID_QTY",
            str(invalid.get("cli", {}).get("lines"))[:200],
        )
    else:
        check("换一条没有生效申请的明细，数量 0 → E_INVALID_QTY", False, "这张发票里没有第二条可申请明细")

    # ---- R-08：缺客户号的明细必须**能从界面提交**（转人工，不是"被拒"、也不是点不动）
    with server.open_db() as conn:
        guess = conn.execute(
            "SELECT invoice, seq FROM order_line"
            " WHERE cust_id IS NULL AND is_cancel = 0 AND is_non_product = 0"
            "   AND is_bad_debt = 0 AND returnable_qty > 0"
            "   AND NOT EXISTS (SELECT 1 FROM return_request r"
            "     WHERE r.line_key = order_line.line_key"
            "       AND r.status IN ('APPLIED','ACCEPTED','RECEIVED','PENDING_MANUAL'))"
            " LIMIT 1"
        ).fetchone()
    if guess is None:
        check("缺客户号的明细可申请（R-08 转人工）", False, "切片里没有「缺客户号且未结案」的明细")
    else:
        no_cust = get_json(f"/api/invoice?q={guess['invoice']}")["data"]
        target = next(row for row in no_cust["lines"] if row["seq"] == guess["seq"])
        check(
            "缺客户号的明细展示为「可申请（缺客户号→人工）」",
            target["code"] == "MANUAL" and target["needs_manual"] is True,
            str({k: target.get(k) for k in ("code", "text", "can_apply", "needs_manual")}),
        )
        check(
            "缺客户号的行按钮可点（否则 R-08 从界面走不通）",
            target["can_apply"] is True,
            f"can_apply={target['can_apply']}",
        )
        manual = post_json("/api/apply", {
            "invoice": guess["invoice"], "seq": guess["seq"], "qty": 1, "operator": "冒烟",
        })
        check(
            "缺客户号提交 → E_MANUAL / exit 3，且带回申请号",
            manual.get("cli", {}).get("result") == "E_MANUAL"
            and manual["cli"]["exitCode"] == 3
            and manual.get("manual") is True
            and bool(manual.get("reqNo")),
            str(manual.get("cli", {}).get("lines"))[:200],
        )
        pending = get_json(f"/api/request?req={manual.get('reqNo') or ''}")["data"]
        check(
            "转人工的单子落在 PENDING_MANUAL 且不占额度",
            pending.get("status") == "PENDING_MANUAL"
            and (pending.get("lineState") or {}).get("retirable") == target["retirable"],
            str({k: pending.get(k) for k in ("status", "reqNo")}),
        )

    # ---- 状态机：跳级必须被拒
    jump = post_json("/api/transition", {"req": req_no, "command": "close", "operator": "冒烟"})
    check(
        "APPLIED 直接结案 → E_BAD_TRANSITION / exit 2",
        jump.get("cli", {}).get("result") == "E_BAD_TRANSITION" and jump["cli"]["exitCode"] == 2,
        str(jump.get("cli", {}).get("lines"))[:200],
    )

    accept = post_json("/api/transition", {"req": req_no, "command": "accept", "operator": "冒烟"})
    check(
        "受理 → ACCEPTED",
        accept.get("ok") and accept.get("request", {}).get("status") == "ACCEPTED",
        str(accept.get("cli", {}).get("lines"))[:200],
    )

    detail = get_json(f"/api/request?req={req_no}")["data"]
    check("申请详情带状态流水与合法下一步", detail.get("found") and len(detail.get("log", [])) >= 2,
          f"log={len(detail.get('log', []))}")
    legal = [t["to"] for t in detail["targets"] if t["legal"]]
    check("ACCEPTED 的合法下一步只有 RECEIVED", legal == ["RECEIVED"], str(legal))

    receive = post_json("/api/transition", {"req": req_no, "command": "receive", "operator": "冒烟"})
    check("收货 → RECEIVED", receive.get("ok") and receive["request"]["status"] == "RECEIVED",
          str(receive.get("cli", {}).get("lines"))[:200])

    line_received = get_json(f"/api/line?invoice={invoice_no}&seq={seq}")["data"]["line"]
    check("RECEIVED 期间额度仍被占着", line_received["retirable"] == retirable - qty,
          f"实际 {line_received['retirable']}")

    close = post_json("/api/transition", {"req": req_no, "command": "close", "operator": "冒烟"})
    check("结案 → CLOSED", close.get("ok") and close["request"]["status"] == "CLOSED",
          str(close.get("cli", {}).get("lines"))[:200])

    line_closed = get_json(f"/api/line?invoice={invoice_no}&seq={seq}")["data"]["line"]
    check(
        f"结案后额度闭环回到 {retirable}",
        line_closed["retirable"] == retirable,
        f"实际 {line_closed['retirable']}",
    )

    # ---- 再申请一次：结案释放后允许
    reapply = post_json("/api/apply", {
        "invoice": invoice_no, "seq": seq, "qty": 1, "operator": "冒烟", "reason": "结案后再申请",
    })
    check("结案后同明细可以再申请（R-06 例外）", reapply.get("ok"),
          str(reapply.get("cli", {}).get("lines"))[:200])

    # ---- 列表与说明
    requests = get_json("/api/requests")["data"]
    check("GET /api/requests 有申请且带状态文案",
          len(requests["items"]) >= 2 and requests["items"][0].get("statusLabel"))

    logs = get_json("/api/log")["data"]
    check("GET /api/log 有流水", len(logs["items"]) > 0)

    rules_payload = get_json("/api/rules")["data"]
    check("GET /api/rules 覆盖 R-01~R-09 共 9 条", len(rules_payload["table"]) == 9,
          f"实际 {len(rules_payload['table'])} 条")

    evidence = get_json("/api/evidence")["data"]
    check("GET /api/evidence 读到程序生成的 13 项不变量",
          evidence.get("found") and len(evidence.get("items", [])) == 13,
          f"items={len(evidence.get('items', []))}")

    # ---- 未知接口要明确报错，不能静默 200
    status, body = get("/api/nope")
    check("未知接口返回错误而不是空 200", status >= 400 and "error" in body, f"HTTP {status}")


if __name__ == "__main__":
    BASE = ""
    raise SystemExit(main())
