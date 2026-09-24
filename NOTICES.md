# NOTICES

## Trademark

**Not affiliated with, endorsed by, or sponsored by Sony Interactive
Entertainment Inc.** "PlayStation", "PS4", "PS5", "Prospero", and related
marks are trademarks or registered trademarks of Sony Interactive
Entertainment Inc. Use of these names in this repository is nominative fair
use to identify the format and console this tool interoperates with.

## User responsibility

This tool builds and processes PS5 package formats (`.ffpfsc`, `.ffpfs`,
`.pkg`) from files that **you supply**. It does not decrypt, decode,
distribute, or download any Sony-owned content, firmware, executables, or
cryptographic keys. The console-side jailbreak stack this tool's output is
compatible with (kstuff-lite, etaHEN, and similar) is developed and hosted
by third parties and is **not** included with this software.

You are responsible for having the legal right to any content you process
with this tool, in your jurisdiction:

  * dumps of games you own on consoles you own;
  * homebrew you have written yourself or that has been licensed for such
    use by its author;
  * fake packages you build for those two categories.

Circumventing technological protection measures may be regulated
differently where you live. Nothing in this repository is legal advice.

## Bundled third-party components

Each component below is bundled as source or as a compiled artifact.
Their upstream authors and licenses:

| Component | Upstream | License | Location in this repo |
|---|---|---|---|
| **MkPFS** 0.0.8 | [PSBrew/MkPFS](https://github.com/PSBrew/MkPFS) | **GPL-3.0-or-later** | `backend/mkpfs/` (LICENSE: `backend/mkpfs/LICENSE`) |
| **LibProsperoPkg** 1.2.0 | [SvenGDK/LibProsperoPKG](https://github.com/SvenGDK/LibProsperoPKG) via drakmor's a53-fpkg 0.5 release | **GPL-3.0-or-later** | embedded into `backend/native/ffpfsc-pkg-tool` (LICENSE: `backend/native/LICENSE.LibProsperoPkg`, NOTICE: `backend/native/NOTICE.LibProsperoPkg`, additional context: `backend/native/NOTICE.fpkg`) |
| **UnRAR** sources | [rarlab.com](https://www.rarlab.com/) by Alexander Roshal / RARLAB | **UnRAR license** (free for extraction; **may not** be used to reverse-engineer the RAR compression algorithm or to build a RAR-compatible compressor) | `backend/unrar/src/` (LICENSE: `backend/unrar/license.txt`) |
| **make_fself** | Alex Free's [ps5-make-fself-recursive](https://github.com/alex-free/ps5-make-fself-recursive), which redistributes `make_fself.py` from the [ps5-payload-dev SDK](https://github.com/ps5-payload-dev/sdk), originally by flatz | **BSD-3-Clause** | `backend/make_fself.py` (attribution in the file header) |
| **Bizkut's ps5-ffpfs-cli** (backend API + shell-out compatibility) | [bizkut/ps5-ffpfs-cli](https://github.com/bizkut/ps5-ffpfs-cli) | **No explicit license upstream.** This project does not redistribute the upstream binary; it re-implements a compatible CLI shell-out API around a vendored, patched fork of MkPFS. Bizkut is credited in the README as the originator of the backend design. If you are Bizkut and want a specific licensing statement or attribution change, please open an issue. | (design credit only; no upstream source or binary bundled) |

## Runtime dependencies (loaded at runtime, not redistributed here)

* **Python** and its standard library — Python Software Foundation License (PSF-2.0).
* **customtkinter** — MIT.
* **tkinterdnd2** — MIT.
* **Pillow** — HPND (permissive).
* **psutil** — BSD-3-Clause.
* **py7zr** — LGPL-2.1-or-later.
* **rarfile** — ISC.
* **cryptography** — Apache-2.0 / BSD-3-Clause.
* **.NET 9 runtime** (embedded into `ffpfsc-pkg-tool`) — MIT.
* **Magick.NET-Q8-AnyCPU** (used by `ffpfsc-pkg-tool` for PNG→DDS) — Apache-2.0.
* **BCnEncoder.NET** (transitive of Magick.NET) — MIT.
* **CommunityToolkit.HighPerformance** (transitive) — MIT.

All of the above are compatible with the GPL-3-or-later that governs the
combined compiled binary.

## What the compiled binary is, legally

Because the compiled `.app` bundle statically embeds MkPFS (GPL-3.0-or-later)
and LibProsperoPkg (GPL-3.0-or-later), the compiled binary as a whole is
distributed as a work under the terms of **GNU General Public License
version 3, or (at your option) any later version**, per GPL-3 section 5.
Source code required to reproduce it is present in this repository (see the
scope block at the end of `LICENSE`). The original Python and C# code
authored for this project is separately licensed under MIT so downstream
projects can reuse it under either compatible license.
