"""清理台：把三个台（演示台 / 验证台 / 修正台）跑出来的东西删回去，回到"刚交上去"的状态。

哪些是跑出来的（白名单，只删这些）
----------------------------------
    1. _scratch/               三个台的临时目录（临时库、改坏的生成器副本）
    2. data/build/demo/        演示台复制或新建的演示库
    3. tests/.tmp/             单元测试的临时库
    4. __pycache__/            所有 Python 字节码缓存

哪些绝对不能碰（黑名单，删前会自检）
------------------------------------
    data/build/实际运行结果.txt、import_invariants.txt、reject_stat.txt、
    发票对照20张.txt、prepare_stats.txt、sample_2000.csv
    data/raw/ 下的原始件
    src/、tests/*.py、tools/*.py、所有 .md / .docx / .pptx
    .gates/（门禁标记，删了 gate check 会失败）

另外单独一项（可选，不属于回退）：数据准备阶段的大件 order_line.db / raw_*.csv /
order_line_filtered.csv。它们不是三个台生成的，是 prepare.py 的产物，能重建；
删它们只是为了腾空间，需要输入 yes 才动。

用法
----
双击 `清理台.cmd`；或 python tools/reset_state.py --dry-run / --all
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "data" / "build"

# 跑出来的东西：只有这些会被"回退"删除
TARGETS: list[tuple[Path, str]] = [
    (ROOT / "_scratch", "三个台的临时目录（临时库 / 改坏的生成器副本）"),
    (BUILD / "demo", "演示台复制或新建的演示库"),
    (ROOT / "tests" / ".tmp", "单元测试的临时库"),
]

# 可以腾空间、但不属于"回退"的大件（需要输入 yes）
BIG_REBUILDABLE: list[tuple[Path, str]] = [
    (BUILD / "order_line.db", "prepare.py 顺手建的库（本系统不读它，规则只用 CSV）"),
    (BUILD / "raw_Year_2009-2010.csv", "第一个工作表原样转存（可重建）"),
    (BUILD / "raw_Year_2010-2011.csv", "第二个工作表原样转存（可重建）"),
    (BUILD / "order_line_filtered.csv", "切片实验的产物（可重建）"),
]

# 提交材料：回退后必须还在
MUST_KEEP: list[tuple[Path, str]] = [
    (BUILD / "实际运行结果.txt", "21 节重跑证据"),
    (BUILD / "import_invariants.txt", "13 项不变量"),
    (BUILD / "reject_stat.txt", "7 类拒绝码计数"),
    (BUILD / "发票对照20张.txt", "20 张发票对照"),
    (BUILD / "prepare_stats.txt", "数据准备统计"),
    (BUILD / "sample_2000.csv", "单测用的 2,000 行切片"),
    (ROOT / "data" / "raw" / "online_retail_II.xlsx", "原始件（只读）"),
    (ROOT / "src" / "main.py", "主程序"),
    (ROOT / "tests" / "test_rule_matrix.py", "测试源码"),
    (ROOT / "tools" / "make_evidence.py", "证据生成器"),
]


def say(text: str = "") -> None:
    print(text, flush=True)


def banner(text: str) -> None:
    say()
    say("=" * 70)
    say(text)
    say("=" * 70)


def size_of(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def mb(value: int) -> str:
    return f"{value / 1024 / 1024:.1f} MB"


def pycache_dirs() -> list[Path]:
    return [
        p for p in ROOT.rglob("__pycache__")
        if p.is_dir() and ".git" not in p.parts
    ]


def free_space() -> str:
    try:
        return f"{shutil.disk_usage(ROOT).free / 1024 ** 3:.1f} GB"
    except OSError:
        return "读不到"


def survey() -> tuple[list[tuple[Path, str, int]], list[Path], int]:
    """列出要删的东西（名字、说明、大小），以及 __pycache__ 列表。"""
    found: list[tuple[Path, str, int]] = []
    for path, desc in TARGETS:
        if path.exists():
            found.append((path, desc, size_of(path)))
    caches = pycache_dirs()
    cache_size = sum(size_of(p) for p in caches)
    return found, caches, cache_size


def cmd_dry_run() -> int:
    banner("预演：会删什么（不删任何东西）")
    found, caches, cache_size = survey()
    total = 0
    if not found and not caches:
        say("  没有需要清理的：三个台都没跑过（或已经清理过）。")
    for path, desc, size in found:
        total += size
        say(f"  {path.relative_to(ROOT)}")
        say(f"      {desc}　{mb(size)}")
    if caches:
        total += cache_size
        say(f"  __pycache__ × {len(caches)}　Python 字节码缓存　{mb(cache_size)}")
    say()
    say(f"  合计可省：{mb(total)}　当前可用空间：{free_space()}")
    say()
    say("  下面这些不会被处理：")
    for path, desc in MUST_KEEP[:5]:
        say(f"    {path.relative_to(ROOT)}　{desc}")
    say("    ……以及 data/raw/、src/、tests/*.py、所有 .md/.docx/.pptx、.gates/")
    return 0


def cmd_clean(assume_yes: bool = False) -> int:
    banner("回退：删掉三个台生成的临时文件")
    found, caches, cache_size = survey()
    if not found and not caches:
        say("  已经干净了，没有可删的。")
        return 0
    total = sum(size for _, _, size in found) + cache_size
    say(f"  准备删除 {len(found)} 个目录 + {len(caches)} 个 __pycache__，共 {mb(total)}：")
    for path, _, size in found:
        say(f"    - {path.relative_to(ROOT)}　{mb(size)}")
    if caches:
        say(f"    - __pycache__ × {len(caches)}　{mb(cache_size)}")
    say()
    say(f"  删除前可用空间：{free_space()}")
    if not assume_yes:
        say()
        say("  确认删除吗？输入 y 回车；直接回车取消：", )
        try:
            if input().strip().lower() != "y":
                say("  已取消，什么都没删。")
                return 0
        except EOFError:
            say("  已取消，什么都没删。")
            return 0

    leftovers: list[str] = []
    for path, _, _ in found:
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            leftovers.append(str(path.relative_to(ROOT)))
    for cache in caches:
        shutil.rmtree(cache, ignore_errors=True)

    say()
    say(f"  删除后可用空间：{free_space()}")
    if leftovers:
        say(f"  [注意] 有 {len(leftovers)} 个目录没删干净（Windows 权限/被占用）：")
        for item in leftovers:
            say(f"    {item}")
        say("         不影响任何结论；重启后再删，或用资源管理器手动删。")
    else:
        say("  ✓ 删净了。")
    say()
    say("  念这句：这三个台都是「只往里写临时文件」，回退就是把临时文件删掉——")
    say("          证据、数据源、报告一个都不在它们的写入范围内。")
    return 0


def cmd_check() -> int:
    banner("回退后自检：提交材料还在不在")
    missing = 0
    for path, desc in MUST_KEEP:
        ok = path.exists()
        missing += 0 if ok else 1
        size = f"{path.stat().st_size:,} 字节" if ok else "缺失"
        say(f"  {'✓' if ok else '✗'} {str(path.relative_to(ROOT)):<34}{desc:<22}{size}")
    say()
    say("  data/build/ 现在的样子：")
    for item in sorted(BUILD.glob("*")):
        say(f"    {item.name:<28}{mb(size_of(item)) if item.is_dir() else f'{item.stat().st_size:,} 字节'}")
    say()
    if missing:
        say(f"  ✗ 有 {missing} 项不见了 —— 别提交，先用 git restore 或重跑工具补回来。")
    else:
        say("  ✓ 提交材料齐全，可以交。")
    return 0 if missing == 0 else 1


def big_candidates() -> list[tuple[Path, str]]:
    """可重建的大件。order_line.csv 只在「看起来是材料包」时才提供删除选项：
    项目根目录有 return_system.db，那份 CSV 是命根子，不给删。"""
    items = [(path, desc) for path, desc in BIG_REBUILDABLE if path.exists()]
    if not (BUILD / "return_system.db").exists() and (BUILD / "order_line.csv").exists():
        items.insert(0, (
            BUILD / "order_line.csv",
            "按 r 重建出来的数据源（约 142 MB，要演示再跑一次 prepare.py）",
        ))
    return items


def cmd_big() -> int:
    banner("可选：删掉可重建的大件（不属于回退）")
    say("  这些不是三个台生成的，是 prepare.py 的产物；删了能腾空间，但要用的时候得重建。")
    say()
    total = 0
    present = big_candidates()
    for path, desc in present:
        size = size_of(path)
        total += size
        say(f"    {str(path.relative_to(ROOT)):<40}{mb(size):>10}  {desc}")
    if not present:
        say("  （都已经不在了）")
        return 0
    say()
    say(f"  合计可省 {mb(total)}。删除后若要演示，先跑 python tools/make_evidence.py 或 src/prepare.py 重建。")
    say()
    say("  这是不可逆操作。确认请完整输入 yes 回车，其他任何输入都取消：", )
    try:
        if input().strip().lower() != "yes":
            say("  已取消。")
            return 0
    except EOFError:
        say("  已取消。")
        return 0
    for path, _ in present:
        path.unlink(missing_ok=True)
    say(f"  已删除，当前可用空间：{free_space()}")
    return 0


def menu() -> int:
    while True:
        say()
        say("=" * 70)
        say("  VC-R-01 清理台（回退到提交状态）　" + str(ROOT))
        say("=" * 70)
        say("   1  预演：列出会删什么、能省多少（不删）")
        say("   2  回退：删掉三个台生成的临时文件（需确认）")
        say("   3  回退后自检：提交材料还在不在")
        say("   4  可选：删可重建的大件（输入 yes，腾空间）")
        say("   q  退出")
        say("-" * 70)
        choice = input("按数字，回车：").strip().lower()
        if choice in ("q", "quit", "exit", ""):
            return 0
        if choice == "1":
            cmd_dry_run()
        elif choice == "2":
            cmd_clean()
        elif choice == "3":
            cmd_check()
        elif choice == "4":
            cmd_big()
        else:
            say("没这个选项，按 1~4 或 q。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VC-R-01 清理台")
    parser.add_argument("--dry-run", action="store_true", help="只列清单，不删")
    parser.add_argument("--all", action="store_true", help="直接回退并自检（不问）")
    parser.add_argument("--yes", action="store_true", help="回退时不问确认")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    if args.dry_run:
        return cmd_dry_run()
    if args.all:
        code = cmd_clean(assume_yes=True)
        return cmd_check() if code == 0 else code
    if args.yes:
        return cmd_clean(assume_yes=True)
    try:
        return menu()
    except (KeyboardInterrupt, EOFError):
        say("\n退出。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
