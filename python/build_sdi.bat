@echo off
chcp 65001 >nul
rem Blackmagic の SDI 出力用ライブラリ (sdi\zvp_decklink.dll) をビルドする。Windows 用。
rem 使い方: build_sdi.bat [DeckLink SDK のフォルダ]
rem   SDK は Blackmagic のサイトから入手する (「Desktop Video SDK」)。フォルダを省くと
rem   環境変数 DECKLINK_SDK、なければこのリポジトリの隣の「Blackmagic DeckLink SDK *」を探す。
rem   Visual Studio (または Build Tools) の「C++ によるデスクトップ開発」が要る (midl と cl を使う)。
rem 実行には Blackmagic Desktop Video (ドライバ) が入っていること。
setlocal
cd /d "%~dp0"

set "SDK=%~1"
if "%SDK%"=="" set "SDK=%DECKLINK_SDK%"
if "%SDK%"=="" for /d %%D in ("..\..\Blackmagic DeckLink SDK*") do set "SDK=%%~fD"
if not exist "%SDK%\Win\include\DeckLinkAPI.idl" (
  echo DeckLink SDK が見つかりません。SDK のフォルダを指定してください: build_sdi.bat ^<SDK のフォルダ^>
  exit /b 1
)

rem Visual Studio の 64bit のビルド環境を読み込む (すでに読み込んであればそのまま使う)
where cl >nul 2>nul
if not errorlevel 1 goto :have_cl
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" goto :no_vs
for /f "usebackq delims=" %%I in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSDIR=%%I"
if not defined VSDIR goto :no_vs
call "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" >nul || goto :error
:have_cl

rem /MT: C++ の実行時ライブラリを DLL に組み込む (Python には msvcp140.dll が入っていないため)
rem SDK の DeckLinkAPI.idl から、C++ のヘッダ (DeckLinkAPI.h) と ID の定義 (DeckLinkAPI_i.c) を作る
set "GEN=%~dp0build\sdi"
if not exist "%GEN%" mkdir "%GEN%"
midl /nologo /env x64 /I "%SDK%\Win\include" /out "%GEN%" /h DeckLinkAPI.h /iid DeckLinkAPI_i.c /tlb DeckLinkAPI.tlb "%SDK%\Win\include\DeckLinkAPI.idl" || goto :error

cl /nologo /O2 /EHsc /MT /LD /std:c++17 /utf-8 /DNDEBUG /I "%GEN%" sdi\decklink_bridge.cpp "%GEN%\DeckLinkAPI_i.c" /Fo"%GEN%\\" /Fe"sdi\zvp_decklink.dll" /link ole32.lib oleaut32.lib || goto :error
echo.
echo ビルド完了: %~dp0sdi\zvp_decklink.dll
exit /b 0
:no_vs
echo Visual Studio (または Build Tools) の「C++ によるデスクトップ開発」が見つかりません。
exit /b 1
:error
echo ビルドに失敗しました。
exit /b 1
