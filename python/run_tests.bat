@echo off
chcp 65001 >nul
rem 自動テストを実行する (画面は出さず、CPU 描画で確かめる)
cd /d "%~dp0"
".venv\Scripts\python.exe" run_tests.py
exit /b %errorlevel%
