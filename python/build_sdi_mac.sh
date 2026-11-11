#!/bin/bash
# Blackmagic の SDI 出力用ライブラリ (sdi/libzvp_decklink.dylib) をビルドする。macOS 用。
# 使い方: bash build_sdi_mac.sh [DeckLink SDK のフォルダ]
#   SDK は Blackmagic のサイトから入手する (「Desktop Video SDK」)。フォルダを省くと
#   環境変数 DECKLINK_SDK、なければこのリポジトリの隣の「Blackmagic DeckLink SDK *」を探す。
# 実行には Blackmagic Desktop Video (ドライバ) が入っていること。
set -e
cd "$(dirname "$0")"
SDK="${1:-$DECKLINK_SDK}"
if [ -z "$SDK" ]; then
  SDK="$(ls -d ../../Blackmagic\ DeckLink\ SDK* 2>/dev/null | sort | tail -1)"
fi
INC="$SDK/Mac/include"
if [ ! -f "$INC/DeckLinkAPI.h" ]; then
  echo "DeckLink SDK が見つかりません。SDK のフォルダを指定してください: bash build_sdi_mac.sh <SDK のフォルダ>"
  exit 1
fi
clang++ -std=c++17 -O2 -dynamiclib -fvisibility=hidden -arch arm64 -arch x86_64 \
  -mmacosx-version-min=11.0 \
  -I"$INC" sdi/decklink_bridge.cpp "$INC/DeckLinkAPIDispatch.cpp" \
  -framework CoreFoundation -o sdi/libzvp_decklink.dylib
echo "ビルド完了: $(pwd)/sdi/libzvp_decklink.dylib"
