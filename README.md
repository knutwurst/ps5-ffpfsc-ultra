# PS5 FFPFSC PRO

A desktop app that packs PS5 game dumps into `.ffpfsc` containers for ShadowMountPlus, and unpacks them again. Feed it a game folder, an `.exfat`/`.ffpkg` disk image, or an archive (ZIP, RAR, or 7z, password-protected included), and it produces a compressed `.ffpfsc` you can mount.

Built by Knutwurst on top of the Bizkut `ps5-ffpfs-cli` backend. Runs on macOS, Windows, and Linux.

Current version: 1.0.16

## What it does

- Packs a decrypted PS5 game folder (`eboot.bin` + `sce_sys/param.json`) or an `.exfat`/`.ffpkg` disk image into a compressed `.ffpfsc`.
- Reads ZIP, RAR, and 7z archives directly. Multi-part RAR sets and password-protected archives work out of the box. macOS carries a self-contained native UnRAR module, so there is no dependency on an external `unrar` or 7-Zip binary.
- Keeps a saved list of archive passwords and tries them automatically, so a recurring scene password never has to be retyped.
- Names the output after the game (`Game Name [v01.004] [PPSA12345].ffpfsc`) so the files stay findable.
- Packs a release folder in one go. It finds the game inside the folder (even when it sits in a RAR or 7z), recreates the folder at the destination, and copies the DLCs and other extras next to the finished `.ffpfsc`.
- Unpacks `.ffpfs` / `.ffpfsc` back into a folder.
- Queue with per-stage progress, a heartbeat, and a live log.

## Requirements

- Python 3.10 or newer, to run from source or to build.
- A C++ compiler for the bundled UnRAR module, needed only when building: Xcode Command Line Tools on macOS, build-essential on Linux, MSVC on Windows.

## Run from source

Windows: double-click `RUN.bat`.

macOS and Linux:

```bash
python3 -m pip install customtkinter pillow tkinterdnd2 py7zr rarfile cryptography
python3 -m pip install ./backend/unrar
python3 PS5_FFPFSC_PRO_v1.0.py
```

## Build a standalone app

macOS, produces `dist/PS5 FFPFSC PRO.app`:

```bash
./BUILD_MACOS_APP.sh
```

Windows, produces `dist\PS5_FFPFSC_PRO.exe`:

```
BUILD_EXE.bat
```

The macOS app is ad-hoc signed. On a Mac other than the one it was built on, clear the quarantine flag before the first launch:

```bash
xattr -dr com.apple.quarantine "PS5 FFPFSC PRO.app"
```

## Sources and credits

This is not a fork. It bundles and builds on the work below, with thanks to the authors:

- [ps5-ffpfs-cli](https://github.com/bizkut/ps5-ffpfs-cli) by Bizkut. The CLI and backend this GUI drives (MIT).
- [MkPFS](https://github.com/PSBrew/MkPFS) by PSBrew. The PFS image builder used for packing and compression.
- UnRAR by RARLAB ([rarlab.com](https://www.rarlab.com/)). Vendored as C++ source for the built-in RAR module, under the UnRAR license (free for extraction; it may not be used to build a RAR-compatible compressor).
- ShadowMountPlus, the loader the `.ffpfsc` containers target.

Python libraries used: customtkinter, py7zr, rarfile, tkinterdnd2, Pillow, cryptography.

## License

The GUI and app code are MIT. The vendored UnRAR source keeps RARLAB's UnRAR license. MkPFS keeps its own license. See the upstream projects for their terms.
