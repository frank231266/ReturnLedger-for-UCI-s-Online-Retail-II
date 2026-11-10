"""一键重生成 `data/build/实际运行结果.txt`（21 节，演示顺序 = 本节顺序）。

为什么需要这个脚本
------------------
CLAUDE.md 铁律 6 要求"证据文件只能由程序生成，不许手改"。
`import_invariants.txt` 和 `reject_stat.txt` 本来就是程序写的，唯独
`实际运行结果.txt` 以前是把命令行输出粘起来的——数字是真的，但"手拼"这件事
本身就违背铁律，老师一问"你重跑一遍给我看"就答不上来。

本脚本怎么保证可信
------------------
1. 每一步都真调 `python src/main.py ...`（subprocess，不是复制粘贴）；
2. 每一步都写死"预期退出码 + 预期 RESULT 码"，对不上就抛错、**不写文件**；
3. 写文件用临时文件 + 原子替换，不会留下半份证据；
4. 删库重建之前先跑 `tools/gate.py verify` 确认数据指纹没变（铁律 8）。

用法
----
    python tools/make_evidence.py            # 全量重跑并覆盖证据文件（约 30 秒）
    python tools/make_evidence.py --dry-run  # 只打印到屏幕，不碰任何文件
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import gate  # noqa: E402  （复用它的数据指纹逻辑，不另写一套）
import evidence_manifest  # noqa: E402  （生成清单：门禁靠它判断证据/生成器有没有变过）

PY = sys.executable
CSV = "data/build/order_line.csv"
DB = "data/build/return_system.db"
OUT = "data/build"

NOW = "2026-10-18T10:00:00"          # 固定演示时钟：不固定就会触发 R-09 释放
LATER = "2026-10-26T10:00:00"         # NOW + 8 天


class EvidenceError(RuntimeError):
    """某一步的实际结果与预期不符 —— 证据不能写，先修代码。"""


def run_cli(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-X", "utf8", str(ROOT / "src" / "main.py"), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    return proc.returncode, (proc.stdout + proc.stderr).rstrip()


def step(
    title: str,
    args: list[str],
    *,
    expect_code: int,
    expect_result: str | None = None,
) -> tuple[str, str]:
    """跑一条命令，核对退出码与 RESULT 行，返回（标题, 正文）。"""
    code, out = run_cli(args)
    want = f"退出码 {expect_code}"
    if expect_result:
        want += f" / RESULT={expect_result}"
    if code != expect_code:
        raise EvidenceError(f"第「{title}」步退出码 {code}，预期 {expect_code}：\n{out}")
    if expect_result and f"RESULT={expect_result}" not in out:
        raise EvidenceError(f"第「{title}」步没有 RESULT={expect_result}：\n{out}")
    body = "\n".join(
        [
            "$ python src/main.py " + " ".join(args),
            f"（预期：{want}）",
            out,
            f"（实际：退出码 {code}" + (f" / RESULT={expect_result}" if expect_result else "") + " ✓）",
        ]
    )
    return title, body


# ---------------------------------------------------------------- 特殊小节


def ttl_section() -> tuple[str, str]:
    """R-09 惰性释放：在副本库上把时钟推到第 8 天，不污染主库。"""
    copy = Path(DB).with_name("_evidence_ttl8.db")   # 跟主库同目录，相对路径好写
    copy_abs = ROOT / copy
    for suffix in ("", "-wal", "-shm"):
        src = ROOT / (DB + suffix)
        if src.exists():
            shutil.copy2(src, copy_abs.with_name(copy_abs.name + suffix))
    db_arg = str(copy).replace("\\", "/")
    try:
        code, out = run_cli(
            [
                "query", "--db", db_arg,
                "--invoice", "567309", "--now", LATER, "--log",
            ]
        )
        if code != 0:
            raise EvidenceError(f"第「R-09 惰性释放」步退出码 {code}：\n{out}")
        if "AUTO_RELEASED" not in out:
            raise EvidenceError(f"第「R-09 惰性释放」步没有出现 AUTO_RELEASED：\n{out}")
        if "R-0002" not in out:
            raise EvidenceError(f"第「R-09 惰性释放」步没有释放 R-0002：\n{out}")

        # 对照：已受理的 R-0003 不该被释放（只有 APPLIED 会超期释放）
        code2, out2 = run_cli(
            [
                "query", "--db", db_arg,
                "--invoice", "489525", "--now", LATER, "--log",
            ]
        )
        if code2 != 0:
            raise EvidenceError(f"第「R-09 惰性释放」对照步退出码 {code2}：\n{out2}")
        if "ACCEPTED" not in out2:
            raise EvidenceError(f"第「R-09 惰性释放」对照步里 R-0003 丢了 ACCEPTED：\n{out2}")

        body = "\n".join(
            [
                "说明：先把主库整份复制成同目录的 _evidence_ttl8.db，再在副本上把时钟推到 8 天后，",
                "      主库的演示状态不受影响；本节跑完立即删除副本。",
                f"$ python src/main.py query --db {db_arg} --invoice 567309 --now {LATER} --log",
                "（预期：退出码 0 / 出现 AUTO_RELEASED，R-0002 被释放）",
                out,
                "（实际：退出码 0 / 出现 AUTO_RELEASED ✓）",
                "",
                "对照：已受理的申请不受 7 天释放影响（只有 APPLIED 会被释放）",
                f"$ python src/main.py query --db {db_arg} --invoice 489525 --now {LATER} --log",
                out2,
            ]
        )
        return "R-09 惰性释放：第 8 天只有 APPLIED 被释放，ACCEPTED 不动（副本库，不污染主库）", body
    finally:
        for suffix in ("", "-wal", "-shm"):
            leftover = copy_abs.with_name(copy_abs.name + suffix)
            if leftover.exists():
                leftover.unlink()


def tally_section() -> tuple[str, str]:
    """把两份证据文件与库内累计状态原样摊出来。"""
    stat_path = ROOT / OUT / "reject_stat.txt"
    stat_text = stat_path.read_text(encoding="utf-8").rstrip()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        def one(sql: str) -> object:
            return conn.execute(sql).fetchone()[0]

        lines = one("SELECT COUNT(*) FROM order_line")
        hist = one("SELECT COUNT(*) FROM order_line WHERE history_returned > 0")
        retirable = one("SELECT COUNT(*) FROM order_line WHERE returnable_qty > 0")
        cancel = one("SELECT COUNT(*) FROM order_line WHERE is_cancel = 1")
        active_qty = one(
            "SELECT COALESCE(SUM(qty), 0) FROM return_request"
            " WHERE status IN ('APPLIED','ACCEPTED','RECEIVED')"
        )
        logs = one("SELECT COUNT(*) FROM status_log")
        dist = conn.execute(
            "SELECT status, COUNT(*) AS n FROM return_request GROUP BY status ORDER BY status"
        ).fetchall()
    finally:
        conn.close()

    body = "\n".join(
        [
            f"--- {OUT}/reject_stat.txt 原样内容（程序写，未手改） ---",
            stat_text,
            "",
            "--- 库内累计状态（主库，演示后） ---",
            f"order_line 总行数             {lines}",
            f"history_returned > 0 的行数   {hist}",
            f"可退明细行（returnable>0）    {retirable}",
            f"取消行（is_cancel=1）         {cancel}",
            "申请单按状态分布：",
            *[f"  {row['status']:<14} {row['n']}" for row in dist],
            f"生效占用合计（APPLIED/ACCEPTED/RECEIVED） {active_qty}",
            f"状态流水条数                  {logs}",
        ]
    )
    return "各校验拒绝计数（reject_stat.txt 原样）+ 库内累计状态", body


# ---------------------------------------------------------------- 演示顺序


def build_sections() -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    base = ["--db", DB, "--now", NOW]
    out_dir = ["--out", OUT]

    sections.append(
        step(
            "导入统计：读取行数 / 去重口径 / 取消行数 / 明细数 / 各校验拒绝计数",
            ["import", "--csv", CSV, *base[:2], *out_dir],
            expect_code=0,
            expect_result="OK",
        )
    )
    sections.append(
        step(
            "查询发票 536809（主流程前：原始 1824、历史已退 4 → 可退 1820）",
            ["query", "--db", DB, "--invoice", "536809", "--now", NOW],
            expect_code=0,
            expect_result="OK",
        )
    )
    sections.append(
        step(
            "主流程 R-04：明细 536809|1 申请退 10 件 → R-0001",
            [
                "apply", *base, "--invoice", "536809", "--seq", "1", "--qty", "10",
                "--reason", "尺寸不符", "--operator", "售后-张",
            ],
            expect_code=0,
            expect_result="OK",
        )
    )
    sections.append(
        step(
            "R-06 重复退货：同一明细再申请 5 件（记录数不增）",
            ["apply", *base, "--invoice", "536809", "--seq", "1", "--qty", "5"],
            expect_code=2,
            expect_result="E_DUPLICATE",
        )
    )
    sections.append(
        step(
            "R-07 数量边界：明细 489525|1 申请 0 件",
            ["apply", *base, "--invoice", "489525", "--seq", "1", "--qty", "0"],
            expect_code=2,
            expect_result="E_INVALID_QTY",
        )
    )
    sections.append(
        step(
            "R-07 超量：明细 567309|1 可退 1608，申请 99999 件",
            ["apply", *base, "--invoice", "567309", "--seq", "1", "--qty", "99999"],
            expect_code=2,
            expect_result="E_EXCEED",
        )
    )
    sections.append(
        step(
            "R-05 非商品：明细 489439|19 是 POST 运费行",
            ["apply", *base, "--invoice", "489439", "--seq", "19", "--qty", "1"],
            expect_code=2,
            expect_result="E_NON_RETURNABLE",
        )
    )
    sections.append(
        step(
            "不存在的明细：发票 999999 序号 1",
            ["apply", *base, "--invoice", "999999", "--seq", "1", "--qty", "1"],
            expect_code=2,
            expect_result="E_NOT_FOUND",
        )
    )
    sections.append(
        step(
            "R-06 幂等：同一 request-id 提交两次，只落 1 条（第一次）",
            [
                "apply", *base, "--invoice", "567309", "--seq", "1", "--qty", "2",
                "--request-id", "demo-567309-1",
            ],
            expect_code=0,
            expect_result="OK",
        )
    )
    sections.append(
        step(
            "R-06 幂等：同一 request-id 第二次提交（返回上次结果，不新增记录）",
            [
                "apply", *base, "--invoice", "567309", "--seq", "1", "--qty", "2",
                "--request-id", "demo-567309-1",
            ],
            expect_code=0,
            expect_result="OK",
        )
    )
    sections.append(
        step(
            "R-08 缺客户号：明细 489525|1 申请 1 件 → 转异常队列 PENDING_MANUAL（不占额度）",
            ["apply", *base, "--invoice", "489525", "--seq", "1", "--qty", "1"],
            expect_code=3,
            expect_result="E_MANUAL",
        )
    )
    sections.append(
        step(
            "R-09 受理：R-0001 APPLIED → ACCEPTED（额度继续占用，不释放）",
            ["accept", *base, "--req", "R-0001", "--operator", "客服主管-李", "--note", "电话核实后受理"],
            expect_code=0,
            expect_result="OK",
        )
    )
    sections.append(
        step(
            "查询 536809：状态列从 APPLIED 变 ACCEPTED，仍占 10 件",
            ["query", "--db", DB, "--invoice", "536809", "--now", NOW, "--log"],
            expect_code=0,
            expect_result="OK",
        )
    )
    sections.append(
        step(
            "R-09 非法流转：还没收货就想结案，必须被拦",
            ["close", *base, "--req", "R-0001"],
            expect_code=2,
            expect_result="E_BAD_TRANSITION",
        )
    )
    sections.append(
        step(
            "R-09 收货：R-0001 ACCEPTED → RECEIVED",
            ["receive", *base, "--req", "R-0001", "--operator", "仓库-王", "--note", "质检通过"],
            expect_code=0,
            expect_result="OK",
        )
    )
    sections.append(
        step(
            "R-09 结案：R-0001 RECEIVED → CLOSED（额度从此释放）",
            ["close", *base, "--req", "R-0001", "--operator", "客服主管-李", "--note", "退款已处理"],
            expect_code=0,
            expect_result="OK",
        )
    )
    sections.append(
        step(
            "查询 536809：结案后额度回到 1820，状态列变回「可申请」（R-06 例外允许再申请）",
            ["query", "--db", DB, "--invoice", "536809", "--now", NOW, "--log"],
            expect_code=0,
            expect_result="OK",
        )
    )
    sections.append(
        step(
            "R-08 人工放行前复核额度：R-0003 PENDING_MANUAL → ACCEPTED",
            ["accept", *base, "--req", "R-0003", "--operator", "客服主管-李", "--note", "客户电话核实，人工放行"],
            expect_code=0,
            expect_result="OK",
        )
    )
    sections.append(
        step(
            "查询 489525：R-0003 已 ACCEPTED（转人工的单子放行后才占额度）",
            ["query", "--db", DB, "--invoice", "489525", "--now", NOW, "--log"],
            expect_code=0,
            expect_result="OK",
        )
    )
    sections.append(ttl_section())
    sections.append(tally_section())
    return sections


# ---------------------------------------------------------------- 主流程


def reset_db() -> None:
    """删库重建前先验数据指纹（铁律 8），避免"证据对了、数据被悄悄改了"。"""
    if not gate.cmd_verify(verbose=True):
        raise EvidenceError(
            "数据指纹与登记时不一致，已停止（先跑 python tools/gate.py verify 查原因）"
        )
    for suffix in ("", "-wal", "-shm"):
        path = ROOT / (DB + suffix)
        if path.exists():
            path.unlink()
            print(f"[重置] 已删除 {DB}{suffix}")


def tests_line() -> str:
    proc = subprocess.run(
        [PY, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    text = proc.stdout + proc.stderr
    summary = next(
        (line.strip() for line in reversed(text.splitlines()) if line.startswith("Ran ")), ""
    )
    verdict = "OK" if proc.returncode == 0 else "FAILED"
    return f"python -m unittest discover -s tests → {summary} {verdict}"


def render(sections: list[tuple[str, str]]) -> str:
    header = [
        "VC-R-01 订单与退货管理系统 —— 实际运行结果（可重跑证据）",
        f"数据源：{CSV}（上游 prepare.py 产物）",
        f"目标库：{DB}",
        f"运行环境：Python {sys.version.split()[0]} / SQLite {sqlite3.sqlite_version} / 仅标准库",
        f"演示时钟：--now {NOW}（固定时钟，避免真实时间推进触发 R-09 释放）",
        f"生成时间：{dt.datetime.now().isoformat(timespec='seconds')}",
        "生成方式：python tools/make_evidence.py —— 每步真调 CLI、逐步核对预期，不手拼",
        f"测试证据：{tests_line()}",
        "",
    ]
    body: list[str] = []
    for index, (title, text) in enumerate(sections, start=1):
        body.append(f"### {index}. {title}")
        body.append(text)
        body.append("")
    return "\n".join(header + body).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    global DB, OUT
    parser = argparse.ArgumentParser(description="重生成 data/build/实际运行结果.txt")
    parser.add_argument(
        "--dry-run", action="store_true", help="在 _scratch/ 下跑一遍并打印，不碰正式证据与正式库"
    )
    parser.add_argument("--out", default=str(ROOT / OUT / "实际运行结果.txt"))
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    scratch = ROOT / "_scratch" / "evidence"
    if args.dry_run:
        # dry-run 不能把正式演示库删了：改在 _scratch 下建一份一模一样的。
        DB = "_scratch/evidence/demo.db"
        OUT = "_scratch/evidence"

    print("=== 准备：校验数据指纹并重置演示库 ===")
    try:
        reset_db()
        print("\n=== 按演示顺序跑 21 节 ===")
        sections = build_sections()
    except EvidenceError as exc:
        print(f"\n[FAIL] 证据未生成：{exc}")
        if args.dry_run:
            shutil.rmtree(scratch, ignore_errors=True)
        return 1

    text = render(sections)

    if args.dry_run:
        print(text)
        shutil.rmtree(scratch, ignore_errors=True)
        print("[dry-run] 正式证据与正式演示库都没有动")
        return 0

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(target.parent), delete=False, suffix=".tmp"
    ) as handle:
        handle.write(text)
        temp_name = handle.name
    os.replace(temp_name, target)  # 原子替换：不会留下半份证据

    print(f"\n[OK] 已写入 {target}（{len(text.splitlines())} 行，{len(sections)} 节）")
    # 生成清单：记下"这次跑出来的四份证据 + 这一版生成器"的摘要。
    # 门禁靠它判断证据有没有被手改、生成器有没有换过；清单本身也是程序写的。
    manifest = evidence_manifest.write(ROOT)
    print(f"[OK] 证据生成清单 -> {manifest.relative_to(ROOT)}"
          f"（{len(evidence_manifest.EVIDENCE_RELS)} 份证据的摘要，供 tools/gate.py 核对）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
