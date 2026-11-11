"""SDI 出力を確かめる: .venv/bin/python tools/check_sdi.py [--mode 2160p59.94]

1. 詰め方: GPU が 2vuy (8bit) / v210 (10bit) に詰めたバイト列を、numpy で計算した値と比べる
   (乱数の YUV のコマを等倍・ピクセル表示で描かせる。機器は要らない)
2. 出力: Blackmagic の機器がつながっていれば、1080p の形式 (--mode で指定も可) で 5 秒再生し、
   機器のどのコマにも新しい絵が出るか (同じ絵が続いたり捨てたりは数回まで)、一時停止中も最後の絵を
   出し続けるかを見る

本物の画面 (OpenGL) が要る。デコード用の子プロセスはこのファイルを読み直すので、重い読み込みは main の中で行う。
"""
import os
import sys
import time
from fractions import Fraction

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def wait(sec):
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(int(sec * 1000), loop.quit)
    loop.exec()


class FakeOutput:
    """機器の代わりに、送られたバイト列を取っておく。"""

    accepts_pointer = False       # 写しを submit で受け取る
    sequence = None
    rendered_frame = None

    def __init__(self, w, h, ten_bit):
        self.width, self.height, self.ten_bit = w, h, ten_bit
        self.row_bytes = (w + 47) // 48 * 128 if ten_bit else w * 2
        self.texels = self.row_bytes // 4
        self.period = 0.0
        self.data = None

    def due(self):
        return True

    def wait_time(self):
        return 0.0

    def rendered(self):
        pass

    def submit(self, data, frame=None, repeat=1):
        self.data = data


def unpack(raw, w, h, row_bytes, ten_bit):
    """2vuy / v210 のバイト列 → Y (h, w), Cb, Cr (h, w/2) のコード値。"""
    import numpy as np
    rows = np.asarray(raw, np.uint8).reshape(h, row_bytes)
    if not ten_bit:
        q = rows[:, :w * 2].reshape(h, -1, 4).astype(np.int64)
        y = np.empty((h, w), np.int64)
        y[:, 0::2], y[:, 1::2] = q[..., 1], q[..., 3]
        return y, q[..., 0], q[..., 2]
    groups = (w + 5) // 6
    words = rows[:, :groups * 16].copy().view("<u4").reshape(h, groups, 4).astype(np.int64)
    c = [(words >> s) & 1023 for s in (0, 10, 20)]      # c[j][..., k] = 語 k の j 番目
    y = np.stack([c[1][..., 0], c[0][..., 1], c[2][..., 1], c[1][..., 2], c[0][..., 3], c[2][..., 3]], -1)
    cb = np.stack([c[0][..., 0], c[1][..., 1], c[2][..., 2]], -1)
    cr = np.stack([c[2][..., 0], c[0][..., 2], c[1][..., 3]], -1)
    return y.reshape(h, -1)[:, :w], cb.reshape(h, -1)[:, :w // 2], cr.reshape(h, -1)[:, :w // 2]


def random_picture(fmt, w, h, seed):
    import numpy as np

    from player_core import PLANAR, frame_size, plane_views
    from player_frames import Picture
    rng = np.random.default_rng(seed)
    bits = PLANAR[fmt][3]
    unit = 1 << (bits - 8)
    data = np.zeros(frame_size(fmt, w, h), np.uint8)
    y, u, v = plane_views(data, fmt, w, h)
    y[:] = rng.integers(16 * unit, 235 * unit + 1, y.shape)
    u[:] = rng.integers(16 * unit, 240 * unit + 1, u.shape)
    v[:] = rng.integers(16 * unit, 240 * unit + 1, v.shape)
    return Picture(float(seed), fmt, w, h, data)


def expected_packed(pic, ten_bit, row_bytes):
    """ピクセル表示 (最近傍) の等倍で描いたときの、正しいバイト列 (player_sdi.pack_reference)。"""
    import numpy as np

    from player_core import PLANAR, plane_views
    from player_sdi import pack_reference
    from player_view import yuv_matrix
    sx, sy, _, bits = PLANAR[pic.fmt]
    y, u, v = (p.astype(np.float64) for p in plane_views(pic.data, pic.fmt, pic.w, pic.h))
    u = np.repeat(np.repeat(u, 1 << sx, axis=1), 1 << sy, axis=0)
    v = np.repeat(np.repeat(v, 1 << sx, axis=1), 1 << sy, axis=0)
    top = (1 << bits) - 1
    m, offset = yuv_matrix(True, False, bits)
    cols = m.astype(np.float64).reshape(3, 3)
    yuv = np.stack([y / top - offset[0], u / top - offset[1], v / top - offset[2]], -1)
    rgb = yuv @ cols                  # = Y * 列0 + U * 列1 + V * 列2
    return pack_reference(rgb, ten_bit, row_bytes)


def main():
    from PySide6.QtWidgets import QApplication

    import player_gui
    import player_sdi

    app = QApplication(sys.argv[:1])
    w = player_gui.MainWindow()
    w.resize(1280, 800)
    w.show()
    wait(1.0)
    results = []
    if not isinstance(w.view, player_gui.GpuWindow) or w.view.sdi_program is None:
        print("GPU 描画になっていないか、SDI 用のシェーダーを作れませんでした。")
        return 1
    view = w.view
    view.pixelated = True

    # 1. 詰め方
    cases = [("yuv422p10le", 1920, 1080, True), ("yuv422p10le", 1920, 1080, False),
             ("yuv420p", 1920, 1080, True), ("yuv420p", 1280, 720, True), ("yuv420p", 1280, 720, False)]
    for n, (fmt, pw, ph, ten_bit) in enumerate(cases):
        pic = random_picture(fmt, pw, ph, n + 1)
        view.set_color(True, False)
        view.set_picture(pic)
        fake = FakeOutput(pw, ph, ten_bit)
        view.set_sdi(fake)
        t = time.perf_counter()
        while fake.data is None and time.perf_counter() - t < 3:
            app.processEvents()
        view.set_sdi(None)
        name = f"詰め方 {fmt} → {'v210' if ten_bit else '2vuy'} {pw}x{ph} (コード値の最大差)"
        if fake.data is None:
            results.append((name, "描かれませんでした", False))
            continue
        got = unpack(fake.data, pw, ph, fake.row_bytes, ten_bit)
        want = unpack(expected_packed(pic, ten_bit, fake.row_bytes), pw, ph, fake.row_bytes, ten_bit)
        worst = max(int(abs(a - b).max()) for a, b in zip(got, want))
        results.append((name, f"{worst} 段階", worst <= 1))

    # 2. 本物の機器
    modes, note = player_sdi.list_modes()
    want = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else None
    if want:
        mode = next((m for m in modes if m.name == want), None)
        if mode is None and modes:
            note = f"形式 {want} がありません (使える形式: {', '.join(m.name for m in modes)})"
    else:
        mode = next((m for m in modes if (m.width, m.height) == (1920, 1080) and not m.interlaced
                     and abs(m.fps - 60) < 0.1), None) or next(iter(modes), None)
    if mode is None:
        print(f"機器での確認は省きます: {note}")
    else:
        from tests.videos import make_video
        view.pixelated = False
        # 出力の形式によらず、デコードに余裕がある 120fps の動画を使う (機器のどのコマにも新しい絵がある)。
        # (4K 59.94fps の動画でも出せるが、画面の描画に余裕が少なく、SDI 出力とは別の理由で揺れやすい)
        w.load(make_video(Fraction(120), 1200, size=(1920, 1080), gop=120, crf=18))
        wait(0.5)
        out = player_sdi.SdiOutput(mode).open()
        w.sdi, w.sdi_sent_mark = out, 0
        view.set_sdi(out)
        view.zoom_center(2.0)
        w.play()
        wait(1.0)
        start, dropped, frame0, serial0, holds0 = out.sent, out.dropped, out.clock()[0], view.serial, out.repeats()[0]
        wait(5.0)
        frames = out.clock()[0] - frame0
        shown = view.serial - serial0          # 画面に出た新しいコマ (デコードが追いつかなければ減る)

        sent, dropped, err = out.sent - start, out.dropped - dropped, out.error
        holds = out.repeats()[0] - holds0     # 新しい絵が間に合わず、機器が同じ絵を続けたコマ
        w.pause()
        wait(0.5)
        held = out.buffered()            # 一時停止中も最後の絵を予約し続けているか
        w.stop_sdi()
        # 機器のどのコマにも新しい絵が出る (送った + 同じ絵が続いた = 機器のコマ)。画面と機器の時計の差を
        # 合わせるため、同じ絵が続く・捨てるのは 5 秒で数回まで
        results.append((f"出力 {mode.device_name} {mode.label()} 再生 5 秒",
                        f"機器 {frames} コマ / 送った {sent} / 同じ絵が続いた {holds} / 捨てた {dropped}"
                        f" / 画面の新しいコマ {shown}" + (f"、{err}" if err else ""),
                        err is None and abs(sent + holds - frames) <= 2 and holds + dropped <= 3))
        results.append(("出力 一時停止中も最後の絵を出し続ける (予約中のコマ数)", f"{held}", held >= 1))
    w.close()

    print()
    ok_all = True
    for name, value, ok in results:
        ok_all &= ok
        print(f"  {'OK ' if ok else 'NG '} {name:52s} {value}")
    print("\nすべて OK" if ok_all else "\nNG があります")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
