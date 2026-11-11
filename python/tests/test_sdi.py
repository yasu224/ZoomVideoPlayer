"""SDI 出力のうち、機器も OpenGL も要らない部分のテスト (拡大の置き換え、詰め方、メニュー)。

GPU が実際に詰めたバイト列は tools/check_sdi.py で本物の画面を使って確かめる。
"""
import unittest

import numpy as np

import player_sdi


class ViewTransformTest(unittest.TestCase):
    def test_fit_maps_to_fit(self):
        """全体表示なら、出力でも全体表示 (真ん中に寄せて縦横比のまま) になる。"""
        fit = min(1000 / 1920, 500 / 1080)
        tx = (1000 - 1920 * fit) / 2
        s, tx, ty = player_sdi.view_transform(1000, 500, fit, tx, 0, 1920, 1080, 1920, 1080)
        self.assertAlmostEqual(s, 1.0)
        self.assertAlmostEqual(tx, 0.0)
        self.assertAlmostEqual(ty, 0.0)

    def test_zoom_keeps_center_point(self):
        """拡大中は、画面の真ん中に見えている画像の点が出力の真ん中に来て、倍率も同じ。"""
        sw, sh, iw, ih = 1200, 600, 1920, 1080
        fit = min(sw / iw, sh / ih)
        s = fit * 4
        cx, cy = 700.0, 300.0                     # 画面の真ん中に見せる画像の点
        tx, ty = sw / 2 - cx * s, sh / 2 - cy * s
        so, ox, oy = player_sdi.view_transform(sw, sh, s, tx, ty, iw, ih, 1920, 1080)
        self.assertAlmostEqual(so, 4.0)
        self.assertAlmostEqual((960 - ox) / so, cx)
        self.assertAlmostEqual((540 - oy) / so, cy)

    def test_edge_is_clamped(self):
        """画像の端を見ているときは、出力でも画像の外 (黒) が出ないように寄せる。"""
        so, ox, oy = player_sdi.view_transform(1200, 600, 2.0, 0, 0, 1920, 1080, 1920, 1080)
        self.assertLessEqual(ox, 0)
        self.assertLessEqual(oy, 0)
        self.assertGreaterEqual(ox + 1920 * so, 1920)
        self.assertGreaterEqual(oy + 1080 * so, 1080)


class PackTest(unittest.TestCase):
    def test_2vuy_order(self):
        """2vuy は Cb Y0 Cr Y1 の順で、色差は 2 画素の平均。白と黒の映像範囲の値になる。"""
        rgb = np.array([[[1, 1, 1], [0, 0, 0]]], np.float64)
        out = player_sdi.pack_reference(rgb, False, 4)
        self.assertEqual(out.tolist(), [[128, 235, 128, 16]])

    def test_v210_words(self):
        """v210 は 6 画素を 4 語に詰める。灰色なら各語の 3 つの値が決まった並びになる。"""
        rgb = np.zeros((1, 6, 3))
        rgb[0, :, :] = np.linspace(0, 1, 6)[:, None]     # 灰色の階段 (色差は 512)
        out = player_sdi.pack_reference(rgb, True, 128)
        words = out[0, :16].copy().view("<u4")
        ys = [64 + round(876 * v) for v in np.linspace(0, 1, 6)]
        f = lambda a, b, c: a | b << 10 | c << 20       # noqa: E731
        self.assertEqual(words.tolist(), [f(512, ys[0], 512), f(ys[1], 512, ys[2]),
                                          f(512, ys[3], 512), f(ys[4], 512, ys[5])])

    def test_draws_once_per_device_frame(self):
        """停止中などは、機器の出力のコマごとに 1 回だけ描く。画面の書き換えが 120Hz でも 60 回/秒。"""
        out = player_sdi.SdiOutput.__new__(player_sdi.SdiOutput)
        out.period, out.rendered_frame = 1 / 60, None
        now = [0.0]
        out.clock = lambda: (int(now[0] * 60), now[0] * 60 % 1)    # 60Hz の機器のクロック
        drawn = 0
        for i in range(1, 1201):                 # 10 秒、少し揺れる 120Hz の書き換え
            now[0] = 100 + i / 120 + (0.002 if i % 3 == 0 else -0.001)
            if out.due():
                out.rendered()
                drawn += 1
            else:
                self.assertGreater(out.wait_time(), 0)
        self.assertAlmostEqual(drawn / 10, 60, delta=1)


class MenuTest(unittest.TestCase):
    def test_menu_explains_when_unavailable(self):
        """CPU 描画では SDI 出力を選べず、理由がメニューに出る。"""
        from PySide6.QtWidgets import QApplication

        import player_gui
        QApplication.instance() or QApplication([])
        w = player_gui.MainWindow()
        try:
            w.refresh_sdi_modes()
            texts = [w.sdi_combo.itemText(i) for i in range(w.sdi_combo.count())]
            self.assertEqual(texts[0], "オフ")
            self.assertIn("GPU 描画のときだけ", texts[-1])
            self.assertIsNone(w.sdi)
        finally:
            w.close()


class SequenceTest(unittest.TestCase):
    def test_integer_ratios(self):
        """動画の進む速さと出力の fps が整数倍なら、並べ方 (出力の n コマに 1 枚, m 枚に 1 枚) が決まる。"""
        f = player_sdi.sequence_for
        self.assertEqual(f(59.94, 59.94), (1, 1))
        self.assertEqual(f(59.94, 60.0), (1, 1))            # 0.1% の違いは同じとみなす
        self.assertEqual(f(59.94, 29.97), (2, 1))
        self.assertEqual(f(59.94, 119.88), (1, 2))
        self.assertEqual(f(59.94, 120.0), (1, 2))
        self.assertEqual(f(24.0, 120.0), (1, 5))

    def test_other_ratios(self):
        """整数倍でなければ None (機器のコマごとに、そのとき画面に出ているコマを出す)。"""
        f = player_sdi.sequence_for
        self.assertIsNone(f(59.94, 50.0))
        self.assertIsNone(f(59.94, 23.976))
        self.assertIsNone(f(50.0, 59.94))
        self.assertIsNone(f(59.94, 0.0))
