"""测试公用小工具（不是被测对象）。

只做三件事：定位路径、跑 CLI 并捕获输出、建/删临时库。
"""

from __future__ import annotations

import io
import shutil
import sys
import time
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import db  # noqa: E402
import main  # noqa: E402

TINY = str(ROOT / "tests" / "fixtures" / "tiny.csv")
OVERLAP = str(ROOT / "tests" / "fixtures" / "overlap.csv")
SAMPLE = str(ROOT / "data" / "build" / "sample_2000.csv")

# 固定时钟：不固定的话，运行时的真实时间与提交时间差超过 7 天，R-09 会把申请释放掉
NOW = "2026-01-01T00:00:00"
LATER = "2026-01-09T00:00:00"   # NOW + 8 天


def new_workdir() -> Path:
    """临时目录放在工作区内：系统 TEMP 在受限环境下清理会触发权限错误。"""
    base = ROOT / "tests" / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    path = base / uuid.uuid4().hex[:8]
    path.mkdir()
    return path


def remove_workdir(path: Path) -> None:
    for _ in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.05)


def run_cli(*args: str) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main.main(list(args))
    return code, buffer.getvalue()


class DbCase(unittest.TestCase):
    """每个用例一个独立临时库，导入 tiny.csv（10 行手写边界样本）。"""

    def setUp(self) -> None:
        self.work = new_workdir()
        self.db_path = str(self.work / "rs.db")
        self.out = str(self.work)

    def tearDown(self) -> None:
        remove_workdir(self.work)

    def do_import(
        self, csv_path: str = TINY, *, baseline: str = "report", extra: tuple[str, ...] = ()
    ) -> tuple[int, str]:
        return run_cli(
            "import", "--csv", csv_path, "--db", self.db_path, "--out", self.out,
            "--baseline", baseline, *extra,
        )

    def apply(self, *extra: str) -> tuple[int, str]:
        return run_cli("apply", "--db", self.db_path, "--now", NOW, *extra)

    def query(self, invoice: str = "489434", *extra: str) -> tuple[int, str]:
        return run_cli(
            "query", "--db", self.db_path, "--invoice", invoice, "--now", NOW, *extra
        )

    def conn(self):
        return db.connect(self.db_path)
