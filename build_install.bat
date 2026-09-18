@echo off
REM Installer build launcher. Logic lives in build.py (--install).
cd /d "%~dp0"
python build.py --install %*
if errorlevel 1 (
    echo.
    echo Build failed - see the messages above.
)
pause
