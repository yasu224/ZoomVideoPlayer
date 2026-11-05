"""動画の表示部分: 拡大縮小・パン・範囲選択・ミニマップ、CPU 描画と GPU (OpenGL) 描画。"""
import importlib
import math
import os

import numpy as np
from player_core import PLANAR, plane_views
from PySide6.QtCore import QCoreApplication, QEvent, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QSurfaceFormat
from PySide6.QtOpenGL import QOpenGLWindow
from PySide6.QtWidgets import QWidget

MAX_ZOOM = 64          # 全体表示に対する最大倍率
MINI_W = 220           # ミニマップの幅


def gpu_wanted():
    """GPU 描画を試すか。PyOpenGL が読み込めれば試す (実際に使えるかは GpuWindow が確かめる)。

    OpenGL の文脈を作って確かめると起動が 0.5 秒ほど遅くなるので、ここでは作らない。
    環境変数 ZVP_RENDERER=cpu で CPU 描画を強制できる。
    """
    if os.environ.get("ZVP_RENDERER", "").lower() == "cpu":
        return False
    try:
        importlib.import_module("OpenGL.GL")
        return True
    except Exception:
        return False


class ViewBase:
    """表示部分の共通処理: 拡大縮小・パン・範囲選択・ミニマップ・バッジ。

    実際の描画は CpuView (Qt の CPU 描画、ウィジェット) と GpuWindow (OpenGL、専用ウィンドウ) が行う。
    """

    renderer = ""

    def _init_view(self):
        self.picture = None
        self.painted = 0          # 新しいフレームを描いた回数 (表示 fps の計測用)
        self.serial = 0           # set_picture のたびに増やす (スロットは使い回すため)
        self._painted_serial = None
        self.s, self.tx, self.ty = 1.0, 0.0, 0.0   # 画面座標 = 画像座標 * s + (tx, ty)
        self.history = []
        self.drag = None
        self.sel = None
        self.pixelated = False
        self.show_minimap = True
        self.stats_text = ""
        self.stats_warn = False
        self.placeholder = "動画ファイルをここにドロップ\nまたは「動画を開く」から選択"

    def area(self):
        return QRectF(0, 0, self.width(), self.height())

    # ---------- view math ----------
    def has_image(self):
        return self.picture is not None and self.width() > 0 and self.height() > 0

    def fit_scale(self):
        return min(self.width() / self.picture.w, self.height() / self.picture.h)

    def zoom_level(self):
        return self.s / self.fit_scale() if self.has_image() else 1.0

    def clamp(self):
        if not self.has_image():
            return
        W, H = self.width(), self.height()
        s0 = self.fit_scale()
        self.s = min(max(self.s, s0), s0 * MAX_ZOOM)
        cw, ch = self.picture.w * self.s, self.picture.h * self.s
        self.tx = (W - cw) / 2 if cw <= W else min(0.0, max(W - cw, self.tx))
        self.ty = (H - ch) / 2 if ch <= H else min(0.0, max(H - ch, self.ty))

    def push_history(self):
        self.history.append((self.s, self.tx, self.ty))
        del self.history[:-50]

    def reset(self, record=True):
        if not self.has_image():
            return
        if record and self.zoom_level() > 1.001:
            self.push_history()
        self.s = self.fit_scale()
        self.clamp()
        self.update()

    def undo(self):
        if not self.history:
            return self.reset(record=False)
        self.s, self.tx, self.ty = self.history.pop()
        self.clamp()
        self.update()

    def zoom_at(self, factor, mx, my):
        if not self.has_image():
            return
        vx, vy = (mx - self.tx) / self.s, (my - self.ty) / self.s
        self.s *= factor
        self.clamp()
        self.tx, self.ty = mx - vx * self.s, my - vy * self.s
        self.clamp()
        self.update()

    def zoom_center(self, factor):
        self.zoom_at(factor, self.width() / 2, self.height() / 2)

    def zoom_to_rect(self, r):
        ax, ay = (r.left() - self.tx) / self.s, (r.top() - self.ty) / self.s
        rw, rh = r.width() / self.s, r.height() / self.s
        if rw <= 0 or rh <= 0:
            return
        self.push_history()
        self.s = min(self.width() / rw, self.height() / rh)
        self.clamp()
        self.tx = self.width() / 2 - (ax + rw / 2) * self.s
        self.ty = self.height() / 2 - (ay + rh / 2) * self.s
        self.clamp()
        self.update()

    def set_picture(self, picture):
        old = self.picture
        self.picture = picture
        self.serial += 1
        if old is None or (old.w, old.h) != (picture.w, picture.h):
            self.history.clear()
            self.reset(record=False)
        self.update()

    def image_rect(self):
        return QRectF(self.tx, self.ty, self.picture.w * self.s, self.picture.h * self.s)

    def count_painted(self):
        if self.serial != self._painted_serial:
            self._painted_serial = self.serial
            self.painted += 1

    # ---------- minimap ----------
    def minimap_rect(self):
        if not (self.has_image() and self.show_minimap and self.zoom_level() > 1.001):
            return None
        mh = MINI_W * self.picture.h / self.picture.w
        return QRectF(self.width() - MINI_W - 12, self.height() - mh - 12, MINI_W, mh)

    def center_on_minimap(self, pos, rect):
        k = self.picture.w / rect.width()
        cx, cy = (pos.x() - rect.left()) * k, (pos.y() - rect.top()) * k
        self.tx = self.width() / 2 - cx * self.s
        self.ty = self.height() / 2 - cy * self.s
        self.clamp()
        self.update()

    # ---------- mouse ----------
    def mousePressEvent(self, e):
        if not self.has_image():
            return
        pos = e.position()
        mini = self.minimap_rect()
        if mini and mini.contains(pos) and e.button() == Qt.LeftButton:
            self.drag = ("mini", mini)
            self.center_on_minimap(pos, mini)
            return
        pan = e.button() in (Qt.RightButton, Qt.MiddleButton) or (
            e.button() == Qt.LeftButton and e.modifiers() & (Qt.ControlModifier | Qt.MetaModifier))
        if pan:
            self.drag = ("pan", pos, self.tx, self.ty)
            self.setCursor(Qt.ClosedHandCursor)
        elif e.button() == Qt.LeftButton:
            self.drag = ("select", pos)

    def mouseMoveEvent(self, e):
        if not self.drag:
            return
        pos = e.position()
        kind = self.drag[0]
        if kind == "mini":
            self.center_on_minimap(pos, self.drag[1])
        elif kind == "pan":
            _, start, tx, ty = self.drag
            self.tx, self.ty = tx + pos.x() - start.x(), ty + pos.y() - start.y()
            self.clamp()
            self.update()
        else:
            self.sel = QRectF(self.drag[1], pos).normalized()
            self.update()

    def mouseReleaseEvent(self, e):
        if self.drag and self.drag[0] == "select" and self.sel is not None:
            if self.sel.width() > 8 and self.sel.height() > 8:
                self.zoom_to_rect(self.sel)
        self.drag = None
        self.sel = None
        self.unsetCursor()
        self.update()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.reset()

    def wheelEvent(self, e):
        if self.has_image():
            pos = e.position()
            self.zoom_at(math.exp(e.angleDelta().y() * 0.0015), pos.x(), pos.y())

    def pinch(self, e):
        """トラックパッドのピンチ (macOS) で拡大縮小する。扱ったら True。"""
        if e.type() != QEvent.NativeGesture or e.gestureType() != Qt.ZoomNativeGesture:
            return False
        if self.has_image():
            pos = e.position()
            self.zoom_at(max(0.2, 1.0 + e.value()), pos.x(), pos.y())
        return True

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.clamp()

    # ---------- overlays ----------
    def draw_placeholder(self, p):
        p.setPen(QColor("#9aa1ab"))
        p.drawText(self.area(), Qt.AlignCenter, self.placeholder)

    def draw_overlays(self, p, mini):
        """選択枠・倍率・統計・ミニマップの枠と影 (ミニマップの絵は各ビューが先に描く)。"""
        if self.sel is not None:
            p.setPen(QPen(QColor("#4da3ff"), 1, Qt.DashLine))
            p.setBrush(QColor(77, 163, 255, 38))
            p.drawRect(self.sel)

        self._badge(p, f"{self.zoom_level():.2f}x", left=True)
        if self.stats_text:
            self._badge(p, self.stats_text, left=False, warn=self.stats_warn)

        if mini:
            iw, ih = self.picture.w, self.picture.h
            k = mini.width() / iw
            vis = QRectF(max(0.0, -self.tx / self.s) * k, max(0.0, -self.ty / self.s) * k,
                         min(iw, self.width() / self.s) * k, min(ih, self.height() / self.s) * k)
            vis.translate(mini.topLeft())
            shade = QColor(0, 0, 0, 115)
            p.fillRect(QRectF(mini.left(), mini.top(), mini.width(), vis.top() - mini.top()), shade)
            p.fillRect(QRectF(mini.left(), vis.bottom(), mini.width(), mini.bottom() - vis.bottom()), shade)
            p.fillRect(QRectF(mini.left(), vis.top(), vis.left() - mini.left(), vis.height()), shade)
            p.fillRect(QRectF(vis.right(), vis.top(), mini.right() - vis.right(), vis.height()), shade)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#4da3ff"), 2))
            p.drawRect(vis.adjusted(1, 1, -1, -1))
            p.setPen(QPen(QColor("#555"), 1))
            p.drawRect(mini)

    def _badge(self, p, text, left, warn=False):
        fm = p.fontMetrics()
        lines = text.split("\n")
        w = max(fm.horizontalAdvance(line) for line in lines) + 16
        h = fm.height() * len(lines) + 8
        x = 12 if left else self.width() - w - 12
        box = QRectF(x, 12, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 155))
        p.drawRoundedRect(box, 4, 4)
        p.setPen(QColor("#ffb84d") if warn else QColor("#e6e8eb"))
        p.drawText(box.adjusted(8, 4, -8, -4), (Qt.AlignLeft if left else Qt.AlignRight) | Qt.AlignVCenter, text)


class CpuView(ViewBase, QWidget):
    """Qt の CPU 描画で表示する (OpenGL が使えない PC 向け)。フレームは BGRA。"""

    renderer = "CPU描画"
    pixel_format = "bgra"

    def __init__(self):
        super().__init__()
        self._init_view()
        self.setMinimumSize(320, 180)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

    def event(self, e):
        return True if self.pinch(e) else super().event(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.area(), Qt.black)
        if not self.has_image():
            return self.draw_placeholder(p)
        image = self.picture.image
        # 見えている範囲だけを切り出して描く (高倍率でも軽い)
        iw, ih = self.picture.w, self.picture.h
        x0 = max(0, math.floor(-self.tx / self.s))
        y0 = max(0, math.floor(-self.ty / self.s))
        x1 = min(iw, math.ceil((self.width() - self.tx) / self.s))
        y1 = min(ih, math.ceil((self.height() - self.ty) / self.s))
        src = QRectF(x0, y0, x1 - x0, y1 - y0)
        dst = QRectF(x0 * self.s + self.tx, y0 * self.s + self.ty, src.width() * self.s, src.height() * self.s)
        p.setRenderHint(QPainter.SmoothPixmapTransform, not self.pixelated)
        p.drawImage(dst, image, src)
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)
        self.count_painted()
        mini = self.minimap_rect()
        if mini:
            p.drawImage(mini, image)
        self.draw_overlays(p, mini)


VERTEX_SHADER = """
#version 120
attribute vec2 pos;
attribute vec2 uv;
varying vec2 v_uv;
void main() {
    gl_Position = vec4(pos, 0.0, 1.0);
    v_uv = uv;
}
"""

FRAGMENT_SHADER = """
#version 120
uniform sampler2D tex_y;
uniform sampler2D tex_u;
uniform sampler2D tex_v;
uniform mat3 yuv_to_rgb;
uniform vec3 offset;
uniform float scale;      // 16bit テクスチャに入った 10/12bit の値を 0〜1 に直す倍率 (8bit は 1)
varying vec2 v_uv;
void main() {
    vec3 yuv = vec3(texture2D(tex_y, v_uv).r, texture2D(tex_u, v_uv).r, texture2D(tex_v, v_uv).r) * scale - offset;
    gl_FragColor = vec4(clamp(yuv_to_rgb * yuv, 0.0, 1.0), 1.0);
}
"""


def yuv_matrix(bt709, full_range, bits=8):
    """YUV → RGB の係数 (列ごと = Y, U, V の寄与、OpenGL の列優先) とオフセット。

    値は「bits ビットの最大値を 1」とした 0〜1 で受け取る (シェーダーで scale を掛けた後)。
    """
    if bt709:
        cols = [(1, 1, 1), (0, -0.1873, 1.8556), (1.5748, -0.4681, 0)]
    else:
        cols = [(1, 1, 1), (0, -0.3441, 1.7720), (1.4020, -0.7141, 0)]
    top = (1 << bits) - 1
    unit = 1 << (bits - 8)          # 8bit の 1 段階が何段階か (10bit なら 4)
    if full_range:
        ys, cs, offset = 1.0, 1.0, (0.0, 128 * unit / top, 128 * unit / top)
    else:   # 映像用の狭い範囲 (8bit で Y 16-235, UV 16-240)
        ys, cs = top / (219 * unit), top / (224 * unit)
        offset = (16 * unit / top, 128 * unit / top, 128 * unit / top)
    m = [c * ys for c in cols[0]] + [c * cs for c in cols[1]] + [c * cs for c in cols[2]]
    return np.array(m, np.float32), offset


class TextureSet:
    """Y / U / V の 3 枚のテクスチャと、いま入っているコマ (key) と形 (layout)。"""

    def __init__(self, textures):
        self.textures = textures
        self.layout = None
        self.key = None


def picture_key(pic):
    """どのコマかを表す印。共有メモリのスロットは使い回すので、配列と時刻の組で見分ける。

    同じスロットに同じ時刻のコマが入るのはループで同じコマに戻ったときだけで、中身も同じ。
    """
    return (id(pic.data), pic.t)


class GpuWindow(ViewBase, QOpenGLWindow):
    """OpenGL で表示する。YUV の 3 プレーンをテクスチャにして、色変換と拡大縮小を GPU で行う。

    ウィジェット (QOpenGLWidget) ではなく専用のウィンドウにしているのは、ウィジェットだと
    描いた絵を Qt がメインウィンドウ全体に合成し直す手間がかかり、120fps に届かないため。
    このウィンドウは画面へ直接書き換える。メインウィンドウには createWindowContainer で埋め込む。
    キー入力とファイルのドロップはこのウィンドウに届くので、key_handler / drop_handler へ渡す。
    """

    renderer = "GPU描画"
    pixel_format = "yuv"        # デコード結果の YUV をそのまま受け取る (player_core.gpu_format)
    key_handler = None
    drop_handler = None
    on_failure = None     # GPU 描画が使えないと分かったときに呼ぶ (理由の文字列を渡す)
    initialized = False
    failed = None

    def __init__(self):
        super().__init__()
        # 表と裏の入れ替えは画面の書き換え (垂直同期) 1 回ごとにする。Qt の初期値も 1 だが、
        # コマの選び方 (frameSwapped の直後に次のコマを選ぶ) がこれを前提にしているので明示する
        fmt = QSurfaceFormat.defaultFormat()
        fmt.setSwapBehavior(QSurfaceFormat.DoubleBuffer)
        fmt.setSwapInterval(1)
        self.setFormat(fmt)
        self._init_view()
        self.bt709, self.full_range = True, False
        self.program = None
        self.sets = []          # テクスチャの組 (表で使う組と、次のコマを先に送っておく裏の組)
        self.front = 0          # いま表示しているコマが入っている組
        self.next_picture = None    # 次に表示するコマを返す関数 (再生中だけ。先に送っておく)
        self.preload_hits = 0       # 先に送ってあったコマをそのまま描けた回数 (確認用)
        self.preload_misses = 0

    _overlay = None
    _overlay_key = None

    def set_color(self, bt709, full_range):
        self.bt709, self.full_range = bt709, full_range
        self.update()

    def initializeGL(self):
        try:
            from OpenGL import GL
            from OpenGL.GL import shaders
            self.GL = GL
            name = (GL.glGetString(GL.GL_RENDERER) or b"").decode(errors="replace")
            if "GDI Generic" in name or "llvmpipe" in name.lower() or "Apple Software" in name:
                raise RuntimeError(f"ソフトウェアの OpenGL です ({name})")
            self.program = shaders.compileProgram(
                shaders.compileShader(VERTEX_SHADER, GL.GL_VERTEX_SHADER),
                shaders.compileShader(FRAGMENT_SHADER, GL.GL_FRAGMENT_SHADER))
            self.loc = {key: GL.glGetUniformLocation(self.program, key)
                        for key in ("tex_y", "tex_u", "tex_v", "yuv_to_rgb", "offset", "scale")}
            self.attr_pos = GL.glGetAttribLocation(self.program, "pos")
            self.attr_uv = GL.glGetAttribLocation(self.program, "uv")
            if self.format().swapInterval() != 1:
                # グラフィックドライバの設定で垂直同期が切られていると、ここで分かる
                self.renderer = "GPU描画 (垂直同期なし)"
                print(f"垂直同期が効いていません (swap interval {self.format().swapInterval()})。"
                      "グラフィックドライバの設定を確認してください")
            textures = list(GL.glGenTextures(6))
            self.sets = [TextureSet(textures[:3]), TextureSet(textures[3:])]
            self.front = 0
            self.initialized = True
        except Exception as e:
            self.fail(f"{type(e).__name__}: {e}")

    def fail(self, reason):
        """GPU 描画を使えない。画面側 (on_failure) に CPU 描画へ切り替えてもらう。"""
        if self.failed:
            return
        self.failed = reason
        if self.on_failure:
            QTimer.singleShot(0, lambda: self.on_failure(reason))

    def _upload(self, pic, tset):
        """3 つのプレーンを tset のそれぞれの大きさのテクスチャへ送る。10/12bit は 16bit のテクスチャ。"""
        GL = self.GL
        bps = PLANAR[pic.fmt][2]
        internal, kind = (GL.GL_LUMINANCE, GL.GL_UNSIGNED_BYTE) if bps == 1 else             (GL.GL_LUMINANCE16, GL.GL_UNSIGNED_SHORT)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        layout = (pic.fmt, pic.w, pic.h)
        realloc = tset.layout != layout
        for tex, plane in zip(tset.textures, plane_views(pic.data, pic.fmt, pic.w, pic.h)):
            ph, pw = plane.shape
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            if realloc:
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
                GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, internal, pw, ph, 0, GL.GL_LUMINANCE, kind, plane)
            else:
                GL.glTexSubImage2D(GL.GL_TEXTURE_2D, 0, 0, 0, pw, ph, GL.GL_LUMINANCE, kind, plane)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        # 既定値に戻す。1 のままだと Qt が文字をテクスチャに書くときに崩れる
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 4)
        tset.layout = layout
        tset.key = picture_key(pic)

    def _textures_for(self, pic):
        """pic が入っているテクスチャの組を返す。先に送ってなければ、今の表でない組へ送る。"""
        key = picture_key(pic)
        if self.sets[self.front].key == key:        # 同じコマの描き直し (拡大や文字の更新)
            return self.front
        back = 1 - self.front
        if self.sets[back].key == key:
            self.preload_hits += 1
        else:
            self._upload(pic, self.sets[back])
            self.preload_misses += 1
        return back

    def _preload(self):
        """次に表示するコマを裏の組へ先に送っておく (次の書き換えでは描くだけで済む)。"""
        nxt = self.next_picture() if self.next_picture else None
        if nxt is None or nxt.fmt not in PLANAR:
            return
        back = self.sets[1 - self.front]
        if back.key != picture_key(nxt) and self.sets[self.front].key != picture_key(nxt):
            self._upload(nxt, back)

    def _quad(self, rect):
        """画面上の rect (論理ピクセル) に画像全体を描く。"""
        GL = self.GL
        W, H = self.width(), self.height()
        x0, x1 = rect.left() / W * 2 - 1, rect.right() / W * 2 - 1
        y0, y1 = 1 - rect.top() / H * 2, 1 - rect.bottom() / H * 2
        pos = np.array([x0, y0, x1, y0, x0, y1, x1, y1], np.float32)
        uv = np.array([0, 0, 1, 0, 0, 1, 1, 1], np.float32)
        GL.glVertexAttribPointer(self.attr_pos, 2, GL.GL_FLOAT, GL.GL_FALSE, 0, pos)
        GL.glVertexAttribPointer(self.attr_uv, 2, GL.GL_FLOAT, GL.GL_FALSE, 0, uv)
        GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)

    def paintGL(self):
        if not self.initialized:
            return
        p = QPainter(self)
        if not self.has_image() or self.picture.fmt not in PLANAR:
            p.fillRect(self.area(), Qt.black)
            return self.draw_placeholder(p)
        mini = self.minimap_rect()
        p.beginNativePainting()
        GL = self.GL
        dpr = self.devicePixelRatioF()
        GL.glViewport(0, 0, round(self.width() * dpr), round(self.height() * dpr))
        GL.glClearColor(0, 0, 0, 1)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        GL.glDisable(GL.GL_BLEND)
        self.front = self._textures_for(self.picture)
        GL.glUseProgram(self.program)
        bits = PLANAR[self.picture.fmt][3]
        matrix, offset = yuv_matrix(self.bt709, self.full_range, bits)
        GL.glUniformMatrix3fv(self.loc["yuv_to_rgb"], 1, GL.GL_FALSE, matrix)
        GL.glUniform3f(self.loc["offset"], *offset)
        # 16bit テクスチャは 65535 を 1 として読めるので、bits ビットの最大値が 1 になるよう直す
        GL.glUniform1f(self.loc["scale"], 1.0 if bits == 8 else 65535 / ((1 << bits) - 1))
        filt = GL.GL_NEAREST if self.pixelated else GL.GL_LINEAR
        for unit, (tex, name) in enumerate(zip(self.sets[self.front].textures, ("tex_y", "tex_u", "tex_v"))):
            GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, filt)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, filt)
            GL.glUniform1i(self.loc[name], unit)
        GL.glEnableVertexAttribArray(self.attr_pos)
        GL.glEnableVertexAttribArray(self.attr_uv)
        self._quad(self.image_rect())
        if mini:
            self._quad(mini)
        GL.glDisableVertexAttribArray(self.attr_pos)
        GL.glDisableVertexAttribArray(self.attr_uv)
        GL.glUseProgram(0)
        for unit in range(3):
            GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        # 描く命令を出し終えてから、次のコマを裏の組へ送る。GPU が今のコマを描いている間に
        # 送れるので、次の書き換えの直後 (コマを選んですぐ描く所) は描くだけになる
        self._preload()
        p.endNativePainting()
        self.count_painted()
        p.drawImage(0, 0, self.overlay_image(mini))

    def overlay_image(self, mini):
        """文字や枠 (draw_overlays) を描いた透明な画像。中身が変わったときだけ描き直す。

        毎コマ QPainter で文字を描くと 1.5 ms ほどかかり、144Hz (1 回 6.9 ms) では重い。
        同じ画像を渡し続ければ Qt は GPU 上のテクスチャを使い回すので、ほぼ手間がかからない。
        """
        dpr = self.devicePixelRatio()
        key = (self.width(), self.height(), dpr, self.stats_text, self.stats_warn, round(self.zoom_level(), 2),
               None if self.sel is None else self.sel.getRect(),
               None if mini is None else (round(self.s, 6), round(self.tx, 1), round(self.ty, 1)))
        if key != self._overlay_key:
            image = QImage(round(self.width() * dpr), round(self.height() * dpr), QImage.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(dpr)
            image.fill(Qt.transparent)
            painter = QPainter(image)
            self.draw_overlays(painter, mini)
            painter.end()
            self._overlay, self._overlay_key = image, key
        return self._overlay

    def render_soon(self):
        """すぐ描き直す。update() は Windows では 5 ms ほど待ってから描くため、144Hz では
        書き換え 1 回分 (6.9 ms) に間に合わない。描画の要求を直接イベントとして送る。"""
        QCoreApplication.postEvent(self, QEvent(QEvent.UpdateRequest))


    def keyPressEvent(self, e):
        if self.key_handler:
            self.key_handler(e)

    def event(self, e):
        kind = e.type()
        if kind in (QEvent.DragEnter, QEvent.DragMove) and e.mimeData().hasUrls():
            e.acceptProposedAction()
            return True
        if kind == QEvent.Drop and self.drop_handler:
            self.drop_handler(e)
            return True
        if self.pinch(e):
            return True
        return super().event(e)
