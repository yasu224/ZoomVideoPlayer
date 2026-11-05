#!/bin/bash
# Zoom Video Player を macOS 用にビルドする。出来上がりは dist/ZoomVideoPlayer.app
# 使い方: ターミナルでこのフォルダへ移動して  bash build_mac.sh
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt pyinstaller
.venv/bin/pyinstaller --noconfirm --clean --distpath dist --workpath build ZoomVideoPlayer.spec
echo
echo "ビルド完了: $(pwd)/dist/ZoomVideoPlayer.app"
