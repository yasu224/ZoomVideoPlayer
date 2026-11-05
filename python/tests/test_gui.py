"""メインウィンドウの操作のテスト (画面を出さない offscreen、CPU 描画)。

表示されたコマに描かれたフレーム番号を読み、狙ったコマが出ているかを確かめる。
"""
import os
import time
import unittest
from fractions import Fraction
from unittest import mock

from PySide6.QtCore import QEventLoop, QRectF, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

import player_gui
from player_view import CpuView
from tests.videos import make_video, read_index_picture

app = QApplication.instance() or QApplication([])


def wait(sec):
    loop = QEventLoop()
    QTimer.singleShot(int(sec * 1000), loop.quit)
    loop.exec()


def shown_index(w):
    return read_index_picture(w.view.picture)


class GuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = make_video(Fraction(120), 300, gop=60)

    def setUp(self):
        self.w = player_gui.MainWindow()
        self.w.resize(900, 600)
        self.w.show()
        self.w.load(self.path)

    def tearDown(self):
        self.w.close()

    def test_loads_first_frame(self):
        self.assertIsInstance(self.w.view, CpuView)
        self.assertEqual(shown_index(self.w), 0)
        self.assertEqual(self.w.frame_count(), 300)

    def test_step_forward_and_back_across_keyframes(self):
        """1 コマずつ進む・戻る。キーフレームをまたいでも、毎回そのコマが出る。"""
        w = self.w
        w.seek_paused(115 / 120)
        for expected in range(116, 126):
            w.step(1)
            self.assertEqual(shown_index(w), expected)
        for expected in range(124, 40, -1):          # 120, 60 のキーフレームをまたぐ
            w.step(-1)
            self.assertEqual((w.frame_index(), shown_index(w)), (expected, expected))

    def wait_backfill(self, timeout=10):
        w = self.w
        deadline = time.perf_counter() + timeout
        wait(0.1)
        while (w.backfill.busy or w.backfill_soon.isActive()) and time.perf_counter() < deadline:
            wait(0.02)

    def test_back_frames_are_ready_while_paused(self):
        """停止中は手前のコマを裏で用意しておくので、戻るコマ送りが最初の 1 回からすぐ出る。"""
        w = self.w
        w.seek_paused(200 / 120)
        self.wait_backfill()
        self.assertTrue(all(i in w.cache.items for i in range(200 - 48, 200)))
        t = time.perf_counter()
        for expected in range(199, 179, -1):
            w.step(-1)
            self.assertEqual(shown_index(w), expected)
        self.assertLess(time.perf_counter() - t, 0.2)

    def test_back_steps_keep_up(self):
        """続けて戻っても、残りが減ったら手前を足すので、キーフレームを何度またいでも正しいコマが出る。"""
        w = self.w
        w.seek_paused(290 / 120)
        self.wait_backfill()
        for expected in range(289, 100, -1):
            w.step(-1)
            self.assertEqual(shown_index(w), expected)
            app.processEvents()
        self.assertGreater(w.cache.ready_below(101), 0)

    def test_backfill_stops_while_playing(self):
        w = self.w
        self.assertTrue(w.decoder.started.wait(20))
        w.seek_paused(250 / 120)
        w.play()
        wait(0.2)
        self.assertIsNone(w.backfill.busy)
        w.pause()
        self.wait_backfill()
        self.assertGreater(w.cache.ready_below(w.frame_index()), 8)

    def test_seek_bar_drag_then_release_is_exact(self):
        w = self.w
        w.slider_pressed()
        w.slider.setSliderDown(True)
        for v in range(0, 280, 7):
            w.slider.setValue(v)
        app.processEvents()
        w.slider.setValue(237)
        w.slider.setSliderDown(False)
        w.slider_released()
        self.assertEqual((w.frame_index(), shown_index(w)), (237, 237))

    def test_seek_bar_click_seeks(self):
        """ドラッグせずに値が変わった (溝のクリックなど) ときも、そのコマへ移動する。"""
        self.w.slider.setValue(150)
        self.assertEqual(shown_index(self.w), 150)

    def test_keys(self):
        w = self.w
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        for _ in range(3):
            w.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Period, Qt.NoModifier, "."))
        self.assertEqual(shown_index(w), 3)
        w.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_End, Qt.NoModifier))
        self.assertEqual(shown_index(w), 299)
        w.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Home, Qt.NoModifier))
        self.assertEqual(shown_index(w), 0)

    def test_play_and_pause(self):
        w = self.w
        self.assertTrue(w.decoder.started.wait(20))
        w.play()
        wait(1.0)
        w.pause()
        self.assertGreater(w.cur_t, 0.3)
        self.assertLess(w.cur_t, 1.5)
        # 一時停止後に表示しているコマと、時刻・フレーム番号が一致している
        self.assertEqual(shown_index(w), w.frame_index())
        w.step(1)
        self.assertEqual(shown_index(w), w.frame_index())

    def test_ab_loop(self):
        w = self.w
        w.seek_paused(0.5)
        w.set_point("A")
        w.seek_paused(0.8)
        w.set_point("B")
        w.seek_paused(0.6)
        w.play()
        times = []
        for _ in range(15):
            wait(0.1)
            times.append(w.cur_t)
        w.pause()
        self.assertTrue(all(0.5 - 0.01 <= t <= 0.8 + 0.05 for t in times), times)
        self.assertLess(min(times[5:]), 0.65)        # 一度は A へ戻っている

    def test_10bit_422_video_steps(self):
        """10bit 4:2:2 の動画でも、シーク・コマ送りで狙ったコマが出る。"""
        w = self.w
        w.load(make_video(Fraction(60), 120, gop=30, pix_fmt="yuv422p10le"))
        w.seek_paused(50 / 60)
        for expected in [51, 52, 51, 50, 49, 25, 24]:
            w.step(expected - w.frame_index())
            self.assertEqual(shown_index(w), expected)

    def test_paused_position_is_prefilled(self):
        """停止中は今の位置から先読みしておき、再生ボタンではデコードをやり直さずにそれを使う。"""
        w = self.w
        self.assertTrue(w.decoder.started.wait(20))     # デコードプロセスの起動を待つ
        w.seek_paused(150 / 120)
        wait(0.5)
        self.assertEqual(w.decoder.params[0], w.cur_t)
        self.assertGreater(w.decoder.buffered(), 8)
        session = w.decoder.session
        w.play()
        wait(0.3)
        w.pause()
        self.assertEqual(w.decoder.session, session)
        self.assertGreater(w.frame_index(), 150)

    def test_whole_file_loop(self):
        w = self.w
        self.assertTrue(w.decoder.started.wait(20))
        w.toggle_loop()
        w.seek_paused(2.2)
        w.play()
        indices = []
        deadline = time.perf_counter() + 3
        while time.perf_counter() < deadline:
            wait(0.05)
            indices.append(w.frame_index())
            if any(b < a for a, b in zip(indices, indices[1:])):
                break
        w.pause()
        self.assertTrue(w.loop_on)
        self.assertTrue(any(b < a for a, b in zip(indices, indices[1:])), indices)   # 先頭へ戻った

    def test_end_without_loop_pauses(self):
        w = self.w
        self.assertTrue(w.decoder.started.wait(20))
        w.seek_paused(2.3)
        w.play()
        deadline = time.perf_counter() + 3
        while w.playing and time.perf_counter() < deadline:
            wait(0.05)
        self.assertFalse(w.playing)
        self.assertGreaterEqual(w.frame_index(), 290)

    def test_plays_at_video_fps_by_default(self):
        w = self.w
        self.assertIsNone(w.fps_combo.currentData())
        self.assertEqual(w.play_fps(), 120)
        self.assertEqual(w.speed(), 1)

    def test_forced_fps_changes_speed_not_numbering(self):
        """「60 で強制」なら 120fps の動画を 1 秒に 60 コマずつ再生する。コマ番号は動画のまま。"""
        w = self.w
        self.assertTrue(w.decoder.started.wait(20))
        w.fps_combo.setCurrentIndex(w.fps_combo.findData(60))
        self.assertEqual(w.frame_count(), 300)
        self.assertAlmostEqual(w.speed(), 0.5)
        w.seek_paused(0)
        w.play()
        wait(0.3)                         # 再生が始まるまでの分を除く
        start, t = w.frame_index(), time.perf_counter()
        wait(1.0)
        advanced = (w.frame_index() - start) / (time.perf_counter() - t)
        w.pause()
        self.assertAlmostEqual(advanced, 60, delta=12)
        w.step(1)                         # コマ送りは動画の 1 コマずつ
        self.assertEqual(shown_index(w), w.frame_index())
        w.fps_combo.setCurrentIndex(0)
        self.assertEqual(w.speed(), 1)

    def test_zoom_selection_and_undo(self):
        v = self.w.view
        v.zoom_to_rect(QRectF(v.width() * 0.4, v.height() * 0.4, v.width() * 0.2, v.height() * 0.2))
        self.assertAlmostEqual(v.zoom_level(), 5.0, places=1)
        self.assertIsNotNone(v.minimap_rect())
        v.undo()
        self.assertAlmostEqual(v.zoom_level(), 1.0, places=3)

    def test_bad_file_shows_message(self):
        bad = os.path.join(os.path.dirname(self.path), "not_a_video.mp4")
        with open(bad, "wb") as f:
            f.write(b"this is not a video" * 100)
        with mock.patch.object(QMessageBox, "warning") as warn:
            self.w.load(bad)
        warn.assert_called_once()
        self.assertEqual(self.w.path, self.path)    # 前の動画はそのまま

    def test_gpu_failure_falls_back_to_cpu(self):
        """GPU 描画が使えないと分かったら CPU 描画に切り替わり、同じコマを出し直す。"""
        w = player_gui.MainWindow.__new__(player_gui.MainWindow)
        with mock.patch.object(player_gui, "gpu_wanted", return_value=True):
            w.__init__()
        try:
            self.assertIsInstance(w.view, player_gui.GpuWindow)
            w.load(self.path)
            w.seek_paused(77 / 120)
            w.view.fail("test")
            wait(0.2)
            self.assertIsInstance(w.view, CpuView)
            self.assertEqual(shown_index(w), 77)
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
