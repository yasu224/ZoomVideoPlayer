#!/bin/bash
# Zoom Video Player (Python 版) を macOS で起動する。
# 使い方: bash run_mac.sh [動画ファイル]   (初回は Python 3.10 以上で .venv を作る)
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "初回セットアップ中です..."
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python zoom_player.py "$@"
