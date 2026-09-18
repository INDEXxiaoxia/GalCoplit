# -*- mode: python ; coding: utf-8 -*-
# GalCoplit 打包规格：onefile + windowed，体积优化（目标 < 150 MB）。
# 关键：只在干净的 build_venv 里构建（numpy<2.0 + onnxruntime==1.18.1），
# 并用 excludes 排除不会用到的可选 OCR 后端与未使用的 PyQt6 扩展绑定，
# 同时剔除 opencv 的视频读写 ffmpeg DLL（OCR 只用图像处理，不需要它）。
import os
from PyInstaller.utils.hooks import collect_all

SPECDIR = os.path.dirname(os.path.abspath(SPEC))

datas = []
binaries = []
hiddenimports = []

# RapidOCR 全家桶：模型 + config.yaml + 隐藏导入
for pkg in ('rapidocr_onnxruntime', 'onnxruntime'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# cv2：收集二进制/数据/隐藏导入，但去掉 video ffmpeg DLL（~29 MB，OCR 用不到）
d, b, h = collect_all('cv2')
datas += d
hiddenimports += h
for entry in b:
    if 'opencv_videoio_ffmpeg' in entry[0].lower():
        continue
    binaries.append(entry)

# 运行时窗口/任务栏图标（exe 资源图标另见 EXE icon=）
icon_src = os.path.join(SPECDIR, 'icon.ico')
if os.path.isfile(icon_src):
    datas += [(icon_src, '.')]

excludes = [
    # 不会打包进 exe 的可选 OCR 后端 / 重型库（防止脏环境被卷入）
    'paddle', 'paddleocr', 'paddlepaddle',
    'torch', 'torchvision', 'scipy', 'pandas', 'matplotlib', 'pypdfium2',
    'pytesseract', 'winrt',
    # 未使用的 PyQt6 扩展绑定（体积大头）
    'PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebChannel', 'PyQt6.QtWebSockets',
    'PyQt6.QtQml', 'PyQt6.QtQuick', 'PyQt6.QtQuickWidgets', 'PyQt6.QtQuick3D',
    'PyQt6.QtMultimedia', 'PyQt6.QtMultimediaWidgets',
    'PyQt6.QtDesigner', 'PyQt6.QtPdf', 'PyQt6.QtPdfWidgets',
    'PyQt6.QtBluetooth', 'PyQt6.QtNfc', 'PyQt6.QtSensors', 'PyQt6.QtSerialPort',
    'PyQt6.QtPositioning', 'PyQt6.QtSql', 'PyQt6.QtTest',
    # 无关紧要的标准库
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

# 标准 hook-cv2.py 仍会把 video ffmpeg DLL 收进来，这里在最终二进制清单里二次过滤（OCR 用不到视频读写）
a.binaries = [b for b in a.binaries if 'opencv_videoio_ffmpeg' not in b[0].lower()]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='GalCoplit',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_src if os.path.isfile(icon_src) else None,
)
