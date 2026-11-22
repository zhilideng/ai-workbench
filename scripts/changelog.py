"""CHANGELOG.md 工具。

    python scripts/changelog.py release 1.2.0   # 把「未发布」改成 v1.2.0，并新建空的「未发布」；打印该版本说明
    python scripts/changelog.py notes 1.2.0     # 打印 v1.2.0 的说明（用于 Tag 与 GitHub Release）
"""
from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
UNRELEASED = "## 未发布"


def _sections(text: str) -> list[tuple[str, int, int]]:
    """[(标题行, 正文起点, 正文终点)]"""
    heads = [m for m in re.finditer(r"^## .+$", text, re.M)]
    return [(m.group(0).strip(), m.end(), heads[i + 1].start() if i + 1 < len(heads) else len(text))
            for i, m in enumerate(heads)]


def notes(version: str, text: str | None = None) -> str:
    text = text if text is not None else FILE.read_text()
    for head, a, b in _sections(text):
        if head == f"## v{version}" or head.startswith(f"## v{version} "):
            return text[a:b].strip()
    sys.exit(f"CHANGELOG.md 里没有 v{version} 的说明")


def release(version: str) -> str:
    text = FILE.read_text()
    sec = next((s for s in _sections(text) if s[0] == UNRELEASED), None)
    if not sec:
        sys.exit("CHANGELOG.md 里没有「## 未发布」段落")
    body = text[sec[1]:sec[2]].strip()
    if not re.search(r"^\s*[-*] \S", body, re.M):
        sys.exit("「未发布」段落没有任何更新条目，先写清楚这个版本更新了什么")
    if any(h.startswith(f"## v{version}") for h, _, _ in _sections(text)):
        sys.exit(f"v{version} 已存在")
    head = f"{UNRELEASED}\n\n## v{version} - {datetime.date.today().isoformat()}"
    FILE.write_text(text.replace(UNRELEASED, head, 1))
    return body


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("release", "notes"):
        sys.exit(__doc__)
    print(release(sys.argv[2]) if sys.argv[1] == "release" else notes(sys.argv[2]))
