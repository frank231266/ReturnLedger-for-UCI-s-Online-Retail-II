"""演示台：把现场那 7 步做成一按就跑，且**从哪启动、材料包在哪台机器上都不会找错目录**。

为什么要有它
------------
1. 现场的 PowerShell 默认开在 C:\\Users\\11049（用户目录），不在项目里，
   直接敲 `copy data\\build\\...` 会报"路径不存在"。本脚本用 __file__ 反推根目录。
2. 7 条命令里有三处最容易敲错：`--req R-0004`、`--now 2026-10-18T14:00:00`、
   以及每行都要写的 `--db data/build/demo/demo.db`。这里替你背下来。
3. 每一步都会**先把命令原样打印出来再执行** —— 台上看到的是真命令，不是黑盒。

两种数据模式（自动判断）
------------------------
- **全量模式**：`data/build/return_system.db` 在 → 直接复制一份来演示，
  用发票 536809（原始 1824、历史已退 4 → 可退 1820），和讲稿上的数字一致。
- **切片模式**：没有全量库（例如把提交材料包拷到别的机器）→ 用包内
  `data/build/sample_2000.csv`（2,000 行真实切片）现场建库，约 1 秒，
  然后自动挑一条可退明细来演。**数字会不同，这是正常的**，全量数字见
  `data/build/实际运行结果.txt`。

用法
----
双击 `演示台.cmd`，按菜单选数字；
或：python tools/demo.py --all / --step 3
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]       # 项目根/材料包根，与当前目录无关
MAIN = ROOT / "src" / "main.py"
SRC_DB = ROOT / "data" / "build" / "return_system.db"
SAMPLE = ROOT / "data" / "build" / "sample_2000.csv"
CSV = ROOT / "data" / "build" / "order_line.csv"
XLSX = ROOT / "data" / "raw" / "online_retail_II.xlsx"
INVARIANTS = ROOT / "data" / "build" / "import_invariants.txt"
DEMO_DIR = ROOT / "data" / "build" / "demo"
DEMO_DB = DEMO_DIR / "demo.db"
SCRATCH = ROOT / "_scratch"

NOW = "2026-10-18T14:00:00"   # 演示时钟：不能用 10:00，会撞上证据库里那条申请的幂等键
DB_ARG = ["--db", "data/build/demo/demo.db"]   # 必须是两个元素，拼成一个字符串会被当成一个参数


@dataclass
class Plan:
    mode: str          # 全量 / 切片
    invoice: str
    seq: int
    qty: int
    returnable: int
    history: int
    retirable: int
    req_no: str
    note: str = ""


CURRENT: Plan | None = None


def say(text: str = "") -> None:
    # flush 是必须的：子进程（main.py）直接写句柄，父进程若被缓冲，两边输出会错位。
    print(text, flush=True)


def banner(text: str) -> None:
    say()
    say("=" * 68)
    say(text)
    say("=" * 68)


def run_cli(args: list[str], *, show: bool = True) -> int:
    if show:
        say(f"\n$ python src/main.py {' '.join(args)}\n")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(MAIN), *args], cwd=str(ROOT)
    )
    return proc.returncode


def run_cli_quiet(args: list[str]) -> tuple[int, str]:
    """跑一条命令但不把输出打出来 —— 用于"建切片库"这种过程性步骤：
    切片只有 2,000 行，13 项不变量当然和全量基线不符，满屏 ✗ 会让人误以为出错。"""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(MAIN), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


def parse_import_facts(path: Path) -> dict[str, str]:
    """从 import_invariants.txt 里把三件事读出来：行数、耗时、13 项打勾情况。

    这份文件是导入时由程序写出的，演示时读它 = 展示"程序当时记下的账"，
    不是脚本里另写一份数字。
    """
    facts: dict[str, str] = {}
    if not path.exists():
        return facts
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("读取行数"):
            facts["rows"] = line.split()[-1]
        elif line.startswith("总耗时"):
            facts["seconds"] = line.split()[-1]
        elif line.startswith("导入不变量"):
            match = re.search(r"(\d+)\s*项吻合\s*/\s*(\d+)\s*项不符", line)
            if match:
                facts["matched"] = match.group(1)
                facts["unmatched"] = match.group(2)
    facts["ticks"] = str(text.count("✓"))
    return facts


def show_import_facts(heading: str) -> None:
    """把"全量导入 1,044,848 行 / 约 19 秒 / 13 项 13/13"摆到屏幕上。"""
    facts = parse_import_facts(INVARIANTS)
    say()
    say(f"  {heading}")
    if not facts.get("rows"):
        say("  （找不到 data/build/import_invariants.txt，跳过）")
        return
    rows = f"{int(facts['rows']):,}"
    say(f"    读取行数      {rows} 行")
    say(f"    导入耗时      {facts.get('seconds', '?')}")
    say(f"    13 项不变量   {facts.get('matched', '?')} 项吻合 / {facts.get('unmatched', '?')} 项不符")
    say("    来源          data/build/import_invariants.txt（导入时程序写出，未手改）")
    say("    去重口径      1,067,371 − 22,523 = 1,044,848")
    say("  念这句：这不是我说它跑了 100 万行，是程序每次导入自己数出来的；")
    say("          13 项里任何一项跟冻结基线不符，整库回滚、退出码 4，一行都不进。")


def next_req_no(db: Path) -> str:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(req_no, 3) AS INTEGER)), 0)"
            " FROM return_request WHERE req_no LIKE 'R-%'"
        ).fetchone()
    finally:
        conn.close()
    return f"R-{int(row[0]) + 1:04d}"


def pick_line(db: Path) -> tuple[str, int, int, int] | None:
    """挑一条"客户号在、不是取消/坏账/非商品、还有余额"的明细。"""
    conn = sqlite3.connect(str(db))
    try:
        for extra in (" AND history_returned = 0", ""):
            row = conn.execute(
                "SELECT invoice, seq, returnable_qty, history_returned FROM order_line"
                " WHERE cust_id IS NOT NULL AND is_cancel = 0 AND is_non_product = 0"
                "   AND is_bad_debt = 0 AND returnable_qty > 2" + extra +
                " ORDER BY invoice, seq LIMIT 1"
            ).fetchone()
            if row is not None:
                return str(row[0]), int(row[1]), int(row[2]), int(row[3])
    finally:
        conn.close()
    return None


def reset() -> int:
    global CURRENT
    banner("第 0 步｜复位演示库（每次都先按这个）")

    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        stale = DEMO_DB.with_name(DEMO_DB.name + suffix)
        if stale.exists():
            stale.unlink()

    if SRC_DB.exists():
        shutil.copy2(SRC_DB, DEMO_DB)
        say("$ copy data\\build\\return_system.db data\\build\\demo\\demo.db")
        CURRENT = Plan(
            mode="全量",
            invoice="536809", seq=1, qty=10,
            returnable=1824, history=4, retirable=1820,
            req_no=next_req_no(DEMO_DB),
        )
        say(f"\n模式：全量数据（本次演示用的就是这份库）　本次申请号预计：{CURRENT.req_no}")
        show_import_facts("全量导入事实：")
        return 0

    if not SAMPLE.exists():
        say("既没有 data/build/return_system.db，也没有 data/build/sample_2000.csv。")
        say("→ 在项目根跑：python tools/make_evidence.py（会重建库，约 30 秒）")
        return 1

    say("没有全量库（提交包按规范不含 149 MB 数据源），改用包内 2,000 行真实切片建库。")
    say("\n$ python src/main.py import --csv data/build/sample_2000.csv "
        "--db data/build/demo/demo.db --out data/build/demo --baseline report\n")
    code, _ = run_cli_quiet([
        "import", "--csv", "data/build/sample_2000.csv",
        *DB_ARG, "--out", "data/build/demo", "--baseline", "report",
    ])
    if code != 0:
        say("  切片建库失败。")
        return 1
    say("  → RESULT=OK（切片只有 2,000 行，所以不跟全量基线比——这正是 --baseline report 的用途）")

    picked = pick_line(DEMO_DB)
    if picked is None:
        say("  切片里没找到可演示的明细（不太可能）。")
        return 1
    invoice, seq, returnable, history = picked
    retirable = returnable - history
    CURRENT = Plan(
        mode="切片",
        invoice=invoice, seq=seq,
        qty=min(3, max(1, retirable)),
        returnable=returnable, history=history, retirable=retirable,
        req_no=next_req_no(DEMO_DB),
        note="本次用的是 2,000 行切片，数字与全量不同；全量数字见 实际运行结果.txt",
    )
    say(f"  演示明细：{invoice}|{seq}（原始 {returnable}、历史已退 {history}"
        f" → 可退 {retirable}）　本次申请号预计：{CURRENT.req_no}")
    say("  提示：这是切片数据，数字和讲稿里的 1820 不同——被问到就照实说，"
        "并翻 实际运行结果.txt 给他看全量。")
    show_import_facts("全量导入事实（本包没有 149 MB 数据源，下面是上次全量导入的账）：")
    return 0


def need_plan() -> Plan | None:
    if CURRENT is None:
        say("\n还没复位。先按 0（复位演示库）。")
        return None
    return CURRENT


def step_query() -> int:
    plan = need_plan()
    if plan is None:
        return 1
    banner("第 1 步｜查一张能心算的发票")
    code = run_cli(["query", *DB_ARG, "--invoice", plan.invoice, "--now", NOW])
    say(f"\n念这句：这条明细原始 {plan.returnable}、历史上退过 {plan.history}、现在没人占，"
        f"所以还能退 {plan.retirable}。{plan.returnable} − {plan.history} = {plan.retirable}。")
    return code


def step_apply() -> int:
    plan = need_plan()
    if plan is None:
        return 1
    banner(f"第 2 步｜提交申请 {plan.qty} 件")
    code = run_cli([
        "apply", *DB_ARG, "--invoice", plan.invoice, "--seq", str(plan.seq),
        "--qty", str(plan.qty), "--operator", "售后-张", "--reason", "尺寸不符",
        "--now", NOW,
    ])
    say(f"\n念这句：申请 {plan.qty} 件，系统给申请号 {plan.req_no}，"
        f"可退从 {plan.retirable} 变成 {plan.retirable - plan.qty}。")
    return code


def step_accept() -> int:
    plan = need_plan()
    if plan is None:
        return 1
    banner("第 3 步｜受理")
    code = run_cli([
        "accept", *DB_ARG, "--req", plan.req_no, "--operator", "客服主管-李",
        "--note", "电话核实后受理", "--now", NOW,
    ])
    say("\n念这句：主管受理了。注意，受理之后额度还占着，不能直接结案。")
    return code


def step_illegal_close() -> int:
    plan = need_plan()
    if plan is None:
        return 1
    banner("第 4 步｜故意跳级结案（应当被拒，全场最值钱的一条）")
    code = run_cli(["close", *DB_ARG, "--req", plan.req_no, "--now", NOW])
    if code == 2:
        say("\n念这句：我想直接结案——被拒了，系统告诉我下一步只能是 RECEIVED。"
            "能不能跳级不是靠我记得，而是写死在一张流转表里。")
    else:
        say(f"\n[注意] 预期退出码 2，实际 {code}。检查第 0~3 步是不是按顺序做的。")
    return code


def step_receive() -> int:
    plan = need_plan()
    if plan is None:
        return 1
    banner("第 5 步｜收货")
    code = run_cli([
        "receive", *DB_ARG, "--req", plan.req_no, "--operator", "仓库-王",
        "--note", "质检通过", "--now", NOW,
    ])
    say("\n念这句：收货。下一步终于可以结案了。")
    return code


def step_close() -> int:
    plan = need_plan()
    if plan is None:
        return 1
    banner("第 6 步｜结案")
    code = run_cli([
        "close", *DB_ARG, "--req", plan.req_no, "--operator", "客服主管-李",
        "--note", "退款已处理", "--now", NOW,
    ])
    say(f"\n念这句：结案，额度回到 {plan.retirable}——只有结案才释放。")
    show_trajectory()
    return code


def step_log() -> int:
    plan = need_plan()
    if plan is None:
        return 1
    banner("第 7 步｜回查流水")
    code = run_cli(["query", *DB_ARG, "--invoice", plan.invoice, "--log", "--now", NOW])
    say("\n念这句：四步流转都在流水里，谁做的、什么时候、为什么，都查得到。")
    if plan.mode == "全量":
        say("会看到 R-0001 和 " + plan.req_no + " 两条都 CLOSED —— 这是对的：同一明细历史上退过两次，")
        say("只要前一条已经结案，就可以再申请。")
    else:
        say(f"这里只有刚才那一条 {plan.req_no}。")
    return code


def run_py(script_rel: str, args: list[str]) -> tuple[int, str]:
    """跑一个项目内脚本并捕获输出（用于 prepare.py 这种话很多的步骤）。"""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / script_rel), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


def show_trajectory() -> None:
    """结案后当场从库里算一遍额度轨迹，把"1820 → 1810 → 1820"钉成实测值。"""
    plan = CURRENT
    if plan is None:
        return
    conn = sqlite3.connect(str(DEMO_DB))
    try:
        row = conn.execute(
            "SELECT line_key, returnable_qty, history_returned FROM order_line"
            " WHERE invoice = ? AND seq = ?", (plan.invoice, plan.seq)
        ).fetchone()
        if row is None:
            return
        line_key, returnable, history = row
        active = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) FROM return_request WHERE line_key = ?"
            " AND status IN ('APPLIED','ACCEPTED','RECEIVED')", (line_key,)
        ).fetchone()[0]
    finally:
        conn.close()

    now_retirable = max(0, int(returnable) - int(history) - int(active))
    closed = now_retirable == plan.retirable
    say()
    say("  额度轨迹（结案后当场从库里重算，不是背的）：")
    say(f"    申请前            {plan.retirable}")
    say(f"    申请 {plan.qty} 件占用后     {plan.retirable - plan.qty}")
    say(f"    结案后            {now_retirable}"
        + ("   ← 回到起点，闭环 ✓" if closed else "   ← 没回到起点，检查一下 ✗"))
    say("  念这句：额度是跟着状态走的——结案那一刻才真正释放，")
    say("          所以同一条明细可以被退多次，但同一时间只能占一次。")


def step_facts() -> int:
    banner("导入事实：行数 / 耗时 / 13 项不变量")
    show_import_facts("全量导入事实（读程序写出的账）：")
    return 0


def step_full_import() -> int:
    banner("现场真跑一次全量导入（约 19 秒）")
    if not CSV.exists():
        if not XLSX.exists():
            say(f"既没有 {CSV.name}，也没有 {XLSX.name}，这台机器真跑不了。")
            say("→ 换到有数据源的机器，或先准备 data/raw/ 下的原始件。")
            return 1
        size = XLSX.stat().st_size / 1024 / 1024
        say(f"没有 {CSV.name}（按规范不进提交包），但原始件在：")
        say(f"  {XLSX.relative_to(ROOT)}（{size:.1f} MB）")
        say("可以先重建 CSV：python src/prepare.py --no-db（约 30~40 秒，会写约 150 MB）")
        say()
        say("要现在重建吗？输入 y 回车继续，直接回车跳过：", )
        try:
            if input().strip().lower() != "y":
                say("已跳过。")
                return 1
        except EOFError:
            say("已跳过。")
            return 1
        say("\n$ python src/prepare.py --no-db\n")
        code, out = run_py("src/prepare.py", ["--no-db"])
        if code != 0:
            say("重建失败，输出末尾：")
            say("\n".join(out.splitlines()[-8:]))
            say("（如果报 openpyxl 缺失：python -m pip install openpyxl）")
            return 1
        say("  → 重建完成：data/build/order_line.csv")
        say("  （顺带生成了 raw_*.csv 与 prepare_stats.txt；不想要就删掉，不影响证据）")

    scratch = SCRATCH / "full"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    say("\n$ python src/main.py import --csv data/build/order_line.csv "
        "--db _scratch/full/demo.db --out _scratch/full\n")
    code, out = run_py("src/main.py", [
        "import", "--csv", "data/build/order_line.csv",
        "--db", "_scratch/full/demo.db", "--out", "_scratch/full",
    ])
    live = parse_import_facts(scratch / "import_invariants.txt")
    shutil.rmtree(scratch, ignore_errors=True)
    if code != 0:
        say("导入失败，输出末尾：")
        say("\n".join(out.splitlines()[-10:]))
        return 1

    say("  刚刚这一次的实测（程序写在临时目录的 import_invariants.txt 里）：")
    say(f"    读取行数      {int(live['rows']):,} 行")
    say(f"    导入耗时      {live['seconds']}")
    say(f"    13 项不变量   {live['ticks']}/13 吻合")
    ok = live.get("ticks") == "13"
    say(f"\n  {'✓' if ok else '✗'} " + ("13 项全部吻合，和证据文件里那次一致。" if ok else "有未吻合项，看上面输出。"))
    say("  （临时库已删，正式证据一个字节没动）")
    return 0 if ok else 1


STEPS = {
    "0": ("复位演示库（全量库或 2,000 行切片，自动判断）", reset),
    "1": ("查询明细", step_query),
    "2": ("提交申请", step_apply),
    "3": ("受理", step_accept),
    "4": ("故意跳级结案（预期被拒）", step_illegal_close),
    "5": ("收货", step_receive),
    "6": ("结案", step_close),
    "7": ("回查流水", step_log),
    "9": ("导入事实：行数 / 耗时 / 13 项", step_facts),
    "r": ("现场真跑一次全量导入", step_full_import),
}
ORDER = ["1", "2", "3", "4", "5", "6", "7"]


def menu() -> int:
    while True:
        say()
        say("=" * 68)
        say("  VC-R-01 演示台　（" + str(ROOT) + "）")
        say("=" * 68)
        say("   0  复位演示库（每次演示前先按这个）")
        for key in ORDER:
            say(f"   {key}  {STEPS[key][0]}")
        say("   8  从复位开始，连着跑完 1~7（省事）")
        say("   9  导入事实：1,044,848 行 / 约 19 秒 / 13 项 13/13（读证据，秒出）")
        say("   r  现场真跑一次全量导入（约 19 秒；没数据源会问你要不要重建）")
        say("   q  退出")
        say("-" * 68)
        choice = input("按数字，回车：").strip().lower()
        if choice in ("q", "quit", "exit", ""):
            return 0
        if choice == "8":
            if reset() != 0:
                continue
            for key in ORDER:
                STEPS[key][1]()
                input("\n（按回车走下一步）")
            say("\n演完了。要再演一遍，按 0 复位。")
            continue
        if choice in STEPS:
            STEPS[choice][1]()
            continue
        say("没这个选项，按 0~9 / r / q。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VC-R-01 演示台")
    parser.add_argument("--step", choices=sorted(STEPS), help="只跑某一步（排练用）")
    parser.add_argument("--all", action="store_true", help="复位后连着跑完 1~7")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    if args.step:
        if args.step == "0":
            return reset()
        return 0 if STEPS[args.step][1]() in (0, 2) else 1
    if args.all:
        if reset() != 0:
            return 1
        for key in ORDER:
            STEPS[key][1]()
        return 0
    try:
        return menu()
    except (KeyboardInterrupt, EOFError):
        say("\n退出。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
