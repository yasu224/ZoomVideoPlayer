"""カメラから読む側 (別プロセスで動く。PyAV と numpy (macOS は AVFoundation を読むライブラリも) を使い、
Qt は読み込まない)。

macOS のカメラ (AVFoundation) では、映像が届いていないと FFmpeg はコマが来るまで待ち続け、時間切れの
指定も効かない。スレッドで読むと止められず、カメラをつかんだまま残ってしまうので、別プロセスで読む。
止まらなければ画面側 (player_live.CameraInput) がプロセスごと終わらせる。

コマは共有メモリのバッファ (BUFFERS 枚) に書き、どのバッファが最新かを共有の数値 (state) で知らせる。
画面が表示中のバッファ (held) と最新のバッファには書かない。形式が変わったら新しい共有メモリを作り、
その名前を queue で知らせる。
"""
import re
import sys
import time

from player_os import IS_MAC, IS_WIN, ObjC
from player_core import attach_shared_memory, even_size, frame_size, gpu_format, unlink_shared_memory, write_frame

BUFFERS = 4           # バッファの数 (表示中・最新・書き込み中 + 予備)
WAIT_SECONDS = 0.002  # コマがまだ届いていないときに待つ時間
EFFECTS_SECONDS = 2.0  # macOS のビデオエフェクトの状態を読み直す間隔
FPS_SKIP, FPS_FRAMES = 10, 60   # fps をコマの時刻から求めるとき: 最初の 10 コマを除き、60 コマ目まで

# 共有の数値 (state) の並び
S_LATEST, S_LATEST_NO, S_HELD, S_FRAMES, S_DROPPED, S_LAST_AT, S_LATEST_T, S_FPS, S_SERIAL = range(9)
STATE_SIZE = 9


def camera_format():
    """FFmpeg でカメラを読む形式 (macOS は AVFoundation、Windows は DirectShow)。無ければ None。"""
    return "avfoundation" if IS_MAC else "dshow" if IS_WIN else None


def ffmpeg_log(url, fmt, options):
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
    fmt = camera_format()
    if fmt is None:
        return []
    import av
    if fmt not in av.formats_available:
        return []
    url = "" if fmt == "avfoundation" else "dummy"
    return list(enumerate(parse_device_names(ffmpeg_log(url, fmt, {"list_devices": "true"}), fmt)))


def parse_device_names(lines, fmt):
    """FFmpeg の機器一覧のログから、映像の機器の名前を順に返す。

    DirectShow の書き方は FFmpeg の版で 2 通りある。古い版は「DirectShow video devices」の見出しの下に
    名前を並べ、新しい版 (5.0 以降) は見出しなしで 1 台ずつ「"名前" (video)」と書く。
    ログは途中で切れて届くことがあり、「(video)」が名前の次の行に来る。
    """
    names, section, pending = [], None, None
    for line in lines:
        text = line.strip()
        lower = text.lower()
        if "video devices" in lower:
            section, pending = "video", None
            continue
        if "audio devices" in lower:
            section, pending = "audio", None
            continue
        if fmt == "avfoundation":
            if section == "video" and line.startswith("[") and "]" in line:
                names.append(line.split("]", 1)[1].strip())
            continue
        if fmt != "dshow":
            continue
        if line.startswith('"') and line.count('"') >= 2:
            name, kind = line.split('"')[1], line.split('"', 2)[2].strip()
            pending = None
            if kind.startswith("(video"):
                names.append(name)
            elif kind.startswith("("):
                pass  # (audio) や (none)
            elif section == "video":
                names.append(name)
            elif section is None:
                pending = name  # 種類は次の行に来る
        elif pending is not None and text:
            if text.startswith("(video"):
                names.append(pending)
            pending = None
    return names


# これより低い fps の形式は出さない (動きがカクついて見えるので使わない。USB カメラは 10 / 15 / 20 / 23fps も
# 知らせてくるが、残すのは 25 / 30 / 60fps など。キャプチャー機器の 50 / 120 / 240fps などは残る)
MIN_FPS = 24.5


def camera_modes(index, name=None):
    """カメラの使える (幅, 高さ, fps) の一覧 (MIN_FPS より低い fps は除く)。Windows は (幅, 高さ, fps, 形式の指定)。"""
    return [m for m in all_camera_modes(index, name) if m[2] >= MIN_FPS]


def all_camera_modes(index, name=None):
    """カメラが知らせてくる形式すべて。

    macOS はわざと無い fps を指定して一覧のログを出させる。Windows は DirectShow に一覧を出させる。
    """
    fmt = camera_format()
    if fmt == "dshow":
        return parse_dshow_modes(ffmpeg_log(f"video={name}", "dshow", {"list_options": "true"}))
    if fmt != "avfoundation":
        return []
    modes = []
    for line in ffmpeg_log(f"{index}:none", "avfoundation", {"framerate": "1234"}):
        if "@[" not in line:
            continue
        try:
            size, rate = line.strip().split("@[")
            w, h = size.split("x")
            modes.append((int(w), int(h), float(rate.split()[0])))
        except ValueError:
            pass
    return sorted(set(modes))


DSHOW_MODE = re.compile(r"(vcodec|pixel_format)=(\S+)\s+min s=\d+x\d+ fps=[\d.]+\s+max s=(\d+)x(\d+) fps=([\d.]+)")


def parse_dshow_modes(lines):
    """DirectShow の list_options の行 (「pixel_format=yuyv422  min s=… fps=… max s=1920x1080 fps=60」など)
    から、(幅, 高さ, いちばん高い fps, ("pixel_format" か "vcodec", その値)) の一覧を作る。

    ログは途中で切れて届くことがあり (FFmpeg の新しい版)、「pixel_format=…」と「min s=…」が別の行に来る。
    そのときは前の行の形式とつないで読む。
    """
    modes = set()
    head = ""                     # 「min s=…」がまだ来ていない「pixel_format=…」の行
    for line in lines:
        text = line.strip()
        if text.startswith(("pixel_format=", "vcodec=")):
            head = text
        elif text.startswith("min s=") and head:
            text = f"{head}  {text}"
        else:
            continue
        m = DSHOW_MODE.search(text)
        if m:
            head = ""
            kind, value, w, h, fps = m.groups()
            modes.add((int(w), int(h), float(fps), (kind, value)))
    return sorted(modes)


def best_mode(modes):
    """1080p の 60fps 前後を優先し、無ければいちばん大きく、fps の高いもの。
    同じ大きさと fps なら、圧縮していない形式 (pixel_format) を選ぶ (MJPEG などは展開に CPU を使う)。"""
    if not modes:
        return None
    full_hd = [m for m in modes if (m[0], m[1]) == (1920, 1080) and m[2] <= 61]
    pool = full_hd or [m for m in modes if m[2] <= 61] or modes
    return max(pool, key=lambda m: (m[0] * m[1], m[2], len(m) < 4 or m[3][0] == "pixel_format"))


def mode_label(mode):
    """形式のメニューに出す文字 (「1920×1080 59.94fps」、Windows は「… (yuyv422)」)。"""
    text = f"{mode[0]}×{mode[1]} {mode[2]:.2f}fps"
    return f"{text} ({mode[3][1]})" if len(mode) > 3 else text


def mode_key(mode):
    """形式を設定に残すときの文字 (機器ごとに、次に起動したときも同じ形式を選ぶため)。"""
    key = f"{mode[0]}x{mode[1]}@{mode[2]:.4f}"
    return f"{key}@{mode[3][0]}={mode[3][1]}" if len(mode) > 3 else key


class CameraEffects:
    """macOS のビデオエフェクト (コントロールセンターの「ビデオエフェクト」) の状態を読む。

    入っていると、映像が加工され (背景のぼかし・照明・自動の切り抜きなど)、顔や手の検出がこのプロセスの
    中で毎コマ動いて CPU も使う。アプリから切れるのはセンターフレームだけ (ほかは使う人が切る) なので、
    センターフレームは切り、ほかは入っていれば知らせる。macOS 以外や、読めないときは何もしない。
    """

    # (読むクラスメソッド, 表示名)
    EFFECTS = [("isPortraitEffectEnabled", "ポートレート"), ("isStudioLightEnabled", "スタジオ照明"),
               ("isBackgroundReplacementEnabled", "背景"), ("isCenterStageEnabled", "センターフレーム"),
               ("reactionEffectsGesturesEnabled", "リアクション")]     # 手のジェスチャーで出るもの

    def __init__(self):
        self.ok = False
        if not IS_MAC:
            return
        try:
            objc = ObjC("AVFoundation")
            c = objc.ctypes
            self._get = objc.msg(c.c_bool)
            self._set_int = objc.msg(None, c.c_long)
            self._set_bool = objc.msg(None, c.c_bool)
            self._objc = objc
            self._cls = objc.cls("AVCaptureDevice")
            self.ok = bool(self._cls)
        except (OSError, AttributeError):
            pass

    def _method(self, name):
        return self._objc.class_method(self._cls, name)

    def turn_off_center_stage(self):
        """センターフレームをアプリが決める側にして切る (カメラを開く前に呼ぶ)。"""
        if not self.ok:
            return
        mode, enable = self._method("setCenterStageControlMode:"), self._method("setCenterStageEnabled:")
        if mode and enable:
            self._set_int(self._cls, mode, 1)          # AVCaptureCenterStageControlModeApp
            self._set_bool(self._cls, enable, False)

    def enabled(self):
        """入っているエフェクトの表示名の一覧。"""
        if not self.ok:
            return []
        names = []
        for method, label in self.EFFECTS:
            sel = self._method(method)
            if sel and self._get(self._cls, sel):
                names.append(label)
        return names


# ---------- macOS: AVFoundation で直接読む (cam/libzvp_avcam.dylib) ----------
AVCAM_LIB = "libzvp_avcam.dylib"
WB_STEP = 500         # カメラのホワイトバランスの色温度のメニューの刻み (K)
_avcam = None
_avcam_tried = False


def avcam_library():
    """AVFoundation でカメラを読むライブラリ (build_cam_mac.sh で作る)。macOS 以外や、無いときは None。

    FFmpeg (avfoundation) は、同じ大きさの形式が 2 つあると一覧の後ろのものを選び、形式の種類も指定できない。
    USB カメラによっては後ろの形式ではコマが届かず (4K U3 Camera の 4K)、開くところで止まるので、
    macOS ではこちらで読む。無ければ FFmpeg で読む。
    """
    global _avcam, _avcam_tried
    if _avcam_tried or not IS_MAC:
        return _avcam
    _avcam_tried = True
    import ctypes
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    paths = [os.path.join(base, "cam", AVCAM_LIB) for base in (getattr(sys, "_MEIPASS", None), here) if base]
    path = next((p for p in paths if os.path.exists(p)), None)
    if path is None:
        return None
    try:
        lib = ctypes.CDLL(path)
    except OSError:
        return None
    lib.zvp_cam_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_double,
                                 ctypes.c_char_p, ctypes.c_int]
    lib.zvp_cam_open.restype = ctypes.c_void_p
    lib.zvp_cam_fps.argtypes = [ctypes.c_void_p]
    lib.zvp_cam_fps.restype = ctypes.c_double
    lib.zvp_cam_latest.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                                   ctypes.POINTER(ctypes.c_double)]
    lib.zvp_cam_latest.restype = ctypes.c_int64
    lib.zvp_cam_copy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int64, ctypes.c_int, ctypes.c_int]
    lib.zvp_cam_copy.restype = ctypes.c_int64
    lib.zvp_cam_close.argtypes = [ctypes.c_void_p]
    lib.zvp_cam_wb_info.argtypes = [ctypes.c_char_p] + [ctypes.POINTER(ctypes.c_int)] * 6
    lib.zvp_cam_wb_set.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    _avcam = lib
    return lib


class CameraWhiteBalance:
    """カメラ自身のホワイトバランス (UVC)。macOS で AVFoundation のライブラリがあるときだけ使える。

    カメラによっては自動ホワイトバランスが切れたまま (固定の色温度) で、映像全体が青や黄色に寄る。
    カメラの中で直すので、画面・SDI 出力・録画のどれも自然な色になる (表示だけを直す「自動WB」とは別)。
    """

    def __init__(self, has_auto, auto_on, t_min, t_max, t_cur, t_def):
        self.has_auto, self.auto_on = bool(has_auto), bool(auto_on)
        self.t_min, self.t_max, self.t_cur, self.t_def = t_min, t_max, t_cur, t_def

    @classmethod
    def read(cls, name, lib=None):
        """今の状態。使えないカメラ (USB の UVC でない、ライブラリが無いなど) は None。"""
        import ctypes
        lib = lib or avcam_library()
        if lib is None or not hasattr(lib, "zvp_cam_wb_info"):
            return None
        values = [ctypes.c_int() for _ in range(6)]
        if not lib.zvp_cam_wb_info(name.encode("utf-8"), *[ctypes.byref(v) for v in values]):
            return None
        return cls(*[v.value for v in values])

    def temperatures(self, step=WB_STEP):
        """メニューに並べる色温度 (最小から最大まで step ごと。両端と初期値も入れる)。"""
        if not self.t_max:
            return []
        values = set(range(-(-self.t_min // step) * step, self.t_max + 1, step)) | {self.t_min, self.t_max}
        if self.t_min <= self.t_def <= self.t_max:
            values.add(self.t_def)
        return sorted(values)

    @staticmethod
    def apply(name, auto_on, temperature=0, lib=None):
        """自動にするか、色温度 (K) に固定する。できたら True。"""
        lib = lib or avcam_library()
        return bool(lib and lib.zvp_cam_wb_set(name.encode("utf-8"), 1 if auto_on else 0, int(temperature)))


class AvCamFrame:
    """AVFoundation から届いた最新のコマ (NV12)。write_into で共有メモリのバッファへ直接写す。"""

    class Format:
        name = "nv12"

    format = Format()

    def __init__(self, camera, width, height, time):
        self.camera, self.width, self.height, self.time = camera, width, height, time

    def write_into(self, dst, width, height):
        import ctypes
        ptr = dst.ctypes.data_as(ctypes.c_void_p)
        return self.camera.lib.zvp_cam_copy(self.camera.handle, ptr, dst.nbytes, width, height) > 0


class AvCamCamera:
    """AVFoundation で開いたカメラ。FFmpeg で開いたもの (av の container) と同じ使い方ができるようにしてある
    (CameraCapture.read が streams.video[0] と demux を使う)。コマが届かない間も、止める合図を見られるように
    POLL_SECONDS ごとに空の知らせを返す。"""

    POLL_SECONDS = 0.002

    class _Packet:
        def __init__(self, frames):
            self.frames = frames

        def decode(self):
            return self.frames

    def __init__(self, lib, name, mode):
        import ctypes
        from fractions import Fraction
        self.lib = lib
        err = ctypes.create_string_buffer(512)
        self.handle = lib.zvp_cam_open(name.encode("utf-8"), mode[0], mode[1], float(mode[2]), err, 512)
        if not self.handle:
            raise OSError(err.value.decode("utf-8", "replace") or "カメラを開けません")
        fps = lib.zvp_cam_fps(self.handle)
        stream = type("Stream", (), {"average_rate": Fraction(fps).limit_denominator(1001) if fps else None,
                                     "guessed_rate": None})()
        self.streams = type("Streams", (), {"video": [stream]})()
        self.last_seq = 0

    def demux(self, stream):
        import ctypes
        w, h, t = ctypes.c_int(), ctypes.c_int(), ctypes.c_double()
        while self.handle:
            seq = self.lib.zvp_cam_latest(self.handle, ctypes.byref(w), ctypes.byref(h), ctypes.byref(t))
            if seq > self.last_seq:
                self.last_seq = seq
                yield self._Packet([AvCamFrame(self, w.value, h.value, t.value)])
            else:
                time.sleep(self.POLL_SECONDS)
                yield self._Packet([])

    def close(self):
        if self.handle:
            self.lib.zvp_cam_close(self.handle)
            self.handle = None


def open_camera(index, name, mode=None):
    """カメラを開く (mode を選んでいればその形式で、無ければ 1080p 60fps 前後があればそれで。NV12 で受け取る)。

    index が文字列なら動画ファイルのパスとして開く (カメラの代わりに確かめるとき用)。
    """
    import av
    if isinstance(index, str):
        return av.open(index)
    fmt = camera_format()
    options = {"pixel_format": "nv12"} if fmt == "avfoundation" else {}
    mode = mode or best_mode(camera_modes(index, name))
    lib = avcam_library() if fmt == "avfoundation" else None
    if lib is not None and mode:
        return AvCamCamera(lib, name, mode)
    if mode:
        options["video_size"] = f"{mode[0]}x{mode[1]}"
        options["framerate"] = f"{mode[2]:.6f}".rstrip("0").rstrip(".")
        if len(mode) > 3:                    # Windows: その大きさと fps を出せる形式も指定する
            options[mode[3][0]] = mode[3][1]
    url = f"{index}:none" if fmt == "avfoundation" else f"video={name}"
    return av.open(url, format=fmt, options=options, timeout=(10.0, 0.5))


class CameraCapture:
    """カメラから読み続けて、共有メモリのバッファへ書く (別プロセスの中で動かす)。

    映像が届いていない (HDMI に何もつながっていないなど) と、FFmpeg はコマを返さないか、「今は読めない」
    などの失敗を返す。カメラの故障ではないので「信号なし」として扱い、少し待って開き直す (映像をつなげば
    そのまま映る)。エラーとして知らせるのは、何度開き直しても開けないときだけ。
    """

    RETRY_SECONDS = 1.0
    OPEN_FAILURES = 3

    def __init__(self, opener, state, messages, stop):
        self.opener = opener            # カメラを開く関数 (確かめるときは作り物にする)
        self.state = state              # 画面側と共有の数値 (multiprocessing.Array)
        self.messages = messages        # 画面側への知らせ (multiprocessing.Queue)
        self.stop = stop                # 止める合図 (multiprocessing.Event)
        self.layout = None              # (形式, 幅, 高さ)
        self.views = []
        self.shms = []                  # 作った共有メモリ (古いものは少し残してから片付ける)
        self.fps = 0.0
        self.times = []                 # fps を求めるためのコマの時刻
        self.effects = None             # macOS のビデオエフェクトを読むもの (CameraEffects。無ければ読まない)
        self.effect_names = None        # 最後に知らせたエフェクトの一覧
        self.effects_at = 0.0

    def run(self):
        failures = 0
        try:
            while not self.stop.is_set():
                try:
                    container = self.opener()
                except Exception as e:
                    failures += 1
                    if failures >= self.OPEN_FAILURES:
                        self.messages.put(("error", f"カメラを開けません: {e}"))
                    self.stop.wait(self.RETRY_SECONDS)
                    continue
                if failures >= self.OPEN_FAILURES:
                    self.messages.put(("error", ""))
                failures = 0
                try:
                    self.read(container)
                except Exception as e:
                    self.messages.put(("problem", f"{type(e).__name__}: {e}"))
                finally:
                    container.close()
                self.stop.wait(self.RETRY_SECONDS)
        finally:
            self.release(keep=0)

    def read(self, container):
        """止める合図か、読み取りの失敗・終わりまで読み続ける。

        macOS のカメラ (AVFoundation) は、次のコマがまだ届いていないと「今は読めない」(EAGAIN) を返す。
        失敗ではないので、少し待って読み直す (読み直すには demux を作り直す)。
        """
        stream = container.streams.video[0]
        rate = stream.average_rate or stream.guessed_rate
        # カメラは fps を知らせず、時刻の単位 (1/1000000 など) が入ってくる。そのときはコマの時刻から求める
        self.fps = float(rate) if rate and rate <= 1000 else 0.0
        self.times = []
        while not self.stop.is_set():
            try:
                for packet in container.demux(stream):
                    if self.stop.is_set():
                        return
                    for frame in packet.decode():
                        self.store(frame)
                return
            except BlockingIOError:
                self.stop.wait(WAIT_SECONDS)

    def measure_fps(self, frame):
        """fps が分からないとき、コマの時刻から求めて知らせる。

        カメラの時刻は 1 コマごとには揺れるので、開いた直後の 10 コマを除いた 50 コマの平均の間隔で求める。
        """
        if frame.time is None or len(self.times) > FPS_FRAMES:
            return
        self.times.append(frame.time)
        if len(self.times) <= FPS_FRAMES:
            return
        span = self.times[-1] - self.times[FPS_SKIP]
        if span <= 0:
            return
        self.fps = round((FPS_FRAMES - FPS_SKIP) / span, 2)
        with self.state.get_lock():
            self.state[S_FPS] = self.fps
        self.messages.put(("fps", self.fps))

    def check_effects(self):
        """入っている macOS のビデオエフェクトを、変わったときに知らせる (使う人が途中で切り替えられる)。"""
        now = time.monotonic()
        if self.effects is None or now - self.effects_at < EFFECTS_SECONDS:
            return
        self.effects_at = now
        names = self.effects.enabled()
        if names != self.effect_names:
            self.effect_names = names
            self.messages.put(("effects", names))

    def store(self, frame):
        fmt = "nv12" if frame.format.name == "nv12" else gpu_format(frame.format.name)
        w, h = even_size(fmt, frame.width, frame.height)
        if (fmt, w, h) != self.layout:
            self.new_buffers(fmt, w, h)
        if not self.fps:
            self.measure_fps(frame)
        self.check_effects()
        st = self.state
        with st.get_lock():
            st[S_FRAMES] += 1
            st[S_LAST_AT] = time.time()
            busy = (int(st[S_HELD]), int(st[S_LATEST]))       # 表示中と最新には書かない
            idx = next((i for i in range(BUFFERS) if i not in busy), -1)
            if idx < 0:
                st[S_DROPPED] += 1
                return
        if hasattr(frame, "write_into"):            # AVFoundation のコマは、共有メモリへ直接写す
            frame.write_into(self.views[idx], w, h)
        else:
            write_frame(self.views[idx], frame, fmt, w, h)
        with st.get_lock():
            st[S_LATEST] = idx
            st[S_LATEST_NO] = st[S_FRAMES]
            st[S_LATEST_T] = float(frame.time or 0.0)

    def new_buffers(self, fmt, w, h):
        """形式が変わった: 新しい共有メモリを作って画面側に知らせる。"""
        import numpy as np
        nbytes = int(np.prod(frame_size(fmt, w, h)))
        shm = attach_shared_memory(create_size=nbytes * BUFFERS)
        self.views = [np.ndarray((nbytes,), np.uint8, buffer=shm.buf, offset=i * nbytes) for i in range(BUFFERS)]
        self.shms.append(shm)
        self.release(keep=2)             # 画面側がまだ前のものを開こうとしているかもしれないので 1 つは残す
        self.layout = (fmt, w, h)
        st = self.state
        with st.get_lock():
            st[S_SERIAL] += 1
            st[S_LATEST] = st[S_HELD] = -1
            st[S_FPS] = self.fps
            serial = int(st[S_SERIAL])
        self.messages.put(("format", serial, shm.name, fmt, w, h, self.fps))

    def release(self, keep):
        """作った共有メモリのうち、新しい keep 個を残して片付ける。"""
        while len(self.shms) > keep:
            shm = self.shms.pop(0)
            if keep == 0:
                self.views = []
            try:
                shm.close()
            except BufferError:          # まだ numpy の配列が指している (最後の片付けでは配列ごと捨てる)
                pass
            unlink_shared_memory(shm)


def camera_worker(index, name, state, messages, stop, mode=None):
    """別プロセスの入口。mode は使う人が選んだ形式 (None なら自動で選ぶ)。"""
    capture = CameraCapture(lambda: open_camera(index, name, mode), state, messages, stop)
    if not isinstance(index, str):          # 本物のカメラのときだけ (動画ファイルで確かめるときは要らない)
        capture.effects = CameraEffects()
        capture.effects.turn_off_center_stage()
    capture.run()
