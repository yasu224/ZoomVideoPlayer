"""テスト用の動画を作る・読むための道具。

各コマの左上に、フレーム番号を 2 進数で白黒のブロックとして描いておく (可逆圧縮)。
表示されたコマからその番号を読めば、狙ったコマが本当に出ているか確かめられる。
"""
import os
import tempfile
from fractions import Fraction

import av
import numpy as np

BITS = 12          # 4096 コマまで
BLOCK = 16         # 1 ビットのブロックの大きさ (ピクセル)
WHITE, BLACK = 235, 16   # 映像用の狭い範囲の Y 値

_made = {}


def make_video(fps=Fraction(120), frames=300, size=(320, 180), gop=60, crf=0, pix_fmt="yuv420p", name=None):
    """フレーム番号入りの H.264 動画を作り、そのパスを返す (同じ条件なら作り直さない)。

    crf=0 は可逆。大きい動画で速度を測るときは crf を上げてファイルを小さくする。
    pix_fmt="yuv422p10le" で業務用カメラ (XAVC) と同じ 10bit 4:2:2 にできる。
    """
    key = (fps, frames, size, gop, crf, pix_fmt)
    if key in _made:
        return _made[key]
    folder = os.path.join(tempfile.gettempdir(), "zvp-tests")
    os.makedirs(folder, exist_ok=True)
    suffix = "" if pix_fmt == "yuv420p" else "_" + pix_fmt
    name = name or f"idx_{float(fps):.3f}_{frames}_{size[0]}x{size[1]}_g{gop}_crf{crf}{suffix}.mp4"
    path = os.path.join(folder, name)
    w, h = size
    with av.open(path, "w") as out:
        stream = out.add_stream("libx264", rate=fps)
        stream.width, stream.height, stream.pix_fmt = w, h, pix_fmt
        stream.codec_context.gop_size = gop
        stream.options = {"crf": str(crf), "preset": "ultrafast", "bframes": "2", "keyint": str(gop),
                          "min-keyint": str(gop), "scenecut": "0"}
        for n in range(frames):
            y = np.full((h, w), 100, np.uint8)
            y[h // 2:, :] = (n * 7) % 200 + 20          # 下半分はコマごとに明るさを変える
            for bit in range(BITS):
                y[0:BLOCK, bit * BLOCK:(bit + 1) * BLOCK] = WHITE if n >> bit & 1 else BLACK
            uv = np.full((h // 2, w // 2), 128, np.uint8)
            frame = av.VideoFrame.from_ndarray(np.concatenate([y, uv.reshape(-1, w), uv.reshape(-1, w)]),
                                               format="yuv420p")
            if pix_fmt != "yuv420p":
                frame = frame.reformat(format=pix_fmt)
            frame.pts = n
            frame.time_base = Fraction(1) / fps
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)
    _made[key] = path
    return path


def read_index_y(y_plane):
    """Y プレーン (2 次元配列) からフレーム番号を読む。"""
    n = 0
    for bit in range(BITS):
        if y_plane[BLOCK // 2, bit * BLOCK + BLOCK // 2] > 128:
            n |= 1 << bit
    return n


def read_index_picture(picture):
    """画面側の Picture (YUV のプレーン形式か bgra) からフレーム番号を読む。"""
    from player_core import PLANAR, plane_views
    if picture.fmt == "bgra":
        return read_index_y(picture.data[:, :, 1])      # BGRA の G (白黒なので R/G/B は同じ)
    y = plane_views(picture.data, picture.fmt, picture.w, picture.h)[0]
    return read_index_y(y >> (PLANAR[picture.fmt][3] - 8))


def read_index_frame(av_frame):
    return read_index_y(av_frame.to_ndarray(format="gray"))


def make_bars(size=(1920, 1080), fps=Fraction(30), frames=10, pix_fmt="yuv420p"):
    """色の確認用: 8 本の縦帯 (Y, U, V の値が分かっている) の BT.709 動画。帯の YUV 値 (8bit) も返す。"""
    folder = os.path.join(tempfile.gettempdir(), "zvp-tests")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"bars_{size[0]}x{size[1]}_{pix_fmt}.mp4")
    bars = [(180, 128, 128), (145, 54, 34), (170, 166, 16), (82, 90, 240),
            (105, 212, 234), (63, 102, 42), (41, 240, 110), (100, 128, 128)]
    w, h = size
    y = np.empty((h, w), np.uint8)
    u = np.empty((h // 2, w // 2), np.uint8)
    v = np.empty((h // 2, w // 2), np.uint8)
    for i, (yy, uu, vv) in enumerate(bars):
        x0, x1 = w * i // 8, w * (i + 1) // 8
        y[:, x0:x1], u[:, x0 // 2:x1 // 2], v[:, x0 // 2:x1 // 2] = yy, uu, vv
    data = np.concatenate([y, u.reshape(-1, w), v.reshape(-1, w)])
    with av.open(path, "w") as out:
        stream = out.add_stream("libx264", rate=fps)
        stream.width, stream.height, stream.pix_fmt = w, h, pix_fmt
        stream.options = {"crf": "0", "preset": "ultrafast",
                          "x264-params": "colorprim=bt709:transfer=bt709:colormatrix=bt709"}
        for n in range(frames):
            frame = av.VideoFrame.from_ndarray(data, format="yuv420p")
            if pix_fmt != "yuv420p":
                frame = frame.reformat(format=pix_fmt)
            frame.pts = n
            frame.time_base = Fraction(1) / fps
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)
    return path, bars


def bt709_rgb(y, u, v, bits=8):
    """映像用の狭い範囲の BT.709 YUV (bits ビット) を 8bit RGB にする (GPU 描画の答え合わせ用)。"""
    unit = 1 << (bits - 8)
    y, u, v = y / unit - 16, u / unit - 128, v / unit - 128
    r = 255 / 219 * y + 255 / 224 * 1.5748 * v
    g = 255 / 219 * y - 255 / 224 * (0.1873 * u + 0.4681 * v)
    b = 255 / 219 * y + 255 / 224 * 1.8556 * u
    return [min(255, max(0, round(c))) for c in (r, g, b)]
