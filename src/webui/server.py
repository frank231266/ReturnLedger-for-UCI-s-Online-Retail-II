"""VC-R-01 本地网页界面：单进程、单用户、无登录。

为什么这样搭
-----------
1. **只用标准库。** 项目本来没有 Web 依赖（requirements 只有 openpyxl），
   `http.server` 够了。材料包拷到任何装了 Python 3.11+ 的机器上双击就能开，
   不需要 pip install，也不会有"演示机没网"的问题。
2. **界面不重写规则。** 每个按钮都是把参数拼成 CLI 参数，调用 `src/main.py`
   的 `main()`，把它的 stdout 原样收上来显示。所以界面上那句
   "退货数量超过可退：可退 3、申请 10" 和终端里一个字都不差——
   不存在"界面一套、命令行一套"的漂移。
3. **SQL 只在 src/db.py。** 界面需要的只读聚合都落在 db.py 的「界面用的只读聚合」段。
4. **只绑 127.0.0.1。** 本机自用；没有登录，所以绝不开到 0.0.0.0。

关于数据
--------
默认操作的是 `data/build/demo/demo.db`（演示沙盒，可随时一键复位）：
有全量库 `return_system.db` 就复制一份，没有就用包内 `sample_2000.csv` 建一个。
**主库、data/raw/、证据文件一律不碰**，一键复位只删这个沙盒。
想直接操作真实库：`python src/webui/server.py --db data/build/return_system.db`。

用法
----
双击 `界面台.cmd`；或
    python src/webui/server.py                  # 默认演示沙盒 + 自动开浏览器
    python src/webui/server.py --port 8848 --no-browser
    python src/webui/server.py --db data/build/return_system.db
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import db as dbmod  # noqa: E402
import main as cli_main  # noqa: E402
import rules  # noqa: E402
import verify  # noqa: E402
from webui import view  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
BUILD = ROOT / "data" / "build"
FULL_DB = BUILD / "return_system.db"
DEMO_DIR = BUILD / "demo"
DEMO_DB = DEMO_DIR / "demo.db"
SAMPLE_CSV = BUILD / "sample_2000.csv"
ORDER_CSV = BUILD / "order_line.csv"
INVARIANTS = BUILD / "import_invariants.txt"
REJECT_STAT = BUILD / "reject_stat.txt"

DEFAULT_OPERATOR = "售后-张"

# 所有会写库的调用都走这把锁：界面是单人用的，不需要并发写，
# 加了锁就永远不会撞上 sqlite 的 "database is locked"。
_CLI_LOCK = threading.Lock()

STATE: dict[str, Any] = {"db": DEMO_DB, "mode": "切片", "note": ""}


# ---------------------------------------------------------------- CLI 桥


def rel(path: Path | str) -> str:
    """转成相对项目根的路径，界面上显示的命令才和文档里抄的一致。"""
    path = Path(path)
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def quote_arg(arg: str) -> str:
    return arg if arg and " " not in arg else f'"{arg}"'


def command_text(args: list[str]) -> str:
    """显示用的命令：把项目根的绝对路径去掉，和文档里能直接抄的命令长得一样。

    注意只有**显示**走相对路径；真正传给 CLI 的一律是绝对路径，
    否则从别的目录启动服务时 --db 会指到别处去。
    """
    prefix = str(ROOT) + os.sep
    shown = [str(a).replace(prefix, "") for a in args]
    return "python src/main.py " + " ".join(quote_arg(a) for a in shown)


def run_cli(args: list[str]) -> dict[str, Any]:
    """跑一条 CLI，把终端输出原样收上来 —— 界面与终端走的是同一条代码路径。"""
    buffer = io.StringIO()
    code = 1
    with _CLI_LOCK:
        try:
            with contextlib.redirect_stdout(buffer):
                code = int(cli_main.main([str(a) for a in args]))
        except SystemExit as exc:  # argparse 缺必填参数
            code = 1
            buffer.write(f"错误：参数不合法（{exc}）\nRESULT=E_IO\n")
        except Exception as exc:  # 兜底：界面不能因为一处异常就整页白屏
            code = 1
            buffer.write(f"错误：{exc}\n{traceback.format_exc(limit=3)}RESULT=E_IO\n")
    lines = [
        line.rstrip()
        for line in buffer.getvalue().replace("\r\n", "\n").splitlines()
        if line.strip()
    ]
    result = ""
    for line in lines:
        if line.startswith("RESULT="):
            result = line.split("=", 1)[1].strip()
    return {
        "exitCode": code,
        "result": result,
        "lines": lines,
        "command": command_text(args),
        # 退出码含义取自 src/main.py 顶部注释，界面据此上色
        "verdict": {
            0: "通过",
            1: "系统/格式错误",
            2: "校验拒绝",
            3: "转人工核验",
            4: "不变量与基线不符（整体回滚）",
        }.get(code, "未知"),
    }


def cli_args(command: str, *extra: str) -> list[str]:
    """拼一条指向当前库的 CLI 参数（--db 由服务端统一注入，界面不用操心）。

    传绝对路径：服务的当前目录不是"契约"的一部分，用相对路径等于把
    "必须从项目根启动"这条隐性前提塞进参数里，迟早出错。
    """
    return [command, "--db", str(STATE["db"]), *extra]


# ---------------------------------------------------------------- 库连接


@contextlib.contextmanager
def open_db(*, schema: bool = True):
    conn = dbmod.connect(STATE["db"])
    try:
        if schema:
            dbmod.init_schema(conn)
        yield conn
    finally:
        conn.close()


def db_ready() -> bool:
    return Path(STATE["db"]).exists()


# ---------------------------------------------------------------- 数据源


def build_demo_db() -> dict[str, Any]:
    """重置演示沙盒：有全量库就复制，没有就用包内 2,000 行切片建库。

    只动 data/build/demo/ —— 主库、data/raw/、证据文件一个字节都不碰。
    """
    if Path(STATE["db"]) != DEMO_DB:
        return {
            "ok": False,
            "error": (
                f"当前库是 {rel(STATE['db'])}，不是演示沙盒，拒绝重置。"
                "要重置请重启服务且不带 --db，或按 tools/demo.py 的做法另建一个库。"
            ),
        }
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        stale = DEMO_DB.with_name(DEMO_DB.name + suffix)
        if stale.exists():
            stale.unlink()

    if FULL_DB.exists():
        shutil.copy2(FULL_DB, DEMO_DB)
        STATE["mode"] = "全量"
        STATE["note"] = "由全量库 return_system.db 复制而来"
        return {
            "ok": True,
            "mode": STATE["mode"],
            "note": STATE["note"],
            "cli": None,
        }

    if not SAMPLE_CSV.exists():
        return {
            "ok": False,
            "error": "既没有 data/build/return_system.db，也没有 data/build/sample_2000.csv，无法建演示库",
        }
    out = run_cli(
        [
            "import",
            "--csv",
            str(SAMPLE_CSV),
            "--db",
            str(DEMO_DB),
            "--out",
            str(DEMO_DIR),
            "--baseline",
            "report",
        ]
    )
    STATE["mode"] = "切片"
    STATE["note"] = "包内 2,000 行真实切片（切片不与全量基线比较，这是 --baseline report 的用途）"
    return {"ok": out["exitCode"] == 0, "mode": STATE["mode"], "note": STATE["note"], "cli": out}


def ensure_db(explicit: str | None) -> None:
    """确定本次要操作哪个库；默认走演示沙盒并保证它存在。"""
    if explicit:
        STATE["db"] = Path(explicit)
        STATE["mode"] = "自定义"
        STATE["note"] = "由命令行 --db 指定"
        if not Path(STATE["db"]).exists():
            print(f"[警告] {rel(STATE['db'])} 不存在，界面上先做一次导入再操作。")
        return
    if FULL_DB.exists():
        STATE["db"] = DEMO_DB
        STATE["mode"] = "全量"
        STATE["note"] = "由全量库 return_system.db 复制而来（演示沙盒，改坏了可一键复位）"
        if not DEMO_DB.exists():
            result = build_demo_db()
            if not result.get("ok"):
                print(f"[警告] 演示库建不起来：{result.get('error')}")
        return
    STATE["db"] = DEMO_DB
    STATE["mode"] = "切片"
    STATE["note"] = "包内 2,000 行真实切片"
    if not DEMO_DB.exists():
        print("没有全量库，用包内 2,000 行切片建演示库 ……")
        result = build_demo_db()
        out = result.get("cli")
        if out:
            print(f"  → {out['result']}（exit {out['exitCode']}）")


# ---------------------------------------------------------------- 证据


def parse_evidence() -> dict[str, Any]:
    """读 data/build/import_invariants.txt（由 main.py import 写出，未手改）。"""
    if not INVARIANTS.exists():
        return {"found": False}
    text = INVARIANTS.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    def pick(prefix: str) -> str:
        for line in lines:
            if line.strip().startswith(prefix):
                return line.strip()[len(prefix):].strip()
        return ""

    items: list[dict[str, Any]] = []
    for line in lines:
        match = re.match(r"^\s{2}([a-z_]+)\s+(\d+)\s*(✓|✗)?\s*$", line)
        if match:
            items.append(
                {
                    "name": match.group(1),
                    "value": int(match.group(2)),
                    "match": match.group(3) == "✓" if match.group(3) else None,
                    "expected": verify.BASELINE.get(match.group(1)),
                }
            )
    return {
        "found": True,
        "source": pick("数据源"),
        "target": pick("目标库"),
        "importedAt": pick("导入时间"),
        "rows": pick("读取行数"),
        "slice": pick("切片模式"),
        "ticks": text.count("✓"),
        "crosses": text.count("✗"),
        "items": items,
        "provenance": verify.provenance(),
        "provenanceOk": not verify.check_provenance(),
        "rejectStat": REJECT_STAT.read_text(encoding="utf-8", errors="replace")
        if REJECT_STAT.exists()
        else "",
    }


# ---------------------------------------------------------------- 只读接口


def api_meta() -> dict[str, Any]:
    return {
        "mode": STATE["mode"],
        "note": STATE["note"],
        "db": rel(STATE["db"]),
        "dbExists": db_ready(),
        "dbBytes": Path(STATE["db"]).stat().st_size if db_ready() else 0,
        "hasFull": FULL_DB.exists(),
        "hasSample": SAMPLE_CSV.exists(),
        "hasOrderCsv": ORDER_CSV.exists(),
        "operator": DEFAULT_OPERATOR,
        # 显示用：界面不传 --now，时间一律由 CLI 取真实当前时间，
        # 免得"演示时钟"与真实时间混用把 R-09 的 7 天惰性释放算歪。
        "serverTime": dt.datetime.now().isoformat(timespec="seconds"),
        "statuses": [
            view.status_meta(status) for status in rules.STATUS_LABELS
        ],
        "transitions": rules.TRANSITIONS,
        "provenance": verify.provenance(),
        "baseline": verify.BASELINE,
        "disclaimer": "本界面只操作演示沙盒 data/build/demo/demo.db；主库、raw 与证据文件不动。",
    }


def api_overview() -> dict[str, Any]:
    if not db_ready():
        return {"empty": True, "hasSample": SAMPLE_CSV.exists(), "hasFull": FULL_DB.exists()}
    with open_db() as conn:
        numbers = dbmod.overview(conn)
        by_status = dbmod.requests_by_status(conn)
        recent = dbmod.list_requests(conn, limit=8)
        recent_log = dbmod.recent_log(conn, limit=10)
        rejects = dbmod.get_reject_stats(conn)
    for row in recent:
        meta = view.status_meta(str(row["status"]))
        row["statusLabel"] = meta["label"]
        row["tone"] = meta["tone"]
    return {
        "empty": numbers["lines"] == 0,
        "numbers": numbers,
        "byStatus": by_status,
        "statuses": [view.status_meta(s) for s in rules.STATUS_LABELS],
        "recent": recent,
        "recentLog": recent_log,
        "rejects": rejects,
    }


def api_invoices(query: str) -> dict[str, Any]:
    if not db_ready():
        return {"found": False, "items": [], "suggestions": []}
    with open_db() as conn:
        items = dbmod.search_invoices(conn, rules.normalize_invoice(query), limit=20)
    return {"found": bool(items), "items": items, "suggestions": []}


def api_invoice(invoice: str) -> dict[str, Any]:
    number = rules.normalize_invoice(invoice)
    if not db_ready():
        return {"found": False, "invoice": number, "lines": [], "suggestions": []}
    with open_db() as conn:
        lines = dbmod.get_lines_by_invoice(conn, number)
        states = dbmod.line_states(conn, [line.line_key for line in lines])
        payload = view.invoice_view(lines, states)
        if payload["found"]:
            payload["header"]["invoice"] = number
        else:
            payload["suggestions"] = dbmod.search_invoices(conn, number[:4], limit=8)
    payload["invoice"] = number
    return payload


def api_requests(status: str | None, limit: int = 200) -> dict[str, Any]:
    if not db_ready():
        return {"items": [], "byStatus": {}}
    with open_db() as conn:
        items = dbmod.list_requests(conn, status=status or None, limit=limit)
        by_status = dbmod.requests_by_status(conn)
    for row in items:
        meta = view.status_meta(str(row["status"]))
        row["statusLabel"] = meta["label"]
        row["tone"] = meta["tone"]
    return {
        "items": items,
        "byStatus": by_status,
        "statuses": [view.status_meta(s) for s in rules.STATUS_LABELS],
    }


def api_request(req_no: str) -> dict[str, Any]:
    with open_db() as conn:
        detail = dbmod.request_detail(conn, req_no)
    if detail is None:
        return {"found": False, "reqNo": req_no}
    detail["found"] = True
    meta = view.status_meta(str(detail["status"]))
    detail["statusLabel"] = meta["label"]
    detail["tone"] = meta["tone"]
    detail["targets"] = view.transition_targets(str(detail["status"]))
    if detail.get("line"):
        detail["lineState"] = view.line_state(
            detail["line"],
            {"active_qty": detail["active_qty"], "open": detail["open_request"]},
        )
    return detail


def api_line(invoice: str, seq: str) -> dict[str, Any]:
    number = rules.normalize_invoice(invoice)
    with open_db() as conn:
        rows = dbmod.get_lines_by_invoice_seq(conn, number, int(seq))
        if not rows:
            return {"found": False}
        line = rows[0]
        states = dbmod.line_states(conn, [line.line_key])
    return {"found": True, "line": view.line_view(line, states.get(line.line_key))}


def api_log(limit: int = 80) -> dict[str, Any]:
    if not db_ready():
        return {"items": []}
    with open_db() as conn:
        items = dbmod.recent_log(conn, limit=limit)
    for row in items:
        for key in ("from_status", "to_status"):
            if row.get(key):
                row[key + "Label"] = rules.STATUS_LABELS.get(str(row[key]), str(row[key]))
    return {"items": items}


def api_rules() -> dict[str, Any]:
    """规则说明页的正文。代码落点写的是真实文件名，方便当场翻给老师看。"""
    table = [
        ("R-01", "定位一条可退明细", "sheet|invoice|seq 全局唯一（导入时实测 1,044,848 唯一键，0 冲突）",
         "src/prepare.py 生成 / src/verify.py 断言 unique_keys"),
        ("R-02", "两表合并", "同一 Invoice 只保留首次出现那张表的行；两表行数一致性断言",
         "src/prepare.py / src/verify.py"),
        ("R-03", "判定取消", "Invoice 以 C 开头 → 取消；A 开头 → 坏账调整；禁止用 Quantity<0 判定",
         "src/rules.py recheck_flags"),
        ("R-04", "算可退数量", "可退 = returnable_qty − history_returned − active_qty，不足按 0；历史占用按 (cust_id, stock_code) FIFO 分摊",
         "src/rules.py compute_retirable / src/history.py"),
        ("R-05", "是否可退商品", "编码不匹配 ^\\d{5}[A-Za-z]{0,2}$ → 非商品（运费/费用/调整）不可退",
         "src/rules.py check_retirable_line"),
        ("R-06", "同明细只许一条生效申请", "APPLIED/ACCEPTED/RECEIVED 同时只允许一条 → 否则 E_DUPLICATE；同 request_id 幂等重放",
         "src/rules.py check_duplicate / src/db.py get_active_request"),
        ("R-07", "数量校验", "qty ≤ 0 → E_INVALID_QTY；qty > 可退 → E_EXCEED",
         "src/rules.py check_qty"),
        ("R-08", "无法判定转人工", "缺 Customer ID、开票时间晚于申请时间 → PENDING_MANUAL，不报错、不占额度",
         "src/rules.py check_anomaly"),
        ("R-09", "状态与额度", "APPLIED → ACCEPTED → RECEIVED → CLOSED，或人工 REJECTED；合法边写死在 TRANSITIONS 表；超 7 天惰性释放",
         "src/rules.py TRANSITIONS / src/db.py update_request_status"),
    ]
    codes = [
        ("E_NOT_FOUND", "没找到明细", "核对发票号与发票内序号"),
        ("E_NON_RETURNABLE", "该行本身不可退", "取消行 / 坏账调整 / 非商品行 / 无可退数量"),
        ("E_DUPLICATE", "已有生效申请", "先走完结案或拒绝那条，才能再申请"),
        ("E_INVALID_QTY", "数量不是正整数", "申请数量必须 > 0"),
        ("E_EXCEED", "超过可退数量", "提示里会写「可退 X、申请 Y」"),
        ("E_MANUAL", "转人工核验", "缺客户号 / 开票时间晚于申请时间 → PENDING_MANUAL，不占额度"),
        ("E_BAD_TRANSITION", "状态流转不合法", "跳级、回头、终态复活一律拒绝，并告诉你下一步只能是哪几个"),
        ("OK", "通过", "落库并写状态流水"),
    ]
    return {
        "table": [
            {"no": no, "title": title, "logic": logic, "where": where}
            for no, title, logic, where in table
        ],
        "codes": [{"code": c, "meaning": m, "hint": h} for c, m, h in codes],
        "order": ["E_NOT_FOUND", "E_NON_RETURNABLE", "E_DUPLICATE", "E_INVALID_QTY",
                  "E_EXCEED", "E_MANUAL", "OK"],
        "transitions": rules.TRANSITIONS,
        "labels": rules.STATUS_LABELS,
    }


def api_evidence() -> dict[str, Any]:
    return parse_evidence()


def api_demo_plan() -> dict[str, Any]:
    if not db_ready():
        return {"found": False, "reason": "演示库还没建好"}
    with open_db() as conn:
        line = dbmod.pick_demo_line(conn)
        if line is None:
            return {"found": False, "reason": "库里没有可演示的明细（可退数量都 ≤ 2），先导入数据"}
        plan = view.demo_plan(line)
        plan["nextReqNo"] = dbmod.next_req_no(conn)
        states = dbmod.line_states(conn, [str(line["line_key"])])
    plan["found"] = True
    plan["lineState"] = view.line_state(line, states.get(str(line["line_key"])))
    plan["mode"] = STATE["mode"]
    return plan


# ---------------------------------------------------------------- 写接口（一律转 CLI）


def api_apply(body: dict[str, Any]) -> dict[str, Any]:
    invoice = rules.normalize_invoice(body.get("invoice"))
    seq = body.get("seq")
    qty = body.get("qty")
    if not invoice or seq in (None, "") or qty in (None, ""):
        return {"ok": False, "error": "发票号、序号、申请数量都必须填写"}
    extra = [
        "--invoice", invoice,
        "--seq", str(int(seq)),
        "--qty", str(int(qty)),
        "--reason", str(body.get("reason") or ""),
        "--operator", str(body.get("operator") or DEFAULT_OPERATOR),
    ]
    if body.get("now"):
        extra += ["--now", str(body["now"])]
    out = run_cli(cli_args("apply", *extra))
    payload: dict[str, Any] = {"ok": out["exitCode"] == 0, "cli": out}
    # E_MANUAL（exit 3）不是"被拒"：申请已经落库成 PENDING_MANUAL，只是要人去看一眼。
    # 把"转人工"和申请号一起交给前端，页面才不会把这条最该演示的规则画成"失败"。
    payload["manual"] = out["exitCode"] == 3
    payload["reqNo"] = ""
    for line in out["lines"]:
        match = re.search(r"申请号 (R-\d+)", line)
        if match:
            payload["reqNo"] = match.group(1)
            break
    payload["invoice"] = api_invoice(invoice)
    if payload["reqNo"]:
        payload["request"] = api_request(payload["reqNo"])
    return payload


def api_transition(body: dict[str, Any]) -> dict[str, Any]:
    req_no = str(body.get("req") or "").strip()
    command = str(body.get("command") or "").strip()
    if not req_no:
        return {"ok": False, "error": "缺少申请号"}
    if command not in ("accept", "receive", "close", "reject"):
        return {"ok": False, "error": f"未知动作 {command}（只能是 accept/receive/close/reject）"}
    extra = [
        "--req", req_no,
        "--operator", str(body.get("operator") or DEFAULT_OPERATOR),
    ]
    note = str(body.get("note") or "").strip()
    if note:
        extra += ["--note", note]
    if body.get("now"):
        extra += ["--now", str(body["now"])]
    out = run_cli(cli_args(command, *extra))
    return {
        "ok": out["exitCode"] == 0,
        "cli": out,
        "request": api_request(req_no),
    }


def api_import(body: dict[str, Any]) -> dict[str, Any]:
    source = str(body.get("source") or "sample")
    csv_path = ORDER_CSV if source == "csv" else SAMPLE_CSV
    if not csv_path.exists():
        return {
            "ok": False,
            "error": f"找不到 {rel(csv_path)}（全量 CSV 按规范不进提交包，可先在项目根跑 python src/prepare.py --no-db）",
        }
    extra = [
        "--csv", str(csv_path),
        "--out", str(Path(STATE["db"]).parent),
        # 切片不与全量基线比较；全量 CSV 才用 strict
        "--baseline", "strict" if source == "csv" else "report",
    ]
    if body.get("rebuild"):
        extra.append("--rebuild")
    out = run_cli(cli_args("import", *extra))
    return {"ok": out["exitCode"] == 0, "cli": out, "meta": api_meta()}


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "VC-R-01-UI/1.0"
    protocol_version = "HTTP/1.1"

    # 控制台提示保持安静：界面自己会把 CLI 原话显示出来
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[ui] {self.address_string()} {fmt % args}\n")

    # ------------------------------------------------------------ 响应

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(404, "Not Found")
            return
        body = path.read_bytes()
        guessed = mimetypes.guess_type(path.name)[0]
        if guessed is None:
            guessed = "application/octet-stream"
        if guessed.startswith("text/") or guessed in ("application/javascript", "application/json"):
            guessed += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", guessed)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: int = 500) -> None:
        self.send_json({"ok": False, "error": message}, status=status)

    # ------------------------------------------------------------ 路由

    def do_GET(self) -> None:  # noqa: N802（BaseHTTPRequestHandler 的约定）
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        try:
            if path.startswith("/api/"):
                self.send_json(self.route_get(path, query))
                return
            self.serve_static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # 任何未预期异常都不能让页面白屏
            self.send_error_json(f"{exc}\n{traceback.format_exc(limit=4)}")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_error_json(f"请求体解析失败：{exc}", status=400)
            return
        try:
            self.send_json(self.route_post(path, body))
        except BrokenPipeError:
            pass
        except Exception as exc:
            self.send_error_json(f"{exc}\n{traceback.format_exc(limit=4)}")

    def serve_static(self, path: str) -> None:
        prefix = "/static/"
        if path.startswith(prefix):
            relative = path[len(prefix):]
        else:
            relative = path.lstrip("/") or "index.html"
        target = (STATIC / relative).resolve()
        base = STATIC.resolve()
        if base != target and base not in target.parents:
            self.send_error(403, "Forbidden")
            return
        self.send_file(target)

    def route_get(self, path: str, query: dict[str, str]) -> dict[str, Any]:
        if path == "/api/meta":
            return {"ok": True, "data": api_meta()}
        if path == "/api/overview":
            return {"ok": True, "data": api_overview()}
        if path == "/api/invoices":
            return {"ok": True, "data": api_invoices(query.get("q", ""))}
        if path == "/api/invoice":
            return {"ok": True, "data": api_invoice(query.get("q", query.get("invoice", "")))}
        if path == "/api/requests":
            return {"ok": True, "data": api_requests(query.get("status") or None)}
        if path == "/api/request":
            return {"ok": True, "data": api_request(query.get("req", ""))}
        if path == "/api/line":
            return {"ok": True, "data": api_line(query.get("invoice", ""), query.get("seq", "0"))}
        if path == "/api/log":
            return {"ok": True, "data": api_log()}
        if path == "/api/rules":
            return {"ok": True, "data": api_rules()}
        if path == "/api/evidence":
            return {"ok": True, "data": api_evidence()}
        if path == "/api/demo/plan":
            return {"ok": True, "data": api_demo_plan()}
        if path == "/api/ping":
            return {"ok": True, "data": {"pong": True, "db": rel(STATE["db"])}}
        raise KeyError(f"未知接口 {path}")

    def route_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path == "/api/apply":
            return api_apply(body)
        if path == "/api/transition":
            return api_transition(body)
        if path == "/api/import":
            return api_import(body)
        if path == "/api/demo/reset":
            result = build_demo_db()
            result["meta"] = api_meta()
            return result
        raise KeyError(f"未知接口 {path}")


# ---------------------------------------------------------------- 启动


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="server.py", description="VC-R-01 本地网页界面（单人、无登录）"
    )
    parser.add_argument("--host", default="127.0.0.1", help="默认只听本机")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--db", default=None, help="指定库；默认演示沙盒 data/build/demo/demo.db")
    parser.add_argument("--no-browser", action="store_true", help="不自动开浏览器")
    parser.add_argument("--no-build", action="store_true", help="不自动建演示库")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    if args.no_build:
        STATE["db"] = Path(args.db) if args.db else DEMO_DB
        STATE["mode"] = "自定义"
        STATE["note"] = "由命令行指定（--no-build）"
    else:
        ensure_db(args.db)

    port = args.port
    httpd: ThreadingHTTPServer | None = None
    for candidate in range(port, port + 21):
        try:
            httpd = ThreadingHTTPServer((args.host, candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if httpd is None:
        print(f"端口 {args.port}~{args.port + 20} 全被占用，请用 --port 换一个")
        return 1

    url = f"http://{args.host}:{port}/"
    print("=" * 64)
    print("  VC-R-01 订单与退货管理 · 本地界面")
    print("=" * 64)
    print(f"  地址     {url}")
    print(f"  数据源   {rel(STATE['db'])}（{STATE['mode']}）")
    print(f"  说明     {STATE['note']}")
    print("  规则     一律经 src/main.py 的 CLI 执行，界面不另写一套裁决")
    print("  停服     Ctrl+C")
    print("=" * 64)
    # 输出被重定向到文件时（python src/webui/server.py > log.txt）Python 会整块缓冲，
    # banner 要等到进程结束才落盘 —— 看上去就像"启动了但什么都没说"。
    # tools/demo.py 为同一个坑加过 flush=True，这里同样补一次。
    sys.stdout.flush()
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
