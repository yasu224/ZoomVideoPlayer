# Zoom Video Player

動画の任意の範囲を拡大して見られる動画プレイヤーです（映像の確認用、音声は再生しません）。

| 版 | 場所 | 特長 |
|---|---|---|
| ブラウザ版 | [`index.html`](index.html) | ブラウザで開くだけで使える |
| Python 版 | [`python/`](python/README.md) | PyAV (FFmpeg) でデコードし、MXF・10bit 4:2:2 も開ける。GPU 描画で 120 / 144 / 240fps、フレーム単位のコマ送り、A-B ループ、指定 fps での強制再生。Windows / macOS |

Python 版の使い方・ビルド方法・仕組みは [`python/README.md`](python/README.md) を見てください。
