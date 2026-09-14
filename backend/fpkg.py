"""
Thin Python wrapper around the bundled `ffpfsc-pkg-tool` native binary.

The binary is a self-contained .NET 9 build of drakmor's LibProsperoPkg 1.2.0
(GPL-3-or-later, sourced from the a53-fpkg 0.5 release). It exposes fPKG
inspect / extract / build without a .NET runtime install and without loading
the leaked Sony `libScePubTools.dll`.

Kraken compression uses LibProsperoPkg's own managed encoder ("BuiltIn"
backend). On-console acceptance is only truly proven for packages built with
Sony's Publishing Tools DLL; the built-in encoder ships format-compatible
Kraken blocks and drakmor's own extractor round-trips them, but the console
verdict is up to the user's PS5 install test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

# Located in backend/native/ next to this module.
_TOOL_NAME = "ffpfsc-pkg-tool"


def _tool_search_paths() -> list[Path]:
    """Where the CLI binary might live. Order matters: first hit wins."""
    here = Path(__file__).resolve().parent
    frozen_dir = getattr(sys, "_MEIPASS", None)
    paths: list[Path] = []
    # Regular dev / source-run layout (backend/native/ffpfsc-pkg-tool).
    paths.append(here / "native" / _TOOL_NAME)
    # PyInstaller-bundled layout (adjacent copy under the frozen backend folder).
    if frozen_dir:
        paths.append(Path(frozen_dir) / "backend" / "native" / _TOOL_NAME)
        paths.append(Path(frozen_dir) / "native" / _TOOL_NAME)
    # Allow an env-var override for developer overrides.
    env = os.environ.get("FFPFSC_PKG_TOOL")
    if env:
        paths.insert(0, Path(env))
    return paths


def tool_path() -> Path:
    """Locate the bundled ffpfsc-pkg-tool binary, or raise FileNotFoundError."""
    for p in _tool_search_paths():
        if p.is_file() and os.access(p, os.X_OK):
            return p
    tried = "\n  ".join(str(p) for p in _tool_search_paths())
    raise FileNotFoundError(
        f"ffpfsc-pkg-tool not found. Tried:\n  {tried}\n"
        "Set FFPFSC_PKG_TOOL to override, or rebuild the app."
    )


def is_available() -> bool:
    try:
        tool_path()
        return True
    except FileNotFoundError:
        return False


def _run(argv: list[str], *, on_line=None) -> int:
    """Run the CLI, streaming its combined output through *on_line* (or stdout)."""
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if on_line is not None:
                on_line(line)
            else:
                print(line, flush=True)
    finally:
        proc.wait()
    return proc.returncode


def version() -> str:
    """Return the CLI version banner, or an error string."""
    argv = [str(tool_path()), "version"]
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout
    except Exception as e:
        return f"[fpkg tool unavailable: {e}]"


def inspect(pkg: Path, *, json_out: bool = False) -> int:
    argv = [str(tool_path()), "inspect", str(pkg)]
    if json_out:
        argv.append("--json")
    return _run(argv)


def extract(pkg: Path, out_dir: Path,
            *, passcode: str = "0" * 32,
            outer: bool = False,
            on_line=None) -> int:
    """
    Extract a finalized fPKG (\\x7FFIH) or metadata CNT (\\x7FCNT).

    outer=False (default) pulls the /app0-style inner-image content files.
    outer=True dumps the outer-PFS entries (uroot, pfs_image.dat itself, naps).
    """
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    cmd = "extract-outer" if outer else "extract-inner"
    argv = [str(tool_path()), cmd, str(pkg), str(out_dir), "--passcode", passcode]
    return _run(argv, on_line=on_line)


def build(src_dir: Path, out_dir: Path,
          *,
          content_id: str,
          title_id: str,
          title: str = "",
          version: str = "01.000.000",
          passcode: str = "0" * 32,
          inner_mode: str = "none",             # "none" | "zlib" | "kraken"
          kraken_backend: str = "builtin",      # "automatic" | "builtin" | "publishingtools" | "uncompressed"
          publishing_tools_dll: Optional[str] = None,
          deterministic: bool = False,
          temp_dir: Optional[str] = None,
          level: Optional[int] = None,
          on_line=None) -> int:
    """
    Build a debug fPKG from a prepared /app0-style source folder.

    - content_id must match XX0000-XXXX00000_00-XXXXXXXXXXXXXXXX (36 chars).
    - title_id must be XXXX00000 (9 chars).
    - Every build Kraken-packs each file individually (raw only when that would not
      shrink it) — that is the native package layout and cannot be switched off.
      inner_mode adds a codec LAYER over the whole inner image on top of that:
      'none' = no extra layer (default), 'kraken' = block-level Kraken layer (v1.2.0
      path), 'zlib' = the legacy whole-inner PFSC layer. Measured on already
      Kraken-packed data the three produce the same size; they differ in structure.
    - kraken_backend 'builtin' uses LibProsperoPkg's own managed encoder (no external DLL).
      'publishingtools' requires the leaked libScePubTools.dll at the given path.
    - temp_dir: where LibProsperoPkg stages the inner image / CNT / outer image
      (defaults to $TMPDIR). Pass the app's fast temp drive for big games.
    - level: Kraken preset. Measured: 0..9 give byte-identical output (the encoder's
      'normal' regime); -4..-1 select the faster, slightly weaker preset. The GUI maps
      normal → 7 and fast → -4.
    """
    src_dir = Path(src_dir); out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        str(tool_path()), "build", str(src_dir), str(out_dir),
        "--content-id", content_id,
        "--title-id", title_id,
        "--version", version,
        "--passcode", passcode,
        "--mode", inner_mode,
        "--kraken-backend", kraken_backend,
    ]
    if title:
        argv += ["--title", title]
    if publishing_tools_dll:
        argv += ["--pubtools-dll", publishing_tools_dll]
    if deterministic:
        argv += ["--deterministic"]
    if temp_dir:
        argv += ["--temp", str(temp_dir)]
    if level is not None:
        argv += ["--level", str(int(level))]
    return _run(argv, on_line=on_line)


def validate(pkg: Path, *, json_out: bool = False, on_line=None) -> int:
    """
    Run the CLI's diagnostic checklist against a package. Prints a
    pass/warn/fail table. Returns 0 iff no failures.
    """
    argv = [str(tool_path()), "validate", str(pkg)]
    if json_out:
        argv.append("--json")
    return _run(argv, on_line=on_line)
