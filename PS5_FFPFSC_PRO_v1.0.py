
from __future__ import annotations

import os
import re
import sys
import time
import json
import queue
import hashlib
import zipfile
import threading
import subprocess
import signal
import shutil
import multiprocessing
from pathlib import Path


def _prepare_multiprocessing_runtime() -> None:
    """Keep frozen multiprocessing children from starting the GUI."""
    multiprocessing.freeze_support()


def _bundled_backend_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "backend"
    return Path(__file__).resolve().parent / "backend"


def _run_packaged_cli_mode() -> None:
    """Run bundled backend entry points without requiring an external Python."""
    if len(sys.argv) <= 1 or sys.argv[1] not in {"--backend-internal", "--mkpfs-internal"}:
        return

    if sys.argv[1] == "--mkpfs-internal" and sys.platform == "darwin":
        # 'fork' avoids re-exec'ing the frozen app per worker (which would re-launch
        # the GUI = fork bomb). It is only safe here because this --mkpfs-internal
        # process is pure compute (zlib/file IO) and has NOT initialized any macOS
        # framework (Cocoa/CoreFoundation) — fork() after that is unsafe on macOS.
        # KEEP this branch framework-free: do NOT import tkinter/customtkinter/PIL
        # (or anything that pulls them in) before the pool runs.
        try:
            multiprocessing.set_start_method("fork", force=True)
        except RuntimeError:
            pass

    backend_dir = _bundled_backend_dir()
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    if sys.argv[1] == "--mkpfs-internal":
        from mkpfs.cli import cli_mkpfs_main

        sys.exit(cli_mkpfs_main(sys.argv[2:]))

    import runpy

    cli_py = backend_dir / "cli.py"
    sys.argv = [str(cli_py)] + sys.argv[2:]
    runpy.run_path(str(cli_py), run_name="__main__")
    sys.exit(0)


_prepare_multiprocessing_runtime()
_run_packaged_cli_mode()

from tkinter import filedialog, messagebox
import tkinter as tk

try:
    import customtkinter as ctk
except ImportError:
    raise SystemExit("Missing customtkinter. Run: py -m pip install customtkinter")

try:
    from PIL import Image, ImageTk
except Exception:
    Image = None
    ImageTk = None

try:
    import winsound
except Exception:
    winsound = None

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _HAS_DND = True
except Exception:
    TkinterDnD = None
    DND_FILES = None
    _HAS_DND = False

APP_NAME = "PS5 FFPFSC PRO"
APP_VERSION = "1.0.48"
# For archive sources, the GUI extraction occupies the first slice of a game's overall
# progress; the worker's pack progress is compressed into the remaining tail so the
# whole-game percentage stays monotonic across extraction → pack (see CLIWorker._set_stage
# and the extraction status_update calls).
ARCHIVE_EXTRACT_OVERALL_PCT = 25
BACKEND_NAME = "bizkut/ps5-ffpfs-cli"
MKPFS_NAME    = "MkPFS"
MKPFS_VERSION = "0.0.8"

if sys.platform == "darwin":
    APP_DIR = Path.home() / "Library" / "Application Support" / "PS5_FFPFSC_PRO_BIZKUT"
else:
    APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "PS5_FFPFSC_PRO_BIZKUT"
RAW_LOG_FILE = APP_DIR / "raw_tool_output.log"
FINAL_REPORT_FILE = APP_DIR / "last_result_report.txt"
HISTORY_FILE = APP_DIR / "history.json"
SETTINGS_FILE = APP_DIR / "settings.json"
COMPAT_FILE = APP_DIR / "compatibility.json"
COMMUNITY_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbzUxOxhfNi3cGs2cP1tlX1etWbj62neOuS_mOOnL8ipPaBhNH_weoIJPPiF-wCIBkOH/exec"
)

TITLE_RE = re.compile(r"\b(PPSA\d{5}|CUSA\d{5})\b", re.I)
PROGRESS_RE = re.compile(r"\[(?P<bar>[#\-]{4,})\]\s*(?P<pct>\d{1,3})%\s*(?P<label>.*)", re.I)
PFS_IMAGE_SUFFIXES = {".ffpfs", ".ffpfsc"}
DISK_IMAGE_SUFFIXES = {".exfat", ".ffpkg"}


def _augment_path_for_gui() -> None:
    """macOS/Linux GUI apps launched via Finder/Dock inherit only a minimal
    PATH (/usr/bin:/bin:/usr/sbin:/sbin), so Homebrew / MacPorts tools such as
    7z and unrar are invisible to shutil.which() and subprocess. Prepend the
    common install dirs so the RAR / 7z CLI fallbacks can be located."""
    if os.name == "nt":
        return
    extra = [
        "/opt/homebrew/bin",   # Homebrew (Apple Silicon)
        "/usr/local/bin",      # Homebrew (Intel) / common
        "/opt/local/bin",      # MacPorts
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ]
    parts = os.environ.get("PATH", "").split(os.pathsep)
    parts = [p for p in parts if p]
    for d in extra:
        if d not in parts and os.path.isdir(d):
            parts.append(d)
    os.environ["PATH"] = os.pathsep.join(parts)


_augment_path_for_gui()

# Each constant is a (light_mode, dark_mode) tuple.
# CTk reads the correct value automatically when set_appearance_mode() is called —
# no manual recoloring needed anywhere in the app.
BLACK   = ("#f0f0f0", "#050505")   # main background
PANEL   = ("#e2e2e2", "#111111")   # panel / card background
CARD    = ("#d4d4d4", "#151515")   # entry / inner card
CARD2   = ("#cacaca", "#1a1a1a")   # secondary card / normal button fill
BORDER  = ("#b0b0b0", "#2a2a2a")   # panel border
BORDER2 = ("#999999", "#3a3a3a")   # entry / button border
GREEN   = "#4ade80"                 # accent — looks fine on both backgrounds
GREEN2  = "#22c55e"                 # accent hover
YELLOW  = "#facc15"
RED     = "#ef4444"
WHITE   = ("#111111", "#f8fafc")   # primary text  (dark text in light mode)
MUTED   = ("#555555", "#a1a1aa")   # secondary text


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)


def open_path(path) -> None:
    """Open a file or folder with the default OS handler (cross-platform)."""
    p = str(path)
    try:
        if os.name == "nt":
            os.startfile(p)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception:
        pass


def now_time() -> str:
    return time.strftime("%H:%M:%S")


def now_datetime() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def format_size(num) -> str:
    try:
        num = float(num)
    except Exception:
        return "—"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024 or unit == "TB":
            return f"{num:.2f} {unit}" if unit != "B" else f"{num:.0f} {unit}"
        num /= 1024


def format_duration(seconds) -> str:
    seconds = int(max(0, seconds))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def humanize_eta(raw) -> str:
    """Turn a backend ETA token into hours/minutes/seconds. mkpfs emits raw seconds
    ('ETA 1695s'), which is unreadable past a minute — convert to '1h 05m' / '28m 15s'
    / '45s'. Accepts an optional unit (s/m/h); a bare number is treated as seconds.
    Returns the input unchanged if it can't be parsed, and '—' for the no-ETA marker."""
    if raw is None:
        return "—"
    s = str(raw).strip().lower()
    if s in ("", "—", "-"):
        return "—"
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(h|hr|hrs|hours?|m|min|mins|minutes?|s|sec|secs|seconds?)?$", s)
    if not m:
        return str(raw)
    val = float(m.group(1))
    unit = m.group(2) or "s"
    if unit.startswith("h"):
        total = val * 3600
    elif unit.startswith("m"):   # m / min / minutes
        total = val * 60
    else:
        total = val
    total = int(round(total))
    if total <= 0:
        return "0s"
    h, r = divmod(total, 3600)
    mm, ss = divmod(r, 60)
    if h:
        return f"{h}h {mm:02d}m"
    if mm:
        return f"{mm}m {ss:02d}s"
    return f"{ss}s"


def get_free_space(path: Path) -> int:
    try:
        target = path if path.exists() else path.parent
        usage = shutil.disk_usage(str(target))
        return usage.free
    except Exception:
        return 0


def get_total_space(path: Path) -> int:
    try:
        target = path if path.exists() else path.parent
        usage = shutil.disk_usage(str(target))
        return usage.total
    except Exception:
        return 0


def same_drive(path_a: Path, path_b: Path) -> bool:
    """True if both paths are on the same filesystem/volume.

    Uses the device id (st_dev) — correct on macOS/Linux, where Path.drive is
    always '' and the old comparison wrongly reported every pair as 'same drive'
    (which over-estimated the temp space needed when temp and output were on
    different volumes). Falls back to drive letters only if stat fails."""
    try:
        a = path_a if path_a.exists() else path_a.parent
        b = path_b if path_b.exists() else path_b.parent
        return os.stat(a).st_dev == os.stat(b).st_dev
    except Exception:
        try:
            return path_a.resolve().drive.lower() == path_b.resolve().drive.lower()
        except Exception:
            return str(path_a)[:2].lower() == str(path_b)[:2].lower()


def space_safety_factor() -> float:
    """User-tunable multiplier on the *temp* free-space requirement: 1.0 keeps the
    recommended worst-case headroom; below 1.0 allows tighter fits (higher risk of
    an out-of-space failure mid-pack). Clamped to [0.5, 2.0]. Output-drive needs are
    never scaled down — the final container can be as large as the source."""
    try:
        return min(2.0, max(0.5, float(load_settings().get("space_safety_factor", 1.0))))
    except Exception:
        return 1.0


# Peak SCRATCH-drive multiples of the EXTRACTED game size for one packing run.
# CRUCIAL: for our config (PS5 / 64 KiB blocks / 32-bit inodes / unsigned) MkPFS pass-2
# uses the direct-to-image STREAMING builder — it writes the final container straight to
# the OUTPUT drive with NO spool on the temp drive (verified: a 109.6 GB game left only
# ~113 GB on the temp drive — the inner image alone, no spool). So the temp/build drive
# only ever holds {extracted source (archives) + inner .ffpfs image}, never a third spool
# copy. The inner image is barely larger than the source — 64 KiB block-alignment waste is
# only ~1-2% for real games (large asset/audio/video files): MEASURED 109.6 GB → 113 GB
# image = 1.03x. (Heavy padding only happens for tiny-file games, which fit the SSD anyway.)
# So image ≈ 1.05x; we budget 1.2x for headroom. Hence:
#   ARCHIVE: source(1.0) + image(1.2) = 2.2x  (a 150 GB game = 330 GB ≤ a 353 GB SSD → its
#            source extracts to the SSD too, so the many-file read is fast). Was 2.5/3.7 —
#            those over-budgeted the image (and a non-existent spool) and needlessly pushed
#            ~140-160 GB games' sources onto the slow HDD.
#   INPLACE: image only (1.2x); folder/disk-image sources are read in place, no 2nd copy.
#   PATCH:   a full game copy + image (~3.0x), no spool.
# The "+1.05 same_temp_output_drive" term in estimate_peak_space_needed() adds the final
# container when the build drive IS the output drive. The backend pre-pass-2 assert is the
# final backstop and space_safety_factor() tunes headroom (raise it for more margin).
ARCHIVE_PEAK_FACTOR = 2.2
INPLACE_PEAK_FACTOR = 1.2
PATCH_PEAK_FACTOR   = 3.0

# Free-space multiple for JUST the inner uncompressed PFS image (pass 1) on the temp/SSD
# drive — used by the split path (image on the SSD, source on the output drive). 1.2x covers
# 64 KiB block padding (~1-2% measured) plus a safety margin; matches INPLACE above.
IMAGE_PEAK_FACTOR = 1.2


def archive_set_ondisk_size(archive: Path) -> int:
    """Sum the on-disk bytes of an archive's whole multi-part volume set. A fallback
    extracted-size proxy when headers can't be read — still COMPRESSED, so a rough floor."""
    try:
        base = re.sub(
            r'(\.part\d+\.rar|\.r\d{2,}|\.7z\.\d+|\.zip\.\d+|\.z\d+|\.\d{3}|\.rar|\.zip|\.7z)$',
            '', archive.name, flags=re.I)
        total = 0
        for p in archive.parent.iterdir():
            if p.is_file() and p.name.startswith(base):
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total or (archive.stat().st_size if archive.exists() else 0)
    except Exception:
        try:
            return archive.stat().st_size
        except Exception:
            return 0


def _item_is_single_pass(item) -> bool:
    """True for directly-supplied disk images (.exfat, .ffpkg, .ffpfs etc.) that are
    compressed in a SINGLE pass by the backend — no inner image on the temp drive.
    Derived purely from persistent attributes so it works after queue save/restore."""
    try:
        return (getattr(item, "source_kind", "") == "inplace"
                and not getattr(item, "archive_path", None)
                and Path(getattr(item, "path", "") or "").is_file())
    except Exception:
        return False


def _build_size_of(item) -> int:
    """The honest EXTRACTED size to base space decisions on: the header-read extracted
    size when known, else item.size (already extracted for folders/disk images). For an
    ARCHIVE whose header could not be read (extracted_size == 0), return 0 = UNKNOWN so
    the gate/placement take the safe path (route to the larger drive / defer) instead of
    silently using the much-smaller COMPRESSED size and false-passing onto a small SSD."""
    es = getattr(item, "extracted_size", 0)
    if es:
        return int(es)
    if getattr(item, "source_kind", None) == "archive":
        return 0   # unknown extracted size — do NOT fall back to compressed size
    return int(getattr(item, "size", 0) or 0)


def shows_extracted_size(item) -> bool:
    """True when the size we display is the EXTRACTED (header-read) size, not the on-disk
    size — i.e. a not-yet-extracted archive with a known uncompressed size. (After
    extraction the item flips to source_kind='inplace' and .size is the real folder size.)"""
    return getattr(item, "source_kind", "") == "archive" and getattr(item, "extracted_size", 0) > 0


def display_size(item) -> int:
    """The size to SHOW the user. For an archive, .size is the COMPRESSED volume set; the
    EXTRACTED size (already read from headers, and what space/placement use) is the
    meaningful number, so show that. Falls back to the on-disk .size when the header was
    unreadable (extracted_size == 0). No extra disk I/O — the value is computed at add time."""
    if shows_extracted_size(item):
        return int(item.extracted_size)
    return int(getattr(item, "size", 0) or 0)


# A tempfile.mkdtemp()-created scratch dir is "tmp" + exactly 8 chars from [a-z0-9_]
# (the prefix mkpfs/the backend use). Match that EXACTLY so cleanup never rmtrees an
# unrelated user folder that merely starts with "tmp" (e.g. "tmp_notes", "Tmp Renders").
_APP_TMP_RE = re.compile(r"tmp[a-z0-9_]{8}$")


def _is_app_tmp_dir(name: str) -> bool:
    return bool(_APP_TMP_RE.fullmatch(name))


def _peak_factor_for(item) -> float:
    """Scratch peak multiple for this item's source kind (and auto-patch mode)."""
    try:
        if getattr(item, "patch_source", None) and load_settings().get("auto_integrate_patch", False):
            return PATCH_PEAK_FACTOR
    except Exception:
        pass
    return INPLACE_PEAK_FACTOR if getattr(item, "source_kind", "archive") == "inplace" else ARCHIVE_PEAK_FACTOR


def estimate_peak_space_needed(extracted_size: int, factor: float = ARCHIVE_PEAK_FACTOR,
                               same_temp_output_drive: bool = True) -> int:
    """Peak free space the SCRATCH (build) drive needs. *factor* is the co-resident
    multiple of the EXTRACTED size for {source + inner image + pass-2 spool}. When the
    final .ffpfsc also lands on this drive (scratch == output), add ~1x for it."""
    mult = factor + (1.05 if same_temp_output_drive else 0.0)
    return int(extracted_size * mult * space_safety_factor())


def estimate_image_space_needed(extracted_size: int) -> int:
    """Free space the temp/SSD drive needs for JUST the inner uncompressed PFS image
    (pass 1), in the split path where the pass-2 spool is placed adaptively elsewhere."""
    return int(extracted_size * IMAGE_PEAK_FACTOR * space_safety_factor())


# Realistic final-.ffpfsc size as a fraction of the UNPACKED game. Measured over 70+
# runs: mean ~0.59, worst ~0.87. 0.75 leaves headroom above the mean without reserving
# the full incompressible worst case (which needlessly skipped easily-fitting games like
# Astro Bot: 148 GB unpacked → 37 GB .rar, but the old 1.05x reserved ~156 GB on the
# output drive and skipped it on a 131 GB-free disk).
COMPRESSED_OUTPUT_RATIO = 0.75


def estimate_output_space_needed(game_size: int, compressed: bool = True,
                                 known_packed: int = 0) -> int:
    """Free space the OUTPUT drive must have for the final container.

    UNCOMPRESSED (.ffpfs): the container IS the full inner image → 1.05x (block
    alignment + headers).

    COMPRESSED (.ffpfsc): reserving 1.05x the UNPACKED size ignores the whole point of
    compression and skips titles that comfortably fit. Reserve a realistic fraction of
    the unpacked size (COMPRESSED_OUTPUT_RATIO), floored by the known compressed source
    set (*known_packed*, the .rar volume total) × 1.25 so a barely-compressible game
    (large .rar) is still reserved near full size, and capped at the incompressible 1.05x
    worst case so it never over-reserves. The backend's pre-pass-2 assert is the hard
    backstop if a title compresses worse than estimated (a clean late skip, no corruption)."""
    worst = int(game_size * 1.05)
    if not compressed:
        return worst
    est = int(game_size * COMPRESSED_OUTPUT_RATIO)
    if known_packed:
        est = max(est, int(known_packed * 1.25))
    return min(est, worst)


def _space_preflight_ok(item, temp_dir: Path, out_dir: Path) -> bool:
    """True only if the chosen placement can complete.

    Two shapes, driven by how _resolve_extract_root placed this run:
      • Split (item._image_only_on_temp): only the inner image lives on temp_dir (the SSD);
        the pass-2 spool is routed adaptively by the backend (temp if it still fits beside
        the image, else the output drive) and the source/final live on the output drive. So
        the SSD only needs the image, and the output drive needs source(when extracted
        there) + final + a spool fallback (only when temp can't also hold the spool).
      • One-drive: the whole scratch (source + image + spool) sits on temp_dir; the output
        drive only needs the final container (skipped when it's the same volume)."""
    temp_dir, out_dir = Path(temp_dir), Path(out_dir)
    size = _build_size_of(item)
    if size <= 0:
        return True   # unknown size — placement used the larger drive; the backend asserts
    # Output-drive reservation reflects the chosen output format (compressed → realistic,
    # uncompressed → full size) and, for archives, the known compressed source-set size.
    comp  = bool(getattr(item, "_output_compressed", True))
    known = int(getattr(item, "size", 0) or 0) if getattr(item, "source_kind", "") == "archive" else 0
    if _item_is_single_pass(item):
        # Single-pass: mkpfs compresses the disk image directly — no inner image on temp,
        # no spool. Only the output drive needs space for the final .ffpfsc.
        return get_free_space(out_dir) >= estimate_output_space_needed(size, comp, known)
    if getattr(item, "_extract_on_pool", False):
        # Two-fast-drive split: inner image on _build_temp (one SSD), extracted source on
        # _build_root (another SSD), final on the output drive. Check each independently.
        # No pass-2 spool term: our config (--inode-bits 32, --block-size 65536, unsigned)
        # streams pass 2 directly to the image with NO spool, so the image drive needs only
        # the image (~1.2x) and the extract drive only the source (~1x) — matching the
        # router's stage-2 placement test. (The backend's pre-pass-2 assert is the backstop.)
        image_dir   = Path(getattr(item, "_build_temp", temp_dir))
        extract_dir = Path(getattr(item, "_build_root", image_dir))
        return (get_free_space(image_dir)   >= estimate_image_space_needed(size)
                and get_free_space(extract_dir) >= int(size)
                and get_free_space(out_dir)  >= estimate_output_space_needed(size, comp, known))
    if getattr(item, "_image_only_on_temp", False):
        temp_free = get_free_space(temp_dir)
        image_need = estimate_image_space_needed(size)
        image_ok = temp_free >= image_need
        spool_need = int(size * 1.10)
        spool_fits_temp = (temp_free - image_need) >= spool_need
        # The source copy is reserved only before extraction (archive); afterwards it is
        # already on the output drive and counted in its free space.
        src_on_out = size if getattr(item, "source_kind", "") == "archive" else 0
        out_need = src_on_out + estimate_output_space_needed(size, comp, known) + (0 if spool_fits_temp else spool_need)
        return image_ok and get_free_space(out_dir) >= out_need
    same = same_drive(temp_dir, out_dir)
    factor = _peak_factor_for(item)
    temp_ok = get_free_space(temp_dir) >= estimate_peak_space_needed(size, factor, same)
    out_ok = same or get_free_space(out_dir) >= estimate_output_space_needed(size, comp, known)
    return temp_ok and out_ok


def get_folder_size(path: Path) -> int:
    return folder_size(path) if path.exists() else 0


def find_newest_ffpfsc_after(folder: Path, started_at: float):
    try:
        if not folder.exists():
            return None
        candidates = []
        # Both the compressed (.ffpfsc) and uncompressed (.ffpfs) deliverables.
        for pat in ("*.ffpfsc", "*.ffpfs"):
            for p in folder.glob(pat):
                try:
                    if p.is_file() and p.stat().st_size > 0 and p.stat().st_mtime >= started_at - 2:
                        candidates.append(p)
                except OSError:
                    pass
        if not candidates:
            return None
        return max(candidates, key=lambda x: x.stat().st_mtime)
    except Exception:
        return None


def compression_rating(saved_pct: float) -> tuple[str, str]:
    if saved_pct >= 25:
        return "EXCELLENT", "Great compression candidate. This title is worth keeping compressed."
    if saved_pct >= 10:
        return "GOOD", "Good result. Compression is likely worth it."
    if saved_pct >= 5:
        return "OKAY", "Small but usable savings. Keep only if storage is tight."
    return "POOR", "Not worth compressing. This title is already highly compressed or not a good candidate."


_DRIVE_TYPE_CACHE: dict = {}


def _drive_cache_key(path: Path):
    try:
        t = path if path.exists() else path.parent
        return os.stat(str(t)).st_dev
    except Exception:
        return str(path)


def get_drive_type(path: Path) -> str:
    """Detect SSD/NVMe vs HDD for the volume holding *path*: 'SSD', 'HDD' or 'Unknown'.
    Cached per device. This MAY SHELL OUT (PowerShell on Windows, diskutil on macOS), so
    call it OFF the UI thread; use drive_type_cached() on the main thread."""
    key = _drive_cache_key(path)
    if key in _DRIVE_TYPE_CACHE:
        return _DRIVE_TYPE_CACHE[key]
    dt = _probe_drive_type(path)
    _DRIVE_TYPE_CACHE[key] = dt
    return dt


def drive_type_cached(path: Path) -> str:
    """Non-blocking: the already-probed drive type for *path*, or 'Unknown' if it hasn't
    been probed yet. Safe on the UI thread — never shells out."""
    return _DRIVE_TYPE_CACHE.get(_drive_cache_key(path), "Unknown")


def temp_drive_label(path: Path) -> str:
    """Honest label for the temp/scratch drive: 'SSD temp' ONLY when we have actually
    confirmed solid-state, otherwise the neutral 'temp drive'. We never call a drive an SSD
    on assumption — an external HDD (or an un-probed drive) must not be mislabelled."""
    return "SSD temp" if drive_type_cached(path) == "SSD" else "temp drive"


KEEP_AWAKE_FILENAME = ".ffpfsc_keepalive"


def poke_drive_keepalive(d: Path) -> bool:
    """Force a tiny physical write to the drive holding *d* and flush it to the device,
    so an idle external HDD doesn't park its heads / spin down. Bus-powered 2.5" USB
    drives (e.g. WD Elements) park aggressively after a few seconds idle; that burns
    through their limited load/unload cycle rating. A flushed write resets the drive's
    idle timer. Reuses one hidden file (overwrite, not create/delete) to avoid directory
    churn. Returns True on success. Safe to call only OFF the UI thread."""
    try:
        f = d / KEEP_AWAKE_FILENAME
        with open(f, "wb") as fh:
            fh.write(b"ffpfsc keep-alive\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except Exception:
        return False


def _probe_drive_type(path: Path) -> str:
    """Actually probe the drive type (may block ~1-2 s). See get_drive_type."""
    try:
        target = path if path.exists() else path.parent
    except Exception:
        target = path
    # ── Windows: PowerShell MediaType / BusType ──────────────────────────────
    if os.name == "nt":
        try:
            drive_letter = path.resolve().drive.rstrip(":\\")
            if not drive_letter:
                return "Unknown"
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-Partition -DriveLetter '{drive_letter}' | Get-Disk | Select-Object -ExpandProperty MediaType"],
                capture_output=True, text=True, timeout=6,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            media = result.stdout.strip().upper()
            if "SSD" in media or "NVM" in media:
                return "SSD"
            if "HDD" in media or "UNSPECIFIED" in media:
                if "UNSPECIFIED" in media:
                    result2 = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         f"Get-Partition -DriveLetter '{drive_letter}' | Get-Disk | Select-Object -ExpandProperty BusType"],
                        capture_output=True, text=True, timeout=6,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    bus = result2.stdout.strip().upper()
                    if "NVME" in bus or "SATA" in bus:
                        return "SSD"
                    if "ATA" in bus:
                        return "HDD"
                return "HDD"
        except Exception:
            pass
        return "Unknown"
    # ── macOS: diskutil 'Solid State: Yes/No' on the backing device ──────────
    if sys.platform == "darwin":
        try:
            df = subprocess.run(["df", str(target)], capture_output=True, text=True, timeout=6)
            dev = df.stdout.strip().splitlines()[-1].split()[0]   # /dev/diskXsY
            whole = re.sub(r"s\d+$", "", dev)                     # /dev/diskX (whole disk)
            for d in ([dev, whole] if whole != dev else [dev]):
                info = subprocess.run(["diskutil", "info", d], capture_output=True, text=True, timeout=8)
                for line in info.stdout.splitlines():
                    if "Solid State" in line:
                        return "SSD" if "Yes" in line else "HDD"
        except Exception:
            pass
        return "Unknown"
    # ── Linux: /sys/dev/block rotational flag (0 = SSD, 1 = HDD) ──────────────
    try:
        st = os.stat(str(target))
        node = Path(f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}")
        disk = node.resolve()
        if (disk / "partition").exists():
            disk = disk.parent
        rot = disk / "queue" / "rotational"
        if rot.exists():
            return "HDD" if rot.read_text().strip() == "1" else "SSD"
    except Exception:
        pass
    return "Unknown"


def get_filesystem_type(path: Path) -> str:
    """Return filesystem label (NTFS, exFAT, FAT32 …) using GetVolumeInformationW."""
    if os.name != "nt":
        return "Unknown"
    try:
        import ctypes
        target = path if path.exists() else path.parent
        drive = str(target.resolve()).split("\\")[0] + "\\"  # e.g. "C:\\"
        buf = ctypes.create_unicode_buffer(64)
        ctypes.windll.kernel32.GetVolumeInformationW(
            drive, None, 0, None, None, None, buf, ctypes.sizeof(buf)
        )
        return buf.value.strip() or "Unknown"
    except Exception:
        return "Unknown"


def is_game_folder(path: Path) -> bool:
    """Return True if *path* looks like a PS5 game folder."""
    return path.is_dir() and (path / "sce_sys").is_dir() and (path / "eboot.bin").is_file()


def find_game_folders(root: Path, max_depth: int = 3) -> list[Path]:
    """Recursively find all PS5 game subfolders under *root* (up to max_depth levels)."""
    found: list[Path] = []

    def _scan(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        if is_game_folder(path):
            found.append(path)
            return  # don't recurse inside a game folder
        try:
            for child in sorted(path.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    _scan(child, depth + 1)
        except (PermissionError, OSError):
            pass

    _scan(root, 0)
    return found


def find_files_by_suffix(root: Path, suffixes: set[str], max_depth: int = 6) -> list[Path]:
    """Recursively find files with selected suffixes without walking unbounded trees."""
    found: list[Path] = []

    def _scan(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            for child in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                if child.is_file() and child.suffix.lower() in suffixes:
                    found.append(child)
                elif child.is_dir() and not child.name.startswith("."):
                    _scan(child, depth + 1)
        except (PermissionError, OSError):
            pass

    if root.is_file() and root.suffix.lower() in suffixes:
        return [root]
    if root.is_dir():
        _scan(root, 0)
    return found


def has_any_files(root: Path) -> bool:
    try:
        if root.is_file():
            return True
        return any(p.is_file() for p in root.rglob("*"))
    except Exception:
        return False


def validate_game_structure(path: Path) -> list[str]:
    """Return a list of human-readable warnings for incomplete PS5 game folders."""
    warnings: list[str] = []
    sce_sys   = path / "sce_sys"
    param_json = sce_sys / "param.json"
    eboot     = path / "eboot.bin"
    if not sce_sys.is_dir():
        warnings.append("sce_sys folder not found — this may not be a PS5 game dump.")
    elif not param_json.is_file():
        warnings.append("sce_sys/param.json missing — ShadowMount compatibility not guaranteed.")
    if not eboot.is_file():
        warnings.append("eboot.bin not found — the dump may be incomplete.")
    return warnings


# Maps log keywords → user-friendly cause + fix.
# Order matters: smart_error_from_log() returns the FIRST keyword found in the
# log, so the most specific causes (post-pack verify mismatches) come first —
# otherwise an incidental keyword like "memoryerror" elsewhere in the output
# would mask the real diagnosis.
_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("missing in image",
     "Verify failed: a file in the source folder is missing from the packed image.\n"
     "Open the Logs tab and search for 'missing in image:' to see the exact file."),
    ("extra in image",
     "Verify failed: the image contains a file that is not in the source folder.\n"
     "Open the Logs tab and search for 'extra in image:' to see the exact file."),
    ("flat_path_table",
     "Verify failed: the image's path table does not match the source tree.\n"
     "See the Logs tab for the mismatching entry."),
    ("unable to stage source file",
     "Temp drive does not support hardlinks or symlinks.\n"
     "Fix: use a temp folder on an NTFS-formatted SSD/NVMe."),
    ("hard link and symlink both failed",
     "Temp drive does not support hardlinks or symlinks.\n"
     "Fix: use a temp folder on an NTFS-formatted SSD/NVMe."),
    ("memoryerror",
     "Not enough RAM during compression.\n"
     "Fix: lower CPU cores to 2 or 1, or set compression Level to 5."),
    ("no space left on device",
     "Drive ran out of space mid-compression.\n"
     "Fix: free up space on the temp or output drive."),
    ("no such file or directory",
     "A required file was not found — the game folder may be incomplete."),
    ("could not find any valid game",
     "No valid PS5 game folders detected.\n"
     "Fix: select the folder that contains sce_sys and eboot.bin."),
    ("missing/invalid param.json",
     "param.json is missing or corrupt — not a valid PS5 game dump."),
    ("permission denied",
     "Access denied.\n"
     "Fix: run as administrator, or move files off a read-only drive."),
    ("winerror 5",
     "Access denied (WinError 5).\n"
     "Fix: run as administrator."),
    ("winerror 1",
     "Windows system error (WinError 1).\n"
     "Fix: run as administrator."),
    ("calledprocesserror",
     "A backend subprocess failed — check the raw log for details."),
]


def smart_error_from_log() -> str:
    """Scan the raw log file and return a user-friendly error string, or ''."""
    if not RAW_LOG_FILE.exists():
        return ""
    try:
        text = RAW_LOG_FILE.read_text(encoding="utf-8", errors="ignore").lower()
    except Exception:
        return ""
    for keyword, message in _ERROR_PATTERNS:
        if keyword in text:
            return message
    return ""


def get_backend_python_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--backend-internal"]
    return [sys.executable]


def backend_base_dir() -> Path:
    return _bundled_backend_dir()


def folder_size(path: Path) -> int:
    total = 0
    try:
        if path.is_file():
            return path.stat().st_size
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    except Exception:
        pass
    return total


def file_count(path: Path) -> int:
    try:
        if path.is_file():
            return 1
        return sum(1 for p in path.rglob("*") if p.is_file())
    except Exception:
        return 0


def parse_title_id(path: Path) -> str:
    # Prefer the game's OWN folder name, then its param.json; only fall back to the
    # full path last — a title id in a PARENT directory (e.g. a "[CUSA12345]" dump
    # folder) must not win over the game's own id.
    m = TITLE_RE.search(path.name)
    if m:
        return m.group(1).upper()
    try:
        for p in path.rglob("param.json"):
            text = p.read_text(encoding="utf-8", errors="ignore")
            m = TITLE_RE.search(text)
            if m:
                return m.group(1).upper()
    except Exception:
        pass
    m = TITLE_RE.search(str(path))   # last resort: anywhere in the path
    if m:
        return m.group(1).upper()
    return "Unknown"


def guess_game_name(path: Path) -> str:
    # 1. Try param.json for the real localised title first
    for candidate in (path / "sce_sys" / "param.json",
                      path / "sce_sys" / "param.sfo"):   # sfo handled below
        pass  # only param.json is plaintext
    param = path / "sce_sys" / "param.json"
    if param.exists():
        try:
            import json as _json
            data = _json.loads(param.read_text(encoding="utf-8", errors="replace"))
            # param.json structure: {"titleId":..., "localizedParameters":{"defaultLanguage":"en-US", "en-US":{"titleName":"..."}}}
            loc = data.get("localizedParameters", {})
            default_lang = loc.get("defaultLanguage", "")
            title = (loc.get(default_lang, {}).get("titleName", "")
                     or loc.get("en-US", {}).get("titleName", "")
                     or next((v.get("titleName", "") for v in loc.values()
                               if isinstance(v, dict) and v.get("titleName")), ""))
            if title:
                return title.strip()
        except Exception:
            pass

    # 2. Fall back to folder name, cleaning up common PS5 dump suffixes
    name = path.name
    # Strip "-app" / "_app" suffix (e.g. PPSA04264-app → PPSA04264)
    # Do NOT use parent folder — it is often a generic dump dir like "PS5 DUMPS"
    name = re.sub(r"[-_]app$", "", name, flags=re.I)
    name = re.sub(r"\s*\[.*?\]\s*", " ", name)
    name = re.sub(r"-\[.*?\]", "", name)
    return name.replace("_", " ").strip(" -") or path.name


def guess_game_version(path: Path) -> str:
    """Best-effort game/content version, or '' if unknown. Prefers the dump's own
    param.json (contentVersion = authoritative), falling back to the folder name.
    Accepts both the short form '01.004' AND the full PS5 form '01.200.000' — the old
    regex rejected the full form, so a patched game's real contentVersion was skipped and
    it fell back to masterVersion ('01.00'), mislabelling patched games as v01.00."""
    # X.YY / X.YYY with an optional third group (the full PS5 XX.YYY.ZZZ version).
    _VER = r"\d{1,2}\.\d{2,3}(?:\.\d{2,3})?"
    try:
        param = path / "sce_sys" / "param.json"
        if param.exists():
            data = json.loads(param.read_text(encoding="utf-8", errors="replace"))
            for key in ("contentVersion", "masterVersion", "appVer", "app_ver", "version"):
                v = str(data.get(key, "")).strip()
                if re.fullmatch(_VER, v):
                    return v
    except Exception:
        pass
    # Fall back to a version pattern in the source folder name (e.g. "…01.004…")
    m = re.search(rf"\bv?({_VER})\b", path.name)
    return m.group(1) if m else ""


# ── AMPR / APR (PlayGo) support ───────────────────────────────────────────────
# APR = a PlayGo game (streamed/chunked delivery, marked by sce_sys/playgo-chunk.dat).
# AMPR = the emu shim it needs to boot from a compressed container: two user-supplied
# .sprx files injected into a fakelib/ folder, plus an ampr_emu.index. No file format —
# a game category + injected runtime files. See _build_ampr_index for the index layout.
AMPR_SPRX_FILES = ["libSceAmpr.sprx", "libScePlayGo.sprx"]


def is_apr_game(path) -> bool:
    """True if *path* is a game folder using PlayGo (an APR title)."""
    try:
        sce = Path(path) / "sce_sys"
        return (sce / "playgo-chunk.dat").exists() or (sce / "playgo_chunk.dat").exists()
    except Exception:
        return False


# Two filename-length ceilings, BOTH in UTF-8 BYTES (the filesystem and ShadowMountPlus
# checks count bytes, not characters — a "™" is 3 bytes, not 1):
#  • MAX_FILENAME_BYTES — the filesystem hard cap (exFAT 255 UTF-16 units / APFS 255 bytes).
#    Used by the general sanitiser so no path component is ever filesystem-illegal.
#  • SHADOWMOUNT_NAME_LIMIT — the stricter limit ShadowMountPlus enforces on the .ffpfsc
#    FILENAME: it rejects longer names with ENAMETOOLONG ("Dateiname zu lang"). EMPIRICAL
#    (2026-06-21): a 59-byte name mounts, a 69-byte name fails → the real cap is ~64. Set
#    conservatively to 63 (one under the likely char[64] buffer) so generated names always
#    fit. The output namer budgets against THIS value — change it in one place if the exact
#    constant turns out different.
MAX_FILENAME_BYTES = 255
SHADOWMOUNT_NAME_LIMIT = 63


def _truncate_to_bytes(s: str, max_bytes: int) -> str:
    """Trim *s* so its UTF-8 encoding is <= *max_bytes*, never splitting a character."""
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", "ignore")


def short_version(ver: str) -> str:
    """Collapse a PS5 version to two groups and drop any leading 'v' for filenames:
    'v01.007.000' -> '01.007', '02.001.010' -> '02.001', '01.030' -> '01.030'."""
    if not ver:
        return ver
    m = re.match(r"v*(\d{1,2}\.\d{2,3})", ver)
    return m.group(1) if m else ver.lstrip("v")


def sanitize_filename(s: str) -> str:
    """Make *s* safe as a cross-platform filename component (keeps spaces,
    brackets, &, etc.; strips path separators and reserved characters), and cap it
    to the filesystem's per-name limit."""
    s = s.replace("/", "-").replace("\\", "-").replace(":", "-")
    s = re.sub(r"[™®©℠℗]", "", s)   # ™ ® © ℠ ℗ — waste bytes, no value on a console drive
    s = re.sub(r'[*?"<>|\x00-\x1f]', "", s)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    return _truncate_to_bytes(s, MAX_FILENAME_BYTES).strip()


# Redundant "edition" qualifiers dropped from an output filename ONLY when the full name
# would otherwise exceed SHADOWMOUNT_NAME_LIMIT (so games that fit keep their full title).
# Longest/most-specific phrases first; an optional leading separator (- – — :) is eaten too.
_EDITION_FLUFF_RE = re.compile(
    r"\s*[-–—:]?\s*\b("
    r"\d{1,3}(?:st|nd|rd|th)\s+anniversary\s+edition"
    r"|game\s+of\s+the\s+year\s+edition|goty\s+edition"
    r"|complete\s+edition|definitive\s+edition|enhanced\s+edition"
    r"|deluxe\s+edition|ultimate\s+edition|standard\s+edition"
    r"|special\s+edition|gold\s+edition|premium\s+edition"
    r"|anniversary\s+edition|remastered|remaster"
    r")\b",
    re.IGNORECASE)


def _strip_edition_fluff(name: str) -> str:
    """Remove redundant 'edition'/'remastered' qualifiers and tidy leftover separators.
    Used only as a fallback when a name is over the ShadowMount length budget."""
    out = _EDITION_FLUFF_RE.sub("", name)
    out = re.sub(r"\s{2,}", " ", out).strip(" -–—:")
    return out


def descriptive_ffpfsc_name(item, ext: str = ".ffpfsc") -> str:
    """Build a findable output filename for *item*:
    '<Game Name> [<TITLEID>] [v<version>]<ext>'  (version omitted if unknown).
    Falls back to the title id alone if the name is missing. *ext* is '.ffpfsc'
    (compressed) or '.ffpfs' (uncompressed)."""
    if not ext.startswith("."):
        ext = "." + ext
    PLACEHOLDERS = {"Unknown", "📦", "💾", "📤", ""}
    tid = (getattr(item, "title_id", "") or "").strip()
    if tid in PLACEHOLDERS:
        tid = ""
    name = (getattr(item, "name", "") or "").strip()
    if name in PLACEHOLDERS or name == tid:
        name = tid or "output"
    suffix_parts = []
    if tid and tid.lower() not in name.lower():
        suffix_parts.append(f"[{tid}]")
    src = getattr(item, "path", None)
    ver = guess_game_version(src) if isinstance(src, Path) else ""
    if ver:
        # Shortened, 'v'-less version tag (e.g. [01.007]) — matches shorten_ffpfsc_versions.sh.
        suffix_parts.append(f"[{short_version(ver)}]")
    suffix = (" " + " ".join(suffix_parts)) if suffix_parts else ""
    # Reserve room (by UTF-8 bytes) for the [version][TITLEID] suffix + extension so those
    # collision-resistant tags survive the filename-length cap instead of being truncated.
    if item is not None:
        try:
            item._name_was_truncated = False
            item._name_fluff_stripped = False
        except Exception:
            pass
    name_budget = max(20, SHADOWMOUNT_NAME_LIMIT - len(suffix.encode("utf-8")) - len(ext.encode("utf-8")))
    clean = sanitize_filename(name)
    fluff_stripped = False
    if len(clean.encode("utf-8")) > name_budget:
        # Over budget — first drop redundant edition qualifiers (much nicer than a blunt
        # cut). Only adopt the result if it actually shortened to something non-empty.
        reduced = sanitize_filename(_strip_edition_fluff(clean))
        if reduced and len(reduced.encode("utf-8")) < len(clean.encode("utf-8")):
            clean = reduced
            fluff_stripped = True
    trunc = _truncate_to_bytes(clean, name_budget)
    truncated = len(trunc.encode("utf-8")) < len(clean.encode("utf-8"))
    if item is not None:
        try:
            item._name_was_truncated = truncated
            item._name_fluff_stripped = fluff_stripped and not truncated
        except Exception:
            pass
    name = trunc.strip() or "output"
    return sanitize_filename(name + suffix) + ext


def find_artwork(path: Path):
    if path.is_file():
        return None
    for name in ["icon0.png", "pic0.png", "pic1.png"]:
        try:
            hits = list(path.rglob(name))
            if hits:
                return hits[0]
        except Exception:
            pass
    return None


def load_history():
    ensure_app_dir()
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_history(items):
    ensure_app_dir()
    HISTORY_FILE.write_text(json.dumps(items[-100:], indent=2), encoding="utf-8")


def load_settings() -> dict:
    ensure_app_dir()
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_settings(data: dict) -> None:
    ensure_app_dir()
    existing = load_settings()
    existing.update(data)
    SETTINGS_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def is_first_run() -> bool:
    return not SETTINGS_FILE.exists()


# ── Compatibility list helpers ─────────────────────────────────────────────────

def load_compat() -> list:
    ensure_app_dir()
    try:
        if COMPAT_FILE.exists():
            return json.loads(COMPAT_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def save_compat(reports: list) -> None:
    ensure_app_dir()
    COMPAT_FILE.write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")


def add_compat_report(report: dict) -> None:
    reports = load_compat()
    tid = report.get("title_id", "").strip().upper()
    if tid:
        # Replace any existing local entry for the same title — don't accumulate duplicates
        reports = [r for r in reports if r.get("title_id", "").strip().upper() != tid]
    reports.insert(0, report)          # newest first
    save_compat(reports)


def get_last_log_lines(n: int = 50) -> str:
    try:
        if RAW_LOG_FILE.exists():
            lines = RAW_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n:])
    except Exception:
        pass
    return ""


# ─── First Run Wizard ──────────────────────────────────────────────────────────

class FirstRunWizard(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("PS5 FFPFSC PRO — First Run Setup")
        self.geometry("640x520")
        self.resizable(False, False)
        self.grab_set()
        self.configure(fg_color=BLACK)

        self.step = 0
        self.temp_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.result = {}

        self._build()
        self._show_step(0)

    def _build(self):
        self.header = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=22, weight="bold"), text_color=WHITE)
        self.header.pack(pady=(24, 6), padx=30, anchor="w")

        self.sub = ctk.CTkLabel(self, text="", text_color=MUTED, wraplength=580, justify="left")
        self.sub.pack(padx=30, anchor="w")

        self.body = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=10)
        self.body.pack(fill="both", expand=True, padx=30, pady=18)

        nav = ctk.CTkFrame(self, fg_color=BLACK)
        nav.pack(fill="x", padx=30, pady=(0, 20))
        nav.grid_columnconfigure(1, weight=1)
        self.back_btn = ctk.CTkButton(nav, text="← Back", width=100, fg_color=CARD2, text_color=WHITE,
                                       hover_color=("#b0b0b0", "#2a2a2a"), command=self._back)
        self.back_btn.grid(row=0, column=0, padx=(0, 8))
        self.next_btn = ctk.CTkButton(nav, text="Next →", width=100, fg_color=GREEN,
                                       text_color="#061006", hover_color=GREEN2, command=self._next)
        self.next_btn.grid(row=0, column=2)

        self.step_var = tk.StringVar(value="Step 1 of 4")
        ctk.CTkLabel(nav, textvariable=self.step_var, text_color=MUTED).grid(row=0, column=1)

    def _clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()

    def _show_step(self, n):
        self.step = n
        self.step_var.set(f"Step {n + 1} of 4")
        self.back_btn.configure(state="normal" if n > 0 else "disabled")
        self.next_btn.configure(text="Finish" if n == 3 else "Next →")
        self._clear_body()

        if n == 0:
            self.header.configure(text="Step 1 — Select Temp Folder")
            self.sub.configure(text="Choose a temp folder on a fast SSD or NVMe drive. Avoid mechanical HDDs for large games.")
            ctk.CTkLabel(self.body, text="Temp Folder:", text_color=WHITE).pack(anchor="w", padx=14, pady=(14, 4))
            row = ctk.CTkFrame(self.body, fg_color=PANEL)
            row.pack(fill="x", padx=14)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkEntry(row, textvariable=self.temp_path, fg_color=CARD, border_color=BORDER2, text_color=WHITE).grid(row=0, column=0, sticky="ew", padx=(0, 8))
            ctk.CTkButton(row, text="Browse", width=80, fg_color=CARD2, text_color=WHITE, hover_color=("#b0b0b0", "#2a2a2a"),
                           command=self._browse_temp).grid(row=0, column=1)

        elif n == 1:
            self.header.configure(text="Step 2 — Select Output Folder")
            self.sub.configure(text="Choose where compressed .ffpfsc files will be saved. This can be an external drive or the same drive.")
            ctk.CTkLabel(self.body, text="Output Folder:", text_color=WHITE).pack(anchor="w", padx=14, pady=(14, 4))
            row = ctk.CTkFrame(self.body, fg_color=PANEL)
            row.pack(fill="x", padx=14)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkEntry(row, textvariable=self.output_path, fg_color=CARD, border_color=BORDER2, text_color=WHITE).grid(row=0, column=0, sticky="ew", padx=(0, 8))
            ctk.CTkButton(row, text="Browse", width=80, fg_color=CARD2, text_color=WHITE, hover_color=("#b0b0b0", "#2a2a2a"),
                           command=self._browse_output).grid(row=0, column=1)

        elif n == 2:
            self.header.configure(text="Step 3 — Storage Check")
            self.sub.configure(text="Checking your selected drives for speed and available space.")
            lines = []
            tp = self.temp_path.get().strip()
            op = self.output_path.get().strip()
            if tp:
                tpath = Path(tp)
                ttype = get_drive_type(tpath)
                tfree = get_free_space(tpath)
                lines.append(f"Temp Drive:    {format_size(tfree)} free  |  Type: {ttype}")
                if ttype == "HDD":
                    lines.append("  ⚠  Temp folder is on a mechanical HDD.\n     Large games may process significantly slower.\n     SSD/NVMe recommended.")
            if op:
                opath = Path(op)
                ofree = get_free_space(opath)
                lines.append(f"Output Drive:  {format_size(ofree)} free")
            if not lines:
                lines.append("No paths selected. Go back and select folders.")
            for line in lines:
                color = YELLOW if "⚠" in line else WHITE
                ctk.CTkLabel(self.body, text=line, text_color=color, anchor="w",
                              font=ctk.CTkFont(family="Consolas", size=12),
                              justify="left").pack(anchor="w", padx=14, pady=3)

        elif n == 3:
            self.header.configure(text="Step 4 — Ready!")
            self.sub.configure(text="Setup is complete. These settings will be saved and pre-filled next time you launch.")
            summary = []
            if self.temp_path.get():
                summary.append(f"Temp Folder:    {self.temp_path.get()}")
            if self.output_path.get():
                summary.append(f"Output Folder:  {self.output_path.get()}")
            summary.append("")
            summary.append("Click Finish to launch PS5 FFPFSC PRO.")
            for line in summary:
                ctk.CTkLabel(self.body, text=line, text_color=WHITE, anchor="w",
                              font=ctk.CTkFont(family="Consolas", size=12)).pack(anchor="w", padx=14, pady=2)

    def _browse_temp(self):
        p = filedialog.askdirectory(title="Select Temp Folder")
        if p:
            self.temp_path.set(str(Path(p) / "_ffpfsc_temp"))

    def _browse_output(self):
        p = filedialog.askdirectory(title="Select Output Folder")
        if p:
            self.output_path.set(p)

    def _back(self):
        if self.step > 0:
            self._show_step(self.step - 1)

    def _next(self):
        if self.step < 3:
            self._show_step(self.step + 1)
        else:
            self.result = {
                "temp_folder": self.temp_path.get(),
                "output_folder": self.output_path.get(),
                "first_run_done": True,
            }
            save_settings(self.result)
            self.destroy()


# ─── Detailed Error Dialog ─────────────────────────────────────────────────────

class ErrorDialog(ctk.CTkToplevel):
    def __init__(self, parent, msg: str, last_cmd: str = "", log_lines: str = ""):
        super().__init__(parent)
        self.title("Compression Failed")
        self.geometry("700x560")
        self.resizable(True, True)
        self.grab_set()
        self.configure(fg_color=BLACK)
        self._msg = msg
        self._cmd = last_cmd
        self._log = log_lines
        self._build()

    def _build(self):
        ctk.CTkLabel(self, text="Compression Failed", font=ctk.CTkFont(size=22, weight="bold"),
                      text_color=RED).pack(anchor="w", padx=20, pady=(20, 4))

        ctk.CTkLabel(self, text=self._msg, text_color=WHITE, wraplength=660, justify="left").pack(anchor="w", padx=20, pady=(0, 10))

        causes = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=8)
        causes.pack(fill="x", padx=20, pady=(0, 12))
        ctk.CTkLabel(causes, text="Possible Causes:", text_color=YELLOW,
                      font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=14, pady=(10, 4))
        for cause in [
            "• Insufficient free space on temp or output drive",
            "• External drive disconnected or write-protected",
            "• Temp folder unavailable or permissions issue",
            "• MkPFS backend failure (corrupted dump or unsupported format)",
            "• Python not found or wrong version",
            "• Antivirus blocking backend process",
        ]:
            ctk.CTkLabel(causes, text=cause, text_color=MUTED, anchor="w").pack(anchor="w", padx=24, pady=1)
        ctk.CTkFrame(causes, height=8, fg_color=PANEL).pack()

        ctk.CTkLabel(self, text="Last 50 Log Lines:", text_color=WHITE,
                      font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=20, pady=(0, 4))
        box = ctk.CTkTextbox(self, fg_color=BLACK, text_color=("#1a7a40", "#4ade80"), border_width=1, border_color=BORDER,
                              font=ctk.CTkFont(family="Consolas", size=11), height=160, wrap="none")
        box.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        box.insert("end", self._log or "(No log available)")
        box.configure(state="disabled")

        btns = ctk.CTkFrame(self, fg_color=BLACK)
        btns.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkButton(btns, text="Copy Error", width=140, fg_color=CARD2, text_color=WHITE,
                       hover_color=("#b0b0b0", "#2a2a2a"), command=self._copy).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btns, text="Export Raw Log", width=140, fg_color=CARD2, text_color=WHITE,
                       hover_color=("#b0b0b0", "#2a2a2a"), command=self._export_log).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btns, text="Open Log Folder", width=140, fg_color=CARD2, text_color=WHITE,
                       hover_color=("#b0b0b0", "#2a2a2a"), command=self._open_folder).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btns, text="Close", width=100, fg_color=RED, text_color=WHITE,
                       hover_color=("#b91c1c", "#5a1a1a"), command=self.destroy).pack(side="right")

    def _copy(self):
        text = f"Error: {self._msg}\n\nLast Command: {self._cmd}\n\nLog:\n{self._log}"
        self.clipboard_clear()
        self.clipboard_append(text)

    def _export_log(self):
        ensure_app_dir()
        if RAW_LOG_FILE.exists():
            open_path(RAW_LOG_FILE)

    def _open_folder(self):
        ensure_app_dir()
        open_path(APP_DIR)


# ─── Summary Dialog ────────────────────────────────────────────────────────────

class SummaryDialog(ctk.CTkToplevel):
    """Compression result summary with copy-to-clipboard button."""

    def __init__(self, parent, report: str):
        super().__init__(parent)
        self.title("Compression Complete")
        self.configure(fg_color=BLACK)
        self.resizable(True, True)
        self.geometry("580x460")
        self.lift()
        self.focus_force()
        self.grab_set()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(self, text="✅  Compression Complete",
                      text_color=GREEN, font=ctk.CTkFont(size=18, weight="bold"),
                      anchor="w").grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 6))

        box = ctk.CTkTextbox(self, fg_color=CARD, border_width=1, border_color=BORDER2,
                              text_color=WHITE, font=ctk.CTkFont(family="Consolas", size=12),
                              wrap="word")
        box.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 8))
        box.insert("1.0", report)
        box.configure(state="disabled")

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 14))
        btn_row.grid_columnconfigure(0, weight=1)
        btn_row.grid_columnconfigure(1, weight=1)

        def _copy():
            self.clipboard_clear()
            self.clipboard_append(report)
            copy_btn.configure(text="✓ Copied!")
            self.after(2000, lambda: copy_btn.winfo_exists() and copy_btn.configure(text="📋  Copy Result"))

        copy_btn = ctk.CTkButton(btn_row, text="📋  Copy Result", command=_copy,
                                  fg_color=CARD2, hover_color=("#b0b0b0", "#2a2a2a"),
                                  text_color=WHITE, border_width=1, border_color=BORDER2)
        copy_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(btn_row, text="Close", command=self.destroy,
                       fg_color=GREEN, hover_color=GREEN2,
                       text_color="#061006").grid(row=0, column=1, sticky="ew")


# ─── Space Diagnostics Dialog ──────────────────────────────────────────────────

class SpaceDiagnosticsDialog(ctk.CTkToplevel):
    """Pre-flight space check shown before compression starts.
    Opens instantly — drive-type detection runs in a background thread."""

    def __init__(self, parent, item, temp_dir: Path, out_dir: Path):
        super().__init__(parent)
        self.title("Drive Space Diagnostics")
        self.geometry("520x520")
        self.resizable(False, False)
        self.configure(fg_color=BLACK)
        self.proceed = False
        self._auto_timer = None
        self._countdown  = 0
        self._proceed_btn = None   # set in _build
        self._build(item, temp_dir, out_dir)
        # Keep dialog above the main window on all platforms
        self.transient(parent)
        self.lift()
        self.focus_force()
        self.after(50, self.grab_set)
        # Auto-proceed after 4 s only when BOTH temp and output drives have room
        if _space_preflight_ok(item, temp_dir, out_dir):
            self._countdown = 4
            self.after(1000, self._tick_countdown)

    def _build(self, item, temp_dir: Path, out_dir: Path):
        ctk.CTkLabel(self, text="Drive Space Diagnostics",
                      font=ctk.CTkFont(size=20, weight="bold"),
                      text_color=WHITE).pack(anchor="w", padx=20, pady=(18, 2))
        ctk.CTkLabel(self, text="Pre-flight check before compression starts.",
                      text_color=MUTED).pack(anchor="w", padx=20, pady=(0, 10))

        panel = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=10)
        panel.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        # ── Fast values (no blocking) ──────────────────────────────────────────
        temp_free   = get_free_space(temp_dir)
        out_free    = get_free_space(out_dir)
        same        = same_drive(temp_dir, out_dir)
        bsize       = _build_size_of(item)
        peak_needed = estimate_peak_space_needed(bsize, _peak_factor_for(item), same)
        out_needed  = estimate_output_space_needed(
            bsize, bool(getattr(item, "_output_compressed", True)),
            int(getattr(item, "size", 0) or 0) if getattr(item, "source_kind", "") == "archive" else 0)
        final_est   = int(bsize * 0.55)
        temp_fs     = get_filesystem_type(temp_dir)   # fast ctypes call
        out_fs      = get_filesystem_type(out_dir)

        def _fs_status(fs):
            if fs in ("exFAT", "FAT32", "FAT"): return "warn"
            if fs == "NTFS": return "ok"
            return None

        def _color(status):
            if status == "ok":   return ("#1a7a40", "#4ade80")
            if status == "warn": return YELLOW
            return WHITE

        temp_ok  = temp_free >= peak_needed
        out_ok   = same or out_free >= out_needed
        space_ok = temp_ok and out_ok

        static_rows = [
            ("Game Size",          format_size(display_size(item)),         None),
            ("Temp Drive Free",    format_size(temp_free),
             "ok" if temp_ok else "warn"),
            ("Temp Needs (image)", format_size(peak_needed),                None),
            ("Output Drive Free",  format_size(out_free),
             None if same else ("ok" if out_ok else "warn")),
            ("Output Needs (≈full)",
             "— (= temp drive)" if same else format_size(out_needed),
             None if same else ("ok" if out_ok else "warn")),
            ("Est. Final Output",  f"~{format_size(final_est)} – {format_size(item.size)}", None),
            ("Temp Filesystem",    temp_fs,    _fs_status(temp_fs)),
            ("Output Filesystem",  out_fs,     _fs_status(out_fs)),
        ]

        for lbl, val, st in static_rows:
            row = ctk.CTkFrame(panel, fg_color=PANEL)
            row.pack(fill="x", padx=14, pady=2)
            row.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(row, text=lbl + ":", text_color=MUTED,
                          anchor="w", width=200).grid(row=0, column=0, sticky="w")
            ctk.CTkLabel(row, text=val, text_color=_color(st),
                          anchor="e").grid(row=0, column=1, sticky="e")

        # ── Drive type row — populated by background thread ───────────────────
        dt_row = ctk.CTkFrame(panel, fg_color=PANEL)
        dt_row.pack(fill="x", padx=14, pady=2)
        dt_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(dt_row, text="Temp Drive Type:", text_color=MUTED,
                      anchor="w", width=200).grid(row=0, column=0, sticky="w")
        self._dt_label = ctk.CTkLabel(dt_row, text="Detecting…", text_color=MUTED,
                                       anchor="e")
        self._dt_label.grid(row=0, column=1, sticky="e")

        # ── Space result banner ────────────────────────────────────────────────
        if space_ok:
            result_text = "✓  Enough space to proceed."
        elif not temp_ok:
            result_text = "⚠  Temp drive low — packing may fail mid-run."
        else:
            result_text = "⚠  Output drive low — the final .ffpfsc may not fit (≈full size at 0% gain)."
        result_color = ("#1a7a40", "#4ade80") if space_ok else YELLOW
        ctk.CTkLabel(panel, text=result_text, text_color=result_color,
                      font=ctk.CTkFont(size=13, weight="bold")
                     ).pack(anchor="w", padx=14, pady=(8, 2))

        # Filesystem warnings (fast — already have temp_fs / out_fs)
        if temp_fs in ("exFAT", "FAT32", "FAT"):
            ctk.CTkLabel(panel,
                          text=f"⚠  Temp drive is {temp_fs} — no hardlink support. Slower copy mode will be used.",
                          text_color=YELLOW, justify="left", wraplength=460
                         ).pack(anchor="w", padx=14, pady=(0, 2))
        if out_fs in ("exFAT", "FAT32", "FAT"):
            ctk.CTkLabel(panel,
                          text=f"⚠  Output drive is {out_fs}. NTFS recommended.",
                          text_color=YELLOW, justify="left", wraplength=460
                         ).pack(anchor="w", padx=14, pady=(0, 2))

        # HDD warning label — shown/hidden by background thread result
        self._hdd_warn = ctk.CTkLabel(panel,
                                       text="⚠  Temp folder is on a mechanical HDD — will be significantly slower.",
                                       text_color=YELLOW, justify="left", wraplength=460)
        # packed conditionally in background callback

        # ── Buttons ───────────────────────────────────────────────────────────
        btns = ctk.CTkFrame(self, fg_color=BLACK)
        btns.pack(fill="x", padx=20, pady=(0, 16))

        self._proceed_btn = ctk.CTkButton(btns, text="▶  START NOW",
                       fg_color=GREEN, hover_color=GREEN2,
                       text_color="#061006",
                       font=ctk.CTkFont(size=14, weight="bold"),
                       height=38,
                       command=self._ok
                      )
        self._proceed_btn.pack(side="right", padx=(8, 0))
        ctk.CTkButton(btns, text="Cancel",
                       fg_color=CARD2, text_color=WHITE,
                       hover_color=("#b0b0b0", "#2a2a2a"),
                       command=self._cancel
                      ).pack(side="right")

        # ── Background thread: drive type detection ───────────────────────────
        def _detect():
            dt = get_drive_type(temp_dir)   # may block up to 6 s
            try:
                self.after(0, lambda: self._apply_drive_type(dt))
            except Exception:
                pass

        threading.Thread(target=_detect, daemon=True).start()

    def _apply_drive_type(self, dt: str):
        """Called on the main thread when background detection finishes."""
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        if dt == "SSD":
            self._dt_label.configure(text="SSD / NVMe", text_color=("#1a7a40", "#4ade80"))
        elif dt == "HDD":
            self._dt_label.configure(text="HDD  ⚠", text_color=YELLOW)
            self._hdd_warn.pack(anchor="w", padx=14, pady=(0, 2))
        else:
            self._dt_label.configure(text="Unknown", text_color=MUTED)

    def _tick_countdown(self):
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        if self._countdown > 0:
            if self._proceed_btn:
                self._proceed_btn.configure(
                    text=f"▶  START NOW  (auto in {self._countdown}s)")
            self._countdown -= 1
            self._auto_timer = self.after(1000, self._tick_countdown)
        else:
            self._ok()

    def _ok(self):
        if self._auto_timer:
            try:
                self.after_cancel(self._auto_timer)
            except Exception:
                pass
        self.proceed = True
        self.destroy()

    def _cancel(self):
        if self._auto_timer:
            try:
                self.after_cancel(self._auto_timer)
            except Exception:
                pass
        self.proceed = False
        self.destroy()


# ─── Export Diagnostic Package ─────────────────────────────────────────────────

def export_diagnostic_zip(last_cmd: str = "", extra_info: str = "") -> Path | None:
    ensure_app_dir()
    zip_path = APP_DIR / f"diagnostic_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if RAW_LOG_FILE.exists():
                zf.write(RAW_LOG_FILE, "raw.log")
            if SETTINGS_FILE.exists():
                zf.write(SETTINGS_FILE, "settings.json")
            if FINAL_REPORT_FILE.exists():
                zf.write(FINAL_REPORT_FILE, "last_result_report.txt")
            def _drive_info(p: str) -> str:
                if not p:
                    return "—"
                try:
                    pp = Path(p)
                    return (f"{get_filesystem_type(pp)} | "
                            f"{get_drive_type(pp)} | "
                            f"Free: {format_size(get_free_space(pp))}")
                except Exception:
                    return "—"

            temp_p  = ""
            out_p   = ""
            try:
                s = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
                temp_p = s.get("temp_folder", "")
                out_p  = s.get("output_folder", "")
            except Exception:
                pass

            session_info = "\n".join([
                f"PS5 FFPFSC PRO {APP_VERSION}",
                f"Generated:      {now_datetime()}",
                f"Python:         {sys.version}",
                f"OS:             {sys.platform} {os.name}",
                "",
                f"Last Command:   {last_cmd}",
                "",
                f"Temp Folder:    {temp_p or '—'}",
                f"Temp Drive:     {_drive_info(temp_p)}",
                f"Output Folder:  {out_p or '—'}",
                f"Output Drive:   {_drive_info(out_p)}",
                "",
                extra_info,
            ])
            zf.writestr("session_info.txt", session_info)
        return zip_path
    except Exception:
        return None


# ─── Settings Window ──────────────────────────────────────────────────────────

class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, parent_widget, app):
        super().__init__(parent_widget)
        self.app = app
        self.title(f"{APP_NAME} — Settings")
        self.geometry("600x680")
        self.resizable(False, True)
        self.grab_set()
        self.configure(fg_color=BLACK)
        self._build()

    def _build(self):
        ctk.CTkLabel(self, text="Settings",
                      font=ctk.CTkFont(size=24, weight="bold"), text_color=WHITE).pack(anchor="w", padx=20, pady=(20, 2))
        ctk.CTkLabel(self, text="Changes apply immediately.", text_color=MUTED).pack(anchor="w", padx=20, pady=(0, 12))

        scroll = ctk.CTkScrollableFrame(self, fg_color=BLACK)
        scroll.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        # FOLDERS
        self._section_label(scroll, "FOLDERS")
        fold = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        fold.pack(fill="x", pady=(4, 12))
        fold.grid_columnconfigure(1, weight=1)
        for row_i, (lbl, var, key, title) in enumerate([
            ("Default Output Folder", self.app.output_var, "output_folder", "Select Output Folder"),
            ("Default Temp Folder",   self.app.temp_var,   "temp_folder",   "Select Temp Folder"),
        ]):
            ctk.CTkLabel(fold, text=lbl + ":", text_color=MUTED, anchor="w", width=170).grid(
                row=row_i, column=0, padx=14, pady=8, sticky="w")
            ctk.CTkEntry(fold, textvariable=var, fg_color=CARD, border_color=BORDER2,
                          text_color=WHITE).grid(row=row_i, column=1, sticky="ew", padx=(0, 8), pady=8)
            ctk.CTkButton(fold, text="Browse", width=80, fg_color=CARD2, text_color=WHITE,
                           hover_color=("#b0b0b0", "#2a2a2a"),
                           command=lambda v=var, k=key, t=title: self._browse_folder(v, k, t)).grid(
                row=row_i, column=2, padx=(0, 14), pady=8)

        # COMPRESSION
        self._section_label(scroll, "COMPRESSION")
        comp = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        comp.pack(fill="x", pady=(4, 12))
        for text, var, key in [
            ("Keep intermediate PFS image",           self.app.keep_pfs_var,        None),
            ("Verify output (slower, uses more RAM)", self.app.verify_output_var,    None),
            ("Auto-clear temp folder after success",  self.app.auto_clear_temp_var,  "auto_clear_temp"),
            ("Verbose mkpfs output (debug)",          self.app.verbose_var,           None),
        ]:
            cb = ctk.CTkCheckBox(comp, text=text, variable=var, fg_color=GREEN,
                                  hover_color=GREEN2, text_color=WHITE)
            if key:
                cb.configure(command=lambda k=key, v=var: save_settings({k: v.get()}))
            cb.pack(anchor="w", padx=14, pady=6)

        # MkPFS tuning sliders
        _st = ctk.CTkFrame(comp, fg_color="transparent")
        _st.pack(fill="x", padx=14, pady=(4, 8))
        _st.columnconfigure(1, weight=1)

        ctk.CTkLabel(_st, text="Compression level (0-9):", text_color=WHITE,
                      font=ctk.CTkFont(size=11)).grid(row=0, column=0, sticky="w", pady=4)
        ctk.CTkSlider(_st, from_=0, to=9, number_of_steps=9,
                       variable=self.app.compression_level_var,
                       fg_color=BORDER2, progress_color=GREEN, button_color=GREEN,
                       button_hover_color=GREEN2).grid(row=0, column=1, sticky="ew", padx=8, pady=4)
        _cl_lbl = ctk.CTkLabel(_st, text=str(self.app.compression_level_var.get()),
                                 text_color=GREEN, font=ctk.CTkFont(size=11, weight="bold"), width=24)
        _cl_lbl.grid(row=0, column=2)
        def _cl_cb(*_):
            # The app-level var already persists on write (trace added at creation);
            # this callback only refreshes the window-local label. Guard against firing
            # after this Settings window was closed (the trace can outlive the label).
            if _cl_lbl.winfo_exists():
                _cl_lbl.configure(text=str(self.app.compression_level_var.get()))
        self.app.compression_level_var.trace_add("write", _cl_cb)

        ctk.CTkLabel(_st, text="CPU cores (0=auto):", text_color=WHITE,
                      font=ctk.CTkFont(size=11)).grid(row=1, column=0, sticky="w", pady=4)
        ctk.CTkSlider(_st, from_=0, to=16, number_of_steps=16,
                       variable=self.app.cpu_count_var,
                       fg_color=BORDER2, progress_color=GREEN, button_color=GREEN,
                       button_hover_color=GREEN2).grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        _cpu_lbl = ctk.CTkLabel(_st, text="auto" if self.app.cpu_count_var.get() == 0 else str(self.app.cpu_count_var.get()),
                                  text_color=GREEN, font=ctk.CTkFont(size=11, weight="bold"), width=24)
        _cpu_lbl.grid(row=1, column=2)
        def _cpu_cb(*_):
            if _cpu_lbl.winfo_exists():
                v = self.app.cpu_count_var.get()
                _cpu_lbl.configure(text="auto" if v == 0 else str(v))
        self.app.cpu_count_var.trace_add("write", _cpu_cb)

        ctk.CTkLabel(_st, text="Block size:", text_color=WHITE,
                      font=ctk.CTkFont(size=11)).grid(row=2, column=0, sticky="w", pady=4)
        _bs_opts = ["auto", "65536"]
        _bs_menu = ctk.CTkOptionMenu(
            _st, values=_bs_opts, variable=self.app.block_size_var,
            fg_color=CARD2, button_color=GREEN, button_hover_color=GREEN2,
            text_color=WHITE, dropdown_fg_color=CARD2, dropdown_text_color=WHITE,
            dropdown_hover_color=GREEN, width=110, height=28,
            font=ctk.CTkFont(size=11),
            command=lambda v: save_settings({"block_size": v}),
        )
        _bs_menu.grid(row=2, column=1, sticky="w", padx=8, pady=4)
        ctk.CTkLabel(_st, text="PS5 needs 64 KiB. auto = 65536 (recommended). Smaller blocks crash the console.",
                      text_color=MUTED, font=ctk.CTkFont(size=10)).grid(
            row=2, column=2, sticky="w", pady=4)

        ctk.CTkLabel(comp, text="Default Archive Password (optional):", text_color=MUTED, anchor="w").pack(
            anchor="w", padx=14, pady=(8, 2))
        ctk.CTkLabel(comp, text="Tried FIRST. A one-off password for the next extraction.",
                      text_color=MUTED, font=ctk.CTkFont(size=11), anchor="w").pack(
            anchor="w", padx=14)
        ctk.CTkEntry(comp, textvariable=self.app.password_var, show="*",
                      fg_color=CARD, border_color=BORDER2, text_color=WHITE).pack(
            fill="x", padx=14, pady=(4, 10))

        # ── Global auto-tried password list ──────────────────────────────────
        ctk.CTkLabel(comp, text="Saved Archive Passwords (auto-tried, one per line):",
                      text_color=MUTED, anchor="w").pack(anchor="w", padx=14, pady=(8, 2))
        ctk.CTkLabel(comp, text="Every password here is tried automatically, in order — handy for a "
                                "queue of differently-protected archives. DLPSGAME.COM is pre-added.",
                      text_color=MUTED, font=ctk.CTkFont(size=11), anchor="w", justify="left").pack(
            anchor="w", padx=14)
        self._pw_list_box = ctk.CTkTextbox(comp, height=110, fg_color=CARD,
                                            border_width=1, border_color=BORDER2, text_color=WHITE,
                                            font=ctk.CTkFont(size=12))
        self._pw_list_box.pack(fill="x", padx=14, pady=(4, 4))
        self._pw_list_box.insert("1.0", "\n".join(self.app.archive_passwords))

        def _save_pw_list(*_):
            raw = self._pw_list_box.get("1.0", "end")
            seen, pwds = set(), []
            for ln in raw.splitlines():
                ln = ln.strip()
                if ln and ln not in seen:
                    seen.add(ln)
                    pwds.append(ln)
            self.app.archive_passwords = pwds
            save_settings({"archive_passwords": pwds})

        self._pw_list_box.bind("<FocusOut>", _save_pw_list, add="+")
        ctk.CTkButton(comp, text="Save Passwords", fg_color=GREEN, text_color="#061006",
                       hover_color=GREEN2, width=150, command=_save_pw_list).pack(
            anchor="w", padx=14, pady=(0, 10))

        # ── Folder bundles ───────────────────────────────────────────────────
        _cs_cb = ctk.CTkCheckBox(
            comp,
            text="Copy extra files (DLCs etc.) next to the .ffpfsc when packing a folder",
            variable=self.app.copy_siblings_var, fg_color=GREEN, hover_color=GREEN2, text_color=WHITE,
            command=lambda: save_settings({"copy_bundle_siblings": self.app.copy_siblings_var.get()}),
        )
        _cs_cb.pack(anchor="w", padx=14, pady=(2, 4))
        ctk.CTkLabel(comp, text="When a folder holds one game plus extras, the source folder is recreated "
                                "at the destination with the .ffpfsc and the extras inside.",
                      text_color=MUTED, font=ctk.CTkFont(size=11), anchor="w", justify="left").pack(
            anchor="w", padx=14, pady=(0, 10))

        # USER INTERFACE
        self._section_label(scroll, "USER INTERFACE")
        ui = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        ui.pack(fill="x", pady=(4, 12))
        for text, var in [
            ("Show summary popup when done", self.app.summary_popup_var),
            ("Ask to share compatibility data after packing", self.app.compat_prompt_var),
            ("Play sound on completion",     self.app.sound_complete_var),
            ("Play sound on errors",         self.app.sound_error_var),
            ("Open output folder when done", self.app.open_output_var),
            ("Auto-integrate patch from release folder", self.app.auto_integrate_patch_var),
        ]:
            ctk.CTkCheckBox(ui, text=text, variable=var, fg_color=GREEN,
                             hover_color=GREEN2, text_color=WHITE).pack(anchor="w", padx=14, pady=6)
        theme_row = ctk.CTkFrame(ui, fg_color=PANEL)
        theme_row.pack(fill="x", padx=14, pady=(4, 10))
        ctk.CTkLabel(theme_row, text="Theme:", text_color=WHITE).pack(side="left", padx=(0, 10))
        ctk.CTkButton(theme_row, text="Toggle Dark / Light", fg_color=CARD2, text_color=WHITE,
                       hover_color=("#b0b0b0", "#2a2a2a"), width=160, command=self.app._toggle_theme).pack(side="left")

        # DRIVE & SPACE
        self._section_label(scroll, "DRIVE & SPACE")
        ds = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        ds.pack(fill="x", pady=(4, 12))
        s = load_settings()

        def _ds_row(label):
            row = ctk.CTkFrame(ds, fg_color=PANEL)
            row.pack(fill="x", padx=14, pady=6)
            ctk.CTkLabel(row, text=label, text_color=WHITE, width=180, anchor="w").pack(side="left")
            return row

        # Drive usage — where archives get extracted (shared with the main panel).
        dm_labels = {"auto": "Auto (smart)", "temp": "Temp drive only", "spread": "Spread across drives"}
        dm_rev = {v: k for k, v in dm_labels.items()}
        dm_menu = ctk.CTkOptionMenu(
            _ds_row("Drive usage:"), values=list(dm_labels.values()), width=200,
            command=lambda disp: self.app.drive_mode_var.set(dm_rev.get(disp, "auto")))
        dm_menu.set(dm_labels.get(self.app.drive_mode_var.get(), "Auto (smart)"))
        dm_menu.pack(side="left")

        # Temp space safety factor — scales only the temp headroom requirement.
        sf_labels = {0.7: "70% (risky)", 0.85: "85%", 1.0: "100% (recommended)",
                     1.2: "120%", 1.5: "150% (cautious)"}
        sf_rev = {v: k for k, v in sf_labels.items()}
        sf_menu = ctk.CTkOptionMenu(
            _ds_row("Temp space safety:"), values=list(sf_labels.values()), width=200,
            command=lambda disp: save_settings({"space_safety_factor": sf_rev.get(disp, 1.0)}))
        try:
            _cur_sf = min(sf_labels, key=lambda k: abs(k - float(s.get("space_safety_factor", 1.0))))
        except Exception:
            _cur_sf = 1.0
        sf_menu.set(sf_labels[_cur_sf])
        sf_menu.pack(side="left")

        # What to do when temp/output is too small for the game.
        lp_labels = {"ask": "Ask me", "auto": "Proceed anyway", "skip": "Skip the game"}
        lp_rev = {v: k for k, v in lp_labels.items()}
        lp_menu = ctk.CTkOptionMenu(
            _ds_row("When space is low:"), values=list(lp_labels.values()), width=200,
            command=lambda disp: save_settings({"low_space_policy": lp_rev.get(disp, "ask")}))
        lp_menu.set(lp_labels.get(s.get("low_space_policy", "ask"), "Ask me"))
        lp_menu.pack(side="left")

        ctk.CTkCheckBox(ds, text="Show drive-space dialog before each pack",
                         variable=self.app.show_space_dialog_var, fg_color=GREEN,
                         hover_color=GREEN2, text_color=WHITE).pack(anchor="w", padx=14, pady=(6, 4))
        ctk.CTkCheckBox(ds, text="Build via exFAT intermediate (macOS) — PSBrew's most-stable path",
                         variable=self.app.build_via_exfat_var, fg_color=GREEN,
                         hover_color=GREEN2, text_color=WHITE).pack(anchor="w", padx=14, pady=(0, 4))
        ctk.CTkCheckBox(ds, text="Keep external drives spun-up during a run (bridges gaps between games)",
                         variable=self.app.keep_drives_awake_var, fg_color=GREEN,
                         hover_color=GREEN2, text_color=WHITE).pack(anchor="w", padx=14, pady=(0, 4))
        ka_labels = {5: "every 5 s", 8: "every 8 s (recommended)", 10: "every 10 s", 15: "every 15 s"}
        ka_rev = {v: k for k, v in ka_labels.items()}
        ka_menu = ctk.CTkOptionMenu(
            _ds_row("Keep-awake interval:"), values=list(ka_labels.values()), width=200,
            command=lambda disp: save_settings({"keep_awake_interval": ka_rev.get(disp, 8)}))
        try:
            _cur_ka = int(s.get("keep_awake_interval", 8))
        except (TypeError, ValueError):
            _cur_ka = 8
        ka_menu.set(ka_labels.get(_cur_ka, "every 8 s (recommended)"))
        ka_menu.pack(side="left")

        # ── AMPR / APR emu folder (PlayGo titles) ────────────────────────────
        ctk.CTkLabel(ds, text="AMPR / APR (PlayGo) — emu files folder:",
                      text_color=MUTED, anchor="w").pack(anchor="w", padx=14, pady=(8, 2))
        ctk.CTkLabel(ds, text="Folder holding libSceAmpr.sprx + libScePlayGo.sprx (you supply these). "
                              "PlayGo/APR games are auto-detected (sce_sys/playgo-chunk.dat); the two "
                              "files are injected into a fakelib/ folder and an ampr_emu.index is built "
                              "before packing, so the game boots from the compressed container.",
                      text_color=MUTED, font=ctk.CTkFont(size=11), anchor="w", justify="left",
                      wraplength=440).pack(anchor="w", padx=14)
        _ampr_row = ctk.CTkFrame(ds, fg_color="transparent")
        _ampr_row.pack(fill="x", padx=14, pady=(4, 4))
        _ampr_entry = ctk.CTkEntry(_ampr_row, textvariable=self.app.ampr_var,
                                   placeholder_text="Folder with the two .sprx files…")
        _ampr_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        def _save_ampr(*_):
            save_settings({"ampr_folder": self.app.ampr_var.get().strip()})
        def _browse_ampr():
            from tkinter import filedialog
            c = filedialog.askdirectory(title="Select AMPR Emu Folder")
            if c:
                self.app.ampr_var.set(c)
                _save_ampr()
        _ampr_entry.bind("<FocusOut>", lambda e: _save_ampr())
        ctk.CTkButton(_ampr_row, text="Browse", width=80, fg_color=GREEN, hover_color=GREEN2,
                      command=_browse_ampr).pack(side="left")

        # ── Extra temp drives (pool) ─────────────────────────────────────────
        ctk.CTkLabel(ds, text="Extra temp drives (pool, one path per line):",
                      text_color=MUTED, anchor="w").pack(anchor="w", padx=14, pady=(8, 2))
        ctk.CTkLabel(ds, text="Add more fast scratch drives here (e.g. external SSDs). For a big archive "
                              "game that won't fit one drive, the source is extracted to one and the inner "
                              "image built on another — so pass 1 stays SSD↔SSD instead of reading off the "
                              "HDD. The main Temp folder is the first pool drive; these are added to it.",
                      text_color=MUTED, font=ctk.CTkFont(size=11), anchor="w", justify="left",
                      wraplength=440).pack(anchor="w", padx=14)
        self._pool_box = ctk.CTkTextbox(ds, height=70, fg_color=CARD, border_width=1,
                                         border_color=BORDER2, text_color=WHITE,
                                         font=ctk.CTkFont(size=12))
        self._pool_box.pack(fill="x", padx=14, pady=(4, 4))
        self._pool_box.insert("1.0", "\n".join(self.app.temp_pool))

        def _save_pool(*_):
            raw = self._pool_box.get("1.0", "end")
            seen, dirs = set(), []
            for ln in raw.splitlines():
                ln = ln.strip()
                if ln and ln not in seen:
                    seen.add(ln)
                    dirs.append(ln)
            self.app.temp_pool = dirs
            save_settings({"temp_pool": dirs})
            self.app._warm_drive_types()   # probe SSD/HDD for the new drives

        self._pool_box.bind("<FocusOut>", _save_pool, add="+")
        _pool_btns = ctk.CTkFrame(ds, fg_color=PANEL)
        _pool_btns.pack(anchor="w", fill="x", padx=14, pady=(0, 6))
        def _add_pool_dir():
            p = filedialog.askdirectory(title="Select an extra temp/scratch drive")
            if p:
                cur = self._pool_box.get("1.0", "end").rstrip("\n")
                self._pool_box.delete("1.0", "end")
                self._pool_box.insert("1.0", (cur + "\n" + p).strip("\n"))
                _save_pool()
        ctk.CTkButton(_pool_btns, text="Add Drive…", fg_color=CARD2, text_color=WHITE,
                       hover_color=("#b0b0b0", "#2a2a2a"), width=120,
                       command=_add_pool_dir).pack(side="left")
        ctk.CTkButton(_pool_btns, text="Save Pool", fg_color=GREEN, text_color="#061006",
                       hover_color=GREEN2, width=120, command=_save_pool).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(ds, text="Safety factor scales only the temp headroom; the output drive must always "
                              "fit the finished .ffpfsc. 'Skip' applies per game during batch runs. "
                              "exFAT mode wraps a real exFAT volume instead of the folder PFS builder "
                              "(slower to build; try it if folder-built .ffpfsc crash the console). "
                              "Keep-awake only runs WHILE a job is packing: a fast tiny write keeps "
                              "bus-powered HDDs (WD Elements) spun-up across the gaps between games, so "
                              "each game skips a fresh spin-up. It pings only the drive the job ISN'T "
                              "currently using (the busy one can't sleep and a write there just costs a "
                              "seek). Keep it under ~8 s (the IntelliPark park timer) or it adds head-"
                              "parking instead of preventing it. When idle the drive sleeps normally.",
                      text_color=MUTED, justify="left", wraplength=440).pack(anchor="w", padx=14, pady=(0, 10))

        # ABOUT
        self._section_label(scroll, "ABOUT")
        about = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=8)
        about.pack(fill="x", pady=(4, 12))
        for line in [
            f"Version:  {APP_VERSION}",
            f"Backend:  {BACKEND_NAME}",
            f"MkPFS:    {MKPFS_NAME} v{MKPFS_VERSION}",
            f"Config:   {SETTINGS_FILE}",
            f"History:  {HISTORY_FILE}",
            f"Log:      {RAW_LOG_FILE}",
        ]:
            ctk.CTkLabel(about, text=line, text_color=MUTED, anchor="w",
                          font=ctk.CTkFont(family="Consolas", size=11)).pack(anchor="w", padx=14, pady=3)

        # Bottom buttons
        btns = ctk.CTkFrame(self, fg_color=BLACK)
        btns.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkButton(btns, text="Close", fg_color=GREEN, text_color="#061006",
                       hover_color=GREEN2, command=self.destroy).pack(side="right")
        ctk.CTkButton(btns, text="Open Config Folder", fg_color=CARD2, text_color=WHITE,
                       hover_color=("#b0b0b0", "#2a2a2a"),
                       command=lambda: open_path(APP_DIR)).pack(side="left")

    def _section_label(self, parent, text):
        ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=12, weight="bold"),
                      text_color=("#1a7a40", "#4ade80")).pack(anchor="w", pady=(10, 4))

    def _browse_folder(self, var, settings_key, title):
        p = filedialog.askdirectory(title=title)
        if p:
            var.set(p)
            save_settings({settings_key: p})


# ─── Archive Extractor ─────────────────────────────────────────────────────────

class ArchiveExtractionCancelled(RuntimeError):
    """Raised when the user cancels an archive extraction."""


class ArchiveExtractor:
    """Extract ZIP / RAR / 7z to a temp subfolder and return the game root Path.

    Libraries used (all optional — falls back to CLI tools if missing):
      • ZIP  — zipfile (stdlib, always available)
      • RAR  — rarfile  (pip install rarfile)
      • 7z   — py7zr    (pip install py7zr)  or  7z / 7za CLI on PATH
    """

    SUPPORTED = {".zip", ".rar", ".7z"}

    @staticmethod
    def extract(archive: Path, dest_root: Path, log_fn=None, progress_fn=None,
                password: str = "", cancel_event: threading.Event | None = None) -> Path:
        """Extract *archive* under *dest_root/<stem>* and return the extracted root.
        progress_fn(pct, filename) is called periodically.
        password is used for encrypted archives (ZIP/RAR/7z)."""
        # Unique per-archive subfolder. Two queued archives that share a stem
        # (Game.zip + Game.rar, or same-named releases from different folders)
        # must not extract into — and rmtree — each other's tree. The digest of
        # the absolute path keeps it stable, so re-extracting the same archive
        # reuses (and refreshes) its own folder.
        digest = hashlib.sha1(str(archive.resolve()).encode("utf-8", "replace")).hexdigest()[:8]
        dest = dest_root / f"{archive.stem}__{digest}"
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        suffix = archive.suffix.lower()
        if log_fn:
            log_fn("INFO", f"Extracting {archive.name} → {dest}"
                   + (" [password-protected]" if password else ""))
        ArchiveExtractor._check_cancel(cancel_event)
        try:
            if suffix == ".zip":
                ArchiveExtractor._zip(archive, dest, log_fn, progress_fn, password, cancel_event)
            elif suffix == ".rar":
                ArchiveExtractor._rar(archive, dest, log_fn, progress_fn, password, cancel_event)
            elif suffix == ".7z":
                ArchiveExtractor._sevenz(archive, dest, log_fn, progress_fn, password, cancel_event)
            else:
                raise ValueError(f"Unsupported archive format: {archive.suffix}")
        except BaseException:
            # Never leave a half-written tree behind (failed/cancelled/wrong-password
            # attempt) — the next password candidate / run starts from a clean dest.
            shutil.rmtree(dest, ignore_errors=True)
            raise
        ArchiveExtractor._check_cancel(cancel_event)
        if not has_any_files(dest):
            raise RuntimeError(
                f"Archive extraction produced no files: {archive.name}\n\n"
                "The archive may be empty, encrypted with a wrong password, or the required macOS extractor failed."
            )
        game_root = ArchiveExtractor._find_root(dest)
        if log_fn:
            log_fn("OK", f"Extracted to: {game_root}")
        return game_root

    @staticmethod
    def _is_password_error(e: Exception) -> bool:
        """True if *e* signals a wrong/missing archive password (any format).

        Type checks first; the string match is phrase-anchored so an unrelated
        error mentioning a file like ``passwords.txt`` or an offset like
        ``error 224`` does not get misread as a password failure."""
        # A bare PermissionError is a real filesystem permission problem, NOT a wrong
        # password — no archive lib here reports a bad password that way (ZIP raises
        # RuntimeError("Bad password"), py7zr/RAR raise their own types or error 22/24).
        # Letting it fall through to the message regex below means a password-mentioning
        # error still counts, but a genuine FS error surfaces correctly instead of being
        # mislabelled "wrong password" (which would also burn every saved password).
        if type(e).__name__ in ("RarWrongPassword", "PasswordRequired",
                                 "WrongPassword", "BadPassword", "PasswordError"):
            return True
        low = str(e).lower()
        return bool(re.search(
            r"(?:wrong|bad|missing|incorrect|no)[ -]?password"
            r"|password[ -]?(?:required|incorrect|protected|needed|wrong)"
            r"|enter the correct password"
            r"|\berror 2[24]\b",        # UnRAR 22 = missing pwd, 24 = bad pwd
            low,
        ))

    @staticmethod
    def extract_with_passwords(archive: Path, dest_root: Path, passwords: list[str],
                               log_fn=None, progress_fn=None,
                               cancel_event: threading.Event | None = None) -> Path:
        """Extract *archive*, trying each candidate password in order until one
        works. Wrong passwords fail fast (at the header / first member), so no
        full wasted extraction. A no-password attempt is always tried last so
        unencrypted archives still extract. Raises a single clear error if none
        of the passwords open the archive; non-password errors (corrupt archive,
        missing RAR volume, no extractor) propagate immediately."""
        named = []
        for p in passwords:
            p = (p or "").strip()
            if p and p not in named:
                named.append(p)
        candidates = named + [""]   # always end with a clean no-password attempt
        for idx, pwd in enumerate(candidates):
            ArchiveExtractor._check_cancel(cancel_event)
            try:
                return ArchiveExtractor.extract(
                    archive, dest_root, log_fn=log_fn, progress_fn=progress_fn,
                    password=pwd, cancel_event=cancel_event,
                )
            except ArchiveExtractionCancelled:
                raise
            except Exception as e:
                if ArchiveExtractor._is_password_error(e):
                    if log_fn and pwd and len(candidates) > 1:
                        log_fn("INFO", f"  password {idx + 1}/{len(named)} did not match — trying next…")
                    continue
                raise   # not a password problem — surface it
        if named:
            raise RuntimeError(
                f"Could not open {archive.name}: wrong or missing password — none of the "
                f"{len(named)} saved password(s) worked.\n\n"
                "Add the correct password in Settings → Saved Archive Passwords "
                "(or in the 'Archive Password' field) and try again."
            )
        raise RuntimeError(
            f"Could not open {archive.name}: it is password-protected and no password "
            "is saved.\n\nAdd the password in Settings → Saved Archive Passwords "
            "(or in the 'Archive Password' field) and try again."
        )

    @staticmethod
    def list_members(archive: Path, passwords=None) -> list[str]:
        """Return member names ('/'-separated) WITHOUT extracting — a cheap peek
        used to tell a game archive from a DLC/extra. Tries candidate passwords
        for header-encrypted archives. Returns [] if it can't be opened."""
        suffix = archive.suffix.lower()
        cands = [p for p in (passwords or []) if p] + [""]
        if suffix == ".zip":
            try:
                with zipfile.ZipFile(archive, "r") as zf:
                    return [n.replace("\\", "/") for n in zf.namelist()]
            except Exception:
                return []
        if suffix == ".rar":
            resolved = ArchiveExtractor._first_volume(archive)
            try:
                backend_dir = backend_base_dir()
                if str(backend_dir) not in sys.path:
                    sys.path.insert(0, str(backend_dir))
                from unrar import rarfile as _br  # type: ignore
            except Exception:
                return []
            for pwd in cands:
                try:
                    with _br.RarFile(str(resolved), pwd=pwd or None) as rf:
                        return [n.replace("\\", "/") for n in rf.namelist()]
                except Exception:
                    continue
            return []
        if suffix == ".7z":
            try:
                import py7zr  # type: ignore
            except ImportError:
                return []
            for pwd in cands:
                try:
                    kwargs = {"password": pwd} if pwd else {}
                    with py7zr.SevenZipFile(str(archive), mode="r", **kwargs) as sz:
                        return [n.replace("\\", "/") for n in sz.getnames()]
                except Exception:
                    continue
            return []
        return []

    @staticmethod
    def uncompressed_size(archive: Path, passwords=None) -> int:
        """Total UNCOMPRESSED size of the archive's members, read from headers WITHOUT
        extracting (milliseconds). Resolves a multi-part set to its FIRST volume first,
        so a directly-dropped later part still reports the whole game (not one 5 GB
        part). Returns 0 when the size cannot be read (encrypted/solid/odd) so callers
        fall back to an estimate. This is the honest input to the space pre-check —
        scene RARs are often compressed ~2:1, so the on-disk size badly undershoots."""
        suffix = archive.suffix.lower()
        cands = [p for p in (passwords or []) if p] + [""]
        try:
            if suffix == ".zip":
                with zipfile.ZipFile(archive, "r") as zf:
                    return sum(int(getattr(zi, "file_size", 0) or 0) for zi in zf.infolist())
            if suffix == ".rar":
                resolved = ArchiveExtractor._first_volume(archive)
                backend_dir = backend_base_dir()
                if str(backend_dir) not in sys.path:
                    sys.path.insert(0, str(backend_dir))
                from unrar import rarfile as _br  # type: ignore
                for pwd in cands:
                    try:
                        with _br.RarFile(str(resolved), pwd=pwd or None) as rf:
                            return sum(int(getattr(ri, "file_size", 0) or 0) for ri in rf.infolist())
                    except Exception:
                        continue
                return 0
            if suffix == ".7z":
                import py7zr  # type: ignore
                for pwd in cands:
                    try:
                        kwargs = {"password": pwd} if pwd else {}
                        with py7zr.SevenZipFile(str(archive), mode="r", **kwargs) as sz:
                            total = sum(int(getattr(f, "uncompressed", 0) or 0) for f in sz.list())
                            if total <= 0:
                                total = int(getattr(sz.archiveinfo(), "uncompressed", 0) or 0)
                            return total
                    except Exception:
                        continue
                return 0
        except Exception:
            return 0
        return 0

    @staticmethod
    def names_look_like_game(names) -> bool:
        """True if a member-name listing contains the PS5 game signature
        (an eboot.bin plus a sce_sys/param.json), at any depth."""
        low = [n.lower().rstrip("/") for n in names]
        has_eboot = any(n == "eboot.bin" or n.endswith("/eboot.bin") for n in low)
        has_param = any(n.endswith("sce_sys/param.json") for n in low)
        return has_eboot and has_param

    # ── format handlers ────────────────────────────────────────────────────────

    @staticmethod
    def _check_cancel(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ArchiveExtractionCancelled("Archive extraction cancelled by user.")

    @staticmethod
    def _zip(archive: Path, dest: Path, log_fn, progress_fn=None, password: str = "",
             cancel_event: threading.Event | None = None):
        pwd_bytes = password.encode() if password else None
        with zipfile.ZipFile(archive, "r") as zf:
            names = zf.namelist()
            total = len(names)
            for i, name in enumerate(names):
                ArchiveExtractor._check_cancel(cancel_event)
                zf.extract(name, dest, pwd=pwd_bytes)
                ArchiveExtractor._check_cancel(cancel_event)
                pct = int((i + 1) / total * 100) if total else 0
                if progress_fn:
                    progress_fn(pct, name)
                elif log_fn and total > 0 and i % max(1, total // 20) == 0:
                    log_fn("INFO", f"  {pct}%  {name}")

    @staticmethod
    def _find_rar_tool(log_fn=None) -> str | None:
        """Return the first usable RAR-extraction executable found, or None."""
        import shutil as _shutil

        _script_dir = Path(getattr(sys, "frozen", None) and sys.executable
                           or __file__).parent

        # Absolute-path candidates (check existence directly — no subprocess needed)
        absolute_candidates = [
            # Next to the app / in app-data (user can drop UnRAR.exe here)
            _script_dir / "unrar.exe",
            _script_dir / "tools" / "unrar.exe",
            APP_DIR / "unrar.exe",
            # 7-Zip standard install locations
            Path(r"C:\Program Files\7-Zip\7z.exe"),
            Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
            # WinRAR standard install locations
            Path(r"C:\Program Files\WinRAR\UnRAR.exe"),
            Path(r"C:\Program Files\WinRAR\Rar.exe"),
            Path(r"C:\Program Files (x86)\WinRAR\UnRAR.exe"),
            Path(r"C:\Program Files (x86)\WinRAR\Rar.exe"),
            # macOS / Linux: Homebrew (Apple Silicon + Intel), MacPorts, system.
            # GUI apps don't inherit the shell PATH, so check these directly.
            Path("/opt/homebrew/bin/unrar"), Path("/opt/homebrew/bin/7z"),
            Path("/opt/homebrew/bin/7za"),   Path("/opt/homebrew/bin/rar"),
            Path("/usr/local/bin/unrar"),    Path("/usr/local/bin/7z"),
            Path("/usr/local/bin/7za"),      Path("/usr/local/bin/rar"),
            Path("/opt/local/bin/unrar"),    Path("/opt/local/bin/7z"),
            Path("/usr/bin/unrar"),          Path("/usr/bin/7z"),
            Path("/usr/bin/7za"),
        ]
        for p in absolute_candidates:
            if p.exists():
                if log_fn:
                    log_fn("INFO", f"RAR tool found: {p}")
                return str(p)

        # Short names resolved via PATH
        for name in ("unrar", "rar", "7z", "7za"):
            if _shutil.which(name):
                if log_fn:
                    log_fn("INFO", f"RAR tool found on PATH: {name}")
                return name

        return None

    @staticmethod
    def _run_extract_process(cmd: list[str], tool_name: str, log_fn=None, progress_fn=None,
                             cancel_event: threading.Event | None = None) -> None:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        lines: queue.Queue[str] = queue.Queue()

        def _reader():
            try:
                for raw in proc.stdout or []:
                    lines.put(raw.rstrip())
            finally:
                try:
                    if proc.stdout:
                        proc.stdout.close()
                except Exception:
                    pass

        threading.Thread(target=_reader, daemon=True).start()
        last_log_t = time.time()

        def _handle_line(line: str) -> None:
            nonlocal last_log_t
            if not line:
                return
            if log_fn and (time.time() - last_log_t >= 5 or "error" in line.lower()):
                log_fn("INFO", f"  extract: {line}")
                last_log_t = time.time()
            if progress_fn:
                m = re.search(r"(\d+)%", line)
                if m:
                    progress_fn(int(m.group(1)), line)

        while True:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                raise ArchiveExtractionCancelled("Archive extraction cancelled by user.")
            try:
                _handle_line(lines.get(timeout=0.1))
            except queue.Empty:
                if proc.poll() is not None:
                    break

        while True:
            try:
                _handle_line(lines.get_nowait())
            except queue.Empty:
                break

        code = proc.wait()
        if code != 0:
            raise RuntimeError(f"{tool_name} exited with code {code} — extraction failed.")

    @staticmethod
    def _first_volume(archive: Path) -> Path:
        """Multi-volume RAR sets must be opened on the FIRST volume. If *archive*
        is a later part, return the first volume in the same folder (else *archive*).

        Handles both the new scheme (name.partN.rar → name.part1.rar, width kept)
        and the old scheme (name.rNN → name.rar)."""
        name = archive.name
        m = re.match(r"^(?P<base>.*\.part)(?P<num>\d+)(?P<ext>\.rar)$", name, re.I)
        if m:
            first = archive.with_name(f"{m.group('base')}{'1'.zfill(len(m.group('num')))}{m.group('ext')}")
            return first if first.exists() else archive
        m = re.match(r"^(?P<base>.+)\.r\d{2,}$", name, re.I)   # .r00 … .r100 … (old scheme)
        if m:
            first = archive.with_name(f"{m.group('base')}.rar")
            return first if first.exists() else archive
        return archive

    @staticmethod
    def _rar_error_hint(e: Exception) -> str:
        """Turn a raw UnRAR error into a short, user-actionable reason."""
        msg = str(e).strip()
        low = msg.lower()
        # Password problems FIRST — header-encrypted (-hp) archives fail while
        # *reading headers* with UnRAR error 22 (no password) or 24 (wrong
        # password). These also contain "read header failed", so they must be
        # matched before the multi-volume branch below.
        if ("password" in low or "error 22" in low or "error 24" in low
                or type(e).__name__ == "RarWrongPassword"):
            return ("this RAR is password-protected — enter the correct password in the "
                    "'Archive Password' field and try again "
                    "(error 22 = no password given, 24 = wrong password)")
        if ("error 12" in low or "read header failed" in low
                or "failed to open" in low or "missing" in low or "volume" in low):
            return ("a volume of this multi-part RAR is missing, incomplete, or it was "
                    "opened on the wrong part — make sure every .partN.rar (or .rNN) file "
                    "is present in the same folder")
        return msg or e.__class__.__name__

    @staticmethod
    def _rar(archive: Path, dest: Path, log_fn, progress_fn=None, password: str = "",
             cancel_event: threading.Event | None = None):
        # Multi-volume sets must be opened on the first volume. If the user added
        # a later part (….part3.rar / ….r02), switch to the first one.
        resolved = ArchiveExtractor._first_volume(archive)
        if resolved != archive:
            if log_fn:
                log_fn("INFO", f"Multi-part RAR detected — using first volume: {resolved.name}")
            archive = resolved

        bundled_err: str | None = None   # the real reason the native module failed
        bundled_imported = False          # did the native module import at all?
        multipart_hint = (
            "If this is a multi-part RAR, make sure ALL parts are present in the "
            "same folder (….part1.rar … .partN.rar, or .rar + .r00 + .r01 …) and "
            "that the download is complete and not corrupted."
        )

        # ── Try bundled native UnRAR bindings first (macOS/Windows/Linux) ─────
        try:
            ArchiveExtractor._check_cancel(cancel_event)
            backend_dir = backend_base_dir()
            if str(backend_dir) not in sys.path:
                sys.path.insert(0, str(backend_dir))
            from unrar import rarfile as bundled_rarfile  # type: ignore
            bundled_imported = True
            _cancel_poll = (lambda: bool(cancel_event is not None and cancel_event.is_set()))
            try:
                with bundled_rarfile.RarFile(str(archive), pwd=password or None) as rf:
                    rf.extractall(
                        str(dest),
                        progress=(lambda p: progress_fn(p, archive.name)) if progress_fn else None,
                        cancel=_cancel_poll,
                    )
            except getattr(bundled_rarfile, "RarExtractionCancelled", ()):
                raise ArchiveExtractionCancelled("Archive extraction cancelled by user.")
            ArchiveExtractor._check_cancel(cancel_event)
            if log_fn:
                log_fn("OK", "RAR extracted via bundled native UnRAR")
            return
        except ArchiveExtractionCancelled:
            raise
        except ImportError as e:
            bundled_err = f"native UnRAR module unavailable ({e})"
            if log_fn:
                log_fn("WARN", f"Bundled UnRAR unavailable ({e}) — trying fallback extractors…")
        except Exception as e:
            bundled_err = ArchiveExtractor._rar_error_hint(e)
            if log_fn:
                log_fn("WARN", f"Bundled UnRAR failed: {e}")

        # macOS: if the native module loaded but extraction failed, the archive
        # itself is the problem (incomplete / wrong part / corrupt). External CLI
        # tools can't fix that and are frequently unsigned Homebrew binaries that
        # macOS Gatekeeper blocks with a scary dialog — so report the real reason
        # instead of spawning them. (Windows/Linux keep the full fallback chain.)
        if sys.platform == "darwin" and bundled_imported:
            # Only add the multi-part hint when it isn't a password problem.
            extra = "" if "password" in (bundled_err or "").lower() else f"\n\n{multipart_hint}"
            raise RuntimeError(
                f"RAR extraction failed — {bundled_err}.{extra}\n\n"
                "ZIP and .7z archives extract without external RAR tools."
            )

        # ── Try rarfile Python library next ───────────────────────────────────
        try:
            ArchiveExtractor._check_cancel(cancel_event)
            import rarfile  # type: ignore
            with rarfile.RarFile(str(archive)) as rf:
                if password:
                    rf.setpassword(password.encode())
                rf.extractall(str(dest))
            ArchiveExtractor._check_cancel(cancel_event)
            if log_fn:
                log_fn("OK", "RAR extracted via rarfile library")
            return
        except ImportError:
            pass
        except Exception as e:
            if log_fn:
                log_fn("WARN", f"rarfile failed ({e}) — trying CLI tools…")

        # ── Find any suitable CLI tool ─────────────────────────────────────────
        tool = ArchiveExtractor._find_rar_tool(log_fn=log_fn)
        if tool:
            tool_name = Path(tool).name.lower()
            is_7z = "7z" in tool_name
            if is_7z:
                # -bsp1 streams progress to stdout; -y auto-confirms
                cmd = [tool, "x", str(archive), f"-o{dest}", "-y", "-bsp1", "-bso0"]
                if password:
                    cmd.append(f"-p{password}")
            else:
                cmd = [tool, "x", "-y"]
                if password:
                    cmd.append(f"-p{password}")
                cmd += [str(archive), str(dest) + os.sep]

            if log_fn:
                log_fn("INFO", f"Running: {Path(tool).name}  (this may take a while for large archives…)")

            try:
                ArchiveExtractor._run_extract_process(
                    cmd, Path(tool).name, log_fn=log_fn,
                    progress_fn=progress_fn, cancel_event=cancel_event
                )
                if log_fn:
                    log_fn("OK", f"RAR extracted via {Path(tool).name}")
                return
            except (ArchiveExtractionCancelled, RuntimeError):
                raise
            except Exception as e:
                raise RuntimeError(f"Extraction error ({Path(tool).name}): {e}")

        # ── Nothing worked — informative error (real reason, not a blanket msg) ──
        # On macOS this is only reached when the native module failed to *import*
        # (a broken build); an import-less Mac genuinely has no RAR extractor.
        reason = bundled_err or "no RAR extractor was available"
        if sys.platform == "darwin":
            raise RuntimeError(
                f"RAR extraction failed — {reason}.\n\n"
                f"{multipart_hint}\n\n"
                "No 7z/unrar CLI was found either. Install one with:\n"
                "  brew install sevenzip      (provides 7z)\n"
                "  brew install carlocab/personal/unrar   (or: brew install rar)\n\n"
                "ZIP and .7z archives extract without external RAR tools."
            )
        raise RuntimeError(
            f"RAR extraction failed — {reason}.\n\n"
            f"{multipart_hint}\n\n"
            "EASIEST FIX — install any ONE of these:\n"
            "  1. 7-Zip:  https://www.7-zip.org/\n"
            "  2. WinRAR: https://www.rarlab.com/download.htm\n\n"
            "ZIP and .7z archives extract without any extra tools."
        )

    @staticmethod
    def _sevenz(archive: Path, dest: Path, log_fn, progress_fn=None, password: str = "",
                cancel_event: threading.Event | None = None):
        try:
            ArchiveExtractor._check_cancel(cancel_event)
            import py7zr  # type: ignore
            kwargs = {}
            if password:
                kwargs["password"] = password
            with py7zr.SevenZipFile(str(archive), mode="r", **kwargs) as sz:
                sz.extractall(str(dest))
            ArchiveExtractor._check_cancel(cancel_event)
            return
        except ImportError:
            pass
        # Fallback: 7z / 7za CLI
        for exe in ("7z", "7za"):
            try:
                cmd = [exe, "x", str(archive), f"-o{dest}", "-y"]
                if password:
                    cmd.append(f"-p{password}")
                ArchiveExtractor._run_extract_process(
                    cmd, exe, log_fn=log_fn,
                    progress_fn=progress_fn, cancel_event=cancel_event
                )
                return
            except FileNotFoundError:
                pass
            except ArchiveExtractionCancelled:
                raise
            except RuntimeError:
                pass
        raise RuntimeError(
            "Cannot extract 7z — install py7zr:  pip install py7zr\n"
            "or put 7z.exe on your PATH."
        )

    # ── helper ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _find_root(dest: Path) -> Path:
        """Walk the extraction tree and return the PS5 game root.

        Priority order:
          1. Any folder that directly contains sce_sys/param.json  (definitive)
          2. Any folder whose name matches a PS5 title-ID pattern  (PPSA/CUSA + 5 digits)
          3. Single top-level folder unwrap (one level, legacy behaviour)
          4. The dest itself as fallback
        """
        # BFS — check up to 4 levels deep so deeply nested archives still work
        from collections import deque
        queue_dirs: deque[Path] = deque([dest])
        visited = 0
        while queue_dirs and visited < 200:
            current = queue_dirs.popleft()
            visited += 1
            # Definitive PS5 game root marker
            if (current / "sce_sys" / "param.json").exists():
                return current
            try:
                subdirs = [p for p in current.iterdir() if p.is_dir()]
            except PermissionError:
                continue
            queue_dirs.extend(subdirs)

        # Second pass — title-ID folder name (e.g. PPSA14002-app, CUSA12345)
        queue_dirs = deque([dest])
        visited = 0
        while queue_dirs and visited < 200:
            current = queue_dirs.popleft()
            visited += 1
            if re.search(r'\b(?:PPSA|CUSA)\d{5}\b', current.name, re.I):
                return current
            try:
                queue_dirs.extend(p for p in current.iterdir() if p.is_dir())
            except PermissionError:
                continue

        # Legacy: single top-level folder unwrap
        try:
            items = [p for p in dest.iterdir() if p.is_dir()]
            if len(items) == 1:
                return items[0]
        except Exception:
            pass

        return dest


# ─── Game Item ─────────────────────────────────────────────────────────────────

# Files copied to the destination alongside a packed game, minus scene junk.
SCENE_JUNK_EXTS = {".nfo", ".sfv", ".txt", ".diz", ".url", ".md5", ".sha1",
                   ".srr", ".jpg", ".jpeg", ".png", ".gif", ".db"}

# OS/Finder/archiver metadata that must never enter the image OR be copied to the
# destination. The mkpfs backend filters the same set inside the image
# (pfs.is_fs_junk); this GUI copy path is a separate process, so it carries its own.
FS_JUNK_NAMES = {".DS_Store", ".localized", ".VolumeIcon.icns", ".apdisk",
                 "Thumbs.db", "ehthumbs.db", "desktop.ini",
                 # junk DIRECTORIES (so they're never carried as a DLC/extra sibling)
                 "__MACOSX", ".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems"}


def is_fs_junk_name(name: str) -> bool:
    """True for macOS/Windows filesystem junk (by basename). '._*' = AppleDouble."""
    return name in FS_JUNK_NAMES or name.startswith("._")


# Glob patterns for the dir-copy path (shutil.ignore_patterns) so a copied DLC/extra
# folder never carries OS/archiver metadata to the destination.
_COPYTREE_JUNK_GLOBS = ("__MACOSX", ".DS_Store", "._*", ".localized",
                        ".Spotlight-V100", ".fseventsd", ".Trashes", "Thumbs.db", "desktop.ini")


def detect_game_bundle(folder: Path, candidate_passwords=None, log_fn=None, detect_patch=False):
    """Inspect *folder* for the 'game + extras' layout.

    Returns (game_source, siblings, all_games, patch_source):
      • game_source — the single game (a game subfolder, a disk image, or an
        archive whose listing shows a PS5 game), or None.
      • siblings    — the other files to copy next to the output (scene junk and
        the game's own multi-part volumes removed).
      • all_games   — every game candidate found (so the caller can warn on >1).
      • patch_source — when *detect_patch* is set and the folder holds a base game
        plus one clearly-smaller game-like sibling (a patch carries eboot.bin, so it
        reads as a 'game' too), that sibling — to be overlaid via --patch. Else None.
    Archives are only *listed* here (peeked), never extracted.
    """
    try:
        entries = list(folder.iterdir())
    except Exception:
        return None, [], [], None   # 4-tuple — callers unpack (game, siblings, all_games, patch)
    files = [p for p in entries if p.is_file()]
    subdirs = [p for p in entries if p.is_dir()]

    archive_files = [f for f in files if f.suffix.lower() in (".zip", ".rar", ".7z")]
    image_files = [f for f in files if f.suffix.lower() in DISK_IMAGE_SUFFIXES]

    games: list[Path] = []

    # Game subfolders directly inside (a loose dump sitting in the folder).
    for d in subdirs:
        if is_game_folder(d):
            games.append(d)
        else:
            inner = find_game_folders(d, max_depth=2)
            if len(inner) == 1:
                games.append(inner[0])

    # Disk images are games as-is.
    games.extend(image_files)

    # Archives: only the first volume of each multi-part set is a candidate
    # (.part02+/.r01+ resolve back to the first volume and are skipped).
    first_volumes = [a for a in archive_files
                     if ArchiveExtractor._first_volume(a) == a]
    if first_volumes:
        if len(first_volumes) == 1 and not games:
            # One archive set and nothing else competing → it IS the game. Don't
            # peek: listing a multi-volume RAR reads through every part (slow over
            # USB). Extraction confirms it later, and a non-game archive just
            # fails with a clear "no game found".
            games.append(first_volumes[0])
            if log_fn:
                log_fn("INFO", f"  Game archive (by structure): {first_volumes[0].name}")
        else:
            # Ambiguous (several archive sets, or one alongside a folder/image) →
            # peek each to find which actually holds the game.
            for a in first_volumes:
                names = ArchiveExtractor.list_members(a, candidate_passwords)
                if names and ArchiveExtractor.names_look_like_game(names):
                    games.append(a)
                    if log_fn:
                        log_fn("INFO", f"  Game archive detected: {a.name}")
                elif log_fn:
                    log_fn("INFO", f"  Not a game (kept as extra): {a.name}")

    def _vol_set(cand: Path) -> set:
        """Every on-disk file belonging to *cand* — an archive's whole volume set,
        or the single file. Empty for a folder candidate."""
        s: set = set()
        if cand.is_file() and cand.suffix.lower() in (".zip", ".rar", ".7z"):
            for a in archive_files:
                if a == cand or ArchiveExtractor._first_volume(a) == cand:
                    s.add(a.resolve())
        elif cand.is_file():
            s.add(cand.resolve())
        return s

    def _cand_size(cand: Path) -> int:
        try:
            if cand.is_dir():
                return folder_size(cand)
            return sum((p.stat().st_size for p in _vol_set(cand)), 0) or cand.stat().st_size
        except Exception:
            return 0

    def _siblings_excluding(*cands: Path) -> list:
        exclude: set = set()
        for c in cands:
            exclude |= _vol_set(c)
        files_out = [f for f in files
                     if f.resolve() not in exclude
                     and f.suffix.lower() not in SCENE_JUNK_EXTS
                     and not is_fs_junk_name(f.name)]
        # Also carry whole EXTRA subfolders (e.g. an '[ ALL DLC ]' wrapper) — anything
        # that isn't the chosen game/patch, doesn't CONTAIN it, and isn't OS junk.
        game_paths = set()
        for g in cands:
            try:
                game_paths.add(g.resolve())
            except Exception:
                pass
        dirs_out = []
        for d in subdirs:
            try:
                rp = d.resolve()
            except Exception:
                continue
            if rp in game_paths:
                continue                                   # the game/patch folder itself
            if any(str(g) == str(rp) or str(g).startswith(str(rp) + os.sep) for g in game_paths):
                continue                                   # a wrapper that holds the game
            if is_fs_junk_name(d.name):
                continue
            dirs_out.append(d)
        return files_out + dirs_out

    # Normal case: exactly one game in the folder.
    if len(games) == 1:
        game = games[0]
        return game, _siblings_excluding(game), games, None

    # Auto-patch: a base game plus a single, clearly-smaller game-like sibling. A
    # patch carries eboot.bin, so it also reads as a 'game'; pick the larger as the
    # base and the smaller (a folder, or a zip/rar the backend can unpack) as the
    # patch — only when it is distinctly smaller, so two real games are not mistaken
    # for a base+patch pair.
    if detect_patch and len(games) == 2:
        base, other = sorted(games, key=_cand_size, reverse=True)
        bs, ps = _cand_size(base), _cand_size(other)
        # The patch must be a folder or a zip/rar the backend can unpack; the base
        # must resolve to a game folder (a disk image can't be overlaid this way).
        patchable = other.is_dir() or other.suffix.lower() in (".zip", ".rar")
        base_ok = base.is_dir() or base.suffix.lower() in (".zip", ".rar", ".7z")
        if patchable and base_ok and bs > 0 and ps <= 0.7 * bs:
            if log_fn:
                log_fn("INFO", f"  Auto-patch: base '{base.name}', patch '{other.name}'")
            return base, _siblings_excluding(base, other), games, other

    return None, [], games, None


def scan_parent_for_bundles(parent: Path, candidate_passwords=None, log_fn=None, detect_patch=False):
    """Treat *parent* as a library of games: every immediate subfolder that holds
    a game becomes its own bundle, so its folder is recreated at the destination
    with just the .ffpfsc (plus any extras) inside. Returns a list of bundle
    GameItems (one per game subfolder)."""
    items = []
    try:
        children = sorted((d for d in parent.iterdir() if d.is_dir()),
                          key=lambda p: p.name.lower())
    except Exception:
        return items
    for child in children:
        game, siblings, patch = None, [], None
        if is_game_folder(child):
            game = child                       # the subfolder itself is the dump
        else:
            g, sib, _all, p = detect_game_bundle(child, candidate_passwords, log_fn, detect_patch)
            if g is not None:
                game, siblings, patch = g, sib, p
            else:
                inner = find_game_folders(child, max_depth=2)
                if len(inner) == 1:
                    game = inner[0]
        if game is not None:
            try:
                items.append(GameItem.from_bundle(child, game, siblings, patch))
                if log_fn:
                    extra = f" + {len(siblings)} extra(s)" if siblings else ""
                    extra += " + patch" if patch else ""
                    log_fn("INFO", f"  Library game: {child.name}{extra}")
            except Exception as e:
                if log_fn:
                    log_fn("WARN", f"  Skipped {child.name}: {e}")
    return items


class GameItem:
    ampr_emu = False   # class-level default — guards history/from_* items built via __new__
    def __init__(self, path: Path):
        self.path       = path
        self.archive_path: Path | None = None   # set for archive placeholders
        self.operation  = "pack"
        self.name       = guess_game_name(path)
        self.title_id   = parse_title_id(path)
        self.size       = folder_size(path)
        self.files      = file_count(path)
        self.artwork    = find_artwork(path)
        self.status     = "Queued"
        self.source_kind    = "inplace"   # a folder is packed in place — no second copy
        self.extracted_size = self.size   # already extracted; honest size for space math
        self.ampr_emu       = is_apr_game(path)   # PlayGo/APR title? (auto-detected)

    @classmethod
    def from_archive(cls, archive: Path) -> "GameItem":
        """Placeholder item for an archive that has not been extracted yet."""
        obj          = cls.__new__(cls)
        # Normalize a directly-dropped multi-part volume back to the FIRST volume so the
        # size and the whole-set logic always cover the entire game, not one part.
        first = ArchiveExtractor._first_volume(archive)
        obj.path         = None
        obj.archive_path = first
        obj.operation    = "pack"
        obj.name         = first.stem
        obj.title_id     = "📦"
        obj.size         = archive_set_ondisk_size(first)   # whole compressed volume set
        obj.files        = 0
        obj.artwork      = None
        obj.status       = "Pending Extract"
        obj.password     = None          # optional per-archive password override
        obj.source_kind  = "archive"     # unpacks a SECOND copy onto the build drive
        # Honest extracted size from headers (scene RARs compress ~2:1, so the on-disk
        # size badly undershoots). Pass saved passwords for header-encrypted sets. Leave
        # 0 only when truly unreadable so the gate defers to the post-extraction re-check.
        try:
            pw = load_settings().get("archive_passwords") or []
        except Exception:
            pw = []
        hdr = ArchiveExtractor.uncompressed_size(first, pw)
        obj.extracted_size = hdr if hdr and hdr > obj.size else 0
        return obj

    @classmethod
    def from_bundle(cls, bundle_dir: Path, game_source: Path, siblings, patch_source=None) -> "GameItem":
        """A source folder holding one game (archive / game folder / disk image)
        plus extra files (DLCs etc.). The game is packed into a recreated
        '<bundle_dir name>' folder at the destination; the extras are copied next
        to the .ffpfsc. Reuses the archive/folder/image item, then tags it. When
        *patch_source* is set (auto-patch), it is overlaid onto the game via --patch
        before packing and is kept out of the copied siblings."""
        suffix = game_source.suffix.lower()
        if game_source.is_dir():
            obj = cls(game_source)
        elif suffix in DISK_IMAGE_SUFFIXES:
            obj = cls.from_exfat(game_source)
        else:
            obj = cls.from_archive(game_source)
        obj.bundle_dir       = bundle_dir
        obj.bundle_subfolder = bundle_dir.name
        obj.bundle_siblings  = list(siblings)
        obj.patch_source     = patch_source
        obj.name             = bundle_dir.name   # nicer queue label until extraction
        return obj

    @classmethod
    def from_exfat(cls, exfat_file: Path) -> "GameItem":
        """Item for a direct .exfat / .ffpkg disk image — passed straight to cli.py, no extraction needed."""
        obj              = cls.__new__(cls)
        obj.path         = exfat_file          # handed directly to the backend
        obj.archive_path = None                # not an archive — no extraction step
        obj.operation    = "pack"
        obj.name         = exfat_file.stem
        obj.title_id     = parse_title_id(exfat_file) or "💾"
        obj.size         = exfat_file.stat().st_size if exfat_file.exists() else 0
        obj.files        = 1
        obj.artwork      = None
        obj.status       = "Queued"
        obj.source_kind    = "inplace"   # disk image is read in place — no second copy
        obj._is_disk_image = True        # single-pass: mkpfs compresses directly, no temp inner image
        obj.extracted_size = obj.size
        return obj

    @classmethod
    def from_pfs_image(cls, image_file: Path) -> "GameItem":
        """Item for an existing .ffpfs / .ffpfsc image that should be unpacked."""
        obj              = cls.__new__(cls)
        obj.path         = image_file
        obj.archive_path = None
        obj.operation    = "unpack"
        obj.name         = image_file.stem
        obj.title_id     = parse_title_id(image_file) or "📤"
        obj.size         = image_file.stat().st_size if image_file.exists() else 0
        obj.files        = 1
        obj.artwork      = None
        obj.status       = "Queued"
        obj.source_kind    = "inplace"   # unpack op — no second copy on the build drive
        obj.extracted_size = obj.size
        return obj


# ─── CLI Worker ────────────────────────────────────────────────────────────────

class CLIWorker(threading.Thread):
    WEIGHTS = {
        "Scanning Files":      (0,    8),
        "Reading Game":        (8,   18),
        "Creating Temp PFS":   (18,  40),
        "Compressing":         (40,  80),
        "Extracting":          (18,  93),
        "Writing Final Image": (80,  93),
        "Verifying Output":    (93,  97),
        "Cleaning Up":         (97, 100),
        "Complete":            (100, 100),
    }

    def __init__(self, app, item, cmd, cwd, output_dir, temp_dir):
        super().__init__(daemon=True)
        self.app = app
        self.item = item
        self.cmd = cmd
        self.cwd = cwd
        self.output_dir = output_dir
        self.temp_dir = temp_dir
        self.proc = None
        self.start_time = 0
        self.last_heartbeat = 0
        self.last_log = 0
        self.last_status_ui = 0
        self.last_log_ui = 0
        self.phase = "Starting"
        self.operation = getattr(item, "operation", "pack")
        # Snapshot the copy-extras toggle on the MAIN thread (CLIWorker is constructed
        # there); Tk variables are not safe to read from the worker thread.
        try:
            self.copy_siblings = bool(app.copy_siblings_var.get())
        except Exception:
            self.copy_siblings = True
        self.output_path = ""
        self.final_size = 0
        self.speed = "—"
        self.temp_start_size = get_folder_size(self.temp_dir)
        self.temp_peak_size = self.temp_start_size
        self.last_cmd_str = " ".join(cmd)
        self.stage_progress = {
            "Scanning Files": 0,
            "Reading Game": 0,
            "Creating Temp PFS": 0,
            "Compressing": 0,
            "Extracting": 0,
            "Writing Final Image": 0,
            "Verifying Output": 0,
            "Cleaning Up": 0,
            "Complete": 0,
        }
        self.last_stage_bucket = {}
        self._mem_error_shown = False   # reset per-job so next run can show it again

    def run(self):
        ensure_app_dir()
        self.start_time = time.time()
        self.last_heartbeat = self.start_time
        _ticker_stop = threading.Event()   # stops the liveness ticker (see below)

        # A bundle writes its .ffpfsc into a recreated sub-folder; make sure that
        # folder exists before the backend tries to write into it.
        if self.operation != "unpack":
            try:
                self.output_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

        # Raw log strategy: rolling tail buffer.
        # All backend lines are held in a deque; error lines are always kept in a
        # separate list so they are never dropped.  At job end the last 10 MB of
        # regular output + all errors are written to disk.
        RAW_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB tail window
        from collections import deque as _deque
        _raw_lines: _deque[str] = _deque()   # rolling ring buffer (all lines)
        _raw_errors: list[str]  = []          # every ERROR/FAILED line (always kept)
        _raw_buf_bytes          = 0           # current byte count in _raw_lines

        def _raw_append(text: str):
            nonlocal _raw_buf_bytes
            encoded = text.encode("utf-8", errors="replace")
            _raw_lines.append(text)
            _raw_buf_bytes += len(encoded)
            # Keep last 10 MB — drop oldest lines from the front
            while _raw_buf_bytes > RAW_LOG_MAX_BYTES and _raw_lines:
                dropped = _raw_lines.popleft()
                _raw_buf_bytes -= len(dropped.encode("utf-8", errors="replace"))

        self.app.log("INFO", f"{APP_NAME} {APP_VERSION} started")
        self.app.log("INFO", f"Backend: {BACKEND_NAME}")
        self.app.log("INFO", f"MkPFS: {MKPFS_NAME} v{MKPFS_VERSION}")
        self.app.log("INFO", f"Operation: {'Unpack' if self.operation == 'unpack' else 'Pack'}")
        self.app.log("INFO", f"Game: {self.item.title_id} | {self.item.name}")
        self.app.log("INFO", f"Original: {format_size(self.item.size)} | Files: {self.item.files}")
        self.app.log("INFO", f"Backend Python: {' '.join(get_backend_python_command()) or 'NOT FOUND'}")
        self.app.log("CMD", self.last_cmd_str)
        _raw_append("[COMMAND] " + self.last_cmd_str + "\n")

        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            env["TEMP"] = str(self.temp_dir)
            env["TMP"] = str(self.temp_dir)
            env["TMPDIR"] = str(self.temp_dir)

            self.proc = subprocess.Popen(
                self.cmd,
                cwd=str(self.cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                universal_newlines=True,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                # Own session/process group on POSIX so cancel/close can kill the
                # whole tree (backend + mkpfs mp.Pool workers), not just the parent.
                start_new_session=(os.name != "nt"),
            )
            self.app.current_process = self.proc

            # ── Liveness ticker: heartbeat + temp-peak sampling on a wall-clock
            #    timer, independent of stdout. Without this the heartbeat lived
            #    inside the blocking stdout loop and never fired during silent
            #    phases (e.g. the final-image write), making the app look hung.
            def _ticker():
                while not _ticker_stop.wait(5):
                    t = time.time()
                    try:
                        self.temp_peak_size = max(self.temp_peak_size, get_folder_size(self.temp_dir))
                    except Exception:
                        pass
                    if t - self.last_heartbeat >= 30:
                        self.last_heartbeat = t
                        elapsed = format_duration(t - self.start_time)
                        try:
                            # Keep the status panel alive (elapsed ticking) during silent
                            # phases, but DON'T spam the log — a 30 s "Still working" line
                            # helps no one and buries the useful output.
                            self.app.status_update("Still Working", "Backend is active. Do not close the app.",
                                                    self.phase, self.stage_progress.get(self.phase, 0),
                                                    self._overall(), elapsed, self.speed, "—")
                        except Exception:
                            pass
            threading.Thread(target=_ticker, daemon=True).start()

            # ── Define flush helper (used periodically during run AND at end) ─
            _last_flush_t = time.time()
            FLUSH_INTERVAL = 30  # write raw log to disk every ~30 s during the run

            def _flush_raw_log():
                try:
                    RAW_LOG_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
                try:
                    with RAW_LOG_FILE.open("w", encoding="utf-8", errors="replace") as f:
                        if _raw_errors:
                            f.write("=" * 60 + "\n")
                            f.write("ERRORS / FAILURES ENCOUNTERED DURING THIS JOB\n")
                            f.write("=" * 60 + "\n")
                            f.writelines(_raw_errors)
                            f.write("=" * 60 + "\n\n")
                        f.write(f"[LAST {RAW_LOG_MAX_BYTES // (1024*1024)} MB OF BACKEND OUTPUT]\n\n")
                        f.writelines(_raw_lines)
                except Exception:
                    pass

            # ── Read backend output line by line ──────────────────────────────
            for line in self.proc.stdout or []:
                if self.app.cancel_requested:
                    self._terminate()
                    break

                clean = line.rstrip("\r\n")
                _raw_append(clean + "\n")
                # Always collect error lines separately so they survive the tail trim
                upper_c = clean.upper()
                if re.search(r'\bERROR\b|\bFAILED\b', upper_c):
                    _raw_errors.append(clean + "\n")
                self._handle_line(clean)

                # Heartbeat now runs on the independent ticker thread (above), so
                # it keeps firing during silent phases. Here we only flush the log.
                t = time.time()
                if t - _last_flush_t >= FLUSH_INTERVAL:
                    _flush_raw_log()
                    _last_flush_t = t

            # ── stdout fully consumed — now wait for process to exit ───────────
            code = self.proc.wait() if self.proc else 1

            # ── Final flush of rolling log buffer ─────────────────────────────
            _flush_raw_log()

            if self.app.cancel_requested:
                self.app.finish(False, "Cancelled by user.", self.last_cmd_str)
                return
            if code != 0:
                smart = smart_error_from_log()
                msg = smart if smart else f"Backend exited with code {code}."
                # Flag an out-of-memory kill so the main thread can auto-retry with fewer
                # cores: either a printed MemoryError, or a SIGKILL (-9 / 137) during a
                # pack — the OS memory-pressure kill leaves mkpfs no chance to print one.
                if self.operation != "unpack" and (getattr(self, "_mem_error_shown", False)
                                                    or code in (-9, 137)):
                    self.oom_killed = True
                self.app.finish(False, msg, self.last_cmd_str)
                return

            self._set_stage("Cleaning Up", 100, "Temporary cleanup finished.")
            try:
                self.temp_peak_size = max(self.temp_peak_size, get_folder_size(self.temp_dir))
            except Exception:
                pass
            if not self._find_output():
                self._write_report(False)
                expected = "extracted output folder" if self.operation == "unpack" else "new .ffpfsc output"
                self.app.finish(False, f"Backend exited but no {expected} was created.", self.last_cmd_str)
                return

            # Bundle: copy the extra files (DLCs etc.) next to the new .ffpfsc.
            self._copy_bundle_siblings()

            # ShadowMount compatibility checks
            if self.operation != "unpack":
                for w in self._validate_shadowmount():
                    self.app.log("WARN", w)
            self._write_report(True)
            # NOTE: history is recorded on the MAIN thread in the done_q handler
            # (add_history mutates Tk widgets, which are not thread-safe).
            success_msg = "Extraction completed successfully." if self.operation == "unpack" else "Compression completed successfully."
            self.app.finish(True, success_msg, self.last_cmd_str)
        except Exception as e:
            try:
                _raw_append(f"[GUI ERROR] {e}\n")
                _flush_raw_log()
            except Exception:
                pass
            self.app.finish(False, str(e), self.last_cmd_str)
        finally:
            _ticker_stop.set()
            self.app.current_process = None

    def _stage_from_label(self, label: str, raw: str) -> str | None:
        """Return the stage name inferred from a progress-bar label, or None if uncertain.

        Returning None means the caller should NOT update the stage — the line
        didn't contain enough signal to be confident about which stage we're in.
        This prevents unrecognised lines from locking the display on the current stage.
        """
        text = f"{label} {raw}".lower()  # full combined text for substring checks

        # The backend progress-bar label is everything after the "%" —
        #   "[###] 65% compress @ 290.95 MB/s ETA 14s"  → label = "compress @ 290.95 MB/s ETA 14s"
        #   "[###] 45% write @ 980.61 MB/s ETA 0s"      → label = "write @ 980.61 MB/s ETA 0s"
        #   "[###]  2% scan"                             → label = "scan"
        # Use the FIRST WORD of the label for reliable matching regardless of trailing speed/ETA.
        first_word = label.strip().lower().split()[0] if label.strip() else ""

        if self.operation == "unpack":
            if first_word in ("extract", "extracting", "unpack", "unpacking") or "extract" in text or "unpack" in text:
                return "Extracting"
            if first_word in ("read", "reading", "scan", "scanning") or "discover" in text:
                return "Reading Game"
            return "Extracting"

        # ── Final output ──────────────────────────────────────────────────────
        # Check before generic "write" so ".ffpfsc" always wins
        if ".ffpfsc" in text or "final image" in text or "final output" in text:
            return "Writing Final Image"

        # ── Backend label "write" ─────────────────────────────────────────────
        # Emitted during temp-PFS construction (before compress) AND final image write (after).
        # Distinguish by whether compression has started yet.
        if first_word in ("write", "writing"):
            if self.stage_progress.get("Compressing", 0) > 0:
                return "Writing Final Image"
            else:
                return "Creating Temp PFS"

        # ── Scan / discovery ──────────────────────────────────────────────────
        if first_word in ("scan", "scanning") or "discover" in text:
            return "Scanning Files"

        # ── Reading game files ────────────────────────────────────────────────
        if first_word in ("read", "reading"):
            return "Reading Game"

        # ── Compression ───────────────────────────────────────────────────────
        if first_word in ("compress", "compressing") or "compress" in text:
            return "Compressing"

        # ── Verify (only from a real progress bar, not plain-text messages) ───
        if first_word in ("verify", "verifying"):
            return "Verifying Output"

        if first_word in ("extract", "extracting", "unpack", "unpacking") or "extract" in text or "unpack" in text:
            return "Extracting"

        # ── Looser substring fallbacks for non-standard backend messages ──────
        if "scan" in text or "discover" in text:
            return "Scanning Files"
        if "read" in text and "write" not in text:
            return "Reading Game"
        if "clean" in text or "delete" in text or "removed" in text:
            return "Cleaning Up"

        # "Complete" is NEVER returned here — set only by run() after exit.
        # Returning None means: "uncertain — don't change the stage display."
        return None

    def _overall_for_stage(self, stage: str, pct: float) -> float:
        start, end = self.WEIGHTS.get(stage, (0, 100))
        return max(0, min(100, start + (max(0, min(100, pct)) / 100) * (end - start)))

    def _overall(self):
        return self._overall_for_stage(self.phase, self.stage_progress.get(self.phase, 0))

    # Ordered list used to prevent backward stage transitions.
    # Must be a plain tuple/list literal here — _STAGE_DEFS is defined later in
    # the module (after CLIWorker), so we can't reference it at class-body time.
    _STAGE_ORDER = [
        "Scanning Files", "Reading Game", "Creating Temp PFS",
        "Compressing", "Extracting", "Writing Final Image", "Verifying Output",
        "Cleaning Up", "Complete",
    ]

    def _set_stage(self, stage, pct, label="", eta="—", force=False):
        # Never allow the stage to regress (e.g. backend prints "Writing PFS image"
        # after compression has already started — that would snap back to Temp PFS).
        if not force and stage in self._STAGE_ORDER and self.phase in self._STAGE_ORDER:
            if self._STAGE_ORDER.index(stage) < self._STAGE_ORDER.index(self.phase):
                return
        self.phase = stage
        pct = max(0, min(100, pct))
        if stage == "Creating Temp PFS" and pct >= 100:
            pct = 99
        self.stage_progress[stage] = max(self.stage_progress.get(stage, 0), pct)

        # When a later stage begins, snap earlier stages to 100% so the
        # breadcrumbs never show a stale partial % (e.g. "Temp PFS 5%").
        # This handles backends that stop emitting progress before 100%.
        if stage == "Compressing":
            self.stage_progress["Creating Temp PFS"] = 100
            self.stage_progress["Reading Game"]       = 100
        elif stage == "Writing Final Image":
            self.stage_progress["Creating Temp PFS"] = 100
            self.stage_progress["Compressing"]        = 100
        elif stage == "Verifying Output":
            self.stage_progress["Writing Final Image"] = 100
            self.stage_progress["Compressing"]         = 100
        elif stage == "Extracting":
            self.stage_progress["Scanning Files"] = 100
            self.stage_progress["Reading Game"]   = 100
        elif stage in ("Cleaning Up", "Complete"):
            for s in ("Scanning Files", "Reading Game", "Creating Temp PFS",
                      "Compressing", "Extracting", "Writing Final Image"):
                if self.stage_progress.get(s, 0) > 0:
                    self.stage_progress[s] = 100

        elapsed = format_duration(time.time() - self.start_time)
        overall = self._overall_for_stage(stage, self.stage_progress[stage])
        # For an item that was unpacked from an archive, the GUI already showed the
        # extraction as the first ARCHIVE_EXTRACT_OVERALL_PCT% of this game's overall
        # progress. Compress the worker's own 0-100 pack progress into the remaining
        # tail so the whole-game `overall` stays MONOTONIC across extraction → pack
        # (no overshoot, no backward jump) and the QUEUE bar isn't a mirror of the step.
        if getattr(self.item, "_from_archive", False):
            overall = ARCHIVE_EXTRACT_OVERALL_PCT + overall * (100 - ARCHIVE_EXTRACT_OVERALL_PCT) / 100.0

        detail = label or f"{stage} is active."
        if stage == "Creating Temp PFS":
            detail = ("Building temporary PFS image. "
                      "Large games may look frozen here — the backend is still working. "
                      "Do NOT close the app.")
        elif stage == "Cleaning Up":
            detail = "Cleaning up temporary files. Please wait before closing the app."
        elif stage == "Writing Final Image":
            # Backend writes the final .ffpfsc silently (no progress bars) — the display
            # may show 0% for a while then snap to 100% when the write finishes.
            detail = ("Writing the final .ffpfsc output file. "
                      "This stage may show 0% — the backend is writing silently. "
                      "Do NOT close the app.")
        elif stage == "Compressing" and not label:
            detail = "Compressing game data."
        elif stage == "Extracting" and not label:
            detail = "Extracting PFS image contents."

        now = time.time()
        bucket = (int(self.stage_progress[stage]) // 5) * 5
        should_update_ui = (force
                            or (now - self.last_status_ui >= 0.5)
                            or self.last_stage_bucket.get(stage, -1) != bucket
                            or int(self.stage_progress[stage]) == 100)
        if should_update_ui:
            self.last_status_ui = now
            self.app.status_update(stage, detail, stage, self.stage_progress[stage],
                                   overall, elapsed, self.speed, eta)

        # Only log at 5 % bucket boundaries or when a stage hits 100 %.
        # Do NOT log at 0 % on every bar — that floods the log when the backend
        # emits dozens of 0 % lines before the first real progress tick.
        if self.last_stage_bucket.get(stage, -1) != bucket or int(self.stage_progress[stage]) == 100:
            self.last_stage_bucket[stage] = bucket
            self.app.log("PROGRESS", f"{stage}: {int(self.stage_progress[stage])}% {label}".strip())

    def _handle_line(self, line):
        if not line:
            return

        lower = line.lower()
        upper = line.upper()

        # ── Intercept raw Python exception tracebacks from the backend ────────
        # Convert confusing Python tracebacks into readable, actionable messages
        # and always include UI settings suggestions the user can act on right now.

        # MemoryError — mkpfs multiprocessing pool ran out of RAM.
        # Each parallel worker loads a chunk of the source file; too many cores = OOM.
        # Only match raw Python exception lines (no leading '[' bracket like [INFO]/[OK]).
        # This avoids false-positives from mkpfs info messages that mention "MemoryError".
        # Only show once per job to avoid log spam.
        stripped = line.strip()
        if (stripped == "MemoryError"
                or ("memoryerror" in lower and not stripped.startswith("[") and "avoid" not in lower)):
            if not getattr(self, "_mem_error_shown", False):
                self._mem_error_shown = True
                self.app.log("ERROR",
                    "❌  Out of RAM — mkpfs ran out of memory during parallel compression.\n"
                    "\n"
                    "  What happened:\n"
                    "    mkpfs spawns one worker process per CPU core. Each worker holds\n"
                    "    compressed data in RAM. Too many cores = not enough memory.\n"
                    "\n"
                    "  ╔═ Try these settings (Compression Tuning bar): ══════════════╗\n"
                    "  ║  CPU cores  →  set to 2 (or 1 for very large games)         ║\n"
                    "  ║  Level      →  try 5 instead of 7 (less RAM per worker)     ║\n"
                    "  ║  Block size →  try 16384 or 32768 (smaller per-block buffers) ║\n"
                    "  ╚═══════════════════════════════════════════════════════════════╝"
                )
            return

        # Downstream noise caused by the MemoryError — suppress silently.
        if "concurrent send_bytes" in lower or "maybeencodingerror" in lower:
            return

        # OSError [Errno 22] Invalid argument on write.
        # Either the output drive is exFAT/FAT32 (4 GB file limit) or a corrupt
        # chunk was written after an OOM crash.
        if ("oserror" in lower or "ioerror" in lower) and (
            "errno 22" in lower or "invalid argument" in lower
        ):
            self.app.log("ERROR",
                "❌  Write failed — OS error 22 (Invalid argument).\n"
                "\n"
                "  Most likely cause:  output drive is exFAT or FAT32\n"
                "    exFAT / FAT32 has a 4 GB per-file limit.\n"
                "    A large .ffpfsc will exceed this and fail mid-write.\n"
                "\n"
                "  ╔═ Settings to check / change: ═══════════════════════════════╗\n"
                "  ║  OUTPUT folder  →  move to an NTFS drive (e.g. C:\\  D:\\)   ║\n"
                "  ║  CPU cores      →  set to 1–2 if RAM could also be the cause ║\n"
                "  ╚══════════════════════════════════════════════════════════════╝"
            )
            return

        # No-space-left / disk full
        if ("errno 28" in lower or "no space left" in lower
                or "there is not enough space" in lower):
            self.app.log("ERROR",
                "❌  Disk full — the output or temp drive ran out of space.\n"
                "\n"
                "  ╔═ Settings to check: ════════════════════════════════════════╗\n"
                "  ║  OUTPUT folder  →  point to a drive with more free space    ║\n"
                "  ║  TEMP folder    →  point to a drive with more free space    ║\n"
                "  ║                    (needs ~1.5× the game size during build) ║\n"
                "  ╚══════════════════════════════════════════════════════════════╝"
            )
            return

        if "calledprocesserror" in lower and "non-zero exit status" in lower:
            self.app.log("ERROR", "❌  mkpfs exited with an error — see messages above.")
            if not getattr(self, "_mem_error_shown", False):
                # Generic hint only when a more specific error wasn't already shown.
                self.app.log("ERROR",
                    "  Common causes & settings to try:\n"
                    "\n"
                    "  ╔═ Check these settings: ══════════════════════════════════════╗\n"
                    "  ║  OUTPUT folder  →  must be NTFS (not exFAT / FAT32)          ║\n"
                    "  ║  TEMP folder    →  needs ~1.5× game size of free space       ║\n"
                    "  ║  CPU cores      →  lower to 2 or 1 if you have limited RAM   ║\n"
                    "  ║  Level          →  try 5 if high level causes OOM            ║\n"
                    "  ╚═══════════════════════════════════════════════════════════════╝\n"
                    "  If mkpfs is missing:  pip install mkpfs\n"
                    "  Full error detail is in the raw log."
                )
            return

        if "Compression complete:" in line:
            self.output_path = line.split("Compression complete:", 1)[-1].strip()
        if "Extraction complete:" in line:
            self._set_stage("Extracting", 100, "Extraction complete.")
            maybe_path = line.split("Extraction complete:", 1)[-1].strip()
            if maybe_path:
                self.output_path = maybe_path
        if self.operation == "unpack" and re.search(r"\bOutput:\s+", line):
            self.output_path = re.split(r"\bOutput:\s+", line, maxsplit=1)[-1].strip()

        prog = PROGRESS_RE.search(line)
        if prog:
            pct = max(0, min(100, int(prog.group("pct"))))
            label = prog.group("label").strip()
            stage = self._stage_from_label(label, line)

            sp = re.search(r"@\s*([0-9.]+\s*(?:GB|MB)/s)", label, re.I)
            if sp:
                self.speed = sp.group(1)

            # Backend reports raw seconds ('ETA 1695s') — show it as 1h 05m / 28m 15s / 45s,
            # both in the dedicated ETA field AND inside the detail line (label is shown as-is).
            eta_match = re.search(r"ETA\s*([0-9]+\s*(?:s|m|h|sec|secs|seconds|min|mins|minutes)?)", label, re.I)
            if eta_match:
                eta = humanize_eta(eta_match.group(1).strip())
                label = label[:eta_match.start()] + f"ETA {eta}" + label[eta_match.end():]
            else:
                eta = "—"

            if stage is None:
                # Unrecognised progress line — update speed/eta but don't change stage
                return

            if stage == "Reading Game" and pct >= 100:
                self._set_stage("Reading Game", 100, label, eta)
                self._set_stage("Creating Temp PFS", 0, "Building temporary PFS image. Do NOT close the app.", "—")
                return

            if stage == "Compressing" and pct >= 100:
                self._set_stage("Compressing", 100, label, eta)
                # Auto-advance: compression finished — final image write is next.
                # If the backend emits its own "write" progress bars for the final
                # output, they will continue updating "Writing Final Image" from here.
                # If it writes silently, this at least moves the display off "Compressing".
                self._set_stage("Writing Final Image", 0, "Writing final .ffpfsc output file…", "—")
                return

            self._set_stage(stage, pct, label, eta)
            return

        # Hardlink / symlink failure — warn immediately, don't wait for exit code
        if "unable to stage source file" in lower or "hard link and symlink both failed" in lower:
            self.app.log("WARN",
                "⚠  Temp drive does not support hardlinks/symlinks. "
                "Fallback to copy mode — compression will be slower and needs extra space.")

        # Inner image auto-rename (MkPFS 0.0.8) — informational, not an error
        if "renaming inner image" in lower or "inner image renamed" in lower:
            self.app.log("INFO",
                "ℹ  mkpfs renamed the inner image to match the outer filename. "
                "This is normal — the .ffpfsc will mount correctly.")

        # Plain-text (non-progress-bar) stage hints.
        # IMPORTANT: only use very specific phrases here — broad keyword matches on
        # paths (e.g. _ffpfsc_temp, pfs_image.dat) fire too early because those
        # strings appear in the parameter dump before scanning even begins.
        if "writing pfs image to" in lower:
            # Only the exact "Writing PFS image to <path>" line marks temp-PFS start.
            # Use 0% so the subsequent [###] x% write progress bars can own the percentage
            # cleanly (the max() guard in _set_stage would pin it at 5 otherwise).
            self._set_stage("Creating Temp PFS", 0, "Building PFS image…")
        elif ".ffpfsc" in lower and self.stage_progress.get("Compressing", 0) > 0:
            # A line mentioning the final .ffpfsc output after compression has run
            # means the final image is being written (or has just been written).
            self._set_stage("Writing Final Image",
                            max(5, self.stage_progress.get("Writing Final Image", 0)),
                            "Writing final .ffpfsc output file…")
        elif "successfully wrote" in lower or "pfs creation complete" in lower:
            # PFS image fully written — advance to Compressing if not already there
            if self.stage_progress.get("Compressing", 0) == 0:
                self._set_stage("Creating Temp PFS", 100, line)
        elif self.operation == "unpack" and ("extract" in lower or "files written" in lower or "dirs created" in lower):
            self._set_stage("Extracting", 100 if "complete" in lower else max(5, self.stage_progress.get("Extracting", 0)), line)
        # NOTE: "Verifying Output" is NOT triggered from plain-text here because
        # lines like "MkPFS post-build verify is disabled..." contain "verify" and
        # would fire this stage at the very start of the run, blocking everything else.
        # Verification stage is advanced only by progress bars in _stage_from_label.
        # NOTE: "Complete" stage is intentionally NOT set here — only by run() after exit.

        tag = "INFO"
        # Use word-boundary regex so "MemoryError", "TypeError", etc. don't
        # falsely tag an INFO line as ERROR.
        if re.search(r'\bERROR\b|\bFAILED\b', upper):
            tag = "ERROR"
        elif re.search(r'\bWARN\b|\bWARNING\b', upper):
            tag = "WARN"
        elif "SUCCESS" in upper or "[OK]" in upper or "COMPLETE" in upper:
            tag = "OK"

        # Errors and warnings are ALWAYS shown — never throttled.
        always_show = tag in ("ERROR", "WARN")
        important = always_show or tag != "INFO" or any(k in upper for k in [
            "BUILD SUMMARY", "TOTAL FILES", "TOTAL UNCOMPRESSED", "TOTAL STORED",
            "INPUT PATH", "OUTPUT PATH", "ELAPSED", "THROUGHPUT",
            "DISCOVERING", "COMPRESSING", "WRITING", "VERIFY", "INSPECT"
        ])
        t = time.time()
        if important or t - self.last_log >= 3:
            if not always_show:
                self.last_log = t
            self.app.log(tag, line)

    def _find_output(self):
        if self.output_path:
            p = Path(self.output_path.strip('"'))
            try:
                if self.operation == "unpack" and p.exists() and p.is_dir():
                    self.final_size = get_folder_size(p)
                    return True
                if p.exists() and p.is_file() and p.stat().st_size > 0 and p.stat().st_mtime >= self.start_time - 2:
                    self.final_size = p.stat().st_size
                    return True
            except OSError:
                pass

        if self.operation == "unpack":
            expected = self.output_dir / f"{self.item.path.stem}_extracted"
            if expected.exists() and expected.is_dir():
                self.output_path = str(expected)
                self.final_size = get_folder_size(expected)
                return True
            if self.output_dir.exists() and self.output_dir.is_dir():
                self.output_path = str(self.output_dir)
                self.final_size = get_folder_size(self.output_dir)
                return True

        # Prefer this job's expected name (<title_id>.ffpfsc) over "newest in the
        # folder" — the latter can wrongly attribute a stale/unrelated .ffpfsc to
        # a run that actually produced nothing, or pick the wrong one in a batch.
        if self.operation != "unpack":
            tid = (getattr(self.item, "title_id", "") or "").strip()
            if tid and tid not in ("📦", "Unknown"):
                try:
                    # Match both "<tid>.ffpfsc"/".ffpfs" and the descriptive
                    # "<name> [<tid>].ffpfsc"; pick the newest one this run created.
                    cands = [p for p in self.output_dir.glob(f"*{tid}*.ffpfs*")
                             if p.is_file() and p.stat().st_size > 0
                             and p.stat().st_mtime >= self.start_time - 2
                             and p.suffix.lower() in (".ffpfsc", ".ffpfs")]
                    if cands:
                        best = max(cands, key=lambda p: p.stat().st_mtime)
                        self.output_path = str(best)
                        self.final_size = best.stat().st_size
                        return True
                except OSError:
                    pass

        newest = find_newest_ffpfsc_after(self.output_dir, self.start_time)
        if newest:
            self.output_path = str(newest)
            self.final_size = newest.stat().st_size
            return True

        self.output_path = ""
        self.final_size = 0
        return False

    def _copy_bundle_siblings(self) -> None:
        """Copy the extra files AND folders (DLCs etc.) next to the packed .ffpfsc.
        Handles both loose files and whole subfolders (e.g. an '[ ALL DLC ]' wrapper),
        copied 1:1 into the output directory. No-op unless the item carries siblings and
        the user has 'copy extras' enabled. The source is never modified (copy, not move)."""
        siblings = getattr(self.item, "bundle_siblings", None)
        if not siblings:
            return
        if not getattr(self, "copy_siblings", True):
            self.app.log("INFO", "Copy extras is off — leaving DLC/extra files in the source folder.")
            return
        dest_dir = self.output_dir
        copied = 0
        for src in siblings:
            try:
                src = Path(src)
                if not src.exists():
                    continue
                target = dest_dir / src.name
                if src.is_dir():
                    # DLC / extra subfolder → copy the whole tree (merge). Skip when an
                    # identical-size copy is already there so a re-run doesn't re-copy GBs.
                    if target.resolve() == src.resolve():
                        continue   # source already sits in the destination — nothing to do
                    if target.exists() and get_folder_size(target) == get_folder_size(src):
                        copied += 1
                        continue
                    self.app.log("INFO", f"Copying extra folder next to output: {src.name} "
                                         f"({format_size(get_folder_size(src))})")
                    shutil.copytree(src, target, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns(*_COPYTREE_JUNK_GLOBS))
                    copied += 1
                elif src.is_file():
                    if target.exists() and target.stat().st_size == src.stat().st_size:
                        copied += 1
                        continue
                    self.app.log("INFO", f"Copying extra next to output: {src.name} ({format_size(src.stat().st_size)})")
                    shutil.copy2(src, target)
                    copied += 1
            except Exception as e:
                self.app.log("WARN", f"Could not copy extra '{Path(src).name}': {e}")
        if copied:
            self.app.log("OK", f"Copied {copied} extra item(s) next to the .ffpfsc.")

    def _validate_shadowmount(self) -> list:
        """Post-compression ShadowMount compatibility checks.
        Returns a list of warning strings (empty = all OK)."""
        warns = []
        if not self.output_path:
            return ["No output path recorded — cannot validate output."]
        p = Path(self.output_path)
        if not p.exists():
            warns.append(f"Output file not found on disk: {p.name}")
            return warns
        name_lower = p.name.lower()
        if name_lower.endswith(".ffpfsc.ffpfsc"):
            warns.append(
                f"⚠ Double extension detected: {p.name}\n"
                "   Rename the file — remove one '.ffpfsc' suffix before mounting in ShadowMount."
            )
        elif not name_lower.endswith(".ffpfsc"):
            warns.append(
                f"⚠ Unexpected output extension '{p.suffix}' — expected .ffpfsc\n"
                "   ShadowMount may not recognise this file."
            )
        sz = p.stat().st_size
        if sz == 0:
            warns.append("⚠ Output file is 0 bytes — compression may have failed silently.")
        elif sz < 1 * 1024 * 1024:
            warns.append(
                f"⚠ Output file is very small ({format_size(sz)}) — "
                "the source dump may be incomplete or empty."
            )
        return warns

    def _write_report(self, success=True):
        if success:
            self._find_output()
        elapsed = time.time() - self.start_time
        if self.operation == "unpack":
            FINAL_REPORT_FILE.write_text(
                f"{APP_NAME} Report\n\n"
                f"Status: {'Success' if success else 'Failed'}\n"
                f"Operation: Extract PFS image\n"
                f"Source: {self.item.path}\n"
                f"Output: {self.output_path or 'Unknown'}\n"
                f"Source Size: {format_size(self.item.size)}\n"
                f"Extracted Size: {format_size(self.final_size)}\n"
                f"Elapsed: {format_duration(elapsed)}\n"
                f"Backend: {BACKEND_NAME}\n"
                f"MkPFS: {MKPFS_NAME} v{MKPFS_VERSION}\n",
                encoding="utf-8",
                errors="replace",
            )
            return
        saved = self.item.size - self.final_size if self.item.size and self.final_size else 0
        pct = (saved / self.item.size * 100) if self.item.size else 0
        rating, recommendation = compression_rating(pct)
        temp_removed = max(0, self.temp_peak_size - get_folder_size(self.temp_dir))
        FINAL_REPORT_FILE.write_text(
            f"{APP_NAME} Report\n\n"
            f"Status: {'Success' if success else 'Failed'}\n"
            f"Game: {self.item.name}\n"
            f"Title ID: {self.item.title_id}\n"
            f"Source: {self.item.path}\n"
            f"Output: {self.output_path or 'Unknown'}\n"
            f"Original Size: {format_size(self.item.size)}\n"
            f"Output Size: {format_size(self.final_size)}\n"
            f"Space Saved: {format_size(saved)} ({pct:.2f}%)\n"
            f"Compression Rating: {rating}\n"
            f"Recommendation: {recommendation}\n"
            f"Peak Temp Usage Seen: {format_size(self.temp_peak_size)}\n"
            f"Temporary Files Removed: {format_size(temp_removed)}\n"
            f"Elapsed: {format_duration(elapsed)}\n"
            f"Backend: {BACKEND_NAME}\n"
            f"MkPFS: {MKPFS_NAME} v{MKPFS_VERSION}\n",
            encoding="utf-8",
            errors="replace",
        )

    def _terminate(self):
        _kill_process_tree(self.proc)


# Stage definitions: (full backend name, short display label)
_STAGE_DEFS = [
    ("Scanning Files",      "Scan"),
    ("Extracting",          "Extract"),    # archives extract first; folders skip this station
    ("Reading Game",        "Read"),
    ("Creating Temp PFS",   "Temp PFS"),
    ("Compressing",         "Compress"),
    ("Writing Final Image", "Write"),
    ("Verifying Output",    "Verify"),
    ("Cleaning Up",         "Cleanup"),
    ("Complete",            "Done"),
]

def _kill_process_tree(proc) -> None:
    """Terminate a backend subprocess AND its child process group (the mkpfs
    multiprocessing Pool workers). The backend is launched with start_new_session,
    so its pgid == its pid; killing the group stops the forked workers too — a bare
    proc.terminate() leaves them orphaned, burning CPU and writing temp with no UI."""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                proc.kill()
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()
    except Exception:
        pass


class PatchDialog(ctk.CTkToplevel):
    """Collect a game (.ffpfsc / folder / archive) + a patch (folder / archive) and
    kick off 'patch into game' — unpack the game, overlay the patch files, repack."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Integrate Patch into Game")
        self.geometry("640x360")
        self.configure(fg_color=BLACK)
        self.resizable(False, False)
        self.transient(app.root); self.lift(); self.focus_force()
        self.after(50, self.grab_set)
        self.game_var = tk.StringVar()
        self.patch_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="new")

        ctk.CTkLabel(self, text="🩹  Integrate Patch into Game",
                      font=ctk.CTkFont(size=18, weight="bold"), text_color=GREEN
                      ).pack(anchor="w", padx=20, pady=(16, 2))
        ctk.CTkLabel(self, text="Unpacks the game, overlays the patch files (overwrite + new), and repacks.",
                      text_color=MUTED, wraplength=600, justify="left").pack(anchor="w", padx=20, pady=(0, 10))

        self._file_row("Game  (.ffpfsc, folder, or archive):", self.game_var,
                       [("Game / Archive", "*.ffpfsc *.zip *.rar *.7z")])
        self._file_row("Patch  (archive or folder of loose files):", self.patch_var,
                       [("Archive", "*.zip *.rar *.7z")])

        mode_row = ctk.CTkFrame(self, fg_color=BLACK); mode_row.pack(fill="x", padx=20, pady=(10, 4))
        ctk.CTkLabel(mode_row, text="Output:", text_color=WHITE).pack(side="left", padx=(0, 10))
        ctk.CTkRadioButton(mode_row, text="New file ( … [patched].ffpfsc )", variable=self.mode_var,
                            value="new", fg_color=GREEN, hover_color=GREEN2).pack(side="left", padx=6)
        ctk.CTkRadioButton(mode_row, text="Overwrite original", variable=self.mode_var,
                            value="overwrite", fg_color=GREEN, hover_color=GREEN2).pack(side="left", padx=6)

        btns = ctk.CTkFrame(self, fg_color=BLACK); btns.pack(fill="x", padx=20, pady=16)
        ctk.CTkButton(btns, text="▶  Start patch", fg_color=GREEN, hover_color=GREEN2,
                       text_color="#061006", font=ctk.CTkFont(size=14, weight="bold"),
                       command=self._go).pack(side="right", padx=(8, 0))
        ctk.CTkButton(btns, text="Cancel", fg_color=CARD2, text_color=WHITE,
                       hover_color=("#b0b0b0", "#2a2a2a"), command=self.destroy).pack(side="right")

    def _file_row(self, label, var, filetypes):
        row = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=8); row.pack(fill="x", padx=20, pady=4)
        ctk.CTkLabel(row, text=label, text_color=WHITE, font=ctk.CTkFont(size=11)).pack(anchor="w", padx=10, pady=(6, 0))
        inner = ctk.CTkFrame(row, fg_color=PANEL); inner.pack(fill="x", padx=10, pady=(2, 8))
        ctk.CTkEntry(inner, textvariable=var, fg_color=CARD2, text_color=WHITE).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(inner, text="File", width=58, fg_color=CARD2, hover_color=GREEN2, text_color=WHITE,
                       command=lambda: self._pick_file(var, filetypes)).pack(side="left", padx=(6, 0))
        ctk.CTkButton(inner, text="Folder", width=64, fg_color=CARD2, hover_color=GREEN2, text_color=WHITE,
                       command=lambda: self._pick_dir(var)).pack(side="left", padx=(6, 0))

    def _pick_file(self, var, filetypes):
        p = filedialog.askopenfilename(filetypes=filetypes + [("All files", "*.*")])
        if p:
            var.set(p)

    def _pick_dir(self, var):
        p = filedialog.askdirectory()
        if p:
            var.set(p)

    def _go(self):
        g = self.game_var.get().strip(); p = self.patch_var.get().strip()
        if not g or not p:
            messagebox.showerror("Missing", "Please select BOTH a game and a patch.", parent=self)
            return
        overwrite = self.mode_var.get() == "overwrite"
        self.destroy()
        self.app._start_patch(g, p, overwrite)


class ConverterDialog(ctk.CTkToplevel):
    """Stepwise image converter: take a packed image apart one step at a time —
    .ffpfsc → .ffpfs (decompress, fast) → folder. The chosen conversion is added to the
    queue; the user then presses START (it reuses the normal unpack pipeline). Compressing
    a folder or a .ffpfs INTO a .ffpfsc is the normal queue + the top format switch."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Image Converter")
        self.geometry("680x440")
        self.configure(fg_color=BLACK)
        self.resizable(False, False)
        self.transient(app.root); self.lift(); self.focus_force()
        self.after(50, self.grab_set)
        self.src_var = tk.StringVar()

        ctk.CTkLabel(self, text="🔄  Image Converter",
                      font=ctk.CTkFont(size=18, weight="bold"), text_color=GREEN
                      ).pack(anchor="w", padx=24, pady=(18, 2))
        ctk.CTkLabel(self, text="Pick an image, then choose a step along the chain. The conversion is "
                                "added to the queue — press ▶ START to run it.",
                      text_color=MUTED, wraplength=632, justify="left").pack(anchor="w", padx=24, pady=(0, 14))

        # Source row + a detected-type badge.
        row = ctk.CTkFrame(self, fg_color=BLACK); row.pack(fill="x", padx=24, pady=(0, 6))
        ctk.CTkEntry(row, textvariable=self.src_var, fg_color=CARD, border_color=BORDER2,
                      text_color=WHITE, placeholder_text="Select a .ffpfsc / .ffpfs file or a folder…"
                      ).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="File…", width=66, fg_color=CARD2, text_color=WHITE,
                       hover_color=("#b0b0b0", "#2a2a2a"), command=self._pick_file).pack(side="left", padx=(8, 0))
        ctk.CTkButton(row, text="Folder…", width=78, fg_color=CARD2, text_color=WHITE,
                       hover_color=("#b0b0b0", "#2a2a2a"), command=self._pick_folder).pack(side="left", padx=(6, 0))

        badge_row = ctk.CTkFrame(self, fg_color=BLACK); badge_row.pack(fill="x", padx=24, pady=(2, 2))
        self.badge = ctk.CTkLabel(badge_row, text="—", fg_color=CARD2, corner_radius=6,
                                   text_color=WHITE, font=ctk.CTkFont(size=12, weight="bold"),
                                   width=72, height=24)
        self.badge.pack(side="left")
        self.type_var = tk.StringVar(value="No file selected.")
        ctk.CTkLabel(badge_row, textvariable=self.type_var, text_color=MUTED,
                      wraplength=540, justify="left").pack(side="left", padx=(10, 0))

        # The conversion chain — nodes with transition buttons between them.
        self.pipe = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=10)
        self.pipe.pack(fill="x", padx=24, pady=(14, 6))

        self.note_var = tk.StringVar(value="")
        ctk.CTkLabel(self, textvariable=self.note_var, text_color=MUTED,
                      font=ctk.CTkFont(size=11), wraplength=632, justify="left").pack(anchor="w", padx=24, pady=(2, 0))

        ctk.CTkButton(self, text="Close", fg_color=CARD2, text_color=WHITE,
                       hover_color=("#b0b0b0", "#2a2a2a"), command=self.destroy
                       ).pack(side="right", padx=24, pady=16)

        self.src_var.trace_add("write", lambda *_: self._refresh())
        self._refresh()

    def _pick_file(self):
        p = filedialog.askopenfilename(title="Select a .ffpfsc / .ffpfs image",
                                       filetypes=[("PFS images", "*.ffpfsc *.ffpfs"), ("All files", "*.*")])
        if p:
            self.src_var.set(p)

    def _pick_folder(self):
        p = filedialog.askdirectory(title="Select a folder of .ffpfsc / .ffpfs images")
        if p:
            self.src_var.set(p)

    def _node(self, text, current=False):
        box = ctk.CTkFrame(self.pipe, fg_color=CARD, corner_radius=8,
                            border_width=2 if current else 1,
                            border_color=GREEN if current else BORDER2)
        box.pack(side="left", padx=4, pady=14)
        ctk.CTkLabel(box, text=text, text_color=(WHITE if current else MUTED),
                      font=ctk.CTkFont(size=14, weight="bold")).pack(padx=14, pady=(8, 0))
        ctk.CTkLabel(box, text=("you have this" if current else "result"),
                      text_color=(GREEN if current else MUTED),
                      font=ctk.CTkFont(size=10)).pack(padx=14, pady=(0, 8))

    def _transition(self, label, action, desc):
        cell = ctk.CTkFrame(self.pipe, fg_color=PANEL)
        cell.pack(side="left", padx=2, pady=8)
        ctk.CTkLabel(cell, text="→", text_color=MUTED, font=ctk.CTkFont(size=20)).pack()
        ctk.CTkButton(cell, text=label, fg_color=GREEN, hover_color=GREEN2, text_color="#061006",
                       width=118, height=30, font=ctk.CTkFont(size=12, weight="bold"),
                       command=lambda a=action: self._go(a)).pack(pady=(2, 2))
        ctk.CTkLabel(cell, text=desc, text_color=MUTED, font=ctk.CTkFont(size=10),
                      wraplength=128, justify="center").pack()

    def _refresh(self):
        for w in self.pipe.winfo_children():
            w.destroy()
        self.note_var.set("")
        raw = (self.src_var.get() or "").strip()
        p = Path(raw) if raw else None
        if not p or not p.exists():
            self.badge.configure(text="—", fg_color=CARD2)
            self.type_var.set("No file selected.")
            ctk.CTkLabel(self.pipe, text="Pick a .ffpfsc / .ffpfs file or a folder above to see the steps.",
                          text_color=MUTED).pack(padx=16, pady=24)
            return
        if p.is_dir():
            self.badge.configure(text="FOLDER", fg_color=CARD2)
            self.type_var.set(f"“{p.name}” — a folder; batch-unpack every image inside.")
            self._node("📁 Images", current=True)
            self._transition("Unpack all", "batch", "each image → its own folder")
            self._node("Folders")
            self.note_var.set("Every .ffpfs / .ffpfsc found in the folder is unpacked to its own folder.")
        elif p.suffix.lower() == ".ffpfsc":
            self.badge.configure(text=".ffpfsc", fg_color=GREEN)
            self.type_var.set(f"“{p.name}” — compressed image.")
            self._node(".ffpfsc", current=True)
            self._transition("Decompress", "decompress", "→ inner .ffpfs (fast, one level)")
            self._node(".ffpfs")
            self._transition("Unpack", "folder", "→ game files")
            self._node("Folder")
            self.note_var.set("Decompress gives the uncompressed .ffpfs (faster to mount, full size). "
                              "Unpack extracts the game files into a folder.")
        elif p.suffix.lower() == ".ffpfs":
            self.badge.configure(text=".ffpfs", fg_color=CARD2)
            self.type_var.set(f"“{p.name}” — uncompressed image.")
            self._node(".ffpfs", current=True)
            self._transition("Unpack", "folder", "→ game files")
            self._node("Folder")
            self.note_var.set("To COMPRESS this .ffpfs into a .ffpfsc instead, add it as a source in the "
                              "main window and use the Output control (Compressed).")
        else:
            self.badge.configure(text="?", fg_color=CARD2)
            self.type_var.set("Not a .ffpfsc / .ffpfs image.")
            ctk.CTkLabel(self.pipe, text="Unsupported file type.", text_color=MUTED).pack(padx=16, pady=24)

    def _go(self, action):
        raw = (self.src_var.get() or "").strip()
        if not raw or not Path(raw).exists():
            messagebox.showerror("Nothing selected", "Pick a .ffpfsc / .ffpfs file or a folder of images.")
            return
        self.app._queue_conversion(Path(raw), action)
        self.destroy()


# ─── Main Application ──────────────────────────────────────────────────────────

class App:
    def __init__(self, root):
        self.root = root
        # Capture first-run BEFORE _setup() seeds settings.json — is_first_run() is a
        # file-existence check, so once _setup writes the file it would always be False
        # and the wizard would never appear for a genuinely new user.
        self._is_first_run = is_first_run()
        self.queue = []
        # Gate queue persistence until the saved queue is restored, so the initial
        # empty render doesn't overwrite the saved queue before we load it.
        self._queue_restored = False
        self.current_process = None
        self.cancel_requested = False
        self.extract_cancel_event = threading.Event()
        self.pending_start = False
        self.worker = None
        self._last_cmd_str = ""
        self._theme = "dark"
        # Batch auto-advance tracking (Feature 4)
        self._batch_total   = 0
        self._batch_done    = 0
        self._batch_failed  = 0
        self._batch_running = False
        self._details_item  = None   # GameItem currently shown in the details panel
        self._settings_win  = None
        self._active_item   = None   # item handed to the current worker (cleanup fallback)
        # Reclaim accounting: the batch must not re-check free space until all in-flight
        # cleanup rmtrees finish, or it reads stale (still-full) space and false-skips.
        self._cleanup_inflight = 0
        self._cleanup_lock     = threading.Lock()
        self._cleanup_wait_ticks = 0

        self.log_q      = queue.Queue()
        self.progress_q = queue.Queue()
        self.status_q   = queue.Queue()
        self.done_q     = queue.Queue()
        self.scan_q     = queue.Queue()
        self._extract_q = queue.Queue()   # archive extraction completion
        self.visible_log_lines = 0
        self.auto_scroll_logs = True

        self._setup()
        self._build()
        self._restore_queue()   # rebuild the saved queue now that the listbox exists
        self._poll()
        self._start_keep_awake()   # keep external HDDs from sleeping (if enabled)

        if self._is_first_run:
            self.root.after(200, self._show_first_run_wizard)

        # After the UI is fully loaded, remind user to report any untested games
        self.root.after(4000, self._check_pending_compat_reports)
        # Restore the user's saved section widths once the layout has settled.
        self.root.after(600, self._restore_sashes)
        # Offer to reclaim leftover scratch from a crashed/cancelled previous run.
        self.root.after(1500, self._offer_startup_sweep)

    def _restore_window_geometry(self):
        """Reapply the last saved window position/size, if valid. Vertically clamp so
        the title bar can never restore above the screen (e.g. hidden under the macOS
        menu bar) or absurdly off-screen. Horizontal position is left intact so an
        external-monitor placement is respected."""
        geo = (getattr(self, "_saved_window_geometry", "") or "").strip()
        m = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", geo)
        if m:
            try:
                w, h, x, y = (int(m.group(i)) for i in range(1, 5))
                if abs(x) > 20000 or abs(y) > 20000:
                    return  # corrupt coordinates — let the default geometry stand
                sh = self.root.winfo_screenheight()
                y = max(0, min(y, max(0, sh - 80)))   # keep the title bar grabbable
                self.root.geometry(f"{w}x{h}+{x}+{y}")
            except Exception:
                pass
        elif re.fullmatch(r"\d+x\d+", geo):
            try:
                self.root.geometry(geo)
            except Exception:
                pass

    def _on_window_configure(self, event):
        """Debounced: persist the window geometry shortly after a move/resize so
        the position survives any quit path (close button, Cmd-Q, …)."""
        if event.widget is not self.root:
            return
        if getattr(self, "_geo_save_after", None):
            try:
                self.root.after_cancel(self._geo_save_after)
            except Exception:
                pass
        self._geo_save_after = self.root.after(800, self._save_window_geometry)

    def _save_window_geometry(self):
        self._geo_save_after = None
        try:
            save_settings({"window_geometry": self.root.geometry()})
        except Exception:
            pass

    def _save_sashes(self):
        """Persist the divider positions: the horizontal content dividers (queue |
        progress | details widths, sash_h) AND the vertical content↔log divider (log
        height, sash_v), so the layout the user set sticks across launches. Called on
        every divider release."""
        h = getattr(self, "_paned_h", None)
        if h:
            try:
                xs = [h.sash_coord(0)[0], h.sash_coord(1)[0]]
                if all(isinstance(x, int) and x > 0 for x in xs):
                    save_settings({"sash_h": xs})
            except Exception:
                pass
        v = getattr(self, "_paned_v", None)
        if v:
            try:
                y = v.sash_coord(0)[1]
                if isinstance(y, int) and y > 0:
                    save_settings({"sash_v": y})
            except Exception:
                pass

    def _restore_sashes(self):
        """Reapply the saved divider positions: the 3-section content widths (sash_h)
        and the content↔log split (sash_v). On first run (or invalid data) fall back to
        sensible proportions of the current size; retry briefly if the layout has not
        settled yet."""
        h = getattr(self, "_paned_h", None)
        v = getattr(self, "_paned_v", None)
        if not h and not v:
            return
        try:
            self.root.update_idletasks()
            W = h.winfo_width() if h else 0
            if h and W < 200:
                # Layout not settled yet — retry a bounded number of times, then give up
                # (a degenerate <200px width must not arm a perpetual 300 ms timer).
                self._sash_restore_tries = getattr(self, "_sash_restore_tries", 0) + 1
                if self._sash_restore_tries <= 12:
                    self.root.after(300, self._restore_sashes)
                return
            if h:
                sh = load_settings().get("sash_h")
                if not (isinstance(sh, list) and len(sh) >= 2
                        and all(isinstance(x, int) for x in sh)):
                    sh = [int(W * 0.30), int(W * 0.63)]
                # Clamp into the visible range and keep the two dividers apart.
                a = max(120, min(int(sh[0]), W - 240))
                b = max(a + 120, min(int(sh[1]), W - 120))
                h.sash_place(0, a, 1)
                h.sash_place(1, b, 1)
            if v:
                H = v.winfo_height()
                if H >= 200:
                    sv = load_settings().get("sash_v")
                    if not isinstance(sv, int):
                        sv = int(H * 0.62)          # ~content 62% / log 38% by default
                    # Keep both panes usable: content ≥160 px tall, log ≥140 px.
                    y = max(160, min(int(sv), H - 140))
                    v.sash_place(0, 1, y)
        except Exception:
            pass

    def _on_close(self):
        # Stop the keep-awake pinger so it can't touch a drive mid-teardown.
        try:
            if getattr(self, "_keepawake_stop", None):
                self._keepawake_stop.set()
        except Exception:
            pass
        # If a job is running, confirm and stop it (kill the whole backend tree)
        # before quitting — otherwise the backend + mkpfs Pool keep running headless.
        try:
            if self.current_process and self.current_process.poll() is None:
                if not messagebox.askyesno(
                    "Quit?", "A job is still running.\n\nQuit and stop it?"
                ):
                    return
                self.cancel_requested = True
                self.extract_cancel_event.set()
                _kill_process_tree(self.current_process)
        except Exception:
            pass
        self._save_window_geometry()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _persisted_bool(self, settings, key, default):
        """A BooleanVar that loads from settings.json and auto-saves on every
        change, so the option sticks regardless of which checkbox toggles it."""
        v = tk.BooleanVar(value=bool(settings.get(key, default)))
        v.trace_add("write", lambda *_: save_settings({key: v.get()}))
        return v

    def _setup(self):
        settings = load_settings()
        self._theme = settings.get("appearance_mode", "dark")
        ctk.set_appearance_mode(self._theme)
        ctk.set_default_color_theme("green")
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("1400x960")
        self.root.minsize(1100, 760)

        self._saved_output = settings.get("output_folder", "")
        self._saved_temp = settings.get("temp_folder", "")
        self._saved_source = settings.get("source_path", "")
        self._saved_auto_clear_temp = settings.get("auto_clear_temp", False)
        def _safe_int(v, default):
            try:
                return int(v)
            except (TypeError, ValueError):
                return default
        # Coerce defensively: a hand-edited / corrupt non-numeric value here would make
        # tk.IntVar(value=...) raise TclError and hard-crash the GUI at startup.
        self._saved_compression_level = _safe_int(settings.get("compression_level", 7), 7)
        self._saved_cpu_count = _safe_int(settings.get("cpu_count", 0), 0)
        self._saved_block_size = settings.get("block_size", "auto")
        # Migrate console-incompatible block sizes: "auto-fit" (picks 4 KiB for many-file
        # games) and explicit sub-64K values build images the PS5 misreads → crash. The
        # native size is 64 KiB; normalise to "auto" (= 65536). The backend enforces this
        # too, but fixing the saved value keeps the UI honest.
        if self._saved_block_size in ("auto-fit", "4096", "8192", "16384", "32768"):
            self._saved_block_size = "auto"
            save_settings({"block_size": "auto"})
        # Global, auto-tried archive password list. Seed once with the most
        # common PS5-scene password so it never has to be typed again. `is None`
        # (not falsiness) so an intentionally-emptied list is not re-seeded.
        self._saved_passwords = settings.get("archive_passwords")
        if self._saved_passwords is None:
            self._saved_passwords = ["DLPSGAME.COM"]
            save_settings({"archive_passwords": self._saved_passwords})
        # Folder bundles: copy DLC/extra files next to the .ffpfsc (default on).
        self._saved_copy_siblings = settings.get("copy_bundle_siblings", True)
        # Remember the window position/size across launches.
        self._saved_window_geometry = settings.get("window_geometry", "")
        self._geo_save_after = None
        self._restore_window_geometry()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Configure>", self._on_window_configure, add="+")

    def _show_first_run_wizard(self):
        wiz = FirstRunWizard(self.root)
        self.root.wait_window(wiz)
        if wiz.result.get("temp_folder"):
            self.temp_var.set(wiz.result["temp_folder"])
        if wiz.result.get("output_folder"):
            self.output_var.set(wiz.result["output_folder"])

    def panel(self, parent, **grid):
        frame = ctk.CTkFrame(parent, fg_color=PANEL, border_width=1, border_color=BORDER, corner_radius=10)
        frame.grid(**grid)
        return frame

    def _bind_dynamic_wrap(self, owner, labels, *, padding=28, min_width=180):
        """Keep long labels from forcing a pane wider than the user wants."""
        def _update(event):
            wrap = max(min_width, event.width - padding)
            for label in labels:
                try:
                    label.configure(wraplength=wrap)
                except Exception:
                    pass

        owner.bind("<Configure>", _update, add="+")

    def _button(self, parent, text, command=None, green=False, red=False, yellow=False, **kw):
        if green:
            color, hover, txt = GREEN, GREEN2, "#061006"
        elif red:
            color, hover, txt = RED, ("#b91c1c", "#5a1a1a"), WHITE
        elif yellow:
            color, hover, txt = YELLOW, ("#a37c10", "#c9a00e"), "#061006"
        else:
            # Normal button: CARD2 fill, slightly darker hover in both modes
            color, hover, txt = CARD2, ("#b0b0b0", "#2a2a2a"), WHITE
        return ctk.CTkButton(parent, text=text, command=command, fg_color=color, hover_color=hover,
                              text_color=txt, border_width=0 if (green or red or yellow) else 1,
                              border_color=BORDER2, **kw)

    def _build(self):
        self.root.configure(fg_color=BLACK)
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        main = ctk.CTkFrame(self.root, fg_color=BLACK, corner_radius=0)
        main.grid(row=0, column=0, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        # Row 2 holds a VERTICAL paned window with the content area (top) and the log/tabs
        # area (bottom) — the user drags the horizontal divider to size the log.
        main.grid_rowconfigure(2, weight=1)

        # ── Header ──────────────────────────────────────────────────────────
        header = ctk.CTkFrame(main, fg_color=BLACK)
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 8))
        header.grid_columnconfigure(1, weight=1)

        title_box = ctk.CTkFrame(header, fg_color=BLACK)
        title_box.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(title_box, text="PS5 FFPFSC PRO",
                      font=ctk.CTkFont(size=26, weight="bold"), text_color=WHITE).pack(anchor="w")
        ctk.CTkLabel(title_box, text=f"v{APP_VERSION}  ·  Bizkut backend  ·  {MKPFS_NAME} v{MKPFS_VERSION}  ·  by Knutwurst",
                      text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", padx=2)

        # Kept for live stage writes (the stage itself is shown in the progress panel and
        # the bottom status bar, so it is no longer crammed into the header).
        self.header_status_var = tk.StringVar(value=f"v{APP_VERSION}  |  Backend: Ready")

        # START / CANCEL — primary actions, top-right.
        self.start_btn = self._button(header, "▶  START QUEUE", self.start, green=True, width=160, height=38)
        self.start_btn.grid(row=0, column=2, padx=(8, 6), sticky="e")
        self.cancel_btn = self._button(header, "✕  CANCEL", self.cancel, red=True, width=110, height=38)
        self.cancel_btn.grid(row=0, column=3, padx=(0, 2), sticky="e")
        self.cancel_btn.configure(state="disabled")

        # One calm toolbar row holds the format control + tools (populated once the
        # output_compressed_var exists — see the Variables section below).
        self._toolbar = ctk.CTkFrame(header, fg_color=BLACK)
        self._toolbar.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(10, 0))

        # ── Variables ────────────────────────────────────────────────────────
        # Remember the last input path across restarts (a browsed folder stays in the
        # field). Only restore it if it still exists, so a moved/deleted path doesn't
        # linger. Picking anything new overwrites it via the trace below.
        _src0 = self._saved_source if (self._saved_source and Path(self._saved_source).exists()) else ""
        self.source_var = tk.StringVar(value=_src0)
        self.source_var.trace_add("write", lambda *_: save_settings({"source_path": self.source_var.get()}))
        self.output_var = tk.StringVar(value=self._saved_output)
        self.temp_var = tk.StringVar(value=self._saved_temp)
        # Persist typed/browsed paths so they survive a restart (previously only
        # browsed paths that happened to be saved elsewhere stuck).
        self.output_var.trace_add("write", lambda *_: save_settings({"output_folder": self.output_var.get()}))
        self.temp_var.trace_add("write", lambda *_: save_settings({"temp_folder": self.temp_var.get()}))
        # Probe (in the background) whether the temp/output drives are SSD or HDD so the
        # runtime placement labels can be honest WITHOUT blocking the UI thread on
        # diskutil/PowerShell. Re-probes when the folders change; cached per device.
        self.temp_var.trace_add("write", lambda *_: self._warm_drive_types())
        self.output_var.trace_add("write", lambda *_: self._warm_drive_types())
        self.source_var.trace_add("write", lambda *_: self._warm_drive_types())
        self._warm_drive_types()
        self.password_var = tk.StringVar()
        # Live copy of the global auto-tried password list (backs the settings editor).
        self.archive_passwords: list[str] = list(getattr(self, "_saved_passwords", ["DLPSGAME.COM"]))
        # Extra temp/scratch drives (a "pool" added to the primary temp field). When more
        # than one fast drive is available the router can keep pass 1 SSD↔SSD for big
        # archive games by extracting the source to one and building the image on another.
        self.temp_pool: list[str] = [str(p) for p in (load_settings().get("temp_pool", []) or []) if str(p).strip()]
        self.copy_siblings_var = tk.BooleanVar(value=getattr(self, "_saved_copy_siblings", True))
        settings = load_settings()   # _build() is a separate method from _setup()
        self.keep_pfs_var        = self._persisted_bool(settings, "keep_pfs", False)
        self.open_output_var     = self._persisted_bool(settings, "open_output", False)
        self.summary_popup_var   = self._persisted_bool(settings, "summary_popup", True)
        self.compat_prompt_var   = self._persisted_bool(settings, "compat_prompt", True)
        self.sound_complete_var  = self._persisted_bool(settings, "sound_complete", True)
        self.sound_error_var     = self._persisted_bool(settings, "sound_error", True)
        self.batch_var = tk.BooleanVar(value=False)        # session state — not persisted
        self.unpack_mode_var = tk.BooleanVar(value=False)  # session state — not persisted
        # Output format for the whole queue: compressed .ffpfsc (smaller) vs uncompressed
        # .ffpfs (faster to build AND to mount — ShadowMountPlus decompresses .ffpfsc at only
        # ~150-250 MB/s and streaming-heavy games can stutter). True = compressed (default).
        self.output_compressed_var = self._persisted_bool(settings, "output_compressed", True)
        # AMPR/APR emu folder (PlayGo titles): holds libSceAmpr.sprx + libScePlayGo.sprx.
        self.ampr_var = tk.StringVar(value=settings.get("ampr_folder", ""))
        # Toolbar (header row 2): a slide switch sets the Output format for the WHOLE queue
        # (ON = compressed .ffpfsc, OFF = uncompressed .ffpfs); the label states which.
        ctk.CTkLabel(self._toolbar, text="Output", text_color=MUTED,
                      font=ctk.CTkFont(size=12)).pack(side="left", padx=(2, 8))
        ctk.CTkSwitch(self._toolbar, text="", width=46, variable=self.output_compressed_var,
                       onvalue=True, offvalue=False, progress_color=GREEN,
                       command=self._on_format_toggle).pack(side="left")
        self.format_hint_var = tk.StringVar()
        ctk.CTkLabel(self._toolbar, textvariable=self.format_hint_var, text_color=WHITE,
                      font=ctk.CTkFont(size=13, weight="bold")).pack(side="left", padx=(8, 0))
        # Tools, grouped on the right.
        self._button(self._toolbar, "⚙  Settings", self.open_settings, width=110).pack(side="right")
        self._button(self._toolbar, "☀ / 🌙  Theme", self._toggle_theme, width=110).pack(side="right", padx=(0, 8))
        self._button(self._toolbar, "🩹  Integrate Patch", self._open_patch_dialog, width=160).pack(side="right", padx=(0, 8))
        self._button(self._toolbar, "🔄  Converter", self.open_converter, green=True, width=130).pack(side="right", padx=(0, 8))
        self._update_format_label()
        self.verify_output_var   = self._persisted_bool(settings, "verify_output", False)
        self.auto_clear_temp_var = self._persisted_bool(settings, "auto_clear_temp", False)
        # Auto-patch: when a release folder holds a base game plus a clearly-smaller
        # game-like sibling (a patch), overlay it onto the game before packing. Off
        # by default — opt in knowingly, since it changes what lands in the .ffpfsc.
        self.auto_integrate_patch_var = self._persisted_bool(settings, "auto_integrate_patch", False)
        # Drive-usage mode: auto | temp | spread (where archives get extracted).
        self.drive_mode_var = tk.StringVar(value=settings.get("drive_mode", "auto"))
        self.drive_mode_var.trace_add("write", lambda *_: save_settings({"drive_mode": self.drive_mode_var.get()}))
        # Show the drive-space pre-flight dialog before each pack (default on). The
        # tunable safety factor and the low-space policy live in settings.json and are
        # edited in the Settings window (see SettingsWindow).
        self.show_space_dialog_var = self._persisted_bool(settings, "show_space_dialog", True)
        # Opt-in: build an exFAT intermediate and compress that (PSBrew's most-stable
        # exfat->ffpfsc path) instead of the folder PFS builder. macOS only. Default off.
        self.build_via_exfat_var = self._persisted_bool(settings, "build_via_exfat", False)
        # Keep external drives awake DURING A RUN only: a fast tiny flushed write so
        # bus-powered 2.5" USB HDDs (WD Elements) stay spun-up with heads LOADED across the
        # short gaps between games in a batch — so each game doesn't pay a fresh spinup. The
        # interval ('keep_awake_interval', default 8 s) is deliberately under WD IntelliPark's
        # 8 s park timer: a SLOWER ping would just unpark→re-park every cycle and ADD load
        # cycles. When no job runs we ping nothing and let the drive fully sleep (its lowest-
        # wear state). Default off; toggle + interval live in Settings → Drive & Space.
        self.keep_drives_awake_var = self._persisted_bool(settings, "keep_drives_awake", False)
        # MkPFS 0.0.8 tuning
        self.compression_level_var = tk.IntVar(value=self._saved_compression_level)
        self.cpu_count_var         = tk.IntVar(value=self._saved_cpu_count)
        self.verbose_var           = self._persisted_bool(settings, "verbose", False)
        self.block_size_var        = tk.StringVar(value=self._saved_block_size)
        # Persist the main-window tuning controls on change (previously only the
        # Settings window saved these, so edits made in the main window were lost).
        self.compression_level_var.trace_add("write", lambda *_: save_settings({"compression_level": self.compression_level_var.get()}))
        self.cpu_count_var.trace_add("write", lambda *_: save_settings({"cpu_count": self.cpu_count_var.get()}))
        self.block_size_var.trace_add("write", lambda *_: save_settings({"block_size": self.block_size_var.get()}))

        # ── Top folder row ───────────────────────────────────────────────────
        top = self.panel(main, row=1, column=0, sticky="ew", padx=18, pady=8)
        top.grid_columnconfigure(1, weight=1)
        top.grid_columnconfigure(3, weight=1)
        top.grid_columnconfigure(5, weight=1)

        add_btns = ctk.CTkFrame(top, fg_color="transparent")
        add_btns.grid(row=0, column=0, padx=(14, 8), pady=14)
        self._button(add_btns, "📁  FOLDER",  self.browse_source_folder,  green=True, width=120).pack(pady=(0, 4))
        self._button(add_btns, "📦  ARCHIVE", self.browse_source_archive,             width=120).pack(pady=(0, 4))
        self._button(add_btns, "📤  PFS",     self.browse_pfs_image,                 width=120).pack()
        ctk.CTkEntry(top, textvariable=self.source_var,
                      placeholder_text="Game folder, archive, disk image (.exfat/.ffpkg), or PFS image (.ffpfs/.ffpfsc)…",
                      fg_color=CARD, border_color=BORDER2, text_color=WHITE).grid(
            row=0, column=1, sticky="ew", padx=(0, 16), pady=14)

        self._button(top, "OUTPUT", self.browse_output_folder, width=100).grid(row=0, column=2, padx=(0, 8), pady=14)
        ctk.CTkEntry(top, textvariable=self.output_var, placeholder_text="Output folder...",
                      fg_color=CARD, border_color=BORDER2, text_color=WHITE).grid(row=0, column=3, sticky="ew", padx=(0, 16), pady=14)

        self._button(top, "TEMP", self.browse_temp_folder, width=90).grid(row=0, column=4, padx=(0, 8), pady=14)
        ctk.CTkEntry(top, textvariable=self.temp_var, placeholder_text="Temp folder on fast drive...",
                      fg_color=CARD, border_color=BORDER2, text_color=WHITE).grid(row=0, column=5, sticky="ew", padx=(0, 14), pady=14)

        # ── Vertical split: content area (top) ↕ log/tabs area (bottom). A horizontal
        #    divider the user drags up/down to grow or shrink the log; its position
        #    persists as sash_v (see _save_sashes/_restore_sashes).
        vpaned = tk.PanedWindow(main, orient="vertical", bg="#2a2a2a", sashwidth=8,
                                sashrelief="flat", bd=0, opaqueresize=True)
        vpaned.grid(row=2, column=0, sticky="nsew", padx=18, pady=(6, 14))
        self._paned_v = vpaned
        vpaned.bind("<ButtonRelease-1>", lambda e: self._save_sashes(), add="+")

        # ── Content area: 3 user-resizable sections (queue | progress | details).
        #    A horizontal paned window lets the user drag the two dividers; the sash
        #    positions are persisted across launches (see _save_sashes/_restore_sashes).
        content = tk.PanedWindow(vpaned, orient="horizontal", bg="#2a2a2a", sashwidth=8,
                                 sashrelief="flat", bd=0, opaqueresize=True)
        vpaned.add(content, minsize=200, stretch="always")
        self._paned_h = content
        content.bind("<ButtonRelease-1>", lambda e: self._save_sashes(), add="+")

        # ── Left: Queue + Options ────────────────────────────────────────────
        left = ctk.CTkFrame(content, fg_color=PANEL, border_width=1, border_color=BORDER, corner_radius=10)
        content.add(left, minsize=300, stretch="always")
        left.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(left, text="QUEUE", font=ctk.CTkFont(size=16, weight="bold"),
                      text_color=WHITE).grid(row=0, column=0, sticky="w", padx=14, pady=(14, 4))

        # Queue listbox — tk.Listbox for native single-row selection
        lb_frame = ctk.CTkFrame(left, fg_color=CARD, corner_radius=6,
                                 border_width=1, border_color=BORDER)
        lb_frame.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 0))
        lb_frame.grid_columnconfigure(0, weight=1)

        _lb_bg   = "#111111"
        _lb_fg   = "#e8e8e8"
        _lb_sel  = "#1a5c2e"
        _lb_muted = "#888888"
        self.queue_listbox = tk.Listbox(
            lb_frame,
            bg=_lb_bg, fg=_lb_fg,
            selectbackground=_lb_sel, selectforeground="#ffffff",
            font=("Consolas", 11),
            borderwidth=0, highlightthickness=0,
            activestyle="none", relief="flat",
            height=7,
            exportselection=False,
        )
        lb_scrollbar = tk.Scrollbar(lb_frame, orient="vertical",
                                     command=self.queue_listbox.yview)
        self.queue_listbox.configure(yscrollcommand=lb_scrollbar.set)
        self.queue_listbox.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        lb_scrollbar.grid(row=0, column=1, sticky="ns", pady=4, padx=(0, 2))
        # Clicking a row refreshes game details; arrow keys reorder
        self.queue_listbox.bind("<<ListboxSelect>>", self._on_queue_select)
        self.queue_listbox.bind("<Up>",   self._lb_key_up)
        self.queue_listbox.bind("<Down>", self._lb_key_down)

        # Everything below the queue list goes into a SCROLLABLE body so it never gets
        # clipped when the pane is short (the OPTIONS block used to fall off the bottom).
        # The queue Listbox stays OUTSIDE this scroll area (row 1) on purpose: ctk only
        # grabs the mouse-wheel inside its OWN canvas subtree, so a listbox that is not a
        # descendant keeps its own wheel/scrollbar and the outer scroll can't fight it.
        left.grid_rowconfigure(2, weight=1)
        # Plain frame (no scrollbar): the left pane below the queue list now holds only the
        # queue buttons + a couple of hint lines, so it always fits — the old scrollable
        # body + its scrollbar just added visual noise.
        body = ctk.CTkFrame(left, fg_color=PANEL, corner_radius=0)
        body.grid(row=2, column=0, sticky="nsew", padx=0, pady=(2, 0))
        body.grid_columnconfigure(0, weight=1)

        # Queue action buttons — row 0: scan/add, row 1: reorder/remove/clear
        qbtns = ctk.CTkFrame(body, fg_color=PANEL)
        qbtns.grid(row=0, column=0, sticky="ew", padx=14, pady=(4, 10))
        qbtns.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self._button(qbtns, "SCAN / ADD", self.add_source_to_queue, green=True).grid(
            row=0, column=0, columnspan=4, sticky="ew", padx=4, pady=(4, 2))

        self._button(qbtns, "↑",         self.queue_move_up,         width=40).grid(
            row=1, column=0, sticky="ew", padx=(4, 2), pady=(2, 4))
        self._button(qbtns, "↓",         self.queue_move_down,       width=40).grid(
            row=1, column=1, sticky="ew", padx=2, pady=(2, 4))
        self._button(qbtns, "✕ REMOVE",  self.queue_remove_selected).grid(
            row=1, column=2, sticky="ew", padx=2, pady=(2, 4))
        self._button(qbtns, "🗑 CLEAR",  self.clear_queue, red=True).grid(
            row=1, column=3, sticky="ew", padx=(2, 4), pady=(2, 4))

        # ── Total / batch counter / drag-drop hint ───────────────────────────
        self.queue_total_var = tk.StringVar(value="Total: 0 game(s)")
        ctk.CTkLabel(body, textvariable=self.queue_total_var,
                      text_color=MUTED).grid(row=1, column=0, sticky="w", padx=14, pady=(8, 0))

        self.batch_counter_var = tk.StringVar(value="")
        self.batch_counter_label = ctk.CTkLabel(
            body, textvariable=self.batch_counter_var,
            text_color=YELLOW, font=ctk.CTkFont(size=12, weight="bold")
        )
        self.batch_counter_label.grid(row=2, column=0, sticky="w", padx=14, pady=(0, 0))

        if _HAS_DND:
            ctk.CTkLabel(body, text="↓ Drag & drop supported", text_color=MUTED,
                          font=ctk.CTkFont(size=11)).grid(row=3, column=0, sticky="w", padx=14, pady=(2, 0))

        # ── Mode ──────────────────────────────────────────────────────────────
        # Everything persistent (passwords, sounds, verify, keep-PFS, auto-clear,
        # auto-patch, drive usage, verbose, output/temp/compression…) lives in the
        # ⚙ Settings window — including the auto-tried archive password list, so the old
        # per-job password field here was redundant and just took up space (password_var
        # stays as a fallback override, still editable in Settings). Unpacking/decompressing
        # is via the 🔄 Converter (top); the old "Unpack PFS images" checkbox is gone.
        ctk.CTkLabel(body, text="To unpack / decompress an image, use  🔄 Converter (top).\n"
                                "Archive passwords & all other options live in  ⚙ Settings (top-right).",
                      text_color=MUTED, font=ctk.CTkFont(size=11), justify="left").grid(
                          row=4, column=0, sticky="w", padx=14, pady=(4, 2))

        # ── Center: Progress + Stages ────────────────────────────────────────
        center = ctk.CTkFrame(content, fg_color=BLACK)
        content.add(center, minsize=360, stretch="always")
        center.grid_columnconfigure(0, weight=1)
        center.grid_rowconfigure(0, weight=0)
        center.grid_rowconfigure(1, weight=0)

        progress = self.panel(center, row=0, column=0, sticky="ew", pady=(0, 8))
        progress.grid_columnconfigure(0, weight=1)
        self.overall_title_var = tk.StringVar(value="QUEUE")
        ctk.CTkLabel(progress, textvariable=self.overall_title_var, text_color=WHITE,
                      font=ctk.CTkFont(size=15, weight="bold")).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 2))
        self.overall_pct_var = tk.StringVar(value="0%")
        ctk.CTkLabel(progress, textvariable=self.overall_pct_var, text_color=("#1a7a40", "#4ade80"),
                      font=ctk.CTkFont(size=24, weight="bold")).grid(row=0, column=1, sticky="e", padx=14)
        self.overall_bar = ctk.CTkProgressBar(progress, progress_color=GREEN, fg_color=("#cccccc", "#242424"), height=14)
        self.overall_bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 10))
        self.overall_bar.set(0)

        self.cur_game_var = tk.StringVar(value="CURRENT STEP")
        ctk.CTkLabel(progress, textvariable=self.cur_game_var, text_color=MUTED,
                      font=ctk.CTkFont(size=12, weight="bold")).grid(row=2, column=0, columnspan=2, sticky="w", padx=14)
        self.stage_title_var = tk.StringVar(value="Ready")
        self.stage_detail_var = tk.StringVar(value="Add a game and start queue.")
        self.stage_pct_var = tk.StringVar(value="0%")
        ctk.CTkLabel(progress, textvariable=self.stage_title_var, text_color=WHITE,
                      font=ctk.CTkFont(size=20, weight="bold")).grid(row=3, column=0, sticky="w", padx=14, pady=(3, 0))
        ctk.CTkLabel(progress, textvariable=self.stage_pct_var, text_color=("#1a7a40", "#4ade80"),
                      font=ctk.CTkFont(size=18, weight="bold")).grid(row=3, column=1, sticky="e", padx=14)
        self.stage_detail_label = ctk.CTkLabel(
            progress, textvariable=self.stage_detail_var, text_color=MUTED,
            wraplength=420, justify="left"
        )
        self.stage_detail_label.grid(row=4, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 6))
        self.stage_bar = ctk.CTkProgressBar(progress, progress_color=GREEN, fg_color=("#cccccc", "#242424"), height=12)
        self.stage_bar.grid(row=5, column=0, columnspan=2, sticky="ew", padx=14, pady=(0, 10))
        self.stage_bar.set(0)

        # Stages strip — one label per stage in a horizontal row
        stages_outer = ctk.CTkFrame(progress, fg_color=PANEL, corner_radius=6)
        stages_outer.grid(row=6, column=0, columnspan=2, sticky="ew", padx=14, pady=(4, 8))
        self._stage_labels = []
        for i, (_, short) in enumerate(_STAGE_DEFS):
            if i > 0:
                ctk.CTkLabel(stages_outer, text="›", text_color=MUTED,
                              font=ctk.CTkFont(size=13)).pack(side="left", padx=0)
            lbl = ctk.CTkLabel(stages_outer, text=f"○ {short}", text_color=MUTED,
                                font=ctk.CTkFont(size=10), width=62, anchor="center")
            lbl.pack(side="left", padx=2, pady=6)
            self._stage_labels.append(lbl)

        self._bind_dynamic_wrap(progress, [self.stage_detail_label], padding=36, min_width=260)

        # ── Compression tuning bar — sits directly under the stages strip ──
        tune_bar = ctk.CTkFrame(progress, fg_color=CARD, corner_radius=6,
                                 border_width=1, border_color=BORDER2)
        tune_bar.grid(row=7, column=0, columnspan=2, sticky="ew", padx=14, pady=(2, 12))
        tune_bar.grid_columnconfigure(1, weight=1)
        tune_bar.grid_columnconfigure(4, weight=1)

        ctk.CTkLabel(tune_bar, text="COMPRESSION TUNING",
                      text_color=WHITE, font=ctk.CTkFont(size=11, weight="bold"),
                      anchor="w").grid(row=0, column=0, columnspan=7, sticky="w",
                                       padx=10, pady=(7, 3))

        # ── Compression level ──
        ctk.CTkLabel(tune_bar, text="Level (0-9):", text_color=MUTED,
                      font=ctk.CTkFont(size=11), anchor="e").grid(
            row=1, column=0, sticky="e", padx=(10, 4), pady=(0, 8))
        ctk.CTkSlider(tune_bar, from_=0, to=9, number_of_steps=9,
                       variable=self.compression_level_var,
                       fg_color=BORDER2, progress_color=GREEN,
                       button_color=GREEN, button_hover_color=GREEN2,
                       height=16).grid(row=1, column=1, sticky="ew", padx=(0, 4), pady=(0, 8))
        self._comp_level_lbl = ctk.CTkLabel(tune_bar, text=str(self.compression_level_var.get()),
                                             text_color=GREEN, font=ctk.CTkFont(size=12, weight="bold"),
                                             width=22, anchor="w")
        self._comp_level_lbl.grid(row=1, column=2, padx=(0, 18), pady=(0, 8))
        def _update_comp_lbl(*_):
            self._comp_level_lbl.configure(text=str(self.compression_level_var.get()))
        self.compression_level_var.trace_add("write", _update_comp_lbl)

        # ── CPU cores ──
        ctk.CTkLabel(tune_bar, text="CPU cores (0=auto):", text_color=MUTED,
                      font=ctk.CTkFont(size=11), anchor="e").grid(
            row=1, column=3, sticky="e", padx=(0, 4), pady=(0, 8))
        ctk.CTkSlider(tune_bar, from_=0, to=16, number_of_steps=16,
                       variable=self.cpu_count_var,
                       fg_color=BORDER2, progress_color=GREEN,
                       button_color=GREEN, button_hover_color=GREEN2,
                       height=16).grid(row=1, column=4, sticky="ew", padx=(0, 4), pady=(0, 8))
        self._cpu_count_lbl = ctk.CTkLabel(tune_bar,
                                            text=("auto" if self.cpu_count_var.get() == 0
                                                  else str(self.cpu_count_var.get())),
                                            text_color=GREEN, font=ctk.CTkFont(size=12, weight="bold"),
                                            width=34, anchor="w")
        self._cpu_count_lbl.grid(row=1, column=5, padx=(0, 10), pady=(0, 8))
        def _update_cpu_lbl(*_):
            v = self.cpu_count_var.get()
            self._cpu_count_lbl.configure(text="auto" if v == 0 else str(v))
        self.cpu_count_var.trace_add("write", _update_cpu_lbl)

        # ── Block size ──  (new in MkPFS 0.0.7/0.0.8 — smaller = less waste for small files)
        ctk.CTkLabel(tune_bar, text="Block size:", text_color=MUTED,
                      font=ctk.CTkFont(size=11), anchor="e").grid(
            row=1, column=6, sticky="e", padx=(14, 4), pady=(0, 8))
        _block_opts = ["auto", "65536"]
        _block_menu = ctk.CTkOptionMenu(
            tune_bar, values=_block_opts, variable=self.block_size_var,
            fg_color=CARD2, button_color=GREEN, button_hover_color=GREEN2,
            text_color=WHITE, dropdown_fg_color=CARD2, dropdown_text_color=WHITE,
            dropdown_hover_color=GREEN, width=96, height=24,
            font=ctk.CTkFont(size=11),
            command=lambda v: save_settings({"block_size": v}),
        )
        _block_menu.grid(row=1, column=7, sticky="w", padx=(0, 10), pady=(0, 8))

        # ── Right: Game Details + Command Preview ────────────────────────────
        right = ctk.CTkFrame(content, fg_color=BLACK)
        content.add(right, minsize=300, stretch="always")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        details = self.panel(right, row=0, column=0, sticky="ew", pady=(0, 8))
        details.grid_columnconfigure(1, weight=1)

        self.art_frame = ctk.CTkFrame(details, width=100, height=110, fg_color=BLACK,
                                       border_width=1, border_color=BORDER2)
        self.art_frame.grid(row=0, column=0, rowspan=2, padx=10, pady=10)
        self.art_frame.grid_propagate(False)
        # Plain tk.Label — CTkLabel can't cleanly switch between image=CTkImage and image=None
        self.art_label = tk.Label(self.art_frame, text="NO\nART",
                                  fg="#888888", bg="#000000",
                                  font=("Segoe UI", 9),
                                  borderwidth=0, highlightthickness=0)
        self.art_label.pack(expand=True)

        ctk.CTkLabel(details, text="GAME DETAILS", text_color=WHITE,
                      font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=1, sticky="w", padx=4, pady=(10, 4))
        self.game_name_var = tk.StringVar(value="Name: No game selected")
        self.title_var = tk.StringVar(value="Title ID: —")
        self.source_detail_var = tk.StringVar(value="Source: —")
        self.orig_var = tk.StringVar(value="Original Size: —")
        self.files_var = tk.StringVar(value="Files: —")
        info = ctk.CTkFrame(details, fg_color=PANEL)
        info.grid(row=1, column=1, sticky="nsew", padx=(4, 10), pady=(0, 10))
        self._detail_value_labels = []
        for v in [self.game_name_var, self.title_var, self.orig_var, self.files_var, self.source_detail_var]:
            lbl = ctk.CTkLabel(
                info, textvariable=v, text_color=WHITE, anchor="w", justify="left",
                wraplength=520, font=ctk.CTkFont(size=11)
            )
            lbl.pack(anchor="w", pady=2, padx=6)
            self._detail_value_labels.append(lbl)
        self._bind_dynamic_wrap(info, self._detail_value_labels, padding=18, min_width=220)

        command = self.panel(right, row=1, column=0, sticky="nsew", pady=(0, 0))
        command.grid_columnconfigure(0, weight=1)
        command.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(command, text="COMMAND PREVIEW", text_color=WHITE,
                      font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 4))
        self.command_label = ctk.CTkLabel(command,
                                           text="Select source, output, and temp folder to preview command.",
                                           text_color=MUTED, wraplength=360, justify="left",
                                           font=ctk.CTkFont(size=11))
        self.command_label.grid(row=1, column=0, sticky="nw", padx=14, pady=(0, 10))
        self._bind_dynamic_wrap(command, [self.command_label], padding=36, min_width=260)

        # ── Bottom: Tabbed Logs / Status / History / Statistics ─────────────
        #    Lives in the vertical paned window as the lower, drag-resizable pane.
        bottom = ctk.CTkFrame(vpaned, fg_color=PANEL, border_width=1, border_color=BORDER, corner_radius=10)
        vpaned.add(bottom, minsize=160, stretch="never")
        bottom.grid_columnconfigure(0, weight=1)
        bottom.grid_rowconfigure(0, weight=1)

        self.bottom_tabs = ctk.CTkTabview(bottom, fg_color=BLACK, segmented_button_fg_color=PANEL,
                                           segmented_button_selected_color=GREEN,
                                           segmented_button_selected_hover_color=GREEN2,
                                           segmented_button_unselected_color=PANEL,
                                           text_color=WHITE)
        self.bottom_tabs.grid(row=0, column=0, sticky="nsew", padx=12, pady=(10, 10))

        self.bottom_tabs.add("Logs")
        self.bottom_tabs.add("Status & Stats")
        self.bottom_tabs.add("Recent Compressions")
        self.bottom_tabs.add("Statistics")
        self.bottom_tabs.add("Compatibility")

        # ── Status & Stats tab — 3 columns side by side ──────────────────────
        ss_tab = self.bottom_tabs.tab("Status & Stats")
        ss_tab.grid_columnconfigure(0, weight=2)   # STATUS
        ss_tab.grid_columnconfigure(1, weight=2)   # STATS
        ss_tab.grid_columnconfigure(2, weight=2)   # TOOLS
        ss_tab.grid_rowconfigure(0, weight=1)

        # ── STATUS (col 0) ────────────────────────────────────────────────────
        status = self.panel(ss_tab, row=0, column=0, sticky="nsew", padx=(0, 4), pady=4)
        ctk.CTkLabel(status, text="STATUS", text_color=WHITE,
                      font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=12, pady=(10, 4))
        self.big_status_var = tk.StringVar(value="Ready")
        self.big_detail_var = tk.StringVar(value="Waiting for a game.")
        ctk.CTkLabel(status, textvariable=self.big_status_var, text_color=WHITE,
                      font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", padx=12)
        ctk.CTkLabel(status, textvariable=self.big_detail_var, text_color=MUTED,
                      wraplength=320, justify="left",
                      font=ctk.CTkFont(size=11)).pack(anchor="w", padx=12, pady=(3, 10))

        # ── STATS (col 1) ─────────────────────────────────────────────────────
        stats = self.panel(ss_tab, row=0, column=1, sticky="nsew", padx=4, pady=4)
        ctk.CTkLabel(stats, text="STATS", text_color=WHITE,
                      font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=12, pady=(10, 4))
        self.speed_var       = tk.StringVar(value="Speed: —")
        self.elapsed_var     = tk.StringVar(value="Elapsed: 00:00")
        self.eta_var         = tk.StringVar(value="ETA: —")
        self.saved_var       = tk.StringVar(value="Saved: —")
        self.ratio_var       = tk.StringVar(value="Compression: —")
        self.rating_var      = tk.StringVar(value="Rating: —")
        self.temp_space_var  = tk.StringVar(value="Temp Needed: —")
        for v in [self.speed_var, self.elapsed_var, self.eta_var,
                  self.saved_var, self.ratio_var, self.rating_var, self.temp_space_var]:
            ctk.CTkLabel(stats, textvariable=v, text_color=WHITE, anchor="w",
                          font=ctk.CTkFont(size=11)).pack(anchor="w", padx=12, pady=2)
        # bottom padding
        ctk.CTkLabel(stats, text="", height=6).pack()

        # ── TOOLS (col 2) ─────────────────────────────────────────────────────
        tools_frame = self.panel(ss_tab, row=0, column=2, sticky="nsew", padx=(4, 0), pady=4)
        ctk.CTkLabel(tools_frame, text="TOOLS", text_color=WHITE,
                      font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=12, pady=(10, 6))
        self._button(tools_frame, "OPEN OUTPUT FOLDER",    self.open_output_folder,  height=34).pack(fill="x", padx=12, pady=3)
        self._button(tools_frame, "EXPORT RAW LOG",        self.open_raw_log,        height=34).pack(fill="x", padx=12, pady=3)
        self._button(tools_frame, "🗑  Clear Temp Files",  self.clear_temp_files,    height=34).pack(fill="x", padx=12, pady=3)
        self._button(tools_frame, "📦  Export Diagnostic", self.export_diagnostics,  height=34).pack(fill="x", padx=12, pady=3)
        self._button(tools_frame, "📋  Copy Last Result",  self.copy_last_result,    height=34).pack(fill="x", padx=12, pady=(3, 10))

        # Logs tab
        log_tab = self.bottom_tabs.tab("Logs")
        log_tab.grid_columnconfigure(0, weight=1)
        log_tab.grid_rowconfigure(0, weight=1)
        log_head = ctk.CTkFrame(log_tab, fg_color=BLACK)
        log_head.grid(row=0, column=0, sticky="ew")
        log_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(log_head, text="LOGS", text_color=WHITE,
                      font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, sticky="w")
        # Live RAM meter — green/amber/red; refreshed ~every 2 s from the poll loop.
        self.ram_var = tk.StringVar(value="")
        self._ram_label = ctk.CTkLabel(log_head, textvariable=self.ram_var, text_color=MUTED,
                                        font=ctk.CTkFont(size=12))
        self._ram_label.grid(row=0, column=1, padx=(0, 10), sticky="e")
        self._button(log_head, "CLEAR LOGS", self.clear_logs, width=110).grid(row=0, column=2, padx=4)
        self.log_box = ctk.CTkTextbox(log_tab, fg_color=BLACK, border_width=1, border_color=BORDER,
                                       text_color="#94a3b8", font=ctk.CTkFont(family="Consolas", size=12), wrap="none")
        self.log_box.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        log_tab.grid_rowconfigure(1, weight=1)
        # Per-level colour tags on the underlying tk.Text widget
        try:
            t = self.log_box._textbox
            t.tag_configure("SUCCESS",  foreground="#4ade80")
            t.tag_configure("OK",       foreground="#4ade80")
            t.tag_configure("ERROR",    foreground="#f87171")
            t.tag_configure("WARN",     foreground="#facc15")
            t.tag_configure("INFO",     foreground="#94a3b8")
            t.tag_configure("PROGRESS", foreground="#60a5fa")
            t.tag_configure("DEBUG",    foreground="#555555")
        except Exception:
            pass

        # History tab
        hist_tab = self.bottom_tabs.tab("Recent Compressions")
        hist_tab.grid_columnconfigure(0, weight=1)
        hist_tab.grid_rowconfigure(1, weight=1)
        hist_head = ctk.CTkFrame(hist_tab, fg_color=BLACK)
        hist_head.grid(row=0, column=0, sticky="ew")
        hist_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hist_head, text="RECENT COMPRESSIONS", text_color=WHITE,
                      font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, sticky="w")
        self._button(hist_head, "REFRESH", self.refresh_history, width=110).grid(row=0, column=1, padx=4)
        self.history_box = ctk.CTkTextbox(hist_tab, fg_color=BLACK, border_width=1, border_color=BORDER,
                                           text_color=WHITE, font=ctk.CTkFont(family="Consolas", size=12), wrap="none")
        self.history_box.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.refresh_history()

        # Statistics tab
        stats_tab = self.bottom_tabs.tab("Statistics")
        stats_tab.grid_columnconfigure(0, weight=1)
        stats_tab.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(stats_tab, text="COMPRESSION STATISTICS", text_color=WHITE,
                      font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.stats_box = ctk.CTkTextbox(stats_tab, fg_color=BLACK, border_width=1, border_color=BORDER,
                                         text_color=WHITE, font=ctk.CTkFont(family="Consolas", size=13), wrap="none")
        self.stats_box.grid(row=1, column=0, sticky="nsew")
        self._button(stats_tab, "REFRESH STATS", self.refresh_statistics, width=140).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.refresh_statistics()

        # ── Compatibility tab ─────────────────────────────────────────────────
        compat_tab = self.bottom_tabs.tab("Compatibility")
        compat_tab.grid_columnconfigure(0, weight=1)
        compat_tab.grid_columnconfigure(1, weight=2)
        compat_tab.grid_rowconfigure(0, weight=1)

        # ── Left: submit form ─────────────────────────────────────────────────
        form_outer = ctk.CTkScrollableFrame(compat_tab, fg_color=PANEL, border_width=1,
                                             border_color=BORDER, corner_radius=10)
        form_outer.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=4)
        form_outer.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(form_outer, text="SUBMIT COMPATIBILITY REPORT",
                      text_color=WHITE, font=ctk.CTkFont(size=13, weight="bold")
                     ).grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 8))

        # Form fields
        self._compat_title_var      = tk.StringVar()
        self._compat_titleid_var    = tk.StringVar()
        self._compat_origsize_var   = tk.StringVar()
        self._compat_compsize_var   = tk.StringVar()
        self._compat_smver_var      = tk.StringVar()
        self._compat_storage_var    = tk.StringVar(value="Internal PS5 SSD")
        self._compat_status_var     = tk.StringVar(value="Working")

        field_rows = [
            ("Game Title",       self._compat_title_var,   False),
            ("Title ID",         self._compat_titleid_var, False),
            ("Original Size",    self._compat_origsize_var,False),
            ("Compressed Size",  self._compat_compsize_var,False),
            ("ShadowMount Ver.", self._compat_smver_var,   False),
        ]
        for ri, (lbl, var, _) in enumerate(field_rows, start=1):
            ctk.CTkLabel(form_outer, text=lbl + ":", text_color=MUTED,
                          font=ctk.CTkFont(size=11)).grid(row=ri, column=0, sticky="w", padx=12, pady=2)
            ctk.CTkEntry(form_outer, textvariable=var, fg_color=CARD,
                          border_color=BORDER2, text_color=WHITE,
                          font=ctk.CTkFont(size=11)).grid(row=ri, column=1, sticky="ew", padx=(4, 12), pady=2)

        ctk.CTkLabel(form_outer, text="Storage:", text_color=MUTED,
                      font=ctk.CTkFont(size=11)).grid(row=6, column=0, sticky="w", padx=12, pady=2)
        ctk.CTkOptionMenu(form_outer, variable=self._compat_storage_var,
                           values=["Internal PS5 SSD", "USB SSD", "USB HDD", "External HDD"],
                           fg_color=CARD2, button_color=CARD2, button_hover_color=BORDER2,
                           text_color=WHITE, font=ctk.CTkFont(size=11)
                          ).grid(row=6, column=1, sticky="ew", padx=(4, 12), pady=2)

        ctk.CTkLabel(form_outer, text="Status:", text_color=MUTED,
                      font=ctk.CTkFont(size=11)).grid(row=7, column=0, sticky="w", padx=12, pady=2)
        status_row = ctk.CTkFrame(form_outer, fg_color="transparent")
        status_row.grid(row=7, column=1, sticky="w", padx=(4, 12), pady=2)
        for st, col in [("✅ Working", GREEN), ("⚠ Partial", YELLOW), ("❌ Not Working", RED)]:
            ctk.CTkRadioButton(status_row, text=st, variable=self._compat_status_var,
                                value=st.split(" ", 1)[1],
                                fg_color=col, hover_color=col,
                                text_color=WHITE, font=ctk.CTkFont(size=11)
                               ).pack(side="left", padx=(0, 8))

        ctk.CTkLabel(form_outer, text="Performance Notes:", text_color=MUTED,
                      font=ctk.CTkFont(size=11)).grid(row=8, column=0, sticky="nw", padx=12, pady=(6, 2))
        self._compat_notes_box = ctk.CTkTextbox(form_outer, fg_color=CARD, border_width=1,
                                                 border_color=BORDER2, text_color=WHITE,
                                                 font=ctk.CTkFont(size=11), height=60, wrap="word")
        self._compat_notes_box.grid(row=8, column=1, sticky="ew", padx=(4, 12), pady=(6, 4))

        # ── Share to community checkbox ───────────────────────────────────────
        self._compat_share_var = tk.BooleanVar(value=True)
        share_row = ctk.CTkFrame(form_outer, fg_color="transparent")
        share_row.grid(row=9, column=0, columnspan=2, sticky="ew", padx=12, pady=(4, 0))
        ctk.CTkCheckBox(share_row, text="Share anonymously to community database",
                        variable=self._compat_share_var,
                        text_color=MUTED, font=ctk.CTkFont(size=11),
                        fg_color=GREEN, hover_color=GREEN2,
                        border_color=BORDER2).pack(side="left")
        self._compat_share_status = ctk.CTkLabel(share_row, text="", text_color=MUTED,
                                                  font=ctk.CTkFont(size=10))
        self._compat_share_status.pack(side="left", padx=(8, 0))

        btn_row = ctk.CTkFrame(form_outer, fg_color="transparent")
        btn_row.grid(row=10, column=0, columnspan=2, sticky="ew", padx=12, pady=(4, 12))
        btn_row.grid_columnconfigure(0, weight=1)
        btn_row.grid_columnconfigure(1, weight=1)
        self._button(btn_row, "✚  Submit Report", self.submit_compat_report,
                      green=True).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._button(btn_row, "⟳  Auto-fill from last game", self._compat_autofill
                     ).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        # ── Right: compatibility list ─────────────────────────────────────────
        list_frame = ctk.CTkFrame(compat_tab, fg_color=PANEL, border_width=1,
                                   border_color=BORDER, corner_radius=10)
        list_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=4)
        list_frame.grid_columnconfigure(0, weight=1)
        list_frame.grid_rowconfigure(1, weight=1)

        list_head = ctk.CTkFrame(list_frame, fg_color="transparent")
        list_head.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        list_head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(list_head, text="COMPATIBILITY LIST", text_color=WHITE,
                      font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, sticky="w")
        hbtn = ctk.CTkFrame(list_head, fg_color="transparent")
        hbtn.grid(row=0, column=1)
        self._button(hbtn, "⟳ Refresh", self.refresh_compat_list, width=80).pack(side="left", padx=(0, 4))
        self._button(hbtn, "CSV Export", self.export_compat_csv,   width=90).pack(side="left")

        self.compat_box = ctk.CTkTextbox(list_frame, fg_color=BLACK, border_width=1, border_color=BORDER,
                                          text_color=WHITE, font=ctk.CTkFont(family="Consolas", size=11),
                                          wrap="word", state="disabled")
        self.compat_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.refresh_compat_list()

        # Footer
        footer = ctk.CTkFrame(main, fg_color=BLACK)
        footer.grid(row=5, column=0, sticky="ew", padx=18, pady=(0, 8))
        self.footer_var = tk.StringVar(value="● Ready")
        ctk.CTkLabel(footer, textvariable=self.footer_var, text_color=("#1a7a40", "#4ade80")).pack(side="left")
        ctk.CTkLabel(footer, text=f"{APP_VERSION}  |  Bizkut Backend  |  {MKPFS_NAME} v{MKPFS_VERSION}", text_color=MUTED).pack(side="right")

        self.update_queue_box()

        # ── Drag & drop registration ─────────────────────────────────────────
        if _HAS_DND:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)

    # ── Theme toggle ─────────────────────────────────────────────────────────
    def _toggle_theme(self):
        self._theme = "light" if self._theme == "dark" else "dark"
        ctk.set_appearance_mode(self._theme)
        save_settings({"appearance_mode": self._theme})
        # No manual recoloring needed — all color constants are (light, dark) tuples
        # so CTk picks the correct value automatically on appearance mode change.

    def _open_patch_dialog(self):
        if getattr(self, "_batch_running", False) or (getattr(self, "worker", None) and self.worker and self.worker.is_alive()):
            messagebox.showinfo("Please wait", "A job is already running. Wait for it to finish or cancel it first.")
            return
        PatchDialog(self)

    def _patch_failed(self, msg: str):
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.status_update("Failed", msg, "Failed", 0, 0, "00:00", "—", "—")
        self.log("ERROR", f"Patch failed: {msg}")
        messagebox.showerror("Patch fehlgeschlagen", msg)

    def _launch_patch_worker(self, item, cmd, cwd, out_dir, temp_dir):
        self._batch_total = 1; self._batch_done = 0; self._batch_failed = 0
        self._batch_running = False
        self._update_batch_counter()
        self._last_cmd_str = " ".join(str(c) for c in cmd)
        self.cancel_requested = False
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.status_update("Patching", f"Patching {item.name}…", "Creating Temp PFS", 0, 0, "00:00", "—", "—")
        self._active_item = item
        self.worker = CLIWorker(self, item, cmd, cwd, out_dir, temp_dir)
        self.worker.start()

    def _start_patch(self, game_str: str, patch_str: str, overwrite: bool):
        """Integrate a patch into a game: unpack the game (.ffpfsc/folder/archive),
        overlay the patch files, repack. Extraction of any archives runs in a worker
        thread; the actual unpack→overlay→repack is the backend --patch flow."""
        game = Path(self._clean_path_str(game_str))
        patch = Path(self._clean_path_str(patch_str))
        if not game.exists():
            messagebox.showerror("Not found", f"Game not found:\n{game}"); return
        if not patch.exists():
            messagebox.showerror("Not found", f"Patch not found:\n{patch}"); return
        temp_base = self.temp_var.get().strip() or str(game.parent / "_ffpfsc_temp")
        self.temp_var.set(temp_base)
        out_base = self.output_var.get().strip()
        self.start_btn.configure(state="disabled"); self.cancel_btn.configure(state="normal")
        self.cancel_requested = False; self.extract_cancel_event.clear()
        self.status_update("Patching", f"Preparing patch: {game.name}…", "Scanning Files", 0, 0, "00:00", "—", "—")
        try:
            self.bottom_tabs.set("Logs")
        except Exception:
            pass
        ARCH = (".zip", ".rar", ".7z")

        # Snapshot every Tk var on the MAIN thread — reading tk.*Var.get() from the worker
        # thread below is cross-thread Tcl access (RuntimeError / corrupt value / segfault,
        # especially on macOS). Everywhere else in the app already snapshots like this.
        snap_pw = self._candidate_passwords()
        snap_cl = self.compression_level_var.get()
        snap_cpu = self.cpu_count_var.get()
        snap_bs = self.block_size_var.get()
        snap_verbose = self.verbose_var.get()

        def work():
            try:
                pw = snap_pw
                patch_inplace = False
                gsfx = game.suffix.lower()
                # resolve the GAME to something the backend --patch understands
                if gsfx == ".ffpfsc" or game.is_dir():
                    game_arg = game
                elif gsfx in ARCH:
                    self.log("INFO", f"Extracting game archive: {game.name}")
                    self.status_update("Extracting", f"Unpacking game: {game.name}…", "Extracting", 0, 0, "—", "—", "—")
                    game_arg = ArchiveExtractor.extract_with_passwords(
                        game, Path(temp_base) / "_patch_game", pw,
                        log_fn=self.log, cancel_event=self.extract_cancel_event)
                    patch_inplace = True   # extracted to a throwaway temp dir → overlay in place
                else:
                    raise RuntimeError(f"Unsupported game type: {game.name}")
                # resolve the PATCH to a folder of loose files
                if patch.is_dir():
                    patch_dir = patch
                elif patch.suffix.lower() in ARCH:
                    self.log("INFO", f"Extracting patch archive: {patch.name}")
                    self.status_update("Extracting", f"Unpacking patch: {patch.name}…", "Extracting", 0, 0, "—", "—", "—")
                    patch_dir = ArchiveExtractor.extract_with_passwords(
                        patch, Path(temp_base) / "_patch_files", pw,
                        log_fn=self.log, cancel_event=self.extract_cancel_event)
                else:
                    raise RuntimeError(f"Patch must be a folder or archive: {patch.name}")
                if self.cancel_requested:
                    raise ArchiveExtractionCancelled("Cancelled by user.")
                # output path (ask-per-run choice from the dialog)
                if overwrite and gsfx == ".ffpfsc":
                    out = game
                else:
                    stem = game.stem if gsfx == ".ffpfsc" else game.name
                    folder = Path(out_base) if out_base else game.parent
                    out = folder / f"{sanitize_filename(stem)} [patched].ffpfsc"
                out.parent.mkdir(parents=True, exist_ok=True)
                # build the backend command
                pycmd = get_backend_python_command()
                backend = backend_base_dir()
                cli_py = backend / "cli.py"
                head = (pycmd + [str(game_arg), str(out)] if getattr(sys, "frozen", False)
                        else pycmd + ["-u", str(cli_py), str(game_arg), str(out)])
                cmd = head + ["--patch", str(patch_dir), "--temp-dir", temp_base, "--overwrite"]
                if patch_inplace:
                    cmd += ["--patch-inplace"]
                if snap_cl != 7: cmd += ["--compression-level", str(snap_cl)]
                if snap_cpu != 0: cmd += ["--cpu-count", str(snap_cpu)]
                if snap_bs and snap_bs != "auto": cmd += ["--block-size", snap_bs]
                if snap_verbose: cmd.append("--verbose")
                # lightweight item for progress / report / history
                item = GameItem.__new__(GameItem)
                item.path = Path(game_arg)
                item.archive_path = None
                item.operation = "pack"
                item.name = (game.stem if gsfx == ".ffpfsc" else game.name) + " (patched)"
                try:
                    item.title_id = parse_title_id(Path(game_arg)) if Path(game_arg).is_dir() else "🩹"
                except Exception:
                    item.title_id = "🩹"
                item.size = 0; item.files = 0; item.artwork = None; item.status = "Patching"
                self.root.after(0, lambda: self._launch_patch_worker(item, cmd, backend, out.parent, Path(temp_base)))
            except ArchiveExtractionCancelled as e:
                self.root.after(0, lambda m=str(e): self._patch_failed(m))
            except Exception as e:
                self.root.after(0, lambda m=str(e): self._patch_failed(m))
        threading.Thread(target=work, daemon=True).start()

    def open_settings(self):
        if self._settings_win and self._settings_win.winfo_exists():
            self._settings_win.focus()
            return
        self._settings_win = SettingsWindow(self.root, self)

    def open_converter(self):
        ConverterDialog(self)

    def _queue_conversion(self, path: Path, action: str):
        """Add a converter job to the queue (the user then presses START — it reuses the
        normal unpack pipeline). action: 'decompress' (.ffpfsc → inner .ffpfs, one level),
        'folder' (full unpack to a folder), 'batch' (a folder of images → unpack each)."""
        if not (self.output_var.get() or "").strip():
            base = path.parent if path.is_file() else path
            self.output_var.set(str(base))
        if action == "batch":
            self.source_var.set(str(path))
            self.unpack_mode_var.set(True)        # the folder-scan unpack path reads this
            self.add_source_to_queue()
            return
        item = GameItem.from_pfs_image(path)
        item.unwrap = (action != "decompress")    # decompress = stop at the inner .ffpfs
        self.queue.append(item)
        self.update_queue_box(select_item=item)
        what = "decompress → .ffpfs" if action == "decompress" else "unpack → folder"
        self.log("OK", f"Conversion queued ({what}): {path.name}.  Press ▶ START to run.")
        self.status_update("Ready", f"Conversion queued: {path.name} — press START.",
                            "Ready", 0, 0, "00:00", "—", "—")

    # ── Drag & drop ───────────────────────────────────────────────────────────
    def _on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        for raw in paths:
            p = Path(raw.strip("{}").strip())
            if p.exists():
                self.source_var.set(str(p))
                self.add_source_to_queue()   # handles folders AND archives uniformly
            else:
                self.log("WARN", f"Dropped path not found: {p}")

    # ── Folder browse ─────────────────────────────────────────────────────────
    def browse_source_folder(self):
        """Folder picker — single game folder or parent folder containing multiple dumps."""
        path = filedialog.askdirectory(title="Select PS5 game folder or parent folder")
        if not path:
            return
        p = Path(path)
        self.source_var.set(str(p))
        if not self.output_var.get():
            self.output_var.set(str(p.parent))
        if not self.temp_var.get():
            self.temp_var.set(str(p.parent / "_ffpfsc_temp"))
        self.preview_light(p)
        self.add_source_to_queue()

    def browse_source_archive(self):
        """File picker — select an archive, disk image, or PFS image."""
        path = filedialog.askopenfilename(
            title="Select archive, disk image, or PFS image",
            filetypes=[
                ("Supported files", "*.zip *.rar *.7z *.exfat *.ffpkg *.ffpfs *.ffpfsc"),
                ("Disk images",     "*.exfat *.ffpkg"),
                ("PFS images",      "*.ffpfs *.ffpfsc"),
                ("Archives",        "*.zip *.rar *.7z"),
                ("ZIP",             "*.zip"),
                ("RAR",             "*.rar"),
                ("7-Zip",           "*.7z"),
                ("All files",       "*.*"),
            ]
        )
        if not path:
            return
        p = Path(path)
        self.source_var.set(str(p))
        if p.suffix.lower() == ".ffpfsc":   # .ffpfs is a PACK source now (re-pack), not unpack
            self.unpack_mode_var.set(True)
        if not self.output_var.get():
            self.output_var.set(str(p.parent))
        if not self.temp_var.get():
            self.temp_var.set(str(p.parent / "_ffpfsc_temp"))
        self.add_source_to_queue()

    def browse_pfs_image(self):
        path = filedialog.askopenfilename(
            title="Select PFS image to unpack",
            filetypes=[
                ("PFS images", "*.ffpfs *.ffpfsc"),
                ("All files", "*.*"),
            ]
        )
        if not path:
            return
        p = Path(path)
        self.source_var.set(str(p))
        self.unpack_mode_var.set(True)
        if not self.output_var.get():
            self.output_var.set(str(p.parent))
        self.add_source_to_queue()

    def browse_output_folder(self):
        p = filedialog.askdirectory(title="Select output folder")
        if p:
            self.output_var.set(p)
            if not self.temp_var.get():
                self.temp_var.set(str(Path(p) / "_ffpfsc_temp"))
            save_settings({"output_folder": p})
            self.update_command_preview()

    def browse_temp_folder(self):
        p = filedialog.askdirectory(title="Select temp folder")
        if p:
            tp = str(Path(p) / "_ffpfsc_temp")
            self.temp_var.set(tp)
            save_settings({"temp_folder": tp})
            self.update_command_preview()

    def preview_light(self, p: Path):
        self.game_name_var.set(f"Name: {guess_game_name(p)}")
        self.title_var.set(f"Title ID: {parse_title_id(p)}")
        self.source_detail_var.set(f"Source: {p}")
        self.orig_var.set("Original Size: click Scan / Add")
        self.files_var.set("Files: click Scan / Add")
        self.load_art(find_artwork(p))
        self.update_command_preview()

    def load_art(self, art):
        # Cache: skip disk I/O if the path hasn't changed
        art_key = str(art) if art else None
        if art_key == getattr(self, "_loaded_art_key", object()):
            return
        self._loaded_art_key = art_key

        if Image and ImageTk and art and art.exists():
            try:
                img = Image.open(art).convert("RGBA")
                img.thumbnail((130, 130))
                tk_img = ImageTk.PhotoImage(img)
                self.art_img = tk_img          # hold reference — GC would delete the Tcl image
                self.art_label.configure(image=tk_img, text="", bg="#000000")
                return
            except Exception as _art_err:
                pass   # fall through to placeholder
        # No art or load failed
        self.art_img = None
        self.art_label.configure(image="", text="NO\nART", fg="#888888", bg="#000000")

    # ── Queue management ──────────────────────────────────────────────────────
    @staticmethod
    def _clean_path_str(raw: str) -> str:
        """Strip whitespace and surrounding quotes Windows sometimes adds."""
        s = raw.strip()
        if len(s) >= 2 and s[0] in ('"', "'") and s[-1] == s[0]:
            s = s[1:-1].strip()
        return s

    def _classify_extracted_payload(self, extracted_root: Path, archive_name: str) -> tuple[str, list[Path]]:
        """Return the supported payload type and paths found inside an extracted archive."""
        pfs_images = find_files_by_suffix(extracted_root, PFS_IMAGE_SUFFIXES)
        if pfs_images:
            self.log("INFO", f"Archive payload detected: {len(pfs_images)} PFS image(s) for extraction")
            return "pfs", pfs_images

        disk_images = find_files_by_suffix(extracted_root, DISK_IMAGE_SUFFIXES)
        if disk_images:
            self.log("INFO", f"Archive payload detected: {len(disk_images)} disk image(s) for compression")
            return "disk", disk_images

        games = find_game_folders(extracted_root)
        if games:
            self.log("INFO", f"Archive payload detected: {len(games)} PS5 game folder(s)")
            return "game", games

        if is_game_folder(extracted_root):
            self.log("INFO", "Archive payload detected: PS5 game folder")
            return "game", [extracted_root]

        # Nested archive (archive inside the archive) — give a clear, actionable error
        # instead of the misleading generic "no payload" message.
        inner_archives = find_files_by_suffix(extracted_root, {".zip", ".rar", ".7z"})
        if inner_archives:
            names = ", ".join(sorted({a.name for a in inner_archives})[:5])
            raise RuntimeError(
                f"{archive_name} contains another archive ({names}), not a game.\n\n"
                "Nested archives are not unpacked automatically — extract the inner "
                "archive yourself first, then add the resulting game folder or disk image."
            )

        raise RuntimeError(
            f"No supported payload found after extracting {archive_name}.\n\n"
            "Expected one of these inside the archive:\n"
            "  • PFS images (.ffpfs / .ffpfsc) for extraction\n"
            "  • Disk images (.exfat / .ffpkg) for compression\n"
            "  • A PS5 game folder containing sce_sys/ and eboot.bin"
        )

    def _item_from_payload_path(self, kind: str, path: Path) -> GameItem:
        if kind == "pfs":
            return GameItem.from_pfs_image(path)
        if kind == "disk":
            return GameItem.from_exfat(path)
        return GameItem(path)

    def _sibling_extras(self, parent: Path, game_paths) -> list:
        """Extra items (DLC subfolders, loose non-junk files) sitting BESIDE the game in an
        extracted archive tree, to copy next to the .ffpfsc. Excludes the detected game
        folder(s) and any wrapper that contains one, OS/Finder junk, and scene-note files
        (.txt/.nfo/images …). The source is never modified — these are copied, not moved."""
        ex = set()
        for g in game_paths:
            try:
                ex.add(str(Path(g).resolve()))
            except Exception:
                pass
        out = []
        try:
            for child in sorted(Path(parent).iterdir()):
                try:
                    rp = str(child.resolve())
                except Exception:
                    continue
                if rp in ex:
                    continue                                       # the game folder itself
                if any(g == rp or g.startswith(rp + os.sep) for g in ex):
                    continue                                       # a wrapper that holds the game
                if is_fs_junk_name(child.name):
                    continue
                if child.is_file() and child.suffix.lower() in SCENE_JUNK_EXTS:
                    continue                                       # scene notes / nfo / cover images
                out.append(child)
        except Exception:
            pass
        return out

    def _copy_item_payload(self, target: GameItem, source: GameItem) -> None:
        target.path         = source.path
        target.archive_path = source.archive_path
        target.operation    = source.operation
        target.name         = source.name
        target.title_id     = source.title_id
        target.size         = source.size
        target.files        = source.files
        target.artwork      = source.artwork
        target.status       = source.status
        # After extraction the source is a real folder on the build drive: it becomes
        # 'inplace' (packed in place), so the post-extraction re-gate sizes only the
        # remaining image+spool (~2.3x) rather than re-counting the already-written tree.
        target.source_kind    = getattr(source, "source_kind", "inplace")
        target.extracted_size = getattr(source, "extracted_size", source.size)
        # Now that the archive is a real folder, detect whether it's a PlayGo/APR title.
        try:
            target.ampr_emu = is_apr_game(target.path)
        except Exception:
            target.ampr_emu = False

    def add_source_to_queue(self):
        src_str = self._clean_path_str(self.source_var.get())
        if not src_str:
            self.pending_start = False
            messagebox.showerror("Nothing selected",
                                  "Enter or browse to a game folder, archive, disk image, or PFS image (.ffpfs/.ffpfsc).")
            return
        src = Path(src_str)
        if not src.exists():
            self.pending_start = False
            messagebox.showerror("Path not found",
                                  f"This path does not exist:\n{src}")
            return

        # ── Existing uncompressed .ffpfs — a PACK source (re-pack to .ffpfsc, or copy
        #    when the format toggle is set to uncompressed). Read in place, no extraction. ─
        if src.is_file() and src.suffix.lower() == ".ffpfs":
            item = GameItem.from_exfat(src)   # single-file image item, operation = "pack"
            self.queue.append(item)
            self.update_queue_box(select_item=item)
            self.log("OK", f".ffpfs image queued for (re)packing: {src.name}  [{format_size(item.size)}]")
            self.status_update("Ready",
                                f".ffpfs image queued for packing: {src.name}",
                                "Ready", 0, 0, "00:00", "—", "—")
            return

        # ── Existing .ffpfsc image — unpack/convert via MkPFS ─────────────────
        if src.is_file() and src.suffix.lower() == ".ffpfsc":
            item = GameItem.from_pfs_image(src)
            self.queue.append(item)
            self.update_queue_box(select_item=item)
            self.unpack_mode_var.set(True)
            self.log("OK", f".ffpfsc image queued for extraction: {src.name}  [{format_size(item.size)}]")
            self.status_update("Ready",
                                f".ffpfsc image queued for extraction: {src.name}",
                                "Ready", 0, 0, "00:00", "—", "—")
            return

        # ── Direct .exfat / .ffpkg disk image — no extraction, passed straight to backend ─
        if src.is_file() and src.suffix.lower() in DISK_IMAGE_SUFFIXES:
            item = GameItem.from_exfat(src)
            self.queue.append(item)
            self.update_queue_box(select_item=item)
            label = "exFAT image" if src.suffix.lower() == ".exfat" else "ffpkg image"
            self.log("OK", f"{label} queued: {src.name}  [{format_size(item.size)}]")
            self.status_update("Ready",
                                f"{label} queued: {src.name}",
                                "Ready", 0, 0, "00:00", "—", "—")
            return

        # ── Single archive file — queue as placeholder, extract on its turn ─────
        if src.is_file() and src.suffix.lower() in (".zip", ".rar", ".7z"):
            item = GameItem.from_archive(src)
            self.queue.append(item)
            self.update_queue_box(select_item=item)
            self.log("OK", f"Archive queued: {src.name}  [{format_size(item.size)}]")
            self.status_update("Ready",
                                f"Archive queued — will extract when compression starts: {src.name}",
                                "Ready", 0, 0, "00:00", "—", "—")
            return

        # ── Folder of existing PFS images to unpack ──────────────────────────
        if src.is_dir() and self.unpack_mode_var.get():
            self.status_update("Scanning", f"Scanning {src.name} for PFS images…",
                                "Scanning Files", 0, 0, "00:00", "—", "—")
            self.log("INFO", f"Scanning folder for .ffpfs/.ffpfsc images: {src}")

            def _scan_pfs_folder(p=src):
                try:
                    images = find_files_by_suffix(p, PFS_IMAGE_SUFFIXES)
                    if not images:
                        self.scan_q.put(("error", f"No .ffpfs or .ffpfsc files found in:\n{p}"))
                    elif len(images) == 1:
                        self.scan_q.put(("ok", GameItem.from_pfs_image(images[0])))
                    else:
                        self.scan_q.put(("pfs_found", images))
                except Exception as e:
                    self.scan_q.put(("error", f"PFS scan error: {e}"))
            threading.Thread(target=_scan_pfs_folder, daemon=True).start()
            return

        # ── Direct PS5 game folder ────────────────────────────────────────────
        if is_game_folder(src):
            warns = validate_game_structure(src)
            if warns:
                msg = "\n".join(f"• {w}" for w in warns)
                if not messagebox.askyesno(
                    "Game Structure Warning",
                    f"Potential issues detected:\n\n{msg}\n\n"
                    "Expected: sce_sys/param.json and eboot.bin\n\nAdd anyway?"
                ):
                    self.pending_start = False
                    return
            self.status_update("Scanning", f"Reading {src.name}…",
                                "Scanning Files", 0, 0, "00:00", "—", "—")
            def _scan_single(p=src):
                try:
                    self.scan_q.put(("ok", GameItem(p)))
                except Exception as e:
                    self.scan_q.put(("error", str(e)))
            threading.Thread(target=_scan_single, daemon=True).start()
            return

        # ── Parent / unknown folder ───────────────────────────────────────────
        # Scan for extracted game folders AND loose archive files
        self.status_update("Scanning", f"Scanning {src.name}…",
                            "Scanning Files", 0, 0, "00:00", "—", "—")
        self.log("INFO", f"Scanning folder: {src}")
        cand_pw = self._candidate_passwords()   # built on the Tk thread, for archive peeking

        def _scan_folder(p=src, cand_pw=cand_pw, detect_patch=self.auto_integrate_patch_var.get()):
            try:
                # 0. Bundle: one game (archive / game folder / disk image) plus
                #    extra files (DLCs etc.). Pack the game into a recreated copy
                #    of this folder and copy the extras next to it. Only triggers
                #    when there ARE extras — a plain game still packs normally.
                #    With auto-patch on, a clearly-smaller game-like sibling is
                #    detected as a patch and overlaid, so a bundle can be just
                #    base + patch with no other extras.
                game, siblings, all_games, patch = detect_game_bundle(p, cand_pw, self.log, detect_patch)
                if game is not None and (siblings or patch):
                    extra = f"1 game + {len(siblings)} extra file(s)"
                    if patch:
                        extra += f" + patch '{patch.name}' (integrated)"
                    self.log("OK", f"Folder bundle: {extra} — "
                                   "the folder will be recreated at the destination.")
                    self.scan_q.put(("ok", GameItem.from_bundle(p, game, siblings, patch)))
                    return
                # 0b. Library: each immediate subfolder holds its own game →
                #     one bundle per subfolder (each recreated at the destination
                #     with only the .ffpfsc plus extras inside). Covers "select
                #     /…/PS5 Spiele and convert every game".
                lib = scan_parent_for_bundles(p, cand_pw, self.log, detect_patch)
                if lib:
                    self.log("OK", f"Library scan: {len(lib)} game folder(s) found — each will be "
                                   "mirrored at the destination.")
                    self.scan_q.put(("bundles", lib))
                    return

                # 1. Look for extracted game folders first
                self.log("INFO", "Looking for PS5 game folders…")
                games = find_game_folders(p)
                if games:
                    self.log("INFO", f"Found {len(games)} game folder(s)")
                    if len(games) == 1:
                        try:
                            self.scan_q.put(("ok", GameItem(games[0])))
                        except Exception as e:
                            self.scan_q.put(("error", str(e)))
                    else:
                        self.scan_q.put(("multi_found", games))
                    return

                # 2. No extracted games — look for .exfat/.ffpkg images and archive files (one level deep)
                self.log("INFO", "No game folders found — scanning for disk images and archives…")
                image_files = []   # .exfat and .ffpkg
                archives    = []
                try:
                    for f in p.iterdir():
                        if not f.is_file():
                            continue
                        suffix = f.suffix.lower()
                        if suffix in DISK_IMAGE_SUFFIXES:
                            image_files.append(f)
                            label = "exFAT" if suffix == ".exfat" else "ffpkg"
                            self.log("INFO", f"  Found {label} image: {f.name}")
                        elif suffix in (".zip", ".rar", ".7z"):
                            archives.append(f)
                            self.log("INFO", f"  Found archive: {f.name}")
                except Exception as e:
                    self.log("WARN", f"Could not list folder contents: {e}")

                image_files.sort(key=lambda f: f.name.lower())
                archives.sort(key=lambda f: f.name.lower())

                if image_files:
                    self.log("INFO", f"Found {len(image_files)} disk image(s) — queuing directly (no extraction needed)")
                    if len(image_files) == 1:
                        self.scan_q.put(("ok", GameItem.from_exfat(image_files[0])))
                    else:
                        self.scan_q.put(("exfat_found", image_files))
                    return

                if archives:
                    self.log("INFO", f"Found {len(archives)} archive(s) — queuing for extraction")
                    self.scan_q.put(("archives_found", archives))
                    return

                # 3a. Split .7z/.zip volumes (.7z.001 / .zip.001 / .z01) — not
                #     recombined automatically. Recognise them so the user gets clear
                #     guidance instead of a confusing "nothing found".
                split_parts = []
                try:
                    for f in p.iterdir():
                        n = f.name.lower()
                        if f.is_file() and (re.search(r"\.7z\.\d{2,}$", n)
                                            or re.search(r"\.zip\.\d{2,}$", n)
                                            or re.search(r"\.z\d{2,}$", n)):
                            split_parts.append(f.name)
                except Exception:
                    pass
                if split_parts:
                    sample = ", ".join(sorted(split_parts)[:4])
                    self.scan_q.put((
                        "error",
                        f"Found split-archive parts in:\n{p}\n\n  {sample}\n\n"
                        "Split .7z / .zip volumes are not recombined automatically. "
                        "Recombine them into a single .7z, .zip or .rar first "
                        "(e.g. with 7-Zip or Keka), then add that file.\n"
                        "(Multi-part RAR — .partN.rar or .rNN — is supported directly.)"
                    ))
                    return

                # 3b. Nothing useful found
                self.log("WARN", f"No games, disk images, or archives found in {p}")
                self.scan_q.put((
                    "error",
                    f"Nothing found in:\n{p}\n\n"
                    "Expected either:\n"
                    "  • Game folders containing sce_sys/ and eboot.bin\n"
                    "  • Disk images (.exfat or .ffpkg)\n"
                    "  • Archive files (.zip / .rar / .7z)"
                ))
            except Exception as e:
                self.log("ERROR", f"Folder scan crashed: {e}")
                self.scan_q.put(("error", f"Scan error: {e}"))

        threading.Thread(target=_scan_folder, daemon=True).start()

    def _archive_set_size(self, archive: Path) -> int:
        """Total on-disk size of the archive's volume set (scene archives are usually
        stored, so this ~= the extracted payload size). Used for the auto drive decision."""
        try:
            base = re.sub(
                r'(\.part\d+\.rar|\.r\d{2,}|\.7z\.\d+|\.zip\.\d+|\.z\d+|\.\d{3}|\.rar|\.zip|\.7z)$',
                '', archive.name, flags=re.I)
            total = 0
            for p in archive.parent.iterdir():
                if p.is_file() and p.name.startswith(base):
                    try:
                        total += p.stat().st_size
                    except OSError:
                        pass
            return total or archive.stat().st_size
        except Exception:
            try:
                return archive.stat().st_size
            except Exception:
                return 0

    def _mark_no_spotlight(self, *dirs) -> None:
        """macOS: drop a `.metadata_never_index` marker in each scratch dir so Spotlight
        skips it. Without this, mdworker indexes every freshly-extracted game file (tens
        of thousands of them) on the same drive we're reading from, stealing HDD I/O and
        throttling the pack. Only our scratch dirs are touched — the user's finished
        outputs stay indexable. Best-effort, idempotent, no-op off macOS."""
        if sys.platform != "darwin":
            return
        for d in dirs:
            if not d:
                continue
            try:
                p = Path(d)
                p.mkdir(parents=True, exist_ok=True)
                marker = p / ".metadata_never_index"
                if not marker.exists():
                    marker.touch()
            except Exception:
                pass

    def _warm_drive_types(self) -> None:
        """Kick off background SSD/HDD probes for the source + temp + output drives so
        temp_drive_label()/drive_type_cached() have honest data at pack time without ever
        blocking the UI thread on diskutil/PowerShell. Cached per device, so this is a
        no-op after the first probe of a given drive. (Source is included so the keep-awake
        pinger correctly SKIPS a confirmed-SSD source instead of treating it as Unknown.)"""
        probes = [self.output_var.get().strip(), self.source_var.get().strip()]
        probes += [str(d) for d in self._temp_pool_dirs()]   # primary temp + extra pool drives
        for p in probes:
            if not p:
                continue
            threading.Thread(target=lambda pp=Path(p): get_drive_type(pp), daemon=True).start()

    # ── Keep-awake pinger ──────────────────────────────────────────────────────
    def _start_keep_awake(self) -> None:
        """One long-lived daemon that pings the configured drives so bus-powered
        external HDDs don't sleep. It checks the toggle each cycle (so flipping the
        Settings checkbox takes effect without restarting the thread)."""
        self._keepawake_stop = threading.Event()
        self._keepawake_announced = False
        self._keepawake_thread = threading.Thread(target=self._keep_awake_loop, daemon=True)
        self._keepawake_thread.start()

    @staticmethod
    def _dev_of(path):
        """st_dev for the volume holding *path* (its parent if it's a file), or None."""
        if not path:
            return None
        try:
            p = Path(path)
            d = p if p.is_dir() else p.parent
            return os.stat(str(d)).st_dev
        except OSError:
            return None

    def _temp_pool_dirs(self):
        """Ordered, device-deduped list of fast-temp candidate directories: the primary
        temp field first, then the extra pool dirs from Settings. The router spreads the
        inner image / extracted source across these (and the keep-awake pinger covers
        them). The primary is always included even if it doesn't exist yet (it's created on
        demand); extra pool entries must exist."""
        out, seen = [], set()
        primary = self.temp_var.get().strip()
        cands = ([primary] if primary else []) + [str(p).strip() for p in getattr(self, "temp_pool", [])]
        for i, raw in enumerate(cands):
            if not raw:
                continue
            d = Path(raw)
            try:
                if i > 0 and not d.is_dir():
                    continue   # skip a missing EXTRA pool dir (unplugged drive, typo)
                dev = _drive_cache_key(d)
            except Exception:
                continue
            if dev in seen:
                continue
            seen.add(dev)
            out.append(d)
        return out

    def _keepalive_dirs(self):
        """(dir, st_dev) per physical drive the pinger may touch — source, output, and
        every temp-pool drive. Skips drives we have CONFIRMED to be SSDs (no point), and
        keeps HDD plus still-unknown drives (the WD Elements externals report 'Unknown')."""
        seen, out = set(), []
        raws = []
        for v in (self.source_var, self.output_var):
            try:
                raws.append((v.get() or "").strip())
            except Exception:
                pass
        raws += [str(d) for d in self._temp_pool_dirs()]
        for raw in raws:
            if not raw:
                continue
            p = Path(raw)
            d = p if p.is_dir() else p.parent
            try:
                if not d.is_dir():
                    continue
                if drive_type_cached(d) == "SSD":
                    continue  # never bother a confirmed SSD
                dev = os.stat(str(d)).st_dev
            except OSError:
                continue
            if dev in seen:
                continue
            seen.add(dev)
            out.append((d, dev))
        return out

    def _busy_devices(self):
        """Best-effort set of device IDs the current job is actively reading/writing, by
        worker phase, so the pinger can SKIP them — a flushed write there forces a pointless
        seek away from the streaming I/O. Empty set ⇒ ping every configured drive (the safe
        default when we can't tell, e.g. during pre-worker archive extraction or 'Starting')."""
        w = getattr(self, "worker", None)
        if w is None or not getattr(w, "is_alive", lambda: False)():
            return set()
        phase = getattr(w, "phase", "") or ""
        item = getattr(self, "_active_item", None)
        out = self.output_var.get().strip()
        src = getattr(item, "path", None) if item else None
        arch = getattr(item, "archive_path", None) if item else None
        br = getattr(item, "_build_root", None) if item else None   # extracted-source dir
        bt = getattr(item, "_build_temp", None) if item else None   # inner-image dir
        busy = set()
        def add(*paths):
            for p in paths:
                dev = self._dev_of(p)
                if dev is not None:
                    busy.add(dev)
        if phase in ("Scanning Files", "Reading Game", "Creating Temp PFS"):
            # pass 1: read source, write inner image. An archive reads from its extracted
            # dir (_build_root); a plain folder reads from its original path (_build_root is
            # an unused extract dir for folders, so don't treat it as busy).
            add((br or src) if arch else src, bt)
        elif phase in ("Compressing", "Writing Final Image"):
            add(bt, out)                 # pass 2: read inner image, write .ffpfsc
        elif phase == "Extracting":
            add(arch or src, br)         # archive extraction: read archive, write extract dir
        elif phase == "Verifying Output":
            add(out)                     # verify: read .ffpfsc
        return busy

    def _job_active(self) -> bool:
        """True while the app is actively working the drives (a batch, a live pack worker,
        a running backend process, or a pending extraction). Used to scope the keep-awake
        pinger to runs only — outside a run we let external HDDs fully sleep."""
        try:
            if getattr(self, "_batch_running", False):
                return True
            w = getattr(self, "worker", None)
            if w is not None and w.is_alive():
                return True
            p = getattr(self, "current_process", None)
            if p is not None and p.poll() is None:
                return True
            if getattr(self, "pending_start", False):
                return True
        except Exception:
            pass
        return False

    def _keep_awake_loop(self) -> None:
        while not self._keepawake_stop.is_set():
            active = False
            try:
                active = bool(self.keep_drives_awake_var.get()) and self._job_active()
            except Exception:
                pass
            if active:
                # Fast ping (default 8 s, under WD IntelliPark's 8 s park timer) keeps heads
                # LOADED across inter-game gaps instead of unpark→re-park churn.
                try:
                    interval = max(3, min(15, int(load_settings().get("keep_awake_interval", 8))))
                except Exception:
                    interval = 8
                # Ping only the drives the job ISN'T currently hammering: a flushed write on
                # an actively-streaming drive just forces a wasteful seek, and a busy drive
                # can't sleep anyway. The idle-but-needed-soon drive is the one worth holding.
                busy = self._busy_devices()
                pinged = 0
                for d, dev in self._keepalive_dirs():
                    if dev in busy:
                        continue
                    poke_drive_keepalive(d)
                    pinged += 1
                if pinged and not self._keepawake_announced:
                    self.log("INFO", f"Keep-awake: holding idle drive(s) spun-up every "
                                     f"{interval}s for this run (skipping the busy one).")
                    self._keepawake_announced = True
            else:
                self._keepawake_announced = False
            # Ping on the fast interval during a run; otherwise just poll the gate every 5 s.
            self._keepawake_stop.wait(interval if active else 5)

    def _resolve_extract_root(self, item) -> Path:
        """Place this run's artifacts across drives to maximise fast (SSD) temp use, and
        record the choice on the item: _build_root (where an archive extracts) and
        _build_temp (the backend --temp-dir = where the inner image goes). The pass-2 spool
        is then placed ADAPTIVELY by the backend (SSD if it still fits beside the image,
        else the output drive), so the SSD no longer has to hold image+spool together.

        AUTO weighs three independent placements, each preferring the temp/SSD drive and
        each falling back to the output drive — and never lets an HDD do a same-drive
        read+write for the heavy steps:
          1) Everything on the SSD (source + image + spool) when the full footprint fits.
          2) SPLIT: inner image on the SSD, source extracted to the output drive (the spool
             is auto-routed). Used when only the image — not image+spool — fits the SSD.
          3) Everything on the output drive when the SSD can't even hold the image.
          4) Nothing fits → leave temp; the space gate skips/aborts with real numbers.

        Modes 'temp'/'spread' force the SSD / the output drive respectively; the backend's
        adaptive spool still saves a too-tight 'temp' run from failing mid-pass-2."""
        archive = getattr(item, "archive_path", None)
        # Record the output format so the space gate sizes the OUTPUT-drive reservation
        # correctly (compressed .ffpfsc → realistic; uncompressed .ffpfs → full size).
        try:
            item._output_compressed = bool(self.output_compressed_var.get())
        except Exception:
            item._output_compressed = True
        temp_base = self.temp_var.get().strip()
        if not temp_base:
            anchor = archive.parent if archive else Path.home()
            temp_base = str(anchor / "_ffpfsc_temp")
            self.temp_var.set(temp_base)
        temp_base_p = Path(temp_base)
        temp_root = temp_base_p / "_extracted"

        def set_temp():
            item._build_root = temp_root
            item._build_temp = temp_base_p
            item._image_only_on_temp = False
            item._extract_on_pool = False
        set_temp()   # default: whole scratch on the temp drive

        # Placement is resolved several times per item (the space gate, the extraction
        # step, and the post-extraction re-gate), so log a given decision only ONCE per
        # item — re-log only if the decision actually changes — to keep the log clean.
        def _plog(level, msg):
            if getattr(item, "_last_placement_log", None) == msg:
                return
            item._last_placement_log = msg
            self.log(level, msg)

        mode = self.drive_mode_var.get() if getattr(self, "drive_mode_var", None) else "auto"
        out_str = self.output_var.get().strip()
        size = _build_size_of(item)
        factor = _peak_factor_for(item)
        if not out_str:
            return temp_root

        out_dir = Path(out_str)
        spread_root = out_dir / "_ffpfsc_extract"
        spread_temp = out_dir / "_ffpfsc_temp"

        def set_spread():
            item._build_root = spread_root
            item._build_temp = spread_temp
            item._image_only_on_temp = False
            item._extract_on_pool = False
        try:
            probe_out = out_dir if out_dir.exists() else out_dir.parent
            out_is_source = bool(archive) and same_drive(probe_out, archive.parent)
        except Exception:
            probe_out, out_is_source = out_dir, False

        same_to = same_drive(temp_base_p, out_dir)
        full_need  = estimate_peak_space_needed(size, factor, same_to)   # src+image+spool on temp
        image_need = estimate_image_space_needed(size)                   # just the inner image on temp
        out_full   = estimate_peak_space_needed(size, factor, True)      # everything on the output drive
        # Output drive must hold, in the split: the source copy extracted there (archives
        # only) + the final container (the spool only spills here if the SSD can't hold it).
        comp_out = bool(getattr(item, "_output_compressed", True))
        known_out = int(getattr(item, "size", 0) or 0) if getattr(item, "source_kind", "") == "archive" else 0
        src_on_out = size if archive else 0
        out_split_need = int(src_on_out) + estimate_output_space_needed(size, comp_out, known_out)
        temp_free = get_free_space(temp_base_p)
        out_free  = get_free_space(probe_out)
        szs = format_size(size) if size else "?"
        contention = " (output is the source drive: read+write contention, slower)" if out_is_source else ""

        # ── Disk images (.exfat, .ffpkg): single-pass — mkpfs compresses the file
        # directly to .ffpfsc without building a temp inner image.  Temp = irrelevant;
        # only the output drive needs space for the final container. ──────────────────
        if _item_is_single_pass(item):
            output_need = estimate_output_space_needed(size, comp_out, 0)
            set_spread()   # _build_temp on the output drive (backend still needs a temp dir)
            if size == 0 or out_free >= output_need:
                if size > 0:
                    _plog("INFO", f"Auto: {item.name} (~{szs}): disk image → single-pass "
                                  f"to {out_dir} (~{format_size(output_need)} needed, "
                                  f"{format_size(out_free)} free){contention}.")
            else:
                _plog("WARN", f"Auto: {item.name} (~{szs}): disk image needs "
                              f"~{format_size(output_need)} on output drive, only "
                              f"{format_size(out_free)} free — the space gate will skip/abort it.")
            return spread_root

        if mode == "temp":
            if size and temp_free < image_need:
                _plog("WARN", f"Drive 'temp': {item.name} image needs ~{format_size(image_need)} on temp, "
                                  f"only {format_size(temp_free)} free — the space gate will handle it.")
            return temp_root
        if mode == "spread":
            set_spread()
            _plog("INFO", f"Drive 'spread': building on the output drive {out_dir}{contention}.")
            return spread_root
        # AUTO ---------------------------------------------------------------------
        # Fast-temp candidates (primary temp + any extra pool drives), each with free space.
        pool = []
        for d in self._temp_pool_dirs():
            try:
                pool.append((d, get_free_space(d)))
            except Exception:
                pass
        if not pool:
            pool = [(temp_base_p, temp_free)]
        is_archive_pre = getattr(item, "source_kind", "") == "archive"

        def _set(image_dir, extract_dir, image_only, on_pool):
            item._build_temp = Path(image_dir)
            item._build_root = Path(extract_dir)
            item._image_only_on_temp = image_only
            item._extract_on_pool = on_pool

        # 1) Everything on ONE fast drive (source + image + spool). Best-fit = most free.
        fit_full = [(d, f) for d, f in pool if f >= full_need]
        if size > 0 and fit_full:
            best = max(fit_full, key=lambda t: t[1])[0]
            _set(best, Path(best) / "_extracted", False, False)
            if _drive_cache_key(best) != _drive_cache_key(temp_base_p):
                _plog("INFO", f"Auto: {item.name} (~{szs}): whole scratch on pool drive {best}.")
            return item._build_root
        # 2) TWO fast drives (archives only): inner image on one, extracted source on
        #    ANOTHER — keeps pass 1 SSD↔SSD when no single fast drive holds image+source.
        if size > 0 and is_archive_pre:
            img_fit = [(d, f) for d, f in pool if f >= image_need]
            if img_fit:
                img_dir = min(img_fit, key=lambda t: t[1])[0]    # smallest fast drive that fits the image
                img_dev = _drive_cache_key(img_dir)
                ext_fit = [(d, f) for d, f in pool
                           if _drive_cache_key(d) != img_dev and f >= int(size)]
                if ext_fit:
                    ext_dir = max(ext_fit, key=lambda t: t[1])[0]  # most-free OTHER fast drive
                    _set(img_dir, Path(ext_dir) / "_extracted", True, True)
                    _plog("INFO", f"Auto: {item.name} (~{szs}): extract source → {ext_dir}; "
                                     f"inner image → {img_dir}; final → {out_dir}. Pass 1 stays SSD↔SSD.")
                    return item._build_root
        # 3) SPLIT — inner image on a fast drive, source extracted to the OUTPUT drive; the
        #    backend routes the spool adaptively. Reads source off one drive while writing
        #    the image to the other, so neither HDD does a same-drive read+write.
        img_fit = [(d, f) for d, f in pool if f >= image_need]
        if size > 0 and img_fit and out_free >= out_split_need:
            img_dir = max(img_fit, key=lambda t: t[1])[0]
            _set(img_dir, spread_root, True, False)          # source extracts to the big output drive
            _plog("INFO", f"Auto: {item.name} (~{szs}): 1) extract source → output drive "
                             f"({out_dir}); 2) build inner image → {temp_drive_label(img_dir)} "
                             f"({img_dir}); spool auto-routed. No same-drive read+write.")
            return spread_root
        # 4) Everything on the output drive (mechanical, slower, but it completes).
        if out_free >= out_full:
            set_spread()
            why = "too big for the temp drive(s)" if size > 0 else "unknown size — using the larger drive for safety"
            _plog("INFO", f"Auto: {item.name} (~{szs}) {why} → building on {out_dir}"
                             f"{contention or ' (mechanical drive, slower, but it completes)'}.")
            return spread_root
        # 5) Nothing fits — leave temp; the space gate aborts/skips with real numbers.
        _plog("WARN", f"Auto: {item.name} (~{szs}) fits neither the temp drive(s) (~{format_size(image_need)}) "
                          f"nor the output drive (~{format_size(out_full)}) — the space gate will skip/abort it.")
        return temp_root

    def _extract_dir_for_item(self, item):
        """The extract subdir THIS item was unpacked into (under a temp '_extracted' or
        an output-drive '_ffpfsc_extract' folder), or None for a plain (non-extracted)
        source. Used so cleanup only ever removes this item's own data."""
        try:
            src = Path(getattr(item, "path", "") or "").resolve()
        except Exception:
            return None
        for parent in src.parents:
            if parent.name in ("_extracted", "_ffpfsc_extract"):
                try:
                    return parent / src.relative_to(parent).parts[0]
                except Exception:
                    return None
        return None

    def _cleanup_item_extract(self, item) -> None:
        """After an item finishes, remove ITS OWN extracted-source subdir (temp or output
        drive). Safe — only this item's subdir, never another's. Threaded (rmtree large)."""
        own = self._extract_dir_for_item(item)
        if own is None:
            return
        def _work(d=own):
            try:
                if d.exists():
                    sz = get_folder_size(d)
                    shutil.rmtree(str(d), ignore_errors=True)
                    if sz:
                        self.log("INFO", f"Cleaned {format_size(sz)} extracted source from {d.parent.name}.")
                if d.parent.name == "_ffpfsc_extract":
                    try:
                        if not any(d.parent.iterdir()):
                            d.parent.rmdir()
                    except Exception:
                        pass
            except Exception:
                pass
        self._run_cleanup(_work)

    def _extract_and_queue_archive(self, archive: Path):
        """Extract *archive* (background thread) with live progress, then queue.
        (Currently unreachable — the queue path _extract_queued_item is used instead.)"""
        extract_root = self._resolve_extract_root(GameItem.from_archive(archive))

        self.log("INFO", f"Extracting archive: {archive.name}")
        self.cancel_requested = False
        self.extract_cancel_event.clear()
        self.cancel_btn.configure(state="normal")
        self.status_update("Extracting", f"Unpacking {archive.name}…  0%",
                            "Extracting", 0, 0, "00:00", "—", "—")
        # Show Logs tab so the user can watch per-file lines
        try:
            self.bottom_tabs.set("Logs")
        except Exception:
            pass

        # Throttle: only update status every 2 % to avoid flooding the queue
        _last_pct = [-1]
        def _progress(pct: int, filename: str):
            if pct - _last_pct[0] >= 2 or pct >= 100:
                _last_pct[0] = pct
                short = Path(filename).name[:50]
                self.status_update(
                    "Extracting",
                    f"Unpacking {archive.name}…  {pct}%\n{short}",
                    "Extracting", pct, pct * ARCHIVE_EXTRACT_OVERALL_PCT / 100.0, "—", "—", "—"
                )

        candidate_passwords = self._candidate_passwords()  # built on the Tk thread

        def worker():
            try:
                extracted_root = ArchiveExtractor.extract_with_passwords(
                    archive, extract_root,
                    candidate_passwords,
                    log_fn=self.log, progress_fn=_progress,
                    cancel_event=self.extract_cancel_event
                )
                if self.cancel_requested:
                    raise ArchiveExtractionCancelled("Archive extraction cancelled by user.")
                self.status_update("Scanning", "Extraction done — scanning for games…",
                                    "Scanning Files", 98, 98, "—", "—", "—")
                kind, paths = self._classify_extracted_payload(extracted_root, archive.name)
                if kind == "pfs":
                    # Tk vars are not thread-safe — marshal the .set() to the main loop.
                    self.root.after(0, lambda: self.unpack_mode_var.set(True))
                    if len(paths) == 1:
                        self.scan_q.put(("ok", GameItem.from_pfs_image(paths[0])))
                    else:
                        self.scan_q.put(("pfs_found", paths))
                elif kind == "disk":
                    if len(paths) == 1:
                        self.scan_q.put(("ok", GameItem.from_exfat(paths[0])))
                    else:
                        self.scan_q.put(("exfat_found", paths))
                elif len(paths) == 1:
                    try:
                        self.scan_q.put(("ok", GameItem(paths[0])))
                    except Exception as e:
                        self.scan_q.put(("error", str(e)))
                else:
                    self.scan_q.put(("multi_found", paths))
            except ArchiveExtractionCancelled as exc:
                self.log("WARN", str(exc))
                self.scan_q.put(("cancelled", str(exc)))
            except Exception as exc:
                self.log("ERROR", f"Extraction failed: {exc}")
                self.scan_q.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _candidate_passwords(self, item=None) -> list[str]:
        """Ordered, de-duplicated password candidates for an extraction:
        1) the explicit single-field password (left panel / settings),
        2) a per-archive override on the queue item (if any),
        3) every entry in the global auto-tried list.
        Blank entries dropped; first occurrence wins."""
        cands: list[str] = []
        explicit = self.password_var.get().strip()
        if explicit:
            cands.append(explicit)
        if item is not None:
            override = (getattr(item, "password", None) or "").strip()
            if override:
                cands.append(override)
        for p in self.archive_passwords:
            p = (p or "").strip()
            if p:
                cands.append(p)
        seen: set[str] = set()
        out: list[str] = []
        for p in cands:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    # ── Extract archive when it reaches the front of the queue ───────────────
    def _extract_queued_item(self, item):
        """Extract item.archive_path in a background thread, then call start() again."""
        archive = item.archive_path
        extract_root = self._resolve_extract_root(item)
        # Keep Spotlight off the scratch: indexing the tens of thousands of files we're
        # about to extract competes for the SAME (often mechanical) drive we then read
        # them back from — a big throttle. Mark the extract + temp dirs no-index first.
        self._mark_no_spotlight(extract_root, getattr(item, "_build_temp", None))

        self._active_item = item   # so terminal handlers can clean THIS item if the queue changed
        item.status = "Extracting"
        self.cancel_requested = False
        self.extract_cancel_event.clear()
        self.update_queue_box()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")

        self.log("INFO", f"Extracting archive: {archive.name}")
        self.status_update("Extracting", f"Unpacking {archive.name}…  0%",
                            "Extracting", 0, 0, "00:00", "—", "—")
        try:
            self.bottom_tabs.set("Logs")
        except Exception:
            pass

        _last_pct = [-1]
        def _progress(pct, filename):
            if pct - _last_pct[0] >= 2 or pct >= 100:
                _last_pct[0] = pct
                # Step bar shows the FULL extraction %; the queue bar (overall) only counts
                # extraction as the first ARCHIVE_EXTRACT_OVERALL_PCT% of this game — so the
                # two bars don't move in lockstep and the queue bar doesn't overshoot.
                self.status_update("Extracting",
                                    f"Unpacking {archive.name}…  {pct}%",
                                    "Extracting", pct, pct * ARCHIVE_EXTRACT_OVERALL_PCT / 100.0,
                                    "—", "—", "—")

        candidate_passwords = self._candidate_passwords(item)  # includes per-archive override

        def worker():
            try:
                extracted_root = ArchiveExtractor.extract_with_passwords(
                    archive, extract_root,
                    candidate_passwords,
                    log_fn=self.log, progress_fn=_progress,
                    cancel_event=self.extract_cancel_event
                )
                if self.cancel_requested:
                    raise ArchiveExtractionCancelled("Archive extraction cancelled by user.")
                kind, paths = self._classify_extracted_payload(extracted_root, archive.name)
                if kind == "pfs":
                    # Tk vars are not thread-safe — marshal the .set() to the main loop.
                    self.root.after(0, lambda: self.unpack_mode_var.set(True))

                payload_items = [self._item_from_payload_path(kind, path) for path in paths]
                primary = payload_items[0]
                self._copy_item_payload(item, primary)
                # Mark this as an extracted-archive item so the pack worker compresses its
                # overall progress into the tail after the extraction slice (monotonic
                # whole-game %). Underscore attr → not persisted in the saved queue.
                item._from_archive = True
                # Carry extra subfolders/files that sit BESIDE the game in the archive
                # (e.g. an '[ ALL DLC ]' wrapper) so they're copied next to the .ffpfsc.
                # Skipped when the archive root IS the game (no wrapper → no siblings).
                if kind == "game" and not (len(paths) == 1
                        and Path(paths[0]).resolve() == Path(extracted_root).resolve()):
                    try:
                        item.bundle_siblings = self._sibling_extras(Path(primary.path).parent, paths)
                    except Exception:
                        item.bundle_siblings = []
                self._extract_q.put(("ok", (item, payload_items[1:])))
            except ArchiveExtractionCancelled as exc:
                self.log("WARN", str(exc))
                self._extract_q.put(("cancelled", str(exc)))
            except Exception as exc:
                self.log("ERROR", f"Extraction failed: {exc}")
                self._extract_q.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    # ── Listbox keyboard reorder ──────────────────────────────────────────────
    def _lb_key_up(self, _event=None):
        self.queue_move_up()
        return "break"   # prevent default selection-navigation

    def _lb_key_down(self, _event=None):
        self.queue_move_down()
        return "break"

    # ── Queue selection helper ─────────────────────────────────────────────────
    def _queue_sel_idx(self) -> int | None:
        """Return the currently selected listbox index, or None."""
        sel = self.queue_listbox.curselection()
        return int(sel[0]) if sel else None

    def _on_queue_select(self, _event=None):
        """When a row is clicked, update the game details panel."""
        idx = self._queue_sel_idx()
        if idx is not None and idx < len(self.queue):
            self.update_game_details(self.queue[idx])

    # ── Queue management ──────────────────────────────────────────────────────
    def queue_move_up(self):
        idx = self._queue_sel_idx()
        if idx is None or idx == 0:
            return
        if self._batch_running and idx == 1:
            return  # can't move above the active game
        moved = self.queue[idx]                                   # capture before swap
        self.queue[idx], self.queue[idx - 1] = self.queue[idx - 1], self.queue[idx]
        self.update_queue_box(select_item=moved)                  # finds moved item at idx-1

    def queue_move_down(self):
        idx = self._queue_sel_idx()
        if idx is None or idx >= len(self.queue) - 1:
            return
        if self._batch_running and idx == 0:
            return  # can't move the active game
        moved = self.queue[idx]                                   # capture before swap
        self.queue[idx], self.queue[idx + 1] = self.queue[idx + 1], self.queue[idx]
        self.update_queue_box(select_item=moved)                  # finds moved item at idx+1

    def queue_remove_selected(self):
        idx = self._queue_sel_idx()
        if idx is None or not self.queue or idx >= len(self.queue):
            return  # nothing selectable (e.g. the "Queue is empty" placeholder row)
        # Don't allow removing the currently running game
        if self._batch_running and idx == 0:
            messagebox.showwarning("In Progress",
                                   "The first game in the queue is currently compressing.\n"
                                   "Cancel the compression first to remove it.")
            return
        # Decide which item to show after removal (next item, or previous if at end)
        if len(self.queue) > 1:
            next_item = self.queue[idx + 1] if idx + 1 < len(self.queue) else self.queue[idx - 1]
        else:
            next_item = None
        self.queue.pop(idx)
        self.update_queue_box(select_item=next_item)

    def remove_first(self):
        """Legacy helper — removes the first (non-running) queue entry."""
        if self.queue and not self._batch_running:
            self.queue.pop(0)
        self.update_queue_box()

    def clear_queue(self):
        if self._batch_running:
            ok = messagebox.askyesno("Clear Queue",
                                      "A compression is running. Clear the waiting games?\n"
                                      "(The current game will finish normally.)")
            if not ok:
                return
            self.queue[1:] = []   # keep index-0 (running game), clear the rest
        else:
            self.queue.clear()
        self.update_queue_box()

    # GameItem fields that hold a Path (everything else is str/int/None and JSON-safe).
    _QUEUE_PATH_FIELDS = ("path", "archive_path", "bundle_dir", "patch_source")

    def _save_queue(self):
        """Persist the current queue (paths + metadata, minus the PIL artwork and the
        transient _-prefixed placement state) so it survives a restart/crash. Gated until
        _restore_queue runs so the initial empty render can't clobber the saved queue.
        Best-effort — never breaks the UI rebuild."""
        if not getattr(self, "_queue_restored", False):
            return
        try:
            items = []
            for it in self.queue:
                d = {}
                for k, v in vars(it).items():
                    if k.startswith("_") or k == "artwork":
                        continue
                    if isinstance(v, Path):
                        d[k] = str(v)
                    elif k == "bundle_siblings":
                        d[k] = [str(s) for s in (v or [])]
                    elif v is None or isinstance(v, (str, int, float, bool)):
                        d[k] = v
                    # anything else (unexpected) is dropped rather than risk a crash
                items.append(d)
            save_settings({"queue": items})
        except Exception:
            pass

    def _restore_queue(self):
        """Rebuild the saved queue on startup. Items whose source path no longer exists
        are skipped; a mid-run status is reset to pending. Best-effort. Enables saving
        afterwards (sets _queue_restored)."""
        saved = []
        try:
            saved = load_settings().get("queue") or []
        except Exception:
            saved = []
        restored = skipped = 0
        for d in saved:
            try:
                if not isinstance(d, dict):
                    skipped += 1
                    continue
                obj = GameItem.__new__(GameItem)
                for k, v in d.items():
                    if k in self._QUEUE_PATH_FIELDS and v:
                        setattr(obj, k, Path(v))
                    elif k == "bundle_siblings":
                        setattr(obj, k, [Path(s) for s in (v or [])])
                    else:
                        setattr(obj, k, v)
                # The source must still be on disk to be packable/unpackable.
                probe = getattr(obj, "archive_path", None) or getattr(obj, "path", None)
                if not probe or not Path(probe).exists():
                    skipped += 1
                    continue
                obj.artwork = None   # re-detected lazily when the row is selected
                if getattr(obj, "status", "") in ("Running", "Extracting", "Patching"):
                    obj.status = "Pending Extract" if getattr(obj, "archive_path", None) else "Queued"
                self.queue.append(obj)
                restored += 1
            except Exception:
                skipped += 1
        self._queue_restored = True   # from here on, queue mutations persist
        if self.queue:
            self.update_queue_box()
        if restored:
            self.log("INFO", f"Restored {restored} item(s) from the saved queue.")
        if skipped:
            self.log("WARN", f"Saved queue: {skipped} item(s) skipped (source path missing/invalid).")

    def update_queue_box(self, select_item=None):
        """Rebuild the listbox.

        select_item: if given, that GameItem will be highlighted after the
        rebuild (used by move-up/down so the correct item is tracked even
        though the listbox selection is stale).  When omitted the previously
        selected item is looked up by object identity; falls back to row 0.
        """
        self._save_queue()   # persist the (just-mutated) queue across restarts
        # Decide which item to keep selected
        if select_item is None:
            prev_idx  = self._queue_sel_idx()
            select_item = (self.queue[prev_idx]
                           if prev_idx is not None and prev_idx < len(self.queue)
                           else None)

        self.queue_listbox.delete(0, "end")
        if not self.queue:
            self.queue_listbox.insert("end", "  Queue is empty")
            self.queue_listbox.itemconfig(0, fg="#555555")
            self.queue_total_var.set("Total: 0 game(s)")
            self._details_item = None
            return

        total = sum(x.size for x in self.queue)
        for i, item in enumerate(self.queue):
            prefix = "▶ " if (self._batch_running and i == 0) else f"{i + 1}. "
            op = "UNPACK" if getattr(item, "operation", "pack") == "unpack" else "PACK"
            # Archives store the COMPRESSED set size in .size; show the EXTRACTED size
            # (what space/placement actually use), tagged with ~ as a header estimate.
            _szs = (f"~{format_size(display_size(item))} unpacked"
                    if shows_extracted_size(item) else format_size(item.size))
            line = f"{prefix}{op}  {item.title_id}  {item.name}  [{_szs}]  {item.status}"
            self.queue_listbox.insert("end", line)
            if self._batch_running and i == 0:
                self.queue_listbox.itemconfig(i, fg="#4ade80")
            elif item.status == "Failed":
                self.queue_listbox.itemconfig(i, fg="#f87171")
            elif item.status == "Done":
                self.queue_listbox.itemconfig(i, fg="#888888")

        self.queue_total_var.set(f"Total: {len(self.queue)} item(s)  |  {format_size(total)}")

        # Find the target item's new index; fall back to row 0
        try:
            sel = self.queue.index(select_item) if select_item in self.queue else 0
        except (ValueError, TypeError):
            sel = 0
        self.queue_listbox.selection_set(sel)
        self.queue_listbox.see(sel)

        # Only refresh the details panel when the selected item actually changed.
        # Using `is` (reference equality) is safe here: we hold _details_item as a
        # real reference so Python cannot reuse the address while it lives in the queue.
        sel_item = self.queue[sel]
        if sel_item is not self._details_item:
            self.update_game_details(sel_item)

    def update_game_details(self, item):
        self._details_item = item   # record before any call that might raise
        self.game_name_var.set(f"Name: {item.name}")
        mode = "Unpack" if getattr(item, "operation", "pack") == "unpack" else "Pack"
        self.title_var.set(f"Title ID: {item.title_id}  |  Mode: {mode}")
        self.source_detail_var.set(f"Source: {item.path}")
        if shows_extracted_size(item):
            self.orig_var.set(f"Original Size: ~{format_size(item.extracted_size)} unpacked  "
                              f"({format_size(item.size)} packed)")
        else:
            self.orig_var.set(f"Original Size: {format_size(item.size)}")
        self.files_var.set(f"Files: {item.files:,}")
        self.load_art(item.artwork)
        self._refresh_space_for_item(item)
        self.update_command_preview()

    def _refresh_space_for_item(self, item=None):
        """Recalculate free-space vs what this game needs and update the stats label."""
        if item is None:
            item = self.queue[0] if self.queue else None
        if item is None or getattr(item, "size", 0) == 0:
            self.temp_space_var.set("Temp Needed: —")
            return
        if getattr(item, "operation", "pack") == "unpack":
            try:
                op = self.output_var.get().strip()
                out_dir = Path(op) if op else None
                out_free = get_free_space(out_dir) if out_dir else 0
                self.temp_space_var.set(
                    f"Extract Needs: ~{format_size(item.size)}+  |  Out Free: {format_size(out_free)}"
                )
            except Exception:
                self.temp_space_var.set(f"Extract Needs: ~{format_size(item.size)}+")
            return
        try:
            tp = self.temp_var.get().strip()
            op = self.output_var.get().strip()
            temp_dir = Path(tp) if tp else None
            out_dir  = Path(op) if op else None
            if temp_dir is None:
                self.temp_space_var.set(f"Peak Needed: ~{format_size(int(_build_size_of(item) * _peak_factor_for(item)))}")
                return
            same   = same_drive(temp_dir, out_dir) if out_dir else True
            size   = _build_size_of(item)
            factor = _peak_factor_for(item)
            free   = get_free_space(temp_dir)
            out_free = get_free_space(out_dir) if out_dir else 0
            image_need = estimate_image_space_needed(size)     # just the inner image on the SSD
            out_full   = estimate_peak_space_needed(size, factor, True)
            # It completes if the SSD can hold the image (split — the spool auto-routes to
            # the big output drive) OR the output drive can hold the whole build.
            ok = (size > 0 and free >= image_need) or (out_dir is not None and out_free >= out_full)
            flag = "✓ OK" if ok else "⚠ LOW"
            self.temp_space_var.set(
                f"Temp image: ~{format_size(image_need)}  |  Temp Free: {format_size(free)}  "
                f"|  Out Free: {format_size(out_free)}  |  {flag}"
            )
        except Exception:
            self.temp_space_var.set(f"Peak Needed: ~{format_size(item.size * 2.2)}")

    def _update_format_label(self):
        comp = self.output_compressed_var.get()
        try:
            self.format_hint_var.set("📦 Compressed (.ffpfsc, smaller)" if comp
                                     else "⚡ Uncompressed (.ffpfs, faster)")
        except Exception:
            pass

    def _on_format_toggle(self):
        """Output format changed — applies to the WHOLE queue. Refresh the hint and the
        command/queue preview so output names/extensions reflect the chosen format."""
        self._update_format_label()
        for fn in ("update_queue_box", "update_command_preview"):
            try:
                getattr(self, fn)()
            except Exception:
                pass
        self.log("INFO", "Output format set to "
                 + ("compressed .ffpfsc (smaller)" if self.output_compressed_var.get()
                    else "uncompressed .ffpfs (faster to build and mount; full size)."))

    def update_command_preview(self):
        item = self.queue[0] if self.queue else None
        # Archive placeholders have no path yet — show a friendly message instead
        if item and getattr(item, "archive_path", None):
            self.command_label.configure(
                text=f"📦 {item.name} — archive will be extracted before compression starts.")
            return
        src = self.source_var.get().strip()
        if not item and src and Path(src).exists():
            p = Path(src)
            pycmd = get_backend_python_command() or ["python"]
            is_unpack = p.suffix.lower() == ".ffpfsc" or self.unpack_mode_var.get()
            if getattr(sys, "frozen", False):
                cmd = pycmd + [str(p), self.output_var.get().strip() or str(p.parent), "--overwrite"]
            else:
                cmd = pycmd + ["-u", str(backend_base_dir() / "cli.py"), str(p),
                               self.output_var.get().strip() or str(p.parent), "--overwrite"]
            if is_unpack:
                cmd.append("--unpack")
        elif item:
            try:
                cmd, _, _, _ = self.build_command(item)
            except Exception:
                self.command_label.configure(text="Select output and temp folder to preview command.")
                return
        else:
            self.command_label.configure(text="Select source, output, and temp folder to preview command.")
            return
        self.command_label.configure(text=" ".join(f'"{x}"' if " " in x else x for x in cmd))

    def build_command(self, item):
        out = Path(self.output_var.get().strip())
        # Give every pack job a findable, descriptive, collision-resistant output name
        # "<Game> [v<ver>] [<TITLEID>].ffpfsc" when the user picked an output FOLDER.
        # The backend honours an explicit .ffpfsc path (it only auto-names by title id
        # when handed a directory) — so naming by title id alone would let two queued
        # games with the same title id overwrite each other in a batch. An explicit
        # .ffpfsc the user typed is respected as-is.
        op = getattr(item, "operation", "pack")
        # Output format (whole-queue toggle): uncompressed .ffpfs ONLY for the PFS family —
        # a game folder or a .ffpfs source. .exfat/.ffpkg disk images are always compressed
        # to .ffpfsc (the backend ignores --no-compress for them). .ffpfs is faster to build
        # (no pass 2) and to mount (no decompression), at full size.
        try:
            _ip = Path(getattr(item, "path", "") or "")
            src_pfs_family = _ip.is_dir() or _ip.suffix.lower() == ".ffpfs"
        except Exception:
            src_pfs_family = False
        self._cmd_uncompressed = (op == "pack") and src_pfs_family and not self.output_compressed_var.get()
        out_ext = ".ffpfs" if self._cmd_uncompressed else ".ffpfsc"
        explicit_file = out.suffix.lower() in (".ffpfsc", ".ffpfs")
        sub = getattr(item, "bundle_subfolder", None)
        if op == "pack" and not explicit_file:
            # Bundle: recreate the source folder under the output dir. Bundle
            # naming applies even in batch (each item has its own subfolder).
            base = (out / sanitize_filename(sub)) if sub else out
            try:
                out = base / descriptive_ffpfsc_name(item, ext=out_ext)
                if getattr(item, "_name_was_truncated", False):
                    self.log("WARN", f"Output filename shortened to fit the {SHADOWMOUNT_NAME_LIMIT}-"
                                     f"byte ShadowMount limit: {out.name}")
                elif getattr(item, "_name_fluff_stripped", False):
                    self.log("INFO", f"Dropped edition suffix to fit the {SHADOWMOUNT_NAME_LIMIT}-"
                                     f"byte ShadowMount limit: {out.name}")
            except Exception:
                out = base   # keep the subfolder; backend names <title_id><ext> inside
        temp = Path(self.temp_var.get().strip())
        backend = backend_base_dir()
        cli_py = Path("backend") / "cli.py"  # macOS-ready pathlib form
        cli_py = backend / "cli.py"
        pycmd = get_backend_python_command()
        if not pycmd:
            raise RuntimeError("Python was not found. Install Python, or run the app from source.")
        if getattr(sys, "frozen", False):
            cmd = pycmd + [str(item.path), str(out)]
        else:
            cmd = pycmd + ["-u", str(cli_py), str(item.path), str(out)]
        if getattr(item, "operation", "pack") == "unpack":
            if out.exists() and out.is_dir():
                out = out / f"{item.path.stem}_extracted"
                if getattr(sys, "frozen", False):
                    cmd = pycmd + [str(item.path), str(out)]
                else:
                    cmd = pycmd + ["-u", str(cli_py), str(item.path), str(out)]
            cmd += ["--unpack", "--overwrite"]
            # Converter "decompress one level" (.ffpfsc → inner .ffpfs): stop unwrapping at
            # the first nested image instead of recursing all the way to a folder.
            if getattr(item, "unwrap", True) is False:
                cmd.append("--no-unwrap")
            return cmd, backend, out, temp
        if getattr(self, "_cmd_uncompressed", False):
            cmd.append("--no-compress")   # emit uncompressed .ffpfs (skip pass-2)
        if self.batch_var.get():
            cmd.append("--batch")
        if self.keep_pfs_var.get():
            cmd.append("--keep-pfs")
        if self.verify_output_var.get():
            cmd.append("--verify")
        # MkPFS 0.0.8 tuning
        comp_level = self.compression_level_var.get()
        if comp_level != 7:  # only pass if non-default
            cmd += ["--compression-level", str(comp_level)]
        # An OOM auto-retry pins an explicit (lower) core count on the item — honour it
        # over the global setting so the retry actually uses fewer mkpfs workers.
        cpu = getattr(item, "_cpu_retry_override", None)
        if cpu is None:
            cpu = self.cpu_count_var.get()
        if cpu:
            cmd += ["--cpu-count", str(cpu)]
        block_size = self.block_size_var.get()
        if block_size and block_size != "auto":
            cmd += ["--block-size", block_size]
        if self.verbose_var.get():
            cmd.append("--verbose")
        # Route the backend scratch (inner image + pass-2 spool) to the SAME drive this
        # run was placed on (item._build_temp), so ALL artifacts follow one drive. Folder
        # and folder+patch jobs are never extracted, so resolve their placement here (the
        # PATCH factor applies for auto-patch jobs); archives carry _build_temp from
        # extraction / the post-extraction re-gate. Falls back to the user temp folder.
        patch_src = getattr(item, "patch_source", None)
        is_patch_job = (bool(patch_src) and self.auto_integrate_patch_var.get()
                        and bool(getattr(item, "path", None)) and item.path.is_dir())
        if getattr(item, "_build_temp", None) is None:
            try:
                self._resolve_extract_root(item)   # sets item._build_temp via the right factor
            except Exception:
                pass
        build_temp = getattr(item, "_build_temp", None)
        if build_temp is not None:
            temp = Path(build_temp)
        temp_str = str(temp) if str(temp) else ""
        if temp_str:
            cmd += ["--temp-dir", temp_str]
        # Spill target for the pass-2 spool when --temp-dir can't hold image+spool: the
        # OUTPUT ROOT (a big drive). The backend writes the spool under <root>/_ffpfsc_temp,
        # which the startup sweep and failure cleanup already reclaim. Lets a big game keep
        # its inner image on the fast SSD instead of falling entirely onto the HDD.
        out_root = self.output_var.get().strip()
        if out_root:
            cmd += ["--spool-fallback-dir", out_root]
        # Auto-patch: overlay a detected patch sibling onto the game before packing,
        # via the backend's PATCH MODE (now routed to the chosen --temp-dir above).
        if is_patch_job:
            cmd += ["--patch", str(patch_src)]
        # Opt-in exFAT workflow: only for a plain folder pack (not patch jobs, not a
        # disk-image source). The backend builds an exFAT image of the folder and
        # compresses that instead of running the folder PFS builder.
        if (self.build_via_exfat_var.get() and not is_patch_job
                and getattr(item, "operation", "pack") == "pack"
                and getattr(item, "path", None) and Path(item.path).is_dir()):
            cmd.append("--via-exfat")
        cmd.append("--overwrite")
        return cmd, backend, out if out.suffix.lower() not in (".ffpfsc", ".ffpfs") else out.parent, temp

    def _run_cleanup(self, work) -> None:
        """Run a cleanup function in a daemon thread, counted so the batch can wait for
        all in-flight reclaims to finish before re-reading free space (a boolean cannot
        represent N concurrent rmtrees)."""
        with self._cleanup_lock:
            self._cleanup_inflight += 1
        def _wrapped():
            try:
                work()
            finally:
                with self._cleanup_lock:
                    self._cleanup_inflight = max(0, self._cleanup_inflight - 1)
        threading.Thread(target=_wrapped, daemon=True).start()

    # ── AMPR / APR (PlayGo) support ───────────────────────────────────────────
    def _ampr_folder(self):
        """The configured folder holding the two emu .sprx files, or None."""
        try:
            p = self.ampr_var.get().strip()
        except Exception:
            p = ""
        return Path(p) if p and Path(p).is_dir() else None

    def _ensure_ampr_folder(self) -> bool:
        """Ensure the AMPR emu folder is set; prompt once if not. True when ready."""
        if self._ampr_folder():
            return True
        result = [False]
        win = ctk.CTkToplevel(self.root)
        win.title("AMPR Emu Files Needed")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.lift()
        win.after(200, lambda: win.attributes("-topmost", False))
        ctk.CTkLabel(win, text="AMPR emu folder not set",
                     font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=WHITE).pack(padx=28, pady=(22, 4))
        ctk.CTkLabel(win,
                     text="This APR (PlayGo) game needs two emu files to boot after compression:\n"
                          "  • libSceAmpr.sprx\n"
                          "  • libScePlayGo.sprx\n\n"
                          "Point to the folder that contains both. They are copied into a\n"
                          "fakelib/ folder inside the game before packing, and an\n"
                          "ampr_emu.index is built. (Stored in Settings — asked only once.)",
                     font=ctk.CTkFont(size=12), text_color=MUTED, justify="left").pack(padx=28, pady=(0, 14))
        path_var = tk.StringVar(value="")
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=28, pady=(0, 16))
        ctk.CTkEntry(row, textvariable=path_var, width=300,
                     placeholder_text="Folder containing libSceAmpr.sprx…").pack(side="left", padx=(0, 8))
        def _browse():
            from tkinter import filedialog
            chosen = filedialog.askdirectory(title="Select AMPR Emu Folder")
            if chosen:
                path_var.set(chosen)
        self._button(row, "Browse", _browse, width=80, height=32).pack(side="left")
        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(pady=(0, 22))
        def _confirm():
            p = path_var.get().strip()
            if p:
                self.ampr_var.set(p)
                save_settings({"ampr_folder": p})
                result[0] = True
            win.destroy()
        self._button(btns, "Confirm & Continue", _confirm, green=True, width=190, height=36).pack(side="left", padx=(0, 10))
        self._button(btns, "Skip (no AMPR)", win.destroy, width=140, height=36).pack(side="left")
        win.grab_set()
        self.root.wait_window(win)
        return result[0]

    def _inject_ampr_files(self, item) -> None:
        """Copy the two emu .sprx into <game>/fakelib/. Tracks injected paths on the item
        so a DIRECT (non-archive) source folder can be cleaned up after packing."""
        ampr_dir = self._ampr_folder()
        if not ampr_dir or not item.path or not getattr(item, "ampr_emu", False):
            return
        target_dir = Path(item.path) / "fakelib"
        target_dir.mkdir(exist_ok=True)
        item._ampr_injected = []
        for fname in AMPR_SPRX_FILES:
            src, dst = ampr_dir / fname, target_dir / fname
            if not src.exists():
                self.log("WARN", f"AMPR: {fname} not found in {ampr_dir}")
                continue
            if dst.exists():
                self.log("INFO", f"AMPR: {fname} already present — skipping injection")
                continue
            try:
                shutil.copy2(src, dst)
                item._ampr_injected.append(dst)
                self.log("INFO", f"AMPR: injected {fname}")
            except Exception as exc:
                self.log("WARN", f"AMPR: failed to inject {fname}: {exc}")

    def _build_ampr_index(self, item) -> None:
        """Build ampr_emu.index (AMPRIDX3) in the game folder. Ported byte-exact from the
        reference tool: header <8sIIQQQII>, records <IIQq>, FNV-1a-64 open-addressed slots
        <QII>, /app0/-prefixed lowercased POSIX paths, atomic temp-rename write."""
        if not getattr(item, "ampr_emu", False) or not item.path or not Path(item.path).is_dir():
            return
        import struct as _struct
        root       = Path(item.path).resolve()
        output     = root / "ampr_emu.index"
        output_tmp = output.with_suffix(output.suffix + ".tmp")

        def _key(p):
            return p.replace("\\", "/").lower()

        def _fnv(p):
            h = 1469598103934665603
            for ch in _key(p):
                h ^= ord(ch)
                h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
            return h or 1

        def _make_slots(rows):
            n = 2
            while n < len(rows) * 2:
                n <<= 1
            table = [(0, 0, 0)] * n
            mask = n - 1
            for i, (_, _, path) in enumerate(rows):
                h = _fnv(path)
                pos = h & mask
                while table[pos][1] != 0:
                    if table[pos][0] == h:
                        oh, oi, of_ = table[pos]
                        table[pos] = (oh, oi, of_ | 1)
                    pos = (pos + 1) & mask
                table[pos] = (h, i + 1, 0)
            return table

        def _write(rows):
            rec_s = _struct.Struct("<IIQq")
            slt_s = _struct.Struct("<QII")
            hdr_s = _struct.Struct("<8sIIQQQII")
            rows = sorted(rows, key=lambda r: _key(r[2]))
            blob = bytearray()
            recs = bytearray()
            for sz, mt, path in rows:
                enc = path.encode("utf-8") + b"\0"
                recs += rec_s.pack(len(blob), len(enc) - 1, sz, mt)
                blob += enc
            table = _make_slots(rows)
            p_end = hdr_s.size + len(recs) + len(blob)
            h_off = (p_end + (slt_s.size - 1)) & ~(slt_s.size - 1)
            with output_tmp.open("wb") as f:
                f.write(hdr_s.pack(b"AMPRIDX3", 3, rec_s.size, len(rows),
                                   len(blob), h_off, slt_s.size, len(table)))
                f.write(recs)
                f.write(blob)
                f.write(b"\0" * (h_off - p_end))
                for h, ip1, fl in table:
                    f.write(slt_s.pack(h, ip1, fl))
            output_tmp.replace(output)

        out_r, tmp_r = output.resolve(), output_tmp.resolve()
        _SKIP = {_key("/app0/ampr_emu.index"), _key("/app0/ampr_emu.index.tmp")}
        seen, rows = {}, []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort(key=str.lower)
            filenames.sort(key=str.lower)
            for fname in filenames:
                fpath = Path(dirpath) / fname
                try:
                    if not fpath.is_file() or fpath.resolve() in (out_r, tmp_r):
                        continue
                    ipath = "/app0/" + fpath.relative_to(root).as_posix()
                    ikey = _key(ipath)
                    if ikey in _SKIP or ikey in seen:
                        continue
                    seen[ikey] = ipath
                    st = fpath.stat()
                    rows.append((st.st_size, int(st.st_mtime), ipath))
                except Exception as exc:
                    self.log("WARN", f"AMPR index: skipping {fpath.name}: {exc}")
        try:
            _write(rows)
            item._ampr_index_path = output
            self.log("INFO", f"AMPR: built index → ampr_emu.index  ({len(rows):,} files)")
        except Exception as exc:
            self.log("WARN", f"AMPR: index build failed: {exc}")

    def _prepare_ampr(self, item) -> None:
        """Right before packing an APR/PlayGo folder: ensure the emu folder, inject the
        .sprx into fakelib/, and build the index. No-op for non-APR games / disk images."""
        if not getattr(item, "ampr_emu", False) or not getattr(item, "path", None):
            return
        self.log("INFO", f"AMPR: {item.name} is a PlayGo/APR title — preparing emu files.")
        if not self._ensure_ampr_folder():
            self.log("WARN", "AMPR: no emu folder set — packing WITHOUT AMPR support; this "
                             "APR title may not boot until you set the folder in Settings.")
            return
        self._inject_ampr_files(item)
        self._build_ampr_index(item)

    def _ampr_cleanup(self, item) -> None:
        """Remove emu files we injected into a DIRECT (non-archive) source folder — that's
        the user's own library, so restore it after packing. Archive-extracted sources sit
        in temp and are removed wholesale by the normal teardown, so they're left alone."""
        if item is None or getattr(item, "_from_archive", False):
            return
        injected = getattr(item, "_ampr_injected", None)
        idx = getattr(item, "_ampr_index_path", None)
        if not injected and not idx:
            return
        for p in (injected or []):
            try:
                Path(p).unlink()
            except Exception:
                pass
        try:
            fl = Path(item.path) / "fakelib"
            if fl.is_dir() and not any(fl.iterdir()):
                fl.rmdir()
        except Exception:
            pass
        if idx:
            try:
                Path(idx).unlink()
            except Exception:
                pass
        item._ampr_injected = []
        item._ampr_index_path = None
        self.log("INFO", f"AMPR: cleaned injected emu files from {Path(item.path).name}.")

    def _oom_retry(self, item) -> bool:
        """Requeue *item* with one fewer mkpfs worker after an out-of-memory kill.
        Returns True if a retry was scheduled (caller should NOT count this as a failure).
        Step-down: an explicit N → N-1 → … → 1; AUTO (0) drops straight to 1 worker (the
        backend already auto-capped it, so 1 is the only guaranteed reduction). Capped at
        two retries; gives up at one worker."""
        MAX_RETRIES = 2
        tries = getattr(item, "_oom_retries", 0)
        prev = getattr(item, "_cpu_retry_override", None)
        if prev is not None:
            new_cpu = prev - 1
        else:
            base = self.cpu_count_var.get()
            new_cpu = (base - 1) if (base and base > 0) else 1
        new_cpu = max(1, new_cpu)
        if tries >= MAX_RETRIES or (prev is not None and new_cpu >= prev):
            self.log("ERROR", f"Still out of memory at {new_cpu} core(s) — giving up on "
                              f"{item.name}. Try a lower compression level or smaller block size.")
            return False
        item._cpu_retry_override = new_cpu
        item._oom_retries = tries + 1
        item.status = "Pending"
        self._cleanup_after_failure(item)   # reclaim the failed run's partial scratch first
        self.queue.insert(0, item)
        self.update_queue_box()
        self.log("WARN", f"Out of memory — retrying {item.name} with {new_cpu} CPU core(s) "
                         f"(attempt {tries + 1}/{MAX_RETRIES}).")
        self.status_update("Retrying", f"Out of memory — retrying with {new_cpu} core(s)…",
                           "Retrying", 0, 0, "—", "—", "—")
        self._batch_running = True   # keep the loop alive even for a single-game run
        self.root.after(800, self._batch_auto_start)
        return True

    def _cleanup_after_failure(self, item) -> None:
        """Reclaim a failed/skipped/cancelled run's scratch: the mkpfs tmp* working dirs
        (orphaned inner image + pass-2 spool) AND this item's own extracted-source subdir
        — scoped to the drive the run actually built on (item._build_temp), the user temp
        folder, AND the output drive's _ffpfsc_temp (where a SPLIT run may have spilled its
        pass-2 spool, ffpfsc_spool_*). Counted so the next batch gate waits for the reclaim.
        Threaded (rmtree of a ~150 GB tree must not freeze the UI)."""
        self._ampr_cleanup(item)   # restore a direct source folder we injected emu files into
        roots, seen = [], set()
        out_str = self.output_var.get().strip()
        out_spool = (str(Path(out_str) / "_ffpfsc_temp") if out_str else None)
        for cand in (getattr(item, "_build_temp", None), self.temp_var.get().strip(), out_spool):
            if not cand:
                continue
            r = Path(cand)
            try:
                key = str(r.resolve())
            except Exception:
                key = str(r)
            if key not in seen and r.exists():
                seen.add(key)
                roots.append(r)
        if not roots and self._extract_dir_for_item(item) is None:
            return

        def _work():
            freed = 0
            for root in roots:
                try:
                    for p in root.iterdir():
                        if p.is_dir() and (_is_app_tmp_dir(p.name) or p.name.startswith("ffpfsc_spool_")):
                            sz = get_folder_size(p)
                            shutil.rmtree(str(p), ignore_errors=True)
                            freed += sz
                except Exception:
                    pass
            try:
                own = self._extract_dir_for_item(item)   # temp/_extracted OR output/_ffpfsc_extract
                if own is not None and own.exists():
                    sz = get_folder_size(own)
                    shutil.rmtree(str(own), ignore_errors=True)
                    freed += sz
                    if own.parent.name == "_ffpfsc_extract":
                        try:
                            if not any(own.parent.iterdir()):
                                own.parent.rmdir()
                        except Exception:
                            pass
            except Exception:
                pass
            if freed:
                self.log("INFO", f"Cleaned {format_size(freed)} of scratch from the failed/skipped run.")

        self._run_cleanup(_work)

    # ── Feature 5: Auto-clear temp ────────────────────────────────────────────
    def _offer_startup_sweep(self):
        """On launch, look for orphaned scratch from a crashed/cancelled run — tmp* and
        _extracted on the temp drive, plus _ffpfsc_temp / _ffpfsc_extract on the output
        drive — and offer to reclaim it. Sizing walks large trees, so it runs in a
        background thread; the confirm prompt + delete are marshalled to the main thread.
        Only this app's own working dirs are ever touched."""
        NAMES = ("_extracted", "_ffpfsc_extract", "_ffpfsc_temp")

        def _scan():
            targets, seen = [], set()
            for base_str in (self.temp_var.get().strip(), self.output_var.get().strip()):
                if not base_str:
                    continue
                base = Path(base_str)
                if not base.exists():
                    continue
                try:
                    for p in base.iterdir():
                        if p.is_dir() and (_is_app_tmp_dir(p.name) or p.name in NAMES):
                            try:
                                key = str(p.resolve())
                            except Exception:
                                key = str(p)
                            if key not in seen:
                                seen.add(key)
                                targets.append(p)
                except Exception:
                    continue
            total = 0
            for p in targets:
                try:
                    total += get_folder_size(p)
                except Exception:
                    pass
            if targets and total >= 1 * 1024**3:   # ignore trivial (<1 GiB) leftovers
                self.root.after(0, lambda: self._prompt_startup_sweep(targets, total))

        threading.Thread(target=_scan, daemon=True).start()

    def _prompt_startup_sweep(self, targets, total):
        try:
            n = len(targets)
            listing = "\n".join(f"  • {p}" for p in targets[:8]) + ("\n  • …" if n > 8 else "")
            msg = (f"Found ~{format_size(total)} of leftover working data from a previous "
                   f"run in {n} folder(s):\n\n{listing}\n\nDelete it now to reclaim the space?")
            if not messagebox.askyesno("Reclaim leftover temp space?", msg):
                return

            def _work():
                freed = 0
                for p in targets:
                    try:
                        sz = get_folder_size(p)
                        shutil.rmtree(str(p), ignore_errors=True)
                        freed += sz
                    except Exception:
                        pass
                if freed:
                    self.log("OK", f"Startup sweep: reclaimed {format_size(freed)} of leftover scratch.")
            self._run_cleanup(_work)
            self.log("INFO", "Reclaiming leftover scratch in the background…")
        except Exception:
            pass

    def _auto_clear_temp(self):
        """Silently clear temp folder contents after a successful compression."""
        tp = self.temp_var.get().strip()
        if not tp:
            return
        temp_dir = Path(tp)
        if not temp_dir.exists():
            return
        freed = 0
        errors = 0
        for p in list(temp_dir.iterdir()):
            # Only remove THIS app's working data (mkpfs tmp* dirs and the _extracted
            # tree); never wipe unrelated files a user may keep in their temp folder.
            if not (_is_app_tmp_dir(p.name) or p.name == "_extracted"):
                continue
            try:
                sz = get_folder_size(p) if p.is_dir() else (p.stat().st_size if p.is_file() else 0)
                if p.is_dir():
                    shutil.rmtree(str(p), ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
                freed += sz
            except Exception:
                errors += 1
        msg = f"🗑  Auto-cleared temp: freed {format_size(freed)}"
        if errors:
            msg += f" ({errors} item(s) could not be removed)"
        self.log("OK", msg)

    # ── Feature 4: Batch auto-advance ─────────────────────────────────────────
    def _update_batch_counter(self):
        if not self._batch_running:
            self.batch_counter_var.set("")
            return
        current = self._batch_done + self._batch_failed + 1
        self.batch_counter_var.set(
            f"Game {current}/{self._batch_total}  |  ✓ {self._batch_done}  ✗ {self._batch_failed}"
        )

    def _space_gate(self, item, out_dir):
        """Place the run on a drive sized for its REAL footprint (sets item._build_root /
        _build_temp via _resolve_extract_root), then decide go/skip/cancel against THAT
        drive. Returns 'proceed', 'skip', or 'cancel' per free space, the low-space policy
        (ask / auto / skip) and the diagnostics-dialog toggle. Honest extracted size +
        per-kind factor + safety factor feed the check, so a game that fits a drive (the
        big HDD) is routed there and proceeds; only a game that fits NO drive is skipped."""
        if getattr(item, "operation", "pack") == "unpack":
            return "proceed"
        out_dir = Path(out_dir)
        try:
            self._resolve_extract_root(item)   # the single place that picks the build drive
        except Exception as e:
            self.log("WARN", f"Placement failed ({e}); using temp.")
        temp_dir = Path(getattr(item, "_build_temp", None)
                        or (self.temp_var.get().strip() or str(Path.home())))
        try:
            ok = _space_preflight_ok(item, temp_dir, out_dir)
        except Exception as e:
            self.log("WARN", f"Space pre-check skipped: {e}")
            return "proceed"

        def _ask():
            diag = SpaceDiagnosticsDialog(self.root, item, temp_dir, out_dir)
            self.root.wait_window(diag)
            return "proceed" if diag.proceed else "cancel"

        if ok:
            return _ask() if self.show_space_dialog_var.get() else "proceed"
        policy = (load_settings().get("low_space_policy", "ask") or "ask").lower()
        if policy == "auto":
            self.log("WARN", f"Low space — proceeding anyway (policy: auto): {item.name}")
            return "proceed"
        if policy == "skip":
            self.log("WARN", f"Low space — {item.name} fits no available drive; "
                             f"skipping (policy: skip).")
            return "skip"
        return _ask()   # 'ask' — show the dialog and let the user decide

    def _batch_auto_start(self):
        """Start the next game in the queue — rechecks disk space before each game."""
        # Honor a cancel requested during the 600 ms advance gap (cancel_requested is
        # reset below, so a dropped cancel would otherwise silently keep the batch going).
        if self.cancel_requested or self.extract_cancel_event.is_set():
            self._batch_running = False
            self.cancel_requested = False
            self.extract_cancel_event.clear()
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            self.status_update("Ready", "Batch cancelled.", "Ready", 0, 0, "00:00", "—", "—")
            self.log("WARN", "Batch cancelled by user.")
            return
        # Wait for any in-flight scratch reclaim to finish before re-reading free space —
        # otherwise the gate sees a still-full drive and false-skips the next game. Cap the
        # wait generously (~10 min) for a slow exFAT rmtree of a 150-260 GB tree.
        if getattr(self, "_cleanup_inflight", 0) > 0:
            self._cleanup_wait_ticks += 1
            if self._cleanup_wait_ticks <= 1200:   # 1200 * 500 ms ≈ 10 min
                if self._cleanup_wait_ticks == 1:
                    self.status_update("Cleaning up", "Reclaiming temp space before the next game…",
                                        "Cleaning", 0, 0, "—", "—", "—")
                self.root.after(500, self._batch_auto_start)
                return
            self.log("WARN", "Cleanup still running past the wait cap — continuing; the space gate decides.")
        self._cleanup_wait_ticks = 0
        if not self.queue:
            self._batch_running = False
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            return
        item = self.queue[0]
        self.update_game_details(item)   # refreshes art + space stats for next game

        # ── Space pre-flight gate — places the run on a drive sized for its real
        #    footprint, then go/skip/cancel. Skip cleans any partial scratch and keeps
        #    the batch moving; a big game that fits the HDD is routed there, not skipped.
        try:
            op = self.output_var.get().strip()
            if op and getattr(item, "operation", "pack") != "unpack":
                od = Path(op)
                gate = self._space_gate(item, od)
                bt = getattr(item, "_build_temp", None)
                if bt is not None:
                    _sz = _build_size_of(item)
                    if getattr(item, "_image_only_on_temp", False):
                        need = estimate_image_space_needed(_sz)
                        kind = f"image on {temp_drive_label(Path(bt))} (spool auto-routed)"
                    else:
                        need = estimate_peak_space_needed(_sz, _peak_factor_for(item), same_drive(Path(bt), od))
                        kind = "full scratch"
                    self.log("INFO", f"Space check — {item.name}: {kind} on {bt} | "
                                     f"need ~{format_size(need)} | free {format_size(get_free_space(bt))} | {gate}")
                if gate == "cancel":
                    self._batch_running = False
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self._update_batch_counter()
                    return
                if gate == "skip":
                    self._cleanup_after_failure(item)   # reclaim any partial scratch
                    self.queue.pop(0)
                    self._batch_failed += 1
                    self._update_batch_counter()
                    self.update_queue_box()
                    self.root.after(600, self._batch_auto_start)
                    return
        except Exception as e:
            self.log("WARN", f"Space pre-check skipped: {e}")

        # Archive placeholder — extract first
        if getattr(item, "archive_path", None):
            self._extract_queued_item(item)
            return
        # Folder pack: keep Spotlight off the temp/image dir (the source folder is the
        # user's own — left indexable). Archives were marked in _extract_queued_item.
        self._mark_no_spotlight(getattr(item, "_build_temp", None))
        # AMPR/APR: inject the emu .sprx + build ampr_emu.index on the resolved game folder
        # (post-extraction for archives) before packing, so they ride into the .ffpfsc.
        if getattr(item, "ampr_emu", False):
            self._prepare_ampr(item)
        try:
            cmd, cwd, out_dir, temp_dir = self.build_command(item)
        except Exception as e:
            self.log("ERROR", f"Auto-advance build_command failed: {e}")
            self._batch_running = False
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            return
        item.status = "Running"
        self.update_queue_box()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        current = self._batch_done + self._batch_failed + 1
        self._update_batch_counter()
        self.header_status_var.set(
            f"v{APP_VERSION}  |  Game {current}/{self._batch_total}  |  ✓{self._batch_done} ✗{self._batch_failed}"
        )
        self.status_update(
            f"Game {current}/{self._batch_total}",
            f"Starting: {item.name}",
            "Scanning Files", 0, 0, "00:00", "—", "—"
        )
        self.log("INFO", f"── Batch auto-advance: game {current}/{self._batch_total} — {item.name}")
        self.cancel_requested = False
        self._active_item = item
        self.worker = CLIWorker(self, item, cmd, cwd, out_dir, temp_dir)
        self.worker.start()

    def _show_batch_complete(self):
        total = self._batch_total
        done  = self._batch_done
        fail  = self._batch_failed
        self.batch_counter_var.set(
            f"Batch complete  |  ✓ {done}/{total}  ✗ {fail}/{total}"
        )
        msg = (
            f"Batch finished.\n\n"
            f"Total items:  {total}\n"
            f"Successful:   {done}\n"
            f"Failed:       {fail}\n"
        )
        if fail == 0:
            self.log("SUCCESS", f"🏁 Batch complete — all {total} item(s) processed successfully.")
        else:
            self.log("WARN", f"🏁 Batch complete — {done}/{total} succeeded, {fail} failed.")
        messagebox.showinfo("Batch Complete", msg)

    # ── Start / Cancel ────────────────────────────────────────────────────────
    def start(self):
        if not self.output_var.get().strip():
            messagebox.showerror("Missing output", "Select an output folder.")
            return
        if not self.temp_var.get().strip():
            self.temp_var.set(str(Path(self.output_var.get()) / "_ffpfsc_temp"))
        if not self.queue:
            self.pending_start = True
            self.add_source_to_queue()
            # A synchronous path (single file in the Source field) queues immediately —
            # honor the Start intent now. Async scans leave the queue empty and let the
            # scan_q handler consume pending_start instead; failed adds clear it (below),
            # so it can't linger and silently auto-start a later, unrelated add.
            if self.queue and self.pending_start:
                self.pending_start = False
                self.start()
            return

        item = self.queue[0]

        # ── Pre-flight space gate FIRST — place the run on a drive sized for its real
        #    footprint and decide go/skip/cancel BEFORE any extraction or packing, so a
        #    too-big archive is judged on its true extracted size (from headers) rather
        #    than extracted onto a full SSD and only then failing. ─────────────────────
        gate = self._space_gate(item, Path(self.output_var.get().strip()))
        if gate == "cancel":
            if self._batch_running:
                self._batch_running = False
            # Always return to idle — start() may run after extraction (buttons left
            # in the running state), so a single-game cancel here must reset them too.
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self._update_batch_counter()
            self.status_update("Ready", "Drive check cancelled.", "Ready", 0, 0, "00:00", "—", "—")
            return
        if gate == "skip":
            # Reclaim any scratch this item already wrote (e.g. an archive extracted
            # before the post-extraction re-gate), then skip.
            self._cleanup_after_failure(item)
            if self._batch_running:
                self.queue.pop(0)
                self._batch_failed += 1
                self._update_batch_counter()
                self.update_queue_box()
                self.root.after(600, self._batch_auto_start)
            else:
                # Single run: reset to idle (Start was disabled / Cancel enabled by the
                # extraction step) and mark the item, so the UI isn't left frozen.
                item.status = "Skipped"
                self.start_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
                self.update_queue_box()
                self.status_update("Ready", f"Skipped — low space: {item.name}", "Ready", 0, 0, "00:00", "—", "—")
            return

        # ── Archive placeholder — extract first, then compress (re-gates after) ──────
        if getattr(item, "archive_path", None):
            self._extract_queued_item(item)
            return

        # Folder pack: keep Spotlight off the temp/image dir (the source folder is the
        # user's own — left indexable). Archives were marked in _extract_queued_item.
        self._mark_no_spotlight(getattr(item, "_build_temp", None))
        # AMPR/APR: inject the emu .sprx + build ampr_emu.index on the resolved game folder
        # (post-extraction for archives) before packing, so they ride into the .ffpfsc.
        if getattr(item, "ampr_emu", False):
            self._prepare_ampr(item)
        try:
            cmd, cwd, out_dir, temp_dir = self.build_command(item)
        except Exception as e:
            messagebox.showerror("Cannot start", str(e))
            return

        # Stale temp data warning
        try:
            if getattr(item, "operation", "pack") != "unpack" and temp_dir.exists():
                # Count ALL leftover temp data — mkpfs tmp* working dirs AND old
                # _extracted game trees — but never the CURRENT item's own source,
                # which legitimately lives under <temp>/_extracted right now.
                cur_src = None
                try:
                    ex_root = (temp_dir / "_extracted").resolve()
                    src = Path(getattr(item, "path", "") or "").resolve()
                    if ex_root in src.parents:
                        cur_src = ex_root / src.relative_to(ex_root).parts[0]
                except Exception:
                    cur_src = None
                stale_items = []
                for p in temp_dir.iterdir():
                    if p.name == "_extracted" and p.is_dir():
                        for child in p.iterdir():
                            if cur_src is None or child.resolve() != cur_src:
                                stale_items.append(child)
                    elif _is_app_tmp_dir(p.name):
                        stale_items.append(p)
                if stale_items:
                    stale_size = sum(folder_size(p) for p in stale_items)
                    if stale_size > 1024 * 1024 * 1024:
                        keep_running = messagebox.askyesno(
                            "Temporary data found",
                            f"Found {format_size(stale_size)} of old temporary data in:\n{temp_dir}\n\n"
                            "Continue anyway?\n\nChoose No to manually delete old temp folders first."
                        )
                        if not keep_running:
                            return
        except Exception:
            pass

        # Initialise batch counters only for a FRESH start. An archive item re-enters
        # start() after extraction (the _extract_q "ok" handler calls start() again);
        # re-running this block then would wipe a running batch's progress and skip the
        # Batch Complete dialog for all-archive batches.
        if not self._batch_running:
            self._batch_total   = len(self.queue)
            self._batch_done    = 0
            self._batch_failed  = 0
            self._batch_running = self._batch_total > 0
            # Snapshot each queued game's size (in queue order) so the QUEUE bar can show
            # SIZE-weighted total progress — a 187 GB game advances it far more than a 35 GB
            # one, instead of every game counting an equal 1/N. Games finish in queue order,
            # so done-bytes = sum of the first _done sizes (see the status drain).
            try:
                self._batch_sizes = [max(0, int(display_size(it) or 0)) for it in self.queue]
            except Exception:
                self._batch_sizes = []
        self._update_batch_counter()

        self._last_cmd_str = " ".join(cmd)
        self.cancel_requested = False
        item.status = "Running"
        self.update_queue_box()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        label = f"Game 1/{self._batch_total}" if self._batch_total > 1 else "Starting"
        self.status_update(label, "Launching backend.", "Starting", 0, 0, "00:00", "—", "—")
        self._active_item = item
        self.worker = CLIWorker(self, item, cmd, cwd, out_dir, temp_dir)
        self.worker.start()

    def cancel(self):
        self.cancel_requested = True
        self.extract_cancel_event.set()
        # Immediate, main-thread visual feedback — don't wait for the ~100 ms poll tick,
        # and repaint the big status + footer + button NOW so it isn't masked by the
        # worker's still-queued progress updates (the "Cancel does nothing" symptom).
        try:
            self.cancel_btn.configure(state="disabled")
            self.big_status_var.set("Cancelling…")
            self.big_detail_var.set("Stopping the current job and reclaiming temp space…")
            self.footer_var.set("● Cancelling…")
            self.header_status_var.set(f"v{APP_VERSION}  |  Cancelling…")
            self.root.update_idletasks()
        except Exception:
            pass
        _kill_process_tree(self.current_process)
        self.status_update("Cancelling", "Cancel requested — stopping…", "Cancelling", 0, 0, "—", "—", "—")

    def status_update(self, title, detail, stage, stage_pct, overall_pct, elapsed, speed, eta):
        if stage == "Creating Temp PFS" and stage_pct >= 100:
            stage_pct = 99
        self.status_q.put((title, detail, stage, stage_pct, overall_pct, elapsed, speed, eta))

    def log(self, tag, msg):
        self.log_q.put((tag, msg))

    def finish(self, success, msg, last_cmd=""):
        self.done_q.put((success, msg, last_cmd))

    def add_history(self, item, output, final_size, elapsed):
        saved = item.size - final_size if item.size and final_size else 0
        pct = saved / item.size * 100 if item.size else 0
        hist = load_history()
        hist.append({
            "date": now_datetime(),
            "name": item.name,
            "title_id": item.title_id,
            "original": item.size,
            "final": final_size,
            "saved": saved,
            "pct": pct,
            "elapsed": elapsed,
            "output": output,
        })
        save_history(hist)
        rating, _ = compression_rating(pct)
        self.saved_var.set(f"Saved: {format_size(saved)}")
        self.ratio_var.set(f"Compression: {pct:.2f}%")
        self.rating_var.set(f"Rating: {rating}")
        self.refresh_history()
        self.refresh_statistics()

    # ── Tools ─────────────────────────────────────────────────────────────────
    def clear_temp_files(self):
        tp = self.temp_var.get().strip()
        if not tp:
            messagebox.showerror("No temp folder", "No temp folder is set.")
            return
        temp_dir = Path(tp)
        if not temp_dir.exists():
            messagebox.showinfo("Clear Temp", "Temp folder does not exist. Nothing to clear.")
            return
        size = get_folder_size(temp_dir)
        if size == 0:
            messagebox.showinfo("Clear Temp", "Temp folder is already empty.")
            return
        ok = messagebox.askyesno(
            "Clear Temp Files",
            f"Delete all contents of:\n{temp_dir}\n\n"
            f"Size to free: {format_size(size)}\n\n"
            "Are you sure? This cannot be undone."
        )
        if not ok:
            return
        try:
            shutil.rmtree(str(temp_dir), ignore_errors=True)
            temp_dir.mkdir(parents=True, exist_ok=True)
            messagebox.showinfo("Clear Temp", f"Temp folder cleared. Freed {format_size(size)}.")
            self.log("OK", f"Temp folder cleared: {temp_dir} ({format_size(size)} freed)")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to clear temp folder:\n{e}")

    def export_diagnostics(self):
        zip_path = export_diagnostic_zip(last_cmd=self._last_cmd_str)
        if zip_path and zip_path.exists():
            ok = messagebox.askyesno(
                "Diagnostic Package",
                f"Diagnostic ZIP saved to:\n{zip_path}\n\nOpen folder?"
            )
            if ok:
                open_path(APP_DIR)
        else:
            messagebox.showerror("Error", "Failed to create diagnostic ZIP.")

    # ── History & Statistics ──────────────────────────────────────────────────
    def refresh_history(self):
        self.history_box.configure(state="normal")
        self.history_box.delete("1.0", "end")
        hist = load_history()
        if not hist:
            self.history_box.insert("end", "No compressions recorded yet.\n")
            self.history_box.configure(state="disabled")
            return
        header = f"{'Date':<20} {'Game':<35} {'Original':>10} {'Output':>10} {'Saved':>10} {'%':>6}  Rating\n"
        self.history_box.insert("end", header)
        self.history_box.insert("end", "─" * len(header) + "\n")
        for entry in reversed(hist[-50:]):
            orig = format_size(entry.get("original", 0))
            final = format_size(entry.get("final", 0))
            saved = format_size(entry.get("saved", 0))
            pct = entry.get("pct", 0)
            rating, _ = compression_rating(pct)
            name = entry.get("name", "Unknown")[:34]
            date = entry.get("date", "")[:19]
            line = f"{date:<20} {name:<35} {orig:>10} {final:>10} {saved:>10} {pct:>5.1f}%  {rating}\n"
            self.history_box.insert("end", line)
        self.history_box.configure(state="disabled")

    def refresh_statistics(self):
        self.stats_box.configure(state="normal")
        self.stats_box.delete("1.0", "end")
        hist = load_history()

        total_games = len(hist)
        total_original = sum(e.get("original", 0) for e in hist)
        total_final = sum(e.get("final", 0) for e in hist)
        total_saved = sum(e.get("saved", 0) for e in hist)
        avg_pct = (sum(e.get("pct", 0) for e in hist) / total_games) if total_games else 0

        lines = [
            f"  Games Compressed:      {total_games}",
            f"  Total Original Size:   {format_size(total_original)}",
            f"  Total Output Size:     {format_size(total_final)}",
            f"  Total Space Saved:     {format_size(total_saved)}",
            f"  Average Compression:   {avg_pct:.1f}%",
            "",
        ]

        if hist:
            best = max(hist, key=lambda e: e.get("pct", 0))
            lines.append(f"  Best Compression:      {best.get('name','?')[:40]}  ({best.get('pct',0):.1f}%)")
            worst = min(hist, key=lambda e: e.get("pct", 0))
            lines.append(f"  Worst Compression:     {worst.get('name','?')[:40]}  ({worst.get('pct',0):.1f}%)")

        for line in lines:
            self.stats_box.insert("end", line + "\n")
        self.stats_box.configure(state="disabled")

    # ── ShadowMount help ──────────────────────────────────────────────────────
    def _show_sm_help(self):
        win = ctk.CTkToplevel(self.root)
        win.title("ShadowMount Compatibility")
        win.configure(fg_color=BLACK)
        win.geometry("480x380")
        win.resizable(False, False)
        win.lift(); win.focus_force(); win.grab_set()
        ctk.CTkLabel(win, text="ℹ  ShadowMount Compatibility",
                      text_color=YELLOW, font=ctk.CTkFont(size=15, weight="bold")
                     ).pack(anchor="w", padx=18, pady=(16, 6))
        ctk.CTkLabel(win,
                      text=(
                          "Output .ffpfsc files are designed for use with ShadowMount,\n"
                          "a PS5 backup manager.\n\n"
                          "To mount a compressed game:\n"
                          "  1.  Copy the .ffpfsc file to your PS5 internal storage\n"
                          "      or an external drive (USB SSD/HDD).\n"
                          "  1a. If you already have a shortcut for this game on the XMB,\n"
                          "      delete it first — the old entry causes a param error and\n"
                          "      the game won't appear after mounting.\n"
                          "  2.  Open ShadowMount on your PS5 and let it scan.\n"
                          "      If the game is not detected or the shortcut is not made,\n"
                          "      re-run ShadowMount.\n"
                          "  3.  Select the game from the XMB and launch it —\n"
                          "      it will appear and run like a standard title.\n\n"
                          "Requirements for full compatibility:\n"
                          "  • sce_sys/param.json must exist in the original dump\n"
                          "  • eboot.bin must exist in the original dump\n\n"
                          "If the game still doesn't appear, verify the dump structure\n"
                          "and try re-compressing."
                      ),
                      text_color=WHITE, font=ctk.CTkFont(size=12),
                      justify="left", anchor="w", wraplength=440
                     ).pack(anchor="w", padx=18, pady=(0, 4))
        ctk.CTkButton(win, text="Close", command=win.destroy,
                       fg_color=GREEN, hover_color=GREEN2, text_color="#061006"
                      ).pack(anchor="e", padx=18, pady=(0, 16))

    # ── Compatibility report ───────────────────────────────────────────────────
    def _prompt_compat_share(self, item, final_size: int = 0):
        """Pop up after a successful compression asking the user to share compat data."""
        if item is None:
            return

        win = ctk.CTkToplevel(self.root)
        win.title("Share Compatibility Data")
        win.configure(fg_color=BLACK)
        win.geometry("480x320")
        win.resizable(False, False)
        win.transient(self.root)
        win.lift()
        win.focus_force()
        win.after(50, win.grab_set)

        ctk.CTkLabel(win, text="🎮  Share Compatibility Report?",
                      font=ctk.CTkFont(size=16, weight="bold"),
                      text_color=GREEN).pack(anchor="w", padx=20, pady=(18, 4))
        ctk.CTkLabel(win,
                      text="Help the community by sharing how well this game compressed.\n"
                           "No personal data is collected — only game info and result.",
                      text_color=MUTED, font=ctk.CTkFont(size=12),
                      justify="left", wraplength=440).pack(anchor="w", padx=20, pady=(0, 12))

        # Summary card
        card = ctk.CTkFrame(win, fg_color=PANEL, corner_radius=8)
        card.pack(fill="x", padx=20, pady=(0, 12))
        rows = [
            ("Game",             item.name),
            ("Title ID",         item.title_id),
            ("Original Size",    format_size(item.size) if item.size else "—"),
            ("Compressed Size",  format_size(final_size) if final_size else "—"),
        ]
        for lbl, val in rows:
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=2)
            ctk.CTkLabel(row, text=lbl + ":", text_color=MUTED,
                          font=ctk.CTkFont(size=11), width=130, anchor="w").pack(side="left")
            ctk.CTkLabel(row, text=val, text_color=WHITE,
                          font=ctk.CTkFont(size=11), anchor="w").pack(side="left")

        status_var = tk.StringVar(value="Not Tested Yet")

        status_lbl = ctk.CTkLabel(win, text="", text_color=MUTED,
                                   font=ctk.CTkFont(size=11))
        status_lbl.pack(anchor="w", padx=20)

        def _send():
            import datetime
            report = {
                "game_title":      item.name,
                "title_id":        item.title_id,
                "original_size":   format_size(item.size) if item.size else "",
                "compressed_size": format_size(final_size) if final_size else "",
                "storage":         self._compat_storage_var.get(),
                "shadowmount_ver": self._compat_smver_var.get().strip(),
                "status":          status_var.get(),
                "notes":           "",
                "submitted":       datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            add_compat_report(report)
            self.refresh_compat_list()
            status_lbl.configure(text="⏳ Sending to community…", text_color=YELLOW)
            def _post():
                try:
                    import urllib.request as _ur, json as _js
                    payload = _js.dumps(report).encode()
                    req = _ur.Request(COMMUNITY_URL, data=payload,
                                      headers={"Content-Type": "application/json"})
                    with _ur.urlopen(req, timeout=15) as resp:
                        resp.read()
                    try:
                        win.after(0, lambda: status_lbl.configure(
                            text="✓ Sent! Thank you for contributing.", text_color=("#1a7a40", "#4ade80")))
                    except Exception:
                        pass   # dialog already closed
                    self.log("OK", f"Compat report sent: {item.name} — {report['status']}")
                    try:
                        win.after(2000, win.destroy)
                    except Exception:
                        pass
                except Exception as e:
                    try:
                        win.after(0, lambda: status_lbl.configure(
                            text=f"⚠ Send failed: {e}", text_color=RED))
                    except Exception:
                        pass
                    self.log("WARN", f"Community share failed: {e}")
            threading.Thread(target=_post, daemon=True).start()

        def _skip():
            win.destroy()

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=(8, 16))
        ctk.CTkButton(btn_row, text="✓  Yes, Share Data", fg_color=GREEN,
                       text_color="#061006", hover_color=GREEN2,
                       command=_send).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ctk.CTkButton(btn_row, text="✗  No Thanks", fg_color=CARD2,
                       text_color=WHITE, hover_color=("#b0b0b0", "#2a2a2a"),
                       command=_skip).pack(side="left", expand=True, fill="x", padx=(6, 0))

    def _check_pending_compat_reports(self):
        """On startup: find history entries with no community report and prompt the user."""
        settings = load_settings()
        if settings.get("skip_compat_reminder", False):
            return
        history  = load_history()
        reported = {r.get("title_id", "").strip().upper() for r in load_compat()}
        # Games compressed by this user but never reported
        seen_tids: set[str] = set()
        pending = []
        for h in history:
            tid = h.get("title_id", "").strip().upper()
            if tid and tid not in reported and tid not in seen_tids:
                seen_tids.add(tid)
                pending.append(h)
        if not pending:
            return
        self._show_pending_compat_dialog(pending)

    def _show_pending_compat_dialog(self, pending: list):
        """Small non-modal dialog listing compressed-but-unreported games."""
        win = ctk.CTkToplevel(self.root)
        win.title("Community Reports — Have You Tested These?")
        win.configure(fg_color=BLACK)
        win.geometry("520x420")
        win.resizable(False, True)
        win.transient(self.root)
        win.lift()

        ctk.CTkLabel(win,
                      text="🎮  Have you tested these compressed games on PS5?",
                      font=ctk.CTkFont(size=14, weight="bold"),
                      text_color=GREEN).pack(anchor="w", padx=18, pady=(16, 4))
        ctk.CTkLabel(win,
                      text="The community hasn't received a report for the games below.\n"
                           "If you've tried them with ShadowMount, please share the result — it only takes a second.",
                      text_color=MUTED, font=ctk.CTkFont(size=11),
                      justify="left", wraplength=480).pack(anchor="w", padx=18, pady=(0, 8))

        scroll = ctk.CTkScrollableFrame(win, fg_color=PANEL, corner_radius=6)
        scroll.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        scroll.grid_columnconfigure(0, weight=1)

        STATUS_OPTS = ["Not Tested Yet", "Working", "Partial", "Not Working"]
        row_vars: list[tuple[dict, tk.StringVar]] = []

        for i, h in enumerate(pending[:15]):   # cap at 15 so dialog doesn't get huge
            name  = h.get("name", "Unknown")
            tid   = h.get("title_id", "")
            date  = h.get("date", "")[:10]

            row = ctk.CTkFrame(scroll, fg_color=CARD, corner_radius=6)
            row.grid(row=i, column=0, sticky="ew", pady=3, padx=2)
            row.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(row,
                          text=f"{name}  [{tid}]",
                          text_color=WHITE, font=ctk.CTkFont(size=11, weight="bold"),
                          anchor="w").grid(row=0, column=0, sticky="w", padx=10, pady=(6, 0))
            ctk.CTkLabel(row,
                          text=f"Compressed {date}",
                          text_color=MUTED, font=ctk.CTkFont(size=10),
                          anchor="w").grid(row=1, column=0, sticky="w", padx=10, pady=(0, 4))

            sv = tk.StringVar(value="Not Tested Yet")
            ctk.CTkOptionMenu(row, variable=sv, values=STATUS_OPTS,
                               fg_color=CARD2, button_color=CARD2,
                               dropdown_fg_color=PANEL,
                               text_color=WHITE, font=ctk.CTkFont(size=11),
                               width=160).grid(row=0, column=1, rowspan=2, padx=10, pady=4)
            row_vars.append((h, sv))

        def _submit_all():
            import datetime
            submitted = 0
            for h, sv in row_vars:
                status = sv.get()
                if status == "Not Tested Yet":
                    continue
                report = {
                    "game_title":      h.get("name", "Unknown"),
                    "title_id":        h.get("title_id", ""),
                    "original_size":   format_size(h["original"]) if h.get("original") else "",
                    "compressed_size": format_size(h["final"])    if h.get("final")    else "",
                    "storage":         self._compat_storage_var.get(),
                    "shadowmount_ver": self._compat_smver_var.get().strip(),
                    "status":          status,
                    "notes":           "",
                    "submitted":       datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                add_compat_report(report)
                def _post(r=report):
                    try:
                        import urllib.request as _ur, json as _js
                        payload = _js.dumps(r).encode()
                        req = _ur.Request(COMMUNITY_URL, data=payload,
                                          headers={"Content-Type": "application/json"})
                        with _ur.urlopen(req, timeout=15) as resp:
                            resp.read()
                        self.log("OK", f"Compat report sent: {r['game_title']} — {r['status']}")
                    except Exception as e:
                        self.log("WARN", f"Community share failed: {e}")
                threading.Thread(target=_post, daemon=True).start()
                submitted += 1
            self.refresh_compat_list()
            win.destroy()
            if submitted:
                self.log("OK", f"Submitted {submitted} community report(s). Thank you!")

        def _dont_ask():
            s = load_settings()
            s["skip_compat_reminder"] = True
            save_settings(s)
            win.destroy()

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=18, pady=(0, 14))
        ctk.CTkButton(btn_row, text="✓  Submit Selected",
                       fg_color=GREEN, hover_color=GREEN2, text_color="#061006",
                       command=_submit_all).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(btn_row, text="Later",
                       fg_color=CARD2, text_color=WHITE,
                       hover_color=("#b0b0b0","#2a2a2a"),
                       command=win.destroy).pack(side="left", expand=True, fill="x", padx=(4, 4))
        ctk.CTkButton(btn_row, text="Don't Ask Again",
                       fg_color=CARD2, text_color=MUTED,
                       hover_color=("#b0b0b0","#2a2a2a"),
                       command=_dont_ask).pack(side="left", expand=True, fill="x", padx=(4, 0))

    def _compat_autofill(self, item=None, final_size: int = 0):
        """Fill the compatibility form from a completed compression result."""
        try:
            report_text = FINAL_REPORT_FILE.read_text(encoding="utf-8", errors="replace")
        except Exception:
            report_text = ""

        def _grab(key: str) -> str:
            for line in report_text.splitlines():
                if line.lower().startswith(key.lower() + ":"):
                    return line.split(":", 1)[1].strip()
            return ""

        # Prefer the passed item, else fall back to queue[0], else parse report
        src = item or (self.queue[0] if self.queue else None)
        if src:
            self._compat_title_var.set(src.name)
            self._compat_titleid_var.set(src.title_id)
            self._compat_origsize_var.set(format_size(src.size) if src.size else "")
        else:
            self._compat_title_var.set(_grab("Game"))
            self._compat_titleid_var.set(_grab("Title ID"))
            self._compat_origsize_var.set(_grab("Original Size"))

        if final_size:
            self._compat_compsize_var.set(format_size(final_size))
        else:
            comp = _grab("Compressed Size") or _grab("Output Size")
            self._compat_compsize_var.set(comp)

        # Clear notes so user fills in their own experience
        self._compat_notes_box.delete("1.0", "end")

    def submit_compat_report(self):
        title   = self._compat_title_var.get().strip()
        tid     = self._compat_titleid_var.get().strip()
        orig    = self._compat_origsize_var.get().strip()
        comp    = self._compat_compsize_var.get().strip()
        smver   = self._compat_smver_var.get().strip()
        storage = self._compat_storage_var.get()
        status  = self._compat_status_var.get()
        notes   = self._compat_notes_box.get("1.0", "end").strip()

        if not title and not tid:
            messagebox.showerror("Missing data", "Enter at least a Game Title or Title ID.")
            return

        import datetime
        report = {
            "game_title":       title or tid,
            "title_id":         tid,
            "original_size":    orig,
            "compressed_size":  comp,
            "storage":          storage,
            "shadowmount_ver":  smver,
            "status":           status,
            "notes":            notes,
            "submitted":        datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        add_compat_report(report)
        self.log("OK", f"Compatibility report saved: {title or tid} — {status}")
        self.refresh_compat_list()
        self._compat_notes_box.delete("1.0", "end")

        # ── Optionally share to community Google Sheet ────────────────────────
        if getattr(self, "_compat_share_var", None) and self._compat_share_var.get():
            self._compat_share_status.configure(text="⏳ Sending…", text_color=YELLOW)
            def _post():
                try:
                    import urllib.request as _ur, json as _js
                    payload = _js.dumps(report).encode()
                    req = _ur.Request(COMMUNITY_URL, data=payload,
                                      headers={"Content-Type": "application/json"})
                    with _ur.urlopen(req, timeout=15) as resp:
                        resp.read()
                    self.root.after(0, lambda: self._compat_share_status.configure(
                        text="✓ Shared!", text_color=("#1a7a40", "#4ade80")))
                    self.log("OK", f"Compat report shared to community: {title or tid}")
                except Exception as e:
                    self.root.after(0, lambda: self._compat_share_status.configure(
                        text=f"⚠ Failed: {e}", text_color=RED))
                    self.log("WARN", f"Community share failed: {e}")
                self.root.after(6000, lambda: self._compat_share_status.configure(text=""))
            threading.Thread(target=_post, daemon=True).start()

        messagebox.showinfo("Submitted", f"Report saved for: {title or tid}")

    def refresh_compat_list(self):
        reports = load_compat()
        self.compat_box.configure(state="normal")
        self.compat_box.delete("1.0", "end")
        if not reports:
            self.compat_box.insert("end", "No compatibility reports yet.\n\n"
                                           "Use the form to submit one after testing a game with ShadowMount.")
            self.compat_box.configure(state="disabled")
            return

        STATUS_ICON = {"Working": "✅", "Partial": "⚠", "Not Working": "❌"}

        # Group reports by title_id so duplicate submissions for the same game
        # are shown as a single aggregated card instead of N identical rows.
        from collections import OrderedDict
        groups: OrderedDict = OrderedDict()
        for r in reports:
            key = r.get("title_id", "").strip().upper() or r.get("game_title", "Unknown")
            groups.setdefault(key, []).append(r)

        for key, group in groups.items():
            # Use the most recent (first) entry for name / size / meta
            latest = group[0]
            name  = latest.get("game_title", "Unknown")
            tid   = latest.get("title_id", "")
            orig  = latest.get("original_size", "")
            comp  = latest.get("compressed_size", "")
            sizes = f"{orig} → {comp}" if orig and comp else (orig or comp)

            if len(group) == 1:
                # Single report — show full detail as before
                r     = latest
                icon  = STATUS_ICON.get(r.get("status", ""), "❓")
                store = r.get("storage", "")
                smver = r.get("shadowmount_ver", "")
                notes = r.get("notes", "")
                date  = r.get("submitted", "")
                line1 = f"{icon}  {name}"
                if tid:
                    line1 += f"  [{tid}]"
                line2_parts = [p for p in [store, f"SM v{smver}" if smver else "", sizes, date] if p]
                self.compat_box.insert("end", line1 + "\n")
                if line2_parts:
                    self.compat_box.insert("end", f"   {'   '.join(line2_parts)}\n")
                if notes:
                    self.compat_box.insert("end", f"   📝 {notes}\n")
            else:
                # Multiple reports — show aggregated summary
                counts = {}
                for r in group:
                    s = r.get("status", "Unknown")
                    counts[s] = counts.get(s, 0) + 1

                # Pick the overall consensus icon (majority status)
                best = max(counts, key=counts.get)
                icon = STATUS_ICON.get(best, "❓")

                line1 = f"{icon}  {name}"
                if tid:
                    line1 += f"  [{tid}]"
                line1 += f"  ({len(group)} reports)"
                self.compat_box.insert("end", line1 + "\n")

                # Status breakdown
                breakdown = "   ".join(
                    f"{STATUS_ICON.get(s, '❓')} {s}: {n}"
                    for s, n in sorted(counts.items(), key=lambda x: -x[1])
                )
                self.compat_box.insert("end", f"   {breakdown}\n")
                if sizes:
                    self.compat_box.insert("end", f"   {sizes}\n")

                # Show individual notes if any report has them
                for r in group:
                    note = r.get("notes", "").strip()
                    if note:
                        date = r.get("submitted", "")
                        self.compat_box.insert("end", f"   📝 {note}" + (f"  ({date})" if date else "") + "\n")

            self.compat_box.insert("end", "\n")
        self.compat_box.configure(state="disabled")

    def export_compat_csv(self):
        reports = load_compat()
        if not reports:
            messagebox.showinfo("No data", "No compatibility reports to export.")
            return
        path = filedialog.asksaveasfilename(
            title="Export Compatibility List",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile="ps5_compat_list.csv",
        )
        if not path:
            return
        import csv
        fields = ["game_title","title_id","original_size","compressed_size",
                  "storage","shadowmount_ver","status","notes","submitted"]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                w.writerows(reports)
            messagebox.showinfo("Exported", f"Saved {len(reports)} report(s) to:\n{path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    # ── Sound + Summary ───────────────────────────────────────────────────────
    def play_complete_sound(self, success=True):
        try:
            want = (success and self.sound_complete_var.get()) or \
                   (not success and self.sound_error_var.get())
            if not want:
                return
            if winsound:
                winsound.MessageBeep(winsound.MB_ICONASTERISK if success else winsound.MB_ICONHAND)
            elif sys.platform == "darwin":
                snd = "/System/Library/Sounds/Glass.aiff" if success else "/System/Library/Sounds/Basso.aiff"
                subprocess.Popen(["afplay", snd],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def show_summary_popup(self):
        try:
            report = FINAL_REPORT_FILE.read_text(encoding="utf-8", errors="replace")
        except Exception:
            report = "Compression complete."
        if "Operation: Extract PFS image" not in report:
            report += (
                "\n\nNote: Compression does not improve FPS or graphics quality. "
                "If the rating is POOR, keep the original uncompressed folder instead."
            )
        self._last_result_text = report
        SummaryDialog(self.root, report)

    def copy_last_result(self):
        text = getattr(self, "_last_result_text", "")
        if not text:
            try:
                text = FINAL_REPORT_FILE.read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            messagebox.showinfo("Copied", "Compression result copied to clipboard.")

    def open_raw_log(self):
        ensure_app_dir()
        RAW_LOG_FILE.touch(exist_ok=True)
        open_path(RAW_LOG_FILE)

    def open_output_folder(self):
        # Prefer the actual result location — a bundle lands in its own subfolder, not
        # directly in the chosen output dir — then fall back to the output folder.
        try:
            out = getattr(self.worker, "output_path", "") if getattr(self, "worker", None) else ""
            if out and Path(out).exists():
                target = Path(out).parent if Path(out).is_file() else Path(out)
                open_path(str(target))
                return
        except Exception:
            pass
        p = self.output_var.get()
        if p and Path(p).exists():
            open_path(p)

    def clear_logs(self):
        self.log_box.delete("1.0", "end")
        self.visible_log_lines = 0

    # ── Stage display ─────────────────────────────────────────────────────────
    def update_stages_display(self, current_stage, pct):
        full_names   = [s[0] for s in _STAGE_DEFS]
        current_idx  = full_names.index(current_stage) if current_stage in full_names else -1
        # Only tick a prior stage ✓ if it was ACTUALLY entered — pack and unpack use
        # disjoint subsets of _STAGE_DEFS, so a raw index check would mark e.g.
        # "✓ Compress" during an unpack or "✓ Extract" during a pack.
        sp = getattr(self.worker, "stage_progress", {}) if getattr(self, "worker", None) else {}
        for i, (lbl, (full, short)) in enumerate(zip(self._stage_labels, _STAGE_DEFS)):
            if i == current_idx:
                dp = min(int(pct), 99) if full == "Creating Temp PFS" else int(pct)
                lbl.configure(text=f"▶ {short} {dp}%", text_color=YELLOW)
            elif current_idx >= 0 and i < current_idx and sp.get(full, 0) >= 100:
                lbl.configure(text=f"✓ {short}", text_color=("#1a7a40", "#4ade80"))
            else:
                lbl.configure(text=f"○ {short}", text_color=MUTED)

    # ── Poll loop ─────────────────────────────────────────────────────────────
    def _tick_elapsed(self):
        """Called every poll cycle while a worker is live — keeps elapsed ticking
        regardless of whether the backend is printing anything."""
        w = getattr(self, "worker", None)
        if w and w.is_alive() and w.start_time:
            self.elapsed_var.set(f"Elapsed: {format_duration(time.time() - w.start_time)}")

    def _update_ram_meter(self):
        """Refresh the RAM readout in the log header (green <70%, amber <85%, red above)."""
        try:
            import psutil
            mem = psutil.virtual_memory()
            avail_gb = mem.available / 1024**3
            total_gb = mem.total / 1024**3
            pct = mem.percent
            color = (("#1a7a40", "#4ade80") if pct < 70
                     else ("#b45309", "#facc15") if pct < 85
                     else ("#b91c1c", "#f87171"))
            self.ram_var.set(f"RAM: {avail_gb:.1f} / {total_gb:.0f} GB free  ({pct:.0f}% used)")
            try:
                self._ram_label.configure(text_color=color)
            except Exception:
                pass
        except Exception:
            # psutil missing or unavailable — hide the meter rather than error.
            try:
                self.ram_var.set("")
            except Exception:
                pass

    def _poll(self):
        try:
            self._poll_inner()
        except Exception as e:
            # Last-resort catch — log and keep the loop alive no matter what.
            try:
                self.log("ERROR", f"[_poll crash — loop kept alive] {e}")
            except Exception:
                pass
        self.root.after(200, self._poll)

    def _poll_inner(self):
        self._tick_elapsed()
        # Refresh the RAM meter ~every 2 s (poll runs every 200 ms → every 10th tick).
        self._ram_tick = getattr(self, "_ram_tick", 0) + 1
        if self._ram_tick >= 10:
            self._ram_tick = 0
            self._update_ram_meter()
        try:
            while True:
                status, payload = self.scan_q.get_nowait()
                if status == "ok":
                    item = payload
                    self.queue.append(item)
                    self.update_queue_box(select_item=item)
                    self.status_update("Ready", f"{item.title_id} added to queue.",
                                        "Ready", 0, 0, "00:00", "—", "—")
                    self.log("OK", f"Added {item.title_id} | {item.name} | {format_size(item.size)}")
                    if self.pending_start:
                        self.pending_start = False
                        self.start()

                elif status == "bundles":
                    # Library scan: one bundle per game subfolder, each mirrored.
                    items: list = payload
                    count = len(items)
                    preview = "\n".join(f"  • {getattr(it, 'bundle_subfolder', it.name)}" for it in items[:12])
                    if count > 12:
                        preview += f"\n  … and {count - 12} more"
                    proceed = count == 1 or messagebox.askyesno(
                        f"Found {count} Games",
                        f"Found {count} game folder(s):\n\n{preview}\n\n"
                        f"Convert all {count}? Each keeps its own folder at the destination, "
                        "with any extras/DLCs next to the .ffpfsc."
                    )
                    if proceed:
                        for it in items:
                            self.queue.append(it)
                        self.update_queue_box(select_item=items[0] if items else None)
                        self.log("OK", f"Queued {count} game folder(s) — each mirrored at the destination.")
                        self.status_update("Ready", f"{count} game(s) added to queue.",
                                            "Ready", 0, 0, "00:00", "—", "—")
                        if self.pending_start:
                            self.pending_start = False
                            self.start()
                    else:
                        self.status_update("Ready", "Batch add cancelled.", "Ready", 0, 0, "00:00", "—", "—")
                        self.pending_start = False

                elif status == "multi_found":
                    # Multiple extracted game folders discovered
                    games: list = payload
                    count = len(games)
                    preview = "\n".join(f"  • {g.name}" for g in games[:10])
                    if count > 10:
                        preview += f"\n  … and {count - 10} more"
                    ok = messagebox.askyesno(
                        f"Found {count} Games",
                        f"Found {count} PS5 game folder(s):\n\n{preview}\n\n"
                        f"Add all {count} to the queue?"
                    )
                    if ok:
                        self.log("INFO", f"Queuing {count} games…")
                        self.status_update("Scanning", f"Adding {count} games to queue…",
                                            "Scanning Files", 0, 0, "00:00", "—", "—")
                        def _add_all(paths=games):
                            for gpath in paths:
                                try:
                                    self.scan_q.put(("ok", GameItem(gpath)))
                                except Exception as e:
                                    self.log("ERROR", f"Skipped {gpath.name}: {e}")
                        threading.Thread(target=_add_all, daemon=True).start()
                    else:
                        self.status_update("Ready", "Batch add cancelled.", "Ready", 0, 0, "00:00", "—", "—")
                        self.pending_start = False

                elif status == "exfat_found":
                    # Multiple .exfat / .ffpkg disk images found — no extraction needed, queue directly
                    image_list: list = payload
                    count = len(image_list)
                    preview = "\n".join(f"  • {f.name}" for f in image_list[:10])
                    if count > 10:
                        preview += f"\n  … and {count - 10} more"
                    ok = messagebox.askyesno(
                        f"Found {count} Disk Image{'s' if count > 1 else ''}",
                        f"Found {count} disk image(s) (.exfat / .ffpkg):\n\n{preview}\n\n"
                        f"Add all {count} to the queue?\n"
                        "(Each image will be compressed directly — no extraction needed.)"
                    )
                    if ok:
                        for img in image_list:
                            item = GameItem.from_exfat(img)
                            self.queue.append(item)
                            lbl = "exFAT" if img.suffix.lower() == ".exfat" else "ffpkg"
                            self.log("OK", f"{lbl} image queued: {img.name}")
                        self.update_queue_box()
                        self.status_update("Ready", f"{count} disk image(s) added to queue.",
                                            "Ready", 0, 0, "00:00", "—", "—")
                        if self.pending_start:
                            self.pending_start = False
                            self.start()
                    else:
                        self.status_update("Ready", "Image add cancelled.", "Ready", 0, 0, "00:00", "—", "—")
                        self.pending_start = False

                elif status == "archives_found":
                    # Archive files found inside a scanned folder — queue as placeholders
                    archives: list = payload
                    count = len(archives)
                    preview = "\n".join(f"  • {a.name}" for a in archives[:10])
                    if count > 10:
                        preview += f"\n  … and {count - 10} more"
                    ok = messagebox.askyesno(
                        f"Found {count} Archive{'s' if count > 1 else ''}",
                        f"Found {count} archive file(s):\n\n{preview}\n\n"
                        f"Add all {count} to the queue?\n"
                        "(Each archive will be extracted when it is its turn.)"
                    )
                    if ok:
                        for arc in archives:
                            item = GameItem.from_archive(arc)
                            self.queue.append(item)
                            self.log("OK", f"Archive queued: {arc.name}")
                        self.update_queue_box()
                        self.status_update("Ready", f"{count} archive(s) added to queue.",
                                            "Ready", 0, 0, "00:00", "—", "—")
                        if self.pending_start:
                            self.pending_start = False
                            self.start()
                    else:
                        self.status_update("Ready", "Archive add cancelled.", "Ready", 0, 0, "00:00", "—", "—")
                        self.pending_start = False

                elif status == "pfs_found":
                    images: list = payload
                    count = len(images)
                    preview = "\n".join(f"  • {f.name}" for f in images[:10])
                    if count > 10:
                        preview += f"\n  … and {count - 10} more"
                    ok = messagebox.askyesno(
                        f"Found {count} PFS Image{'s' if count > 1 else ''}",
                        f"Found {count} .ffpfs/.ffpfsc image(s):\n\n{preview}\n\n"
                        f"Add all {count} to the queue for extraction?"
                    )
                    if ok:
                        for image in images:
                            item = GameItem.from_pfs_image(image)
                            self.queue.append(item)
                            self.log("OK", f"PFS image queued for extraction: {image.name}")
                        self.update_queue_box()
                        self.status_update("Ready", f"{count} PFS image(s) added to queue.",
                                            "Ready", 0, 0, "00:00", "—", "—")
                        if self.pending_start:
                            self.pending_start = False
                            self.start()
                    else:
                        self.status_update("Ready", "PFS image add cancelled.", "Ready", 0, 0, "00:00", "—", "—")
                        self.pending_start = False

                elif status == "cancelled":
                    self.pending_start = False
                    self._batch_running = False
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self.status_update("Ready", str(payload), "Ready", 0, 0, "00:00", "—", "—")

                else:  # "error"
                    self.pending_start = False
                    messagebox.showerror("Scan failed", str(payload))
        except queue.Empty:
            pass

        # Capture whether the log is scrolled to the bottom BEFORE inserting — yview()
        # read AFTER an insert always reports < 1.0 (the content grew but the view hasn't
        # moved yet), so checking it post-insert would never re-follow during active
        # logging. Empty/short logs read (0.0, 1.0) → treated as "at bottom" → follow.
        try:
            was_at_bottom = self.log_box._textbox.yview()[1] >= 0.98
        except Exception:
            try:
                was_at_bottom = self.log_box.yview()[1] >= 0.98
            except Exception:
                was_at_bottom = True

        processed = 0
        try:
            t = self.log_box._textbox
            while processed < 50:
                tag, msg = self.log_q.get_nowait()
                line = f"[{now_time()}] [{tag}] {msg}\n"
                t.insert("end", line, (tag,))
                self.visible_log_lines += 1
                processed += 1
        except (queue.Empty, AttributeError):
            if processed == 0:
                try:
                    while processed < 50:
                        tag, msg = self.log_q.get_nowait()
                        self.log_box.insert("end", f"[{now_time()}] [{tag}] {msg}\n")
                        self.visible_log_lines += 1
                        processed += 1
                except queue.Empty:
                    pass
        if processed:
            if self.visible_log_lines > 1500:
                try:
                    self.log_box._textbox.delete("1.0", "300.0")
                except Exception:
                    self.log_box.delete("1.0", "300.0")
                self.visible_log_lines -= 300

            # Auto-scroll to the newest line — but only when the user was already at the
            # bottom (captured above), so scrolling up to read older output isn't yanked back.
            if was_at_bottom:
                try:
                    self.log_box._textbox.see("end")
                except Exception:
                    try:
                        self.log_box.see("end")
                    except Exception:
                        pass

        try:
            while True:
                title, detail, stage, stage_pct, overall_pct, elapsed, speed, eta = self.status_q.get_nowait()
                self.big_status_var.set(title)
                self.big_detail_var.set(detail)
                # Lower bar = CURRENT STEP: the progress of the operation running right now
                # (extract / read / temp-PFS / compress / write …) = stage_pct. The game name
                # sits above as context; whole-game progress feeds the QUEUE bar below.
                _gname = (self.queue[0].name if self.queue else "") or ""
                self.cur_game_var.set(f"CURRENT STEP  ·  {_gname}" if _gname else "CURRENT STEP")
                self.stage_title_var.set(stage or title)
                self.stage_detail_var.set(detail)
                self.stage_pct_var.set(f"{int(stage_pct)}%")
                self.stage_bar.set(max(0, min(1, stage_pct / 100)))
                # Upper bar = QUEUE: TOTAL progress over the whole batch — finished games plus
                # the current game's fraction. SIZE-weighted when we have the size snapshot
                # (so a 187 GB game moves the bar far more than a 35 GB one and it does NOT
                # just mirror the current game); falls back to equal-weight game count.
                _total = max(1, getattr(self, "_batch_total", 1))
                _done  = getattr(self, "_batch_done", 0) + getattr(self, "_batch_failed", 0)
                _sizes = getattr(self, "_batch_sizes", []) or []
                _tot_bytes = sum(_sizes)
                if _tot_bytes > 0 and _done <= len(_sizes):
                    _done_bytes = sum(_sizes[:_done])
                    _cur_bytes  = _sizes[_done] if _done < len(_sizes) else 0
                    _qfrac = max(0.0, min(1.0, (_done_bytes + _cur_bytes * overall_pct / 100.0) / _tot_bytes))
                else:
                    _qfrac = max(0.0, min(1.0, (_done + overall_pct / 100.0) / _total))
                self.overall_pct_var.set(f"{int(_qfrac * 100)}%")
                self.overall_bar.set(_qfrac)
                self.overall_title_var.set(
                    f"QUEUE  ·  Game {min(_done + 1, _total)}/{_total}" if _total > 1 else "QUEUE")
                self.speed_var.set(f"Speed: {speed}")
                self.elapsed_var.set(f"Elapsed: {elapsed}")
                self.eta_var.set(f"ETA: {eta}")
                self.header_status_var.set(f"v{APP_VERSION}  |  Stage: {stage}")
                self.footer_var.set(f"● {title}")
                self.update_stages_display(stage, stage_pct)
        except queue.Empty:
            pass

        # ── Archive extraction completion ─────────────────────────────────────
        try:
            status, payload = self._extract_q.get_nowait()
            if status == "ok":
                if isinstance(payload, tuple):
                    item, extra_items = payload
                else:
                    item, extra_items = payload, []
                if extra_items:
                    try:
                        idx = self.queue.index(item)
                    except ValueError:
                        idx = 0
                    for offset, extra in enumerate(extra_items, start=1):
                        self.queue.insert(idx + offset, extra)
                    self.log("OK", f"Queued {len(extra_items)} additional payload item(s) from the archive")
                # Item was updated in-place — clear cache so details panel refreshes fully
                self._details_item   = None
                self._loaded_art_key = None
                self.update_queue_box(select_item=item)
                self.log("OK", f"Extraction complete: {item.name}  [{format_size(item.size)}]")
                # Continue into compression now that the item has a real path
                self.start()
            elif status == "cancelled":
                # Cancel = STOP the batch (don't advance). Clean the partial extract of
                # the item being unpacked (it stays in the queue, marked Cancelled).
                if self.queue:
                    self._cleanup_after_failure(self.queue[0])
                    self.queue[0].status = "Cancelled"
                self._batch_running = False
                self.pending_start = False
                self.start_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
                self.update_queue_box()
                self.status_update("Ready", str(payload), "Ready", 0, 0, "00:00", "—", "—")
                self.log("WARN", str(payload))
            else:
                # Extraction failed — clean the partial tree, pop the stuck archive so the
                # queue doesn't freeze, then continue or end the batch like a pack failure.
                failed_item = self.queue[0] if self.queue else self._active_item
                if failed_item is not None:
                    self._cleanup_after_failure(failed_item)
                if self.queue:
                    self.queue[0].status = "Failed"
                    self.queue.pop(0)
                    self._batch_failed += 1   # only count when an item was actually popped
                self.update_queue_box()
                self.log("ERROR", f"Extraction failed: {payload}")
                if self._batch_running and self.queue:
                    self.log("WARN", "Continuing batch with the next item after extraction failure.")
                    self._refresh_space_for_item(self.queue[0])
                    self.root.after(600, self._batch_auto_start)
                else:
                    self._batch_running = False
                    self.pending_start = False
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self._update_batch_counter()
                    if self._batch_total > 1:
                        self._show_batch_complete()
                    else:
                        messagebox.showerror("Extraction Failed", str(payload))
        except queue.Empty:
            pass

        try:
            success, msg, last_cmd = self.done_q.get_nowait()
            self._last_cmd_str = last_cmd

            # Mark current game done/failed and pop from queue. When the queue is already
            # empty (single-game / last-in-batch) fall back to the tracked active item so
            # a failure still cleans its scratch (otherwise a single-game failure strands).
            if self.queue:
                self.queue[0].status = "Done" if success else "Failed"
                completed_item = self.queue.pop(0)
            else:
                completed_item = self._active_item

            if success:
                self._batch_done += 1
                self.status_update("Complete", msg, "Complete", 100, 100, "—", "—", "—")
                self.log("SUCCESS", msg)
                self.play_complete_sound(True)

                # Auto-fill compatibility form with completed game data
                _final_sz = getattr(self.worker, "final_size", 0) if self.worker else 0
                completed_operation = getattr(completed_item, "operation", "pack") if completed_item else "pack"
                if completed_operation != "unpack":
                    self._compat_autofill(item=completed_item, final_size=_final_sz)
                    # Record history HERE (main thread) — add_history mutates Tk widgets.
                    try:
                        _w = self.worker
                        self.add_history(
                            completed_item,
                            getattr(_w, "output_path", "") if _w else "",
                            _final_sz,
                            (time.time() - _w.start_time) if (_w and getattr(_w, "start_time", None)) else 0.0,
                        )
                    except Exception as e:
                        self.log("WARN", f"Could not record history: {e}")

                # Feature 5: auto-clear temp after success
                if self.auto_clear_temp_var.get():
                    self._auto_clear_temp()
                # Always reclaim THIS item's extracted source — covers the spread-mode
                # extract on the OUTPUT drive, which _auto_clear_temp (temp only) misses.
                if completed_item is not None:
                    self._cleanup_item_extract(completed_item)
                    self._ampr_cleanup(completed_item)   # restore a direct source folder

                # Feature 4: batch auto-advance
                if self._batch_running and self.queue:
                    self.update_queue_box()
                    self._refresh_space_for_item(self.queue[0])
                    self.root.after(600, self._batch_auto_start)
                else:
                    self._batch_running = False
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self.update_queue_box()
                    self._update_batch_counter()
                    if self._batch_total > 1:
                        self._show_batch_complete()
                    else:
                        if self.open_output_var.get():
                            self.open_output_folder()
                        if self.summary_popup_var.get():
                            self.show_summary_popup()

                # Prompt user to share compatibility data — only if enabled, and never
                # stacked on top of the summary popup (wait until no modal is grabbing).
                if completed_operation != "unpack" and self.compat_prompt_var.get():
                    def _share_when_free(_i=completed_item, _s=_final_sz):
                        try:
                            if self.root.grab_current() is not None:
                                self.root.after(500, _share_when_free)
                                return
                        except Exception:
                            pass
                        self._prompt_compat_share(_i, _s)
                    self.root.after(400, _share_when_free)
            elif self.cancel_requested or self.extract_cancel_event.is_set():
                # A user cancel surfaces here as a failed result — treat it as a cancel,
                # not a failure: stop the batch and don't inflate the failed count.
                self._batch_running = False
                self.cancel_requested = False
                self.extract_cancel_event.clear()
                self.update_queue_box()
                self.start_btn.configure(state="normal")
                self.cancel_btn.configure(state="disabled")
                self._update_batch_counter()
                self.status_update("Ready", "Cancelled by user.", "Ready", 0, 0, "00:00", "—", "—")
                self.log("WARN", "Cancelled by user.")
                # Reclaim the cancelled run's scratch (it was popped above, so use it
                # directly — not _active_item, which would double-handle).
                if completed_item is not None:
                    self._cleanup_after_failure(completed_item)
            else:
                # OOM auto-retry: if the backend was out-of-memory-killed and we can still
                # drop a core, requeue the SAME game with fewer workers instead of failing.
                if (self.worker is not None and getattr(self.worker, "oom_killed", False)
                        and completed_item is not None and self._oom_retry(completed_item)):
                    return
                self._batch_failed += 1
                self.update_queue_box()
                self.status_update("Failed", msg, "Failed", 0, 0, "—", "—", "—")
                self.log("ERROR", msg)
                self.play_complete_sound(False)
                if completed_item is not None:
                    self._cleanup_after_failure(completed_item)
                # Batch resilience: one failure must not abort the rest of the queue.
                if self._batch_running and self.queue:
                    self.log("WARN", "Continuing batch with the next item after failure.")
                    self._refresh_space_for_item(self.queue[0])
                    self.root.after(600, self._batch_auto_start)
                else:
                    self._batch_running = False
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self._update_batch_counter()
                    if self._batch_total > 1:
                        # Aggregate (done/failed) is reported here; no modal per failure.
                        self._show_batch_complete()
                    else:
                        log_lines = get_last_log_lines(50)
                        ErrorDialog(self.root, msg, last_cmd, log_lines)
        except queue.Empty:
            pass


if _HAS_DND:
    class _CTkDnD(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)
else:
    _CTkDnD = ctk.CTk


def main():
    ensure_app_dir()
    root = _CTkDnD()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
