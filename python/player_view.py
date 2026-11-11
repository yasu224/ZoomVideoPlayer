"""動画の表示部分: 拡大縮小・パン・範囲選択・ミニマップ、CPU 描画と GPU (OpenGL) 描画。"""
import ctypes
import importlib
from concurrent.futures import ThreadPoolExecutor
import math
import os

import numpy as np
from player_core import SEMI, YUV_FORMATS, plane_sizes, texture_scale
from player_sdi import SEQUENCE, view_transform
from PySide6.QtCore import QCoreApplication, QEvent, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QSurfaceFormat
from PySide6.QtOpenGL import QOpenGLWindow
from PySide6.QtWidgets import QWidget

MAX_ZOOM = 64          # 全体表示に対する最大倍率
MINI_W = 220           # ミニマップの幅
SDI_COLLECT_MS = 10    # 停止中など書き換えが来ないときに、SDI 用に描いた絵の読み出しを頼むまでの待ち
SDI_PBOS = 4           # SDI 用の読み出しバッファの数
SDI_CODES_UNIT = 4     # v210 の 2 回目で、1 回目の結果を付けるテクスチャユニット (0〜2 は Y/U/V、3 は作るとき用)


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
uniform float semi;       // 1: 色差を交互に並べた形式 (tex_u に Cb を明るさ、Cr を透明度として持つ)
varying vec2 v_uv;
void main() {
    vec3 yuv = (semi > 0.5 ? vec3(texture2D(tex_y, v_uv).r, texture2D(tex_u, v_uv).ra)
                           : vec3(texture2D(tex_y, v_uv).r, texture2D(tex_u, v_uv).r, texture2D(tex_v, v_uv).r))
               * scale - offset;
    gl_FragColor = vec4(clamp(yuv_to_rgb * yuv, 0.0, 1.0), 1.0);
}
"""


# SDI 出力用: 出力の 1 行を RGBA8 のテクセルに見立てて、2vuy (8bit) か v210 (10bit) の並びを
# そのまま書く。読み出した RGBA のバイト列が、そのまま機器へ送る 1 行になる。
# 行は gl_FragCoord.y の小さい方 (読み出しの先頭) を画像の上にする。
# RGB は切り詰めずに BT.709 の YUV へ戻すので、BT.709 の動画は YUV のまま (超白なども) 出る。
# 2vuy は 1 回で描く (stage 0)。v210 は 2 回に分ける: 2 画素ごとに色を変換して 10bit のコード値を
# 16bit のテクスチャへ書き (stage 1)、それを読んで 6 画素 = 4 語に詰める (stage 2)。
# 頂点は位置だけ。古い OpenGL (macOS の 2.1) では属性 0 が有効でないと何も描かれないので、
# 属性を pos 1 つにして必ず 0 番にする
SDI_VERTEX_SHADER = """
#version 120
attribute vec2 pos;
void main() {
    gl_Position = vec4(pos, 0.0, 1.0);
}
"""

SDI_SHADER = """
#version 120
uniform sampler2D tex_y;
uniform sampler2D tex_u;
uniform sampler2D tex_v;
uniform mat3 yuv_to_rgb;
uniform vec3 offset;
uniform float scale;
uniform vec2 out_size;     // 出力の幅・高さ (画素)
uniform vec4 xform;        // 出力の画素 → 画像の uv: uv = (p - xform.xy) / xform.zw
uniform float semi;        // 1: 色差を交互に並べた形式 (FRAGMENT_SHADER と同じ)
uniform float stage;       // 0: 2vuy を詰める / 1: 2 画素ごとの 10bit のコード値 / 2: 1 の結果を v210 に詰める
uniform sampler2D tex_codes;   // stage 2 で読む、stage 1 の結果 (Y0, Y1, Cb, Cr のコード値 / 1023)
uniform vec2 codes_size;

vec3 yuv_at(float x, float y) {          // 出力の画素 (x 列目, 上から y 行目) の BT.709 の Y, Cb, Cr
    vec2 uv = (vec2(x + 0.5, y + 0.5) - xform.xy) / xform.zw;
    if (x >= out_size.x || uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0)
        return vec3(0.0);
    vec3 src = (semi > 0.5 ? vec3(texture2D(tex_y, uv).r, texture2D(tex_u, uv).ra)
                           : vec3(texture2D(tex_y, uv).r, texture2D(tex_u, uv).r, texture2D(tex_v, uv).r))
               * scale - offset;
    vec3 c = yuv_to_rgb * src;
    float l = dot(c, vec3(0.2126, 0.7152, 0.0722));
    return vec3(l, (c.b - l) / 1.8556, (c.r - l) / 1.5748);
}

float code_y(float l, float unit) { return clamp(floor(16.0 * unit + 219.0 * unit * l + 0.5), unit == 4.0 ? 4.0 : 1.0, unit == 4.0 ? 1019.0 : 254.0); }
float code_c(float c, float unit) { return clamp(floor(128.0 * unit + 224.0 * unit * c + 0.5), unit == 4.0 ? 4.0 : 1.0, unit == 4.0 ? 1019.0 : 254.0); }

vec4 word(float c0, float c1, float c2) {   // 10bit を 3 つ詰めた 32bit (下位から) を 4 バイトに
    float b0 = mod(c0, 256.0);
    float b1 = floor(c0 / 256.0) + mod(c1, 64.0) * 4.0;
    float b2 = floor(c1 / 64.0) + mod(c2, 16.0) * 16.0;
    float b3 = floor(c2 / 16.0);
    return vec4(b0, b1, b2, b3) / 255.0;
}

vec4 pair_at(float x, float y) {          // stage 1 の結果: x 番目の 2 画素の (Y0, Y1, Cb, Cr) のコード値
    return floor(texture2D(tex_codes, vec2((x + 0.5) / codes_size.x, (y + 0.5) / codes_size.y)) * 1023.0 + 0.5);
}

void main() {
    float i = floor(gl_FragCoord.x);
    float y = floor(gl_FragCoord.y);
    if (stage > 0.5 && stage < 1.5) {    // 2 画素 (2i, 2i+1) の Y0, Y1 と、2 画素の平均の Cb, Cr を 10bit で書く
        vec3 p0 = yuv_at(2.0 * i, y), p1 = yuv_at(2.0 * i + 1.0, y);
        vec2 c = (p0.yz + p1.yz) * 0.5;
        gl_FragColor = vec4(code_y(p0.x, 4.0), code_y(p1.x, 4.0), code_c(c.x, 4.0), code_c(c.y, 4.0)) / 1023.0;
        return;
    }
    if (stage < 0.5) {                   // 2vuy: Cb Y0 Cr Y1 (2 画素)
        vec3 p0 = yuv_at(2.0 * i, y), p1 = yuv_at(2.0 * i + 1.0, y);
        vec2 c = (p0.yz + p1.yz) * 0.5;
        gl_FragColor = vec4(code_c(c.x, 1.0), code_y(p0.x, 1.0), code_c(c.y, 1.0), code_y(p1.x, 1.0)) / 255.0;
        return;
    }
    // v210 (stage 2): 6 画素 (3 組) を 4 語 (16 バイト) に詰める。色の変換と丸めは stage 1 で 2 画素ごとに
    // 1 回だけ済ませてあるので、ここは 3 組を読んで詰めるだけ (以前は 1 語ごとに 6 画素とも変換していて、
    // 4K では GPU が 1 コマ 12 ms かかり画面の描画まで遅れていた)
    float g = floor(i / 4.0);
    float w = i - g * 4.0;
    vec4 q0 = pair_at(g * 3.0, y), q1 = pair_at(g * 3.0 + 1.0, y), q2 = pair_at(g * 3.0 + 2.0, y);
    float Y0 = q0.x, Y1 = q0.y, Y2 = q1.x, Y3 = q1.y, Y4 = q2.x, Y5 = q2.y;
    float Cb0 = q0.z, Cr0 = q0.w, Cb1 = q1.z, Cr1 = q1.w, Cb2 = q2.z, Cr2 = q2.w;
    if (w < 0.5)      gl_FragColor = word(Cb0, Y0, Cr0);
    else if (w < 1.5) gl_FragColor = word(Y1, Cb1, Y2);
    else if (w < 2.5) gl_FragColor = word(Cr1, Y3, Cb2);
    else              gl_FragColor = word(Y4, Cr2, Y5);
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


class SdiRead:
    """SDI 用に描いた 1 枚の、GPU からの読み出し。描く → 読み出しを頼む (fence を立てる) → 受け取る。"""

    __slots__ = ("pbo", "out", "size", "tag", "repeat", "fb", "width", "height", "fence")

    def __init__(self, pbo, out, tag, repeat, fb, width, height):
        self.pbo, self.out, self.tag, self.repeat = pbo, out, tag, repeat
        self.fb, self.width, self.height = fb, width, height
        self.size = width * 4 * height            # RGBA8 で読む
        self.fence = None


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
        # 次のコマを転送用バッファへ写すのは裏のスレッドで行う (4K 10bit で 2〜4 ms、GUI を止めない)。
        # 写している間は (Future, コマ, 送り先の組の番号)
        self._copier = ThreadPoolExecutor(max_workers=1, thread_name_prefix="texture-copy")
        self._copying = None
        self.sdi = None             # SDI 出力 (player_sdi.SdiOutput)。あれば拡大中の絵を描いて送る
        self._sdi_key = None        # 最後に送った絵 (コマと拡大の状態)。変わらなければ送り直さない
        self._sdi_target = None     # (フレームバッファ, テクスチャ, 幅, 高さ, [読み出し用バッファ SDI_PBOS 個])
        # 読み出しを頼んで、まだ受け取っていないもの (SdiRead、古い順)。終わりの印 (GL の fence) が
        # 立つまでは開かない (開くと GPU が読み出しを終えるまで画面側が止まるため)
        self._sdi_pending = []
        self._sdi_poll = False      # 読み出しの終わりを確かめ直す予定があるか
        self._sdi_codes = None      # v210 の 1 回目の書く先 (フレームバッファ, テクスチャ, 幅, 高さ)
        self._sdi_drawn = None      # 描いて、まだ読み出しを頼んでいない絵 (SdiRead。次の書き換えで頼む)
        self._sdi_mapped = set()    # 開いて送るスレッドへ渡している読み出し用バッファ
        self._sdi_unmap = []        # 送り終えて閉じてよいバッファ (送るスレッドが足す)
        self._sdi_retry = False     # 描けなかった変化を後で描き直す予定があるか
        self._sdi_seq_serial = None   # 順番に並べるとき: 最後に数えた新しいコマ (serial) と、数えた枚数
        self._sdi_seq_count = 0
        self.sdi_program = None

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
                        for key in ("tex_y", "tex_u", "tex_v", "yuv_to_rgb", "offset", "scale", "semi")}
            self.attr_pos = GL.glGetAttribLocation(self.program, "pos")
            self.attr_uv = GL.glGetAttribLocation(self.program, "uv")
            try:
                self.sdi_program = shaders.compileProgram(
                    shaders.compileShader(SDI_VERTEX_SHADER, GL.GL_VERTEX_SHADER),
                    shaders.compileShader(SDI_SHADER, GL.GL_FRAGMENT_SHADER))
                self.sdi_loc = {key: GL.glGetUniformLocation(self.sdi_program, key)
                                for key in ("tex_y", "tex_u", "tex_v", "yuv_to_rgb", "offset", "scale",
                                            "out_size", "xform", "stage", "tex_codes", "codes_size", "semi")}
                self.sdi_attr_pos = GL.glGetAttribLocation(self.sdi_program, "pos")
            except Exception as e:      # SDI 出力が使えないだけで、画面の表示は続ける
                print(f"SDI 出力用のシェーダーを作れません: {e}")
                self.sdi_program = None
            if self.format().swapInterval() != 1:
                # グラフィックドライバの設定で垂直同期が切られていると、ここで分かる
                self.renderer = "GPU描画 (垂直同期なし)"
                print(f"垂直同期が効いていません (swap interval {self.format().swapInterval()})。"
                      "グラフィックドライバの設定を確認してください")
            # テクスチャ転送用 (_upload)。[0] はその場で送るとき、[1] は裏のスレッドで写すとき (_preload)
            self.upload_pbos = [int(b) for b in GL.glGenBuffers(2)]
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
        """3 つのプレーンを tset のそれぞれの大きさのテクスチャへ、その場で送る。"""
        ptr = self._map_upload(0, pic.data.nbytes)
        ctypes.memmove(ptr, pic.data.ctypes.data, pic.data.nbytes)
        self._textures_from_buffer(0, pic, tset)

    # テクスチャへは、いったん GPU の転送用バッファ (PBO) へまとめて写し、そこから GPU が写す。
    # 手元のメモリから直接 glTexSubImage2D すると、macOS ではドライバが CPU で写し直すので
    # 4K 8bit で 7 ms、10bit で 14 ms かかり、4K 10bit 4:2:2 は 60 コマ出せなかった (PBO なら 0.5〜2 ms)。
    def _map_upload(self, i, nbytes):
        """転送用バッファ i を nbytes の大きさで開き、書き込む先のアドレスを返す。"""
        GL = self.GL
        GL.glBindBuffer(GL.GL_PIXEL_UNPACK_BUFFER, self.upload_pbos[i])
        # 中身を捨てて作り直すと、GPU が前のコマをまだ読んでいても待たずに書ける
        GL.glBufferData(GL.GL_PIXEL_UNPACK_BUFFER, nbytes, None, GL.GL_STREAM_DRAW)
        ptr = GL.glMapBuffer(GL.GL_PIXEL_UNPACK_BUFFER, GL.GL_WRITE_ONLY)
        GL.glBindBuffer(GL.GL_PIXEL_UNPACK_BUFFER, 0)
        if not ptr:
            raise RuntimeError("テクスチャ転送用のバッファを開けません")
        return int(ptr)

    def _textures_from_buffer(self, i, pic, tset):
        """書き終えた転送用バッファ i を閉じ、そこから tset のテクスチャへ送る。10/12bit は 16bit。

        Y・U・V の形式は 3 枚。色差を交互に並べた形式は 2 枚で、2 枚目は Cb を明るさ、Cr を透明度とする
        2 チャンネルのテクスチャにする (シェーダーの semi)。
        """
        GL = self.GL
        bps = YUV_FORMATS[pic.fmt][2]
        kind = GL.GL_UNSIGNED_BYTE if bps == 1 else GL.GL_UNSIGNED_SHORT
        one = (GL.GL_LUMINANCE if bps == 1 else GL.GL_LUMINANCE16, GL.GL_LUMINANCE, 1)
        two = (GL.GL_LUMINANCE8_ALPHA8 if bps == 1 else GL.GL_LUMINANCE16_ALPHA16, GL.GL_LUMINANCE_ALPHA, 2)
        GL.glBindBuffer(GL.GL_PIXEL_UNPACK_BUFFER, self.upload_pbos[i])
        GL.glUnmapBuffer(GL.GL_PIXEL_UNPACK_BUFFER)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        layout = (pic.fmt, pic.w, pic.h)
        realloc = tset.layout != layout
        offset = 0
        semi = pic.fmt in SEMI
        for n, (tex, (cols, rows)) in enumerate(zip(tset.textures, plane_sizes(pic.fmt, pic.w, pic.h))):
            internal, fmt, channels = two if semi and n == 1 else one
            width = cols // channels               # テクセルの数 (CbCr は 2 つで 1 テクセル)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            src = ctypes.c_void_p(offset)          # 転送用バッファの中の位置
            if realloc:
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
                GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, internal, width, rows, 0, fmt, kind, src)
            else:
                GL.glTexSubImage2D(GL.GL_TEXTURE_2D, 0, 0, 0, width, rows, fmt, kind, src)
            offset += cols * rows * bps
        GL.glBindBuffer(GL.GL_PIXEL_UNPACK_BUFFER, 0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        # 既定値に戻す。1 のままだと Qt が文字をテクスチャに書くときに崩れる
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 4)
        tset.layout = layout
        tset.key = picture_key(pic)

    def wait_copy(self):
        """裏のスレッドがコマのメモリから写している途中なら、写し終わるまで待つ。

        画面側はコマのスロットを空きに戻す (デコード側が上書きする) 前にこれを呼ぶ (Decoder.before_free)。
        """
        if self._copying is not None:
            self._copying[0].result()

    def _finish_copy(self):
        """裏のスレッドで写したコマを、テクスチャの組へ送る (写し終わっていなければ待つ)。"""
        if self._copying is None:
            return
        future, pic, index = self._copying
        self._copying = None
        future.result()
        self._textures_from_buffer(1, pic, self.sets[index])

    def _textures_for(self, pic):
        """pic が入っているテクスチャの組を返す。先に送ってなければ、今の表でない組へ送る。"""
        self._finish_copy()
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
        """次に表示するコマを裏の組へ先に送っておく (次の書き換えでは描くだけで済む)。

        転送用バッファへ写すのは裏のスレッドで始め、テクスチャへ送るのは次に描くとき (_finish_copy)。
        """
        nxt = self.next_picture() if self.next_picture else None
        if nxt is None or nxt.fmt not in YUV_FORMATS or self._copying is not None:
            return
        index = 1 - self.front
        if self.sets[index].key == picture_key(nxt) or self.sets[self.front].key == picture_key(nxt):
            return
        self.sets[index].key = None                  # 写し終わるまで、この組の中身は使えない
        ptr = self._map_upload(1, nxt.data.nbytes)
        # ctypes.memmove は写している間 GIL を手放すので、GUI のスレッドと並んで動く
        future = self._copier.submit(ctypes.memmove, ptr, nxt.data.ctypes.data, nxt.data.nbytes)
        self._copying = (future, nxt, index)

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
        if not self.has_image() or self.picture.fmt not in YUV_FORMATS:
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
        fmt = self.picture.fmt
        bits = YUV_FORMATS[fmt][3]
        matrix, offset = yuv_matrix(self.bt709, self.full_range, bits)
        GL.glUniformMatrix3fv(self.loc["yuv_to_rgb"], 1, GL.GL_FALSE, matrix)
        GL.glUniform3f(self.loc["offset"], *offset)
        # 16bit テクスチャは 65535 を 1 として読めるので、bits ビットの最大値が 1 になるよう直す
        GL.glUniform1f(self.loc["scale"], texture_scale(fmt))
        GL.glUniform1f(self.loc["semi"], 1.0 if fmt in SEMI else 0.0)
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
        if self.sdi is not None:
            self._send_sdi(matrix, offset, fmt)
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

    # ---------- SDI 出力 ----------
    def sdi_state(self):
        """SDI へ送る絵を決めるもの。変わったときだけ描き直して送る。"""
        return (self.serial, round(self.s, 9), round(self.tx, 3), round(self.ty, 3), self.width(), self.height(),
                self.pixelated, self.bt709, self.full_range, id(self.sdi))

    def _send_sdi(self, matrix, offset, fmt):
        """拡大中の絵を SDI の解像度で描き、2vuy / v210 に詰めて読み出し、SDI 出力へ渡す。

        画面と同じテクスチャ (いま表の組) を使うので、画面に出したコマと同じコマが出る。
        いつ描くかは player_sdi の説明のとおり (順番に並べるときは新しいコマごと、それ以外は機器のコマごと)。
        読み出し (4K では GPU で 5 ms ほど) は描いたコマでは頼まず、次の書き換えで頼む (_read_sdi)。
        描くのと同じ書き換えの間に読み出しまで行うと、4K では GPU の仕事が 1 回分 (120Hz で 8.3 ms) を
        超えて画面の書き換えを取りこぼし、SDI へ送るコマも減っていた。受け取るのはさらにその後 (_collect_sdi)。
        """
        if self.sdi_program is None:
            return
        self._read_sdi()
        self._collect_sdi()
        self._unmap_sdi()
        out = self.sdi
        key = self.sdi_state()
        if key == self._sdi_key:
            return
        sequence = out.sequence
        if sequence:
            # 新しいコマが出るたびに数え、m 枚に 1 枚を描く。拡大の変化だけでは描かない (次のコマに乗る)
            if self.serial == self._sdi_seq_serial:
                return
            self._sdi_seq_serial = self.serial
            self._sdi_seq_count += 1
            if self._sdi_seq_count % sequence[1]:
                return
        elif not out.due():
            if not self._sdi_retry:          # 止まっているときも最後の変化を出すため、後で描き直す
                self._sdi_retry = True
                QTimer.singleShot(max(1, math.ceil(out.wait_time() * 1000)), self._sdi_again)
            return
        fb, _, TW, H, pbos = self._sdi_target_for(out.texels, out.height)
        busy = self._sdi_mapped | {r.pbo for r in self._sdi_pending}
        if self._sdi_drawn is not None:
            busy.add(self._sdi_drawn.pbo)
        pbo = next((b for b in pbos if b not in busy), None)
        if pbo is None:                  # 送るのが追いつかず、読み出し用バッファが空いていない
            return
        GL = self.GL
        so, ox, oy = view_transform(self.width(), self.height(), self.s, self.tx, self.ty,
                                    self.picture.w, self.picture.h, out.width, H)
        GL.glUseProgram(self.sdi_program)
        loc = self.sdi_loc
        GL.glUniformMatrix3fv(loc["yuv_to_rgb"], 1, GL.GL_FALSE, matrix)
        GL.glUniform3f(loc["offset"], *offset)
        GL.glUniform1f(loc["scale"], texture_scale(fmt))
        GL.glUniform1f(loc["semi"], 1.0 if fmt in SEMI else 0.0)
        for unit, name in enumerate(("tex_y", "tex_u", "tex_v")):
            GL.glUniform1i(loc[name], unit)
        GL.glUniform1i(loc["tex_codes"], SDI_CODES_UNIT)
        GL.glUniform2f(loc["out_size"], out.width, H)
        GL.glUniform4f(loc["xform"], ox, oy, self.picture.w * so, self.picture.h * so)
        pos = np.array([-1, -1, 1, -1, -1, 1, 1, 1], np.float32)
        GL.glEnableVertexAttribArray(self.sdi_attr_pos)
        GL.glVertexAttribPointer(self.sdi_attr_pos, 2, GL.GL_FLOAT, GL.GL_FALSE, 0, pos)
        if out.ten_bit:
            # 1 回目: 2 画素ごとに色を変換してコード値を書く。2 回目: それを v210 に詰める
            cfb, ctex, CW = self._sdi_codes_for(TW * 3 // 4, H)     # 1 語 = 1.5 画素、2 画素で 1 組
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, cfb)
            GL.glViewport(0, 0, CW, H)
            GL.glUniform1f(loc["stage"], 1.0)
            GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fb)
            GL.glViewport(0, 0, TW, H)
            GL.glActiveTexture(GL.GL_TEXTURE0 + SDI_CODES_UNIT)
            GL.glBindTexture(GL.GL_TEXTURE_2D, ctex)
            GL.glUniform2f(loc["codes_size"], CW, H)
            GL.glUniform1f(loc["stage"], 2.0)
            GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
            GL.glActiveTexture(GL.GL_TEXTURE0)
        else:
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fb)
            GL.glViewport(0, 0, TW, H)
            GL.glUniform1f(loc["stage"], 0.0)
            GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
        GL.glDisableVertexAttribArray(self.sdi_attr_pos)
        GL.glFlush()                     # 描く命令は今の書き換えのうちに GPU へ送る
        # 画面へ描く状態に戻す
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        dpr = self.devicePixelRatioF()
        GL.glViewport(0, 0, round(self.width() * dpr), round(self.height() * dpr))
        out.rendered()
        self._sdi_key = key
        tag, repeat = (SEQUENCE, sequence[0]) if sequence else (out.rendered_frame, 1)
        self._sdi_drawn = SdiRead(pbo, out, tag, repeat, fb, TW, H)
        # 書き換えが来ないとき (停止中) は、少し後に読み出しを頼む
        self._poll_sdi_later(SDI_COLLECT_MS)

    def _read_sdi(self):
        """前に描いた SDI の絵の読み出しを頼む (GPU のバッファへ。待たない)。OpenGL の文脈が今のときに呼ぶ。"""
        read, self._sdi_drawn = self._sdi_drawn, None
        if read is None or read.out is not self.sdi:
            return
        GL = self.GL
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, read.fb)
        GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 4)
        GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, read.pbo)
        GL.glReadPixels(0, 0, read.width, read.height, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, ctypes.c_void_p(0))
        GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, 0)
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, self.defaultFramebufferObject())
        read.fence = self._fence()
        self._sdi_pending.append(read)
        self._poll_sdi_later(2)

    def _sdi_target_for(self, width, height):
        """SDI 用に描く先 (width × height の RGBA8 テクスチャを付けたフレームバッファ) と読み出し用バッファ。
        大きさが変わったら作り直す。"""
        target = self._sdi_target
        if target is not None and target[2:4] == (width, height):
            return target
        GL = self.GL
        if target is not None:
            GL.glDeleteFramebuffers(1, [target[0]])
            GL.glDeleteTextures([target[1]])
            GL.glDeleteBuffers(len(target[4]), target[4])
        tex = GL.glGenTextures(1)
        # ユニット 0〜2 には Y / U / V が付いているので、空いているユニットで作る
        GL.glActiveTexture(GL.GL_TEXTURE3)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, width, height, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, None)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        fb = GL.glGenFramebuffers(1)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fb)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, tex, 0)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        # 読み出し中のもの・送るスレッドが使っているものがあるので、いくつかを順に使う
        pbos = [int(b) for b in GL.glGenBuffers(SDI_PBOS)]
        for b in pbos:
            GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, b)
            GL.glBufferData(GL.GL_PIXEL_PACK_BUFFER, width * 4 * height, None, GL.GL_STREAM_READ)
        GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, 0)
        self._sdi_target = (fb, tex, width, height, pbos)
        return self._sdi_target

    def _sdi_codes_for(self, width, height):
        """v210 の 1 回目 (2 画素ごとのコード値) を書く先。16bit の RGBA テクスチャを付けたフレームバッファ。"""
        target = self._sdi_codes
        if target is not None and target[2:4] == (width, height):
            return target[0], target[1], width
        GL = self.GL
        if target is not None:
            GL.glDeleteFramebuffers(1, [target[0]])
            GL.glDeleteTextures([target[1]])
        tex = GL.glGenTextures(1)
        GL.glActiveTexture(GL.GL_TEXTURE0 + SDI_CODES_UNIT)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        # 10bit のコード値を 4 つ (Y0, Y1, Cb, Cr) 持つ (8bit では足りない)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA16, width, height, 0, GL.GL_RGBA, GL.GL_UNSIGNED_SHORT, None)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        fb = GL.glGenFramebuffers(1)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fb)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, tex, 0)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        self._sdi_codes = (fb, tex, width, height)
        return fb, tex, width

    def _fence(self):
        """ここまでの GPU の命令が終わったら立つ印。使えない環境では None (開くときに待つ)。"""
        GL = self.GL
        try:
            fence = GL.glFenceSync(GL.GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
        except Exception:                # OpenGL 3.2 / ARB_sync が無い
            return None
        GL.glFlush()                     # 命令を GPU へ送り出す (送らないと印がいつまでも立たない)
        return fence

    def _fence_done(self, fence):
        """印が立ったか (待たずに確かめる)。印が無い (fence を使えない環境) ときは立ったとみなす。"""
        if fence is None:
            return True
        GL = self.GL
        return GL.glClientWaitSync(fence, 0, 0) in (GL.GL_ALREADY_SIGNALED, GL.GL_CONDITION_SATISFIED)

    def _collect_sdi(self):
        """読み出しが終わったものを古い順に受け取り、SDI 出力へ渡す。OpenGL の文脈が今のときに呼ぶ。

        まだ終わっていないものは開かずに残す (開くと終わるまで画面側が止まり、4K では 80 ms も止まって
        画面のコマが遅れ、SDI へ送るコマも減っていた)。止めた出力のものは捨てる。
        """
        GL = self.GL
        while self._sdi_pending:
            read = self._sdi_pending[0]
            current = read.out is self.sdi
            if current and not self._fence_done(read.fence):
                break                    # 順番を守るため、これより新しいものも待つ
            self._sdi_pending.pop(0)
            if read.fence is not None:
                GL.glDeleteSync(read.fence)
            if current:
                self._submit_sdi(read)

    def _submit_sdi(self, read):
        """読み出しを終えたバッファを開き、SDI 出力へ渡す。"""
        GL = self.GL
        GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, read.pbo)
        ptr = GL.glMapBuffer(GL.GL_PIXEL_PACK_BUFFER, GL.GL_READ_ONLY)
        out, pbo = read.out, read.pbo
        if ptr and out.accepts_pointer:
            # 開いたまま送るスレッドへ渡し、そこから機器のコマへ直接写す。閉じるのは送り終えてから
            self._sdi_mapped.add(pbo)
            out.submit_pointer(int(ptr), read.tag, lambda: self._sdi_unmap.append(pbo), read.repeat)
        elif ptr:                       # 写しを渡す (確認用の出力など)
            data = np.ctypeslib.as_array((ctypes.c_uint8 * read.size).from_address(int(ptr))).copy()
            GL.glUnmapBuffer(GL.GL_PIXEL_PACK_BUFFER)
            out.submit(data, read.tag, read.repeat)
        GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, 0)

    def _unmap_sdi(self):
        """送り終えた読み出し用バッファを閉じる。OpenGL の文脈が今のときに呼ぶ。"""
        if not self._sdi_unmap:
            return
        GL = self.GL
        while self._sdi_unmap:
            pbo = self._sdi_unmap.pop()
            GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, pbo)
            GL.glUnmapBuffer(GL.GL_PIXEL_PACK_BUFFER)
            self._sdi_mapped.discard(pbo)
        GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, 0)

    def _poll_sdi_later(self, ms):
        if not self._sdi_poll:
            self._sdi_poll = True
            QTimer.singleShot(ms, self._collect_sdi_later)

    def _collect_sdi_later(self):
        """書き換えを待たずに、読み出しを受け取り、送り終えたバッファを閉じる。まだ終わっていない
        読み出しがあれば、少し後に確かめ直す。"""
        self._sdi_poll = False
        if (self._sdi_drawn or self._sdi_pending or self._sdi_unmap) and self.initialized:
            self.makeCurrent()
            self._read_sdi()
            self._collect_sdi()
            self._unmap_sdi()
            self.doneCurrent()
        if self._sdi_pending:
            self._poll_sdi_later(2)

    def _sdi_again(self):
        self._sdi_retry = False
        if self.sdi is not None and self.sdi_state() != self._sdi_key:
            self.update()

    def set_sdi(self, output):
        """SDI 出力を付け替える (None で止める)。古い出力は呼んだ側が閉じる。"""
        self.sdi = output
        self._sdi_key = None              # 古い出力の読み出しは、受け取るときに捨てる
        self.update()
        # 止めた出力が送り終えたバッファを閉じる (呼んだ側が古い出力を閉じた後)
        QTimer.singleShot(50, self._collect_sdi_later)

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
