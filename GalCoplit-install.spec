# -*- mode: python ; coding: utf-8 -*-
# GalCoplit 安装版（onedir）：启动比 onefile 快，配合 Inno Setup 生成 Setup.exe。
# 用户数据（config / db / paddle 插件）不打进包，覆盖安装时自然保留。
import os
from PyInstaller.utils.hooks import collect_all

SPECDIR = os.path.dirname(os.path.abspath(SPEC))

datas = []
binaries = []
hiddenimports = []

for pkg in ('rapidocr_onnxruntime', 'onnxruntime'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

d, b, h = collect_all('cv2')
datas += d
hiddenimports += h
for entry in b:
    if 'opencv_videoio_ffmpeg' in entry[0].lower():
        continue
    binaries.append(entry)

# 运行时窗口/任务栏图标
icon_src = os.path.join(SPECDIR, 'icon.ico')
if os.path.isfile(icon_src):
    datas += [(icon_src, '.')]

excludes = [
    'paddle', 'paddleocr', 'paddlepaddle',
    'torch', 'torchvision', 'scipy', 'pandas', 'matplotlib', 'pypdfium2',
    'pytesseract', 'winrt',
    'PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebChannel', 'PyQt6.QtWebSockets',
    'PyQt6.QtQml', 'PyQt6.QtQuick', 'PyQt6.QtQuickWidgets', 'PyQt6.QtQuick3D',
    'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets',
    'PyQt6.QtDesigner', 'PyQt6.QtPdf', 'PyQt6.QtPdfWidgets',
    'PyQt6.QtBluetooth', 'PyQt6.QtNfc', 'PyQt6.QtSensors', 'PyQt6.QtSerialPort',
    'PyQt6.QtPositioning', 'PyQt6.QtSql', 'PyQt6.QtTest',
    'unittest', 'pydoc', 'doctest', 'tkinter',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
a.binaries = [b for b in a.binaries if 'opencv_videoio_ffmpeg' not in b[0].lower()]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='GalCoplit',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_src if os.path.isfile(icon_src) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='GalCoplit-install',
)
