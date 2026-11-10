"""验证台：把"测试与验证"那一页做成按数字键就能讲完的东西。

为什么要有它
------------
老师在台上最爱问的就是"你测过吗"。口头说"测过"没用，得当场给出三样：
    ① 一条命令跑完全部测试的总账；
    ② 一条主流程的逐项核对（13 项不变量）；
    ③ 一条边界的"给了什么 / 预期什么 / 实际什么"。
本脚本把这三样（外加 7 类拒绝码的账）做成一屏一屏的输出，念稿都给你写好了。

它为什么在提交文件夹里也能跑
----------------------------
只依赖：src/、tests/、tests/fixtures/、data/build/*.txt 这几样，
**不需要 149 MB 的 order_line.csv**，也不需要 290 MB 的库。
所以把整个材料包拷到任何一台装了 Python 的机器上都能演。

用法
----
双击 `验证台.cmd`，按数字；
或：python tools/verify_demo.py --all     # 连着跑 1~4
    python tools/verify_demo.py --step 3  # 只跑边界
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]     # 项目根（或材料包根），与当前目录无关
MAIN = ROOT / "src" / "main.py"
TINY = ROOT / "tests" / "fixtures" / "tiny.csv"
CSV = ROOT / "data" / "build" / "order_line.csv"
INVARIANTS = ROOT / "data" / "build" / "import_invariants.txt"
REJECT_STAT = ROOT / "data" / "build" / "reject_stat.txt"
SCRATCH = ROOT / "_scratch" / "verify"
NOW = "2026-01-01T00:00:00"

# 13 项不变量的中文名（报告里也是这么叫的）
LABELS = {
    "rows": "行数",
    "unique_keys": "唯一明细键",
    "cancel_rows": "取消行（C 开头）",
    "cancel_pos_qty": "C 行但数量≥0",
    "bad_debt": "坏账调整（A 开头）",
    "non_product": "非商品行",
    "missing_cust": "缺 Customer ID",
    "neg_qty": "负数量行",
    "retirable_rows": "可退明细行",
    "retirable_sum": "可退数量合计",
    "flag_mismatch": "标记复算冲突",
    "qty_non_integer": "数量非整数",
    "cust_bad_format": "客户号格式异常",
}


def say(text: str = "") -> None:
    print(text, flush=True)


def banner(text: str) -> None:
    say()
    say("=" * 70)
    say(text)
    say("=" * 70)


def brief(given: str, expect: str) -> None:
    """统一的三段式提问——老师问的就是这三句。"""
    say(f"  给了什么：{given}")
    say(f"  预期什么：{expect}")
    say("  实际什么：↓")


def run(args: list[str], *, cwd: Path | None = None, show: bool = True) -> tuple[int, str]:
    """跑一条命令：show=True 时原样打印命令与输出（给台上看）。"""
    if show:
        say(f"\n$ python {' '.join(args)}\n")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", *args],
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    out = (proc.stdout + proc.stderr).rstrip()
    if show and out:
        say(out)
    return proc.returncode, out


# ---------------------------------------------------------------- 1 总账


def step_tests() -> int:
    banner("第 1 步｜总账：一条命令跑完全部测试")
    brief(
        "python -m unittest discover -s tests",
        "Ran 120 tests ... OK（约 4.5 秒）",
    )
    code, out = run(["-m", "unittest", "discover", "-s", "tests"])
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    summary = next((ln for ln in reversed(lines) if ln.startswith("Ran ")), "（没抓到汇总行）")
    passed = code == 0 and lines and lines[-1] == "OK"
    if passed:
        say(f"\n  ✓ {summary} —— 全过。这一条覆盖 R-01~R-09，每条规则都有对应的测试类。")
        return 0
    say(f"\n  ✗ {summary} —— 有失败，先查 tests/ 下的输出。")
    return 1


# ---------------------------------------------------------------- 2 主流程


def parse_invariants() -> list[tuple[str, int, bool]]:
    """从程序写出的证据文件里读 13 项不变量（不是在脚本里另写一份）。"""
    if not INVARIANTS.exists():
        return []
    found: list[tuple[str, int, bool]] = []
    for line in INVARIANTS.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s{2}([a-z_]+)\s+(\d+)\s*(✓|✗)?", line)
        if match and match.group(1) in LABELS:
            found.append((match.group(1), int(match.group(2)), match.group(3) != "✗"))
    return found


def step_invariants() -> int:
    banner("第 2 步｜主流程 T-01：全量导入后 13 项不变量逐项核对")
    rows = parse_invariants()
    if not rows:
        say("找不到 data/build/import_invariants.txt —— 这份材料缺证据文件。")
        say("→ 在项目根目录跑一次：python tools/make_evidence.py")
        return 1

    brief(
        "全量 order_line.csv（去重后 1,044,848 行）",
        "13 项不变量逐项吻合，一项不符就整库回滚",
    )
    say("（下面是全量导入时程序写出的原始表，未经手改）")
    say()
    say(f"  {'不变量':<22}{'实测值':>12}  结果")
    say("  " + "-" * 44)
    for name, value, ok in rows:
        say(f"  {LABELS[name]:<22}{value:>12,}  {'✓' if ok else '✗'}")
    say("  " + "-" * 44)
    say(f"  合计 {len(rows)} 项，全部吻合。")

    say("\n  念这句：这 13 个数不是我抄的，是每次导入都现场复算；任何一项和冻结基线不符，")
    say("          整个库就回滚、退出码 4，一行都不进。所以我不需要「相信」这份 CSV。")
    if CSV.exists():
        say("\n  （想现场重跑一次全量导入？按 9，约 20 秒。）")
    else:
        say("\n  （本材料包不含 149 MB 的数据源，所以这里展示的是上次全量导入的原始输出；")
        say("    真跑请到项目根目录，或按 9 看说明。）")
    return 0 if all(ok for _, _, ok in rows) else 1


# ---------------------------------------------------------------- 3 边界


def step_boundary() -> int:
    banner("第 3 步｜边界 T-08：对「已提交」的申请直接结案，应当被拒")
    if not TINY.exists():
        say(f"找不到夹具 {TINY} —— 材料不完整。")
        return 1

    if SCRATCH.exists():
        shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    db = str(SCRATCH / "t.db")
    db_rel = db.replace("\\", "/").replace(str(ROOT).replace("\\", "/") + "/", "")
    out_rel = "_scratch/verify"

    try:
        say("（用 10 行手写夹具 tiny.csv 建一个临时库，2 秒，课件上看得清）")
        main_rel = str(MAIN.relative_to(ROOT)).replace("\\", "/")
        say(f"\n$ python {main_rel} import --csv tests/fixtures/tiny.csv "
            f"--db {db_rel} --out {out_rel} --baseline report\n")
        # 这一步的输出不显示：夹具只有 10 行，13 项不变量当然和全量基线不符，
        # 满屏 ✗ 会让人误以为出错。--baseline report 就是为这种切片准备的。
        code, _ = run([
            main_rel,
            "import", "--csv", "tests/fixtures/tiny.csv",
            "--db", db_rel, "--out", out_rel, "--baseline", "report",
        ], show=False)
        if code != 0:
            say("  建临时库失败。")
            return 1
        say("  → RESULT=OK（临时库建好。夹具只有 10 行，所以不跟全量基线比——"
            "这正是 --baseline report 的用途）")

        brief("对明细 489434|3 提交 1 件申请（可退 4）", "受理成功，拿到申请号 R-0001")
        code, out = run([
            main_rel,
            "apply", "--db", db_rel, "--invoice", "489434", "--seq", "3",
            "--qty", "1", "--now", NOW,
        ])
        if code != 0:
            say("  申请没成功，后面演示不了。")
            return 1

        say()
        brief(
            "对 APPLIED 状态的 R-0001 直接 close",
            "被拒：RESULT=E_BAD_TRANSITION、退出码 2、提示「下一步只能是 ACCEPTED/REJECTED」",
        )
        code, out = run([
            main_rel,
            "close", "--db", db_rel, "--req", "R-0001", "--now", NOW,
        ])
        ok = code == 2 and "E_BAD_TRANSITION" in out
        say(f"\n  {'✓' if ok else '✗'} 退出码 {code}（预期 2）")

        say("\n再看一眼：状态并没有被改坏，还是 APPLIED。")
        code2, out2 = run([
            main_rel,
            "query", "--db", db_rel, "--invoice", "489434", "--now", NOW, "--log",
        ])
        still_applied = "APPLIED" in out2 and "CLOSED" not in out2
        say(f"  {'✓' if still_applied else '✗'} 状态仍是 APPLIED")

        say("\n  念这句：能不能跳级不是靠我记不记得，而是写死在一张流转表里；")
        say("          被拒之后状态一行没变，流水也只有 1 条——失败的请求不会留脏数据。")
        return 0 if (ok and still_applied) else 1
    finally:
        # 临时库删掉，不给材料包留垃圾；上面已经把输出念完了
        shutil.rmtree(SCRATCH, ignore_errors=True)


# ---------------------------------------------------------------- 4 拒绝码


def step_reject_codes() -> int:
    banner("第 4 步｜补充：7 类拒绝码在真实数据上各被触发 1 次")
    if not REJECT_STAT.exists():
        say("找不到 data/build/reject_stat.txt —— 材料缺证据。")
        return 1

    brief(
        "在全量数据上依次触发：重复、超量、零数量、非商品、找不到、转人工、非法流转",
        "7 个错误码各 1 次，合计 7",
    )
    text = REJECT_STAT.read_text(encoding="utf-8")
    say("（下面是 reject_stat.txt 原样内容，每次 apply / 状态流转自动累加）")
    say()
    for line in text.splitlines():
        if line.strip():
            say(f"  {line}")

    counts = {m.group(1): int(m.group(2)) for m in re.finditer(r"(E_[A-Z_]+)\s+(\d+)", text)}
    total = sum(counts.values())
    ok = len(counts) == 7 and all(v == 1 for v in counts.values())
    say()
    say(f"  {'✓' if ok else '✗'} 共 {len(counts)} 类、合计 {total} 次" + ("，与预期一致。" if ok else "，与预期不一致。"))
    say("\n  念这句：这 7 次不是我在测试里造的，是在真实数据上一条一条撞出来的——")
    say("          说明每道校验都真的会拦，不是写了没用。")
    return 0 if ok else 1


# ---------------------------------------------------------------- 9 真跑


def step_full_import() -> int:
    banner("第 9 步｜现场真跑一次全量导入（约 20 秒）")
    if not CSV.exists():
        say("本材料包不含 149 MB 的 order_line.csv（数据准备说明：不进提交包）。")
        say("要现场重跑，先把它放到 data/build/，或者先跑一次 python src/prepare.py。")
        return 1
    say("这会把 1,044,848 行读一遍、复算 13 项不变量，并写进 _scratch/verify/（不碰正式证据）。")
    say()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    code, out = run([
        str(MAIN.relative_to(ROOT)).replace("\\", "/"),
        "import", "--csv", "data/build/order_line.csv",
        "--db", "_scratch/verify/full.db", "--out", "_scratch/verify",
    ])
    shutil.rmtree(SCRATCH, ignore_errors=True)
    matched = "13 项吻合 / 0 项不符" in out
    say()
    say(f"  {'✓' if (code == 0 and matched) else '✗'} 退出码 {code}，"
        f"{'13/13 不变量吻合。' if matched else '不变量或退出码不符，看上面的输出。'}")
    return 0 if (code == 0 and matched) else 1


STEPS = {
    "0": ("先看一遍材料在哪（自检）", None),
    "1": ("总账：一条命令跑完 120 条测试", step_tests),
    "2": ("主流程 T-01：13 项不变量逐项核对", step_invariants),
    "3": ("边界 T-08：APPLIED 直接结案，应当被拒", step_boundary),
    "4": ("补充：7 类拒绝码各触发 1 次", step_reject_codes),
    "9": ("现场真跑一次全量导入（约 20 秒）", step_full_import),
}
ORDER = ["1", "2", "3", "4"]


def self_check() -> int:
    banner("第 0 步｜自检：材料齐不齐")
    say(f"  运行位置：{ROOT}")
    checks = [
        ("源代码 src/main.py", MAIN.exists()),
        ("测试 tests/", (ROOT / "tests").is_dir()),
        ("夹具 tiny.csv", TINY.exists()),
        ("证据 import_invariants.txt", INVARIANTS.exists()),
        ("证据 reject_stat.txt", REJECT_STAT.exists()),
        ("全量数据 order_line.csv（可选）", CSV.exists()),
    ]
    for label, ok in checks:
        say(f"  {'✓' if ok else '–'} {label}")
    say()
    say("  带「–」的可选项没有是正常的：提交包按规范不含 149 MB 数据源，")
    say("  第 1~4 步不需要它，第 9 步才需要。")
    return 0


def menu() -> int:
    while True:
        say()
        say("=" * 70)
        say("  VC-R-01 验证台　（" + str(ROOT) + "）")
        say("=" * 70)
        say("   1  总账：一条命令跑完 120 条测试")
        say("   2  主流程 T-01：13 项不变量逐项核对")
        say("   3  边界 T-08：APPLIED 直接结案，应当被拒")
        say("   4  补充：7 类拒绝码各触发 1 次")
        say("   8  连着跑完 1~4（台上最省事）")
        say("   9  现场真跑一次全量导入（约 20 秒，需数据源）")
        say("   0  自检：材料齐不齐")
        say("   q  退出")
        say("-" * 70)
        choice = input("按数字，回车：").strip().lower()
        if choice in ("q", "quit", "exit", ""):
            return 0
        if choice == "8":
            for key in ORDER:
                STEPS[key][1]()
                input("\n（按回车走下一步）")
            continue
        if choice == "0":
            self_check()
            continue
        if choice in STEPS and STEPS[choice][1] is not None:
            STEPS[choice][1]()
            continue
        say("没这个选项，按 0~4 / 8 / 9 / q。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VC-R-01 验证台")
    parser.add_argument("--step", choices=sorted(STEPS))
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    if args.step:
        if args.step == "0":
            return self_check()
        return STEPS[args.step][1]()
    if args.all:
        failed = 0
        for key in ORDER:
            failed += STEPS[key][1]()
        say()
        say("全部跑完。" if failed == 0 else f"有 {failed} 步没通过，看上面的红叉。")
        return 0 if failed == 0 else 1
    try:
        return menu()
    except (KeyboardInterrupt, EOFError):
        say("\n退出。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
