"""カメラから読む側 (別プロセスで動く。PyAV と numpy だけを使い、Qt は読み込まない)。

macOS のカメラ (AVFoundation) では、映像が届いていないと FFmpeg はコマが来るまで待ち続け、時間切れの
指定も効かない。スレッドで読むと止められず、カメラをつかんだまま残ってしまうので、別プロセスで読む。
止まらなければ画面側 (player_live.CameraInput) がプロセスごと終わらせる。

コマは共有メモリのバッファ (BUFFERS 枚) に書き、どのバッファが最新かを共有の数値 (state) で知らせる。
画面が表示中のバッファ (held) と最新のバッファには書かない。形式が変わったら新しい共有メモリを作り、
その名前を queue で知らせる。
"""
import sys
import time

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
    return {"darwin": "avfoundation", "win32": "dshow"}.get(sys.platform)


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
    names, video = [], False
    for line in ffmpeg_log(url, fmt, {"list_devices": "true"}):
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


def camera_modes(index):
    """カメラの使える (幅, 高さ, fps) の一覧 (macOS)。わざと無い fps を指定して、一覧のログを出させる。"""
    if camera_format() != "avfoundation":
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


def best_mode(modes):
    """1080p の 60fps 前後を優先し、無ければいちばん大きく、fps の高いもの。"""
    if not modes:
        return None
    full_hd = [m for m in modes if (m[0], m[1]) == (1920, 1080) and m[2] <= 61]
    pool = full_hd or [m for m in modes if m[2] <= 61] or modes
    return max(pool, key=lambda m: (m[0] * m[1], m[2]))


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
        if sys.platform != "darwin":
            return
        try:
            import ctypes
            import ctypes.util
            objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
            ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
            objc.objc_getClass.restype = objc.sel_registerName.restype = ctypes.c_void_p
            objc.objc_getClass.argtypes = objc.sel_registerName.argtypes = [ctypes.c_char_p]
            objc.class_getClassMethod.restype = ctypes.c_void_p
            objc.class_getClassMethod.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            send = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
            # objc_msgSend は呼ぶメソッドの型に合わせて呼ぶ (arm64 では可変長引数として呼べない)
            self._get = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(send)
            self._set_int = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(send)
            self._set_bool = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool)(send)
            self._objc = objc
            self._cls = objc.objc_getClass(b"AVCaptureDevice")
            self.ok = bool(self._cls)
        except (OSError, AttributeError):
            pass

    def _method(self, name):
        sel = self._objc.sel_registerName(name.encode())
        return sel if self._objc.class_getClassMethod(self._cls, sel) else None

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


def open_camera(index, name):
    """カメラを開く (1080p 60fps 前後があればそれで、NV12 で受け取る)。

    index が文字列なら動画ファイルのパスとして開く (カメラの代わりに確かめるとき用)。
    """
    import av
    if isinstance(index, str):
        return av.open(index)
    fmt = camera_format()
    options = {"pixel_format": "nv12"} if fmt == "avfoundation" else {}
    mode = best_mode(camera_modes(index))
    if mode:
        options["video_size"] = f"{mode[0]}x{mode[1]}"
        options["framerate"] = f"{mode[2]:.6f}".rstrip("0").rstrip(".")
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
            idx = next((i for i in range(BUFFERS) if i not in (int(st[S_HELD]), int(st[S_LATEST]))), -1)
            if idx < 0:
                st[S_DROPPED] += 1
                return
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


def camera_worker(index, name, state, messages, stop):
    """別プロセスの入口。"""
    capture = CameraCapture(lambda: open_camera(index, name), state, messages, stop)
    if not isinstance(index, str):          # 本物のカメラのときだけ (動画ファイルで確かめるときは要らない)
        capture.effects = CameraEffects()
        capture.effects.turn_off_center_stage()
    capture.run()
