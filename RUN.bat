@echo off
title PS5 FFPFSC ULTRA v1.0
echo.
echo  PS5 FFPFSC ULTRA v1.0
echo  IMPORTANT: Extract the ZIP first. Do not run from inside the ZIP.
echo.
py -m pip install customtkinter pillow mkpfs tkinterdnd2 py7zr rarfile
py PS5_FFPFSC_ULTRA_v1.0.py
if errorlevel 1 python PS5_FFPFSC_ULTRA_v1.0.py
pause
