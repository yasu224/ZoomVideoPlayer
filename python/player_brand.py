"""MARY のロゴ (assets/mary.svg) を、画面の飾りとアプリのアイコンに描く。

ロゴは SVG のまま持ち、使う大きさ・色で描き直す (どの拡大率の画面でもぼやけない)。
アイコンは暗い角丸の四角に白い文字と、アプリの選択色 (青) の細い線。tools/make_icon.py が同じ絵を
assets/mary.ico (Windows の exe) と assets/mary.icns (macOS の .app) に書き出す。
"""
import os
import sys

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

ICON_BG = "#1b1e24"        # アプリの操作パネルと同じ色
ICON_TEXT = "#e6e8eb"
ACCENT = "#4da3ff"         # アプリの選択・強調の色
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)


def asset_path(name):
    """assets/ のファイルの場所。PyInstaller でまとめたときはその中。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "assets", name)


_renderer = None


def renderer():
    global _renderer
    if _renderer is None:
        _renderer = QSvgRenderer(asset_path("mary.svg"))
    return _renderer if _renderer.isValid() else None


def aspect():
    """ロゴの幅 / 高さ。"""
    r = renderer()
    if r is None:
        return 4.0
    box = r.viewBoxF()
    return box.width() / box.height()


def draw_logo(p, rect, color):
    """rect (QRectF) の中に、縦横比を保って真ん中にロゴを color で描く。"""
    r = renderer()
    if r is None or rect.width() <= 0 or rect.height() <= 0:
        return
    w = min(rect.width(), rect.height() * aspect())
    h = w / aspect()
    target = QRectF(rect.center().x() - w / 2, rect.center().y() - h / 2, w, h)
    # SVG の黒い文字を描いてから、文字の形のまま色を塗り替える
    dpr = p.device().devicePixelRatioF() if p.device() else 1.0
    img = QImage(max(1, round(w * dpr)), max(1, round(h * dpr)), QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    q = QPainter(img)
    q.setRenderHint(QPainter.Antialiasing)
    r.render(q, QRectF(0, 0, img.width(), img.height()))
    q.setCompositionMode(QPainter.CompositionMode_SourceIn)
    q.fillRect(img.rect(), QColor(color))
    q.end()
    img.setDevicePixelRatio(dpr)
    p.drawImage(target, img)


def logo_pixmap(height, color, dpr=1.0):
    """高さ height (論理ピクセル) のロゴの画像 (操作パネルのラベル用)。"""
    w = height * aspect()
    pm = QPixmap(max(1, round(w * dpr)), max(1, round(height * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    draw_logo(p, QRectF(0, 0, w, height), color)
    p.end()
    return pm


def icon_image(size):
    """size × size のアイコンの絵。"""
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    # macOS のアイコンと同じく、少し内側に角丸の四角を置く (小さいアイコンでは端まで使う)
    m = 0.0 if size <= 32 else s * 0.06
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(ICON_BG))
    p.drawRoundedRect(QRectF(m, m, s - 2 * m, s - 2 * m), s * 0.2, s * 0.2)
    inner = s - 2 * m
    text_w = inner * (0.82 if size <= 32 else 0.72)
    text_h = text_w / aspect()
    cy = m + inner * 0.47
    draw_logo(p, QRectF(s / 2 - text_w / 2, cy - text_h / 2, text_w, text_h), ICON_TEXT)
    # 文字の下に青い細い線 (16px では潰れるので 24px から)
    if size >= 24:
        line_h = max(1.0, inner * 0.035)
        p.setBrush(QColor(ACCENT))
        p.drawRoundedRect(QRectF(s / 2 - text_w * 0.35, cy + text_h / 2 + inner * 0.07, text_w * 0.7, line_h),
                          line_h / 2, line_h / 2)
    p.end()
    return img


def app_icon():
    """ウィンドウとタスクバー / Dock のアイコン。"""
    icon = QIcon()
    for size in ICON_SIZES[:7]:
        icon.addPixmap(QPixmap.fromImage(icon_image(size)))
    return icon
