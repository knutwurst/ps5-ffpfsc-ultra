"""
Same-format copy/move for the queue's ``copy`` operation.

Used when the source format equals the target format (``.ffpfsc`` → ``.ffpfsc``,
``.ffpfs`` → ``.ffpfs``, ``.pkg`` → ``.pkg``): re-encoding is pure waste, so the
file is transported as-is.

Transport is chosen by comparing the filesystem device:
  * Same drive (same ``st_dev``) → ``os.rename`` (atomic, instant, no data copy)
  * Cross-drive → chunked copy with progress markers, then optionally delete
    the source on success so the whole operation feels like a move.

Emits the same ``[PHASE]`` and ``[####] NN%`` markers ``CLIWorker`` already
parses, so the queue's progress bar and stage indicators light up unchanged.

Exit codes:
  * ``0`` — success
  * ``1`` — I/O or unexpected error
  * ``2`` — source == destination (would rename onto itself) → skip
  * ``3`` — a different file already occupies the destination name → skip
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional

CHUNK = 4 * 1024 * 1024  # 4 MiB — same order as extract_members' write chunk

_ALLOWED_SUFFIXES = frozenset({".ffpfsc", ".ffpfs", ".pkg"})


def _print(on_line: Optional[Callable[[str], None]], msg: str) -> None:
    if on_line is None:
        print(msg, flush=True)
    else:
        on_line(msg)


def _same_device(a: Path, b: Path) -> bool:
    """True when ``a`` and ``b`` live on the same filesystem, so ``os.rename``
    is a metadata-only operation instead of a copy. Compares the closest
    EXISTING parent for each — a not-yet-created destination folder has no
    st_dev on its own."""
    def dev(p: Path) -> int:
        q = p
        while not q.exists():
            q = q.parent
            if q == q.parent:
                break
        return os.stat(q).st_dev if q.exists() else -1

    da, db = dev(a), dev(b)
    return da != -1 and da == db


def _resolves_same(src: Path, dst: Path) -> bool:
    """True when ``src`` and ``dst`` resolve to the same file — including the
    macOS case-insensitive equality that ``resolve()`` normalizes. A missing
    ``dst`` cannot equal an existing ``src`` (nothing to resolve to)."""
    try:
        return dst.exists() and src.resolve() == dst.resolve()
    except Exception:
        return False


def run_copy(src, dst_dir, *,
             dst_name: Optional[str] = None,
             delete_source: bool = True,
             on_line: Optional[Callable[[str], None]] = None) -> int:
    """
    Copy or move *src* into *dst_dir*.

    Args:
      src: source file path (must be an existing regular file with a supported
           suffix).
      dst_dir: destination directory (created if missing).
      dst_name: destination filename (defaults to ``src.name``).
      delete_source: on a cross-drive copy, remove the source file after a
                     successful write so the operation is effectively a move.
      on_line: line sink (mirrors backend logging). ``None`` prints to stdout.

    Returns an exit code (see module docstring).
    """
    src = Path(src)
    dst_dir = Path(dst_dir)
    if not src.is_file():
        _print(on_line, f"[ERROR] copy: source not found or not a file: {src}")
        return 1
    if src.suffix.lower() not in _ALLOWED_SUFFIXES:
        _print(on_line, f"[ERROR] copy: unsupported source type: {src.suffix} "
                        f"(need one of {sorted(_ALLOWED_SUFFIXES)})")
        return 1

    dst = dst_dir / (dst_name or src.name)

    if _resolves_same(src, dst):
        _print(on_line, f"[WARN] copy: source and destination are the same file, "
                        f"skipping: {src}")
        return 2

    if dst.exists() and not _resolves_same(src, dst):
        try:
            same_bytes = (dst.is_file() and dst.stat().st_size == src.stat().st_size
                          and dst.samefile(src))
        except Exception:
            same_bytes = False
        if not same_bytes:
            _print(on_line, f"[WARN] copy: destination already occupied by a "
                            f"different file, skipping: {dst}")
            return 3

    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _print(on_line, f"[ERROR] copy: cannot create destination folder {dst_dir}: {e}")
        return 1

    same_drive = _same_device(src, dst_dir)
    total = src.stat().st_size

    _print(on_line, "[PHASE] Writing Final Image")

    if same_drive:
        # Metadata-only rename — no data movement. Feels instantaneous even on
        # a 100 GB game because we never touch the payload bytes.
        _print(on_line, f"[INFO] copy: same-drive move — {src.name} → {dst}")
        try:
            os.rename(src, dst)
        except OSError as e:
            _print(on_line, f"[ERROR] copy: rename failed: {e}")
            return 1
        _print(on_line, f"[####] 100% move")
        # No "[PHASE] Complete" — the worker owns stage transitions; emitting our own
        # would race the completion path and briefly show "Complete: 0%" in the log.
        _print(on_line, f"[SUCCESS] Moved {src.name} → {dst}")
        return 0

    # Cross-drive: chunked copy through a *.copy-tmp file so an interrupted
    # write never leaves a truncated target visible under the final name.
    tmp = dst.with_suffix(dst.suffix + ".copy-tmp")
    _print(on_line, f"[INFO] copy: cross-drive copy — {src.name} → {dst}")
    written = 0
    last_pct = -1
    try:
        with open(src, "rb", buffering=0) as fin, open(tmp, "wb", buffering=0) as fout:
            while True:
                buf = fin.read(CHUNK)
                if not buf:
                    break
                fout.write(buf)
                written += len(buf)
                if total > 0:
                    pct = min(99, int(written * 100 / total))
                    if pct != last_pct:
                        _print(on_line, f"[####] {pct}% copy")
                        last_pct = pct
        os.replace(tmp, dst)
    except Exception as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        _print(on_line, f"[ERROR] copy: write failed: {e}")
        return 1

    _print(on_line, f"[####] 100% copy")

    if delete_source:
        _print(on_line, "[PHASE] Cleaning Up")
        try:
            src.unlink()
            _print(on_line, f"[INFO] copy: source deleted after successful copy: {src}")
        except Exception as e:
            # Copy succeeded — losing the source delete is a warning, not a fail:
            # the target is intact, the user can delete the source manually.
            _print(on_line, f"[WARN] copy: source could not be deleted: {e}")

    _print(on_line, f"[SUCCESS] {'Moved' if delete_source else 'Copied'} "
                    f"{src.name} → {dst}")
    return 0
