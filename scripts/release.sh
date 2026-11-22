#!/usr/bin/env bash
# 发布一个新版本：版本说明 → 改版本号 → 测试 → 密钥检查 → 提交 → 打附注 Tag → 更新本地应用 [→ 推送 + GitHub Release]
#   ./scripts/release.sh 1.2.0            # 本地发布
#   ./scripts/release.sh 1.2.0 --push     # 同时推送提交和 Tag，并创建 GitHub Release
# 要求：
#   - 功能改动已单独提交，工作区干净
#   - CHANGELOG.md 的「## 未发布」段落写好了本版本的更新条目（为空时拒绝发布）
set -euo pipefail
cd "$(dirname "$0")/.."

VER="${1:-}"
[[ "$VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "用法：$0 <主.次.修订> [--push]" >&2; exit 1; }
TAG="v$VER"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && { echo "Tag $TAG 已存在" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "工作区有未提交的改动，先提交或清理" >&2; exit 1; }

PY=.venv/bin/python
"$PY" scripts/changelog.py release "$VER" >/dev/null   # 「未发布」→ vX.Y.Z，为空时在这里退出
sed -i '' -E "s/^version = \".*\"/version = \"$VER\"/" pyproject.toml
sed -i '' -E "s/^__version__ = \".*\"/__version__ = \"$VER\"/" src/workbench/__init__.py

"$PY" -m pytest -q
"$PY" scripts/check_secrets.py --all

NOTES="$("$PY" scripts/changelog.py notes "$VER")"
git add CHANGELOG.md pyproject.toml src/workbench/__init__.py
git commit -q -F - <<EOF
build(release): 发布 $TAG

$NOTES
EOF
git tag -a "$TAG" -F - <<EOF
$TAG

$NOTES
EOF
echo "已提交并打 Tag $TAG："
echo "$NOTES"

./scripts/build_app.sh --install

if [ "${2:-}" = "--push" ]; then
  git push origin HEAD
  git push origin "$TAG"
  gh release create "$TAG" --title "$TAG" --notes "$NOTES" --verify-tag
fi
