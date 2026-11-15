"""映像入力まわり (player_live) のうち、機器の要らない部分のテスト: カメラの形式の選び方と録画。"""
import os
import tempfile
import unittest
from fractions import Fraction

import av
import numpy as np

import player_live
import player_sdi
from player_core import fill_frame, frame_size, plane_views, write_frame, yuv_planes
from player_frames import Picture


class CameraModeTest(unittest.TestCase):
    def test_prefers_1080p_around_60(self):
        modes = [(640, 480, 30.0), (1920, 1080, 30.0), (1920, 1080, 59.94), (3840, 2160, 60.0)]
        self.assertEqual(player_live.best_mode(modes), (1920, 1080, 59.94))

    def test_largest_when_no_1080p(self):
        modes = [(640, 480, 60.0), (1280, 720, 59.94), (1280, 720, 120.0)]
        self.assertEqual(player_live.best_mode(modes), (1280, 720, 59.94))
        self.assertIsNone(player_live.best_mode([]))


class FillFrameTest(unittest.TestCase):
    def test_round_trip(self):
        """fill_frame で PyAV のフレームへ写し、write_frame で戻すと元のバイト列になる (行末の詰め物があっても)。"""
        rng = np.random.default_rng(3)
        for fmt, w, h in (("p210le", 70, 34), ("nv12", 70, 34)):
            with self.subTest(fmt):
                data = rng.integers(0, 256, frame_size(fmt, w, h)).astype(np.uint8)
                frame = av.VideoFrame(w, h, fmt)
                fill_frame(frame, data, fmt, w, h)
                back = np.empty_like(data)
                write_frame(back, frame, fmt, w, h)
                np.testing.assert_array_equal(back, data)


class UnpackV210Test(unittest.TestCase):
    """SDI 入力で使う v210 → P210 のほどき方 (DeckLink のライブラリの中)。"""

    def test_round_trip(self):
        """numpy で v210 に詰めた絵をほどくと、詰める前の 10bit の値 (P210 では上位に詰めたもの) に戻る。"""
        lib = player_sdi.load_library()
        if lib is None or not hasattr(lib, "zvp_unpack_v210"):
            self.skipTest("SDI 用のライブラリがない")
        import ctypes
        rng = np.random.default_rng(5)
        for w, h in ((1920, 4), (1280, 3), (64, 2)):          # 1280 は 6 の倍数でない
            with self.subTest(width=w):
                rgb = rng.random((h, w, 3))
                row_bytes = (w + 47) // 48 * 128
                packed = player_sdi.pack_reference(rgb, True, row_bytes)
                out = np.zeros(frame_size("p210le", w, h), np.uint8)
                lib.zvp_unpack_v210(ctypes.c_void_p(packed.ctypes.data), ctypes.c_int64(row_bytes),
                                    ctypes.c_void_p(out.ctypes.data), ctypes.c_int32(w), ctypes.c_int32(h))
                y, u, v = yuv_planes(out, "p210le", w, h)
                ey, ecb, ecr = player_sdi.rgb_to_codes(rgb, True)
                code = lambda a: np.clip(np.floor(a + 0.5), 4, 1019).astype(np.int64)   # noqa: E731
                np.testing.assert_array_equal(y, code(ey))
                np.testing.assert_array_equal(u, code((ecb[:, 0::2] + ecb[:, 1::2]) / 2))
                np.testing.assert_array_equal(v, code((ecr[:, 0::2] + ecr[:, 1::2]) / 2))


class RecorderTest(unittest.TestCase):
    def test_records_10bit_422(self):
        """10bit 4:2:2 (P210) の入力を、10bit 4:2:2 の動画に書き出す。コマ数と色がおおよそ保たれる。"""
        if player_live.pick_encoder("p210le") is None:
            self.skipTest("書き出し方式がない")
        w, h, n = 320, 180, 30
        data = np.zeros(frame_size("p210le", w, h), np.uint8)
        y, uv = plane_views(data, "p210le", w, h)
        y[:] = 500 << 6
        uv[:, 0::2], uv[:, 1::2] = 300 << 6, 700 << 6
        path = os.path.join(tempfile.mkdtemp(), "rec.mov")
        rec = player_live.Recorder(path, "p210le", w, h, Fraction(60000, 1001))
        for i in range(n):
            rec.push(Picture(i / 60, "p210le", w, h, data))
        rec.stop()
        self.assertIsNone(rec.error)
        self.assertEqual((rec.written, rec.dropped), (n, 0))
        with av.open(path) as c:
            s = c.streams.video[0]
            frames = list(c.decode(s))
            self.assertEqual(len(frames), n)
            self.assertIn(s.codec_context.pix_fmt, ("yuv422p10le", "p210le"))
            f = frames[-1].reformat(format="yuv422p10le")
            got = np.empty(frame_size("yuv422p10le", w, h), np.uint8)
            write_frame(got, f, "yuv422p10le", w, h)
            gy, gu, gv = yuv_planes(got, "yuv422p10le", w, h)
            self.assertLess(abs(int(np.median(gy)) - 500), 6)
            self.assertLess(abs(int(np.median(gu)) - 300), 6)
            self.assertLess(abs(int(np.median(gv)) - 700), 6)

    def test_drops_other_sizes(self):
        """入力の形式が途中で変わったコマは、録画に入れずに数える。"""
        if player_live.pick_encoder("nv12") is None:
            self.skipTest("書き出し方式がない")
        path = os.path.join(tempfile.mkdtemp(), "rec.mov")
        rec = player_live.Recorder(path, "nv12", 64, 32, Fraction(30))
        rec.push(Picture(0, "nv12", 64, 32, np.zeros(frame_size("nv12", 64, 32), np.uint8)))
        rec.push(Picture(1, "nv12", 128, 64, np.zeros(frame_size("nv12", 128, 64), np.uint8)))
        rec.stop()
        self.assertEqual((rec.written, rec.dropped), (1, 1))
