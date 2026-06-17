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
import zlib
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
        # Disk images (.exfat/.ffpkg) AND a bare uncompressed PFS image (.ffpfs) are valid
        # pack sources: a .ffpfs is just re-wrapped into a compressed .ffpfsc (its native
        # nested layout), so an already-built image can be (re)packed without a folder
        # round-trip. .ffpfsc is NOT packable here (it's already the compressed deliverable).
        if path.suffix.lower() in _DISK_IMAGE_SUFFIXES or path.suffix.lower() == ".ffpfs":
            return [path]
        print(f"[ERROR] Unsupported file type: {path.name}. Supported: .exfat, .ffpkg, .ffpfs, or a game folder.")
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

def _assert_pass2_spool_space(image_path, temp_dir) -> None:
    """Before pass-2 PFSC compression — which spools roughly the image size into
    *temp_dir* — make sure the temp drive can hold it. If not, exit non-zero with a
    distinct, parseable message BEFORE mkpfs starts. Call this INSIDE the enclosing
    TemporaryDirectory block so its unwind reclaims the inner image, instead of letting
    mkpfs crash mid-write and strand a ~150 GB image (the Crimson Desert failure)."""
    try:
        need = int(Path(image_path).stat().st_size * 1.10)
        free = shutil.disk_usage(str(temp_dir)).free
    except Exception:
        return
    if free < need:
        g = 1024 ** 3
        print(f"[ERROR] Insufficient temp space for pass-2 spool: need ~{need // g} GiB, "
              f"have {free // g} GiB in {temp_dir}. Point --temp-dir at a drive with more "
              f"free space (e.g. the output drive).", flush=True)
        sys.exit(1)


def _open_pass2_spool_dir(image_path, default_temp_dir, output_path, spill_base=None):
    """Pick where the pass-2 PFSC spool lives — the key to using a fast SSD temp even when
    it can't hold image+spool together.

    The inner image (pass 1) stays on *default_temp_dir* (the --temp-dir the GUI placed on
    the SSD). The transient spool (~ the image size) goes there too WHEN it still fits
    beside the image; otherwise it spills onto the OUTPUT drive (a different, usually much
    larger volume). That keeps the image on the fast drive for the compression read instead
    of forcing the whole build onto the slow drive. The spool is pure scratch — where it
    lives does NOT change the resulting .ffpfsc bytes.

    *spill_base* (the GUI's output-root, via --spool-fallback-dir) is the preferred spill
    location (under <spill_base>/_ffpfsc_temp) so the GUI's startup sweep and failure
    cleanup find it; it falls back to the output file's own folder.

    Returns (spool_dir, cleanup_ctx): cleanup_ctx is a TemporaryDirectory to .cleanup()
    (spool spilled to the output drive) or None (spool on default_temp_dir, reclaimed by the
    caller's own temp dir). Never raises — on any doubt it returns default_temp_dir and the
    pre-pass-2 assert remains the backstop."""
    default_temp_dir = Path(default_temp_dir) if default_temp_dir else Path(tempfile.gettempdir())
    try:
        need = int(Path(image_path).stat().st_size * 1.10)
    except Exception:
        return default_temp_dir, None
    try:
        if shutil.disk_usage(str(default_temp_dir)).free >= need:
            return default_temp_dir, None          # fits beside the image — fastest path
    except Exception:
        return default_temp_dir, None
    # default_temp_dir can't hold image + spool. Spill the spool onto the output drive if it
    # is a DIFFERENT volume with room (the image keeps reading from the fast temp drive).
    try:
        base = Path(spill_base) if spill_base else Path(output_path).parent
        probe = base if base.exists() else base.parent
        same = os.stat(str(default_temp_dir)).st_dev == os.stat(str(probe)).st_dev
    except Exception:
        return default_temp_dir, None
    if not same:
        try:
            if shutil.disk_usage(str(probe)).free >= need:
                spill = base / "_ffpfsc_temp"
                spill.mkdir(parents=True, exist_ok=True)
                ctx = tempfile.TemporaryDirectory(prefix="ffpfsc_spool_", dir=str(spill))
                print(f"[INFO] Pass-2 spool routed to the output drive ({spill}) — temp can't "
                      f"hold image+spool; the inner image stays on temp for fast reads.", flush=True)
                return Path(ctx.name), ctx
        except Exception as e:
            print(f"[WARN] Could not place the pass-2 spool on the output drive: {e}", flush=True)
    return default_temp_dir, None   # nothing better; the assert below errors cleanly if needed


def _build_exfat_image(folder: Path, outdir: Path, title_id: str):
    """macOS: build a raw exFAT filesystem image of *folder* (via hdiutil) so it can be
    compressed straight into a .ffpfsc — PSBrew's most-stable 'exfat -> ffpfsc' workflow,
    which wraps a real exFAT volume (read natively by the PS5) instead of going through
    the folder PFS builder. Returns the .exfat path, or None when unsupported (non-macOS,
    no hdiutil) or hdiutil fails — the caller then falls back to the two-pass folder image."""
    if sys.platform != "darwin":
        print("[WARN] --via-exfat needs macOS (hdiutil); using the two-pass folder image instead.", flush=True)
        return None
    if shutil.which("hdiutil") is None:
        print("[WARN] hdiutil not found; using the two-pass folder image instead.", flush=True)
        return None
    vol = (re.sub(r"[^A-Za-z0-9_]", "", (title_id or "PS5GAME"))[:15] or "PS5GAME")
    base = outdir / f"{title_id or 'game'}_exfat"
    out = outdir / f"{title_id or 'game'}.exfat"
    print("[INFO] Building exFAT image from the game folder via hdiutil...", flush=True)
    # UDTO = a RAW image (no UDIF/koly trailer), so it survives MkPFS's 64 KB padding
    # round-trip and re-mounts cleanly (UDRW's trailer does NOT). COPYFILE_DISABLE keeps
    # macOS from injecting AppleDouble ._* sidecars into the volume.
    env = dict(os.environ)
    env["COPYFILE_DISABLE"] = "1"
    try:
        r = subprocess.run(
            ["hdiutil", "create", "-srcfolder", str(folder), "-fs", "ExFAT",
             "-volname", vol, "-layout", "NONE", "-format", "UDTO",
             "-nospotlight", "-ov", "-o", str(base)],
            capture_output=True, text=True, env=env,
        )
    except Exception as e:
        print(f"[WARN] hdiutil failed to start: {e}", flush=True)
        return None
    dmg = Path(str(base) + ".cdr")   # hdiutil UDTO APPENDS .cdr; with_suffix() would
                                     # mangle a title id containing a dot (e.g. 'Game v1.05')
    if r.returncode != 0 or not dmg.exists():
        print(f"[WARN] hdiutil exFAT build failed (rc={r.returncode}): {r.stderr.strip()[:300]}", flush=True)
        try:
            if dmg.exists():
                dmg.unlink()
        except Exception:
            pass
        return None
    # -layout NONE + UDTO yields a RAW exFAT volume; rename so the backend treats it as one.
    dmg.rename(out)
    print(f"[OK] exFAT image built: {out.name} ({out.stat().st_size // (1024**2)} MiB)", flush=True)
    return out


def _extract_exfat_to(exfat_path: Path, dest: Path) -> bool:
    """macOS: mount a RAW exFAT image (CRawDiskImage) read-only and copy its contents
    into *dest*, skipping AppleDouble/OS junk. Returns True on success. Used to turn a
    nested exFAT (from a --via-exfat .ffpfsc) back into a plain folder."""
    if sys.platform != "darwin" or shutil.which("hdiutil") is None:
        print("[WARN] A nested exFAT image needs macOS (hdiutil) to extract to a folder; "
              "leaving the .exfat in place.", flush=True)
        return False
    # Mount OUTSIDE the destination so the mountpoint can never end up among the
    # recovered files (a stranded _exfat_mnt inside dest would pollute the folder and
    # _fully_unwrap would mistake it for real content).
    try:
        mnt = Path(tempfile.mkdtemp(prefix="ffpfsc_exfatmnt_", dir=str(dest.parent)))
    except Exception:
        return False
    a = subprocess.run(
        ["hdiutil", "attach", str(exfat_path), "-imagekey", "diskimage-class=CRawDiskImage",
         "-nobrowse", "-readonly", "-mountpoint", str(mnt)],
        capture_output=True, text=True,
    )
    if a.returncode != 0:
        print(f"[WARN] Could not mount the nested exFAT image: {a.stderr.strip()[:200]}", flush=True)
        try:
            mnt.rmdir()
        except Exception:
            pass
        return False
    def _ad_ignore(_srcdir, names):
        # macOS injects AppleDouble ._* sidecars (and Spotlight/Trash dirs) onto exFAT;
        # drop them at EVERY level so the recovered folder is clean game content only.
        return [n for n in names if n.startswith("._") or n in (
            ".DS_Store", ".Spotlight-V100", ".fseventsd", ".Trashes",
            ".TemporaryItems", "System Volume Information")]
    ok = True
    try:
        for child in mnt.iterdir():
            if _ad_ignore(mnt, [child.name]):
                continue
            target = dest / child.name
            if child.is_dir():
                shutil.copytree(child, target, dirs_exist_ok=True, ignore=_ad_ignore)
            else:
                shutil.copy2(child, target)
    except Exception as e:
        print(f"[WARN] Error copying from the exFAT image: {e}", flush=True)
        ok = False
    finally:
        subprocess.run(["hdiutil", "detach", str(mnt), "-force"], capture_output=True)
        try:
            mnt.rmdir()
        except Exception:
            pass
    if ok:
        try:
            exfat_path.unlink()
        except Exception:
            pass
    return ok


def _fully_unwrap(out_dir: Path, mkpfs_cmd_base, mkpfs_cwd) -> None:
    """Turn a freshly-unpacked image directory into the actual game FOLDER: keep
    unwrapping a SINGLE nested image — .ffpfs/.ffpfsc via another PFS unpack, .exfat/
    .ffpkg via a mount+copy (macOS) — until real files/folders remain. So 'unpack a
    .ffpfsc' yields a folder in ONE action, whether it was packed folder->ffpfsc (the
    nested inner .ffpfs) or via exFAT (the nested .exfat)."""
    for _ in range(8):
        try:
            entries = [p for p in out_dir.iterdir()
                       if not (p.name.startswith("._") or p.name == ".DS_Store"
                               or p.name == "_exfat_mnt" or p.name.startswith("ffpfsc_exfatmnt_"))]
        except Exception:
            return
        pfs = [p for p in entries if p.is_file() and p.suffix.lower() in (".ffpfs", ".ffpfsc")]
        exf = [p for p in entries if p.is_file() and p.suffix.lower() in (".exfat", ".ffpkg")]
        others = [p for p in entries if p not in pfs and p not in exf]
        if others:
            return   # real content present → this IS the folder
        if len(pfs) == 1 and not exf:
            img = pfs[0]
            print(f"[INFO] Unwrapping nested image {img.name} -> folder...", flush=True)
            with tempfile.TemporaryDirectory(dir=out_dir) as td:
                sub = Path(td)
                unpack_pfs_image(img, sub, mkpfs_cmd_base, mkpfs_cwd, overwrite=True)
                try:
                    img.unlink()
                except Exception:
                    pass
                for child in list(sub.iterdir()):
                    shutil.move(str(child), str(out_dir / child.name))
            continue
        if len(exf) == 1 and not pfs:
            print(f"[INFO] Unwrapping nested exFAT {exf[0].name} -> folder...", flush=True)
            if _extract_exfat_to(exf[0], out_dir):
                continue
            return   # couldn't extract (non-macOS) — leave the .exfat for the user
        if pfs or exf:
            print(f"[WARN] Stopped unwrapping — nested image(s) left in place: "
                  f"{[p.name for p in (pfs + exf)]}. The output is NOT a plain folder.", flush=True)
        return   # nothing single to unwrap (empty / multiple images)


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


def _looks_incompressible(path: Path, *, samples: int = 24, chunk: int = 1 << 20,
                          min_gain_pct: float = 2.0) -> bool:
    """Cheap heuristic: sample ~`samples` x `chunk` bytes spread across `path`,
    zlib-compress them, and return True if the aggregate gain is below
    `min_gain_pct`%.

    Used to skip the expensive deflate on already-compressed games: when the inner
    image barely shrinks, the .ffpfsc would store every block raw anyway (the per-
    block threshold rejects non-shrinking blocks), so a level-0 pass produces the
    same container far faster. Conservative by design — on any error, a small file,
    or genuine compressibility it returns False (i.e. compress normally), so the
    worst case is a slightly-larger-but-correct file, never a broken one."""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < 64 * (1 << 20):   # small images: just compress normally
        return False
    raw_total = 0
    comp_total = 0
    try:
        with path.open("rb") as fh:
            step = max(chunk, size // max(1, samples))
            offset = 0
            taken = 0
            while offset < size and taken < samples:
                fh.seek(offset)
                buf = fh.read(chunk)
                if not buf:
                    break
                raw_total += len(buf)
                comp_total += len(zlib.compress(buf, 6))
                taken += 1
                offset += step
    except OSError:
        return False
    if raw_total == 0:
        return False
    gain_pct = (1.0 - comp_total / raw_total) * 100.0
    return gain_pct < min_gain_pct


_PATCH_TITLE_RE = re.compile(r'\b(PPSA\d{5}|CUSA\d{5})\b')
_PATCH_JUNK = ("__MACOSX", ".AppleDouble", ".Spotlight-V100", ".Trashes", ".fseventsd")


def _patch_descend_wrapper(root: Path) -> Path:
    """Descend folders that hold exactly one subdir and no files, so a patch or game
    wrapped in an extra folder (e.g. <CUSA...>/eboot.bin) resolves to its real root."""
    cur = root
    for _ in range(8):
        try:
            entries = list(cur.iterdir())
        except Exception:
            break
        files = [p for p in entries if p.is_file()]
        dirs = [p for p in entries if p.is_dir()]
        if not files and len(dirs) == 1:
            cur = dirs[0]
        else:
            break
    return cur


def _patch_find_game_root(folder: Path) -> Path:
    """Locate the game root (the dir holding sce_sys/eboot.bin) inside an unpacked tree."""
    cand = _patch_descend_wrapper(folder)
    if (cand / "sce_sys").exists() or (cand / "eboot.bin").exists():
        return cand
    for p in sorted(folder.rglob("sce_sys")):
        if p.is_dir():
            return p.parent
    return cand


def _patch_dir_title_id(d: Path) -> str:
    """Best-effort title id for a game/patch directory (param.json, else folder name)."""
    try:
        pj = d / "sce_sys" / "param.json"
        if pj.is_file():
            m = _PATCH_TITLE_RE.search(pj.read_text(encoding="utf-8", errors="ignore"))
            if m:
                return m.group(1).upper()
    except Exception:
        pass
    m = _PATCH_TITLE_RE.search(d.name)
    return m.group(1).upper() if m else ""


def overlay_patch(game_root: Path, patch_dir: Path) -> int:
    """Copy every file from the patch onto the game at matching relative paths,
    overwriting existing files and adding new ones. Skips OS/archiver junk. Returns
    the number of files applied."""
    src_root = _patch_descend_wrapper(patch_dir)
    count = 0
    for src in sorted(src_root.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(src_root)
        if (rel.name == ".DS_Store" or rel.name in ("Thumbs.db", "desktop.ini")
                or rel.name.startswith("._") or any(part in _PATCH_JUNK for part in rel.parts)):
            continue
        dst = game_root / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            count += 1
        except Exception as e:
            print(f"[WARN] Could not apply patch file {rel}: {e}", flush=True)
    return count


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
    parser.add_argument("--no-compress",  dest="no_compress", action="store_true",
                        help="Emit the UNCOMPRESSED inner PFS image (.ffpfs) instead of wrapping "
                             "it in a compressed .ffpfsc. Faster to build (no pass 2) and to mount "
                             "(no decompression), at full size. Applies to game folders and .ffpfs "
                             "sources; .exfat/.ffpkg inputs are still compressed.")
    parser.add_argument("--via-exfat",    action="store_true",
                        help="Build an exFAT image of the game folder and compress THAT into "
                             ".ffpfsc (PSBrew's most-stable exfat->ffpfsc path; macOS only, "
                             "falls back to the two-pass folder image otherwise)")
    parser.add_argument("--no-unwrap", dest="unwrap", action="store_false", default=True,
                        help="When unpacking, stop at the inner image instead of unwrapping "
                             "all the way to a game folder (default: unwrap to a folder)")
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
    parser.add_argument("--spool-fallback-dir", type=str, default=None, metavar="DIR",
                        help="Output-drive root to spill the pass-2 spool into (under "
                             "<DIR>/_ffpfsc_temp) when --temp-dir can't hold image+spool. "
                             "Keeps the inner image on the fast temp drive for big games.")
    parser.add_argument("--patch",        type=str, default=None, metavar="DIR",
                        help="PATCH MODE: overlay the loose files in DIR onto the game "
                             "(a folder or an existing .ffpfsc), then (re)pack to OUTPUT.")
    parser.add_argument("--patch-inplace", action="store_true",
                        help="The game folder is a throwaway temp extract — overlay in "
                             "place instead of copying it first.")

    args = parser.parse_args()

    # ── PS5 console compatibility: force a 64 KiB PFS block size ─────────────────
    # The PS5 reads PFS filesystems with the native 64 KiB (0x10000) logical block.
    # A smaller block — which "auto-fit" picks for many-file games (it chose 4 KiB for
    # Crimson Desert, saving ~5 MB on a 153 GB image) and which 16384/32768 select
    # explicitly — builds an image that verifies fine locally but the console MISREADS,
    # crashing on launch. Everything we pack targets PS5 (--version PS5 is hardcoded in
    # the mkpfs invocations), so normalise any sub-64K / auto-fit request to 64 KiB here.
    _bs = str(getattr(args, "block_size", "auto")).strip().lower()
    _sub64k = _bs in ("auto-fit", "auto_small_files", "auto-small-files") or (_bs.isdigit() and int(_bs) < 0x10000)
    if _sub64k:
        print(f"[INFO] Forcing 64 KiB PFS block size for PS5 console compatibility "
              f"(requested '{args.block_size}'; sub-64K images crash the console).", flush=True)
        args.block_size = "65536"

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
    # Output-drive root the pass-2 spool spills into when --temp-dir can't hold image+spool
    # (the GUI passes its output root; the spill lands under <root>/_ffpfsc_temp).
    user_spill: Path | None = Path(args.spool_fallback_dir).resolve() if args.spool_fallback_dir else None

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

    # ── PATCH MODE: overlay loose patch files onto a game, then (re)pack ──────────
    if args.patch:
        patch_arg = Path(args.patch).resolve()
        if not patch_arg.exists():
            print(f"[ERROR] Patch source not found: {patch_arg}")
            sys.exit(1)
        if ffpfs_path.exists() and not args.overwrite:
            print(f"[ERROR] Output already exists: {ffpfs_path}  (use --overwrite)")
            sys.exit(1)
        patch_pack_kwargs = dict(
            compression_level=max(0, min(9, args.compression_level)),
            cpu_count=max(0, args.cpu_count),
            threshold_gain=max(0, args.threshold_gain),
            block_size=args.block_size,
            verbose=args.verbose,
        )
        print(f"[INFO] PATCH MODE: overlay '{patch_arg.name}' onto '{game_folder.name}'", flush=True)
        with tempfile.TemporaryDirectory(dir=user_temp) as td:
            td = Path(td)
            # The patch may arrive as an archive (auto-patch hands a sibling RAR/ZIP
            # straight through). Extract zip/rar here; a folder is used as-is. A 7z
            # patch is left to the GUI to pre-extract.
            patch_dir = patch_arg
            if patch_arg.is_file() and patch_arg.suffix.lower() in (".zip", ".rar"):
                patch_dir = td / "_patch"
                patch_dir.mkdir(parents=True, exist_ok=True)
                print(f"[INFO] Extracting patch archive '{patch_arg.name}'...", flush=True)
                try:
                    if patch_arg.suffix.lower() == ".zip":
                        with zipfile.ZipFile(patch_arg) as zf:
                            # Guard against Zip-Slip: every member must resolve inside patch_dir.
                            root = patch_dir.resolve()
                            for member in zf.infolist():
                                dest = (patch_dir / member.filename).resolve()
                                try:
                                    dest.relative_to(root)
                                except ValueError:
                                    print(f"[ERROR] Patch ZIP path traversal blocked: {member.filename}")
                                    sys.exit(1)
                            zf.extractall(patch_dir, pwd=args.password.encode() if args.password else None)
                    else:
                        first = patch_arg
                        m = re.match(r"^(?P<b>.*\.part)(?P<n>\d+)(?P<e>\.rar)$", patch_arg.name, re.I)
                        if m:
                            cand = patch_arg.with_name(f"{m.group('b')}{'1'.zfill(len(m.group('n')))}{m.group('e')}")
                            if cand.exists():
                                first = cand
                        from unrar import rarfile  # bundled extractall already guards traversal
                        with rarfile.RarFile(first, pwd=args.password or None) as rf:
                            rf.extractall(str(patch_dir))
                except SystemExit:
                    raise
                except Exception as exc:
                    print(f"[ERROR] Patch archive extraction failed ('{patch_arg.name}'): {exc} "
                          "(corrupt archive, or wrong/missing password)")
                    sys.exit(1)
            if game_folder.is_file() and game_folder.suffix.lower() == ".ffpfsc":
                print("[INFO] Unpacking the existing .ffpfsc to patch it (outer → inner → files)...", flush=True)
                outer = td / "_outer"
                unpack_pfs_image(game_folder, outer, mkpfs_cmd_base, mkpfs_cwd, overwrite=True)
                inner = next((p for p in sorted(outer.rglob("*"))
                              if p.is_file() and p.suffix.lower() == ".ffpfs"), None)
                if inner is None:
                    print("[ERROR] No inner .ffpfs image found inside the .ffpfsc.")
                    sys.exit(1)
                game_unpacked = td / "_game"
                unpack_pfs_image(inner, game_unpacked, mkpfs_cmd_base, mkpfs_cwd, overwrite=True)
                game_root = _patch_find_game_root(game_unpacked)
            elif game_folder.is_dir():
                if args.patch_inplace:
                    game_root = _patch_find_game_root(game_folder)
                else:
                    print("[INFO] Copying the game folder to temp before patching (source untouched)...", flush=True)
                    game_copy = td / "_game"
                    shutil.copytree(game_folder, game_copy)
                    game_root = _patch_find_game_root(game_copy)
            else:
                print(f"[ERROR] Patch game must be a folder or a .ffpfsc: {game_folder}")
                sys.exit(1)

            applied = overlay_patch(game_root, patch_dir)
            print(f"[OK] Applied {applied} patch file(s) onto the game.", flush=True)
            if applied == 0:
                print("[ERROR] The patch contained no files to overlay — nothing to do.")
                sys.exit(1)
            try:
                gtid = _patch_dir_title_id(game_root)
                ptid = _patch_dir_title_id(_patch_descend_wrapper(patch_dir))
                if gtid and ptid and gtid != ptid:
                    print(f"[WARN] Patch title id {ptid} differs from the game {gtid} — packing anyway.", flush=True)
            except Exception:
                pass

            title_id = _patch_dir_title_id(game_root) or "patched"
            with tempfile.TemporaryDirectory(dir=user_temp) as td2:
                temp_pfs = Path(td2) / f"{title_id}.ffpfs"
                pack_folder_uncompressed(
                    game_root, temp_pfs, mkpfs_cmd_base, mkpfs_cwd,
                    verify_enabled=args.verify, temp_folder=Path(td2), **patch_pack_kwargs,
                )
                pass2_kwargs = dict(patch_pack_kwargs)
                if pass2_kwargs.get("compression_level", 7) > 0 and _looks_incompressible(temp_pfs):
                    print("[INFO] Patched image sampled as incompressible — storing without compression.", flush=True)
                    pass2_kwargs["compression_level"] = 0
                if ffpfs_path.exists() and args.overwrite:
                    try:
                        ffpfs_path.unlink()
                    except Exception:
                        pass
                spool_dir, spool_ctx = _open_pass2_spool_dir(temp_pfs, td2, ffpfs_path, spill_base=user_spill)
                try:
                    _assert_pass2_spool_space(temp_pfs, spool_dir)
                    compress_file_to_ffpfsc(
                        temp_pfs, ffpfs_path, mkpfs_cmd_base, mkpfs_cwd,
                        temp_folder=spool_dir, **pass2_kwargs,
                    )
                finally:
                    if spool_ctx is not None:
                        spool_ctx.cleanup()
        print("\n[SUCCESS] Patch integrated successfully!")
        return

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
            # Unwrap nested images all the way to a folder (ffpfsc -> inner .ffpfs ->
            # game files, or ffpfsc -> inner .exfat -> mount+copy), so one unpack action
            # yields a folder regardless of how the image was packed.
            if getattr(args, "unwrap", True):
                _fully_unwrap(current_output_dir, mkpfs_cmd_base, mkpfs_cwd)
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

        # An explicit .ffpfsc output is a single-FILE target — never mkdir it into a
        # directory, even under --batch. (--batch is a backend folder-scan mode that
        # expects a directory output; the GUI hands a descriptive .ffpfsc file path.)
        explicit_file = ffpfs_path.suffix.lower() in (".ffpfsc", ".ffpfs")
        # Guard: --batch writes one file per game; a single explicit output FILE path would
        # make every game overwrite the SAME file (only the last survives). Refuse it.
        if args.batch and explicit_file and len(game_items) > 1:
            print(f"[ERROR] --batch with a single output file would overwrite all "
                  f"{len(game_items)} games into one file. Point the output at a DIRECTORY "
                  f"for batch mode.")
            sys.exit(1)
        if args.batch and not explicit_file:
            ffpfs_path.mkdir(parents=True, exist_ok=True)
        elif not ffpfs_path.is_dir() and not ffpfs_path.suffix:
            ffpfs_path.mkdir(parents=True, exist_ok=True)

        for item in game_items:
            title_id = get_title_id(item)
            # Uncompressed output (.ffpfs) applies to the PFS family — a game folder or a
            # .ffpfs source; .exfat/.ffpkg are always compressed to .ffpfsc.
            src_pfs_family = item.is_dir() or item.suffix.lower() == ".ffpfs"
            uncompressed = getattr(args, "no_compress", False) and src_pfs_family
            ext = ".ffpfs" if uncompressed else ".ffpfsc"

            if (args.batch and not explicit_file) or ffpfs_path.is_dir():
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

            # OPT-IN exFAT path (--via-exfat): build an exFAT image of the game folder and
            # compress THAT into the .ffpfsc — PSBrew's most-stable workflow, wrapping a
            # real exFAT volume the PS5 reads natively instead of the folder PFS builder.
            # On non-macOS / hdiutil failure it returns None and we fall through to two-pass.
            if getattr(args, "via_exfat", False) and item.is_dir():
                with tempfile.TemporaryDirectory(dir=user_temp) as exdir:
                    exfat_img = _build_exfat_image(item, Path(exdir), title_id)
                    if exfat_img is not None:
                        spool_dir, spool_ctx = _open_pass2_spool_dir(exfat_img, exdir, current_ffpfs_path, spill_base=user_spill)
                        try:
                            _assert_pass2_spool_space(exfat_img, spool_dir)
                            print("[INFO] Compressing exFAT image -> .ffpfsc...", flush=True)
                            compress_file_to_ffpfsc(
                                exfat_img, current_ffpfs_path, mkpfs_cmd_base, mkpfs_cwd,
                                temp_folder=spool_dir, **pack_kwargs,
                            )
                        finally:
                            if spool_ctx is not None:
                                spool_ctx.cleanup()
                        continue   # done with this item; skip the two-pass folder build
                print("[WARN] Falling back to the two-pass folder image (exFAT path unavailable).", flush=True)

            if uncompressed and item.is_file() and item.suffix.lower() == '.ffpfs':
                # Uncompressed output + already a PFS image → emit the .ffpfs directly (copy;
                # no compression, no temp). Re-pack of a .ffpfs to a faster uncompressed copy.
                if item.resolve() != current_ffpfs_path.resolve():
                    print(f"[INFO] Uncompressed output — copying {item.name} -> {current_ffpfs_path.name}", flush=True)
                    shutil.copy2(item, current_ffpfs_path)
                else:
                    print("[INFO] Source already IS the requested .ffpfs output — nothing to do.", flush=True)
            elif item.is_file() and item.suffix.lower() in ('.exfat', '.ffpkg', '.ffpfs'):
                # Direct image (.exfat / .ffpkg / .ffpfs) → .ffpfsc (single-file streaming
                # path). A .ffpfs is re-wrapped into its native compressed container. The
                # source is read in place; route the spool to the SSD temp if it fits, else
                # spill onto the output drive (same adaptive rule as the folder path).
                spool_dir, spool_ctx = _open_pass2_spool_dir(item, user_temp, current_ffpfs_path, spill_base=user_spill)
                try:
                    compress_file_to_ffpfsc(
                        item, current_ffpfs_path, mkpfs_cmd_base, mkpfs_cwd,
                        temp_folder=spool_dir,
                        **pack_kwargs,
                    )
                finally:
                    if spool_ctx is not None:
                        spool_ctx.cleanup()
            else:
                # Game folder: build an UNCOMPRESSED inner PFS image, then wrap it in a
                # compressed PFSC container (.ffpfsc). This two-pass "wrapper" flow is
                # REQUIRED, not an inefficiency: packing a game folder directly with
                # per-file PFSC compression (single-pass "pack folder --compress") builds
                # a valid-looking, locally-verifiable image that the PS5 console MISREADS
                # (upstream MkPFS issue #49 — see the warning in backend/mkpfs/cli.py). A
                # green local build/verify is NOT proof of console correctness, so never
                # "optimize" this into single-pass to save the temp intermediate.
                if uncompressed:
                    # Uncompressed deliverable: build the inner PFS image STRAIGHT to the
                    # output (.ffpfs) and stop — no pass 2, no compressed spool. Faster to
                    # build and (per ShadowMountPlus) far faster to mount; full size on disk.
                    print("[INFO] Uncompressed output — building the PFS image directly to "
                          f"{current_ffpfs_path.name} (skipping pass-2 compression).", flush=True)
                    current_ffpfs_path.parent.mkdir(parents=True, exist_ok=True)
                    pack_folder_uncompressed(
                        item, current_ffpfs_path, mkpfs_cmd_base, mkpfs_cwd,
                        verify_enabled=args.verify,
                        temp_folder=Path(user_temp) if user_temp else None,
                        **pack_kwargs,
                    )
                    continue   # done with this item — no pass 2
                with tempfile.TemporaryDirectory(dir=user_temp) as temp_dir:
                    temp_pfs = Path(temp_dir) / f"{title_id}.ffpfs"

                    pack_folder_uncompressed(
                        item, temp_pfs, mkpfs_cmd_base, mkpfs_cwd,
                        verify_enabled=args.verify,
                        temp_folder=Path(temp_dir),
                        **pack_kwargs,
                    )

                    # Incompressible-image fast path: if the inner image barely shrinks
                    # (already-compressed game assets — common; gain ~0%), pass 2 at
                    # compression-level 0 stores every block raw, which is exactly what the
                    # per-block threshold produces for incompressible data anyway. Same
                    # .ffpfsc, but without spending CPU on millions of futile deflate
                    # attempts over a ~150 GB image.
                    pass2_kwargs = dict(pack_kwargs)
                    if pass2_kwargs.get("compression_level", 7) > 0 and _looks_incompressible(temp_pfs):
                        print("[INFO] Inner image sampled as incompressible — storing without "
                              "compression (level 0) to skip wasted CPU; the .ffpfsc is the same "
                              "size either way.", flush=True)
                        pass2_kwargs["compression_level"] = 0
                    # Adaptive pass-2 spool: keep the inner image on temp_dir (the SSD the
                    # GUI chose) and put the spool there too if it still fits beside it,
                    # else spill the spool onto the output drive — so a big game still
                    # compresses off the fast drive instead of falling entirely to the HDD.
                    spool_dir, spool_ctx = _open_pass2_spool_dir(temp_pfs, temp_dir, current_ffpfs_path, spill_base=user_spill)
                    try:
                        _assert_pass2_spool_space(temp_pfs, spool_dir)
                        compress_file_to_ffpfsc(
                            temp_pfs, current_ffpfs_path, mkpfs_cmd_base, mkpfs_cwd,
                            temp_folder=spool_dir,
                            **pass2_kwargs,
                        )
                    finally:
                        if spool_ctx is not None:
                            spool_ctx.cleanup()

                    if args.keep_pfs:
                        saved = current_ffpfs_path.parent / f"{title_id}.ffpfs"
                        print(f"[INFO] Saving intermediate PFS image to {saved}...")
                        # move (not copy): pass 2 already consumed temp_pfs, and the
                        # TemporaryDirectory is about to delete it — relocating frees the
                        # SSD copy instead of leaving it to be wiped.
                        try:
                            shutil.move(str(temp_pfs), str(saved))
                        except Exception as e:
                            print(f"[WARN] Could not save intermediate PFS image: {e}")

    print("\n[SUCCESS] All operations completed successfully!")


if __name__ == "__main__":
    main()
