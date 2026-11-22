"""仓库里已跟踪的文件不能含密钥、令牌、私钥等敏感信息。"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_secrets_in_tracked_files():
    r = subprocess.run([sys.executable, "scripts/check_secrets.py", "--all"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
