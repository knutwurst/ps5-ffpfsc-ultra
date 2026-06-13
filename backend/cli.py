#!/usr/bin/env python3
"""PS5 FFPFSC PRO — backend wrapper (MkPFS 0.0.8+)"""
import sys
import os

# ── Bundled mkpfs detection ───────────────────────────────────────────────────
# When this script lives inside a 'backend/' folder, look for 'backend/mkpfs/'
# and add 'backend/' to sys.path so 'import mkpfs' resolves to the bundled copy.
_CLI_DIR = os.path.dirname(os.path.abspath(__file__))
_BUNDLED_MKPFS = os.path.join(_CLI_DIR, "mkpfs", "__main__.py")
if os.path.isfile(_BUNDLED_MKPFS) and _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

# ── Frozen-mode internal mkpfs intercept ─────────────────────────────────────
if len(sys.argv) > 1 and sys.argv[1] == "--mkpfs-internal":
    try:
        from mkpfs.cli import cli_mkpfs_main
        sys.exit(cli_mkpfs_main(sys.argv[2:]))
    except Exception as e:
        print(f"[ERROR] Internal MkPFS call failed: {e}", file=sys.stderr)
        sys.exit(1)

import argparse
import contextlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_title_id_from_name(name: str) -> str:
    match = re.search(r'\b([A-Z]{4}\d{5})\b', name, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    fallback = name
    for suffix in [".exfat", ".ffpkg", ".ffpfs", ".ffpfsc", "-app0", "-app", "-patch0", "-patch"]:
        if fallback.lower().endswith(suffix):
            fallback = fallback[:-len(suffix)]
    return fallback


def get_title_id(item_path: Path) -> str:
    if item_path.is_dir():
        param_path = item_path / "sce_sys" / "param.json"
        try:
            if param_path.is_file():
                with open(param_path, encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get("titleId") or data.get("title_id") or ""
        except Exception as e:
            print(f"[WARN] Could not parse param.json for title ID: {e}")
    return get_title_id_from_name(item_path.name)


_DISK_IMAGE_SUFFIXES = {'.exfat', '.ffpkg'}
_PFS_IMAGE_SUFFIXES = {'.ffpfs', '.ffpfsc'}


def find_game_items(path: Path, batch: bool = False) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() in _DISK_IMAGE_SUFFIXES:
            return [path]
        print(f"[ERROR] Unsupported file type: {path.name}. Supported: .exfat, .ffpkg, or a game folder.")
        sys.exit(1)

    print(f"[INFO] Scanning for game folder(s) and disk image(s) (.exfat / .ffpkg) in {path}...")

    image_items: list[Path] = []
    for dirpath, _, filenames in os.walk(path):
        curr = Path(dirpath)
        for f in filenames:
            if Path(f).suffix.lower() in _DISK_IMAGE_SUFFIXES:
                image_items.append(curr / f)

    folder_items: list[Path] = []
    for dirpath, _, _ in os.walk(path):
        curr = Path(dirpath)
        if (curr / "eboot.bin").is_file() and (curr / "sce_sys" / "param.json").is_file():
            folder_items.append(curr)

    # A disk image that sits *inside* a detected game folder is a by-product of
    # that game, not a separate item — drop it so one game isn't counted twice
    # (which would otherwise trip the "multiple items, use --batch" error, or
    # pack the same game twice in batch mode).
    folder_resolved = [f.resolve() for f in folder_items]
    def _inside_a_game_folder(p: Path) -> bool:
        rp = p.resolve()
        return any(fr in rp.parents for fr in folder_resolved)

    valid_items: list[Path] = folder_items + [
        img for img in image_items if not _inside_a_game_folder(img)
    ]

    seen: set[Path] = set()
    deduped: list[Path] = []
    for item in valid_items:
        r = item.resolve()
        if r not in seen:
            seen.add(r)
            deduped.append(item)
    valid_items = deduped

    if not valid_items:
        print(f"[ERROR] Could not find any valid game folders or disk images (.exfat / .ffpkg) in {path}.")
        sys.exit(1)

    if not batch and len(valid_items) > 1:
        print(f"[ERROR] Multiple game folders/files found in {path}:")
        for item in valid_items:
            print(f"  - {item}")
        print("Use --batch to process all.")
        sys.exit(1)

    if not batch:
        print(f"[OK] Found game source at {valid_items[0]}")
    else:
        print(f"[OK] Found {len(valid_items)} game item(s) for batch processing.")
    return valid_items


def find_pfs_images(path: Path, batch: bool = False) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() in _PFS_IMAGE_SUFFIXES:
            return [path]
        print(f"[ERROR] Unsupported file type for unpack: {path.name}. Supported: .ffpfs or .ffpfsc.")
        sys.exit(1)

    print(f"[INFO] Scanning for PFS image(s) (.ffpfs / .ffpfsc) in {path}...")
    images: list[Path] = []
    for dirpath, _, filenames in os.walk(path):
        curr = Path(dirpath)
        for f in filenames:
            if Path(f).suffix.lower() in _PFS_IMAGE_SUFFIXES:
                images.append(curr / f)

    images = sorted({p.resolve(): p for p in images}.values(), key=lambda p: str(p).lower())

    if not images:
        print(f"[ERROR] Could not find any .ffpfs or .ffpfsc images in {path}.")
        sys.exit(1)

    if not batch and len(images) > 1:
        print(f"[ERROR] Multiple PFS images found in {path}:")
        for image in images:
            print(f"  - {image}")
        print("Use --batch to process all.")
        sys.exit(1)

    if not batch:
        print(f"[OK] Found PFS image at {images[0]}")
    else:
        print(f"[OK] Found {len(images)} PFS image(s) for batch extraction.")
    return images


def _mkpfs_error_hint(exc: subprocess.CalledProcessError, output_path: Path) -> None:
    """Print a clear [ERROR] summary when mkpfs returns a non-zero exit code.
    Advice is platform-aware — Windows talks NTFS/drive letters, macOS/Linux do not."""
    print(f"[ERROR] mkpfs failed with exit code {exc.returncode}.", flush=True)
    if os.name == "nt":
        fs_label = ""
        try:
            import ctypes as _ct
            drive = str(output_path.resolve())[:3]
            buf = _ct.create_unicode_buffer(64)
            _ct.windll.kernel32.GetVolumeInformationW(drive, None, 0, None, None, None, buf, _ct.sizeof(buf))
            fs_label = buf.value.strip()
        except Exception:
            pass
        if fs_label in ("exFAT", "FAT32", "FAT"):
            print(
                f"[ERROR] OUTPUT DRIVE IS {fs_label} — 4 GB per-file limit exceeded.\n"
                f"[ERROR] PS5 .ffpfsc files are almost always larger than 4 GB.\n"
                f"[ERROR]   OUTPUT folder  →  change to an NTFS drive (e.g. C:\\ or D:\\)\n"
                f"[ERROR]   TEMP folder    →  also move to NTFS if it is on the same drive",
                flush=True,
            )
        else:
            print(
                f"[ERROR] Output path: {output_path}\n"
                f"[ERROR]   OUTPUT folder  →  ensure the drive is NTFS (not exFAT/FAT32) with enough space\n"
                f"[ERROR]   TEMP folder    →  needs ~1.5x the game size of free space during compression\n"
                f"[ERROR]   CPU cores      →  try lowering to 2 or 1 if RAM could be the cause\n"
                f"[ERROR]   Level          →  try 5 if the default (7) runs out of memory",
                flush=True,
            )
        return
    # macOS / Linux — no NTFS / drive-letter advice; exFAT 4 GB limit is the
    # common culprit on external PS5 transfer drives.
    print(
        f"[ERROR] Output path: {output_path}\n"
        f"[ERROR] Common causes & fixes:\n"
        f"[ERROR]   exFAT/FAT drive →  4 GB per-file limit; PS5 .ffpfsc files are usually larger.\n"
        f"[ERROR]                      Use an APFS or HFS+ drive (Disk Utility → Erase → APFS),\n"
        f"[ERROR]                      or choose a different OUTPUT drive.\n"
        f"[ERROR]   Free space      →  TEMP folder needs ~1.5x the game size free during compression\n"
        f"[ERROR]   Memory          →  lower CPU cores to 2/1, or compression Level to 5, if RAM runs out",
        flush=True,
    )


def _locate_mkpfs() -> tuple[list[str], str | None]:
    """Return (cmd_base, cwd) for invoking mkpfs."""
    # Frozen EXE — use internal bundle
    if getattr(sys, "frozen", False):
        print("[INFO] Running in packaged/frozen environment. Using internal MkPFS bundle.")
        return [sys.executable, "--mkpfs-internal"], None

    # Bundled package next to this script (backend/mkpfs/)
    if os.path.isfile(_BUNDLED_MKPFS):
        print(f"[INFO] Using bundled MkPFS package at {_CLI_DIR}")
        return [sys.executable, "-m", "mkpfs"], _CLI_DIR

    # Sibling workspace (legacy detection)
    parent_dir = Path(__file__).resolve().parent.parent
    try:
        for sibling in sorted(parent_dir.iterdir()):
            if sibling.is_dir() and (sibling / "mkpfs" / "__main__.py").is_file():
                print(f"[INFO] Using local workspace directory at {sibling}")
                return [sys.executable, "-m", "mkpfs"], str(sibling)
    except Exception:
        pass

    # System PATH
    if shutil.which("mkpfs"):
        print("[INFO] Using system mkpfs from PATH.")
        return ["mkpfs"], None

    # Auto-install via pip
    print("[INFO] MkPFS not found. Installing automatically via pip...")
    res = subprocess.run(
        [sys.executable, "-m", "pip", "install", "mkpfs==0.0.8"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        print("[ERROR] Failed to install mkpfs. Please install it manually: pip install mkpfs")
        print(res.stderr)
        sys.exit(1)
    print("[OK] MkPFS 0.0.8 installed successfully.")
    return [sys.executable, "-m", "mkpfs"], None


# ─────────────────────────────────────────────────────────────────────────────
# MkPFS wrappers
# ─────────────────────────────────────────────────────────────────────────────

def pack_folder_uncompressed(
    game_folder: Path,
    pfs_path: Path,
    mkpfs_cmd_base: list[str],
    mkpfs_cwd: str | None,
    *,
    verify_enabled: bool = False,
    compression_level: int = 7,
    cpu_count: int = 0,
    threshold_gain: int = 5,
    block_size: str = "auto",
    verbose: bool = False,
    temp_folder: Path | None = None,
) -> None:
    print(f"[INFO] Packing folder {game_folder.name} to uncompressed PFS image {pfs_path.name}...")
    cmd = mkpfs_cmd_base + [
        "pack", "folder",
        "--no-compress",
        "--no-adjust-output-file-extension",
        "--version", "PS5",
        "--inode-bits", "32",
        "--block-size", str(block_size),
    ]
    if temp_folder:
        cmd += ["--temp-folder", str(temp_folder)]
    if verbose:
        cmd.append("--verbose")
    if verify_enabled:
        print("[INFO] Post-pack verify is ENABLED (full check against the source folder — slower, more RAM).", flush=True)
        cmd.append("--verify")
    else:
        # "Verify Output" off → skip the post-pack verify entirely. Without this,
        # mkpfs runs its DEFAULT structure verify, which still compares the image's
        # file list against the source folder and fails the whole build on a single
        # discrepancy (a stray .DS_Store, an empty file, an extraction artifact) —
        # verification the user never asked for. The final .ffpfsc still gets a cheap
        # internal structure check in the compress pass.
        print("[INFO] Post-pack verify is off (enable 'Verify Output' to check against the source). Skipping it.", flush=True)
        cmd.append("--no-verify-structure")
    cmd += [str(game_folder), str(pfs_path)]
    print(f"[INFO] Running: {' '.join(cmd)}", flush=True)
    try:
        subprocess.run(cmd, cwd=mkpfs_cwd, check=True)
    except subprocess.CalledProcessError as e:
        _mkpfs_error_hint(e, pfs_path)
        sys.exit(1)
    print(f"[OK] Uncompressed PFS creation complete: {pfs_path}")


def compress_file_to_ffpfsc(
    source_file: Path,
    ffpfsc_path: Path,
    mkpfs_cmd_base: list[str],
    mkpfs_cwd: str | None,
    *,
    compression_level: int = 7,
    cpu_count: int = 0,
    threshold_gain: int = 5,
    block_size: str = "auto",
    verbose: bool = False,
    temp_folder: Path | None = None,
) -> None:
    print(f"[INFO] Compressing {source_file.name} to outer container {ffpfsc_path.name} using MkPFS...")
    cmd = mkpfs_cmd_base + [
        "pack", "file",
        "--compress",
        "--version", "PS5",
        "--inode-bits", "32",
        "--compression-level", str(compression_level),
        "--cpu-count", str(cpu_count),
        "--threshold-gain", str(threshold_gain),
        "--block-size", str(block_size),
    ]
    if temp_folder:
        cmd += ["--temp-folder", str(temp_folder)]
    if verbose:
        cmd.append("--verbose")
    cmd += [str(source_file), str(ffpfsc_path)]
    print(f"[INFO] Running: {' '.join(cmd)}", flush=True)
    try:
        subprocess.run(cmd, cwd=mkpfs_cwd, check=True)
    except subprocess.CalledProcessError as e:
        _mkpfs_error_hint(e, ffpfsc_path)
        sys.exit(1)
    print(f"[OK] Compression complete: {ffpfsc_path}")


def unpack_pfs_image(
    image_file: Path,
    output_dir: Path,
    mkpfs_cmd_base: list[str],
    mkpfs_cwd: str | None,
    *,
    overwrite: bool = False,
) -> None:
    print(f"[INFO] Extracting {image_file.name} to {output_dir} using MkPFS...")
    cmd = mkpfs_cmd_base + ["unpack", str(image_file), str(output_dir)]
    if overwrite:
        cmd.append("--overwrite")
    print(f"[INFO] Running: {' '.join(cmd)}", flush=True)
    try:
        subprocess.run(cmd, cwd=mkpfs_cwd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] mkpfs unpack failed with exit code {e.returncode}.", flush=True)
        print(f"[ERROR] Source image: {image_file}", flush=True)
        print(f"[ERROR] Output folder: {output_dir}", flush=True)
        sys.exit(1)
    print(f"[OK] Extraction complete: {output_dir}")


def resolve_unpack_output_dir(image_file: Path, requested_output: Path, *, batch: bool = False) -> Path:
    if batch or requested_output == Path(".").resolve():
        return requested_output / f"{image_file.stem}_extracted"
    return requested_output


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="PS5 FFPFSC PRO backend — create .ffpfsc containers or extract .ffpfs/.ffpfsc images."
    )
    parser.add_argument("game_folder", nargs='?', help="Source game folder, .exfat/.ffpkg file, or .ffpfs/.ffpfsc image")
    parser.add_argument("output", nargs='?', default=".", help="Output .ffpfsc file/directory, or extraction directory")
    parser.add_argument("--pack", dest="operation", action="store_const", const="pack", help="Force pack/compress mode")
    parser.add_argument("--unpack", dest="operation", action="store_const", const="unpack", help="Extract .ffpfs/.ffpfsc image(s)")
    parser.add_argument("--keep-pfs",     action="store_true", help="Keep intermediate pfs_image.dat")
    parser.add_argument("--verify",       action="store_true", help="Run MkPFS post-build verification (slower, more RAM)")
    parser.add_argument("--batch",        action="store_true", help="Process all supported items found under source")
    parser.add_argument("-f", "--force", "--overwrite", dest="overwrite", action="store_true", help="Overwrite existing files")
    parser.add_argument("--password",     type=str, help="Password for ZIP/RAR archives")
    # MkPFS 0.0.8 tuning flags (forwarded to mkpfs pack file)
    parser.add_argument("--compression-level", type=int, default=7,  metavar="0-9",
                        help="Zlib compression level (0=store, 9=max, default: 7)")
    parser.add_argument("--cpu-count",    type=int, default=0,  metavar="N",
                        help="CPU cores for compression (0=auto, default: 0)")
    parser.add_argument("--threshold-gain", type=int, default=5, metavar="PCT",
                        help="Minimum per-block compression gain %% to keep compressed (default: 5)")
    parser.add_argument("--block-size",   type=str, default="auto",
                        help="PFS block size in bytes, 'auto' (65536), or 'auto-fit' (default: auto)")
    parser.add_argument("--verbose",      action="store_true", help="Verbose per-file mkpfs output")
    parser.add_argument("--temp-dir",     type=str, default=None,
                        help="Temp folder for intermediate files (default: system temp). "
                             "Use a fast NVMe drive for best performance.")

    args = parser.parse_args()

    if not args.game_folder:
        parser.print_help()
        sys.exit(1)

    game_folder = Path(args.game_folder).resolve()
    ffpfs_path  = Path(args.output).resolve()

    if not game_folder.exists():
        print(f"[ERROR] Source path does not exist: {game_folder}")
        sys.exit(1)

    operation = args.operation
    if operation is None:
        operation = "unpack" if game_folder.is_file() and game_folder.suffix.lower() in _PFS_IMAGE_SUFFIXES else "pack"

    # Resolve temp dir — use user-specified fast drive if provided
    user_temp: Path | None = Path(args.temp_dir).resolve() if args.temp_dir else None
    if user_temp:
        user_temp.mkdir(parents=True, exist_ok=True)
        print(f"[INFO] Using user-specified temp folder: {user_temp}", flush=True)

    _is_zip = lambda p: p.suffix.lower() == ".zip"
    _is_rar = lambda p: p.suffix.lower() in (".rar", ".r00")

    @contextlib.contextmanager
    def prepare_source_path(path: Path):
        if _is_zip(path):
            with tempfile.TemporaryDirectory(dir=user_temp) as tmpdir:
                try:
                    with zipfile.ZipFile(path) as zf:
                        for member in zf.infolist():
                            dest = Path(tmpdir) / member.filename
                            try:
                                dest.resolve().relative_to(Path(tmpdir).resolve())
                            except ValueError:
                                print(f"[ERROR] ZIP path traversal detected: {member.filename}")
                                sys.exit(1)
                        zf.extractall(tmpdir, pwd=args.password.encode() if args.password else None)
                    yield Path(tmpdir)
                except (zipfile.BadZipFile, RuntimeError) as exc:
                    print(f"[ERROR] ZIP extraction failed: {exc}")
                    sys.exit(1)
        elif _is_rar(path):
            # Multi-volume sets must be opened on the FIRST volume. The bundled
            # rarfile.extractall() already guards against path traversal, and the
            # native binding takes the password as a str.
            first = path
            m = re.match(r"^(?P<b>.*\.part)(?P<n>\d+)(?P<e>\.rar)$", path.name, re.I)
            if m:
                cand = path.with_name(f"{m.group('b')}{'1'.zfill(len(m.group('n')))}{m.group('e')}")
                if cand.exists():
                    first = cand
            elif re.match(r"^.+\.r\d{2,}$", path.name, re.I):
                cand = path.with_suffix(".rar")
                if cand.exists():
                    first = cand
            with tempfile.TemporaryDirectory(dir=user_temp) as tmpdir:
                try:
                    from unrar import rarfile
                    with rarfile.RarFile(first, pwd=args.password or None) as rf:
                        rf.extractall(tmpdir)
                    yield Path(tmpdir)
                except rarfile.RarWrongPassword as exc:
                    print(f"[ERROR] RAR extraction failed: wrong or missing password ({exc})")
                    sys.exit(1)
                except Exception as exc:
                    print(f"[ERROR] RAR extraction failed: {exc} "
                          "(for a multi-part RAR, ensure all .partN.rar / .rNN files are present)")
                    sys.exit(1)
        else:
            yield path

    mkpfs_cmd_base, mkpfs_cwd = _locate_mkpfs()

    # Print MkPFS version
    try:
        ver = subprocess.run(
            mkpfs_cmd_base + ["-V"],
            capture_output=True, text=True,
            cwd=mkpfs_cwd,
        )
        print(f"[INFO] MkPFS: {ver.stdout.strip() or ver.stderr.strip()}", flush=True)
    except Exception:
        pass

    if operation == "unpack":
        images = find_pfs_images(game_folder, args.batch)
        if args.batch:
            ffpfs_path.mkdir(parents=True, exist_ok=True)
        for image in images:
            current_output_dir = resolve_unpack_output_dir(image, ffpfs_path, batch=args.batch)
            if current_output_dir.exists() and args.overwrite:
                print(f"[WARN] Output folder already exists. MkPFS will overwrite files in: {current_output_dir}")
            elif current_output_dir.exists() and not args.overwrite:
                print(f"[ERROR] Output folder already exists: {current_output_dir}")
                print("[ERROR] Use --overwrite to replace existing extracted files.")
                sys.exit(1)
            current_output_dir.parent.mkdir(parents=True, exist_ok=True)
            unpack_pfs_image(
                image,
                current_output_dir,
                mkpfs_cmd_base,
                mkpfs_cwd,
                overwrite=args.overwrite,
            )
        print("\n[SUCCESS] All operations completed successfully!")
        return

    # Pack options forwarded to mkpfs
    pack_kwargs = dict(
        compression_level=max(0, min(9, args.compression_level)),
        cpu_count=max(0, args.cpu_count),
        threshold_gain=max(0, args.threshold_gain),
        block_size=args.block_size,
        verbose=args.verbose,
    )

    with prepare_source_path(game_folder) as active_source_path:
        game_items = find_game_items(active_source_path, args.batch)

        if args.batch:
            ffpfs_path.mkdir(parents=True, exist_ok=True)
        elif not ffpfs_path.is_dir() and not ffpfs_path.suffix:
            ffpfs_path.mkdir(parents=True, exist_ok=True)

        for item in game_items:
            title_id = get_title_id(item)
            ext = ".ffpfsc"

            if args.batch or ffpfs_path.is_dir():
                current_ffpfs_path = ffpfs_path / f"{title_id}{ext}"
            else:
                current_ffpfs_path = ffpfs_path.with_suffix(ext)

            if args.batch:
                print(f"\n[INFO] --- Processing batch item: {title_id} ({item.name}) ---")

            if current_ffpfs_path.exists():
                if args.overwrite:
                    print(f"[WARN] Output file already exists. Overwriting: {current_ffpfs_path}")
                    try:
                        current_ffpfs_path.unlink()
                    except Exception as e:
                        print(f"[ERROR] Failed to remove existing output file: {e}")
                        sys.exit(1)
                else:
                    print(f"[WARN] Output file already exists: {current_ffpfs_path}")
                    try:
                        if sys.stdin.isatty():
                            response = input("Overwrite existing file? [y/N]: ").strip().lower()
                        else:
                            print("[INFO] Non-interactive shell — skipping overwrite.")
                            response = 'n'
                    except (KeyboardInterrupt, EOFError):
                        print("\n[INFO] Cancelled.")
                        sys.exit(0)
                    if response not in ('y', 'yes'):
                        print(f"[INFO] Skipping: {current_ffpfs_path.name}")
                        continue
                    try:
                        current_ffpfs_path.unlink()
                    except Exception as e:
                        print(f"[ERROR] Failed to remove existing output file: {e}")
                        sys.exit(1)

            if item.is_file() and item.suffix.lower() in ('.exfat', '.ffpkg'):
                # Direct disk image (.exfat / .ffpkg) → .ffpfsc (single-file streaming path)
                compress_file_to_ffpfsc(
                    item, current_ffpfs_path, mkpfs_cmd_base, mkpfs_cwd,
                    temp_folder=user_temp,
                    **pack_kwargs,
                )
            else:
                # Game folder: pack uncompressed PFS, then compress → .ffpfsc
                with tempfile.TemporaryDirectory(dir=user_temp) as temp_dir:
                    temp_pfs = Path(temp_dir) / f"{title_id}.ffpfs"

                    pack_folder_uncompressed(
                        item, temp_pfs, mkpfs_cmd_base, mkpfs_cwd,
                        verify_enabled=args.verify,
                        temp_folder=Path(temp_dir),
                        **pack_kwargs,
                    )
                    compress_file_to_ffpfsc(
                        temp_pfs, current_ffpfs_path, mkpfs_cmd_base, mkpfs_cwd,
                        temp_folder=Path(temp_dir),
                        **pack_kwargs,
                    )

                    if args.keep_pfs:
                        saved = current_ffpfs_path.parent / f"{title_id}.ffpfs"
                        print(f"[INFO] Saving intermediate PFS image to {saved}...")
                        shutil.copy2(temp_pfs, saved)

    print("\n[SUCCESS] All operations completed successfully!")


if __name__ == "__main__":
    main()
