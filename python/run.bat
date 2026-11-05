@echo off
chcp 65001 >nul
rem Zoom Video Player (Python 版) を起動する。動画ファイルをこの bat にドロップしても開ける。
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo 初回セットアップ中です...
  python -m venv .venv || goto :error
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0zoom_player.py" %*
exit /b 0
:error
echo セットアップに失敗しました。Python 3.10 以上が必要です。
pause
