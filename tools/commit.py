"""带门禁的提交：门禁不过就不提交。

为什么是"手动跑一条命令"而不是自动拦截：
  本项目试过 PreToolUse 自动拦截（`dsh-hooks-claude-code` 插件 + 钩子脚本），
  脚本自测 8/8 通过，但插件挂载始终没被证实真的触发过。既然证明不了它在拦，
  就按过程记录第 8 轮的决定撤掉，只保留这道可复现、可演示的手动门禁。
  （原因与证据见 运行说明.md 第九节、过程记录.md 第 8 轮。）

用法：
    python tools/commit.py "说明：补完某某"
    python tools/commit.py --check-only      # 只检查不提交
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "tools" / "gate.py"


def main(argv: list[str]) -> int:
    args = argv[1:]
    check_only = "--check-only" in args
    message = " ".join(item for item in args if not item.startswith("--")).strip()

    if not message and not check_only:
        print('用法：python tools/commit.py "提交说明"')
        return 2

    print("== 提交门禁（commit.py）==")
    result = subprocess.run([sys.executable, "-X", "utf8", str(GATE), "check"], cwd=str(ROOT))
    if result.returncode != 0:
        print()
        print("=" * 46)
        print(" 提交被拒绝：门禁没过。先补齐这三条再提交：")
        print("   python tools/gate.py verify")
        print("   python tools/gate.py tests")
        print("   python tools/gate.py evidence")
        print("=" * 46)
        return 1

    if check_only:
        print("\n只检查：门禁通过（未提交）。")
        return 0

    subprocess.run(["git", "add", "-A"], cwd=str(ROOT), check=False)
    commit = subprocess.run(["git", "commit", "-m", message], cwd=str(ROOT), check=False)
    return commit.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
