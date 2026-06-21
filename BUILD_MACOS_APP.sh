#!/usr/bin/env bash
set -euo pipefail

python3 -m pip install customtkinter pillow pyinstaller tkinterdnd2 py7zr rarfile cryptography psutil
python3 -m pip install ./backend/unrar
(cd backend/unrar && python3 setup.py build_ext --inplace)
python3 -m PyInstaller --clean --noconfirm PS5_FFPFSC_PRO_macos.spec

echo "Built: dist/PS5 FFPFSC PRO.app"
