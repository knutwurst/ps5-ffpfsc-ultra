# -*- mode: python ; coding: utf-8 -*-

import re as _re
from PyInstaller.utils.hooks import collect_all, collect_data_files

# Single source of truth: read APP_VERSION straight from the app script so the
# bundle version always matches what the UI shows (bump APP_VERSION only).
_m = _re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']',
                open("PS5_FFPFSC_ULTRA_v1.0.py", encoding="utf-8").read(), _re.M)
APP_VERSION = _m.group(1) if _m else "1.0"


datas = [
    ("backend", "backend"),
]
datas += collect_data_files("customtkinter")
datas += collect_data_files("tkinterdnd2")
cryptography_datas, cryptography_binaries, cryptography_hiddenimports = collect_all("cryptography")
datas += cryptography_datas


a = Analysis(
    ["PS5_FFPFSC_ULTRA_v1.0.py"],
    pathex=["backend", "backend/unrar"],
    binaries=cryptography_binaries,
    datas=datas,
    hiddenimports=cryptography_hiddenimports + [
        "py7zr",
        "py7zr.helpers",
        "py7zr.compressor",
        "rarfile",
        "unrar",
        "unrar.rarfile",
        "unrar._unrar",
        "tkinterdnd2",
        "psutil",
        "PIL._tkinter_finder",
        "cryptography",
        "cryptography.hazmat.primitives.ciphers",
        "mkpfs",
        "mkpfs.cli",
        "mkpfs.pfs",
        "mkpfs.utils",
        "mkpfs.logging",
        "mkpfs.pbar",
        "make_fself",
        "fake_sign",
        "argparse",
        "contextlib",
        "dataclasses",
        "enum",
        "hashlib",
        "hmac",
        "json",
        "multiprocessing",
        "queue",
        "shutil",
        "struct",
        "tempfile",
        "uuid",
        "zlib",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PS5 FFPFSC ULTRA",
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
    name="PS5 FFPFSC ULTRA",
)

app = BUNDLE(
    coll,
    name="PS5 FFPFSC ULTRA.app",
    icon=None,
    bundle_identifier="com.kingdkak.ps5ffpfscpro",
    info_plist={
        "CFBundleDisplayName": "PS5 FFPFSC ULTRA",
        "CFBundleName": "PS5 FFPFSC ULTRA",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    },
)
