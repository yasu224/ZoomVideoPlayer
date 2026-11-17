"""アプリのアイコンのファイルを作り直す: .venv\\Scripts\\python.exe tools\\make_icon.py

player_brand.icon_image の絵を、assets/mary.ico (Windows の exe 用) と assets/mary.icns (macOS の .app 用) に
書き出す。どちらも中身は PNG を大きさ違いで並べたもの (Windows Vista 以降・macOS 10.7 以降で読める)。
ロゴやアイコンの絵を変えたときだけ実行して、できたファイルをコミットする (ビルドのたびには要らない)。
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
# icns の種類: 大きさ → 4 文字の名前 (PNG を入れられるもの)
ICNS_TYPES = {16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07", 256: b"ic08", 512: b"ic09", 1024: b"ic10"}


def png_bytes(size):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    from player_brand import icon_image
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    icon_image(size).save(buf, "PNG")
    buf.close()
    return bytes(data)


def write_ico(path):
    images = [(size, png_bytes(size)) for size in ICO_SIZES]
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, body = b"", b""
    for size, png in images:
        side = 0 if size >= 256 else size          # 256 は 0 と書く決まり
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(png), offset + len(body))
        body += png
    with open(path, "wb") as f:
        f.write(head + entries + body)


def write_icns(path):
    body = b""
    for size, kind in ICNS_TYPES.items():
        png = png_bytes(size)
        body += kind + struct.pack(">I", 8 + len(png)) + png
    with open(path, "wb") as f:
        f.write(b"icns" + struct.pack(">I", 8 + len(body)) + body)


def main():
    global _app
    from PySide6.QtGui import QGuiApplication
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])     # 絵を描くのに要る
    folder = os.path.join(HERE, "assets")
    write_ico(os.path.join(folder, "mary.ico"))
    write_icns(os.path.join(folder, "mary.icns"))
    from player_brand import icon_image
    icon_image(256).save(os.path.join(folder, "mary_icon_256.png"))     # 見た目の確認・説明用
    print("書き出しました:", folder)


if __name__ == "__main__":
    main()
