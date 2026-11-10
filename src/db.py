"""所有 SQL 只在这个文件里。

三张表
------
order_line     可退明细（= order_line.csv 原样落地 + history_returned）
return_request 退货申请（幂等键 request_id，业务号 req_no）
status_log     状态流水（含 AUTO_RELEASED）

为什么另建 return_system.db 而不复用 order_line.db
-----------------------------------------------
prepare.py 每次重建都会删掉 db。如果申请表和明细表放在同一个库，
演示前一晚顺手重跑一次 prepare.py，课堂上的申请记录就全没了。
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple

from csv_loader import LineRaw

SCHEMA_VERSION = "1"
# 占额度：这三态都算"额度已被这条申请拿走"
ACTIVE_STATUSES: tuple[str, ...] = ("APPLIED", "ACCEPTED", "RECEIVED")
# 未结案：展示用，把待人工核验也算进来。
# 否则查询页会对一条已经躺在异常队列里的明细说"可申请"，跟 R-08 自相矛盾。
OPEN_STATUSES: tuple[str, ...] = (*ACTIVE_STATUSES, "PENDING_MANUAL")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS order_line (
    line_key          TEXT PRIMARY KEY,
    sheet             TEXT NOT NULL,
    invoice           TEXT NOT NULL,
    stock_code        TEXT NOT NULL,
    description       TEXT,
    quantity          INTEGER NOT NULL,
    invoice_date      TEXT NOT NULL,
    price             REAL NOT NULL,
    cust_id           TEXT,
    country           TEXT,
    seq               INTEGER NOT NULL,
    is_cancel         INTEGER NOT NULL DEFAULT 0,
    is_bad_debt       INTEGER NOT NULL DEFAULT 0,
    is_non_product    INTEGER NOT NULL DEFAULT 0,
    returnable_qty    INTEGER NOT NULL DEFAULT 0,
    history_returned  INTEGER NOT NULL DEFAULT 0,
    imported_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS return_request (
    request_id   TEXT PRIMARY KEY,
    req_no       TEXT NOT NULL UNIQUE,
    line_key     TEXT NOT NULL,
    qty          INTEGER NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,
    operator     TEXT NOT NULL DEFAULT '',
    anomaly_kind TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS status_log (
    log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    req_no      TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    operator    TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reject_stat (
    code    TEXT PRIMARY KEY,
    count   INTEGER NOT NULL DEFAULT 0,
    last_at TEXT
);
"""

_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_line_invoice   ON order_line(invoice);
CREATE INDEX IF NOT EXISTS idx_line_cust      ON order_line(cust_id);
CREATE INDEX IF NOT EXISTS idx_line_group     ON order_line(cust_id, stock_code, invoice_date);
CREATE INDEX IF NOT EXISTS idx_req_line       ON return_request(line_key, status);
CREATE INDEX IF NOT EXISTS idx_log_req        ON status_log(req_no);
"""

_LINE_COLS = (
    "line_key, sheet, invoice, stock_code, description, quantity, invoice_date,"
    " price, cust_id, country, seq, is_cancel, is_bad_debt, is_non_product,"
    " returnable_qty, history_returned"
)

_REQ_COLS = (
    "request_id, req_no, line_key, qty, reason, status, operator, anomaly_kind,"
    " created_at, updated_at"
)

_INSERT_LINE = (
    "INSERT INTO order_line (line_key, sheet, invoice, stock_code, description,"
    " quantity, invoice_date, price, cust_id, country, seq, is_cancel, is_bad_debt,"
    " is_non_product, returnable_qty, imported_at)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


class LineRow(NamedTuple):
    line_key: str
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
    is_cancel: int
    is_bad_debt: int
    is_non_product: int
    returnable_qty: int
    history_returned: int


class RequestRow(NamedTuple):
    request_id: str
    req_no: str
    line_key: str
    qty: int
    reason: str
    status: str
    operator: str
    anomaly_kind: str | None
    created_at: str
    updated_at: str


# ---------------------------------------------------------------- 连接与建表


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """幂等建表：重复执行不删数据。"""
    conn.executescript(_SCHEMA_SQL)
    conn.executescript(_INDEX_SQL)
    conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
    conn.commit()


def schema_version(conn: sqlite3.Connection) -> str:
    return str(conn.execute("PRAGMA user_version").fetchone()[0])


# ---------------------------------------------------------------- 导入


def insert_lines(
    conn: sqlite3.Connection, rows: Iterable[LineRaw], *, batch: int = 5000
) -> int:
    """批量入库，不提交（由调用方决定 commit / rollback，便于 strict 模式整体回滚）。"""
    imported_at = dt.datetime.now().isoformat(timespec="seconds")
    total = 0
    buffer: list[tuple[object, ...]] = []
    for row in rows:
        buffer.append(
            (
                row.line_key, row.sheet, row.invoice, row.stock_code, row.description,
                row.quantity, row.invoice_date, row.price, row.cust_id, row.country,
                row.seq, row.is_cancel, row.is_bad_debt, row.is_non_product,
                row.returnable_qty, imported_at,
            )
        )
        if len(buffer) >= batch:
            conn.executemany(_INSERT_LINE, buffer)
            total += len(buffer)
            buffer.clear()
    if buffer:
        conn.executemany(_INSERT_LINE, buffer)
        total += len(buffer)
    return total


def build_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(_INDEX_SQL)


def iter_for_allocation(conn: sqlite3.Connection) -> Iterator[LineRaw]:
    """按 (cust_id, stock_code, 时间, 发票, 序号) 排序流式取行 —— R-04 的输入窗口。"""
    sql = (
        f"SELECT {_LINE_COLS} FROM order_line"
        " ORDER BY cust_id IS NULL, cust_id, stock_code, invoice_date, invoice, seq"
    )
    for row in conn.execute(sql):
        yield LineRaw(
            sheet=row["sheet"], invoice=row["invoice"], stock_code=row["stock_code"],
            description=row["description"] or "", quantity=row["quantity"],
            invoice_date=row["invoice_date"], price=row["price"],
            cust_id=row["cust_id"], country=row["country"] or "", seq=row["seq"],
            line_key=row["line_key"], is_cancel=row["is_cancel"],
            is_bad_debt=row["is_bad_debt"], is_non_product=row["is_non_product"],
            returnable_qty=row["returnable_qty"],
        )


def apply_history(
    conn: sqlite3.Connection, updates: Iterable[tuple[str, int]]
) -> int:
    payload = [(qty, line_key) for line_key, qty in updates]
    if not payload:
        return 0
    conn.executemany(
        "UPDATE order_line SET history_returned = ? WHERE line_key = ?", payload
    )
    return len(payload)


# ---------------------------------------------------------------- 明细查询


def _to_line(row: sqlite3.Row | None) -> LineRow | None:
    if row is None:
        return None
    return LineRow(**{key: row[key] for key in row.keys()})


def get_line(conn: sqlite3.Connection, line_key: str) -> LineRow | None:
    row = conn.execute(
        f"SELECT {_LINE_COLS} FROM order_line WHERE line_key = ?", (line_key,)
    ).fetchone()
    return _to_line(row)


def get_lines_by_invoice_seq(
    conn: sqlite3.Connection, invoice: str, seq: int
) -> list[LineRow]:
    rows = conn.execute(
        f"SELECT {_LINE_COLS} FROM order_line WHERE invoice = ? AND seq = ?",
        (invoice, seq),
    ).fetchall()
    return [line for line in (_to_line(row) for row in rows) if line is not None]


def get_lines_by_invoice(conn: sqlite3.Connection, invoice: str) -> list[LineRow]:
    rows = conn.execute(
        f"SELECT {_LINE_COLS} FROM order_line WHERE invoice = ? ORDER BY seq", (invoice,)
    ).fetchall()
    return [line for line in (_to_line(row) for row in rows) if line is not None]


def count_lines(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM order_line").fetchone()[0])


def count_history_hit(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM order_line WHERE history_returned > 0"
        ).fetchone()[0]
    )


# ---------------------------------------------------------------- 申请单


def _to_request(row: sqlite3.Row | None) -> RequestRow | None:
    if row is None:
        return None
    return RequestRow(**{key: row[key] for key in row.keys()})


def get_line_requests(conn: sqlite3.Connection, line_key: str) -> list[RequestRow]:
    rows = conn.execute(
        f"SELECT {_REQ_COLS} FROM return_request WHERE line_key = ? ORDER BY created_at",
        (line_key,),
    ).fetchall()
    return [req for req in (_to_request(row) for row in rows) if req is not None]


def get_active_request(conn: sqlite3.Connection, line_key: str) -> RequestRow | None:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    row = conn.execute(
        f"SELECT {_REQ_COLS} FROM return_request"
        f" WHERE line_key = ? AND status IN ({placeholders})"
        " ORDER BY created_at DESC LIMIT 1",
        (line_key, *ACTIVE_STATUSES),
    ).fetchone()
    return _to_request(row)


def get_open_request(conn: sqlite3.Connection, line_key: str) -> RequestRow | None:
    """未结案申请（含 PENDING_MANUAL）—— 只给查询页看着用，不参与额度计算。"""
    placeholders = ",".join("?" for _ in OPEN_STATUSES)
    row = conn.execute(
        f"SELECT {_REQ_COLS} FROM return_request"
        f" WHERE line_key = ? AND status IN ({placeholders})"
        " ORDER BY created_at DESC LIMIT 1",
        (line_key, *OPEN_STATUSES),
    ).fetchone()
    return _to_request(row)


def sum_active_qty(conn: sqlite3.Connection, line_key: str) -> int:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    row = conn.execute(
        f"SELECT COALESCE(SUM(qty), 0) FROM return_request"
        f" WHERE line_key = ? AND status IN ({placeholders})",
        (line_key, *ACTIVE_STATUSES),
    ).fetchone()
    return int(row[0])


def get_request_by_id(conn: sqlite3.Connection, request_id: str) -> RequestRow | None:
    row = conn.execute(
        f"SELECT {_REQ_COLS} FROM return_request WHERE request_id = ?", (request_id,)
    ).fetchone()
    return _to_request(row)


def get_request_by_no(conn: sqlite3.Connection, req_no: str) -> RequestRow | None:
    row = conn.execute(
        f"SELECT {_REQ_COLS} FROM return_request WHERE req_no = ?", (req_no,)
    ).fetchone()
    return _to_request(row)


def next_req_no(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT COALESCE(MAX(CAST(SUBSTR(req_no, 3) AS INTEGER)), 0)"
        " FROM return_request WHERE req_no LIKE 'R-%'"
    ).fetchone()
    return f"R-{int(row[0]) + 1:04d}"


def insert_request(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    req_no: str,
    line_key: str,
    qty: int,
    reason: str,
    operator: str,
    anomaly_kind: str | None,
    now_iso: str,
    status: str = "APPLIED",
) -> RequestRow:
    conn.execute(
        "INSERT INTO return_request (request_id, req_no, line_key, qty, reason, status,"
        " operator, anomaly_kind, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (request_id, req_no, line_key, qty, reason, status, operator, anomaly_kind,
         now_iso, now_iso),
    )
    conn.execute(
        "INSERT INTO status_log (req_no, from_status, to_status, operator, note, at)"
        " VALUES (?,?,?,?,?,?)",
        (req_no, None, status, operator, "首次提交", now_iso),
    )
    return get_request_by_id(conn, request_id)  # type: ignore[return-value]


def update_request_status(
    conn: sqlite3.Connection,
    *,
    req_no: str,
    to_status: str,
    operator: str,
    note: str,
    now_iso: str,
) -> None:
    current = get_request_by_no(conn, req_no)
    if current is None:
        raise KeyError(f"申请单不存在：{req_no}")
    conn.execute(
        "UPDATE return_request SET status = ?, updated_at = ? WHERE req_no = ?",
        (to_status, now_iso, req_no),
    )
    conn.execute(
        "INSERT INTO status_log (req_no, from_status, to_status, operator, note, at)"
        " VALUES (?,?,?,?,?,?)",
        (req_no, current.status, to_status, operator, note, now_iso),
    )


def get_status_log(conn: sqlite3.Connection, req_no: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT from_status, to_status, operator, note, at FROM status_log"
        " WHERE req_no = ? ORDER BY log_id",
        (req_no,),
    ).fetchall()


def release_expired(
    conn: sqlite3.Connection, *, now_iso: str, ttl_days: int = 7
) -> list[str]:
    """R-09 惰性释放：APPLIED 超过 ttl_days 未处理 → REJECTED，流水记 AUTO_RELEASED。

    惰性 = 每次 apply/query 前调用，不依赖定时任务（演示机断网也能跑）。
    """
    now = dt.datetime.fromisoformat(now_iso)
    cutoff = (now - dt.timedelta(days=ttl_days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT req_no, created_at FROM return_request WHERE status = 'APPLIED'"
    ).fetchall()
    released: list[str] = []
    for row in rows:
        created = dt.datetime.fromisoformat(row["created_at"])
        if created < dt.datetime.fromisoformat(cutoff):
            update_request_status(
                conn,
                req_no=row["req_no"],
                to_status="REJECTED",
                operator="system",
                note=f"AUTO_RELEASED：提交后超过 {ttl_days} 天未处理，自动释放额度",
                now_iso=now_iso,
            )
            released.append(row["req_no"])
    if released:
        conn.commit()
    return released


def count_requests(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM return_request").fetchone()[0])


# ---------------------------------------------------------------- 拒绝计数（可重跑的证据）


def record_reject(conn: sqlite3.Connection, code: str, now_iso: str) -> None:
    """每条被拒/转人工的申请都按错误码累加，供导入统计与证据文件使用。"""
    conn.execute(
        "INSERT INTO reject_stat (code, count, last_at) VALUES (?, 1, ?)"
        " ON CONFLICT(code) DO UPDATE SET count = count + 1, last_at = excluded.last_at",
        (code, now_iso),
    )


def get_reject_stats(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT code, count FROM reject_stat ORDER BY count DESC, code"
    ).fetchall()
    return {row["code"]: int(row["count"]) for row in rows}


def format_reject_stats(conn: sqlite3.Connection) -> list[str]:
    stats = get_reject_stats(conn)
    if not stats:
        return ["（暂无拒绝记录：本次运行还没有被拒或转人工的申请）"]
    width = max(len(code) for code in stats)
    total = sum(stats.values())
    lines = [f"  {'错误码':<{width}} 次数"]
    lines.extend(f"  {code:<{width}} {count:>4}" for code, count in stats.items())
    lines.append(f"  {'合计':<{width}} {total:>4}")
    return lines


# ---------------------------------------------------------------- 界面用的只读聚合
# 这些函数只服务 src/webui/（本地网页界面）：全部只读，不写任何状态，
# 也不参与裁决 —— 能不能退永远由 rules.decide 决定（经 CLI apply 走同一条路）。

_REQ_WITH_LINE = (
    "SELECT r.req_no, r.line_key, r.qty, r.reason, r.status, r.operator,"
    " r.anomaly_kind, r.created_at, r.updated_at,"
    " l.invoice, l.seq, l.stock_code, l.description, l.quantity,"
    " l.returnable_qty, l.history_returned, l.is_cancel, l.is_bad_debt,"
    " l.is_non_product, l.cust_id"
    " FROM return_request r LEFT JOIN order_line l ON l.line_key = r.line_key"
)


def overview(conn: sqlite3.Connection) -> dict[str, int]:
    """界面首页一屏到底的数字；口径与 CLI 的 query / import 输出一致。"""
    numbers = stats(conn)
    row = conn.execute(
        "SELECT"
        " COALESCE(SUM(CASE WHEN returnable_qty > 0 THEN 1 ELSE 0 END), 0),"
        " COALESCE(SUM(CASE WHEN returnable_qty > 0 THEN returnable_qty ELSE 0 END), 0),"
        " COALESCE(SUM(history_returned), 0),"
        " COALESCE(SUM(CASE WHEN is_cancel = 1 THEN 1 ELSE 0 END), 0),"
        " COALESCE(SUM(CASE WHEN is_bad_debt = 1 THEN 1 ELSE 0 END), 0),"
        " COALESCE(SUM(CASE WHEN is_non_product = 1 THEN 1 ELSE 0 END), 0),"
        " COALESCE(SUM(CASE WHEN cust_id IS NULL THEN 1 ELSE 0 END), 0),"
        " COALESCE(SUM(CASE WHEN history_returned > 0 THEN 1 ELSE 0 END), 0)"
        " FROM order_line"
    ).fetchone()
    numbers.update(
        {
            "retirable_rows": int(row[0]),
            "retirable_sum": int(row[1]),
            "history_sum": int(row[2]),
            "cancel_rows": int(row[3]),
            "bad_debt": int(row[4]),
            "non_product": int(row[5]),
            "missing_cust": int(row[6]),
            "history_hit_rows": int(row[7]),
        }
    )
    return numbers


def requests_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM return_request GROUP BY status"
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def list_requests(
    conn: sqlite3.Connection, *, status: str | None = None, limit: int = 200
) -> list[dict[str, object]]:
    """申请单列表（带明细字段；line_key 指向的明细可能已被重建清空，故用 LEFT JOIN）。"""
    sql = _REQ_WITH_LINE
    params: list[object] = []
    if status:
        sql += " WHERE r.status = ?"
        params.append(status)
    sql += " ORDER BY r.created_at DESC, r.req_no DESC LIMIT ?"
    params.append(int(limit))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def request_detail(conn: sqlite3.Connection, req_no: str) -> dict[str, object] | None:
    """一条申请的全貌：申请记录 + 明细 + 状态流水 + 当前占用。界面右侧抽屉用。"""
    row = conn.execute(_REQ_WITH_LINE + " WHERE r.req_no = ?", (req_no,)).fetchone()
    if row is None:
        return None
    detail = dict(row)
    line_key = str(detail["line_key"])
    line = get_line(conn, line_key)
    open_req = get_open_request(conn, line_key)
    detail.update(
        {
            "active_qty": sum_active_qty(conn, line_key),
            "log": [dict(entry) for entry in get_status_log(conn, req_no)],
            "line": line._asdict() if line is not None else None,
            "open_request": (
                {"req_no": open_req.req_no, "status": open_req.status, "qty": open_req.qty}
                if open_req is not None
                else None
            ),
        }
    )
    return detail


def line_states(
    conn: sqlite3.Connection, line_keys: Iterable[str]
) -> dict[str, dict[str, object]]:
    """一次取出一批明细的「已占额度」与「未结案申请」（整张发票出状态用，避免 N+1）。

    返回 {line_key: {"active_qty": int, "open": {"req_no","status","qty"} | None}}，
    口径与 get_active_request / sum_active_qty 逐字一致。
    """
    keys = list(dict.fromkeys(line_keys))
    state: dict[str, dict[str, object]] = {
        key: {"active_qty": 0, "open": None} for key in keys
    }
    if not keys:
        return state
    marks = ",".join("?" for _ in keys)
    open_marks = ",".join("?" for _ in OPEN_STATUSES)
    rows = conn.execute(
        "SELECT line_key, req_no, qty, status FROM return_request"
        f" WHERE line_key IN ({marks}) AND status IN ({open_marks})"
        " ORDER BY created_at DESC, req_no DESC",
        (*keys, *OPEN_STATUSES),
    ).fetchall()
    for row in rows:
        entry = state[str(row["line_key"])]
        if row["status"] in ACTIVE_STATUSES:
            entry["active_qty"] = int(entry["active_qty"]) + int(row["qty"])
        if entry["open"] is None:
            entry["open"] = {
                "req_no": str(row["req_no"]),
                "status": str(row["status"]),
                "qty": int(row["qty"]),
            }
    return state


def search_invoices(
    conn: sqlite3.Connection, query: str, limit: int = 20
) -> list[dict[str, object]]:
    """按发票号前缀搜（取消单 C536379 也能按 'C5363' 搜到）。"""
    rows = conn.execute(
        "SELECT invoice, COUNT(*) AS lines, MIN(invoice_date) AS invoice_date,"
        " MAX(cust_id) AS cust_id,"
        " COALESCE(SUM(CASE WHEN returnable_qty > 0 THEN 1 ELSE 0 END), 0)"
        "   AS retirable_rows,"
        " COALESCE(SUM(CASE WHEN returnable_qty > 0 THEN returnable_qty ELSE 0 END), 0)"
        "   AS retirable_sum"
        " FROM order_line WHERE invoice LIKE ?"
        " GROUP BY invoice ORDER BY invoice LIMIT ?",
        (f"{query}%", int(limit)),
    ).fetchall()
    return [dict(row) for row in rows]


def pick_demo_line(conn: sqlite3.Connection) -> dict[str, object] | None:
    """挑一条"客户号在、不是取消/坏账/非商品、还有余额"的明细给演示剧本用。

    优先挑历史上没被退过的（数字更好心算），挑不到再放宽。
    """
    for extra in (" AND history_returned = 0", ""):
        row = conn.execute(
            f"SELECT {_LINE_COLS} FROM order_line"
            " WHERE cust_id IS NOT NULL AND is_cancel = 0 AND is_non_product = 0"
            "   AND is_bad_debt = 0 AND returnable_qty > 2" + extra +
            " ORDER BY invoice, seq LIMIT 1"
        ).fetchone()
        if row is not None:
            line = _to_line(row)
            assert line is not None  # 上面已判空
            return dict(line._asdict())
    return None


def recent_log(conn: sqlite3.Connection, limit: int = 60) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT req_no, from_status, to_status, operator, note, at FROM status_log"
        " ORDER BY log_id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    active = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(qty), 0) FROM return_request"
        f" WHERE status IN ({placeholders})",
        ACTIVE_STATUSES,
    ).fetchone()
    return {
        "lines": count_lines(conn),
        "lines_with_history": count_history_hit(conn),
        "requests": count_requests(conn),
        "active_requests": int(active[0]),
        "active_qty": int(active[1]),
    }
