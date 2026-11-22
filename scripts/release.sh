#!/usr/bin/env bash
# 发布一个新版本：改版本号 → 测试 → 密钥检查 → 提交 → 打 Tag → 更新本地应用
#   ./scripts/release.sh 1.2.0            # 本地发布
#   ./scripts/release.sh 1.2.0 --push     # 同时推送提交和 Tag 到 GitHub
# 要求：工作区除本次版本改动外是干净的（功能改动应已单独提交）。
set -euo pipefail
cd "$(dirname "$0")/.."

VER="${1:-}"
[[ "$VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "用法：$0 <主.次.修订> [--push]" >&2; exit 1; }
TAG="v$VER"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && { echo "Tag $TAG 已存在" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "工作区有未提交的改动，先提交或清理" >&2; exit 1; }

PY=.venv/bin/python
sed -i '' -E "s/^version = \".*\"/version = \"$VER\"/" pyproject.toml
sed -i '' -E "s/^__version__ = \".*\"/__version__ = \"$VER\"/" src/workbench/__init__.py

"$PY" -m pytest -q
"$PY" scripts/check_secrets.py --all

git add pyproject.toml src/workbench/__init__.py
git commit -q -m "build(release): 发布 $TAG"
git tag -a "$TAG" -m "$TAG"
echo "已提交并打 Tag $TAG"

./scripts/build_app.sh --install

if [ "${2:-}" = "--push" ]; then
  git push origin HEAD
  git push origin "$TAG"
fi
