"""提交门禁（只用标准库）。

背景：讲义第 5 讲要求「配一道门禁」，靠机制而不是靠自觉。
旧版有三个能被"假通过"的口子，这一版逐个堵上：

1. **必需输入缺失也放行**：旧版 `verify` 遇到 `.gates/data.json` 不存在时，会把
   `order_line.csv` 记成 `{"exists": false}` 然后返回 PASS；之后每次比对，两边
   的 sha256 都是 None，"相等" → 永远放行。提交包里没有 149 MB 的 CSV，于是
   "门禁全绿"其实是假的。
   → 现在：**必需输入必须存在**，缺了直接 FAIL；且 `data.json` 里若把必需文件
     记成不存在，也判 FAIL（要求补齐后重新登记），绝不"将错就错"。

2. **标记过期仍放行**：旧版 `check` 只问 `tests.passed` / `evidence.passed`
   这两个文件在不在，内容一个字都不看。改完源码、换完证据，旧标记照样放行。
   → 现在：`tests.passed` 里写 `tree=<src+tests 摘要>`，`evidence.passed` 里写
     `set=<证据联合摘要> gen=<生成器摘要>`；`check` 每次都重新算并比对，
     对不上就是"标记过期"，要求重跑。

3. **证据检查太弱**：旧版只查"非空 + 有 RESULT= + ### 够 5 个"，手拼一份也能过。
   → 现在：21 节逐节校验（每节有命令、有"预期/实际"且两者一致），再做四组交叉核对：
     §1 与 import_invariants.txt 逐项相等、去重口径自洽、reject_stat 与 §21 逐码相等、
     20 张发票 20/20；最后与 `.gates/evidence.json`（由 make_evidence.py 写的生成清单）
     逐文件核对摘要 —— 这一条证明"是这一版生成器一次跑出来的，之后没被手改"。

负面向自测：`python tools/gate.py selftest` 会在 `_scratch/gate-selftest/` 里造一个
沙盒项目，把「缺 CSV」「测试后改源码」「证据被改」「生成器被改」四种情形真跑一遍，
断言门禁必须拒绝。对应 测试记录.md 的 T-17、T-23~T-26。

边界：本工具只**读**数据、只写 `.gates/` 下的标记与日志；
`data/raw/` 只读、`data/build/` 里的正式证据不手改（证据只能由 make_evidence.py 生成）。

用法（项目根目录）：
    python tools/gate.py verify      # 数据指纹 + 必需输入是否齐
    python tools/gate.py tests       # 跑全部测试，通过则写带摘要的标记
    python tools/gate.py evidence    # 结构校验 + 生成清单核对，通过则写标记
    python tools/gate.py check       # 提交前总检：三项全绿才放行（退出码 0/1）
    python tools/gate.py selftest    # 门禁自己的负向自测（沙盒里跑，不动正式材料）
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import evidence_manifest  # noqa: E402

GATES = ROOT / ".gates"
FINGERPRINT = GATES / "data.json"
TEST_MARK = GATES / "tests.passed"
EVIDENCE_MARK = GATES / "evidence.passed"
TEST_LOG = GATES / "tests.log"
SELFTEST_DIR = ROOT / "_scratch" / "gate-selftest"

# 必需输入：没有它，系统根本跑不起来 —— 缺了就是 FAIL，不接受"登记成不存在"
REQUIRED_INPUTS = [ROOT / "data" / "build" / "order_line.csv"]

# 受保护的数据：只登记、不修改（原始件允许不在，但一旦登记过就必须还在且一致）
WATCHED = REQUIRED_INPUTS + [ROOT / "data" / "raw" / "online_retail_II.xlsx"]

# 必须存在的证据文件（非空）
EVIDENCE = [
    ROOT / "data" / "build" / "实际运行结果.txt",
    ROOT / "data" / "build" / "import_invariants.txt",
    ROOT / "data" / "build" / "reject_stat.txt",
    ROOT / "data" / "build" / "发票对照20张.txt",
    ROOT / "测试记录.md",
    ROOT / "规则与决定.md",
]

# 摘要覆盖的代码范围：源码 + 测试（含夹具）。改任何一处，测试标记即作废。
TREE_DIRS = [ROOT / "src", ROOT / "tests"]

EXPECTED_SECTIONS = 21
EXPECTED_INVARIANTS = 13
EXPECTED_INVOICES = 20

OK = "PASS"
BAD = "FAIL"


# ---------------------------------------------------------------- 小工具


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def rel(path: Path) -> str:
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def tree_digest(dirs: list[Path] | None = None) -> str:
    """源码 + 测试的联合摘要（跳过 __pycache__ 与 .pyc）。

    文本文件按**换行归一**算摘要：git checkout / git restore 会把 LF 变回 CRLF，
    那是换行策略的差别，不是"代码改了"，不该让测试标记失效。
    二进制文件仍按字节算。
    """
    roots = dirs if dirs is not None else TREE_DIRS
    items: list[tuple[str, str]] = []
    for base in roots:
        if not Path(base).is_dir():
            continue
        for path in sorted(Path(base).rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            items.append((rel(path), source_digest(path)))
    digest = hashlib.sha256()
    for name, value in sorted(items):
        digest.update(f"{name}\0{value}\n".encode("utf-8"))
    return digest.hexdigest()


TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".csv", ".json", ".js", ".css", ".html", ".yml", ".yaml", ".cfg", ".ini",
}


def source_digest(path: Path) -> str:
    """源码文件摘要：文本按换行归一，其它按字节。"""
    if Path(path).suffix.lower() in TEXT_SUFFIXES:
        text = (
            Path(path)
            .read_text(encoding="utf-8", errors="replace")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    return sha256_of(path)


def read_marker(path: Path) -> dict[str, str] | None:
    """读标记文件；返回 None 表示不存在，返回 {} 表示存在但格式不对（旧格式）。"""
    if not Path(path).is_file():
        return None
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    fields = {
        key: value
        for key, value in re.findall(r"(tree|set|gen)=([0-9a-f]{16})", text)
    }
    return fields


def short(digest: str) -> str:
    return digest[:16]


# ---------------------------------------------------------------- 数据指纹


def current_fingerprint() -> dict[str, dict[str, object]]:
    data: dict[str, dict[str, object]] = {}
    for path in WATCHED:
        if not path.exists():
            data[rel(path)] = {"exists": False}
            continue
        data[rel(path)] = {
            "exists": True,
            "size": path.stat().st_size,
            "sha256": sha256_of(path),
        }
    return data


def missing_required() -> list[str]:
    return [rel(path) for path in REQUIRED_INPUTS if not path.is_file()]


def cmd_verify(verbose: bool = True) -> bool:
    GATES.mkdir(exist_ok=True)

    # 第一关：必需输入必须存在 —— 不看 data.json 怎么说
    absent = missing_required()
    if absent:
        if verbose:
            print(f"[{BAD}] 必需输入缺失：{'、'.join(absent)}")
            print("       门禁不会把「不存在」登记成通过：先把数据补齐（见 运行说明.md 第二节），")
            print("       再跑 python tools/gate.py verify。")
            print("       （注：提交包里按规范不带 149 MB 的 order_line.csv，"
                  "所以门禁只在有全量数据的机器上跑。）")
        return False

    now = current_fingerprint()
    if not FINGERPRINT.exists():
        FINGERPRINT.write_text(
            json.dumps(now, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if verbose:
            print(f"[{OK}] 首次登记数据指纹 -> .gates/data.json")
            for name, info in now.items():
                print(f"       {name}  {info.get('size', '（缺失）')}")
        return True

    try:
        saved = json.loads(FINGERPRINT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        if verbose:
            print(f"[{BAD}] .gates/data.json 读不了（不是合法 JSON）："
                  "数据指纹不可信，删掉它重新登记")
        return False

    bad: list[str] = []
    # 第二关：登记时就缺失的必需文件，绝不当成通过
    for name in missing_required():
        if not (saved.get(name) or {}).get("exists", True):
            bad.append(f"{name}：登记时就记成「不存在」，这不是合法基线（补齐后删掉 .gates/data.json 重新登记）")
    for name, info in now.items():
        old = saved.get(name, {})
        if bool(old.get("exists")) != bool(info.get("exists")):
            if info.get("exists"):
                bad.append(f"{name}：登记时不存在，现在出现了（指纹要重新登记）")
            else:
                bad.append(f"{name}：登记时存在，现在不见了")
            continue
        if info.get("exists") and old.get("sha256") != info.get("sha256"):
            bad.append(f"{name}：内容变了（指纹不一致）")

    if bad:
        if verbose:
            print(f"[{BAD}] 数据指纹与登记时不一致：")
            for item in bad:
                print(f"       - {item}")
            print("       如果这是有意的（比如重跑了 prepare.py），"
                  "删掉 .gates/data.json 重新登记，并在 过程记录.md 写清原因。")
        return False
    if verbose:
        print(f"[{OK}] 数据指纹未变（{len(now)} 个受保护文件，必需输入齐全）")
    return True


# ---------------------------------------------------------------- 测试标记


def cmd_tests(verbose: bool = True) -> bool:
    GATES.mkdir(exist_ok=True)
    if TEST_MARK.exists():
        TEST_MARK.unlink()
    with TEST_LOG.open("w", encoding="utf-8") as log:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=1800,
        )
    text = TEST_LOG.read_text(encoding="utf-8", errors="replace")
    tail = [line for line in text.splitlines() if line.strip()][-12:]
    passed = result.returncode == 0 and "OK" in text[-400:]
    summary = ""
    for line in reversed(text.splitlines()):
        if line.startswith("Ran "):
            summary = line.strip()
            break
    if passed:
        digest = short(tree_digest())
        TEST_MARK.write_text(
            f"{OK} {stamp()} {summary} tree={digest}\n", encoding="utf-8"
        )
    if verbose:
        print(f"[{OK if passed else BAD}] 全部测试 {'通过' if passed else '失败'}")
        if summary:
            print(f"       {summary}（完整输出：.gates/tests.log）")
        if passed:
            print(f"       源码+测试摘要 tree={short(tree_digest())}（改代码后此标记自动作废）")
        else:
            for line in tail:
                print(f"       {line}")
    return passed


# ---------------------------------------------------------------- 证据


def _sections(text: str) -> list[tuple[int, str, str]]:
    """按 `### N. 标题` 切块；返回 [(编号, 标题, 正文)]。"""
    matches = list(re.finditer(r"(?m)^### (\d+)\.\s*(.+)$", text))
    blocks: list[tuple[int, str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append((int(match.group(1)), match.group(2).strip(), text[start:end]))
    return blocks


def _token(text: str) -> str:
    """把"预期/实际"里的结论词取出来比较：以逗号截断，去掉尾部的 ✓。"""
    token = re.split(r"[，,]", text)[0].strip()
    return token.rstrip(" ✓").strip()


def _check_sections(text: str) -> list[str]:
    problems: list[str] = []
    blocks = _sections(text)
    numbers = [number for number, _, _ in blocks]
    if len(blocks) != EXPECTED_SECTIONS:
        problems.append(f"实际运行结果.txt 应为 {EXPECTED_SECTIONS} 节，实际 {len(blocks)} 节")
    if numbers != list(range(1, len(numbers) + 1)):
        problems.append(f"章节编号不是 1..N 连续：{numbers}")

    expect_re = re.compile(r"（预期：退出码 (\d+) / (.+?)）")
    actual_re = re.compile(r"（实际：退出码 (\d+) / (.+?)）")
    # 末尾的汇总节（"各校验拒绝计数…"）没有命令，单独按"计数表"校验，不套命令/预期规则
    tally_re = re.compile(r"计数|统计")
    for number, title, block in blocks:
        is_tally = bool(tally_re.search(title)) and "$ python " not in block
        if is_tally:
            if not re.search(r"合计\s+\d+", block):
                problems.append(f"§{number}（{title}）看起来是汇总节，但没有「合计」行")
            continue
        if "$ python " not in block:
            problems.append(f"§{number} 没有命令实录（找不到 $ python …）")
        if "RESULT=" not in block:
            problems.append(f"§{number} 没有任何 RESULT= 行")
        expect = expect_re.findall(block)
        actual = actual_re.findall(block)
        if not expect:
            problems.append(f"§{number} 缺少「（预期：退出码 … / …）」")
        if len(expect) != len(actual):
            problems.append(
                f"§{number} 预期与实际不成对（预期 {len(expect)} 条 / 实际 {len(actual)} 条）"
            )
        for (want_code, want_text), (got_code, got_text) in zip(expect, actual):
            if want_code != got_code:
                problems.append(f"§{number} 预期退出码 {want_code}，实际记的是 {got_code}")
            if _token(want_text) != _token(got_text):
                problems.append(
                    f"§{number} 预期「{_token(want_text)}」与实际「{_token(got_text)}」不一致"
                )
    return problems


def _check_invariants(evidence: str) -> list[str]:
    problems: list[str] = []
    inv_path = ROOT / "data" / "build" / "import_invariants.txt"
    if not inv_path.is_file():
        return [f"缺少 {rel(inv_path)}，无法与证据 §1 交叉核对"]
    inv_text = inv_path.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(r"(?m)^\s{2}([a-z_]+)\s+(\d+)\s*✓")
    inv = dict(pattern.findall(inv_text))
    blocks = _sections(evidence)
    if not blocks:
        return ["实际运行结果.txt 里没有章节，无法交叉核对不变量"]
    first = blocks[0][2]
    shown = dict(pattern.findall(first))
    if len(inv) != EXPECTED_INVARIANTS:
        problems.append(
            f"import_invariants.txt 应有 {EXPECTED_INVARIANTS} 项，实际 {len(inv)} 项"
        )
    if not shown:
        problems.append("证据 §1 里没有不变量逐项打勾的区块")
    for name, value in inv.items():
        if shown.get(name) != value:
            problems.append(
                f"§1 的 {name}={shown.get(name, '（缺）')} 与 import_invariants.txt 的 {value} 不一致"
            )
    for name in shown:
        if name not in inv:
            problems.append(f"§1 出现了 import_invariants.txt 里没有的项：{name}")

    # 去重口径自洽：去重前 − 丢弃 = 去重后，且与 §1 的行数一致
    numbers: dict[str, int] = {}
    for label, pattern_text in (
        ("去重前", r"去重前 ([\d,]+) 行"),
        ("丢弃", r"重叠发票 [\d,]+ 张 / ([\d,]+) 行"),
        ("去重后", r"去重后 ([\d,]+) 行"),
    ):
        match = re.search(pattern_text, first)
        if not match:
            problems.append(f"§1 缺少口径数字：{label}")
            continue
        numbers[label] = int(match.group(1).replace(",", ""))
    if len(numbers) == 3 and numbers["去重前"] - numbers["丢弃"] != numbers["去重后"]:
        problems.append(
            f"去重口径不自洽：{numbers['去重前']} − {numbers['丢弃']} ≠ {numbers['去重后']}"
        )
    if numbers.get("去重后") and inv.get("rows") and str(numbers["去重后"]) != inv["rows"]:
        problems.append(
            f"§1 的去重后行数 {numbers['去重后']} 与不变量 rows={inv['rows']} 不一致"
        )
    return problems


def _check_reject_stats(evidence: str) -> list[str]:
    problems: list[str] = []
    stat_path = ROOT / "data" / "build" / "reject_stat.txt"
    if not stat_path.is_file():
        return [f"缺少 {rel(stat_path)}"]
    stat_text = stat_path.read_text(encoding="utf-8", errors="replace")
    code_re = re.compile(r"(?m)^\s{2}(E_[A-Z_]+)\s+(\d+)\s*$")
    total_re = re.compile(r"(?m)^\s{2}合计\s+(\d+)\s*$")
    stats = {code: int(count) for code, count in code_re.findall(stat_text)}
    stat_total = total_re.search(stat_text)
    blocks = _sections(evidence)
    if not blocks:
        return ["实际运行结果.txt 没有章节"]
    tally_block = blocks[-1][2]
    shown = {code: int(count) for code, count in code_re.findall(tally_block)}
    shown_total = total_re.search(tally_block)
    if not shown:
        problems.append("证据 §21 里没有拒绝计数表")
    if stats != shown:
        problems.append(f"reject_stat.txt 与证据 §21 不一致：文件 {stats} / 证据 {shown}")
    if stat_total is None:
        problems.append("reject_stat.txt 缺少「合计」行")
    elif int(stat_total.group(1)) != sum(stats.values()):
        problems.append(
            f"reject_stat.txt 合计 {stat_total.group(1)} ≠ 各码之和 {sum(stats.values())}"
        )
    if shown_total and stats and int(shown_total.group(1)) != sum(stats.values()):
        problems.append(f"证据 §21 合计 {shown_total.group(1)} 与 reject_stat.txt 对不上")
    return problems


def _check_invoices() -> list[str]:
    path = ROOT / "data" / "build" / "发票对照20张.txt"
    if not path.is_file():
        return [f"缺少 {rel(path)}"]
    text = path.read_text(encoding="utf-8", errors="replace")
    rows = re.findall(r"(?m)^\S+\s+Year \d{4}-\d{4}\s+.*?(一致|不一致)\s*$", text)
    problems: list[str] = []
    if len(rows) != EXPECTED_INVOICES:
        problems.append(f"发票对照应有 {EXPECTED_INVOICES} 行，实际 {len(rows)} 行")
    if rows.count("不一致"):
        problems.append(f"发票对照里有 {rows.count('不一致')} 张不一致")
    if f"{EXPECTED_INVOICES} 张中 {EXPECTED_INVOICES} 张一致" not in text:
        problems.append("发票对照缺少「20 张中 20 张一致」的结论行")
    return problems


def _check_docs() -> list[str]:
    problems: list[str] = []
    rec = ROOT / "测试记录.md"
    rec_text = rec.read_text(encoding="utf-8", errors="replace")
    if "## 2. 测试编号总表" not in rec_text:
        problems.append("测试记录.md 缺少「测试编号总表」一节")
    ids = re.findall(r"(?m)^\| (T-\d\d) \|", rec_text)
    if len(ids) < 20:
        problems.append(f"测试记录.md 的 T-xx 只有 {len(ids)} 条（少于 20）")
    if len(ids) != len(set(ids)):
        problems.append("测试记录.md 的 T-xx 有重复编号")
    rules = (ROOT / "规则与决定.md").read_text(encoding="utf-8", errors="replace")
    for number in range(1, 10):
        if f"R-{number:02d}" not in rules:
            problems.append(f"规则与决定.md 里找不到 R-{number:02d}")
    return problems


def validate_evidence(verbose: bool = True) -> list[str]:
    """证据结构 + 交叉核对 + 生成清单。返回问题清单（空 = 全过）。"""
    problems: list[str] = []

    for path in EVIDENCE:
        if not path.exists():
            problems.append(f"缺少 {rel(path)}")
        elif path.stat().st_size == 0:
            problems.append(f"{rel(path)} 是空文件")
    if problems:
        return problems

    evidence_text = (ROOT / "data" / "build" / "实际运行结果.txt").read_text(
        encoding="utf-8", errors="replace"
    )
    problems += _check_sections(evidence_text)
    problems += _check_invariants(evidence_text)
    problems += _check_reject_stats(evidence_text)
    problems += _check_invoices()
    problems += _check_docs()

    # 生成清单：证明这些证据是这一版生成器一次跑出来的，之后没被手改
    problems += evidence_manifest.problems(ROOT)
    if verbose and problems:
        print(f"[{BAD}] 证据校验没通过：")
        for item in problems:
            print(f"       - {item}")
    return problems


def cmd_evidence(verbose: bool = True) -> bool:
    GATES.mkdir(exist_ok=True)
    if EVIDENCE_MARK.exists():
        EVIDENCE_MARK.unlink()
    problems = validate_evidence(verbose=verbose)
    if problems:
        return False

    manifest = evidence_manifest.load(ROOT) or {}
    EVIDENCE_MARK.write_text(
        f"{OK} {stamp()} {len(EVIDENCE)} 个文件 "
        f"set={short(str(manifest.get('evidence_set', '')))} "
        f"gen={short(str((manifest.get('generator') or {}).get('sha256', '')))}\n",
        encoding="utf-8",
    )
    if verbose:
        print(f"[{OK}] 证据齐全且可核对（{len(EVIDENCE)} 个文件；"
              f"21 节结构 / §1↔不变量 / 去重口径 / §21↔拒绝计数 / 20 张发票 / 生成清单）")
    return True


# ---------------------------------------------------------------- 总检


def _tests_state() -> tuple[bool, str]:
    marker = read_marker(TEST_MARK)
    if marker is None:
        return False, "-> 先跑 python tools/gate.py tests"
    if not marker.get("tree"):
        return False, "-> 标记是旧格式（没有源码摘要），重跑 python tools/gate.py tests"
    now = short(tree_digest())
    if marker["tree"] != now:
        return False, f"-> 标记过期：源码/测试已变（标记 tree={marker['tree']}，现在 tree={now}），重跑 tests"
    return True, f"tree={marker['tree']}"


def _evidence_state() -> tuple[bool, str]:
    marker = read_marker(EVIDENCE_MARK)
    if marker is None:
        return False, "-> 先跑 python tools/gate.py evidence"
    if not marker.get("set"):
        return False, "-> 标记是旧格式（没有证据摘要），重跑 python tools/gate.py evidence"
    manifest = evidence_manifest.load(ROOT)
    if manifest is None:
        return False, "-> 缺少 .gates/evidence.json（先跑 python tools/make_evidence.py）"
    issues = evidence_manifest.problems(ROOT)
    if issues:
        return False, "-> 标记过期：" + "；".join(issues[:2])
    if str(manifest.get("evidence_set", ""))[:16] != marker["set"]:
        return False, "-> 标记与当前证据摘要不一致（证据换过），重跑 evidence"
    return True, f"set={marker['set']}"


def cmd_check() -> bool:
    print("=== 提交门禁总检 ===")
    fingerprint_ok = cmd_verify()
    if not fingerprint_ok:
        print()
        print("  数据指纹   FAIL  -> 先跑 python tools/gate.py verify")
        tests_ok = False
        evidence_ok = False
        print("  测试通过   --    -> 数据都没齐，先补数据")
        print("  证据齐全   --    -> 数据都没齐，先补数据")
        print()
        print("门禁未通过：数据指纹这一关就没过，后面的标记一律不看。")
        return False

    tests_ok, tests_note = _tests_state()
    evidence_ok, evidence_note = _evidence_state()

    print()
    print(f"  数据指纹   {'PASS' if fingerprint_ok else 'FAIL'}")
    print(f"  测试通过   {'PASS' if tests_ok else 'FAIL'}  {tests_note}")
    print(f"  证据齐全   {'PASS' if evidence_ok else 'FAIL'}  {evidence_note}")

    if not (tests_ok and evidence_ok):
        print()
        print("门禁未通过：标记缺失或已过期，按上面的提示重跑，再 check。")
        return False
    print()
    print("门禁通过，可以提交。")
    return True


# ---------------------------------------------------------------- 自测（沙盒）


def _selftest_record(results: list[tuple[str, bool, str]], name: str, ok: bool, detail: str) -> None:
    results.append((name, bool(ok), detail))


def _run_in(cwd: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(Path(cwd) / "tools" / "gate.py"), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _seed_sandbox() -> Path:
    """在 _scratch 里造一个"像模像样"的沙盒项目（真实源码/测试/证据，数据用占位）。"""
    proj = SELFTEST_DIR / "proj"
    shutil.rmtree(proj, ignore_errors=True)
    proj.mkdir(parents=True)

    for name in ("gate.py", "evidence_manifest.py", "make_evidence.py"):
        _copy_file(ROOT / "tools" / name, proj / "tools" / name)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "src", proj / "src", ignore=ignore)
    shutil.copytree(ROOT / "tests", proj / "tests", ignore=ignore)

    # 证据：用真实的四份 + 两份文档（结构校验才有意义）
    for path in EVIDENCE:
        _copy_file(path, proj / rel(path))
    _copy_file(
        ROOT / "data" / "build" / "sample_2000.csv", proj / "data" / "build" / "sample_2000.csv"
    )
    # order_line.csv 只用来算指纹，门禁不解析它 —— 用几行占位即可
    (proj / "data" / "build" / "order_line.csv").write_text(
        "sheet,invoice,stock_code\nYear 2009-2010,1,A\n", encoding="utf-8"
    )

    # 清单：调用与生产同一套算法
    subprocess.run(
        [sys.executable, "-X", "utf8", "-c",
         "import sys; sys.path.insert(0, 'tools'); import evidence_manifest as m; m.write('.')"],
        cwd=str(proj), check=True, capture_output=True, text=True,
    )
    return proj


def cmd_selftest() -> bool:
    print("=== 门禁负向自测（沙盒里跑，不动正式材料）===")
    proj = _seed_sandbox()
    print(f"沙盒：{rel(proj)}")
    results: list[tuple[str, bool, str]] = []

    # 0. 正向对照：必须先能通过，否则后面的"拒绝"没有意义
    code, out = _run_in(proj, "verify")
    _selftest_record(results, "正向 · 数据齐 → verify 通过", code == 0, f"exit={code}")
    code, out = _run_in(proj, "tests")
    _selftest_record(results, "正向 · 跑测试 → 拿到带摘要的标记", code == 0, out.strip().splitlines()[-1] if out.strip() else "")
    code, out = _run_in(proj, "evidence")
    _selftest_record(results, "正向 · 证据校验通过", code == 0, out.strip().splitlines()[-1] if out.strip() else "")
    code, out = _run_in(proj, "check")
    _selftest_record(results, "正向 · check 全绿", code == 0, f"exit={code}")

    # 1. 缺 CSV：verify 与 check 都必须失败（T-23）
    csv = proj / "data" / "build" / "order_line.csv"
    csv.unlink()
    code_v, out_v = _run_in(proj, "verify")
    code_c, out_c = _run_in(proj, "check")
    _selftest_record(
        results,
        "缺 CSV · verify 必须失败（T-23）",
        code_v == 1 and "必需输入缺失" in out_v,
        f"exit={code_v}",
    )
    _selftest_record(
        results,
        "缺 CSV · check 必须失败（T-23）",
        code_c == 1 and "数据指纹   FAIL" in out_c,
        f"exit={code_c}",
    )

    # 2. data.json 把必需文件记成"不存在"：仍然必须失败（T-23）
    (proj / ".gates" / "data.json").write_text(
        json.dumps({"data/build/order_line.csv": {"exists": False}}, ensure_ascii=False),
        encoding="utf-8",
    )
    code_v2, out_v2 = _run_in(proj, "verify")
    _selftest_record(
        results,
        "记成缺失 · verify 仍失败（T-23）",
        code_v2 == 1 and "不存在" in out_v2,
        f"exit={code_v2}",
    )

    # 复原：补回 CSV、删掉坏基线、重新登记
    (proj / "data" / "build" / "order_line.csv").write_text(
        "sheet,invoice,stock_code\nYear 2009-2010,1,A\n", encoding="utf-8"
    )
    (proj / ".gates" / "data.json").unlink()
    code, _ = _run_in(proj, "verify")
    _selftest_record(results, "复原 · 补回 CSV 后 verify 通过", code == 0, f"exit={code}")

    # 3. 测试通过后改源码 → 测试标记作废（T-24）
    rules = proj / "src" / "rules.py"
    original = rules.read_bytes()          # 字节级备份/还原：换行策略不同也会被算成"改过"
    rules.write_bytes(original + b"\n# gate-selftest: \xe7\xa0\x81\xe6\x94\xb9\xe4\xb8\x80\xe8\xa1\x8c\n")
    code, out = _run_in(proj, "check")
    _selftest_record(
        results,
        "改源码 · tests 标记过期被拒（T-24）",
        code == 1 and "测试通过   FAIL" in out and "标记过期" in out,
        f"exit={code}",
    )
    rules.write_bytes(original)
    _run_in(proj, "tests")
    code, _ = _run_in(proj, "check")
    _selftest_record(results, "复原 · 重跑 tests 后 check 恢复", code == 0, f"exit={code}")

    # 4. 证据被改 → 证据标记作废（T-25）
    evidence = proj / "data" / "build" / "实际运行结果.txt"
    original_ev = evidence.read_bytes()
    evidence.write_bytes(original_ev + b"\n(hand-edit tail)\n")
    code, out = _run_in(proj, "check")
    _selftest_record(
        results,
        "改证据 · evidence 标记过期被拒（T-25）",
        code == 1 and "证据齐全   FAIL" in out and "标记过期" in out,
        f"exit={code}",
    )
    code_ev, out_ev = _run_in(proj, "evidence")
    _selftest_record(
        results,
        "改证据 · evidence 复检也拒绝",
        code_ev == 1 and "不一致" in out_ev,
        f"exit={code_ev}",
    )
    evidence.write_bytes(original_ev)
    # 复原时要把标记重新打出来：`evidence` 在开始时会先删旧标记（这是有意的，
    # 免得"校验失败但旧标记还留着"），所以还原文件后必须真的重跑一次。
    _run_in(proj, "evidence")
    code, _ = _run_in(proj, "check")
    _selftest_record(results, "复原 · 重跑 evidence 后 check 恢复", code == 0, f"exit={code}")

    # 5. 生成器被改 → 旧清单失效（T-26）
    gen = proj / "tools" / "make_evidence.py"
    original_gen = gen.read_bytes()
    gen.write_bytes(original_gen + b"\n# gate-selftest: generator changed\n")
    code, out = _run_in(proj, "check")
    _selftest_record(
        results,
        "改生成器 · 旧清单失效被拒（T-26）",
        code == 1 and "生成器已改" in out,
        f"exit={code}",
    )
    gen.write_bytes(original_gen)
    code, _ = _run_in(proj, "check")
    _selftest_record(results, "复原 · 生成器还原后 check 恢复", code == 0, f"exit={code}")

    print()
    width = max(len(name) for name, _, _ in results)
    for name, ok, detail in results:
        print(f"[{OK if ok else BAD}] {name:<{width}}  {detail}")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 58}\n  {passed}/{len(results)} 项符合预期\n{'=' * 58}")
    if passed == len(results):
        shutil.rmtree(SELFTEST_DIR, ignore_errors=True)
        return True
    print(f"失败项留在 {rel(SELFTEST_DIR)} 里，方便查")
    return False


# ---------------------------------------------------------------- 入口


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "check"
    if command == "verify":
        return 0 if cmd_verify() else 1
    if command == "tests":
        return 0 if cmd_tests() else 1
    if command == "evidence":
        return 0 if cmd_evidence() else 1
    if command == "check":
        return 0 if cmd_check() else 1
    if command == "selftest":
        return 0 if cmd_selftest() else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
