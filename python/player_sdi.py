"""Blackmagic DeckLink / UltraStudio への SDI 出力。

画面に出している拡大中の範囲を、出力形式の解像度で描き直して送る (描くのは player_view.GpuWindow)。
機器とのやり取りは sdi/libzvp_decklink.dylib (build_sdi_mac.sh でビルド) を ctypes で呼ぶ。
今は macOS だけに対応している。

出力は 8bit YUV 4:2:2 (2vuy、UYVY の並び) か 10bit YUV 4:2:2 (v210)、色は BT.709 の映像範囲。

コマの間隔をそろえるため、機器の予約出力を使う (切り替えは機器が行うので、GPU からの読み出しや
送るのが多少遅れても出力の間隔は乱れない):
- 停止中や、動画と出力の fps が整数倍でない再生中は、機器の出力が次のコマへ進んでから最初の書き換えで
  1 回だけ描き、「機器の何コマ目のときに描いたか」を付けて送る。ライブラリがその 3 コマ後
  (1 コマが 8MB を超える 4K などは 4 コマ後) に出すよう予約する
- 再生中で、動画の進む速さと出力の fps が整数倍なら (sequence_for)、画面に新しいコマが出るたびに描き、
  前の予約のすぐ後ろへ順番に並べる (SEQUENCE)。出力の時計で拾い直さないので、画面と出力の時計のずれで
  コマが重なったり飛んだりしない
- 新しい絵が来ない間 (一時停止中など) は、ライブラリが最後の絵を予約し続ける

送るのは別スレッドで行い、GPU の読み出し用バッファを開いたまま受け取って、機器のコマへ直接写す。
"""
import ctypes
import os
import sys
import threading

import numpy as np

LIB_NAME = "libzvp_decklink.dylib"
SEQUENCE = -2         # 送るときの印: 前に予約した絵のすぐ後ろへ順番に並べる (動画と出力の fps が整数倍のとき)
MAX_PENDING = 4       # 送る前の絵をいくつまで貯めるか (超えたら古いものを捨てる)


class ModeInfo(ctypes.Structure):
    _fields_ = [
        ("mode", ctypes.c_uint32),
        ("name", ctypes.c_char * 64),
        ("width", ctypes.c_int32),
        ("height", ctypes.c_int32),
        ("frame_duration", ctypes.c_int64),
        ("time_scale", ctypes.c_int64),
        ("interlaced", ctypes.c_int32),
        ("ok8", ctypes.c_int32),
        ("ok10", ctypes.c_int32),
    ]


class SdiMode:
    """出力できる形式 1 つ (機器と組)。"""

    def __init__(self, device, device_name, info):
        self.device = device
        self.device_name = device_name
        self.mode = info.mode
        self.name = info.name.decode("utf-8", "replace")
        self.width, self.height = info.width, info.height
        self.fps = info.time_scale / info.frame_duration if info.frame_duration else 0.0
        self.interlaced = bool(info.interlaced)
        self.ten_bit = bool(info.ok10)      # 10bit で出せるなら 10bit にする

    def label(self):
        return f"{self.name} ({'10bit' if self.ten_bit else '8bit'})"


def _lib_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    paths = [os.path.join(here, "sdi", LIB_NAME)]
    base = getattr(sys, "_MEIPASS", None)          # PyInstaller でまとめたとき
    if base:
        paths.insert(0, os.path.join(base, "sdi", LIB_NAME))
    return paths


_lib = None
_lib_error = None


def load_library():
    """ライブラリを読み込む。無いときや読めないときは None (理由は library_error())。"""
    global _lib, _lib_error
    if _lib is not None or _lib_error is not None:
        return _lib
    if sys.platform != "darwin":
        _lib_error = "SDI 出力は今は macOS だけに対応しています"
        return None
    path = next((p for p in _lib_paths() if os.path.exists(p)), None)
    if path is None:
        _lib_error = "SDI 出力用のライブラリがありません (build_sdi_mac.sh でビルドしてください)"
        return None
    try:
        lib = ctypes.CDLL(path)
    except OSError as e:
        _lib_error = f"SDI 出力用のライブラリを読み込めません: {e}"
        return None
    lib.zvp_dl_refresh.restype = ctypes.c_int
    lib.zvp_dl_device_name.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.zvp_dl_modes.argtypes = [ctypes.c_int, ctypes.POINTER(ModeInfo), ctypes.c_int]
    lib.zvp_dl_open.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.zvp_dl_open.restype = ctypes.c_void_p
    lib.zvp_dl_row_bytes.argtypes = [ctypes.c_void_p]
    lib.zvp_dl_send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int64, ctypes.c_int]
    lib.zvp_dl_close.argtypes = [ctypes.c_void_p]
    lib.zvp_dl_clock.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64), ctypes.POINTER(ctypes.c_double)]
    lib.zvp_dl_buffered.argtypes = [ctypes.c_void_p]
    lib.zvp_dl_stats.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64), ctypes.POINTER(ctypes.c_int64)]
    _lib = lib
    return lib


def library_error():
    return _lib_error


def list_modes():
    """つながっている機器と、それぞれが出せる形式。(モードのリスト, 問題があればその説明)。"""
    lib = load_library()
    if lib is None:
        return [], library_error()
    count = lib.zvp_dl_refresh()
    if count < 0:
        return [], "Blackmagic Desktop Video (ドライバ) が入っていません"
    if count == 0:
        return [], ("Blackmagic の機器が見つかりません (つながっているか、"
                    "システム設定でドライバ拡張が許可されているかを確かめてください)")
    modes = []
    buf = (ModeInfo * 128)()
    for dev in range(count):
        name = ctypes.create_string_buffer(256)
        lib.zvp_dl_device_name(dev, name, 256)
        n = lib.zvp_dl_modes(dev, buf, 128)
        for i in range(max(n, 0)):
            modes.append(SdiMode(dev, name.value.decode("utf-8", "replace"), buf[i]))
    return modes, None


class _Picture:
    """送る前の絵 1 枚。address のメモリから写す。keep はそのメモリ (numpy 配列) を持っておくため。"""

    __slots__ = ("tag", "address", "keep", "done", "repeat")

    def __init__(self, tag, address, keep, done, repeat):
        self.tag, self.address, self.keep, self.done, self.repeat = tag, address, keep, done, repeat

    def finish(self):
        """写し終えたか、送らずに捨てた。メモリを貸した側に知らせる。"""
        if self.done is not None:
            self.done()


class SdiOutput:
    """1 つの出力。open で始め、submit / submit_pointer で描いた絵を渡し、close で止める。"""

    accepts_pointer = True        # submit_pointer で GPU のバッファから直接受け取れる

    def __init__(self, mode):
        self.mode = mode
        self.width, self.height = mode.width, mode.height
        self.ten_bit = mode.ten_bit
        self.period = 1.0 / mode.fps if mode.fps else 1 / 60
        self.sent = 0                 # 送ったコマ数 (統計用)
        self.dropped = 0              # 間に合わなかったなどで出さなかった絵の数
        self.error = None
        self.rendered_frame = None    # 画面側が最後に描いたときの、機器のコマ番号
        # 再生中で、動画と出力の fps が整数倍のとき (n, m): 画面に出た新しいコマ m 枚に 1 枚を、
        # 出力の n コマ分続けて順番に並べる。それ以外 (停止中など) は None で、機器のコマごとに描く
        self.sequence = None
        self._handle = None
        self._pending = []            # 送る前の絵 (_Picture)。古い順
        self._cond = threading.Condition()
        self._quit = False
        self._thread = None

    def open(self):
        lib = load_library()
        err = ctypes.create_string_buffer(256)
        handle = lib.zvp_dl_open(self.mode.device, self.mode.mode, int(self.ten_bit), err, 256)
        if not handle:
            raise RuntimeError(err.value.decode("utf-8", "replace") or "出力を始められませんでした")
        self._handle = handle
        self.row_bytes = lib.zvp_dl_row_bytes(handle)
        self.texels = self.row_bytes // 4         # 1 行を RGBA8 のテクセルで数えた幅
        self._thread = threading.Thread(target=self._run, name="sdi-output", daemon=True)
        self._thread.start()
        return self

    def submit(self, data, tag=None, repeat=1):
        """1 コマ分 (row_bytes * height バイトの uint8 配列) を渡す。待たずに戻る。

        tag は描いたときの機器のコマ番号 (rendered_frame) で、その 3 コマ後に出る。
        SEQUENCE なら前の絵の後ろへ repeat コマ分並べる。
        """
        data = np.ascontiguousarray(data)
        if data.nbytes >= self.row_bytes * self.height:
            self._push(_Picture(tag, data.ctypes.data, data, None, repeat))

    def submit_pointer(self, address, tag, done, repeat=1):
        """1 コマ分が置いてあるメモリ (GPU の読み出し用バッファを開いたもの) を渡す。待たずに戻る。

        送るスレッドがそこから機器のコマへ直接写す (画面側で写さずに済む)。写し終えたら、
        または送らずに捨てたら done() を呼ぶ (送るスレッドから呼ぶ)。それまでメモリを閉じないこと。
        """
        self._push(_Picture(tag, address, None, done, repeat))

    def _push(self, picture):
        with self._cond:
            if self._quit:
                return picture.finish()
            self._pending.append(picture)
            if len(self._pending) > MAX_PENDING:
                self._pending.pop(0).finish()
                self.dropped += 1
            self._cond.notify()

    def clock(self):
        """機器の出力のクロック: (いま出しているコマの番号, そのコマの中の位置 0〜1)。読めなければ None。"""
        if not self._handle:
            return None
        frame, phase = ctypes.c_int64(), ctypes.c_double()
        if _lib.zvp_dl_clock(self._handle, ctypes.byref(frame), ctypes.byref(phase)) != 0:
            return None
        return frame.value, phase.value

    def _run(self):
        lib = _lib
        while True:
            with self._cond:
                while not self._pending and not self._quit:
                    self._cond.wait()
                if self._quit:
                    for picture in self._pending:
                        picture.finish()
                    self._pending.clear()
                    return
                picture = self._pending.pop(0)
            # 機器のコマへ写して予約する。ctypes は呼ぶ間 GIL を手放す
            tag = -1 if picture.tag is None else picture.tag
            try:
                result = lib.zvp_dl_send(self._handle, picture.address, tag, picture.repeat)
            finally:
                picture.finish()
            if result == 0:
                self.sent += 1
            elif result > 0:
                self.dropped += 1
            else:
                self.error = f"送れませんでした ({result})"

    def repeats(self):
        """(最後の絵を繰り返して出したコマ数, 並べ直した回数)。再生中に増えるのは、新しい絵が
        間に合わず同じ絵が続いたとき。"""
        holds, resyncs = ctypes.c_int64(), ctypes.c_int64()
        if not self._handle or _lib.zvp_dl_stats(self._handle, ctypes.byref(holds), ctypes.byref(resyncs)):
            return 0, 0
        return holds.value, resyncs.value

    def buffered(self):
        """機器に予約されていて、まだ出していないコマの数 (確認用)。"""
        return _lib.zvp_dl_buffered(self._handle) if self._handle else 0

    def close(self):
        if self._thread:
            with self._cond:
                self._quit = True
                self._cond.notify()
            self._thread.join(timeout=2)
            self._thread = None
        if self._handle:
            _lib.zvp_dl_close(self._handle)
            self._handle = None

    # 停止中などに、機器のコマごとに 1 回だけ描くための判断 (それより多く描いても出せない)。
    # 機器のクロックが読めないとき (出力が動いていない) は毎回描いてよいことにする
    def due(self):
        """機器の出力が、前に描いたときより先のコマへ進んだか。"""
        clock = self.clock()
        return clock is None or clock[0] != self.rendered_frame

    def wait_time(self):
        """描いてよくなる (機器が次のコマへ進む) まであと何秒か。"""
        clock = self.clock()
        if clock is None or clock[0] != self.rendered_frame:
            return 0.0
        return (1.0 - clock[1]) * self.period + 0.0005

    def rendered(self):
        """描いた。いまの機器のコマ番号を覚え、送るときの印 (tag) にする。"""
        clock = self.clock()
        self.rendered_frame = clock[0] if clock is not None else None


def sequence_for(out_fps, video_rate, tolerance=0.005):
    """動画の進む速さ (動画 fps × 速度、コマ/秒) と出力の fps が整数倍なら (n, m) を返す。

    出力の n コマに動画の 1 コマ (n = 出力 / 動画)、または動画の m コマに出力の 1 コマ (m = 動画 / 出力)。
    59.94 と 60 のように 0.5% までの違いは同じとみなす (差は予約の調整で吸収する)。整数倍でなければ None。
    """
    if out_fps <= 0 or video_rate <= 0:
        return None
    for ratio, make in ((out_fps / video_rate, lambda k: (k, 1)), (video_rate / out_fps, lambda k: (1, k))):
        k = round(ratio)
        if 1 <= k <= 8 and abs(ratio - k) / ratio < tolerance:
            return make(k)
    return None


def view_transform(screen_w, screen_h, s, tx, ty, img_w, img_h, out_w, out_h):
    """画面の拡大の状態 (画面座標 = 画像座標 * s + (tx, ty)) を、出力の大きさに置き換える。

    倍率 (全体表示に対する倍率) と、画面の真ん中に見えている画像の点を合わせる。出力の縦横比が
    画面と違えば見える範囲は少し変わる。戻り値は出力での (s, tx, ty)。
    """
    fit_screen = min(screen_w / img_w, screen_h / img_h)
    fit_out = min(out_w / img_w, out_h / img_h)
    so = fit_out * (s / fit_screen)
    cx, cy = (screen_w / 2 - tx) / s, (screen_h / 2 - ty) / s
    ox, oy = out_w / 2 - cx * so, out_h / 2 - cy * so
    cw, ch = img_w * so, img_h * so          # 画面と同じく、はみ出さないように寄せる
    ox = (out_w - cw) / 2 if cw <= out_w else min(0.0, max(out_w - cw, ox))
    oy = (out_h - ch) / 2 if ch <= out_h else min(0.0, max(out_h - ch, oy))
    return so, ox, oy


# ---------- シェーダーと同じ詰め方の、確認用の計算 (numpy) ----------
def rgb_to_codes(rgb, ten_bit):
    """0〜1 の RGB (…, 3) → BT.709 映像範囲の Y, Cb, Cr のコード値 (float)。"""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    cb, cr = (b - y) / 1.8556, (r - y) / 1.5748
    unit = 4 if ten_bit else 1
    return 16 * unit + 219 * unit * y, 128 * unit + 224 * unit * cb, 128 * unit + 224 * unit * cr


def pack_reference(rgb, ten_bit, row_bytes):
    """RGB の画像 (高さ, 幅, 3) を 2vuy / v210 に詰める。シェーダーの結果と比べるために使う。"""
    h, w, _ = rgb.shape
    y, cb, cr = rgb_to_codes(rgb.astype(np.float64), ten_bit)
    hi = 1019 if ten_bit else 254
    lo = 4 if ten_bit else 1
    code = lambda v: np.clip(np.floor(v + 0.5), lo, hi).astype(np.int64)   # noqa: E731
    pw = (w + 1) // 2 * 2 if not ten_bit else (w + 5) // 6 * 6
    pad = lambda a: np.pad(a, ((0, 0), (0, pw - w)))                       # noqa: E731
    y, cb, cr = pad(y), pad(cb), pad(cr)
    # 色差は隣り合う 2 画素の平均
    cb2 = (cb[:, 0::2] + cb[:, 1::2]) / 2
    cr2 = (cr[:, 0::2] + cr[:, 1::2]) / 2
    Y, CB, CR = code(y), code(cb2), code(cr2)     # はみ出した所 (RGB を 0 で埋めた所) は黒になる
    out = np.zeros((h, row_bytes), np.uint8)
    if not ten_bit:
        packed = np.stack([CB, Y[:, 0::2], CR, Y[:, 1::2]], axis=-1).reshape(h, -1)
        out[:, :packed.shape[1]] = packed
        return out
    g = pw // 6
    Yg, CBg, CRg = Y.reshape(h, g, 6), CB.reshape(h, g, 3), CR.reshape(h, g, 3)
    words = np.stack([
        CBg[..., 0] | Yg[..., 0] << 10 | CRg[..., 0] << 20,
        Yg[..., 1] | CBg[..., 1] << 10 | Yg[..., 2] << 20,
        CRg[..., 1] | Yg[..., 3] << 10 | CBg[..., 2] << 20,
        Yg[..., 4] | CRg[..., 2] << 10 | Yg[..., 5] << 20,
    ], axis=-1).reshape(h, -1).astype("<u4")
    raw = words.view(np.uint8)
    out[:, :raw.shape[1]] = raw
    return out
