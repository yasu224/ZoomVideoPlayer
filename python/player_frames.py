"""画面側で扱うフレームと、再生用デコードプロセスとの受け渡し。"""
import multiprocessing as mp
import queue
import threading
import time
from collections import OrderedDict
from multiprocessing import shared_memory

import numpy as np
from PySide6.QtGui import QImage

from player_core import SESSION_END, SharedClock, decode_worker, frame_size, slot_count, write_frame


class Picture:
    """表示する 1 コマ。data は frame_size(fmt) の形の配列 (共有メモリのスロットか、手元のコピー)。"""

    __slots__ = ("t", "fmt", "w", "h", "data", "image")

    def __init__(self, t, fmt, w, h, data):
        self.t, self.fmt, self.w, self.h, self.data = t, fmt, w, h, data
        # CPU 描画用。BGRA を Qt がそのまま描ける Format_RGB32 として見せる
        self.image = QImage(data.data, w, h, w * 4, QImage.Format_RGB32) if fmt == "bgra" else None

    def copy(self, t=None):
        return Picture(self.t if t is None else t, self.fmt, self.w, self.h, self.data.copy())


class FrameCache:
    """停止中に表示したコマを覚えておく (フレーム番号 → Picture)。

    コマ送りで 1 コマ戻るたびにキーフレームからデコードし直すと遅いので、手前のコマを
    前もってデコードしてここに入れておく (BackFiller)。容量を超えたら、focus (いま表示して
    いるコマ) から遠いものを捨てる。先のコマは順にデコードすればすぐ出せるので、
    先のコマは手前のコマより AHEAD_WEIGHT 倍遠いとみなす。focus がなければ古いものから捨てる。
    """

    AHEAD_WEIGHT = 4

    def __init__(self, budget_bytes=384 * 1024 * 1024):
        self.budget = budget_bytes
        self.items = OrderedDict()
        self.bytes = 0
        self.focus = None

    def distance(self, index):
        d = index - self.focus
        return d * self.AHEAD_WEIGHT if d > 0 else -d

    def clear(self):
        self.items.clear()
        self.bytes = 0

    def get(self, index):
        picture = self.items.get(index)
        if picture is not None:
            self.items.move_to_end(index)
        return picture

    def put(self, index, picture):
        if index in self.items:
            self.items.move_to_end(index)
            return
        self.items[index] = picture
        self.bytes += picture.data.nbytes
        while self.bytes > self.budget and len(self.items) > 1:
            if self.focus is None:
                _, old = self.items.popitem(last=False)
            else:
                old = self.items.pop(max(self.items, key=self.distance))
            self.bytes -= old.data.nbytes

    def ready_below(self, index):
        """index の手前に続けて入っているコマ数。"""
        n = 0
        while index - n - 1 in self.items:
            n += 1
        return n

    def capacity(self, frame_bytes):
        """入るコマ数。"""
        return max(1, self.budget // max(1, frame_bytes))


def make_picture(av_frame, t, fmt, w, h):
    data = np.empty(frame_size(fmt, w, h), np.uint8)
    write_frame(data, av_frame, fmt, w, h)
    return Picture(t, fmt, w, h, data)


class BackFiller:
    """停止中に、表示しているコマより手前のコマを裏のスレッドでデコードしておく。

    デコードは前へしか進めないので、1 コマ戻るにはキーフレームから読み直すしかない。
    長い GOP の 10bit 4:2:2 (XAVC) では 1 コマ 13 ミリ秒ほどかかり、まとめて戻ると
    0.3〜0.5 秒止まっていた。手前のコマを先に用意しておけば、戻るコマ送りはすぐ出る。

    画面側とは別のファイルハンドル (VideoSource) を使う。デコード中は FFmpeg と numpy が
    GIL を手放すので、停止中の画面の邪魔にはならない。再生中は動かさない (cancel)。
    結果は results に (世代, フレーム番号, Picture) で入り、画面側が取り出してキャッシュへ入れる。
    頼んだ範囲を終えると (世代, None, None) が入る。
    """

    def __init__(self, path, fmt, width, height):
        self.path, self.fmt, self.w, self.h = path, fmt, width, height
        self.requests = queue.Queue()
        self.results = queue.Queue()
        self.gen = 0
        self.busy = None          # 実行中の (lo, hi, 世代)
        self.thread = None

    def request(self, lo, hi, fps):
        """lo〜hi 番のコマを作る (キーフレームから hi までに読んだコマもすべて返す)。"""
        self.gen += 1
        self.busy = (lo, hi, self.gen)
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        self.requests.put((self.gen, lo, hi, fps))

    def cancel(self):
        self.gen += 1             # 実行中の分は次のコマで止まる
        self.busy = None

    def covers(self, index):
        return self.busy is not None and self.busy[0] <= index <= self.busy[1]

    def finished(self, gen):
        if self.busy is not None and self.busy[2] == gen:
            self.busy = None

    def close(self):
        self.cancel()
        if self.thread is not None:
            self.requests.put(None)
            self.thread.join(2)

    def _run(self):
        from player_core import VideoSource
        src = None
        try:
            while (req := self.requests.get()) is not None:
                gen, lo, hi, fps = req
                if gen != self.gen:
                    continue
                if src is None:
                    src = VideoSource(self.path)
                src.seek(lo / fps)
                while gen == self.gen:
                    frame = src.next()
                    if frame is None:
                        break
                    t = src.frame_time(frame)
                    i = round(t * fps)
                    if i > hi:
                        break
                    self.results.put((gen, i, make_picture(frame, t, self.fmt, self.w, self.h)))
                    if i == hi:
                        break
                self.results.put((gen, None, None))
        except Exception as e:           # 読めない所があっても画面側の操作は続けられる
            print(f"手前のコマの先読みを止めました: {e}")
            self.thread = None           # 次に頼まれたら作り直す
            self.results.put((self.gen, None, None))
        finally:
            if src is not None:
                src.close()


class Decoder:
    """デコード用プロセスと、フレームを受け渡す共有メモリのスロットを管理する (画面側)。

    スロットは「空き」→ プロセスが書き込み → 画面側が表示 → 次のコマを表示したら空きに戻す。
    スロットの数だけデコードが先へ進める (先読みの貯め)。1 つの共有メモリを区切って使う。
    """

    def __init__(self, path, fmt, width, height):
        ctx = mp.get_context("spawn")
        shape = frame_size(fmt, width, height)
        nbytes = int(np.prod(shape))
        self.slots = slot_count(nbytes)
        self.shm = shared_memory.SharedMemory(create=True, size=nbytes * self.slots)
        self.pictures = [Picture(0.0, fmt, width, height,
                                 np.ndarray(shape, np.uint8, buffer=self.shm.buf, offset=i * nbytes))
                         for i in range(self.slots)]
        self.cmd_q, self.ready_q, self.free_q = ctx.Queue(), ctx.Queue(), ctx.Queue()
        for i in range(self.slots):
            self.free_q.put(i)
        self.clock = SharedClock(ctx)
        self.decoded = ctx.Value("q", 0, lock=False)
        self.sent = ctx.Value("q", 0, lock=False)     # デコード側が ready_q へ送った数
        self.got = 0                                  # 画面側が ready_q から受け取った数
        self.started = ctx.Event()     # デコードプロセスの準備ができた
        self.proc = ctx.Process(
            target=decode_worker, daemon=True,
            args=(path, fmt, self.shm.name, self.slots, self.cmd_q, self.ready_q, self.free_q,
                  self.clock.values, self.decoded, self.sent, self.started))
        self.proc.start()
        self.session = 0
        self.active = False      # 実行中のセッションがある (SESSION_END 未着)
        self.params = None       # 実行中のセッションの (開始秒, fps, ループ)

    def play(self, t, fps, loop=None):
        """t 秒からデコードを始める。表示の時計は最初のコマを表示したときに合わせる。"""
        self.stop()
        self.session += 1
        self.clock.reset()
        self.active = True
        self.params = (t, fps, loop)
        self.cmd_q.put(("play", self.session, t, fps, loop))

    def poll(self):
        """届いた (slot, t) を 1 つ返す。slot が負なら印 (END_OF_FILE / WRAP)。

        古いセッションの分はスロットを空きに戻して捨てる。
        """
        while True:
            try:
                session, slot, t = self.ready_q.get_nowait()
            except queue.Empty:
                return None
            self.got += 1
            if slot == SESSION_END:
                if session == self.session:
                    self.active = False
                continue
            if session != self.session:
                if slot >= 0:
                    self.release(slot)
                continue
            return slot, t

    def buffered(self):
        """先読みして届いているコマ数 (おおよそ。折り返しなどの印も含む)。"""
        return max(0, self.sent.value - self.got)

    def release(self, slot):
        self.free_q.put(slot)

    def stop(self):
        """実行中のセッションを止め、届いていたフレームのスロットをすべて空きに戻す。"""
        self.params = None
        if not self.active:
            return
        self.cmd_q.put(("stop",))
        if not self.started.is_set():
            # まだ準備中なら、届いている命令を順に処理するだけで、このセッションは
            # 1 コマも作らずに終わる (始めた直後に停止の命令が見える)。待つ必要はない
            self.active = False
            return
        deadline = time.perf_counter() + 3
        while self.active and time.perf_counter() < deadline:
            try:
                session, slot, _ = self.ready_q.get(timeout=0.1)
            except queue.Empty:
                continue
            self.got += 1
            if slot >= 0:
                self.release(slot)
            elif slot == SESSION_END and session == self.session:
                self.active = False
        self.active = False

    def close(self):
        self.stop()
        self.cmd_q.put(("quit",))
        self.proc.join(2)
        if self.proc.is_alive():
            self.proc.terminate()
        self.pictures.clear()
        try:
            self.shm.close()
            self.shm.unlink()
        except (BufferError, FileNotFoundError):
            pass   # まだ参照が残っている場合はプロセス終了時に解放される
