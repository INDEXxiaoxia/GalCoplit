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
)
call build_venv\Scripts\activate.bat

echo [2/4] 安装固定版本依赖 + PyInstaller ...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt pyinstaller

echo [3/4] 按 GalCoplit.spec 打包 (onefile / windowed / 体积优化) ...
pyinstaller --clean --noconfirm GalCoplit.spec

echo [4/4] 检查产物大小 ...
if exist "dist\GalCoplit.exe" (
    for /f %%i in ('powershell -NoProfile -Command "(Get-Item 'dist\GalCoplit.exe').Length"') do set BYTES=%%i
    set /a MB=BYTES/1048576
    echo 产物: dist\GalCoplit.exe  大小约 !MB! MB (精确值见下)
    powershell -NoProfile -Command "$s=(Get-Item 'dist\GalCoplit.exe').Length; '{0:N2} MB' -f ($s/1MB)"
    if !MB! GEQ 150 (
        echo [警告] 体积超过 150 MB，请检查是否在脏环境里构建、或依赖版本已变化。
    ) else (
        echo [OK] 体积在 150 MB 以内。
    )
) else (
    echo [失败] 未生成 dist\GalCoplit.exe，请向上查看报错。
)

echo.
echo 打包完成。请把 config.json 与 GalCoplit.exe 放在同一目录下；
echo 首次运行会自动在同目录生成 records.db。
pause