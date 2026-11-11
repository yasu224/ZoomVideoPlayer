# PyInstaller 設定。ビルドは build.bat (Windows) / build_mac.sh (macOS) から行う。
# 使わない Qt 部品 (QML / Quick / PDF / ソフトウェア OpenGL / 翻訳 / 画像プラグイン) を外して、
# 配布サイズと起動時間を減らしている。Qt の OpenGL 部品は GPU 描画で使うので残す。
# GPU の OpenGL が使えない PC では、アプリが自動で CPU 描画に切り替える。
# macOS では ZoomVideoPlayer.app を作る (Finder の「このアプリケーションで開く」にも対応)。
import sys

MAC = sys.platform == "darwin"

# 名前に含まれていたら外すファイル (パスは小文字・/ 区切りで比較する)。
# Windows は Qt6Quick.dll など、macOS は QtQuick.framework など
DROP = [
    "opengl32sw.dll",
    "qt6quick", "qt6qml", "qt6pdf", "qt6network", "qt6virtualkeyboard",
    "qtquick", "qtqml", "qtpdf", "qtnetwork", "qtvirtualkeyboard",
    "/translations/",
    "/plugins/imageformats/",
    "/plugins/iconengines/",
    "/plugins/generic/",
    "/plugins/networkinformation/",
    "/plugins/tls/",
    "/plugins/platforminputcontexts/",
    "/platforms/qdirect2d", "/platforms/qminimal", "/platforms/qoffscreen",
    "/platforms/libqminimal", "/platforms/libqoffscreen",
    "libcrypto", "libssl",
]


def keep(entry):
    name = (entry[0] + "|" + entry[1]).replace("\\", "/").lower()
    return not any(d in name for d in DROP)


import os

# SDI 出力用のライブラリ (build_sdi_mac.sh / build_sdi.bat で作ったもの) があれば一緒に入れる
SDI_LIB = os.path.join("sdi", "libzvp_decklink.dylib" if MAC else "zvp_decklink.dll")
SDI_BINARIES = [(SDI_LIB, "sdi")] if os.path.exists(SDI_LIB) else []

a = Analysis(
    ["zoom_player.py"],
    pathex=["."],
    binaries=SDI_BINARIES,
    excludes=["tkinter", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick"],
    # GPU 描画 (PyOpenGL) は関数内で読み込むので明示する
    hiddenimports=["OpenGL.GL", "OpenGL.GL.shaders", "OpenGL.arrays.numpymodule",
                   "OpenGL.platform.darwin" if MAC else "OpenGL.platform.win32"],
    noarchive=False,
)
a.binaries = [b for b in a.binaries if keep(b)]
a.datas = [d for d in a.datas if keep(d)]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ZoomVideoPlayer",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ZoomVideoPlayer", upx=False)

if MAC:
    app = BUNDLE(
        coll,
        name="ZoomVideoPlayer.app",
        bundle_identifier="local.zoomvideoplayer",
        info_plist={
            "CFBundleName": "Zoom Video Player",
            "CFBundleDisplayName": "Zoom Video Player",
            "NSHighResolutionCapable": True,
            # Finder で動画を右クリック →「このアプリケーションで開く」に出す
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "Video",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["public.movie"],
                # MXF などシステムが動画と見なさない形式は拡張子で受ける
                "CFBundleTypeExtensions": ["mp4", "mov", "m4v", "mkv", "avi", "mxf", "mts", "m2ts", "ts", "webm"],
            }],
        },
    )
