#!/bin/bash
# macOS のカメラを AVFoundation で直接読むライブラリ (cam/libzvp_avcam.dylib) をビルドする。
# 使い方: bash build_cam_mac.sh   (Xcode か Command Line Tools の clang が要る。SDK などは要らない)
# 無くてもアプリは動く (FFmpeg でカメラを読む。USB カメラによっては一部の形式が映らない)
set -e
cd "$(dirname "$0")"
clang -O2 -fobjc-arc -dynamiclib -fvisibility=hidden -arch arm64 -arch x86_64 -mmacosx-version-min=11.0 \
  cam/avcam_bridge.m -framework AVFoundation -framework CoreMedia -framework CoreVideo -framework Foundation \
  -o cam/libzvp_avcam.dylib
echo "ビルド完了: $(pwd)/cam/libzvp_avcam.dylib"
