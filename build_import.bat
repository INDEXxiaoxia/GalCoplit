@echo off
REM ASCII-only. Packs the old-data converter into one exe: dist\GalCoplit-Import.exe
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --onefile --windowed --name GalCoplit-Import --icon icon.ico --paths . --hidden-import theme --hidden-import recorder --hidden-import story_tree_page --exclude-module torch --exclude-module paddle --exclude-module transformers --exclude-module sklearn --exclude-module matplotlib old_data_import_app\__main__.py
if errorlevel 1 (
    echo.
    echo Build failed - see the messages above.
    pause
    exit /b 1
)
echo.
echo Output: dist\GalCoplit-Import.exe
pause
