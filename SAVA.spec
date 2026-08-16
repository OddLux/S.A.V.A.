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
    #
    # dp-257: 'config' was previously bundled as data too. Nothing at
    # runtime ever reads it -- config/settings.py resolves to %APPDATA%
    # when frozen and falls back to its own in-source DEFAULT_SETTINGS dict;
    # core/artnet_config.py does the same with an in-source DEFAULT_INI
    # string. So the entry was dead weight AND a real privacy leak: whatever
    # happened to sit in the dev's config/ folder at build time -- including
    # sava_settings.json, which is gitignored but still a local file --
    # would get copied verbatim into the shipped exe. Removed.
    datas=[('assets', 'assets'), ('VERSION', '.')],
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
