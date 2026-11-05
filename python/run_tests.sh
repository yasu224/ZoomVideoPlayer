#!/bin/bash
# 自動テストを実行する (画面は出さず、CPU 描画で確かめる)。macOS / Linux 用
cd "$(dirname "$0")"
exec .venv/bin/python run_tests.py
