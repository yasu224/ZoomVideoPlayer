"""映像入力まわり (player_live) のうち、機器の要らない部分のテスト: カメラの形式の選び方と録画。"""
import os
import tempfile
import sys
import unittest
from fractions import Fraction

import av
import numpy as np

import player_capture
import player_live
import player_sdi
from player_core import fill_frame, frame_size, plane_views, write_frame, yuv_planes
from player_frames import Picture


class CameraModeTest(unittest.TestCase):
    def test_prefers_1080p_around_60(self):
        modes = [(640, 480, 30.0), (1920, 1080, 30.0), (1920, 1080, 59.94), (3840, 2160, 60.0)]
        self.assertEqual(player_capture.best_mode(modes), (1920, 1080, 59.94))

    def test_largest_when_no_1080p(self):
        modes = [(640, 480, 60.0), (1280, 720, 59.94), (1280, 720, 120.0)]
        self.assertEqual(player_capture.best_mode(modes), (1280, 720, 59.94))
        self.assertIsNone(player_capture.best_mode([]))

    def test_windows_device_names(self):
        """Windows (DirectShow) の機器一覧を、FFmpeg の古い書き方でも新しい書き方でも読める。"""
        new = [
            '"Live Gamer ULTRA S GC553Pro"', ' (video', ')', '',
            '  Alternative name "@device_pnp_\\\\?\\usb#vid_07ca&pid_1553"',
            '"Streaming Center Virtual Camera" (video)',
            '  Alternative name "@device_sw_{860BB310}"',
            '"HDMI (Live Gamer ULTRA S GC553Pro)"', ' (audio', ')', '',
            '"Both" (video, audio)',
            '"Nothing" (none)',
        ]
        self.assertEqual(player_capture.parse_device_names(new, "dshow"),
                         ["Live Gamer ULTRA S GC553Pro", "Streaming Center Virtual Camera", "Both"])
        old = [
            'DirectShow video devices (some may be both video and audio devices)',
            ' "USB Camera"',
            '    Alternative name "@device_pnp_x"',
            'DirectShow audio devices',
            ' "Microphone"',
        ]
        old = [line.strip() if '"' in line and "Alternative" not in line else line for line in old]
        self.assertEqual(player_capture.parse_device_names(old, "dshow"), ["USB Camera"])
        mac = ['AVFoundation video devices:', '[0] FaceTime HD Camera', '[1] Capture screen 0',
               'AVFoundation audio devices:', '[0] MacBook Pro Microphone']
        self.assertEqual(player_capture.parse_device_names(mac, "avfoundation"),
                         ["FaceTime HD Camera", "Capture screen 0"])

    def test_windows_modes(self):
        """Windows (DirectShow) の一覧から形式も選ぶ。同じ大きさと fps なら圧縮していない形式を選ぶ。"""
        lines = [
            'DirectShow video device options (from video devices)',
            ' Pin "Capture" (alternative pin name "0")',
            '  pixel_format=yuyv422  min s=640x480 fps=5 max s=640x480 fps=30',
            '  pixel_format=yuyv422  min s=1920x1080 fps=5 max s=1920x1080 fps=5',
            '  vcodec=mjpeg  min s=1920x1080 fps=5 max s=1920x1080 fps=60.0002',
            '  vcodec=mjpeg  min s=1920x1080 fps=5 max s=1920x1080 fps=60.0002 (tv, bt470bg/bt709/unknown, center)',
            '  pixel_format=nv12  min s=1280x720 fps=5 max s=1280x720 fps=60',
            '  vcodec=mjpeg  min s=1280x720 fps=5 max s=1280x720 fps=60',
        ]
        modes = player_capture.parse_dshow_modes(lines)
        self.assertEqual(len(modes), 5)
        self.assertEqual(player_capture.best_mode(modes), (1920, 1080, 60.0002, ("vcodec", "mjpeg")))
        same = [m for m in modes if m[:2] == (1280, 720)]
        self.assertEqual(player_capture.best_mode(same), (1280, 720, 60.0, ("pixel_format", "nv12")))


    def test_windows_modes_split_lines(self):
        """新しい FFmpeg ではログが切れて届き、形式と大きさが別の行に来る (GC553Pro で見た形)。"""
        lines = [
            'DirectShow video device options (from video devices)',
            ' Pin "Capture" (alternative pin name "0")',
            '  pixel_format=yuyv422', '  min s=1920x1080 fps=29.97 max s=1920x1080 fps=60.0002', '',
            '  pixel_format=yuyv422', '  min s=1920x1080 fps=29.97 max s=1920x1080 fps=60.0002',
            ' (tv, bt709/bt709/unknown', ', topleft', ')', '',
            '  pixel_format=yuyv422', '  min s=1280x720 fps=29.97 max s=1280x720 fps=60.0002', '',
            '  vcodec=mjpeg', '  min s=3840x2160 fps=5 max s=3840x2160 fps=30', '',
        ]
        self.assertEqual(player_capture.parse_dshow_modes(lines), [
            (1280, 720, 60.0002, ("pixel_format", "yuyv422")),
            (1920, 1080, 60.0002, ("pixel_format", "yuyv422")),
            (3840, 2160, 30.0, ("vcodec", "mjpeg")),
        ])

    def test_mode_label_and_key(self):
        """形式のメニューの文字と、設定に残す文字。"""
        mac = (1920, 1080, 59.94)
        win = (1920, 1080, 120.0, ("pixel_format", "yuyv422"))
        self.assertEqual(player_capture.mode_label(mac), "1920×1080 59.94fps")
        self.assertEqual(player_capture.mode_label(win), "1920×1080 120.00fps (yuyv422)")
        self.assertEqual(player_capture.mode_key(mac), "1920x1080@59.9400")
        self.assertEqual(player_capture.mode_key(win), "1920x1080@120.0000@pixel_format=yuyv422")
        self.assertNotEqual(player_capture.mode_key(win),
                            player_capture.mode_key((1920, 1080, 120.0, ("vcodec", "mjpeg"))))

    def test_chosen_mode_is_used(self):
        """形式を選んでいれば、自動で選ばずにその形式でカメラを開く。"""
        opened = []

        class FakeAv:
            @staticmethod
            def open(url, format=None, options=None, timeout=None):
                opened.append(options)
        real_modes, real_av, real_lib = player_capture.camera_modes, sys.modules["av"], player_capture.avcam_library
        player_capture.camera_modes = lambda index, name: [(1920, 1080, 60.0), (1920, 1080, 120.0)]
        player_capture.avcam_library = lambda: None        # FFmpeg で開く道 (AVFoundation のライブラリが無いとき)
        sys.modules["av"] = FakeAv
        try:
            player_capture.open_camera(0, "cam")
            player_capture.open_camera(0, "cam", (1920, 1080, 120.0))
        finally:
            player_capture.camera_modes, sys.modules["av"] = real_modes, real_av
            player_capture.avcam_library = real_lib
        self.assertEqual((opened[0]["video_size"], opened[0]["framerate"]), ("1920x1080", "60"))
        self.assertEqual((opened[1]["video_size"], opened[1]["framerate"]), ("1920x1080", "120"))


class AvCamTest(unittest.TestCase):
    """macOS で AVFoundation から読む側 (player_capture.AvCamCamera)。ライブラリの代わりに作り物を使う。"""

    class FakeLib:
        def __init__(self, frames, opens=True):
            self.frames, self.opens, self.calls, self.closed = list(frames), opens, [], False
            self.seq = 0

        def zvp_cam_open(self, name, w, h, fps, err, errlen):
            self.calls.append((name, w, h, fps))
            if not self.opens:
                err.value = "3840×2160 60.00fps の形式がありません".encode()
                return None
            return 1

        def zvp_cam_fps(self, handle):
            return 60.00024

        def zvp_cam_latest(self, handle, w, h, t):
            if self.frames:              # 呼ばれるたびに 1 コマ届いたことにする
                self.frames.pop(0)
                self.seq += 1
                w._obj.value, h._obj.value, t._obj.value = 4, 2, self.seq / 60
            return self.seq

        def zvp_cam_copy(self, handle, dst, size, w, h):
            import ctypes
            ctypes.memset(dst, self.seq, w * h * 3 // 2)
            return self.seq

        def zvp_cam_close(self, handle):
            self.closed = True

    def test_frames_and_empty_polls(self):
        lib = self.FakeLib([1, 2])
        cam = player_capture.AvCamCamera(lib, "4K U3 Camera", (3840, 2160, 60.0))
        self.assertEqual(lib.calls, [(b"4K U3 Camera", 3840, 2160, 60.0)])
        self.assertEqual(cam.streams.video[0].average_rate, Fraction(60))
        packets = cam.demux(cam.streams.video[0])
        got = [next(packets).decode() for _ in range(4)]
        self.assertEqual([len(g) for g in got], [1, 1, 0, 0])      # 届かない間も止める合図を見られるよう空で返す
        frame = got[1][0]
        self.assertEqual((frame.format.name, frame.width, frame.height), ("nv12", 4, 2))
        dst = np.zeros(12, np.uint8)
        self.assertTrue(frame.write_into(dst, 4, 2))
        self.assertTrue((dst == 2).all())
        cam.close()
        self.assertTrue(lib.closed)

    def test_open_camera_uses_library_on_mac(self):
        """macOS でライブラリがあれば、選んだ形式 (無ければ自動で選んだ形式) で AVFoundation から開く。"""
        if player_capture.camera_format() != "avfoundation":
            self.skipTest("macOS だけ")
        lib = self.FakeLib([])
        real_modes, real_lib = player_capture.camera_modes, player_capture.avcam_library
        player_capture.camera_modes = lambda index, name: [(1920, 1080, 60.0), (3840, 2160, 60.0)]
        player_capture.avcam_library = lambda: lib
        try:
            self.assertIsInstance(player_capture.open_camera(0, "cam"), player_capture.AvCamCamera)
            player_capture.open_camera(0, "cam", (3840, 2160, 60.0))
        finally:
            player_capture.camera_modes, player_capture.avcam_library = real_modes, real_lib
        self.assertEqual(lib.calls, [(b"cam", 1920, 1080, 60.0), (b"cam", 3840, 2160, 60.0)])

    def test_open_failure_has_reason(self):
        with self.assertRaisesRegex(OSError, "形式がありません"):
            player_capture.AvCamCamera(self.FakeLib([], opens=False), "cam", (3840, 2160, 60.0))

    def test_capture_writes_avcam_frames(self):
        """CameraCapture が AVFoundation のコマを共有メモリへ写す (FFmpeg のコマと同じ流れで)。"""
        import multiprocessing as mp
        import queue
        import threading
        import time
        lib = self.FakeLib([1, 2, 3])
        state = mp.get_context("spawn").Array("d", [-1, 0, -1, 0, 0, 0, 0, 0, 0])
        messages, stop = queue.Queue(), threading.Event()
        cap = player_capture.CameraCapture(lambda: player_capture.AvCamCamera(lib, "cam", (4, 2, 60.0)),
                                           state, messages, stop)
        thread = threading.Thread(target=cap.run)
        thread.start()
        time.sleep(0.2)
        stop.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())                  # コマが来なくても止まる
        self.assertEqual(int(state[player_capture.S_FRAMES]), 3)
        fmt = messages.get_nowait()
        self.assertEqual(fmt[0], "format")
        self.assertEqual(fmt[3:6], ("nv12", 4, 2))
        self.assertTrue(lib.closed)


class CameraCaptureTest(unittest.TestCase):
    """カメラを読む側 (別プロセスで動く player_capture.CameraCapture) を、作り物のカメラで同じプロセスの中で動かす。"""

    class FakeContainer:
        class Stream:
            average_rate = Fraction(30)
            guessed_rate = None

        def __init__(self, error=None, frames=()):
            self.streams = type("S", (), {"video": [self.Stream()]})()
            self.error, self.frames, self.closed = error, list(frames), False

        def demux(self, stream):
            for f in self.frames:
                yield type("P", (), {"decode": lambda self, f=f: [f]})()
            if self.error:
                raise self.error

        def close(self):
            self.closed = True

    def run_capture(self, opener, seconds=0.4):
        import multiprocessing as mp
        import queue
        import threading
        import time
        state = mp.get_context("spawn").Array("d", [-1, 0, -1, 0, 0, 0, 0, 0, 0])
        messages, stop = queue.Queue(), threading.Event()
        cap = player_capture.CameraCapture(opener, state, messages, stop)
        cap.RETRY_SECONDS = 0.02
        thread = threading.Thread(target=cap.run)
        thread.start()
        time.sleep(seconds)
        stop.set()
        thread.join(5)
        out = []
        while not messages.empty():
            out.append(messages.get())
        return cap, state, out

    def test_read_failures_are_no_signal(self):
        """映像が届かず読み取りに失敗しても、エラーにせずに開き直し続ける (開いたものは閉じる)。"""
        opened = []

        def opener():
            c = self.FakeContainer(error=OSError(5, "Input/output error"))
            opened.append(c)
            return c
        _, state, messages = self.run_capture(opener)
        self.assertGreater(len(opened), 3)
        self.assertTrue(all(c.closed for c in opened))
        self.assertFalse([m for m in messages if m[0] == "error"])
        self.assertIn("Input/output error", messages[-1][1])
        self.assertEqual(state[player_capture.S_FRAMES], 0)

    def test_open_failures_become_error(self):
        def opener():
            raise OSError(5, "Input/output error")
        _, _, messages = self.run_capture(opener)
        self.assertTrue(any(m[0] == "error" and "カメラを開けません" in m[1] for m in messages))

    def test_frames_go_to_shared_memory(self):
        """届いたコマは共有メモリのバッファに書かれ、形式と最新のバッファが知らされる。"""
        from player_core import attach_shared_memory
        frame = av.VideoFrame(64, 32, "nv12")
        frame.planes[0].update(bytes([77]) * frame.planes[0].buffer_size)
        frame.planes[1].update(bytes([128]) * frame.planes[1].buffer_size)
        cap, state, messages = self.run_capture(lambda: self.FakeContainer(frames=[frame] * 3), seconds=0.1)
        fmt_msgs = [m for m in messages if m[0] == "format"]
        self.assertEqual(fmt_msgs[0][3:6], ("nv12", 64, 32))
        self.assertGreaterEqual(state[player_capture.S_FRAMES], 3)
        self.assertGreaterEqual(state[player_capture.S_LATEST], 0)
        # 止めたら共有メモリは片付けられている
        with self.assertRaises(FileNotFoundError):
            attach_shared_memory(fmt_msgs[-1][2])


    def test_not_ready_is_waited_not_reopened(self):
        """macOS のカメラは次のコマがまだ無いと「今は読めない」を返す。開き直さずに待って読み続け、
        fps を知らせないカメラでは、コマの時刻から fps を求める。"""
        frames = []
        for i in range(80):
            frame = av.VideoFrame(64, 32, "nv12")
            frame.pts, frame.time_base = i * 1_000_000 // 60, Fraction(1, 1_000_000)
            frames.append(frame)

        class NotReadyContainer(self.FakeContainer):
            class Stream:
                average_rate = None
                guessed_rate = Fraction(1_000_000)     # カメラは時刻の単位を返してくる

            def demux(self, stream):
                while self.frames:
                    frame = self.frames.pop(0)
                    yield type("P", (), {"decode": lambda self, f=frame: [f]})()
                    raise BlockingIOError(35, "Resource temporarily unavailable")
                while True:                            # 次のコマが来ないまま
                    raise BlockingIOError(35, "Resource temporarily unavailable")
                    yield

        opened = []

        def opener():
            opened.append(NotReadyContainer(frames=frames))
            return opened[-1]
        cap, state, messages = self.run_capture(opener, seconds=0.5)
        self.assertEqual(len(opened), 1)
        self.assertEqual(state[player_capture.S_FRAMES], 80)
        self.assertFalse([m for m in messages if m[0] == "problem"])
        self.assertIn(("fps", 60.0), messages)
        self.assertEqual(state[player_capture.S_FPS], 60.0)
        self.assertTrue(opened[0].closed)


    def test_effects_are_told_when_changed(self):
        """macOS のビデオエフェクトの一覧は、変わったときだけ知らせる。"""
        import queue
        cap = player_capture.CameraCapture(None, None, queue.Queue(), None)
        names = [["ポートレート"], ["ポートレート"], []]
        cap.effects = type("E", (), {"enabled": lambda self: names.pop(0)})()
        for _ in range(3):
            cap.effects_at = 0.0
            cap.check_effects()
        self.assertEqual([cap.messages.get_nowait() for _ in range(cap.messages.qsize())],
                         [("effects", ["ポートレート"]), ("effects", [])])

    def test_effects_can_be_read(self):
        """macOS ではエフェクトの状態を読める (ほかの OS では何もしない)。"""
        effects = player_capture.CameraEffects()
        self.assertEqual(effects.ok, sys.platform == "darwin")
        self.assertIsInstance(effects.enabled(), list)


class CameraInputTest(unittest.TestCase):
    """画面側 (CameraInput) と、別プロセスで読む側を通して、本物のコマが届く (カメラの代わりに動画ファイル)。"""

    def test_frames_arrive_through_the_process(self):
        import time
        from tests.videos import make_video, read_index_picture
        path = make_video(Fraction(30), 600, size=(320, 180))
        cam = player_live.LiveDevice("camera", path, "テスト用").open()
        try:
            got, deadline = [], time.perf_counter() + 20
            while len(got) < 5 and time.perf_counter() < deadline:
                pic = cam.latest()
                if pic is not None:
                    self.assertEqual((pic.fmt, pic.w, pic.h), ("yuv420p", 320, 180))
                    got.append(read_index_picture(pic))
                time.sleep(0.005)
            self.assertEqual(len(got), 5)
            self.assertEqual(got, sorted(got))           # 新しいコマほど後のコマ番号
            self.assertTrue(cam.signal)
            self.assertEqual(cam.error, "")
        finally:
            t = time.perf_counter()
            cam.close()
            self.assertLess(time.perf_counter() - t, 3)


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


class WhiteBalanceTest(unittest.TestCase):
    """自動WB: 色の平均を測り、平均が灰色になる倍率を求める。"""

    @staticmethod
    def frame(fmt, y, cb, cr, w=64, h=32):
        """全面が同じ色 (8bit の値) のコマを、frame_size の形の配列で作る。"""
        import numpy as np
        from player_core import SEMI, YUV_FORMATS, frame_size, plane_sizes
        _, _, bps, bits = YUV_FORMATS[fmt]
        shift = (bits - 8) + (16 - bits if fmt in SEMI and bps == 2 else 0)   # 10bit の交互の形式は上位に詰める
        sizes = plane_sizes(fmt, w, h)
        planes = [np.full(sizes[0][::-1], y << shift)]
        if fmt in SEMI:
            c = np.empty(sizes[1][::-1], int)
            c[:, 0::2], c[:, 1::2] = cb << shift, cr << shift
            planes.append(c)
        else:
            planes += [np.full(sizes[1][::-1], cb << shift), np.full(sizes[2][::-1], cr << shift)]
        out = np.concatenate([p.ravel() for p in planes]).astype(np.uint16 if bps == 2 else np.uint8)
        data = out.view(np.uint8)
        assert data.shape == frame_size(fmt, w, h)
        return data

    def test_samples_of_each_format(self):
        from player_core import yuv_samples
        for fmt in ("nv12", "yuv420p", "yuv422p10le", "p210le", "p010le"):
            samples = yuv_samples(fmt, 64, 32, self.frame(fmt, 100, 150, 110), count=32)
            self.assertEqual(samples.shape, (8 * 4, 3), fmt)
            for got, want in zip(samples.mean(0) * 255, (100, 150, 110)):
                self.assertAlmostEqual(got, want, delta=0.5, msg=fmt)

    @staticmethod
    def samples_of(rgb):
        """R, G, B (0〜1) の並びを、BT.709 の限定範囲の (Y, Cb, Cr) の並びにする。"""
        import numpy as np
        from player_view import yuv_matrix
        m, offset = yuv_matrix(True, False)
        return (rgb @ np.linalg.inv(m.reshape(3, 3)) + np.array(offset)).astype(np.float32)

    @staticmethod
    def corrected(samples, wb):
        import numpy as np
        from player_view import apply_white_balance, yuv_matrix
        m, offset = apply_white_balance(*yuv_matrix(True, False), wb)
        return (samples - np.array(offset)) @ m.reshape(3, 3)

    def test_gray_needs_no_correction(self):
        import numpy as np
        from player_view import white_balance
        gray = np.repeat(np.linspace(0.05, 0.9, 400)[:, None], 3, 1)
        gains, shifts = white_balance(self.samples_of(gray), True, False)
        np.testing.assert_allclose(gains, 1, atol=1e-3)
        np.testing.assert_allclose(shifts, 0, atol=1e-3)

    def test_tinted_blacks_and_whites_become_gray(self):
        """暗い部分ほど青く、R が沈んでいる (USB カメラで測ったずれ方に近い) 映像も、白から黒まで灰色にそろう。"""
        import numpy as np
        from player_view import white_balance
        g = np.linspace(0.15, 0.85, 400)
        tinted = np.stack([0.9 * g - 0.1, g, 0.85 * g + 0.15], -1)
        samples = self.samples_of(tinted)
        wb = white_balance(samples, True, False)
        self.assertGreater(wb[0][0], 1.0)            # R は上げ、B は黒を沈める
        self.assertLess(wb[1][2], 0.0)
        rgb = self.corrected(samples, wb)
        np.testing.assert_allclose(rgb[:, 0], rgb[:, 1], atol=1e-3)
        np.testing.assert_allclose(rgb[:, 2], rgb[:, 1], atol=1e-3)

    def test_flat_picture_is_left_alone(self):
        """明るさの幅が無い (信号なしの黒など) と求めない。"""
        import numpy as np
        from player_view import white_balance
        black = np.full((400, 3), 0.02)
        self.assertIsNone(white_balance(self.samples_of(black), True, False))
