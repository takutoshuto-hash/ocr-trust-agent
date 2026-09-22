#!/usr/bin/env bash
# 合成帳票用の手書き風フォント（Google Fonts, OFL）を data/fonts/ に取得する。git 管理外。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/fonts
BASE="https://raw.githubusercontent.com/google/fonts/main/ofl"
for f in yomogi/Yomogi-Regular.ttf zenkurenaido/ZenKurenaido-Regular.ttf kleeone/KleeOne-Regular.ttf hachimarupop/HachiMaruPop-Regular.ttf; do
  curl -fsSL "$BASE/$f" -o "data/fonts/$(basename "$f")"
done
ls -la data/fonts/*.ttf
