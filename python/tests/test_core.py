"""デコード側 (player_core) と受け渡し (player_frames) のテスト。"""
import time
import unittest
from fractions import Fraction

import numpy as np

from player_core import (WRAP, VideoSource, even_size, frame_size, gpu_format, hw_device, plane_sizes, plane_views,
                         texture_scale, write_frame, yuv_planes)
from player_frames import BackFiller, Decoder, FrameCache, Picture
from tests.videos import make_video, read_index_frame, read_index_picture, read_index_y


class VideoSourceTest(unittest.TestCase):
    def test_metadata_120(self):
        src = VideoSource(make_video(Fraction(120), 300))
        self.assertAlmostEqual(src.fps, 120.0)
        self.assertAlmostEqual(src.duration, 300 / 120, places=2)
        self.assertEqual((src.width, src.height), (320, 180))
        src.close()

    def test_metadata_11988(self):
        src = VideoSource(make_video(Fraction(120000, 1001), 120))
        self.assertAlmostEqual(src.fps, 119.88, places=2)
        src.close()

    def test_frame_at_is_exact(self):
        """どのコマへシークしても、そのコマが出る (キーフレームの前後・B フレームも含めて)。"""
        src = VideoSource(make_video(Fraction(120), 300, gop=60))
        for i in [0, 1, 2, 58, 59, 60, 61, 119, 120, 121, 179, 200, 241, 298, 299]:
            frame = src.frame_at(i / src.fps, src.fps)
            self.assertEqual(read_index_frame(frame), i, f"frame {i}")
        src.close()

    def test_sequential_decode_after_seek(self):
        src = VideoSource(make_video(Fraction(120), 300, gop=60))
        src.frame_at(100 / 120, 120)
        got = [read_index_frame(src.next()) for _ in range(30)]
        self.assertEqual(got, list(range(101, 131)))
        src.close()

    def test_end_of_file(self):
        src = VideoSource(make_video(Fraction(120), 300))
        src.seek(290 / 120)
        frames = []
        while (f := src.next()) is not None:
            frames.append(read_index_frame(f))
        self.assertEqual(frames[-1], 299)
        self.assertIsNone(src.next())
        src.close()


class FrameLayoutTest(unittest.TestCase):
    def test_write_frame_yuv_planes(self):
        """I420 に詰めた Y/U/V が、デコード結果の各プレーンと一致する。"""
        src = VideoSource(make_video(Fraction(120), 300))
        frame = src.frame_at(10 / 120, 120)
        w, h = even_size("yuv420p", src.width, src.height)
        dst = np.empty(frame_size("yuv420p", w, h), np.uint8)
        write_frame(dst, frame, "yuv420p", w, h)
        ref = frame.to_ndarray(format="yuv420p")
        np.testing.assert_array_equal(dst, ref.reshape(-1))
        self.assertEqual(read_index_y(plane_views(dst, "yuv420p", w, h)[0]), 10)
        src.close()

    def test_10bit_422_passes_through(self):
        """10bit 4:2:2 (XAVC と同じ) は変換せず、そのままの値で詰める。"""
        src = VideoSource(make_video(Fraction(60), 120, pix_fmt="yuv422p10le"))
        self.assertEqual(src.pix_fmt, "yuv422p10le")
        self.assertEqual(gpu_format(src.pix_fmt), "yuv422p10le")
        frame = src.frame_at(37 / 60, 60)
        fmt = gpu_format(src.pix_fmt)
        w, h = even_size(fmt, src.width, src.height)
        dst = np.empty(frame_size(fmt, w, h), np.uint8)
        self.assertEqual(dst.nbytes, (w * h + 2 * (w // 2) * h) * 2)
        write_frame(dst, frame, fmt, w, h)
        y, u, v = plane_views(dst, fmt, w, h)
        self.assertEqual((y.shape, u.shape, v.shape), ((h, w), (h, w // 2), (h, w // 2)))
        self.assertEqual(y.dtype, np.uint16)
        plane = frame.planes[0]
        ref = np.frombuffer(plane, np.uint16, count=plane.line_size // 2 * h).reshape(h, -1)[:, :w]
        np.testing.assert_array_equal(y, ref)
        self.assertEqual(read_index_y(y >> 2), 37)
        self.assertGreater(int(y.max()), 255)            # 10bit の値のまま
        src.close()

    def test_unsupported_format_falls_back(self):
        self.assertEqual(gpu_format("nv12"), "yuv420p")
        self.assertEqual(gpu_format("rgb24"), "yuv420p")

    def test_write_frame_bgra(self):
        src = VideoSource(make_video(Fraction(120), 300))
        frame = src.frame_at(33 / 120, 120)
        dst = np.empty(frame_size("bgra", src.width, src.height), np.uint8)
        write_frame(dst, frame, "bgra", src.width, src.height)
        self.assertEqual(read_index_y(dst[:, :, 1]), 33)
        src.close()

    def test_even_size(self):
        self.assertEqual(even_size("yuv420p", 1921, 1081), (1920, 1080))
        self.assertEqual(even_size("bgra", 1921, 1081), (1921, 1081))
        self.assertEqual(even_size("p210le", 1921, 1081), (1920, 1081))


class SemiFormatTest(unittest.TestCase):
    """ハードウェアデコードが返す、色差を交互に並べた形式 (NV12 / P010 / P210)。"""

    CASES = [("nv12", "yuv420p", 8), ("p010le", "yuv420p10le", 10), ("p210le", "yuv422p10le", 10)]

    def test_layout(self):
        self.assertEqual(plane_sizes("nv12", 64, 32), [(64, 32), (64, 16)])        # CbCr は 32 組 × 16 行
        self.assertEqual(plane_sizes("p210le", 64, 32), [(64, 32), (64, 32)])
        self.assertEqual(frame_size("p010le", 64, 32), (64 * 32 * 2 + 64 * 16 * 2,))
        self.assertEqual(frame_size("p210le", 64, 32), frame_size("yuv422p10le", 64, 32))

    def test_texture_scale(self):
        """10bit は 16bit の上位に詰めてあるので、最大の値 (1023 << 6) が 1 になる。"""
        self.assertEqual(texture_scale("nv12"), 1.0)
        self.assertAlmostEqual(texture_scale("p010le") * (1023 << 6) / 65535, 1.0)
        self.assertAlmostEqual(texture_scale("yuv420p10le") * 1023 / 65535, 1.0)

    def test_write_frame_matches_planar(self):
        """詰めた値を Y, U, V に分けると、同じコマをプレーン形式にしたものと一致する。"""
        src = VideoSource(make_video(Fraction(60), 120, pix_fmt="yuv422p10le"))
        frame = src.frame_at(21 / 60, 60)
        for semi, planar, bits in self.CASES:
            with self.subTest(semi):
                w, h = even_size(semi, src.width, src.height)
                got = np.empty(frame_size(semi, w, h), np.uint8)
                write_frame(got, frame.reformat(format=semi), semi, w, h)
                ref = np.empty(frame_size(planar, w, h), np.uint8)
                write_frame(ref, frame.reformat(format=planar), planar, w, h)
                for a, b in zip(yuv_planes(got, semi, w, h), plane_views(ref, planar, w, h)):
                    np.testing.assert_array_equal(a, b)
                self.assertEqual(read_index_y(yuv_planes(got, semi, w, h)[0] >> (bits - 8)), 21)
        src.close()


class FrameCacheTest(unittest.TestCase):
    def picture(self, nbytes):
        return Picture(0.0, "yuv420p", 2, 2, np.zeros(nbytes, np.uint8))

    def test_evicts_oldest_over_budget(self):
        cache = FrameCache(budget_bytes=300)
        for i in range(5):
            cache.put(i, self.picture(100))
        self.assertEqual(list(cache.items), [2, 3, 4])
        self.assertLessEqual(cache.bytes, 300)

    def test_get_refreshes_order(self):
        cache = FrameCache(budget_bytes=300)
        for i in range(3):
            cache.put(i, self.picture(100))
        cache.get(0)
        cache.put(3, self.picture(100))
        self.assertIn(0, cache.items)
        self.assertNotIn(1, cache.items)

    def test_capacity(self):
        self.assertEqual(FrameCache(budget_bytes=1000).capacity(300), 3)

    def test_evicts_farthest_from_focus(self):
        """表示中のコマから遠いものを捨てる。先のコマは手前のコマより遠いとみなす。"""
        cache = FrameCache(budget_bytes=500)
        cache.focus = 50
        for i in [50, 52, 49, 48, 47]:
            cache.put(i, self.picture(100))
        cache.put(46, self.picture(100))         # 52 (先へ 2 = 手前へ 8 相当) を捨てる
        self.assertEqual(sorted(cache.items), [46, 47, 48, 49, 50])
        cache.put(10, self.picture(100))         # いちばん遠いのは今入れた 10
        self.assertNotIn(10, cache.items)
        self.assertEqual(cache.ready_below(50), 4)


class BackFillerTest(unittest.TestCase):
    def test_returns_requested_range_in_order(self):
        path = make_video(Fraction(120), 300, gop=60)
        filler = BackFiller(path, "yuv420p", 320, 180)
        try:
            filler.request(130, 140, 120.0)
            got = []
            while True:
                gen, i, picture = filler.results.get(timeout=20)
                if i is None:
                    filler.finished(gen)
                    break
                self.assertEqual(read_index_picture(picture), i)
                self.assertAlmostEqual(picture.t, i / 120, places=4)
                got.append(i)
            self.assertEqual(got, list(range(120, 141)))     # キーフレーム 120 から読んだ分も返る
            self.assertIsNone(filler.busy)
        finally:
            filler.close()


class DecoderProcessTest(unittest.TestCase):
    """別プロセスのデコードから共有メモリ経由で正しいコマが順に届く。"""

    def run_session(self, fmt, hw=False):
        path = make_video(Fraction(120), 300)
        src = VideoSource(path)
        w, h = even_size(fmt, src.width, src.height)
        src.close()
        dec = Decoder(path, fmt, w, h, hw=hw)
        try:
            dec.play(50 / 120, 120.0)
            got = []
            deadline = time.perf_counter() + 20
            while len(got) < 40 and time.perf_counter() < deadline:
                item = dec.poll()
                if item is None:
                    time.sleep(0.002)
                    continue
                slot, t = item
                self.assertGreaterEqual(slot, 0)
                got.append(read_index_picture(dec.pictures[slot]))
                dec.release(slot)
            self.assertEqual(got, list(range(50, 90)))
            dec.stop()
            self.assertFalse(dec.active)
            # 停止後にもう一度別の位置から始められる
            dec.play(200 / 120, 120.0)
            deadline = time.perf_counter() + 20
            while (item := dec.poll()) is None and time.perf_counter() < deadline:
                time.sleep(0.002)
            self.assertEqual(read_index_picture(dec.pictures[item[0]]), 200)
            dec.release(item[0])
        finally:
            dec.close()

    def test_loop_wraps_inside_decoder(self):
        """ループはデコード側で折り返す: B までのコマ → 折り返しの印 → A からのコマ、の順に届く。"""
        path = make_video(Fraction(120), 300)
        dec = Decoder(path, "yuv420p", 320, 180)
        try:
            self.assertGreater(dec.slots, 8)
            dec.play(12 / 120, 120.0, loop=(10 / 120, 20 / 120))
            seq = []
            deadline = time.perf_counter() + 20
            while len(seq) < 30 and time.perf_counter() < deadline:
                item = dec.poll()
                if item is None:
                    time.sleep(0.002)
                    continue
                slot, t = item
                if slot == WRAP:
                    seq.append("wrap")
                    self.assertAlmostEqual(t, 21 / 120, places=4)   # B のコマの終わり
                    continue
                seq.append(read_index_picture(dec.pictures[slot]))
                dec.release(slot)
            expected = list(range(12, 21)) + ["wrap"] + list(range(10, 21)) + ["wrap"] + list(range(10, 18))
            self.assertEqual(seq[:len(expected)], expected)
        finally:
            dec.close()

    def test_yuv_session(self):
        self.run_session("yuv420p")

    def test_bgra_session(self):
        self.run_session("bgra")

    def test_hw_session(self):
        """ハードウェアデコード (NV12) でも、同じコマが順に届く。"""
        if hw_device() is None:
            self.skipTest("この PC ではハードウェアデコードを使えない")
        self.run_session("nv12", hw=True)


if __name__ == "__main__":
    unittest.main()
