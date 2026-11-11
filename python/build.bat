@echo off
chcp 65001 >nul
rem Zoom Video Player をビルドする。出来上がりは dist\ZoomVideoPlayer\ZoomVideoPlayer.exe
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv || goto :error
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller || goto :error
rem SDI 出力用のライブラリ。DeckLink SDK か Visual Studio が見つからなければ SDI 出力なしでビルドする
call build_sdi.bat || echo SDI 出力なしでビルドします
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --distpath dist --workpath build ZoomVideoPlayer.spec || goto :error
echo.
echo ビルド完了: %~dp0dist\ZoomVideoPlayer\ZoomVideoPlayer.exe
exit /b 0
:error
echo ビルドに失敗しました。
exit /b 1
