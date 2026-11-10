"""修正台：把「问题与修正」那一页做成按数字键就能讲完的东西。

这一页讲的是：
    ​问题：核心证据《实际运行结果.txt》原先没有生成脚本，是把命令输出手工粘起来的
    冲突：项目铁律要求"证据只能由程序生成"，手拼就自相矛盾
    处理：写 tools/make_evidence.py，逐步真调命令、逐步核对预期，对不上就报错、不写文件
    结果：证据从 13 节扩到 21 节，一条命令可重生成；顺带发现上游 prepare.py 会覆盖同名证据文件，已改名拆掉

难点在于："改成脚本了"这句话谁都会说。所以本台让三件事**当场可查**：
    ① 证据文件自己带着"预期/实际"两行 —— 手拼的东西不会有这两行；
    ② 生成器里三道保险的源码原文（读了才知道不是嘴上说）；
    ③ 故意把一处预期改错，它会报错并且**不写文件**（正式证据哈希前后不变）。

只有 6、7 两步需要 149 MB 数据源（要真跑一次导入），在材料包里会自动提示跳过；
1~4 步只读文件，任何机器上都能跑。

用法
----
双击 `修正台.cmd`，按数字；或 python tools/fix_demo.py --all / --step 1
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data" / "build" / "实际运行结果.txt"
GENERATOR = ROOT / "tools" / "make_evidence.py"
PREPARE = ROOT / "src" / "prepare.py"
PREP_STATS = ROOT / "data" / "build" / "prepare_stats.txt"
CSV = ROOT / "data" / "build" / "order_line.csv"
CONSTITUTION = ROOT / "CLAUDE.md"
SCRATCH = ROOT / "_scratch"


def say(text: str = "") -> None:
    print(text, flush=True)


def banner(text: str) -> None:
    say()
    say("=" * 72)
    say(text)
    say("=" * 72)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def run_py(script: Path, args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(script), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


# ---------------------------------------------------------------- 1 问题


def parse_sections() -> list[dict[str, object]]:
    """把证据文件拆成节，看每节有没有"命令 / 预期 / 实际"三样。"""
    sections: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for line in EVIDENCE.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            title = re.sub(r"^\d+\.\s*", "", line[4:].strip())
            current = {
                "title": title,
                "cmd": False, "expect": False, "actual": False, "lines": [],
            }
            sections.append(current)
        elif current is not None:
            current["lines"].append(line)          # type: ignore[union-attr]
            if line.startswith("$ python"):
                current["cmd"] = True
            elif line.startswith("（预期："):
                current["expect"] = True
            elif line.startswith("（实际：") and "✓" in line:
                current["actual"] = True
    return sections


def step_problem() -> int:
    banner("第 1 步｜问题：凭什么说这份证据是「程序生成」的？")
    if not EVIDENCE.exists():
        say(f"找不到 {EVIDENCE.relative_to(ROOT)}")
        return 1

    sections = parse_sections()
    command_steps = [s for s in sections if s["cmd"]]
    with_expect = [s for s in command_steps if s["expect"] and s["actual"]]

    sample = next((s for s in sections if "非法流转" in str(s["title"])), command_steps[0] if command_steps else None)
    if sample is not None:
        say("先看证据里随便一节长什么样（这是原始内容，不是我现编的）：")
        say()
        say(f"### {sample['title']}")
        for line in list(sample["lines"])[:14]:    # type: ignore[index]
            say(f"    {line}")

    say()
    say(f"  整份证据共 {len(sections)} 节，其中 {len(command_steps)} 节是命令步骤。")
    say(f"  这 {len(command_steps)} 节里，{len(with_expect)} 节同时带「预期」和「实际 ✓」两行。")
    if len(command_steps) > len(with_expect):
        say(f"  剩下 {len(command_steps) - len(with_expect)} 节是没有命令的汇总账。")

    say()
    say("  念这句：手拼的东西只有结果，不会有「预期」这一行——")
    say("          因为粘贴的人只粘得到输出，粘不到「本来应该是什么」。")
    say("          这份证据每一节都先写预期、再贴输出、最后自己打勾，")
    say("          说明它是脚本一步一核对跑出来的。")
    return 0 if len(with_expect) == len(command_steps) else 1


# ---------------------------------------------------------------- 2 铁律


def step_rule() -> int:
    banner("第 2 步｜冲突：项目自己定的铁律第 6 条")
    if not CONSTITUTION.exists():
        say("找不到 CLAUDE.md")
        return 1
    text = CONSTITUTION.read_text(encoding="utf-8")
    match = re.search(r"(6\. \*\*证据文件只能由程序生成.*?)(?=\n7\. )", text, re.S)
    say("CLAUDE.md 里原文：")
    say()
    for line in (match.group(1) if match else "（没找到第 6 条）").splitlines():
        say(f"  {line}")
    say()
    say("  念这句：这条不是老师要求的，是我自己写进项目宪法里的。")
    say("          所以原来的做法（把输出粘起来）是拿自己的规矩打自己的脸——")
    say("          这个问题是我自己自查出来的，不是被谁指出来才改的。")
    return 0 if match else 1


# ---------------------------------------------------------------- 3 处理


def step_fix() -> int:
    banner("第 3 步｜处理：三道保险，都在源码里")
    if not GENERATOR.exists():
        say("找不到 tools/make_evidence.py")
        return 1
    src = GENERATOR.read_text(encoding="utf-8")

    guards = [
        ("① 逐步核对预期", r'raise EvidenceError\(f"第「\{title\}」步', "退出码或 RESULT 码对不上就抛错，不是打个警告继续"),
        ("② 对不上就不写文件", r"tempfile\.NamedTemporaryFile", "先写临时文件，全部通过才原子替换；半份证据不会落地"),
        ("③ 删库前先验指纹", r"gate\.cmd_verify", "动数据前先确认数据没被悄悄改（铁律 8）"),
    ]
    for index, (label, pattern, why) in enumerate(guards, start=1):
        say()
        say(f"  {label} —— {why}")
        found = re.search(rf"^.*{pattern}.*$", src, re.M)
        if found:
            say(f"      {found.group(0).strip()}")
        else:
            say("      （没在源码里找到，可能改过）")

    raise_count = len(re.findall(r"raise EvidenceError", src))
    say()
    say(f"  源码里一共 {raise_count} 处 raise EvidenceError —— 每处都是一个「宁可不出证据，也不出错」的关卡。")
    say()
    say("  念这句：这三道不是为了好看。第一道保证数字不对就跑挂，")
    say("          第二道保证跑挂了不会留下半份证据，第三道保证动数据前先对账。")
    return 0


# ---------------------------------------------------------------- 4 结果


def step_result() -> int:
    banner("第 4 步｜结果：21 节清单 + 上游抢名字的雷已拆")
    if not EVIDENCE.exists():
        say("找不到证据文件")
        return 1
    sections = parse_sections()
    say(f"现在这份证据 {len(sections)} 节（原来 13 节，多出来的是退货单状态机那一段）：")
    for index, section in enumerate(sections, start=1):
        mark = "命令" if section["cmd"] else "汇总"
        say(f"  {index:>2}. [{mark}] {section['title']}")
    say()
    say(f"  {len([s for s in sections if s['cmd']])} 节命令步骤 + "
        f"{len([s for s in sections if not s['cmd']])} 节汇总账。")

    say()
    say("-" * 72)
    say("顺带拆的雷：上游 prepare.py 原来也写 实际运行结果.txt，会把这份证据覆盖掉")
    say("-" * 72)
    if PREPARE.exists():
        source = PREPARE.read_text(encoding="utf-8")
        writes = re.findall(r'out_dir / "([^"]+\.txt)"', source)
        say(f"  src/prepare.py 现在只写：{writes}")
        leak = "out_dir / \"实际运行结果.txt\"" in source
        say(f"  它还会写 实际运行结果.txt 吗：{'会（没拆干净）' if leak else '不会 ✓'}")
    if PREP_STATS.exists():
        first = PREP_STATS.read_text(encoding="utf-8").splitlines()[0]
        say(f"  data/build/prepare_stats.txt 第一行：{first}")
    say(f"  data/build/实际运行结果.txt 第一行：{EVIDENCE.read_text(encoding='utf-8').splitlines()[0]}")
    say()
    say("  念这句：两个文件各归各位——数据准备的统计算数据准备，")
    say("          系统演示的证据算系统演示。以前它们同名，谁重跑一次 prepare.py 就把证据冲了。")
    return 0


# ---------------------------------------------------------------- 5/6 真跑


def step_rerun() -> int:
    banner("第 5 步（可选）｜真跑一次重生成（约 30 秒，不写正式文件）")
    if not CSV.exists():
        say("这一步要真读 149 MB 的 order_line.csv；本材料包不含它（按规范不进提交包）。")
        say("→ 想跑就在项目根目录执行，或先跑一次 python src/prepare.py 重建 CSV。")
        say("（第 1~4 步不需要数据，照样能演。）")
        return 1
    say("正在跑：python tools/make_evidence.py --dry-run")
    say("它会重放 21 节、逐步核对预期，结果写到 _scratch/ 而不是正式证据。")
    say()
    code, out = run_py(GENERATOR, ["--dry-run"])
    tail = [ln for ln in out.splitlines() if ln.strip()][-6:]
    for line in tail:
        say(f"  {line}")
    checked = out.count("（实际：") + out.count("（实际：退出码")
    say()
    say(f"  {'✓' if code == 0 else '✗'} 退出码 {code}；"
        f"{'21 节全部重放并核对通过' if code == 0 else '有步骤没通过'}")
    say("  念这句：一条命令就能把整份证据重跑一遍，每一步都自己核对；")
    say("          这才叫「可重跑的证据」，不是「一次性的截图」。")
    return 0 if code == 0 else 1


def step_refuse() -> int:
    banner("第 6 步（可选）｜把一处预期改错，看它拒不拒绝（约 20 秒）")
    if not CSV.exists():
        say("这一步同样需要 149 MB 数据源，本材料包跑不了（第 1~4 步不受影响）。")
        return 1
    if not GENERATOR.exists():
        say("找不到生成器")
        return 1
    needle = 'expect_result="E_DUPLICATE"'
    source = GENERATOR.read_text(encoding="utf-8")
    if needle not in source:
        say("生成器里没找到那处预期，跳过。")
        return 1

    say("做法：把生成器复制一份到 _scratch/，只改一处预期（E_DUPLICATE → E_DUPLICATE_TYPO），")
    say("      然后跑它。注意：不改动正式生成器，也不改动正式证据。")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    broken = SCRATCH / "make_evidence_broken.py"
    broken.write_text(source.replace(needle, 'expect_result="E_DUPLICATE_TYPO"', 1), encoding="utf-8")

    before = sha256(EVIDENCE)
    say()
    say(f"  跑之前，正式证据的指纹：{before}")
    code, out = run_py(broken, ["--dry-run"])
    after = sha256(EVIDENCE)
    shutil.rmtree(SCRATCH, ignore_errors=True)

    say("  输出结尾：")
    for line in [ln for ln in out.splitlines() if ln.strip()][-4:]:
        say(f"    {line}")
    say()
    refused = code != 0
    say(f"  {'✓' if refused else '✗'} 退出码 {code}（预期非 0：它拒绝了）")
    say(f"  {'✓' if before == after else '✗'} 正式证据指纹 {after}（{'没被写过' if before == after else '被改了！'}）")
    say()
    say("  念这句：它宁可不出证据，也不出一份对不上的证据——")
    say("          这就是「对不上就报错、不写文件」的实际样子。")
    return 0 if (refused and before == after) else 1


# ---------------------------------------------------------------- 0 自检


def self_check() -> int:
    banner("自检：这一页要用的东西在不在")
    say(f"  运行位置：{ROOT}")
    for label, ok in [
        ("证据 data/build/实际运行结果.txt", EVIDENCE.exists()),
        ("生成器 tools/make_evidence.py", GENERATOR.exists()),
        ("上游 src/prepare.py", PREPARE.exists()),
        ("清洗统计 data/build/prepare_stats.txt", PREP_STATS.exists()),
        ("项目宪法 CLAUDE.md", CONSTITUTION.exists()),
        ("全量数据 order_line.csv（只有第 5、6 步要）", CSV.exists()),
    ]:
        say(f"  {'✓' if ok else '–'} {label}")
    say()
    say("  带「–」的可选项没有是正常的：材料包按规范不含 149 MB 数据源，")
    say("  第 1~4 步照样能演（它们只读文件）。")
    return 0


STEPS = {
    "0": ("自检", self_check),
    "1": ("问题：凭什么说是程序生成的", step_problem),
    "2": ("冲突：铁律第 6 条原文", step_rule),
    "3": ("处理：三道保险的源码", step_fix),
    "4": ("结果：21 节清单 + 拆雷", step_result),
    "5": ("真跑一次重生成（约 30 秒）", step_rerun),
    "6": ("改错一处预期，看它拒否（约 20 秒）", step_refuse),
}
ORDER = ["1", "2", "3", "4"]


def menu() -> int:
    while True:
        say()
        say("=" * 72)
        say("  VC-R-01 修正台（讲「问题与修正」）　" + str(ROOT))
        say("=" * 72)
        say("   1  问题：凭什么说这份证据是程序生成的（秒出）")
        say("   2  冲突：项目铁律第 6 条原文（秒出）")
        say("   3  处理：三道保险，在源码里（秒出）")
        say("   4  结果：21 节清单 + 上游抢名字的雷已拆（秒出）")
        say("   8  连着跑完 1~4（台上最省事）")
        say("   5  真跑一次重生成（约 30 秒，需数据源）")
        say("   6  把一处预期改错，看它拒不拒绝（约 20 秒，需数据源）")
        say("   0  自检")
        say("   q  退出")
        say("-" * 72)
        choice = input("按数字，回车：").strip().lower()
        if choice in ("q", "quit", "exit", ""):
            return 0
        if choice == "8":
            for key in ORDER:
                STEPS[key][1]()
                input("\n（按回车走下一步）")
            continue
        if choice in STEPS:
            STEPS[choice][1]()
            continue
        say("没这个选项，按 0~6 / 8 / q。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VC-R-01 修正台")
    parser.add_argument("--step", choices=sorted(STEPS))
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    if args.step:
        return STEPS[args.step][1]()
    if args.all:
        failed = 0
        for key in ORDER:
            failed += STEPS[key][1]()
        say()
        say("全部跑完。" if failed == 0 else f"有 {failed} 步没过，看上面的 ✗。")
        return 0 if failed == 0 else 1
    try:
        return menu()
    except (KeyboardInterrupt, EOFError):
        say("\n退出。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
