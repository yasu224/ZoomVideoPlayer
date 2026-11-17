"""MARY のロゴとアイコン (player_brand) のテスト。"""
import os
import struct
import unittest

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

import player_brand

app = QApplication.instance() or QApplication([])


def painted(img):
    """透明でない画素の数。"""
    return sum(1 for y in range(img.height()) for x in range(img.width()) if img.pixelColor(x, y).alpha() > 0)


class BrandTest(unittest.TestCase):
    def test_logo_is_wide_wordmark(self):
        self.assertIsNotNone(player_brand.renderer())
        self.assertAlmostEqual(player_brand.aspect(), 240.5 / 59.5, places=2)

    def test_draw_logo_in_given_color(self):
        img = QImage(200, 60, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        player_brand.draw_logo(p, QRectF(0, 0, 200, 60), "#ff0000")
        p.end()
        self.assertGreater(painted(img), 1000)
        colors = {img.pixelColor(x, 30).name() for x in range(200) if img.pixelColor(x, 30).alpha() == 255}
        self.assertEqual(colors, {"#ff0000"})

    def test_icon_image(self):
        for size in (16, 256):
            img = player_brand.icon_image(size)
            self.assertEqual((img.width(), img.height()), (size, size))
            # 真ん中は背景の色か文字の色 (透明でない)
            self.assertEqual(img.pixelColor(size // 2, size // 4).alpha(), 255)
        self.assertEqual(player_brand.icon_image(256).pixelColor(128, 64), QColor(player_brand.ICON_BG))
        self.assertFalse(player_brand.app_icon().isNull())

    def test_icon_files(self):
        """配る exe / .app のアイコン (tools/make_icon.py で作ったもの) がそろっている。"""
        with open(player_brand.asset_path("mary.ico"), "rb") as f:
            reserved, kind, count = struct.unpack("<HHH", f.read(6))
        self.assertEqual((reserved, kind), (0, 1))
        self.assertGreaterEqual(count, 5)
        with open(player_brand.asset_path("mary.icns"), "rb") as f:
            head = f.read(8)
        self.assertEqual(head[:4], b"icns")
        self.assertEqual(struct.unpack(">I", head[4:])[0], os.path.getsize(player_brand.asset_path("mary.icns")))

    def test_main_window_shows_logo(self):
        import player_gui
        w = player_gui.MainWindow()
        try:
            marks = w.findChildren(player_gui.LogoMark)
            self.assertEqual(len(marks), 1)
            self.assertGreater(marks[0].width(), marks[0].height() * 3)
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
