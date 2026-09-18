# -*- mode: python ; coding: utf-8 -*-
# 将 paddle_ocr_worker.py 打成独立旁路 exe。
# 必须在已安装 paddleocr + paddlepaddle(或 gpu) 的干净 venv 中构建，
# 产物放到主程序旁的 paddle_ocr_cpu/ 或 paddle_ocr_gpu/ 目录。
import os
from PyInstaller.utils.hooks import collect_all

SPECDIR = os.path.dirname(os.path.abspath(SPEC))
icon_src = os.path.join(SPECDIR, 'icon.ico')

datas = []
binaries = []
hiddenimports = []

for pkg in ("paddleocr", "paddle", "cv2", "PIL", "numpy", "skimage", "shapely", "pyclipper", "imgaug"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ['paddle_ocr_worker.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PyQt6', 'PyQt5', 'tkinter', 'matplotlib', 'torch', 'torchvision',
        'rapidocr_onnxruntime', 'onnxruntime',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PaddleOcrWorker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_src if os.path.isfile(icon_src) else None,
)
