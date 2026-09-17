@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo  GalCoplit 打包脚本  (目标: dist\GalCoplit.exe 小于 150 MB)
echo ============================================================
echo.

echo [1/4] 创建/复用干净的构建虚拟环境 build_venv ...
if not exist "build_venv\Scripts\python.exe" (
    python -m venv build_venv
    if errorlevel 1 (
        echo [失败] 创建虚拟环境失败，请确认 python 已加入 PATH。
        pause
        exit /b 1
    )
)
call "build_venv\Scripts\activate.bat"

echo [2/4] 安装固定版本依赖 + PyInstaller ...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo [失败] 依赖安装失败，请检查网络或 requirements.txt。
    pause
    exit /b 1
)

echo [3/4] 按 GalCoplit.spec 打包 (onefile / windowed / 体积优化) ...
pyinstaller --clean --noconfirm GalCoplit.spec
if errorlevel 1 (
    echo [失败] PyInstaller 打包失败，请向上查看报错。
    pause
    exit /b 1
)

echo [4/4] 检查产物大小 ...
if not exist "dist\GalCoplit.exe" (
    echo [失败] 未生成 dist\GalCoplit.exe，请向上查看报错。
    pause
    exit /b 1
)
for %%I in ("dist\GalCoplit.exe") do set "BYTES=%%~zI"
set /a MB=BYTES/1048576
echo 产物: dist\GalCoplit.exe  约 !MB! MB (!BYTES! 字节)
if !MB! GEQ 150 (
    echo [警告] 体积超过 150 MB，请检查是否在脏环境里构建、或依赖版本已变化。
) else (
    echo [OK] 体积在 150 MB 以内。
)

echo.
echo 打包完成。请把 config.json 与 GalCoplit.exe 放在同一目录下；
echo 首次运行会自动在同目录生成 records.db。
pause
