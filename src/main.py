"""VC-R-01 订单与退货管理：最小可运行版本（CLI）。

七条路径
--------
import   order_line.csv → order_line 表 + 不变量表 + 历史占用（R-04）
apply    提交退货申请 → 五道校验 → 落库 / 转异常队列（R-06/R-07/R-08/R-09）
query    按发票号查询明细：可退多少、被谁占了、申请到哪一步
accept   受理申请：APPLIED / PENDING_MANUAL → ACCEPTED（R-09，人工放行前复核额度）
receive  登记收货：ACCEPTED → RECEIVED（R-09）
close    结案归档：RECEIVED → CLOSED（R-09，额度从此释放）
reject   人工拒绝：APPLIED / PENDING_MANUAL → REJECTED（R-09，额度立即释放）

退出码（供测试与脚本断言，不看中文提示）
------------------------------------
0 通过   1 系统/格式错误   2 校验拒绝   3 转人工核验   4 不变量与基线不符（整体回滚）
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import csv_loader
import db
import history
import rules
import verify

EXIT_OK = 0
EXIT_IO = 1
EXIT_REJECT = 2
EXIT_MANUAL = 3
EXIT_BASELINE = 4

DEFAULT_CSV = "data/build/order_line.csv"
DEFAULT_DB = "data/build/return_system.db"
DEFAULT_OUT = "data/build"

_PENDING_STATUS = "PENDING_MANUAL"


# ---------------------------------------------------------------- 输出


def emit(result: rules.CheckResult, *, exit_code: int, head: tuple[str, ...] = ()) -> int:
    for line in head:
        print(line)
    print(result.message)
    extra = "、".join(
        f"{key}={value}"
        for key, value in result.data.items()
        if key not in ("reason", "active_request_no")
    )
    if extra:
        print(f"  （{extra}）")
    print(f"RESULT={result.code}")
    return exit_code


def resolve_now(ns: argparse.Namespace) -> str:
    now = getattr(ns, "now", None)
    if now:
        return rules.normalize_ts(now)
    return dt.datetime.now().isoformat(timespec="seconds")


def _fail(message: str) -> int:
    print(f"错误：{message}")
    print("RESULT=E_IO")
    return EXIT_IO


def _write_reject_stats(conn, db_path: str | Path) -> Path | None:
    """把累计拒绝计数落成文件，作为"每条校验都被触发过"的证据。"""
    if str(db_path) == ":memory:":
        return None
    path = Path(db_path).parent / "reject_stat.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# 校验拒绝计数（每次 import / apply 自动累加，未手改）",
                "# 只统计申请校验与状态流转被拒的次数；query 查不到发票不计入",
                "",
                *db.format_reject_stats(conn),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------- import


def cmd_import(ns: argparse.Namespace) -> int:
    csv_path = Path(ns.csv)
    db_path = Path(ns.db)
    out_dir = Path(ns.out)

    print(f"[1/4] 表头校验：{csv_path}")
    csv_loader.check_header(str(csv_path))
    print(f"      通过，{len(csv_loader.HEADER)} 列且顺序一致")

    conn = db.connect(db_path)
    db.init_schema(conn)
    existing = db.count_lines(conn)
    if existing and not ns.rebuild:
        conn.close()
        return _fail(
            f"库中已有 {existing} 条明细。重跑导入请加 --rebuild（会清空 order_line，"
            f"保留申请记录），或换一个 --db"
        )
    if existing:
        conn.execute("DELETE FROM order_line")
        conn.commit()
        print(f"      --rebuild：已清空旧明细 {existing} 条，申请记录保留")

    acc = verify.Accumulator()
    limit = ns.limit

    def rows() -> Iterator[csv_loader.LineRaw]:
        for row in csv_loader.iter_lines(str(csv_path), limit=limit):
            acc.add(row)
            yield row

    started = dt.datetime.now()
    try:
        inserted = db.insert_lines(conn, rows())
    except (csv_loader.CsvFormatError, sqlite3.Error) as exc:
        conn.rollback()
        conn.close()
        return _fail(str(exc))

    inv = acc.result()
    diffs = verify.compare(inv, ignore=verify.SAMPLE_IGNORED if limit else ())
    strict = ns.baseline == "strict" and limit is None
    report = verify.format_report(inv, diffs)

    if diffs and strict:
        conn.rollback()
        conn.close()
        print(report)
        print("错误：不变量与基线不符，已整体回滚（如需切片导入，加 --baseline report）")
        print("RESULT=E_BASELINE")
        return EXIT_BASELINE

    conn.commit()
    print(f"[2/4] 读取并入库 {inserted:,} 行（明细数）")
    prov = verify.provenance()
    prov_diffs = verify.check_provenance()
    print(
        f"      去重口径（上游 prepare.py）：去重前 {prov['去重前行数']:,} 行"
        f" → 去重后 {prov['去重后行数']:,} 行"
        f"（重叠发票 {prov['重叠发票数']:,} 张 / {prov['去重丢弃行数']:,} 行）"
        f"{' ✓' if not prov_diffs else ' ✗'}"
    )

    db.build_indexes(conn)
    conn.commit()

    updates, alloc = history.allocate(db.iter_for_allocation(conn))
    hit = db.apply_history(conn, updates)
    conn.commit()
    elapsed = (dt.datetime.now() - started).total_seconds()

    print(f"[3/4] {report}")
    print(
        f"[4/4] 历史占用（R-04 FIFO）：分组 {alloc.groups:,}，命中明细 {hit:,} 行，"
        f"分摊 {alloc.assigned:,} 件"
    )
    print(
        f"      无原单 {alloc.unmatched_cancel:,}；超量 {alloc.over_returned:,}；"
        f"C 行但数量>=0 {alloc.cancel_pos_skipped:,}；缺客户号跳过 {alloc.guest_skipped:,}"
    )
    if alloc.over_samples:
        print(f"      超量样例：{'、'.join(alloc.over_samples)}")
    print(f"      取消行（C 开头）{inv.cancel_rows:,}；可退明细 {inv.retirable_rows:,} 行"
          f"（可退数量合计 {inv.retirable_sum:,} 件）")
    reject_lines = db.format_reject_stats(conn)
    print("      各校验拒绝计数（累计）：")
    for line in reject_lines:
        print(f"    {line}")
    if prov_diffs:
        for item in prov_diffs:
            print(f"      ! {item}")
    print(f"总耗时 {elapsed:.1f}s")

    out_dir.mkdir(parents=True, exist_ok=True)
    stat_path = out_dir / "import_invariants.txt"
    stat_path.write_text(
        "\n".join(
            [
                f"数据源   {csv_path}",
                f"目标库   {db_path}",
                f"导入时间 {dt.datetime.now().isoformat(timespec='seconds')}",
                f"读取行数 {inserted}",
                f"明细数（order_line 行数）{inserted}",
                f"切片模式 {'是（limit=' + str(limit) + '，不与全量基线比较）' if limit else '否'}",
                "",
                "去重口径（上游 prepare.py）",
                f"  去重前行数      {prov['去重前行数']}",
                f"  重叠发票数      {prov['重叠发票数']}",
                f"  去重丢弃行数    {prov['去重丢弃行数']}",
                f"  去重后行数      {prov['去重后行数']}",
                f"  自洽性校验      {'通过' if not prov_diffs else '不通过：' + '；'.join(prov_diffs)}",
                "",
                report,
                "",
                "明细分类",
                f"  取消行（C 开头）{inv.cancel_rows}",
                f"  坏账调整（A）   {inv.bad_debt}",
                f"  非商品行        {inv.non_product}",
                f"  负数量行        {inv.neg_qty}",
                f"  可退明细行      {inv.retirable_rows}",
                f"  可退数量合计    {inv.retirable_sum}",
                "",
                "历史占用（R-04 FIFO）",
                f"  分组            {alloc.groups}",
                f"  命中明细行      {hit}",
                f"  分摊件数        {alloc.assigned}",
                f"  找不到原单      {alloc.unmatched_cancel}",
                f"  超量退货        {alloc.over_returned}",
                f"  C 行数量>=0     {alloc.cancel_pos_skipped}",
                f"  缺客户号跳过    {alloc.guest_skipped}",
                f"  超量样例        {'、'.join(alloc.over_samples) or '-'}",
                "",
                "各校验拒绝计数（累计）",
                *reject_lines,
                "",
                f"总耗时 {elapsed:.1f}s",
            ]
        ),
        encoding="utf-8",
    )
    print(f"不变量表已写入 {stat_path}")
    reject_path = _write_reject_stats(conn, db_path)
    print(f"拒绝计数已写入 {reject_path}")
    print("RESULT=OK")
    conn.close()
    return EXIT_OK


# ---------------------------------------------------------------- apply


def _resolve_line(conn, ns: argparse.Namespace) -> tuple[db.LineRow | None, str]:
    if ns.line_key:
        return db.get_line(conn, ns.line_key), ns.line_key
    if ns.invoice is None or ns.seq is None:
        raise ValueError("需要 --line-key，或同时给出 --invoice 与 --seq")
    invoice = rules.normalize_invoice(ns.invoice)
    rows = db.get_lines_by_invoice_seq(conn, invoice, ns.seq)
    if not rows:
        return None, f"{invoice}|{ns.seq}"
    if len(rows) > 1:
        raise ValueError(
            f"发票 {invoice} 序号 {ns.seq} 匹配到 {len(rows)} 行，请改用 --line-key"
        )
    return rows[0], rows[0].line_key


def cmd_apply(ns: argparse.Namespace) -> int:
    conn = db.connect(ns.db)
    db.init_schema(conn)
    now_iso = resolve_now(ns)

    released = db.release_expired(conn, now_iso=now_iso, ttl_days=ns.ttl_days)
    if released:
        print(f"已自动释放超期申请：{'、'.join(released)}（R-09）")

    try:
        line, line_key = _resolve_line(conn, ns)
    except ValueError as exc:
        conn.close()
        return _fail(str(exc))

    request_id = ns.request_id or hashlib.sha1(
        f"{line_key}|{ns.qty}|{now_iso}".encode("utf-8")
    ).hexdigest()[:12]

    replay = db.get_request_by_id(conn, request_id)
    if replay is not None:
        conn.close()
        result = rules.accept(
            code="OK",
            message=(
                f"重复提交同一请求（request_id={request_id}），返回上次结果："
                f"申请号 {replay.req_no}，状态 {replay.status}，数量 {replay.qty}"
            ),
            req_no=replay.req_no,
            replay=1,
        )
        return emit(result, exit_code=EXIT_OK)

    if line is None:
        active_no = None
        result = rules.decide(
            line_key=line_key,
            found=False,
            returnable_qty=0,
            is_cancel=0,
            is_bad_debt=0,
            is_non_product=0,
            cust_id=None,
            invoice_date_iso=now_iso,
            history_returned=0,
            active_qty=0,
            active_request_no=active_no,
            now_iso=now_iso,
            qty_requested=ns.qty,
        )
        db.record_reject(conn, result.code, now_iso)
        conn.commit()
        _write_reject_stats(conn, ns.db)
        conn.close()
        return emit(result, exit_code=EXIT_REJECT)

    active = db.get_active_request(conn, line.line_key)
    result = rules.decide(
        line_key=line.line_key,
        found=True,
        returnable_qty=line.returnable_qty,
        is_cancel=line.is_cancel,
        is_bad_debt=line.is_bad_debt,
        is_non_product=line.is_non_product,
        cust_id=line.cust_id,
        invoice_date_iso=line.invoice_date,
        history_returned=line.history_returned,
        active_qty=db.sum_active_qty(conn, line.line_key),
        active_request_no=active.req_no if active else None,
        now_iso=now_iso,
        qty_requested=ns.qty,
    )

    if result.ok:
        req = db.insert_request(
            conn,
            request_id=request_id,
            req_no=db.next_req_no(conn),
            line_key=line.line_key,
            qty=ns.qty,
            reason=ns.reason,
            operator=ns.operator,
            anomaly_kind=None,
            now_iso=now_iso,
        )
        conn.commit()
        conn.close()
        result = rules.accept(
            code="OK",
            message=f"申请号 {req.req_no}｜{result.message}",
            req_no=req.req_no,
            **{k: v for k, v in result.data.items() if k != "message"},
        )
        return emit(result, exit_code=EXIT_OK, head=(f"明细 {line.line_key}｜{line.stock_code}",))

    if result.code == "E_MANUAL":
        req = db.insert_request(
            conn,
            request_id=request_id,
            req_no=db.next_req_no(conn),
            line_key=line.line_key,
            qty=ns.qty,
            reason=ns.reason,
            operator=ns.operator,
            anomaly_kind=str(result.data.get("reason", "unknown")),
            now_iso=now_iso,
            status=_PENDING_STATUS,
        )
        db.record_reject(conn, "E_MANUAL", now_iso)
        conn.commit()
        _write_reject_stats(conn, ns.db)
        conn.close()
        result = rules.reject(
            "E_MANUAL",
            f"{result.message}（申请号 {req.req_no}，不占用可退额度）",
            req_no=req.req_no,
            anomaly_kind=req.anomaly_kind,
        )
        return emit(result, exit_code=EXIT_MANUAL, head=(f"明细 {line.line_key}",))

    db.record_reject(conn, result.code, now_iso)
    conn.commit()
    _write_reject_stats(conn, ns.db)
    conn.close()
    return emit(result, exit_code=EXIT_REJECT, head=(f"明细 {line.line_key}",))


# ---------------------------------------------------------------- query


def cmd_query(ns: argparse.Namespace) -> int:
    conn = db.connect(ns.db)
    db.init_schema(conn)
    now_iso = resolve_now(ns)
    released = db.release_expired(conn, now_iso=now_iso, ttl_days=ns.ttl_days)
    if released:
        print(f"已自动释放超期申请：{'、'.join(released)}（R-09）")

    invoice = rules.normalize_invoice(ns.invoice)
    lines = db.get_lines_by_invoice(conn, invoice)
    if not lines:
        conn.close()
        print(f"发票 {invoice} 下没有明细，请核对发票号")
        print("RESULT=E_NOT_FOUND")
        return EXIT_REJECT

    first = lines[0]
    cust = first.cust_id or "（缺 Customer ID）"
    print(f"发票 {invoice}｜客户 {cust}｜{first.country}｜开票 {first.invoice_date}")
    print("-" * 104)
    header = (
        f"{'序号':<4}{'商品编码':<10}{'描述':<30}{'数量':>5}{'历史已退':>8}"
        f"{'已占用':>7}{'可退':>6}  状态"
    )
    print(header)

    total_retirable = 0
    for line in lines:
        active = db.get_open_request(conn, line.line_key)
        active_qty = db.sum_active_qty(conn, line.line_key)
        retirable = rules.compute_retirable(
            line.returnable_qty, line.history_returned, active_qty
        )
        total_retirable += retirable
        if line.is_non_product:
            status = "不可退(非商品)"
        elif line.is_bad_debt:
            status = "不可退(坏账调整)"
        elif line.is_cancel:
            status = "取消行"
        elif line.returnable_qty <= 0:
            status = "不可退(无可退数量)"
        elif active is not None:
            status = f"{active.status}({active.req_no} {active.qty}件)"
        elif retirable == 0 and line.history_returned > 0:
            status = "历史已退满"
        elif line.cust_id is None:
            status = "可申请(缺客户号→人工)"
        else:
            status = "可申请"
        desc = (line.description or "")[:28]
        print(
            f"{line.seq:<4}{line.stock_code:<10}{desc:<30}{line.quantity:>5}"
            f"{line.history_returned:>8}{active_qty:>7}{retirable:>6}  {status}"
        )

    print("-" * 104)
    print(f"本发票可退合计 {total_retirable} 件（明细 {len(lines)} 行）")

    if ns.log:
        for line in lines:
            for req in db.get_line_requests(conn, line.line_key):
                print(f"\n[{req.req_no}] {line.line_key} 申请 {req.qty} 件 状态 {req.status}")
                for entry in db.get_status_log(conn, req.req_no):
                    note = f"（{entry['note']}）" if entry["note"] else ""
                    print(
                        f"  {entry['at']}  {entry['from_status'] or '-':<14} → "
                        f"{entry['to_status']:<14} by {entry['operator']}{note}"
                    )

    conn.close()
    print("RESULT=OK")
    return EXIT_OK


# ---------------------------------------------------------------- 状态流转 R-09

# 四个动词 → 目标状态。动词只负责"说什么话"，能去哪由 rules.TRANSITIONS 决定。
_TRANSITION_VERBS: dict[str, tuple[str, str]] = {
    "accept": ("ACCEPTED", "受理"),
    "receive": ("RECEIVED", "收货"),
    "close": ("CLOSED", "结案"),
    "reject": ("REJECTED", "拒绝"),
}


def cmd_transition(ns: argparse.Namespace) -> int:
    """APPLIED → ACCEPTED → RECEIVED → CLOSED，外加人工 REJECTED。

    这一层只做三件事：找到申请 → 问规则"能不能走" → 写库并留流水。
    额度语义靠 db.ACTIVE_STATUSES 兜住：ACCEPTED/RECEIVED 仍占额度，
    CLOSED/REJECTED 自动释放，不用在这里改任何数量。
    """
    to_status = ns.to_status
    verb = _TRANSITION_VERBS[ns.command][1]

    conn = db.connect(ns.db)
    db.init_schema(conn)
    now_iso = resolve_now(ns)

    def refuse(result: rules.CheckResult, head: tuple[str, ...] = ()) -> int:
        """被拒也要计数落盘：reject_stat 不能只记住 apply 的账。"""
        db.record_reject(conn, result.code, now_iso)
        conn.commit()
        _write_reject_stats(conn, ns.db)
        conn.close()
        return emit(result, exit_code=EXIT_REJECT, head=head)

    req = db.get_request_by_no(conn, ns.req)
    if req is None:
        return refuse(
            rules.reject(
                "E_NOT_FOUND",
                f"没有申请号 {ns.req}，请先用 apply 提交或核对申请号",
                req_no=ns.req,
            )
        )

    head = (f"申请 {req.req_no}｜明细 {req.line_key}",)

    transition = rules.check_transition(req.status, to_status)
    if not transition.ok:
        return refuse(transition, head)

    # R-08 的人工放行要再核一次额度：转人工的单子当初**没占**额度，
    # 期间额度可能已被别的申请占掉，直接改成 ACCEPTED 就会超卖。
    # 复核不通过就保持 PENDING_MANUAL 原样，让人工先去处理占用它的那条。
    if req.status == "PENDING_MANUAL" and to_status == "ACCEPTED":
        line = db.get_line(conn, req.line_key)
        if line is None:
            return refuse(
                rules.reject(
                    "E_NOT_FOUND",
                    f"申请 {req.req_no} 指向的明细 {req.line_key} 已不存在，无法放行",
                    req_no=req.req_no,
                ),
                head,
            )
        retirable = rules.compute_retirable(
            line.returnable_qty,
            line.history_returned,
            db.sum_active_qty(conn, req.line_key),
        )
        quota = rules.check_qty(req.qty, retirable)
        if not quota.ok:
            return refuse(
                rules.reject(
                    "E_EXCEED",
                    f"人工放行前复核额度未通过：{quota.message}"
                    f"（申请 {req.req_no} 保持 PENDING_MANUAL，不占额度）",
                    req_no=req.req_no,
                    retirable=retirable,
                    qty=req.qty,
                ),
                head,
            )

    db.update_request_status(
        conn,
        req_no=req.req_no,
        to_status=to_status,
        operator=ns.operator,
        note=ns.note or f"人工{verb}",
        now_iso=now_iso,
    )
    conn.commit()
    conn.close()

    result = rules.accept(
        message=(
            f"申请 {req.req_no} 已{verb}：{req.status} → {to_status}"
            f"（{rules.STATUS_LABELS.get(to_status, to_status)}），数量 {req.qty} 件"
        ),
        req_no=req.req_no,
        from_status=req.status,
        to_status=to_status,
        qty=req.qty,
    )
    return emit(result, exit_code=EXIT_OK, head=head)


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py", description="VC-R-01 订单与退货管理系统（最小可运行版本）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="导入 order_line.csv，生成可退明细表")
    p_import.add_argument("--csv", default=DEFAULT_CSV)
    p_import.add_argument("--db", default=DEFAULT_DB)
    p_import.add_argument("--out", default=DEFAULT_OUT)
    p_import.add_argument("--limit", type=int, default=None, help="只导入前 N 行（测试用）")
    p_import.add_argument("--rebuild", action="store_true", help="清空 order_line 后重建")
    p_import.add_argument(
        "--baseline", choices=("strict", "report"), default="strict",
        help="不变量不符时：strict 回滚退出，report 只报警继续",
    )
    p_import.add_argument("--now", default=None)
    p_import.set_defaults(func=cmd_import)

    p_apply = sub.add_parser("apply", help="提交退货申请")
    p_apply.add_argument("--db", default=DEFAULT_DB)
    p_apply.add_argument("--line-key", dest="line_key", default=None)
    p_apply.add_argument("--invoice", default=None)
    p_apply.add_argument("--seq", type=int, default=None)
    p_apply.add_argument("--qty", type=int, required=True)
    p_apply.add_argument("--reason", default="")
    p_apply.add_argument("--operator", default="售后-未署名")
    p_apply.add_argument("--request-id", dest="request_id", default=None)
    p_apply.add_argument("--ttl-days", dest="ttl_days", type=int, default=7)
    p_apply.add_argument("--now", default=None)
    p_apply.set_defaults(func=cmd_apply)

    p_query = sub.add_parser("query", help="按发票号查询明细与申请状态")
    p_query.add_argument("--db", default=DEFAULT_DB)
    p_query.add_argument("--invoice", required=True)
    p_query.add_argument("--log", action="store_true", help="打印状态流水")
    p_query.add_argument("--ttl-days", dest="ttl_days", type=int, default=7)
    p_query.add_argument("--now", default=None)
    p_query.set_defaults(func=cmd_query)

    for name, (to_status, verb) in _TRANSITION_VERBS.items():
        p = sub.add_parser(
            name, help=f"{verb}一条申请（→ {to_status}），非法流转会被拒绝"
        )
        p.add_argument("--db", default=DEFAULT_DB)
        p.add_argument("--req", required=True, metavar="申请号", help="如 R-0001")
        p.add_argument("--operator", default="售后-未署名")
        p.add_argument("--note", default="", help="人工备注，写进状态流水")
        p.add_argument("--now", default=None)
        p.set_defaults(func=cmd_transition, to_status=to_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ns = build_parser().parse_args(argv)
    try:
        return int(ns.func(ns))
    except (csv_loader.CsvFormatError, sqlite3.Error, OSError, ValueError) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
