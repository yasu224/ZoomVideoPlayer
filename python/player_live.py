"""映像入力 (カメラ、Blackmagic の SDI 入力) と、その録画。

入力は画面側へ「最新のコマ」を渡すだけの作り。コマは入力ごとに何枚かのバッファを順に使い、
画面が表示中の 1 枚 (held) と最新の 1 枚 (latest) には書かない。画面側は書き換えのたびに latest() を呼び、
新しいコマがあれば表示する (古いコマは自然に手放される)。
- SDI 入力: DeckLink のライブラリ (sdi/) が受け取り、P210 (10bit 4:2:2) にほどいてバッファへ書く
- カメラ: FFmpeg (macOS は AVFoundation、Windows は DirectShow) から NV12 で受け取る (裏のスレッド)
どちらも画面 (GPU) がそのまま読める形式で、録画 (HEVC) にもそのまま渡せる。
"""
import ctypes
import queue
import sys
import threading
import time
from fractions import Fraction

import numpy as np

import player_sdi
from player_core import even_size, fill_frame, frame_size, gpu_format, write_frame
from player_frames import Picture

BUFFERS = 4           # 入力ごとのバッファの数 (表示中・最新・書き込み中 + 予備)


class LiveDevice:
    """選べる入力 1 つ。kind は "sdi" か "camera"。"""

    def __init__(self, kind, index, name):
        self.kind, self.index, self.name = kind, index, name

    def label(self):
        return f"{'SDI 入力' if self.kind == 'sdi' else 'カメラ'}: {self.name}"

    def same(self, other):
        return other is not None and (self.kind, self.index, self.name) == (other.kind, other.index, other.name)

    def open(self):
        return (SdiInput if self.kind == "sdi" else CameraInput)(self).open()


def list_inputs():
    """つながっている入力。SDI 入力 (DeckLink) とカメラ。(入力のリスト, 問題があればその説明)。"""
    devices, notes = [], []
    sdi_names = set()
    lib = _sdi_lib()
    if lib is not None:
        count = lib.zvp_in_refresh()
        for dev in range(max(count, 0)):
            name = ctypes.create_string_buffer(256)
            lib.zvp_in_device_name(dev, name, 256)
            text = name.value.decode("utf-8", "replace")
            sdi_names.add(text)
            devices.append(LiveDevice("sdi", dev, text))
    for index, name in camera_names():
        # Blackmagic の機器はカメラとしても見えるが、SDI 入力として扱う (10bit のまま受け取れる)
        if any(n in name for n in sdi_names) or name.startswith("Capture screen"):
            continue
        devices.append(LiveDevice("camera", index, name))
    if not devices:
        notes.append("入力が見つかりません")
    return devices, "".join(notes)


# ---------- SDI 入力 (DeckLink) ----------
_lib_ready = False


def _sdi_lib():
    global _lib_ready
    lib = player_sdi.load_library()
    if lib is None or _lib_ready:
        return lib
    lib.zvp_in_refresh.restype = ctypes.c_int
    lib.zvp_in_device_name.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.zvp_in_open.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.zvp_in_open.restype = ctypes.c_void_p
    i32p, i64p = ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int64)
    lib.zvp_in_format.argtypes = [ctypes.c_void_p, i32p, i32p, i64p, i64p, ctypes.c_char_p, ctypes.c_int,
                                  i64p, i32p, ctypes.c_char_p, ctypes.c_int]
    lib.zvp_in_set_buffers.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
                                       ctypes.c_int32, ctypes.c_int32]
    lib.zvp_in_latest.argtypes = [ctypes.c_void_p, ctypes.c_int64, i64p, ctypes.POINTER(ctypes.c_double)]
    lib.zvp_in_release.argtypes = [ctypes.c_void_p]
    lib.zvp_in_stats.argtypes = [ctypes.c_void_p, i64p, i64p]
    lib.zvp_in_close.argtypes = [ctypes.c_void_p]
    _lib_ready = True
    return lib


class SdiInput:
    """Blackmagic の SDI 入力。入力の形式は機器が見分け、変わったらバッファを用意し直す。"""

    fmt = "p210le"

    def __init__(self, device):
        self.device = device
        self.name = device.name
        self.handle = None
        self.bufs = []
        self.size = (0, 0)
        self.fps = 0.0
        self.mode_name = ""
        self.signal = False
        self.error = ""
        self.serial = None
        self.last_no = 0
        # ライブラリとの受け渡しに使う入れ物。書き換えのたびに (1 秒 60〜120 回) 読むので作り置く
        self._w, self._h, self._serial, self._signal = (ctypes.c_int32(), ctypes.c_int32(), ctypes.c_int64(),
                                                        ctypes.c_int32())
        self._dur, self._scale, self._no, self._t = ctypes.c_int64(), ctypes.c_int64(), ctypes.c_int64(), ctypes.c_double()
        self._name, self._err = ctypes.create_string_buffer(64), ctypes.create_string_buffer(128)

    def open(self):
        lib = _sdi_lib()
        err = ctypes.create_string_buffer(256)
        handle = lib.zvp_in_open(self.device.index, err, 256)
        if not handle:
            raise RuntimeError(err.value.decode("utf-8", "replace") or "入力を始められませんでした")
        self.handle = handle
        self._check_format()
        return self

    def _check_format(self):
        """入力の形式を読み、変わっていればバッファを用意し直す。"""
        lib = _sdi_lib()
        w, h, dur, scale, serial, signal = self._w, self._h, self._dur, self._scale, self._serial, self._signal
        name, err = self._name, self._err
        lib.zvp_in_format(self.handle, ctypes.byref(w), ctypes.byref(h), ctypes.byref(dur), ctypes.byref(scale),
                          name, 64, ctypes.byref(serial), ctypes.byref(signal), err, 128)
        self.signal = bool(signal.value)
        self.error = err.value.decode("utf-8", "replace") if err.value else ""
        if serial.value == self.serial and (w.value, h.value) == self.size:
            return
        self.serial = serial.value
        self.mode_name = name.value.decode("utf-8", "replace")
        self.fps = scale.value / dur.value if dur.value else 0.0
        self.size = (w.value, h.value)
        if w.value <= 0 or h.value <= 0:
            return
        bufs = [np.zeros(frame_size(self.fmt, w.value, h.value), np.uint8) for _ in range(BUFFERS)]
        ptrs = (ctypes.c_void_p * BUFFERS)(*[b.ctypes.data for b in bufs])
        lib.zvp_in_set_buffers(self.handle, ptrs, BUFFERS, w.value, h.value)
        self.bufs = bufs              # 前のバッファは、ライブラリが使わなくなってから手放す

    def latest(self):
        """前に返したものより新しいコマがあれば Picture、無ければ None。"""
        self._check_format()
        no, t = self._no, self._t
        idx = _sdi_lib().zvp_in_latest(self.handle, self.last_no, ctypes.byref(no), ctypes.byref(t))
        if idx < 0 or idx >= len(self.bufs):
            return None
        self.last_no = no.value
        w, h = self.size
        return Picture(t.value, self.fmt, w, h, self.bufs[idx])

    def stats(self):
        frames, dropped = ctypes.c_int64(), ctypes.c_int64()
        _sdi_lib().zvp_in_stats(self.handle, ctypes.byref(frames), ctypes.byref(dropped))
        return frames.value, dropped.value

    def status(self):
        """統計の表示に出す、入力の様子。"""
        if self.error:
            return self.error
        if not self.signal:
            return "信号なし"
        return f"{self.mode_name}"

    def frame_rate(self):
        """録画に使う fps (分数)。"""
        return Fraction(self.fps).limit_denominator(1001) if self.fps else Fraction(60)

    def close(self):
        if self.handle:
            _sdi_lib().zvp_in_close(self.handle)
            self.handle = None


# ---------- カメラ (FFmpeg) ----------
def _camera_format():
    return {"darwin": "avfoundation", "win32": "dshow"}.get(sys.platform)


def _ffmpeg_log(url, fmt, options):
    """FFmpeg に開かせて、出たログの文字を返す (機器の一覧や、使える形式の一覧を読むため)。"""
    import av
    import av.logging
    level = av.logging.get_level()
    av.logging.set_level(av.logging.INFO)
    lines = []
    try:
        with av.logging.Capture(True) as captured:
            try:
                av.open(url, format=fmt, options=options, timeout=5).close()
            except Exception:
                pass
        lines = [msg.rstrip() for _, _, msg in captured]
    finally:
        av.logging.set_level(level)
    return lines


def camera_names():
    """カメラの (番号, 名前) の一覧。"""
    fmt = _camera_format()
    if fmt is None:
        return []
    import av
    if fmt not in av.formats_available:
        return []
    url = "" if fmt == "avfoundation" else "dummy"
    lines = _ffmpeg_log(url, fmt, {"list_devices": "true"})
    names, video = [], False
    for line in lines:
        if "video devices" in line.lower():
            video = True
            continue
        if "audio devices" in line.lower():
            video = False
            continue
        if not video:
            continue
        if fmt == "avfoundation" and line.startswith("[") and "]" in line:
            names.append(line.split("]", 1)[1].strip())
        elif fmt == "dshow" and line.startswith('"') and line.count('"') >= 2:
            names.append(line.split('"')[1])
    return list(enumerate(names))


def camera_modes(device):
    """カメラの使える (幅, 高さ, fps) の一覧 (macOS)。わざと無い fps を指定して、一覧のログを出させる。"""
    if _camera_format() != "avfoundation":
        return []
    modes = []
    for line in _ffmpeg_log(f"{device.index}:none", "avfoundation", {"framerate": "1234"}):
        if "@[" not in line:
            continue
        try:
            size, rate = line.strip().split("@[")
            w, h = size.split("x")
            fps = float(rate.split()[0])
            modes.append((int(w), int(h), fps))
        except ValueError:
            pass
    return sorted(set(modes))


def best_mode(modes):
    """1080p の 60fps 前後を優先し、無ければいちばん大きく、fps の高いもの。"""
    if not modes:
        return None
    full_hd = [m for m in modes if (m[0], m[1]) == (1920, 1080) and m[2] <= 61]
    pool = full_hd or [m for m in modes if m[2] <= 61] or modes
    return max(pool, key=lambda m: (m[0] * m[1], m[2]))


class CameraInput:
    """カメラ。裏のスレッドで FFmpeg から読み、NV12 (使えなければ GPU 用の形式) でバッファへ書く。"""

    def __init__(self, device):
        self.device = device
        self.name = device.name
        self.fmt = "nv12"
        self.size = (0, 0)
        self.fps = 0.0
        self.mode_name = ""
        self.error = ""
        self.frames = 0
        self.dropped = 0
        self.last_frame_at = 0.0
        self.last_no = 0
        self._lock = threading.Lock()
        self._bufs = []
        self._held = self._latest = -1
        self._latest_no = 0
        self._latest_t = 0.0
        self._stop = threading.Event()
        self._thread = None

    def open(self):
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()
        return self

    def _open_container(self):
        import av
        fmt = _camera_format()
        options = {"pixel_format": "nv12"} if fmt == "avfoundation" else {}
        mode = best_mode(camera_modes(self.device))
        if mode:
            options["video_size"] = f"{mode[0]}x{mode[1]}"
            options["framerate"] = f"{mode[2]:.6f}".rstrip("0").rstrip(".")
        url = f"{self.device.index}:none" if fmt == "avfoundation" else f"video={self.name}"
        # 読む方の待ちを短くして、止めるときにすぐ気づけるようにする
        return av.open(url, format=fmt, options=options, timeout=(10.0, 0.5))

    def _run(self):
        import av
        try:
            container = self._open_container()
        except Exception as e:
            self.error = f"カメラを開けません: {e}"
            return
        stream = container.streams.video[0]
        rate = stream.average_rate or stream.guessed_rate
        self.fps = float(rate) if rate else 0.0
        try:
            while not self._stop.is_set():
                try:
                    for packet in container.demux(stream):
                        if self._stop.is_set():
                            break
                        for frame in packet.decode():
                            self._store(frame)
                except Exception as e:
                    # 読む待ちの時間切れ (映像が届いていない) は、止める合図を確かめて読み直す
                    if self._stop.is_set():
                        break
                    text = str(e).lower()
                    if isinstance(e, (av.error.ExitError, TimeoutError)) or "timed out" in text or "exit" in text:
                        continue
                    self.error = f"カメラから読めません: {e}"
                    break
        finally:
            container.close()

    def _store(self, frame):
        fmt = "nv12" if frame.format.name == "nv12" else gpu_format(frame.format.name)
        w, h = even_size(fmt, frame.width, frame.height)
        with self._lock:
            if (fmt, (w, h)) != (self.fmt, self.size) or not self._bufs:
                self.fmt, self.size = fmt, (w, h)
                self.mode_name = f"{w}×{h} {self.fps:.2f}fps" if self.fps else f"{w}×{h}"
                self._bufs = [np.zeros(frame_size(fmt, w, h), np.uint8) for _ in range(BUFFERS)]
                self._held = self._latest = -1
            idx = next((i for i in range(len(self._bufs)) if i not in (self._held, self._latest)), -1)
            bufs = self._bufs
        self.frames += 1
        self.last_frame_at = time.perf_counter()
        if idx < 0:
            self.dropped += 1
            return
        write_frame(bufs[idx], frame, fmt, w, h)
        with self._lock:
            if bufs is self._bufs:
                self._latest = idx
                self._latest_no = self.frames
                self._latest_t = float(frame.time or 0.0)

    def latest(self):
        with self._lock:
            if self._latest < 0 or self._latest_no <= self.last_no:
                return None
            self._held = self._latest
            self.last_no = self._latest_no
            w, h = self.size
            return Picture(self._latest_t + self.last_no * 1e-9, self.fmt, w, h, self._bufs[self._held])

    def stats(self):
        return self.frames, self.dropped

    @property
    def signal(self):
        return time.perf_counter() - self.last_frame_at < 1.0

    def status(self):
        if self.error:
            return self.error
        if not self.signal:
            return "信号なし (映像が届いていません)"
        return self.mode_name

    def frame_rate(self):
        return Fraction(self.fps).limit_denominator(1001) if self.fps else Fraction(30)

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None


# ---------- 録画 ----------
# (入力の形式, 書き出し方式, 書き出し方式に渡す形式) の候補。上から使えるものを使う
ENCODERS = {
    "p210le": [("hevc_videotoolbox", "p210le"), ("libx265", "yuv422p10le"), ("libx264", "yuv422p10le")],
    "nv12": [("hevc_videotoolbox", "nv12"), ("h264_videotoolbox", "nv12"), ("libx264", "nv12")],
}


def pick_encoder(fmt):
    import av
    for name, pix in ENCODERS.get(fmt, ENCODERS["nv12"]):
        if name in av.codecs_available:
            return name, pix
    return None


class Recorder:
    """入力のコマを .mov に書き出す。書き出しは裏のスレッドで行い、追いつかないコマは捨てて数える。

    10bit 4:2:2 の SDI 入力は HEVC の 10bit 4:2:2 (Apple シリコンではハードウェアで書き出す) のまま残す。
    音声は録らない。
    """

    QUEUE_SECONDS = 2.0

    def __init__(self, path, fmt, width, height, rate):
        import av
        choice = pick_encoder(fmt)
        if choice is None:
            raise RuntimeError("使える書き出し方式がありません")
        self.codec, self.pix = choice
        self.path, self.fmt, self.width, self.height, self.rate = path, fmt, width, height, rate
        self.container = av.open(path, "w")
        stream = self.container.add_stream(self.codec, rate=rate)
        stream.width, stream.height, stream.pix_fmt = width, height, self.pix
        stream.time_base = 1 / rate
        # 1080p 60fps で 60 Mbps ほど (画素数と fps に比べる)。ソフトの書き出しは速さを優先する
        stream.bit_rate = int(60_000_000 * width * height * float(rate) / (1920 * 1080 * 60))
        if self.codec == "hevc_videotoolbox" and self.pix == "p210le":
            stream.options = {"profile": "main42210"}
        elif self.codec.startswith("lib"):
            stream.options = {"preset": "ultrafast"}
        self.stream = stream
        self.written = 0
        self.dropped = 0
        self.error = None
        self.started_at = time.perf_counter()
        self._queue = queue.Queue(maxsize=max(4, int(float(rate) * self.QUEUE_SECONDS)))
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def push(self, picture):
        """表示した入力のコマを渡す (写しを取って、すぐ戻る)。"""
        if (picture.fmt, picture.w, picture.h) != (self.fmt, self.width, self.height):
            self.dropped += 1
            return
        try:
            self._queue.put_nowait(picture.data.copy())
        except queue.Full:
            self.dropped += 1

    def _run(self):
        import av
        try:
            while True:
                data = self._queue.get()
                if data is None:
                    break
                frame = av.VideoFrame(self.width, self.height, self.fmt)
                fill_frame(frame, data, self.fmt, self.width, self.height)
                if self.pix != self.fmt:
                    frame = frame.reformat(format=self.pix)
                frame.pts = self.written
                for packet in self.stream.encode(frame):
                    self.container.mux(packet)
                self.written += 1
            for packet in self.stream.encode():
                self.container.mux(packet)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.container.close()

    def elapsed(self):
        return time.perf_counter() - self.started_at

    def stop(self):
        """残りを書き出して閉じる。"""
        self._queue.put(None)
        self._thread.join(timeout=30)
