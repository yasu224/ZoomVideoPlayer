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
# カメラを AVFoundation で直接読むライブラリ。無いか、元のファイルの方が新しければ作り直す (作れなくても起動する)
if [ ! -f cam/libzvp_avcam.dylib ] || [ cam/avcam_bridge.m -nt cam/libzvp_avcam.dylib ]; then
  bash build_cam_mac.sh >/dev/null 2>&1 || echo "カメラ用のライブラリを作れませんでした (カメラは FFmpeg で読みます)"
fi
exec .venv/bin/python zoom_player.py "$@"
