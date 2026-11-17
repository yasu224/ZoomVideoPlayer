"""映像入力 (カメラ、Blackmagic の SDI 入力) と、その録画。

入力は画面側へ「最新のコマ」を渡すだけの作り。コマは入力ごとに何枚かのバッファを順に使い、
画面が表示中の 1 枚 (held) と最新の 1 枚 (latest) には書かない。画面側は書き換えのたびに latest() を呼び、
新しいコマがあれば表示する (古いコマは自然に手放される)。
- SDI 入力: DeckLink のライブラリ (sdi/) が受け取り、P210 (10bit 4:2:2) にほどいてバッファへ書く
- カメラ: FFmpeg (macOS は AVFoundation、Windows は DirectShow) から NV12 で受け取る (別プロセス: player_capture)
どちらも画面 (GPU) がそのまま読める形式で、録画 (HEVC) にもそのまま渡せる。
"""
import ctypes
import queue
import threading
import time
from fractions import Fraction

import numpy as np

import player_capture as capture
import player_sdi
from player_capture import camera_names
from player_core import attach_shared_memory, fill_frame, frame_size, unlink_shared_memory
from player_frames import Picture

BUFFERS = capture.BUFFERS    # 入力ごとのバッファの数 (表示中・最新・書き込み中 + 予備)


class LiveDevice:
    """選べる入力 1 つ。kind は "sdi" か "camera"。"""

    def __init__(self, kind, index, name):
        self.kind, self.index, self.name = kind, index, name
        self.modes = None        # カメラの使える形式の一覧 (初めて開くときに読む)
        self.mode = None         # 使う人が選んだ形式 (None なら自動で選ぶ)

    def load_modes(self):
        """カメラの使える形式の一覧を読む (一度読んだら覚えておく)。"""
        if self.modes is None:
            self.modes = capture.camera_modes(self.index, self.name) if self.kind == "camera" else []
        return self.modes

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


# ---------- カメラ (FFmpeg、別プロセスで読む: player_capture) ----------
class CameraInput:
    """カメラ。別プロセス (player_capture.camera_worker) が読んで共有メモリのバッファへ書いたコマを受け取る。

    macOS では映像が届いていないとカメラの読み取りが止められないので、閉じるときに止まらなければ
    プロセスごと終わらせる。
    """

    def __init__(self, device):
        self.device = device
        self.name = device.name
        self.fmt = "nv12"
        self.size = (0, 0)
        self.fps = 0.0
        self.mode_name = ""
        self.error = ""
        self.problem = ""             # 信号なしのときに FFmpeg が返した失敗 (確かめるとき用)
        self.effects = []             # 入っている macOS のビデオエフェクト (映像が加工され、CPU も使う)
        self.last_no = 0
        self._serial = 0
        self._shm_names = []          # 知らされた共有メモリの名前 (プロセスごと終わらせたときに片付ける)
        self._shm = None
        self._views = []
        self._proc = None

    def open(self):
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        self._state = ctx.Array("d", [-1, 0, -1, 0, 0, 0, 0, 0, 0][:capture.STATE_SIZE])
        self._messages = ctx.Queue()
        self._stop = ctx.Event()
        self._proc = ctx.Process(target=capture.camera_worker, daemon=True,
                                 args=(self.device.index, self.name, self._state, self._messages, self._stop,
                                       self.device.mode))
        self._proc.start()
        return self

    def _poll(self):
        """子プロセスからの知らせ (形式が変わった、エラー) を受け取る。"""
        latest_format = None
        while True:
            try:
                msg = self._messages.get_nowait()
            except queue.Empty:
                break
            if msg[0] == "format":
                latest_format = msg
                self._shm_names.append(msg[2])
            elif msg[0] == "error":
                self.error = msg[1]
            elif msg[0] == "problem":
                self.problem = msg[1]
            elif msg[0] == "effects":
                self.effects = msg[1]
            elif msg[0] == "fps":                # 最初のコマの時刻から fps が分かった
                self.fps = msg[1]
                self._name_mode()
        if latest_format is None:
            return
        _, serial, shm_name, fmt, w, h, fps = latest_format
        try:
            shm = attach_shared_memory(shm_name)
        except FileNotFoundError:        # すでに次の形式に変わって片付けられた
            return
        nbytes = int(np.prod(frame_size(fmt, w, h)))
        self._views = [np.ndarray((nbytes,), np.uint8, buffer=shm.buf, offset=i * nbytes)
                       for i in range(capture.BUFFERS)]
        self._close_shm()
        self._shm = shm
        self._serial = serial
        self.fmt, self.size = fmt, (w, h)
        self.fps = fps or self._state[capture.S_FPS]     # fps をコマの時刻から求めた後の形式の知らせもある
        self._name_mode()

    def _name_mode(self):
        w, h = self.size
        self.mode_name = f"{w}×{h} {self.fps:.2f}fps" if self.fps else f"{w}×{h}"

    def _close_shm(self):
        if self._shm is not None:
            try:
                self._shm.close()
            except BufferError:          # まだ表示中のコマが指している。捨てられたときに閉じられる
                pass
            self._shm = None

    def latest(self):
        self._poll()
        st = self._state
        with st.get_lock():
            if int(st[capture.S_SERIAL]) != self._serial or not self._views:
                return None
            idx, no = int(st[capture.S_LATEST]), int(st[capture.S_LATEST_NO])
            if idx < 0 or no <= self.last_no:
                return None
            st[capture.S_HELD] = idx
            t = st[capture.S_LATEST_T]
        self.last_no = no
        w, h = self.size
        return Picture(t + no * 1e-9, self.fmt, w, h, self._views[idx])

    def stats(self):
        return int(self._state[capture.S_FRAMES]), int(self._state[capture.S_DROPPED])

    @property
    def signal(self):
        return time.time() - self._state[capture.S_LAST_AT] < 1.0

    def status(self):
        self._poll()
        if self.error:
            return self.error
        if not self.signal:
            return "信号なし (映像が届いていません)"
        return self.mode_name

    def frame_rate(self):
        return Fraction(self.fps).limit_denominator(1001) if self.fps else Fraction(30)

    def close(self):
        """読むのをやめる。止まらなければ (映像が届かずに待ち続けているなど) プロセスごと終わらせる。"""
        if self._proc is None:
            return
        self._stop.set()
        self._proc.join(timeout=0.5)        # 映像が届いていれば、1 コマのうちに止まる
        if self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(timeout=2)
            self._unlink_left()
        self._proc = None
        self._views = []
        self._close_shm()

    def _unlink_left(self):
        """プロセスごと終わらせたときは、子プロセスが片付けられなかった共有メモリを消す。"""
        self._poll()
        for name in self._shm_names:
            try:
                shm = attach_shared_memory(name)
            except FileNotFoundError:
                continue
            unlink_shared_memory(shm)
            try:
                shm.close()
            except BufferError:
                pass


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
