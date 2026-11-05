"""Zoom Video Player (Python 版) の起動スクリプト。

使い方: python zoom_player.py [動画ファイル]

デコード用の子プロセスはこのファイルを読み直すので、ここでは何も読み込まず、
画面側のモジュールは main の中で読み込む (子プロセスの起動を軽くするため)。
"""
import multiprocessing as mp
import sys


def main():
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    from player_gui import MainWindow

    class App(QApplication):
        """macOS では、Finder で動画を開いたり Dock のアイコンへドロップしたりすると、
        ファイルが引数ではなく FileOpen の知らせで届く。それを受けて開く。"""

        window = None
        pending = None

        def event(self, e):
            if e.type() == QEvent.FileOpen and e.file():
                if self.window:
                    self.window.load(e.file())
                else:
                    self.pending = e.file()
                return True
            return super().event(e)

    app = App(sys.argv)
    app.setApplicationName("Zoom Video Player")
    win = MainWindow()
    app.window = win
    win.show()
    # macOS で Finder から起動したときに付く -psn_... は動画ではない
    files = [a for a in sys.argv[1:] if not a.startswith("-psn")]
    path = files[0] if files else app.pending
    if path:
        win.load(path)
    sys.exit(app.exec())


if __name__ == "__main__":
    mp.freeze_support()
    main()
