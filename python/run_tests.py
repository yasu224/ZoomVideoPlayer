"""自動テストを実行する: .venv\\Scripts\\python.exe run_tests.py

画面は出さずに (Qt の offscreen)、CPU 描画で動かす。GPU 描画と実際の fps は
tools\\check_gpu.py で、本物の画面に出して確かめる。

デコード用の子プロセスはこのファイルを読み直すので、テストの読み込みは main の中で行う。
"""
import os
import sys
import unittest


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("ZVP_RENDERER", "cpu")
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    suite = unittest.defaultTestLoader.discover(os.path.join(here, "tests"), top_level_dir=here)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
