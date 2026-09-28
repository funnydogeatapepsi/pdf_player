@echo off
rem Run a built pdf_player.exe with a bare-bones PATH, so it can't borrow DLLs or programs (ffmpeg,
rem conda's Library\bin, ...) from your dev environment. If it works here, the bundle is self-contained.
rem
rem Usage: double-click to test build\dist\pdf_player.exe (what build\make.py produces),
rem        or drag another exe onto this file - e.g. the GitHub artifact - to test that one instead.

setlocal

set "EXE=%~dp0dist\pdf_player.exe"
if not "%~1"=="" set "EXE=%~1"

if not exist "%EXE%" (
    echo Not found: %EXE%
    echo Build it first with:  python build\make.py
    goto :end
)

rem Only the Windows system directories - everything else (conda, Python, ffmpeg) is gone for this window.
set "PATH=%SystemRoot%\system32;%SystemRoot%"

echo Running: %EXE%
echo PATH:    %PATH%
echo ------------------------------------------------------------
"%EXE%"
set "RC=%ERRORLEVEL%"
echo ------------------------------------------------------------
echo Exit code: %RC%

:end
echo.
pause
endlocal
