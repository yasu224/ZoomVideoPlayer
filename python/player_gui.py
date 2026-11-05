"""Zoom Video Player (Python 版) のメインウィンドウ: 操作パネル、再生の進行、ループ、統計表示。

PySide6 で画面を作り、PyAV (FFmpeg) で動画をデコードする。モジュールの分担:
- player_core.py   デコード (PyAV)。再生中は別プロセスで動く
- player_frames.py 画面側のフレームと、デコードプロセスとの受け渡し
- player_view.py   表示 (拡大縮小・ミニマップ、CPU 描画 / GPU 描画)
- player_gui.py    このファイル

起動は zoom_player.py から行う。
"""
import queue
import sys
import time

from player_core import END_OF_FILE, WRAP, VideoSource, even_size, gpu_format
from player_frames import BackFiller, Decoder, FrameCache, make_picture
from player_view import CpuView, GpuWindow, gpu_wanted
from player_vsync import VsyncLock, VsyncTimer, vsync_lock_step
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QSlider, QStyle, QStyleOptionSlider, QVBoxLayout, QWidget,
)

COMMON_FPS = [23.976, 24, 25, 29.97, 30, 50, 59.94, 60, 119.88, 120, 144, 240]
SPEEDS = [0.1, 0.25, 0.5, 1, 1.5, 2, 4]
STYLE = """
    QMainWindow, QWidget { background:#111317; color:#e6e8eb; }
    QWidget#bar { background:#1b1e24; }
    QLabel#dim { color:#9aa1ab; }
    QLabel#time { color:#9aa1ab; font-family:Consolas,Menlo,monospace; }
    QPushButton, QComboBox { background:#262a32; border:1px solid #2c313a; border-radius:5px; padding:4px 10px; }
    QPushButton:hover, QComboBox:hover { border-color:#46505e; }
    QPushButton:checked { background:#1f4a7a; border-color:#4da3ff; }
    QPushButton:disabled { color:#5c626b; }
    QComboBox QAbstractItemView { background:#262a32; selection-background-color:#1f4a7a; }
"""
BACK_READY = 48   # 停止中に、表示しているコマの手前に用意しておくコマ数 (キャッシュの 2/3 まで)

HELP_TEXT = """\
左ドラッグ\t範囲を選んで拡大
ホイール / ピンチ\tカーソル位置を中心に拡大 / 縮小
右ドラッグ / Ctrl+ドラッグ\t拡大中の表示を移動
ダブルクリック / Esc / 0\t全体表示に戻す
Backspace\t1つ前の拡大状態に戻る
+ / -\t中央を基準に拡大 / 縮小
ミニマップをクリック\tその位置へ移動

Space / K\t再生 / 一時停止
← / →\t5秒戻る / 進む
, / .\t1フレーム戻る / 進む
Home / End\t先頭 / 末尾のフレーム
L\tループ再生 ON / OFF
[ / ]\t区間ループの開始点 A / 終了点 B
\\\t区間ループ解除
F\tフルスクリーン

※ 音声は再生しません (映像の確認用)。"""
if sys.platform == "darwin":
    HELP_TEXT = HELP_TEXT.replace("Ctrl+ドラッグ", "⌘+ドラッグ (control+ドラッグ)")


def fmt_time(t):
    t = max(0.0, t or 0.0)
    h, m, s = int(t // 3600), int(t // 60) % 60, t % 60
    head = f"{h}:{m:02d}" if h else f"{m:02d}"
    return f"{head}:{s:06.3f}"


def fmt_fps(f):
    return f"{f:.3f}".rstrip("0").rstrip(".")


class SeekSlider(QSlider):
    """シークバー。A-B 区間を黄色で重ねて描く。"""

    def __init__(self):
        super().__init__(Qt.Horizontal)
        self.ab = None        # (開始値, 終了値, 有効か)

    def paintEvent(self, e):
        super().paintEvent(e)
        if not self.ab or self.maximum() <= self.minimum():
            return
        a, b, active = self.ab
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
        span = groove.width() - handle.width()
        x = lambda v: groove.left() + handle.width() / 2 + QStyle.sliderPositionFromValue(
            self.minimum(), self.maximum(), v, span)
        p = QPainter(self)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 196, 0, 150 if active else 60))
        p.drawRoundedRect(QRectF(x(a), groove.center().y() - 3, max(2, x(b) - x(a)), 6), 3, 3)


def raise_ui_thread_priority():
    """画面側のスレッド (いま呼んだスレッド) の優先度を上げる。

    再生中はデコード用プロセスが CPU を大きく使う (240fps の 1080p ならほぼ全コア)。
    画面側は書き換えのたびに数ミリ秒だけ動けばよいが、その時刻に CPU をもらえないと
    書き換えを取りこぼす。プロセスごと優先度を変えるとデコードが他のアプリに負けて
    先読みが空になるので、画面のスレッドだけを上げる。
    """
    import ctypes
    if sys.platform == "darwin":
        # macOS: スレッドの QoS を「ユーザー操作中」(いちばん高い) にする
        try:
            libc = ctypes.CDLL(None)          # 起動中のプロセス (libSystem を含む) から探す
            QOS_CLASS_USER_INTERACTIVE = 0x21
            libc.pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0)
        except (OSError, AttributeError):
            pass
        return
    if sys.platform != "win32":
        return
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentThread.restype = wintypes.HANDLE
    kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    THREAD_PRIORITY_HIGHEST = 2
    kernel32.SetThreadPriority(kernel32.GetCurrentThread(), THREAD_PRIORITY_HIGHEST)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        raise_ui_thread_priority()
        self.setWindowTitle("Zoom Video Player")
        self.resize(1280, 800)
        self.setAcceptDrops(True)

        self.src = None          # 停止中のシーク・コマ送り用 (画面側プロセス)
        self.decoder = None      # 再生用のデコードプロセス
        self.cur_frame = None    # 停止中に表示しているフレーム
        self.cur_slot = None     # 再生中に表示している共有メモリのスロット
        self.cur_t = 0.0
        self.playing = False
        self.pending = None
        self.loop_on = False
        self.point_a = None
        self.point_b = None
        self.was_playing = False
        self.decoded_mark = 0
        self.time_shown_at = 0.0
        self.stats_t0 = time.perf_counter()
        self.hz = 60.0               # 画面の書き換え回数 (統計の更新ごとに読み直す)
        self.path = None
        self.cache = FrameCache()    # 停止中に見たコマ (戻るコマ送りを速くする)
        self.scrub_value = None      # シークバーをドラッグ中の、まだ表示していない位置

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.setInterval(2)
        self.timer.timeout.connect(self.on_timer)
        self.last_swap = 0.0
        self.vsync = VsyncTimer()     # 書き換えの時刻 (揺れを除いたもの)
        self.swap_wall = None         # 直前の書き換えの推定時刻。再生中のコマはこの時刻を基準に選ぶ
        self.lock = None              # 動画と画面の速さが同じとき、書き換えに合わせて進める (VsyncLock)
        self.starved_at = 0.0         # デコードが追いつかず、時計を遅らせた最後の時刻
        self.arm_timer = QTimer(self)           # 停止中の先読みを始めるまでの待ち
        self.arm_timer.setSingleShot(True)
        self.arm_timer.setInterval(150)
        self.arm_timer.timeout.connect(self.arm)
        self.backfill = None                    # 停止中に手前のコマをデコードしておく裏のスレッド
        self.backfill_last = None               # 最後に頼んだ範囲 (同じ範囲を頼み続けないため)
        self.backfill_timer = QTimer(self)      # その結果をキャッシュへ取り込む
        self.backfill_timer.setInterval(10)
        self.backfill_timer.timeout.connect(self.take_backfill)
        self.backfill_soon = QTimer(self)       # コマ送りやシークが落ち着いたら始める
        self.backfill_soon.setSingleShot(True)
        self.backfill_soon.setInterval(30)
        self.backfill_soon.timeout.connect(self.start_backfill)
        # 見た目は部品を作る前に設定する (後から設定すると全部品を作り直すため 0.6 秒ほどかかる)
        self.setStyleSheet(STYLE)
        self.make_view(gpu=gpu_wanted())
        self._build_ui()
        self.stats_timer = QTimer(self)
        self.stats_timer.setInterval(500)
        self.stats_timer.timeout.connect(self.update_stats)
        self.stats_timer.start()
        self.set_enabled(False)

    # ---------- view ----------
    def make_view(self, gpu):
        """表示部分を作る。GPU 描画は専用ウィンドウを埋め込み、CPU 描画は普通のウィジェット。"""
        if gpu:
            self.view = GpuWindow()
            self.view.key_handler = self.keyPressEvent
            self.view.drop_handler = self.dropEvent
            self.view.on_failure = self.fall_back_to_cpu
            self.view.next_picture = self.next_picture
            self.view.frameSwapped.connect(self.on_swapped)
            self.view_widget = QWidget.createWindowContainer(self.view)
            self.view_widget.setMinimumSize(320, 180)
            self.view_widget.setFocusPolicy(Qt.StrongFocus)
            # OpenGL の文脈が作れないと initializeGL も呼ばれないので、少し待って確かめる
            QTimer.singleShot(4000, self.check_gpu_started)
        else:
            self.view = self.view_widget = CpuView()
        if hasattr(self, "pixel_check"):     # CPU 描画へ切り替えたときは表示の設定を引き継ぐ
            self.view.pixelated = self.pixel_check.isChecked()
            self.view.show_minimap = self.mini_check.isChecked()
        self.pix_fmt = self.view.pixel_format

    def check_gpu_started(self):
        if isinstance(self.view, GpuWindow) and not self.view.initialized and self.view.isExposed():
            self.view.fail("OpenGL を初期化できませんでした")

    def fall_back_to_cpu(self, reason):
        """GPU 描画が使えないので CPU 描画に切り替え、開いていた動画を開き直す。"""
        if not isinstance(self.view, GpuWindow):
            return
        print(f"GPU 描画を使えないため CPU 描画に切り替えます: {reason}")
        self.pause()
        old = self.view_widget
        index = self.root_layout.indexOf(old)
        self.make_view(gpu=False)
        self.root_layout.insertWidget(index, self.view_widget, 1)
        old.deleteLater()
        if self.path:
            t = self.cur_t
            self.load(self.path)
            self.seek_paused(t)

    # ---------- UI ----------
    def _button(self, text, slot, tip=None):
        b = QPushButton(text)
        b.setFocusPolicy(Qt.NoFocus)
        b.clicked.connect(slot)
        if tip:
            b.setToolTip(tip)
        return b

    def _build_ui(self):
        top = QHBoxLayout()
        top.addWidget(self._button("動画を開く", self.open_dialog))
        self.name_label = QLabel("")
        self.name_label.setObjectName("dim")
        top.addWidget(self.name_label, 1)
        self.pixel_check = QCheckBox("ピクセル表示")
        self.pixel_check.setFocusPolicy(Qt.NoFocus)
        self.pixel_check.toggled.connect(self.set_pixelated)
        top.addWidget(self.pixel_check)
        self.mini_check = QCheckBox("ミニマップ")
        self.mini_check.setChecked(True)
        self.mini_check.setFocusPolicy(Qt.NoFocus)
        self.mini_check.toggled.connect(self.set_minimap)
        top.addWidget(self.mini_check)
        top.addWidget(self._button("操作方法", self.show_help))

        row1 = QHBoxLayout()
        self.play_btn = self._button("▶ 再生", self.toggle_play, "再生 / 一時停止 (Space)")
        self.prev_btn = self._button("◀|", lambda: self.step(-1), "1フレーム戻る (,)")
        self.next_btn = self._button("|▶", lambda: self.step(1), "1フレーム進む (.)")
        self.slider = SeekSlider()
        self.slider.setFocusPolicy(Qt.NoFocus)
        self.slider.sliderPressed.connect(self.slider_pressed)
        self.slider.valueChanged.connect(self.slider_changed)
        self.slider.sliderReleased.connect(self.slider_released)
        self.time_label = QLabel(fmt_time(0) + " / " + fmt_time(0))
        self.time_label.setObjectName("time")
        for w in (self.play_btn, self.prev_btn, self.next_btn):
            row1.addWidget(w)
        row1.addWidget(self.slider, 1)
        row1.addWidget(self.time_label)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("再生fps"))
        # 標準は動画の fps のまま。値を選ぶと、動画の fps ではなくその fps で 1 コマずつ再生する
        # (59.94fps と記録された 120fps 撮影の映像を 120 で見るときなど)。コマ番号は動画の fps のまま
        self.fps_combo = QComboBox()
        self.fps_combo.setFocusPolicy(Qt.NoFocus)
        self.fps_combo.setToolTip("標準は動画の fps。値を選ぶと、その fps で全コマを再生する (強制)")
        self.fps_combo.addItem("動画のまま", None)
        for f in COMMON_FPS:
            self.fps_combo.addItem(f"{fmt_fps(f)} で強制", f)
        self.fps_combo.currentIndexChanged.connect(self.play_fps_changed)
        row2.addWidget(self.fps_combo)
        self.loop_btn = self._button("ループ", self.toggle_loop, "ループ再生 (L)")
        self.loop_btn.setCheckable(True)
        self.a_btn = self._button("A", lambda: self.set_point("A"), "区間ループの開始点 ([)")
        self.b_btn = self._button("B", lambda: self.set_point("B"), "区間ループの終了点 (])")
        self.clear_btn = self._button("A-B解除", self.clear_ab, "区間ループ解除 (\\)")
        for w in (self.loop_btn, self.a_btn, self.b_btn, self.clear_btn):
            row2.addWidget(w)
        row2.addSpacing(12)
        row2.addWidget(QLabel("速度"))
        self.speed_combo = QComboBox()
        self.speed_combo.setFocusPolicy(Qt.NoFocus)
        for sp in SPEEDS:
            self.speed_combo.addItem(f"{sp}x", sp)
        self.speed_combo.setCurrentIndex(SPEEDS.index(1))
        self.speed_combo.currentIndexChanged.connect(self.speed_changed)
        row2.addWidget(self.speed_combo)
        row2.addStretch(1)
        row2.addWidget(self._button("-", lambda: self.view.zoom_center(1 / 1.5), "縮小 (-)"))
        row2.addWidget(self._button("+", lambda: self.view.zoom_center(1.5), "拡大 (+)"))
        row2.addWidget(self._button("全体表示", lambda: self.view.reset(), "全体表示 (Esc)"))
        row2.addWidget(self._button("全画面", self.toggle_fullscreen, "フルスクリーン (F)"))

        bars = []
        for layout in (top, row1, row2):
            bar = QWidget()
            bar.setObjectName("bar")
            layout.setContentsMargins(10, 5, 10, 5)
            bar.setLayout(layout)
            bars.append(bar)
        self.bars = bars

        root = self.root_layout = QVBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(bars[0])
        root.addWidget(self.view_widget, 1)
        root.addWidget(bars[1])
        root.addWidget(bars[2])
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

    def set_enabled(self, on):
        for w in (self.play_btn, self.prev_btn, self.next_btn, self.slider, self.a_btn, self.b_btn):
            w.setEnabled(on)
        self.clear_btn.setEnabled(on and (self.point_a is not None or self.point_b is not None))

    def show_help(self):
        QMessageBox.information(self, "操作方法", HELP_TEXT)

    def set_pixelated(self, on):
        self.view.pixelated = on
        self.view.update()

    def set_minimap(self, on):
        self.view.show_minimap = on
        self.view.update()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            for b in self.bars:
                b.show()
        else:
            for b in self.bars:
                b.hide()
            self.showFullScreen()

    # ---------- file ----------
    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "動画を開く", "",
            "動画 (*.mp4 *.mov *.mkv *.avi *.webm *.mxf *.m4v *.mts *.m2ts *.ts *.wmv);;すべてのファイル (*)")
        if path:
            self.load(path)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = [u for u in e.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.load(urls[0].toLocalFile())

    def load(self, path):
        self.pause()
        try:
            src = VideoSource(path)
            first = src.frame_at(0.0, src.fps)
            if first is None:
                raise ValueError("フレームを読み込めません")
        except Exception as e:
            QMessageBox.warning(self, "開けません", f"この動画は開けませんでした。\n{path}\n\n{e}")
            return
        if self.src:
            self.src.close()
        if self.decoder:
            self.decoder.close()
        self.close_backfill()
        self.src = src
        self.path = path
        self.cache.clear()
        # GPU 描画はデコード結果の形式のまま (10bit / 4:2:2 も) 受け渡す。CPU 描画は BGRA
        self.pix_fmt = gpu_format(src.pix_fmt) if self.view.pixel_format == "yuv" else "bgra"
        self.frame_w, self.frame_h = even_size(self.pix_fmt, src.width, src.height)
        self.decoder = Decoder(path, self.pix_fmt, self.frame_w, self.frame_h)
        self.backfill = BackFiller(path, self.pix_fmt, self.frame_w, self.frame_h)
        if isinstance(self.view, GpuWindow):
            self.view.set_color(src.bt709, src.full_range)
        self.point_a = self.point_b = None
        self.fps_combo.setItemText(0, f"動画のまま ({fmt_fps(src.fps)})")
        self.name_label.setText(f"{path}   {src.width}×{src.height}  {fmt_fps(src.fps)} fps")
        self.setWindowTitle(f"Zoom Video Player - {path}")
        self.set_slider_range()
        self.show_frame(first)
        self.set_enabled(True)
        self.update_loop_ui()

    # ---------- frame helpers ----------
    def fps(self):
        """動画の fps (コマ番号・シーク・コマ送りはこれで数える)。"""
        return self.src.fps if self.src else 30.0

    def play_fps(self):
        """再生する fps。標準は動画の fps、「○ で強制」を選んだらその fps。"""
        return self.fps_combo.currentData() or self.fps()

    def frame_count(self):
        return max(1, round(self.src.duration * self.fps())) if self.src else 1

    def frame_index(self, t=None):
        return round((self.cur_t if t is None else t) * self.fps())

    def set_slider_range(self):
        self.slider.blockSignals(True)
        self.slider.setRange(0, self.frame_count() - 1)
        self.slider.blockSignals(False)

    def show_frame(self, av_frame):
        """停止中に、画面側でデコードしたフレームを表示する (覚えておいて次からは使い回す)。"""
        picture = self.to_picture(av_frame)
        self.cache.put(self.frame_index(picture.t), picture)
        self.show_picture(picture)

    def to_picture(self, av_frame):
        return make_picture(av_frame, self.src.frame_time(av_frame), self.pix_fmt, self.frame_w, self.frame_h)

    def show_picture(self, picture):
        self.cur_frame = picture
        self.cur_t = picture.t
        self.cache.focus = self.frame_index()
        self.view.set_picture(picture)
        self.update_time()
        self.arm_soon()
        self.backfill_last = None
        if self.backfill:
            self.backfill_soon.start()

    def update_time(self):
        if not self.src:
            return
        # 再生中の文字とシークバーの更新は 30 回/秒で十分 (毎コマ更新すると描画の邪魔になる)
        now = time.perf_counter()
        if self.playing and now - self.time_shown_at < 1 / 30:
            return
        self.time_shown_at = now
        idx = self.frame_index()
        self.time_label.setText(
            f"{fmt_time(self.cur_t)} / {fmt_time(self.src.duration)}   #{idx} / {self.frame_count() - 1}")
        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(idx)
            self.slider.blockSignals(False)

    def seek_paused(self, t):
        """停止中に t 秒のフレームを表示する。"""
        t = min(max(0.0, t), max(0.0, self.src.duration - 0.5 / self.fps()))
        cached = self.cache.get(self.frame_index(t))
        if cached is not None:
            return self.show_picture(cached)
        frame = self.src.frame_at(t, self.fps())
        if frame is not None:
            self.show_frame(frame)

    def step(self, n):
        if not self.src:
            return
        self.pause()
        idx = max(0, min(self.frame_index() + n, self.frame_count() - 1))
        cached = self.cache.get(idx)
        if cached is not None:
            return self.show_picture(cached)
        # 1 コマ進むだけなら、デコーダの続きを読む方が速い
        if n == 1 and self.src.last_t is not None and abs(self.src.last_t - self.cur_t) < 0.25 / self.fps():
            frame = self.src.next()
            if frame is not None:
                self.show_frame(frame)
            return
        if n < 0:
            return self.step_back_to(idx)
        self.seek_paused(idx / self.fps())

    # ---------- 手前のコマの先読み ----------
    # 停止中は、表示しているコマの手前 BACK_READY コマを裏のスレッドでデコードしてキャッシュに
    # 入れておく。戻るコマ送りで残りが 2/3 を切ったら、その手前を続けて用意する
    # (キーを押し続けて毎秒 30 コマ戻っても、10bit 4:2:2 の MXF で追いつく余裕を残す)。
    def step_back_to(self, idx):
        """idx のコマを表示する。用意が間に合っていなければ、裏のスレッドがそこまで作るのを待つ。"""
        self.take_backfill()
        if idx not in self.cache.items and not self.backfill.covers(idx):
            self.backfill.request(idx, idx, self.fps())   # キーフレームから idx まで (その間のコマも残る)
            self.backfill_timer.start()
        deadline = time.perf_counter() + 5
        while idx not in self.cache.items and self.backfill.covers(idx) and time.perf_counter() < deadline:
            self.take_backfill(wait=0.5)
        picture = self.cache.get(idx)
        if picture is None:
            return self.seek_paused(idx / self.fps())
        self.show_picture(picture)

    def take_backfill(self, wait=None):
        """裏のスレッドが作ったコマをキャッシュへ入れる。wait 秒まで最初の 1 つを待てる。"""
        if not self.backfill:
            return
        results = self.backfill.results
        while True:
            try:
                gen, i, picture = results.get(timeout=wait) if wait else results.get_nowait()
            except queue.Empty:
                break
            wait = None
            if i is None:
                self.backfill.finished(gen)
            else:
                self.cache.put(self.frame_index(picture.t), picture)
        if self.backfill.busy is None:
            self.backfill_timer.stop()
            if not self.backfill_soon.isActive():
                self.backfill_soon.start()

    def start_backfill(self):
        if self.playing or not self.backfill or not self.cur_frame or self.slider.isSliderDown():
            return
        cur = self.frame_index()
        want = min(BACK_READY, self.cache.capacity(self.cur_frame.data.nbytes) * 2 // 3, cur)
        busy = self.backfill.busy
        if busy is not None:
            if busy[1] >= cur - want and busy[0] < cur:
                return                     # いま作っている範囲がちょうど要る所
            self.backfill.cancel()         # シークなどで要らなくなった
        ready = self.cache.ready_below(cur)
        if want <= 0 or ready >= want or ready > want * 2 // 3:
            return
        job = (cur - want, cur - ready - 1, self.fps())
        if job == self.backfill_last:
            return                         # 頼んでも埋まらなかった (fps の指定で番号が飛ぶなど)
        self.backfill_last = job
        self.backfill.request(*job)
        self.backfill_timer.start()

    def close_backfill(self):
        self.backfill_soon.stop()
        self.backfill_timer.stop()
        if self.backfill:
            self.backfill.close()
            self.backfill = None

    def skip(self, sec):
        if self.src:
            self.seek_to(self.cur_t + sec)

    def seek_to(self, t):
        if self.playing:
            self.start_playback(min(max(0.0, t), self.src.duration))
        else:
            self.seek_paused(t)

    # ---------- seek bar ----------
    # ドラッグ中は位置が細かく何度も届くので、最後の位置だけを、近いキーフレームで素早く表示する。
    # 離したら正確なコマを表示する。溝をクリックしたときはそのまま正確に移動する。
    def slider_pressed(self):
        self.was_playing = self.playing
        self.pause()

    def slider_changed(self, value):
        if not self.src:
            return
        if not self.slider.isSliderDown():
            return self.seek_to(value / self.fps())
        if self.scrub_value is None:
            QTimer.singleShot(0, self.scrub)
        self.scrub_value = value

    def scrub(self):
        value, self.scrub_value = self.scrub_value, None
        if value is None or not self.slider.isSliderDown():
            return
        cached = self.cache.get(value)
        if cached is not None:
            return self.show_picture(cached)
        self.src.seek(value / self.fps())
        frame = self.src.next()
        if frame is not None:
            self.show_picture(self.to_picture(frame))

    def slider_released(self):
        self.scrub_value = None
        self.seek_paused(self.slider.value() / self.fps())
        if self.was_playing:
            self.play()

    # ---------- playback ----------
    def toggle_play(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def play_start(self):
        """再生ボタンで始める位置。末尾にいるときや A-B 区間の外にいるときは区間の頭から。"""
        start = self.cur_t
        loop = self.loop_range()
        half = 0.5 / self.fps()
        if loop and loop[1] is not None and not (loop[0] - half <= start <= loop[1] + half):
            return loop[0]
        if start >= self.src.duration - 1.5 / self.fps():
            return self.loop_start()
        return start

    def play(self):
        if not self.src or self.playing:
            return
        self.start_playback(self.play_start())

    def start_playback(self, t):
        self.pause()
        self.arm_timer.stop()
        self.backfill_soon.stop()
        if self.backfill:
            self.backfill.cancel()      # 再生中はデコードプロセスに CPU を譲る
        self.playing = True
        self.play_btn.setText("一時停止")
        self.pending = None
        self.vsync.reset()
        self.swap_wall = None
        self.lock = None
        self.update_lock()
        params = (t, self.fps(), self.loop_range())
        if self.decoder.params != params:     # 停止中に先読みしてあれば、それをそのまま使う
            self.decoder.play(*params)
        self.update_display_period()
        self.decoded_mark = self.decoder.decoded.value
        self.timer.start()

    def pause(self):
        if not self.playing:
            return
        self.playing = False
        self.timer.stop()
        if self.pending and self.pending[0] >= 0:
            self.decoder.release(self.pending[0])
        self.pending = None
        self.decoder.stop()
        if self.cur_slot is not None:
            # 表示中のスロットは返すので、画像を手元にコピーしておく
            self.cur_frame = self.decoder.pictures[self.cur_slot].copy(self.cur_t)
            self.cache.focus = self.frame_index()
            self.cache.put(self.frame_index(), self.cur_frame)
            self.decoder.release(self.cur_slot)
            self.cur_slot = None
            self.view.set_picture(self.cur_frame)
        self.update_time()
        self.play_btn.setText("▶ 再生")
        self.update_stats()
        self.arm_soon()
        self.backfill_last = None
        if self.backfill:
            self.backfill_soon.start()

    # 停止中は、少し落ち着いたら (コマ送りやシークが続いている間は待つ) 今の位置から
    # デコードプロセスに先読みさせておく。再生ボタンを押すと貯めてあるコマからすぐ始まる。
    def arm_soon(self):
        if self.decoder and not self.playing:
            self.arm_timer.start()

    def arm(self):
        if self.playing or not self.decoder:
            return
        params = (self.play_start(), self.fps(), self.loop_range())
        if self.decoder.params != params:
            self.decoder.play(*params)

    def speed(self):
        """再生の速さ (動画内の秒 / 実時間の秒)。速度の指定に、強制 fps の分 (再生 fps / 動画 fps) を掛ける。"""
        return self.speed_combo.currentData() * self.play_fps() / self.fps()

    def refresh_rate(self):
        screen = self.windowHandle().screen() if self.windowHandle() else None
        return screen.refreshRate() if screen else 60.0

    def lookahead(self):
        """いま選んだコマが画面に出るまでの時間に、半コマの余裕を足したもの (動画内の秒)。

        GPU 描画では、選んだコマは次の書き換え (書き換え 1 回分あと) に出る。さらに半コマ足して
        「その時に表示されているべきコマ」を四捨五入で選ぶ (切り替わりちょうどで比べると、
        わずかな揺れでコマが重複したり飛んだりする)。動画と画面の速さが同じときは
        書き換えに合わせて進めるので (VsyncLock)、選ぶ時刻はいつもコマの真ん中になる。
        """
        if not isinstance(self.view, GpuWindow):
            return 0.0
        per_swap = self.lock.step if self.lock else self.speed() / self.hz
        return per_swap + 0.5 / self.fps()

    # GPU 描画では、コマを選ぶのは画面の書き換えの直後だけにする。基準の時刻は、書き換えの知らせの
    # 届いた時刻から揺れを除いた推定 (VsyncTimer)。再生中は毎回描き直して、この流れを途切れさせない。
    # タイマーは、書き換えが止まっているときに描き直しを頼むためだけに使う。
    def on_swapped(self):
        self.last_swap = time.perf_counter()
        if self.playing:
            self.swap_wall = self.vsync.update(self.last_swap, 1 / self.hz)
            clock = self.decoder.clock
            lock = self.lock
            if lock and lock.t is not None and clock.started:
                # 画面に合わせる: 書き換え 1 回分ちょうど進め、共有の時計 (デコード側も見る) もそこへ揃える
                clock.set(lock.advance(self.swap_wall), self.swap_wall, self.speed())
            self.tick()
            if clock.started:
                t = clock.at(self.swap_wall)
                if lock and t != lock.t:          # 最初のコマ・ループの折り返しで時計が合わせ直された
                    lock.rebase(self.swap_wall, t)
                # デコード側の間引きに、次の書き換えの時刻を知らせる
                clock.set_next_vsync(t + self.lookahead())
            self.view.render_soon()

    def on_timer(self):
        if not isinstance(self.view, GpuWindow):
            return self.tick()
        if self.playing and time.perf_counter() - self.last_swap > 0.05:
            self.view.render_soon()

    def update_lock(self):
        """動画と画面の速さがほぼ同じ (か整数倍) なら、画面の書き換えに合わせて進める (vsync_lock_step)。"""
        gpu = isinstance(self.view, GpuWindow)
        step = vsync_lock_step(self.fps(), self.speed(), self.hz) if gpu else None
        if step is None:
            self.lock = None
        elif not self.lock or (self.lock.step, self.lock.speed) != (step, self.speed()):
            self.lock = VsyncLock(step, self.speed())     # 次の書き換えで今の時刻に合わせて始める
        self.update_display_period()

    def update_display_period(self):
        """デコード側に、画面の書き換え 1 回で進む動画内の秒を知らせる (出せないコマを間引く)。"""
        if self.decoder:
            self.decoder.clock.set_display_period(self.lock.step if self.lock else self.speed() / self.hz)

    def restart_clock(self, t):
        """再生の時計を t から始める。GPU 描画では直前の書き換えの時刻を起点にする。"""
        clock = self.decoder.clock
        if self.swap_wall is not None:
            clock.set(t, self.swap_wall, self.speed())
        else:
            clock.start(t, self.speed())

    def speed_changed(self):
        if self.playing and self.decoder.clock.started:
            self.decoder.clock.start(self.decoder.clock.now(), self.speed())
        self.update_lock()

    def play_fps_changed(self):
        """再生 fps を変えた。コマ番号は変わらず、再生の速さだけが変わる。"""
        self.speed_changed()
        self.update_stats()

    def next_picture(self):
        """再生中に、次に表示する予定のコマ (届いていてまだ時刻になっていないもの)。

        GPU 描画はこれを裏のテクスチャへ先に送っておき、次の書き換えでは描くだけにする。
        """
        if not self.playing or self.pending is None or self.pending[0] < 0:
            return None
        slot, t = self.pending
        picture = self.decoder.pictures[slot]
        picture.t = t
        return picture

    def show_slot(self, slot, t):
        old, self.cur_slot = self.cur_slot, slot
        self.cur_t = t
        picture = self.decoder.pictures[slot]
        picture.t = t
        self.view.set_picture(picture)
        if old is not None:
            self.decoder.release(old)
        self.update_time()

    def pick_time(self):
        """コマを選ぶ基準の動画時刻。GPU 描画では直前の書き換えの推定時刻 (VsyncTimer) から求める。"""
        clock = self.decoder.clock
        return clock.at(self.swap_wall) if self.swap_wall is not None else clock.now()

    def tick(self):
        """表示時刻に達したフレームを受け取って表示する。間に合わなかったコマは捨てる。"""
        if not self.playing:
            return
        clock = self.decoder.clock
        latest = None
        # デコードが追いつかないとき (下の starved_at) は、コマを飛ばさず 1 回に 1 コマずつ出す
        starving = time.perf_counter() - self.starved_at < 0.5
        while True:
            if self.pending is None:
                self.pending = self.decoder.poll()
                if self.pending is None:
                    break
            slot, t = self.pending
            if slot == END_OF_FILE:
                self.pending = None
                if latest:
                    self.show_slot(*latest)
                if t:
                    self.pause()
                    QMessageBox.warning(self, "デコードエラー", t)
                    return
                return self.pause()
            if slot == WRAP:
                # ループの折り返し。t (B の先の終わり) に達したら、時計を A に戻して次の周へ
                if clock.started and self.pick_time() + self.lookahead() < t:
                    break
                over = self.pick_time() - t if clock.started else 0.0
                clock.next_lap()
                self.restart_clock(self.decoder.params[2][0] + max(0.0, over))
                self.pending = None
                continue
            if not clock.started:                 # 最初のフレームで時計を合わせる
                self.restart_clock(t)
            # 選んだコマが画面に出るのは次の書き換えなので、その分だけ先のコマまで選ぶ
            if t > self.pick_time() + self.lookahead():
                break
            if latest:
                if starving:
                    break
                self.decoder.release(latest[0])
            latest, self.pending = self.pending, None
        if latest:
            self.show_slot(*latest)
            # デコードが再生の速さに追いつかず (強制 fps を上げすぎたなど)、次のコマがまだ届いていないのに
            # 出したコマが 1 コマ以上遅れているなら、時計をこのコマに合わせ直す。時刻どおりに進めるには
            # コマを飛ばすしかないが、映像の確認では全コマ見えるほうがよいので、出せる速さで全コマ出す
            if self.pending is None and clock.started and self.pick_time() - latest[1] > 1 / self.fps():
                self.restart_clock(latest[1])
                self.starved_at = time.perf_counter()

    def forced_label(self):
        """再生 fps を強制しているときの表示。"""
        forced = self.fps_combo.currentData()
        return f" (動画 {fmt_fps(self.fps())} を {fmt_fps(forced)} で強制再生)" if forced else ""

    def sync_label(self):
        """動画と画面の速さが同じで、書き換えに合わせてコマを進めているときの表示。"""
        return " ・ 書き換えに合わせて進行" if self.lock else ""

    def update_stats(self):
        now = time.perf_counter()
        dt = now - self.stats_t0
        self.stats_t0 = now
        shown, self.view.painted = self.view.painted, 0
        if not self.src:
            self.view.stats_text = ""
            return
        hz = self.hz = self.refresh_rate()
        self.update_lock()                    # ウィンドウを別の画面へ動かしたときのため
        if not self.playing:
            self.view.stats_text = (f"一時停止中\n動画 {fmt_fps(self.fps())} fps{self.forced_label()}"
                                    f" ・ 画面 {round(hz)} Hz ・ {self.view.renderer}")
            self.view.stats_warn = False
        else:
            total = self.decoder.decoded.value
            decoded, self.decoded_mark = total - self.decoded_mark, total
            want = self.fps() * self.speed()
            shown_fps, decoded_fps = shown / dt, decoded / dt
            ahead = self.decoder.buffered() / min(want, hz)   # 貯めてあるのは画面に出すコマ
            # Qt の通常ウィンドウはモニターの垂直同期を知らせないので、描いたコマ数と
            # モニターの Hz から「目に見えるコマ数」の上限を判断する
            if time.perf_counter() - self.starved_at < 1.0:
                reason = "デコードが追いつかないため、出せる速さで全コマ表示 (スロー)"
            elif decoded_fps < want * 0.9 and ahead < 0.1:
                reason = "デコードが追いつかない"
            elif hz < want * 0.95:
                reason = f"画面 {round(hz)} Hz のため見えるのは最大 {round(hz)} コマ/秒"
            elif shown_fps < want * 0.9:
                reason = "描画が追いつかない"
            else:
                reason = "全コマ表示中"
            self.view.stats_text = (f"描画 {shown_fps:.1f} / {want:.1f} fps{self.forced_label()}"
                                    f" ・ 先読み {ahead:.2f} 秒\n"
                                    f"{reason} ・ デコード {decoded_fps:.0f} fps ・ 画面 {round(hz)} Hz"
                                    f" ・ {self.view.renderer}{self.sync_label()}")
            self.view.stats_warn = reason != "全コマ表示中"
        self.view.update()

    # ---------- loop / A-B ----------
    def loop_start(self):
        return self.point_a if self.point_a is not None else 0.0

    def loop_range(self):
        """デコード側に渡すループ: None (しない) か (A 秒, B 秒)。B が None なら終端まで。"""
        if not self.loop_on:
            return None
        return (self.loop_start(), self.point_b)

    def loop_changed(self):
        self.update_loop_ui()
        if self.playing:
            self.start_playback(self.cur_t)      # 新しい区間でデコードし直す
        else:
            self.arm_soon()

    def toggle_loop(self):
        self.loop_on = not self.loop_on
        self.loop_changed()

    def set_point(self, which):
        if not self.src:
            return
        if which == "A":
            self.point_a = self.cur_t
        else:
            self.point_b = self.cur_t
        if self.point_a is not None and self.point_b is not None and self.point_a > self.point_b:
            self.point_a, self.point_b = self.point_b, self.point_a
        self.loop_on = True
        self.loop_changed()

    def clear_ab(self):
        self.point_a = self.point_b = None
        self.loop_changed()

    def update_loop_ui(self):
        self.loop_btn.setChecked(self.loop_on)
        self.a_btn.setText("A" if self.point_a is None else f"A {fmt_time(self.point_a)}")
        self.b_btn.setText("B" if self.point_b is None else f"B {fmt_time(self.point_b)}")
        has_ab = self.point_a is not None or self.point_b is not None
        self.clear_btn.setEnabled(has_ab)
        if has_ab and self.src:
            a = self.frame_index(self.loop_start())
            b = self.frame_index(self.point_b) if self.point_b is not None else self.frame_count() - 1
            self.slider.ab = (a, b, self.loop_on)
        else:
            self.slider.ab = None
        self.slider.update()

    # ---------- keyboard ----------
    def keyPressEvent(self, e):
        k = e.key()
        actions = {
            Qt.Key_Space: self.toggle_play, Qt.Key_K: self.toggle_play,
            Qt.Key_Left: lambda: self.skip(-5), Qt.Key_Right: lambda: self.skip(5),
            Qt.Key_Comma: lambda: self.step(-1), Qt.Key_Period: lambda: self.step(1),
            Qt.Key_Home: lambda: self.src and self.seek_to(0),
            Qt.Key_End: lambda: self.src and (self.pause(), self.seek_paused(self.src.duration)),
            Qt.Key_Plus: lambda: self.view.zoom_center(1.5), Qt.Key_Equal: lambda: self.view.zoom_center(1.5),
            Qt.Key_Minus: lambda: self.view.zoom_center(1 / 1.5),
            Qt.Key_0: self.view.reset, Qt.Key_Backspace: self.view.undo,
            Qt.Key_L: self.toggle_loop,
            Qt.Key_BracketLeft: lambda: self.set_point("A"), Qt.Key_BracketRight: lambda: self.set_point("B"),
            Qt.Key_Backslash: self.clear_ab, Qt.Key_yen: self.clear_ab,   # 日本語キーボードの \ は ¥
            Qt.Key_F: self.toggle_fullscreen,
        }
        if k == Qt.Key_Escape:
            if self.isFullScreen():
                self.toggle_fullscreen()
            else:
                self.view.reset()
        elif k in actions:
            actions[k]()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        self.arm_timer.stop()
        self.pause()
        self.close_backfill()
        if self.src:
            self.src.close()
        if self.decoder:
            self.decoder.close()
        super().closeEvent(e)
