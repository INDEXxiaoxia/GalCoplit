# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['old_data_import_app\\__main__.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=['theme', 'recorder', 'story_tree_page'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'paddle', 'transformers', 'sklearn', 'matplotlib'],
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
    name='GalCoplit-Import',
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
    icon=['icon.ico'],
)
