"""デコード側の処理 (PyAV と numpy だけを使う)。

再生用のデコードは別プロセスで動かす。そのプロセスが Qt を読み込まずに済むよう、
画面 (PySide6) とは別のモジュールにしている。
"""
import math
import queue
import time
from fractions import Fraction

import av


class VideoSource:
    """1 つの動画ファイル。メタデータ、フレーム単位のシーク、順次デコードを受け持つ。

    同時に使うのは 1 スレッドだけ (再生中はデコードスレッド、停止中は GUI スレッド)。
    """

    def __init__(self, path):
        self.container = av.open(path)
        if not self.container.streams.video:
            raise ValueError("映像トラックがありません")
        self.stream = self.container.streams.video[0]
        self.stream.thread_type = "AUTO"
        self.time_base = float(self.stream.time_base)
        rate = self.stream.average_rate or self.stream.guessed_rate or Fraction(30)
        self.fps = float(rate)
        self.start = (self.stream.start_time or 0) * self.time_base
        if self.stream.duration:
            self.duration = self.stream.duration * self.time_base
        elif self.container.duration:
            self.duration = self.container.duration / av.time_base
        elif self.stream.frames:
            self.duration = self.stream.frames / self.fps
        else:
            self.duration = 0.0
        self.bad_packets = 0     # 壊れていて読み飛ばしたパケットの数
        self.width = self.stream.codec_context.width
        self.height = self.stream.codec_context.height
        # YUV → RGB の変換式 (GPU 描画用)。不明なときは HD 以上を BT.709 とみなす
        cc = self.stream.codec_context
        colorspace = getattr(cc, "colorspace", 2) or 2
        self.bt709 = colorspace == 1 or (colorspace == 2 and self.height >= 720)
        self.pix_fmt = cc.pix_fmt or "yuv420p"   # デコード結果の形式 (例: yuv422p10le)
        self.full_range = getattr(cc, "color_range", 1) == 2 or self.pix_fmt.startswith("yuvj")
        self._frames = None
        self.last_t = None   # 直前に next() で返したフレームの時刻

    def close(self):
        self.container.close()

    def frame_time(self, frame):
        if frame.pts is None:
            return frame.time - self.start
        return frame.pts * self.time_base - self.start

    def seek(self, t):
        """t 秒より前のキーフレームへ移動する。次の next() はそこから返す。"""
        target = int(max(0.0, t + self.start) / self.time_base)
        self.container.seek(target, stream=self.stream, backward=True, any_frame=False)
        self._frames = self._decode()
        self.last_t = None

    def _decode(self):
        """パケットを順にデコードする。壊れたパケットは読み飛ばして続ける。

        container.decode() だと最初のエラーで止まって以降のフレームが読めなくなるため、自前で回す。
        """
        for packet in self.container.demux(self.stream):
            try:
                frames = packet.decode()
            except av.error.EOFError:
                return
            except av.error.FFmpegError:
                self.bad_packets += 1
                continue
            yield from frames

    def next(self):
        if self._frames is None:
            self.seek(0)
        frame = next(self._frames, None)
        if frame is not None:
            self.last_t = self.frame_time(frame)
        return frame

    def frame_at(self, t, fps):
        """t 秒を含むフレームをデコードして返す (末尾を越えたら最後のフレーム)。"""
        self.seek(t)
        last = None
        while True:
            frame = self.next()
            if frame is None:
                return last
            last = frame
            if self.frame_time(frame) >= t - 0.5 / fps:
                return frame


def clock_now(values):
    """共有時計 [基準の動画時刻, 基準の壁時計, 速度, 周回, 書き換え間隔, 次の書き換え] から現在の動画時刻を求める。"""
    base = values[0]
    if math.isnan(base):
        return None
    return base + (time.perf_counter() - values[1]) * values[2]


class SharedClock:
    """再生位置 (動画内の秒) を壁時計から求める。デコード用プロセスとも共有する。

    ループ再生では動画内の時刻が折り返すので、何周目を表示しているか (lap) も持つ。
    デコード側はこれを見て、次の周のコマを「遅れたコマ」と取り違えないようにする。
    画面の書き換え 1 回あたりの動画内の秒 (display_period) と、次の書き換えで表示する
    動画内の時刻 (next_vsync) も持つ。デコード側はこれで、画面が実際に選ぶコマだけを送り、
    出せないコマ (240fps を 144Hz で出すときの余り) の変換を省く。
    """

    def __init__(self, ctx):
        self.values = ctx.Array("d", [math.nan, 0.0, 1.0, 0.0, 0.0, math.nan], lock=False)

    def reset(self):
        self.values[0] = math.nan
        self.values[3] = 0.0
        self.values[5] = math.nan

    def start(self, t, rate):
        self.values[1] = time.perf_counter()
        self.values[2] = rate
        self.values[0] = t

    def next_lap(self):
        self.values[3] += 1

    def set_display_period(self, seconds):
        """画面の書き換え 1 回あたりに進む動画内の秒 (速度 / Hz)。0 なら間引かない。"""
        self.values[4] = seconds

    def set_next_vsync(self, t):
        """次の書き換えで、画面はこの動画内の時刻までのコマを選ぶ (nan なら不明)。"""
        self.values[5] = t

    @property
    def started(self):
        return not math.isnan(self.values[0])

    @property
    def lap(self):
        return int(self.values[3])

    def now(self):
        return clock_now(self.values)

    def set(self, t, wall, rate):
        """壁時計 wall のときに動画時刻 t となるよう合わせる (書き換えの時刻に揃えるとき)。"""
        self.values[1] = wall
        self.values[2] = rate
        self.values[0] = t

    def at(self, wall):
        """壁時計 wall (perf_counter の秒) のときの動画時刻。"""
        return self.values[0] + (wall - self.values[1]) * self.values[2]


# 受け渡すフレームの形式。
#   YUV のプレーン形式 (PLANAR にあるもの): デコード結果を変換せずにそのまま渡し、GPU で RGB に
#       変換・拡大縮小する。10bit や 4:2:2 (XAVC などの業務用カメラ) も CPU で変換しない
#   "bgra": CPU で RGB に変換して Qt に描かせる (OpenGL が使えない PC 向け)
# 名前: (色差の横の縮み, 縦の縮み, 1 画素のバイト数, ビット数)。縮みは 2 のべき (1 なら半分)
PLANAR = {
    "yuv420p": (1, 1, 1, 8), "yuvj420p": (1, 1, 1, 8),
    "yuv422p": (1, 0, 1, 8), "yuvj422p": (1, 0, 1, 8),
    "yuv444p": (0, 0, 1, 8), "yuvj444p": (0, 0, 1, 8),
    "yuv420p10le": (1, 1, 2, 10), "yuv422p10le": (1, 0, 2, 10), "yuv444p10le": (0, 0, 2, 10),
    "yuv420p12le": (1, 1, 2, 12), "yuv422p12le": (1, 0, 2, 12), "yuv444p12le": (0, 0, 2, 12),
}


def gpu_format(source_fmt):
    """GPU 描画で受け渡す形式。そのまま扱えない形式 (NV12 や RGB など) は yuv420p に変換する。"""
    return source_fmt if source_fmt in PLANAR else "yuv420p"


def plane_sizes(fmt, width, height):
    """Y・U・V 各プレーンの (幅, 高さ)。"""
    sx, sy, _, _ = PLANAR[fmt]
    return [(width, height), (width >> sx, height >> sy), (width >> sx, height >> sy)]


def frame_size(fmt, width, height):
    """フレームを置く配列の形。YUV は 3 プレーンを詰めて並べた 1 次元のバイト列。"""
    if fmt == "bgra":
        return (height, width, 4)
    bps = PLANAR[fmt][2]
    return (sum(w * h for w, h in plane_sizes(fmt, width, height)) * bps,)


def even_size(fmt, width, height):
    """色差が縮む向きは偶数にそろえる (はみ出す 1 画素は表示しない)。"""
    if fmt == "bgra":
        return (width, height)
    sx, sy, _, _ = PLANAR[fmt]
    return (width & ~((1 << sx) - 1), height & ~((1 << sy) - 1))


def plane_views(data, fmt, width, height):
    """詰めて並べたバイト列 data を、プレーンごとの 2 次元配列 (8bit は uint8、それ以上は uint16) で見る。"""
    import numpy as np

    bps = PLANAR[fmt][2]
    dtype = np.uint8 if bps == 1 else np.uint16
    views, offset = [], 0
    for w, h in plane_sizes(fmt, width, height):
        n = w * h * bps
        views.append(data[offset:offset + n].view(dtype).reshape(h, w))
        offset += n
    return views


def write_frame(dst, frame, fmt, width, height):
    """デコードしたフレームを dst (frame_size の形) へ書き込む。"""
    import numpy as np

    if fmt == "bgra":
        np.copyto(dst, frame.to_ndarray(format=fmt, width=width, height=height))
        return
    if frame.format.name != fmt or frame.width < width or frame.height < height:
        frame = frame.reformat(format=fmt, width=width, height=height)      # 遅い道 (形式が違うときだけ)
    bps = PLANAR[fmt][2]
    offset = 0
    # 各プレーンは行末に詰め物があるので、見える幅だけを詰めて写す
    for plane, (cols, rows) in zip(frame.planes, plane_sizes(fmt, width, height)):
        row_bytes = cols * bps
        src = np.frombuffer(plane, np.uint8, count=plane.line_size * rows).reshape(rows, plane.line_size)
        dst[offset:offset + row_bytes * rows].reshape(rows, row_bytes)[:] = src[:, :row_bytes]
        offset += row_bytes * rows


# ---------- デコード用プロセス ----------
# 1080p / 120fps のデコードは CPU を使い切る。同じプロセスのスレッドで行うと
# Python の GIL を画面側と奪い合って描画が止まるため、別プロセスで行い、
# フレームは共有メモリのスロット経由で受け渡す。
#
# スロットは先読みの貯め (デコードキャッシュ) でもある。デコードが一瞬遅れても、
# 貯めてあるコマで表示を続けられる。ループ再生の折り返しもデコード側で行い、
# B の先を表示している間に A からのコマを貯めておくので、折り返しで止まらない。
END_OF_FILE = -1     # 終端に達した (t にエラー文字列が入ることがある)
SESSION_END = -2     # そのセッションの送信がすべて終わった
WRAP = -3            # ループで折り返した (t = 折り返す前の最後のコマの終わりの時刻)
SLOT_BUDGET = 256 * 1024 * 1024   # 先読みに使う共有メモリの上限
SLOT_MIN, SLOT_MAX = 8, 96
LATE = 0.025         # 表示の時刻をこれだけ (実時間の秒) 過ぎたコマは、変換せずに捨てる (追いつくため)
STARVED = 0.05       # これだけ (秒) コマを送れていなければ、遅れたコマでも送る


def slot_count(frame_bytes):
    """先読みできるコマ数 (1080p の YUV なら 82 コマ = 120fps で約 0.7 秒)。"""
    return max(SLOT_MIN, min(SLOT_MAX, SLOT_BUDGET // max(1, frame_bytes)))


class _CountedQueue:
    """送った数を共有の数 (sent) に数える Queue。画面側は受け取った数との差で、貯まっている数を知る
    (macOS では Queue.qsize() が使えないため)。"""

    def __init__(self, q, sent):
        self.q, self.sent = q, sent

    def put(self, item):
        self.q.put(item)
        self.sent.value += 1


def attach_shared_memory(name):
    """画面側が作った共有メモリを開く。片付けは作った側 (画面側) が行う。

    macOS / Linux では、開いただけの子プロセスも後始末の対象として登録され、終了時に
    「片付け忘れ」の警告が出たり、先に消されたりする。その登録をしないようにする。
    子プロセスは画面側と同じ後始末係 (resource_tracker) を共有しているので、開いたあとで
    登録を外すと画面側の登録まで消え、画面側が片付けるときに KeyError が出る。
    そのため、開くときに登録そのものをしない (Python 3.13 の track=False と同じ)。
    """
    import os
    import sys
    from multiprocessing import shared_memory

    if os.name != "posix":                 # Windows は後始末の登録をしない (OS が片付ける)
        return shared_memory.SharedMemory(name=name)
    if sys.version_info >= (3, 13):
        return shared_memory.SharedMemory(name=name, track=False)
    from multiprocessing import resource_tracker
    register = resource_tracker.register

    def register_except_shm(res_name, rtype):
        if rtype != "shared_memory":
            register(res_name, rtype)

    resource_tracker.register = register_except_shm
    try:
        return shared_memory.SharedMemory(name=name)
    finally:
        resource_tracker.register = register


def decode_worker(path, fmt, shm_name, slots, cmd_q, ready_q, free_q, clock_values, decoded, sent, started):
    """再生用のデコードを続ける。命令: ("play", セッション, 開始秒, fps, ループ) / ("stop",) / ("quit",)

    ループは None (しない) か (A 秒, B 秒)。B が None なら終端まで再生して A へ戻る。
    """
    import numpy as np

    ready_q = _CountedQueue(ready_q, sent)
    shm = attach_shared_memory(shm_name)
    src = VideoSource(path)
    w, h = even_size(fmt, src.width, src.height)
    shape = frame_size(fmt, w, h)
    nbytes = int(np.prod(shape))
    views = [np.ndarray(shape, np.uint8, buffer=shm.buf, offset=i * nbytes) for i in range(slots)]
    started.set()                     # ここまで (Python と PyAV の読み込み) に 1 秒ほどかかる
    while True:
        cmd = cmd_q.get()
        if cmd[0] == "quit":
            break
        if cmd[0] != "play":
            continue
        _, session, start_t, fps, loop = cmd
        try:
            _play(src, views, session, start_t, fps, loop, cmd_q, ready_q, free_q, clock_values, decoded, fmt, w, h)
        except Exception as e:
            ready_q.put((session, END_OF_FILE, f"{type(e).__name__}: {e}"))
        ready_q.put((session, SESSION_END, None))
    del views
    shm.close()


def _play(src, views, session, start_t, fps, loop, cmd_q, ready_q, free_q, clock_values, decoded, fmt, w, h):
    half = 0.5 / fps
    lap = 0                  # 何周目のコマを作っているか
    held = None              # 送るか決めかねているコマ (次のコマを見てから決める)
    first_t = start_t        # この周で最初に出すコマの時刻
    last_t = None            # この周で最後に扱ったコマの時刻
    last_sent = time.perf_counter()   # 最後にコマを送った時刻 (壁時計)
    src.seek(start_t)

    def send(frame, t):
        """空きスロットに書き込んで送る。停止の命令が来たら False。"""
        slot = None
        while slot is None and cmd_q.empty():
            try:
                slot = free_q.get(timeout=0.01)   # 待つ間も停止の命令にすぐ気づけるように短く
            except queue.Empty:
                pass
        if slot is None:
            return False
        nonlocal last_sent
        write_frame(views[slot], frame, fmt, w, h)
        ready_q.put((session, slot, t))
        last_sent = time.perf_counter()
        return True

    def target(t):
        """画面がこのコマを選べるようになる書き換えの番号 (間引きをしないときは None)。

        画面は書き換えのたびに「次の書き換えの時刻 V までのコマ」の最後の 1 つを選ぶ。
        V は書き換え 1 回ごとに period ずつ進むので、t <= V0 + k * period となる最小の k。
        """
        period, v0 = clock_values[4], clock_values[5]
        if period <= 1.2 / fps or math.isnan(v0) or lap != int(clock_values[3]):
            return None
        return math.ceil((t - v0) / period - 1e-6)

    def boundary(k):
        """書き換え k 回目で選ばれるコマの時刻の上限。"""
        return clock_values[5] + k * clock_values[4]

    def flush():
        nonlocal held
        if held is not None:
            frame, t = held
            held = None
            return send(frame, t)
        return True

    def wrap():
        nonlocal lap, first_t, last_t
        if not flush():
            return False
        end_t = (last_t if last_t is not None else first_t) + 1 / fps
        ready_q.put((session, WRAP, end_t))
        lap += 1
        first_t, last_t = loop[0], None
        src.seek(first_t)
        return True

    while cmd_q.empty():                  # 次の命令 (停止など) が来たら終える
        frame = src.next()
        t = None if frame is None else src.frame_time(frame)
        past_b = loop is not None and loop[1] is not None and t is not None and t > loop[1] + half
        if frame is None or past_b:
            if loop is None or last_t is None:    # 終端 (ループしない) か、区間にコマが 1 つもない
                if flush():
                    ready_q.put((session, END_OF_FILE, None))
                return
            if not wrap():
                return
            continue
        if t < first_t - half:
            continue
        last_t = t
        decoded.value += 1
        now = clock_now(clock_values)
        # 遅れは実時間で測る (速い再生では動画内の時刻がすぐ進むので、動画内の秒で測ると厳しすぎる)
        late = (now is not None and lap == int(clock_values[3])
                and (now - t) / max(clock_values[2], 1e-6) > LATE)
        if late and time.perf_counter() - last_sent < STARVED:
            held = None
            continue                      # 表示に間に合わないコマは変換しない (追いつくため)
        if late:
            # デコードが再生の速さにずっと追いつかないとき (強制 fps を上げすぎたなど) は、
            # 遅れたコマばかりになる。全部捨てると画面が止まるので、遅れていても送って表示を進める
            held = None
            if not send(frame, t):
                return
            continue
        # 画面の書き換えより細かいコマ (240fps の動画を 144Hz で出すときなど) は、画面が
        # 選ぶコマだけを送る。前のコマと同じ書き換えで選べるなら、前のコマは画面に出ない
        if held is not None:
            a, b = target(held[1]), target(t)
            # 境目のすぐ手前のコマは、画面側の時刻の揺れで次の書き換えにずれることがあるので、
            # そのときは前のコマも捨てずに送る (どちらに転んでも画面に選ぶコマがあるように)
            if a is not None and b is not None and b <= a and boundary(b) - t > 0.3 * clock_values[4]:
                held = (frame, t)
                continue
            if not flush():
                return
        held = (frame, t)
