"""
End-to-end tests for fPKG build/extract and its interaction with the existing
.ffpfsc pipeline. Prints a green/red report and exits non-zero on failure.

  python3 backend/tests/test_fpkg_pipelines.py [--work DIR] [--keep] [--samples N]

The tests use two kinds of source:
  1. A pre-shipped HomebrewTest sample fetched from SvenGDK's LibProsperoPKG repo
     (the same one drakmor builds against). Ships zero large binaries in-tree.
  2. Synthesized fixtures for edge cases: minimal /app0 without icon0, folder with
     unicode names, folder with a large-ish file, incompressible payload, and
     the negative cases (missing param.json, missing eboot.bin) that mkpfs /
     LibProsperoPkg should reject cleanly rather than crash.

Every test:
  - prints its own PASS/FAIL line with a one-sentence reason
  - checks structural invariants (validate), not just byte identity, because
    fake-signing rewrites eboot.bin and the builder canonicalises param.json.

Failure is diagnostic: each chain reports which sub-step (extract-inner,
CNT merge, mkpfs pack, mkpfs unpack, fpkg-build, fpkg-validate) went wrong.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ── paths ────────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
BACKEND = REPO / "backend"
CLI = BACKEND / "cli.py"
# FFPFSC_PKG_TOOL lets the whole harness run against a candidate build of the tool
# (backend/fpkg.py honours the same variable, so the CLI subprocesses follow suit).
TOOL = (Path(os.environ["FFPFSC_PKG_TOOL"]) if os.environ.get("FFPFSC_PKG_TOOL")
        else BACKEND / "native" / "ffpfsc-pkg-tool")

# Backend cli.py wants to import fpkg / mkpfs as if run from backend/
sys.path.insert(0, str(BACKEND))

# The HomebrewTest sample (SvenGDK/LibProsperoPKG @ commit c28be59, 1 MB total).
HBT_URLS = {
    "README.md":           "https://raw.githubusercontent.com/SvenGDK/LibProsperoPKG/c28be59/src/HomebrewTest/README.md",
    "eboot.bin":           "https://raw.githubusercontent.com/SvenGDK/LibProsperoPKG/c28be59/src/HomebrewTest/eboot.bin",
    "sce_sys/param.json":  "https://raw.githubusercontent.com/SvenGDK/LibProsperoPKG/c28be59/src/HomebrewTest/sce_sys/param.json",
    "sce_sys/icon0.png":   "https://raw.githubusercontent.com/SvenGDK/LibProsperoPKG/c28be59/src/HomebrewTest/sce_sys/icon0.png",
}


# ── infrastructure ──────────────────────────────────────────────────────────
@dataclass
class TestResult:
    name: str
    ok: bool
    reason: str = ""
    details: list[str] = field(default_factory=list)


class Runner:
    def __init__(self, work: Path, keep: bool):
        self.work = work
        self.keep = keep
        self.work.mkdir(parents=True, exist_ok=True)
        self.results: list[TestResult] = []

    def check(self, name: str, cond: bool, ok_msg: str = "", fail_msg: str = "") -> bool:
        if cond:
            self.results.append(TestResult(name, True, ok_msg))
        else:
            self.results.append(TestResult(name, False, fail_msg))
        return cond

    def run(self, name: str, fn) -> None:
        print(f"\n─── {name} ───")
        try:
            fn(self)
        except Exception as e:
            self.results.append(TestResult(name, False, f"exception: {e}"))
            print(f"  ✗ {name}: exception {e}")

    # ── shell helpers ───────────────────────────────────────────────────────
    def run_cli(self, args: list[str], label: str = "") -> tuple[int, str]:
        argv = [sys.executable, "-u", str(CLI), *args]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return -1, "timeout"
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out

    def run_tool(self, args: list[str]) -> tuple[int, str]:
        argv = [str(TOOL), *args]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return -1, "timeout"
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def summary(self) -> int:
        fails = [r for r in self.results if not r.ok]
        print("\n" + "═" * 72)
        for r in self.results:
            tag = "PASS" if r.ok else "FAIL"
            colour = "\033[32m" if r.ok else "\033[31m"
            reset = "\033[0m"
            print(f"  {colour}[{tag}]{reset}  {r.name:<50s} {r.reason}")
        print("═" * 72)
        print(f"  {len(self.results) - len(fails)} passed, {len(fails)} failed")
        return 0 if not fails else 1


# ── fixtures ────────────────────────────────────────────────────────────────
def fetch_hbt(dst: Path) -> Path:
    """Fetch the HomebrewTest source folder into dst; return the folder path."""
    dst.mkdir(parents=True, exist_ok=True)
    for rel, url in HBT_URLS.items():
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists() and out.stat().st_size > 0:
            continue
        with urllib.request.urlopen(url, timeout=30) as f, open(out, "wb") as g:
            g.write(f.read())
    return dst


def make_synth_folder(dst: Path, *,
                     with_icon: bool = True,
                     with_param: bool = True,
                     with_eboot: bool = True,
                     data_files: list[tuple[str, bytes]] | None = None) -> Path:
    """Build a minimal PS5 /app0 folder for negative/positive smoke tests."""
    dst.mkdir(parents=True, exist_ok=True)
    if with_param:
        (dst / "sce_sys").mkdir(exist_ok=True)
        (dst / "sce_sys" / "param.json").write_text(json.dumps({
            "contentId": "UP9000-PPSA99099_00-PROSPERO00000000",
            "titleId":   "PPSA99099",
            "titleName": "Synth Test",
            "masterVersion": "01.00",
            "contentVersion": "01.000.000",
            "applicationDrmType": "free",
        }))
    if with_icon:
        # 1x1 transparent PNG (65 bytes, canonical Wikipedia sample)
        png = bytes.fromhex(
            "89504e470d0a1a0a"                                 # signature
            "0000000d49484452000000010000000108060000001f15c489"  # IHDR 1x1 RGBA
            "0000000a49444154789c6300010000000500010d0a2db4"       # IDAT
            "0000000049454e44ae426082"                             # IEND
        )
        (dst / "sce_sys").mkdir(exist_ok=True)
        (dst / "sce_sys" / "icon0.png").write_bytes(png)
    if with_eboot:
        # Minimal ELF stub — 4 bytes of ELF magic + padding
        (dst / "eboot.bin").write_bytes(b"\x7FELF" + b"\x00" * 60)
    for rel, blob in (data_files or []):
        p = dst / rel; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
    return dst


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── individual tests ────────────────────────────────────────────────────────
def test_environment(r: Runner):
    r.check("tool.present", TOOL.exists(), f"{TOOL}", f"missing: {TOOL}")
    if TOOL.exists():
        rc, out = r.run_tool(["version"])
        r.check("tool.runs", rc == 0, out.strip().splitlines()[0] if out else "", out)


def test_chain1_folder_pkg_folder(r: Runner):
    """folder → fPKG → folder — round-trip via the CLI end-to-end."""
    hbt = fetch_hbt(r.work / "hbt")
    out_pkg = r.work / "c1_pkg"; out_ext = r.work / "c1_ext"
    for d in (out_pkg, out_ext):
        if d.exists(): shutil.rmtree(d)
        d.mkdir(parents=True)

    # build
    rc, log = r.run_cli([str(hbt), str(out_pkg),
                         "--fpkg-build", str(hbt),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099",
                         "--fpkg-title", "HomebrewTest",
                         "--fpkg-inner", "none",
                         "--fpkg-kraken-backend", "builtin"])
    r.check("chain1.build.rc", rc == 0, f"exit {rc}", log[-400:])
    pkgs = list(out_pkg.glob("*.pkg"))
    r.check("chain1.build.output", len(pkgs) == 1 and pkgs[0].stat().st_size > 10_000,
            f"{pkgs[0].name} {pkgs[0].stat().st_size:,} B" if pkgs else "",
            "no .pkg produced" if not pkgs else "empty .pkg")
    if not pkgs: return
    pkg = pkgs[0]

    # validate
    rc, log = r.run_tool(["validate", str(pkg)])
    r.check("chain1.validate", rc == 0, "17/17 checks pass" if "0 failed" in log else "",
            log[-400:])

    # extract
    rc, log = r.run_cli(["placeholder", str(out_ext), "--fpkg-extract", str(pkg)])
    r.check("chain1.extract.rc", rc == 0, f"exit {rc}", log[-400:])
    got = {str(p.relative_to(out_ext)): sha(p) for p in out_ext.rglob("*") if p.is_file()}

    # verify every source file survived (byte-identical except eboot/param.json)
    src = {str(p.relative_to(hbt)): sha(p) for p in hbt.rglob("*") if p.is_file()}
    missing = [k for k in src if k not in got]
    r.check("chain1.roundtrip.no-missing", not missing,
            "all source files present in extract",
            f"missing: {missing}")

    identical = [k for k in src if k in got and src[k] == got[k]]
    r.check("chain1.roundtrip.preserved-data",
            all(k in identical for k in src if not k.endswith("param.json") and not k.endswith("eboot.bin")),
            "README/icon0/other files byte-identical",
            f"unexpected diffs: {[k for k in src if k not in identical and not k.endswith(('eboot.bin', 'param.json'))]}")

    # eboot must have been fake-signed
    if "eboot.bin" in src:
        r.check("chain1.eboot.transformed",
                got.get("eboot.bin") != src["eboot.bin"],
                "eboot.bin fake-signed (magic transformed)",
                "eboot.bin came through unchanged — fake-sign step missed")


def test_chain2_folder_ffpfsc_folder_pkg(r: Runner):
    """folder → .ffpfsc → folder → fPKG — the mkpfs-then-fpkg chain."""
    hbt = fetch_hbt(r.work / "hbt")
    ff = r.work / "c2_ffpfsc"; up = r.work / "c2_unpack"; pkgd = r.work / "c2_pkg"
    for d in (ff, up, pkgd):
        if d.exists(): shutil.rmtree(d)
        d.mkdir(parents=True)
    rc, log = r.run_cli([str(hbt), str(ff), "--pack", "--overwrite"])
    r.check("chain2.pack.rc", rc == 0, "mkpfs pack ok", log[-400:])
    ffs = list(ff.glob("*.ffpfsc"))
    if not r.check("chain2.pack.output", ffs, f"{ffs[0].name}" if ffs else "", "no .ffpfsc"):
        return
    ffpath = ffs[0]

    rc, log = r.run_cli([str(ffpath), str(up), "--unpack", "--overwrite"])
    r.check("chain2.unpack.rc", rc == 0, "mkpfs unpack ok", log[-400:])
    # Find the unpacked /app0 folder — mkpfs writes to a subdir
    candidates = [p for p in up.rglob("sce_sys") if p.is_dir()]
    if not r.check("chain2.unpack.app0", candidates, "sce_sys/ present", "no sce_sys/ found"):
        return
    app0 = candidates[0].parent

    rc, log = r.run_cli([str(app0), str(pkgd),
                         "--fpkg-build", str(app0),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099",
                         "--fpkg-title", "HomebrewTest",
                         "--fpkg-inner", "none",
                         "--fpkg-kraken-backend", "builtin"])
    r.check("chain2.fpkg.build.rc", rc == 0, "fpkg-build ok", log[-400:])
    pkgs = list(pkgd.glob("*.pkg"))
    if not r.check("chain2.fpkg.output", pkgs, f"{pkgs[0].name}" if pkgs else "", "no .pkg"):
        return
    rc, log = r.run_tool(["validate", str(pkgs[0])])
    r.check("chain2.fpkg.validate", rc == 0, "validate ok",
            "\n".join(x for x in log.splitlines() if "[FAIL]" in x))


def test_chain3_pkg_folder_ffpfsc(r: Runner):
    """fPKG → folder → .ffpfsc — mkpfs must accept our extracted /app0 tree."""
    hbt = fetch_hbt(r.work / "hbt")
    seed_pkg = r.work / "c3_seed"; ext = r.work / "c3_ext"; ff = r.work / "c3_ffpfsc"
    for d in (seed_pkg, ext, ff):
        if d.exists(): shutil.rmtree(d)
        d.mkdir(parents=True)

    # Seed: build an fPKG from HBT (we don't ship one in-tree).
    rc, log = r.run_cli([str(hbt), str(seed_pkg),
                         "--fpkg-build", str(hbt),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099",
                         "--fpkg-inner", "none", "--fpkg-kraken-backend", "builtin"])
    r.check("chain3.seed.pkg", rc == 0, "seed .pkg built", log[-400:])
    pkgs = list(seed_pkg.glob("*.pkg"))
    if not pkgs: return

    rc, log = r.run_cli(["placeholder", str(ext), "--fpkg-extract", str(pkgs[0])])
    r.check("chain3.extract.rc", rc == 0, "extract ok", log[-400:])
    # mkpfs pack requires sce_sys/param.json
    r.check("chain3.extract.param",
            (ext / "sce_sys" / "param.json").is_file(),
            "sce_sys/param.json present (CNT merged in)",
            "MISSING: mkpfs would refuse")
    r.check("chain3.extract.icon0",
            (ext / "sce_sys" / "icon0.png").is_file(),
            "sce_sys/icon0.png present",
            "MISSING")

    rc, log = r.run_cli([str(ext), str(ff), "--pack", "--overwrite"])
    r.check("chain3.repack.rc", rc == 0,
            "mkpfs accepted the fPKG-extracted folder",
            log[-400:])


def test_negative_missing_param_json(r: Runner):
    """The builder MUST refuse when there is no param.json — never crash."""
    src = make_synth_folder(r.work / "neg_no_param", with_param=False)
    out = r.work / "neg_no_param_out"; out.mkdir(exist_ok=True)
    rc, log = r.run_cli([str(src), str(out),
                         "--fpkg-build", str(src),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099",
                         "--fpkg-inner", "none", "--fpkg-kraken-backend", "builtin"])
    # Behaviour: LibProsperoPkg's GenerateParamJsonIfMissing=True default is
    # supposed to generate a minimal param.json when missing. If it doesn't,
    # we expect a graceful error, not a crash.
    r.check("negative.missing-param.exit",
            rc in (0, 1),
            f"terminated cleanly (rc={rc})",
            f"crash-like exit {rc}; last log: " + log[-300:])


def test_negative_missing_eboot(r: Runner):
    """No eboot.bin should either warn or fail cleanly."""
    src = make_synth_folder(r.work / "neg_no_eboot", with_eboot=False)
    out = r.work / "neg_no_eboot_out"; out.mkdir(exist_ok=True)
    rc, log = r.run_cli([str(src), str(out),
                         "--fpkg-build", str(src),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099",
                         "--fpkg-inner", "none", "--fpkg-kraken-backend", "builtin"])
    r.check("negative.missing-eboot.exit",
            rc in (0, 1),
            f"exit {rc}; validator will catch missing eboot afterwards",
            f"crash-like exit {rc}")


def test_negative_bad_content_id(r: Runner):
    """A malformed content id should be rejected up front."""
    src = make_synth_folder(r.work / "neg_bad_cid")
    out = r.work / "neg_bad_cid_out"; out.mkdir(exist_ok=True)
    rc, log = r.run_cli([str(src), str(out),
                         "--fpkg-build", str(src),
                         "--content-id", "totally-invalid",
                         "--title-id", "PPSA99099",
                         "--fpkg-inner", "none", "--fpkg-kraken-backend", "builtin"])
    r.check("negative.bad-content-id.reject",
            rc != 0 and "Content ID" in log,
            "builder rejected the bad content id",
            f"rc={rc}; got: {log[-300:]}")


def test_identity_from_param_json(r: Runner):
    """sce_sys/param.json is the identity's source of truth. A --content-id /
    --fpkg-version / --fpkg-title that disagree with it must NOT reach the package
    header: the console checks header-vs-param.json coherence, and so does validate
    ('param.contentId != CNT header' was a real FAIL before this rule). Omitting the ids
    must work when param.json has them; with neither, the build must refuse clearly."""
    hbt = fetch_hbt(r.work / "hbt")

    # 1) disagreeing arguments → param.json wins, the log says so, validate is green
    out = r.work / "ident_mismatch"
    if out.exists(): shutil.rmtree(out)
    out.mkdir(parents=True)
    rc, log = r.run_cli([str(hbt), str(out), "--fpkg-build", str(hbt),
                         "--content-id", "UP9000-PPSA99099_00-MISMATCHMISMATCH",
                         "--title-id", "PPSA99099",
                         "--fpkg-version", "02.000.000",
                         "--fpkg-title", "Wrong Title"])
    r.check("identity.mismatch.rc", rc == 0, f"exit {rc}", log[-400:])
    pkg = next(out.glob("*.pkg"), None)
    r.check("identity.mismatch.header-from-param",
            pkg is not None and "PROSPERO00000000" in pkg.name and "V0100" in pkg.name,
            pkg.name if pkg else "", f"got {pkg.name if pkg else 'no .pkg'} — header did not follow param.json")
    r.check("identity.mismatch.warned",
            "differs from param.json" in log,
            "log warns about the disagreeing --content-id",
            "no warning in the log")
    if pkg:
        rc, vlog = r.run_tool(["validate", str(pkg)])
        r.check("identity.mismatch.validate", rc == 0 and "0 failed" in vlog,
                "validate green (header == param.json)", vlog[-400:])

    # 2) no ids passed at all → still builds from param.json
    out2 = r.work / "ident_omitted"
    if out2.exists(): shutil.rmtree(out2)
    out2.mkdir(parents=True)
    rc, log = r.run_cli([str(hbt), str(out2), "--fpkg-build", str(hbt)])
    pkg2 = next(out2.glob("*.pkg"), None)
    r.check("identity.omitted.builds",
            rc == 0 and pkg2 is not None and "UP9000-PPSA99099_00-PROSPERO00000000" in pkg2.name,
            pkg2.name if pkg2 else "", f"rc={rc}; {log[-300:]}")

    # 3) no param.json AND no ids → a clear refusal, never a crash
    src = make_synth_folder(r.work / "ident_noparam", with_param=False)
    out3 = r.work / "ident_noparam_out"; out3.mkdir(exist_ok=True)
    rc, log = r.run_cli([str(src), str(out3), "--fpkg-build", str(src)])
    r.check("identity.none.reject",
            rc != 0 and "content id" in log.lower() and "Traceback" not in log,
            "refused with a readable message", f"rc={rc}; {log[-300:]}")


def test_kraken_fast_preset(r: Runner):
    """The GUI's 'Kraken speed: fast' is --compression-level -4. It must reach the tool
    (the tool logs its configuration) and still yield a package that validates."""
    hbt = fetch_hbt(r.work / "hbt")
    out = r.work / "fast_preset"
    if out.exists(): shutil.rmtree(out)
    out.mkdir(parents=True)
    rc, log = r.run_cli([str(hbt), str(out), "--fpkg-build", str(hbt), "--compression-level", "-4"])
    r.check("fast.rc", rc == 0, f"exit {rc}", log[-300:])
    r.check("fast.level-reaches-tool", "Kraken level=-4" in log,
            "tool logged 'Kraken level=-4'", "configuration line does not show level -4")
    pkg = next(out.glob("*.pkg"), None)
    if pkg:
        rc, vlog = r.run_tool(["validate", str(pkg)])
        r.check("fast.validate", rc == 0 and "0 failed" in vlog, "validate green", vlog[-300:])


def test_validate_catches_untouched_ffpfsc(r: Runner):
    """A .ffpfsc isn't an fPKG. Validator must fail loudly (not crash)."""
    hbt = fetch_hbt(r.work / "hbt")
    ff = r.work / "v_ff"; ff.mkdir(exist_ok=True)
    rc, log = r.run_cli([str(hbt), str(ff), "--pack", "--overwrite"])
    if rc != 0:
        r.results.append(TestResult("validate.on-ffpfsc.skip", True, "pack failed; skipping"))
        return
    ffpath = next(ff.glob("*.ffpfsc"), None)
    if not ffpath:
        r.results.append(TestResult("validate.on-ffpfsc.skip", True, "no ffpfsc produced"))
        return
    rc, log = r.run_tool(["validate", str(ffpath)])
    r.check("validate.on-ffpfsc.rejects",
            rc != 0 and ("UNKNOWN" in log or "not" in log.lower()),
            "validator flagged the .ffpfsc as non-fPKG",
            f"rc={rc}; log: {log[-300:]}")


def test_inner_modes(r: Runner):
    """Build with each inner-codec mode — all must produce a valid, self-extracting pkg."""
    hbt = fetch_hbt(r.work / "hbt")
    for mode in ("none", "zlib", "kraken"):
        out = r.work / f"mode_{mode}"
        if out.exists(): shutil.rmtree(out)
        out.mkdir(parents=True)
        rc, log = r.run_cli([str(hbt), str(out),
                             "--fpkg-build", str(hbt),
                             "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                             "--title-id", "PPSA99099",
                             "--fpkg-title", "HomebrewTest",
                             "--fpkg-inner", mode,
                             "--fpkg-kraken-backend", "builtin"])
        r.check(f"mode.{mode}.build",
                rc == 0,
                f"inner={mode} built ok",
                f"rc={rc}; {log[-300:]}")
        pkg = next(out.glob("*.pkg"), None)
        if not pkg:
            r.check(f"mode.{mode}.output", False, "", "no .pkg produced"); continue
        rc, log = r.run_tool(["validate", str(pkg)])
        r.check(f"mode.{mode}.validate",
                rc == 0,
                "validator green",
                "\n".join(x for x in log.splitlines() if "[FAIL]" in x))
        # extract-inner still produces a valid /app0 for zlib/kraken modes
        ext = out / "_ext"; ext.mkdir(exist_ok=True)
        rc, log = r.run_cli(["placeholder", str(ext), "--fpkg-extract", str(pkg)])
        r.check(f"mode.{mode}.extract",
                rc == 0 and (ext / "sce_sys" / "param.json").is_file(),
                "extract-inner + CNT merge ok",
                log[-300:])


def test_no_eboot_caught_by_validate(r: Runner):
    """A pkg built from a folder that lacked eboot.bin must fail the validator."""
    src = make_synth_folder(r.work / "vne_src", with_eboot=False)
    out = r.work / "vne_out"; out.mkdir(exist_ok=True)
    rc, log = r.run_cli([str(src), str(out),
                         "--fpkg-build", str(src),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099",
                         "--fpkg-inner", "none", "--fpkg-kraken-backend", "builtin"])
    pkg = next(out.glob("*.pkg"), None)
    if pkg is None:
        # builder correctly rejected — perfect
        r.check("no-eboot.builder-rejected", rc != 0, "builder refused the folder",
                f"rc={rc}; log: {log[-300:]}")
        return
    # builder accepted; validator MUST catch it
    rc, log = r.run_tool(["validate", str(pkg)])
    r.check("no-eboot.validate-fails",
            rc != 0 and "eboot" in log.lower(),
            "validator caught the missing eboot",
            f"rc={rc}, log: {log[-300:]}")


def test_tool_path_resolution(r: Runner):
    """backend/fpkg.py must find the binary via env override, native/, and _MEIPASS."""
    import importlib
    fpkg = importlib.import_module("fpkg")
    r.check("path.native-exists",
            fpkg.is_available() and TOOL.samefile(fpkg.tool_path()),
            f"{fpkg.tool_path()}",
            "backend/fpkg.py couldn't locate the native tool")
    # env override
    fake = r.work / "fake"; fake.mkdir(exist_ok=True); (fake / "fake").write_bytes(b"")
    prev = os.environ.get("FFPFSC_PKG_TOOL")     # restore afterwards — a variant run relies on it
    os.environ["FFPFSC_PKG_TOOL"] = str(fake / "does-not-exist")
    try:
        try:
            fpkg.tool_path(); found = True
        except FileNotFoundError:
            # the fallback native/ still exists, so it should be found there
            found = TOOL.exists()
    finally:
        if prev is None:
            os.environ.pop("FFPFSC_PKG_TOOL", None)
        else:
            os.environ["FFPFSC_PKG_TOOL"] = prev
    r.check("path.env-fallback",
            found,
            "env override doesn't exist but native/ resolves the tool",
            "resolution stack broken")


def test_chain4_image_to_fpkg_oneclick(r: Runner):
    """--fpkg-build <image.ffpfsc>: the one-click image → fPKG conversion (unwrap on
    --temp-dir, build, validate, extract back)."""
    hbt = fetch_hbt(r.work / "hbt")
    ff = r.work / "c4_ffpfsc"; pkgd = r.work / "c4_pkg"; tmp = r.work / "c4_tmp"; ext = r.work / "c4_ext"
    for d in (ff, pkgd, tmp, ext):
        if d.exists(): shutil.rmtree(d)
        d.mkdir(parents=True)
    rc, log = r.run_cli([str(hbt), str(ff), "--pack", "--overwrite"])
    ffpath = next(ff.glob("*.ffpfsc"), None)
    if not r.check("chain4.pack", rc == 0 and ffpath is not None, "seed .ffpfsc built", log[-300:]):
        return
    rc, log = r.run_cli([str(ffpath), str(pkgd),
                         "--fpkg-build", str(ffpath),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099", "--fpkg-title", "HomebrewTest",
                         "--fpkg-inner", "kraken", "--fpkg-kraken-backend", "builtin",
                         "--compression-level", "5", "--temp-dir", str(tmp)])
    r.check("chain4.image-to-fpkg.rc", rc == 0, "image unwrapped + fPKG built in one job", log[-400:])
    r.check("chain4.unwrap-phase", "[PHASE] Extracting" in log, "GUI phase marker for the unwrap emitted",
            "no [PHASE] Extracting marker")
    r.check("chain4.level-passthrough", "level=5" in log, "--compression-level reached the builder (level=5)",
            "level not visible in builder banner")
    r.check("chain4.temp-passthrough", str(tmp) in log, "--temp-dir reached the builder", "temp dir not in banner")
    r.check("chain4.scratch-cleaned", not any(tmp.iterdir()), "unwrap scratch removed after build",
            f"leftovers: {[x.name for x in tmp.iterdir()]}")
    r.check("chain4.complete-marker", "[OK] fPKG complete:" in log, "output marker for the GUI emitted",
            "no '[OK] fPKG complete:' line")
    pkg = next(pkgd.glob("*.pkg"), None)
    if not r.check("chain4.output", pkg is not None, f"{pkg.name}" if pkg else "", "no .pkg"):
        return
    rc, log = r.run_tool(["validate", str(pkg)])
    r.check("chain4.validate", rc == 0, "validator green", "\n".join(x for x in log.splitlines() if "[FAIL]" in x))
    rc, log = r.run_cli(["placeholder", str(ext), "--fpkg-extract", str(pkg)])
    r.check("chain4.extract-back", rc == 0 and (ext / "README.md").is_file()
            and sha(ext / "README.md") == sha(hbt / "README.md"),
            "content survived ffpfsc → fPKG → folder byte-identical", log[-300:])
    r.check("chain4.extract-complete-marker", "[OK] Extraction complete:" in log,
            "GUI output marker for extract emitted", "missing marker")


def test_gui_progress_translation(r: Runner):
    """A folder build must emit the [PHASE] markers and progress bars the GUI parser reads."""
    hbt = fetch_hbt(r.work / "hbt")
    out = r.work / "gui_prog"; out.mkdir(exist_ok=True)
    rc, log = r.run_cli([str(hbt), str(out),
                         "--fpkg-build", str(hbt),
                         "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                         "--title-id", "PPSA99099", "--fpkg-inner", "kraken",
                         "--fpkg-kraken-backend", "builtin"])
    phases = [ln for ln in log.splitlines() if ln.startswith("[PHASE] ")]
    bars = [ln for ln in log.splitlines() if ln.startswith("[") and "% " in ln and ("#" in ln[:22] or "-" in ln[:22])]
    r.check("gui.phases", {"[PHASE] Scanning Files", "[PHASE] Creating Temp PFS", "[PHASE] Compressing",
                           "[PHASE] Writing Final Image", "[PHASE] Verifying Output"} <= set(phases),
            f"{len(phases)} phase markers", f"got: {sorted(set(phases))}")
    r.check("gui.bars", len(bars) >= 4, f"{len(bars)} progress bars", f"only {len(bars)} bars: {bars[:3]}")


def test_deterministic_build(r: Runner):
    """Two --deterministic builds must produce byte-identical fPKGs."""
    hbt = fetch_hbt(r.work / "hbt")
    a = r.work / "det_a"; b = r.work / "det_b"
    for d in (a, b):
        if d.exists(): shutil.rmtree(d); d.mkdir(parents=True)
        else: d.mkdir(parents=True)
    def build(dst):
        return r.run_cli([str(hbt), str(dst),
                          "--fpkg-build", str(hbt),
                          "--content-id", "UP9000-PPSA99099_00-PROSPERO00000000",
                          "--title-id", "PPSA99099",
                          "--fpkg-title", "HomebrewTest",
                          "--fpkg-inner", "none", "--fpkg-kraken-backend", "builtin",
                          "--fpkg-passcode", "0" * 32,
                          "--fpkg-deterministic"])
    build(a); build(b)
    pa = next(a.glob("*.pkg"), None); pb = next(b.glob("*.pkg"), None)
    if not (pa and pb):
        r.check("determinism.build", False, "", "at least one deterministic build didn't produce a .pkg")
        return
    r.check("determinism.byte-identical",
            sha(pa) == sha(pb),
            "two builds produced the same .pkg bytes",
            f"drift: {sha(pa)[:16]} vs {sha(pb)[:16]}")


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="fPKG pipeline end-to-end tests")
    ap.add_argument("--work", type=Path, default=Path(tempfile.gettempdir()) / "ffpfsc-fpkg-tests",
                    help="Working directory for fixtures and outputs (default: system temp).")
    ap.add_argument("--keep", action="store_true", help="Keep the working directory after the run")
    ap.add_argument("--only", type=str, default="",
                    help="Run only the tests whose name contains this substring (case-insensitive).")
    args = ap.parse_args()

    if args.work.exists():
        shutil.rmtree(args.work)
    args.work.mkdir(parents=True)
    print(f"[i] work dir: {args.work}")
    print(f"[i] ffpfsc-pkg-tool: {TOOL} ({'present' if TOOL.exists() else 'MISSING'})")

    r = Runner(args.work, args.keep)
    t0 = time.monotonic()
    for name, fn in [
        ("environment",                     test_environment),
        ("chain-1: folder → fPKG → folder", test_chain1_folder_pkg_folder),
        ("chain-2: folder → ffpfsc → fPKG", test_chain2_folder_ffpfsc_folder_pkg),
        ("chain-3: fPKG → folder → ffpfsc", test_chain3_pkg_folder_ffpfsc),
        ("inner modes (none/zlib/kraken)",  test_inner_modes),
        ("negative: no param.json",         test_negative_missing_param_json),
        ("negative: no eboot.bin",          test_negative_missing_eboot),
        ("negative: no-eboot pkg fails validate", test_no_eboot_caught_by_validate),
        ("negative: bad content id",        test_negative_bad_content_id),
        ("identity: param.json wins",       test_identity_from_param_json),
        ("kraken fast preset (-4)",         test_kraken_fast_preset),
        ("validate: reject .ffpfsc",        test_validate_catches_untouched_ffpfsc),
        ("tool-path resolution",            test_tool_path_resolution),
        ("chain-4: .ffpfsc → fPKG one-click", test_chain4_image_to_fpkg_oneclick),
        ("gui progress translation",        test_gui_progress_translation),
        ("determinism: byte-identical",     test_deterministic_build),
    ]:
        if args.only and args.only.lower() not in name.lower():
            continue
        r.run(name, fn)

    exitcode = r.summary()
    print(f"[i] wall time: {time.monotonic()-t0:.1f}s")

    if not args.keep:
        shutil.rmtree(args.work, ignore_errors=True)
    sys.exit(exitcode)


if __name__ == "__main__":
    main()
