"""再生のリアルタイム性を測る: .venv\\Scripts\\python.exe tools\\check_realtime.py [画面番号] [--fps 144]

フレーム番号入りの 1080p 動画 (既定 120fps、--fps で変えられる) を再生し、画面に出たコマの番号を毎回読んで数える。
- 飛んだコマ数と、表示が止まった最長時間 (等速 8 秒)
- 再生ボタンを押してから最初のコマが出るまで (停止直後・シーク直後)
- A-B 区間ループで折り返すときの止まり (B の次に A が出るまでの時間)
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


def main():
    from PySide6.QtWidgets import QApplication

    import player_gui
    from tests.videos import make_video, read_index_picture

    args = sys.argv[1:]
    fps = 120
    if "--fps" in args:
        i = args.index("--fps")
        fps = int(args[i + 1])
        del args[i:i + 2]
    app = QApplication(sys.argv[:1])
    screens = app.screens()
    screen = screens[int(args[0])] if args else max(screens, key=lambda s: s.refreshRate())
    w = player_gui.MainWindow()
    w.show()
    w.windowHandle().setScreen(screen)
    w.setGeometry(screen.availableGeometry())
    w.showMaximized()
    wait(1.0)
    w.load(make_video(Fraction(fps), 1800, size=(1920, 1080), gop=fps, crf=18))
    wait(1.0)
    hz = screen.refreshRate()
    print(f"動画: {fps} fps  画面: {screen.name()} {hz:.0f} Hz  描画: {w.view.renderer}")

    # 画面に描いたコマの番号と時刻を記録する
    log = []
    view = w.view
    original = view.count_painted

    def record():
        if view.serial != view._painted_serial and view.picture is not None:
            log.append((time.perf_counter(), read_index_picture(view.picture)))
        original()
    view.count_painted = record

    def first_frame_delay(start_frame):
        w.seek_paused(start_frame / fps)
        wait(0.6)                         # 停止中の状態 (先読みがあるならその分) を作る
        log.clear()
        t = time.perf_counter()
        w.play()
        while not any(i > start_frame for _, i in log) and time.perf_counter() - t < 5:
            app.processEvents()
        w.pause()
        moving = [ts for ts, i in log if i > start_frame]
        return (moving[0] - t) * 1000 if moving else float("nan")

    results = []
    # 1. 等速 8 秒
    w.seek_paused(0)
    wait(0.6)
    log.clear()
    w.play()
    wait(8.0)
    w.pause()
    frames = [i for _, i in log]
    times = [ts for ts, _ in log]
    skipped = sum(max(0, b - a - 1) for a, b in zip(frames, frames[1:]))
    longest = max((b - a for a, b in zip(times, times[1:])), default=0) * 1000
    expected_gap = 1000 / min(fps, hz)
    results.append(("等速 8 秒で飛んだコマ", f"{skipped} / {frames[-1] - frames[0] + 1} コマ"))
    results.append(("表示が止まった最長時間", f"{longest:.1f} ms (1 コマは {expected_gap:.1f} ms)"))

    # 2. 再生開始の速さ
    results.append(("再生開始まで (停止中の位置から)", f"{first_frame_delay(600):.0f} ms"))
    results.append(("再生開始まで (別の位置へシーク直後)", f"{first_frame_delay(1300):.0f} ms"))

    # 3. A-B ループの折り返し
    w.seek_paused(300 / fps)
    w.set_point("A")
    w.seek_paused(420 / fps)
    w.set_point("B")
    w.seek_paused(380 / fps)
    wait(0.6)
    log.clear()
    w.play()
    wait(4.0)
    w.pause()
    wraps = [(b[0] - a[0]) * 1000 for a, b in zip(log, log[1:]) if b[1] < a[1]]
    results.append(("A-B ループの折り返しで止まった時間", ", ".join(f"{x:.0f}" for x in wraps) + " ms"
                    if wraps else "折り返しなし"))
    w.clear_ab()
    w.close()

    print()
    for name, value in results:
        print(f"  {name:28s} {value}")


if __name__ == "__main__":
    sys.exit(main())
