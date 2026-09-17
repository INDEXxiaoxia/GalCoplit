@echo off
REM ASCII-only launcher on purpose: all build logic and Chinese messages live in
REM build.py, because cmd.exe misparses long non-ASCII lines under chcp 65001.
cd /d "%~dp0"
python build.py %*
if errorlevel 1 (
    echo.
    echo Build failed - see the messages above.
)
pause
