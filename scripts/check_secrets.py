"""检查是否有密钥、令牌、私钥等敏感信息将被提交。

    python scripts/check_secrets.py            # 检查暂存区（pre-commit 钩子调用）
    python scripts/check_secrets.py --all      # 检查所有已跟踪文件（测试调用）

误报时在该行末尾加注释 `secret-scan: allow`。发现问题时退出码为 1。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOW = "secret-scan: allow"

# 不允许提交的文件（按文件名匹配）
FORBIDDEN_FILES = [
    re.compile(p) for p in (
        r"(^|/)\.env(\.[\w.-]+)?$", r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$", r"\.(pem|p12|pfx|key|keystore|jks)$",
        r"(^|/)credentials(\.json)?$", r"(^|/)\.credentials\.json$", r"(^|/)\.claude/settings\.local\.json$",
        r"(^|/)\.netrc$", r"(^|/)\.npmrc$", r"(^|/)\.pypirc$", r"(^|/)workbench\.db$",
    )
]
# 允许的例外（示例文件）
ALLOWED_FILES = [re.compile(r"\.env\.example$")]

PATTERNS = {
    "私钥": re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"),
    "Anthropic API Key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "OpenAI API Key": re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"),
    "GitHub Token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "AWS Access Key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Google API Key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "Slack Token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "带密码的连接串": re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@'\"]+:[^/\s@'\"]{6,}@"),
    "硬编码的密钥 / 密码": re.compile(
        r"(?i)\b[\w.-]*(?:api[_-]?key|secret|token|passwd|password|access[_-]?key)\b[\"']?\s*[:=]\s*[\"']([^\"'\s]{12,})[\"']"),
}
# 明显是占位符的值不算
PLACEHOLDER = re.compile(r"(?i)^(?:x+|\*+|<[^>]+>|\$\{?\w+\}?|your[_-].*|changeme|example.*|dummy.*|test.*|fake.*)$")


def _files(all_files: bool) -> list[str]:
    args = ["git", "ls-files"] if all_files else ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    return [f for f in subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines() if f]


def _content(path: str, all_files: bool) -> str | None:
    if all_files:
        try:
            data = (ROOT / path).read_bytes()
        except OSError:
            return None
    else:  # 检查暂存的内容，而不是工作区
        data = subprocess.run(["git", "show", f":{path}"], cwd=ROOT, capture_output=True).stdout
    if b"\0" in data[:8000]:  # 二进制文件
        return None
    return data.decode("utf-8", errors="replace")


def scan(all_files: bool = False) -> list[str]:
    problems = []
    for path in _files(all_files):
        if any(p.search(path) for p in FORBIDDEN_FILES) and not any(p.search(path) for p in ALLOWED_FILES):
            problems.append(f"{path}: 不允许提交此类文件（可能含密钥或私密数据）")
            continue
        text = _content(path, all_files)
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if ALLOW in line:
                continue
            for name, pat in PATTERNS.items():
                m = pat.search(line)
                if not m:
                    continue
                if m.groups() and PLACEHOLDER.match(m.group(1)):
                    continue
                problems.append(f"{path}:{n}: 疑似{name}")
    return problems


def main() -> int:
    problems = scan("--all" in sys.argv)
    if problems:
        print("发现可能的敏感信息，已阻止提交：", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        print(f"如确认是误报，在该行末尾加注释 `{ALLOW}`。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
