"""本物の画面で GPU 描画を確かめる: .venv\\Scripts\\python.exe tools\\check_gpu.py [画面番号]
(macOS は .venv/bin/python tools/check_gpu.py [画面番号])

1. 色: 値の分かっている色の帯を GPU で描き、BT.709 の計算結果と比べる
2. コマ: コマ送りしたとき、GPU が描いた絵に正しいフレーム番号が出ているか
3. 速さ: 1080p / 120fps の動画を等速・0.5 倍・0.25 倍で、144fps と 240fps の動画を等速で再生し、
   描画できたコマ数を測る (目標は動画の fps と画面の Hz の小さい方)

画面番号を省くと、書き換え回数 (Hz) がいちばん高い画面を使う。結果は最後にまとめて出す。
デコード用の子プロセスはこのファイルを読み直すので、重い読み込みは main の中で行う。
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


def wait_painted(w, timeout=2.0):
    """いま表示を頼んだコマが GPU で描き終わるまで待ち、その時間 (秒) を返す。"""
    from PySide6.QtWidgets import QApplication
    t = time.perf_counter()
    while w.view._painted_serial != w.view.serial and time.perf_counter() - t < timeout:
        QApplication.processEvents()
    return time.perf_counter() - t


def grab_index(w):
    """GPU が描いた絵 (フレームバッファ) から、左上のブロックのフレーム番号を読む。

    画面は 2 枚のバッファを入れ替えて表示しているので、描き終えた直後に読むと裏側 (1 つ前のコマ)
    が読めてしまう。同じコマをもう一度描かせてから読む。
    """
    from tests.videos import BITS, BLOCK
    wait_painted(w)
    w.view.update()
    wait(0.05)
    shot = w.view.grabFramebuffer()
    dpr = shot.devicePixelRatio()
    v = w.view
    n = 0
    for bit in range(BITS):
        x = v.tx + (bit * BLOCK + BLOCK / 2) * v.s
        y = v.ty + (BLOCK / 2) * v.s
        if shot.pixelColor(int(x * dpr), int(y * dpr)).green() > 128:
            n |= 1 << bit
    return n


def main():
    from PySide6.QtWidgets import QApplication

    import player_gui
    from tests.videos import bt709_rgb, make_bars, make_video

    app = QApplication(sys.argv[:1])
    screens = app.screens()
    screen = screens[int(sys.argv[1])] if len(sys.argv) > 1 else max(screens, key=lambda s: s.refreshRate())
    w = player_gui.MainWindow()
    w.show()
    w.windowHandle().setScreen(screen)
    w.setGeometry(screen.availableGeometry())
    w.showMaximized()
    wait(1.0)
    results = []
    print(f"画面: {screen.name()}  {screen.refreshRate():.0f} Hz  描画: {w.view.renderer}")
    if not isinstance(w.view, player_gui.GpuWindow):
        print("GPU 描画になっていません。OpenGL が使えない可能性があります。")
        return 1

    # 1. 色 (8bit 4:2:0 と、業務用カメラと同じ 10bit 4:2:2)
    from player_core import PLANAR, plane_views
    for pix_fmt in ("yuv420p", "yuv422p10le"):
        path, _ = make_bars(pix_fmt=pix_fmt)
        w.load(path)
        wait(0.5)
        shot = w.view.grabFramebuffer()
        dpr = shot.devicePixelRatio()
        rect = w.view.image_rect()
        pic = w.cur_frame
        bits = PLANAR[pic.fmt][3]
        y, u, v = plane_views(pic.data, pic.fmt, pic.w, pic.h)
        worst = 0
        for i in range(8):
            fx, fy = (i + 0.5) / 8, 0.5
            yy = int(y[int(fy * y.shape[0]), int(fx * y.shape[1])])
            uu = int(u[int(fy * u.shape[0]), int(fx * u.shape[1])])
            vv = int(v[int(fy * v.shape[0]), int(fx * v.shape[1])])
            c = shot.pixelColor(int((rect.left() + fx * rect.width()) * dpr), int((rect.top() + fy * rect.height()) * dpr))
            expected = bt709_rgb(yy, uu, vv, bits)
            worst = max(worst, max(abs(a - b) for a, b in zip(expected, (c.red(), c.green(), c.blue()))))
        results.append((f"色 {pic.fmt} (BT.709 との最大差)", f"{worst} 段階", worst <= 2))

    # 2. コマ (GPU が描いた絵に出ている番号)
    path = make_video(Fraction(120), 600, size=(1920, 1080), gop=120, crf=18)
    w.load(path)
    wrong = []
    delays = []
    w.seek_paused(130 / 120)
    wait_painted(w)
    for n in [1] * 5 + [-1] * 20:
        w.step(n)
        delays.append(wait_painted(w))
        got = grab_index(w)
        if got != w.frame_index():
            wrong.append((w.frame_index(), got))
    results.append(("コマ送り (25 回、キーフレームをまたぐ)", f"ずれ {len(wrong)} 回", not wrong))
    slowest = max(delays[1:]) * 1000      # 最初の 1 回はまとめてデコードする分を含む
    results.append(("コマ送りから表示までの時間 (最大)", f"{slowest:.0f} ms", slowest < 100))

    # 3. 速さ (120fps と、144Hz の画面と同じ 144fps と、画面の Hz を超える 240fps)
    hz = screen.refreshRate()
    cases = [(120, 1), (120, 0.5), (120, 0.25), (144, 1), (240, 1)]
    for fps, speed in cases:
        if fps != 120:
            w.load(make_video(Fraction(fps), fps * 10, size=(1920, 1080), gop=fps, crf=18))
            wait(1.0)
        w.speed_combo.setCurrentIndex(player_gui.SPEEDS.index(speed))
        w.seek_paused(0)
        w.play()
        wait(1.0)
        w.update_stats()
        shown = decoded = 0.0
        for _ in range(4):
            wait(0.5)
            text = w.view.stats_text
            shown += float(text.split()[1])
            decoded += float(text.split("デコード ")[1].split()[0])
        w.pause()
        shown, decoded = shown / 4, decoded / 4
        want = min(fps * speed, hz)
        results.append((f"再生 {fps}fps {speed}x (目標 {want:.0f} コマ/秒)", f"描画 {shown:.1f} / デコード {decoded:.0f}",
                        shown >= want * 0.95))
    w.close()

    print()
    ok_all = True
    for name, value, ok in results:
        ok_all &= ok
        print(f"  {'OK ' if ok else 'NG '} {name:36s} {value}")
    print("\nすべて OK" if ok_all else "\nNG があります")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
