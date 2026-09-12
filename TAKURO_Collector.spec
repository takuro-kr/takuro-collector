# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

pw_datas, pw_bins, pw_hidden = collect_all('playwright')
bs_datas, bs_bins, bs_hidden = collect_all('bs4')

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=pw_bins + bs_bins,
    datas=pw_datas + bs_datas,
    hiddenimports=pw_hidden + bs_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TAKURO Collector',
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TAKURO Collector',
)
