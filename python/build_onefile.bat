@echo off
chcp 65001 >nul
rem Zoom Video Player を exe 1 本にビルドする。出来上がりは dist\ZoomVideoPlayer.exe
rem (Python などはすべて exe の中に入る。起動のたびに一時フォルダへ展開するので、フォルダ形式より起動が数秒遅い)
setlocal
set ZVP_ONEFILE=1
call "%~dp0build.bat"
exit /b %errorlevel%
