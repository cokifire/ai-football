#!/usr/bin/env bash
# 安装 football-bayes skill 到 codex 的 skills 目录（codex 自动发现，prompt 里以
# $football-bayes 触发）。在服务器上执行：bash scripts/install-codex-skill.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_SRC="$REPO_DIR/skills/football-bayes"
SKILL_DST="${CODEX_HOME:-$HOME/.codex}/skills/football-bayes"

if [ ! -f "$SKILL_SRC/SKILL.md" ]; then
    echo "错误: 仓库内缺少 $SKILL_SRC/SKILL.md" >&2
    exit 1
fi

mkdir -p "$SKILL_DST"
cp -r "$SKILL_SRC/." "$SKILL_DST/"
echo "已安装 skill: $SKILL_DST"
echo "验证: codex exec '列出你可用的 skills' 或检查 $SKILL_DST/SKILL.md"
