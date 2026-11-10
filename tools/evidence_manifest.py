"""证据生成清单：证明「这几份证据是同一次、由这一版生成器跑出来的，之后没被手改」。

为什么单独一个模块
------------------
`tools/make_evidence.py` 负责**写**清单，`tools/gate.py` 负责**核对**清单。
两边必须用同一套路径与同一套摘要算法，否则"核对"本身就会漂移——
门禁说通过，其实比的是另一批文件。所以抽成这里，两边都 import 它。

清单长什么样（写到 `.gates/evidence.json`）
------------------------------------------
    {
      "generated_at": "2026-11-01T14:05:00",
      "generator": {"path": "tools/make_evidence.py", "sha256": "<64 hex>"},
      "files": {
        "data/build/实际运行结果.txt": {"size": 17618, "sha256": "<64 hex>"},
        "data/build/import_invariants.txt": {...},
        "data/build/reject_stat.txt": {...},
        "data/build/发票对照20张.txt": {...}
      },
      "evidence_set": "<16 hex>"
    }

`evidence_set` 是对「四个文件的 (相对路径, sha256) 排序后 + 生成器 sha256」的摘要，
门禁的 `evidence.passed` 标记里存的就是它。任何一份证据被改、或生成器被改，
`evidence_set` 就对不上，旧标记直接作废。

边界
----
本模块只**读**文件算摘要、只写 `.gates/evidence.json`（不是数据文件）；
`data/raw/` 与 `data/build/` 里的正式证据一个字节都不碰。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

GATES_DIR = ".gates"
MANIFEST_NAME = "evidence.json"
GENERATOR_REL = "tools/make_evidence.py"

# 由生成器产出的四份证据（不含手写文档：文档由 gate 另外做结构检查）
EVIDENCE_RELS: tuple[str, ...] = (
    "data/build/实际运行结果.txt",
    "data/build/import_invariants.txt",
    "data/build/reject_stat.txt",
    "data/build/发票对照20张.txt",
)


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def digest_text(path: Path) -> str:
    """按"换行归一"算摘要。

    生成器摘要用它：Windows 上 git 检出是 LF、Python 写回是 CRLF，
    同一个文件会因为编辑器/换行策略被算成"改过"，那是噪声不是证据问题。
    证据文件仍用 sha256_of（字节级）——证据是数据，改一个空格都要算改。
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def file_entry(path: Path) -> dict[str, object] | None:
    path = Path(path)
    if not path.is_file():
        return None
    return {"size": path.stat().st_size, "sha256": sha256_of(path)}


def generator_entry(root: Path) -> dict[str, object] | None:
    path = Path(root) / GENERATOR_REL
    if not path.is_file():
        return None
    return {
        "size": path.stat().st_size,
        "sha256": digest_text(path),
        "normalized_newlines": True,
    }


def evidence_set(root: Path) -> str:
    """四个证据文件 + 生成器的联合摘要；任何一份缺失返回空串（=不可信）。"""
    root = Path(root)
    parts: list[str] = []
    for rel in EVIDENCE_RELS:
        entry = file_entry(root / rel)
        if entry is None:
            return ""
        parts.append(f"{rel}\0{entry['sha256']}")
    gen = generator_entry(root)
    if gen is None:
        return ""
    parts.append(f"generator\0{gen['sha256']}")
    return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()


def build(root: Path) -> dict[str, object]:
    root = Path(root)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generator": {
            "path": GENERATOR_REL,
            "sha256": (generator_entry(root) or {}).get("sha256"),
        },
        "files": {
            rel: (file_entry(root / rel) or {"missing": True}) for rel in EVIDENCE_RELS
        },
        "evidence_set": evidence_set(root),
    }


def write(root: Path) -> Path:
    """由生成器调用：把当前证据的摘要落成清单。"""
    root = Path(root)
    target = root / GATES_DIR / MANIFEST_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(build(root), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def load(root: Path) -> dict[str, object] | None:
    target = Path(root) / GATES_DIR / MANIFEST_NAME
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def problems(root: Path) -> list[str]:
    """核对清单与当前文件；返回问题清单（空 = 一致）。"""
    root = Path(root)
    manifest = load(root)
    if manifest is None:
        return ["没有证据生成清单 .gates/evidence.json（先跑 python tools/make_evidence.py）"]

    issues: list[str] = []
    gen_now = generator_entry(root)
    if gen_now is None:
        issues.append(f"生成器不见了：{GENERATOR_REL}")
    else:
        saved_gen = (manifest.get("generator") or {}).get("sha256")
        if saved_gen != gen_now["sha256"]:
            issues.append(
                "生成器已改（tools/make_evidence.py 与清单里的不一致）："
                "证据必须用改后的生成器重跑，旧清单不再可信"
            )

    saved_files = manifest.get("files") or {}
    for rel in EVIDENCE_RELS:
        now = file_entry(root / rel)
        old = saved_files.get(rel) or {}
        if now is None:
            issues.append(f"清单里的 {rel} 现在不见了")
            continue
        if old.get("missing"):
            issues.append(f"清单记录 {rel} 当时就缺失，不可信")
        elif old.get("sha256") != now["sha256"]:
            issues.append(f"{rel} 与清单不一致（生成后被改过）")

    if manifest.get("evidence_set") != evidence_set(root):
        issues.append("证据联合摘要与清单不一致（evidence_set 对不上）")
    return issues
