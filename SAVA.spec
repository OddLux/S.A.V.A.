# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['PyQt6.sip']
hiddenimports += collect_submodules('PyQt6')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # dp-227: VERSION is bundled at the bundle ROOT ('.') -- core/version.py
    # reads it from sys._MEIPASS in a frozen build, and without it the About
    # dialog falls back to its "0.0.0-unknown" sentinel.
    datas=[('assets', 'assets'), ('config', 'config'), ('VERSION', '.')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'PIL', 'pandas', 'IPython', 'jupyter', 'test', 'unittest', 'pydoc_data'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SAVA',
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
    icon=['assets\\sava.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SAVA',
)
