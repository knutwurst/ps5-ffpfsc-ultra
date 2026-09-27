"""End-to-end test on a REAL game: every conversion path, compared file by file.

    python3 backend/tests/test_e2e_corpus.py --source <game.ffpfsc | game folder> --work <scratch dir>
                                             [--app "<PS5 FFPFSC ULTRA.app>"] [--keep]

The source is the reference (a container known to run on the console, or its folder).
Nothing is written next to it; everything happens in --work, which needs about four times
the game size. Paths (each result is compared against the reference folder R):

  R        the source itself (folder) or the source .ffpfsc unpacked
  C1       .ffpfsc/folder → .pkg (one click) → validate → extract     == R  (expected changes only)
  C2       R → .ffpfsc → unpack                                        == R  (byte for byte)
  C3       R → .ffpfs (uncompressed) → unpack                          == R  (byte for byte)
  C4       extract of C1 → .ffpfsc → unpack                            == extract of C1
  C5       .pkg built from R vs .pkg built from the container, both --fpkg-deterministic: same bytes

"Expected changes" in a package are exactly what the builder does on purpose and nothing
else: raw ELFs become fake-signed SELFs (and SELFs get the retail flag byte), sce_sys/
param.json is canonicalised, placeholder license files and non-JSON *.json in sce_sys are
dropped, a corrupt PlayGo set is regenerated, DDS icons are added, and ampr_emu.index is
rebuilt when the AMPR emulator ships. Every other file must be byte-identical and present.
sce_sys/keystone (the save-data key) must always be identical.

The report is written to <work>/e2e-report.json and printed; exit code 0 = all passed.
With --app the frozen app's backend (…/Contents/MacOS/…) is used instead of backend/cli.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "backend" / "cli.py"
SELF_MAGIC = b"\x54\x14\xF5\xEE"
ELF_MAGIC = b"\x7fELF"
EXEC_SUFFIXES = (".bin", ".elf", ".prx", ".sprx", ".self")
SCE_SYS_DROPPABLE = {"license.dat", "license.info", "origin-param.json",
                     "playgo-chunk.dat", "playgo-hash-table.dat", "playgo-ficm.dat"}


class E2E:
    def __init__(self, work: Path, app: Path | None):
        self.work = work
        self.app = app
        self.results: list[dict] = []

    # ── plumbing ─────────────────────────────────────────────────────────────
    def backend(self, args: list[str], label: str, timeout: int = 7200) -> tuple[int, str]:
        if self.app:
            exe = next((self.app / "Contents" / "MacOS").iterdir())
            argv = [str(exe), "--backend-internal", *args]
        else:
            argv = [sys.executable, "-u", str(CLI), *args]
        t0 = time.time()
        proc = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout)
        log = (proc.stdout or "") + (proc.stderr or "")
        (self.work / f"{label}.log").write_text(log, encoding="utf-8")
        print(f"  … {label}: rc={proc.returncode} in {time.time() - t0:.0f}s", flush=True)
        return proc.returncode, log

    def tool(self) -> Path:
        if self.app:
            return next(self.app.rglob("ffpfsc-pkg-tool"))
        return REPO / "backend" / "native" / "ffpfsc-pkg-tool"

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append({"check": name, "ok": bool(ok), "detail": detail[:2000]})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail[:160]}", flush=True)
        return bool(ok)

    @staticmethod
    def tree(root: Path) -> dict[str, Path]:
        out = {}
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                if fn == ".DS_Store" or fn.startswith("._"):
                    continue
                p = Path(dp) / fn
                out[p.relative_to(root).as_posix()] = p
        return out

    @staticmethod
    def sha(p: Path) -> str:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def head(p: Path, n: int = 4) -> bytes:
        with open(p, "rb") as f:
            return f.read(n)

    def app0_in(self, root: Path) -> Path | None:
        hits = sorted((p.parent for p in root.rglob("sce_sys") if p.is_dir()), key=lambda p: len(p.parts))
        return hits[0] if hits else None

    # ── comparisons ──────────────────────────────────────────────────────────
    def identical(self, name: str, ref: Path, got: Path) -> None:
        a, b = self.tree(ref), self.tree(got)
        missing = sorted(set(a) - set(b)); extra = sorted(set(b) - set(a))
        differ = [k for k in sorted(set(a) & set(b))
                  if a[k].stat().st_size != b[k].stat().st_size or self.sha(a[k]) != self.sha(b[k])]
        self.check(f"{name}.byte-identical", not (missing or extra or differ),
                   f"{len(a)} files; missing={missing[:5]} extra={extra[:5]} differ={differ[:5]}")

    def expected_changes_only(self, name: str, ref: Path, got: Path, ampr: bool) -> None:
        a, b = self.tree(ref), self.tree(got)
        problems, changed = [], []
        for rel, rp in sorted(a.items()):
            low = rel.lower()
            gp = b.get(rel)
            base = low.rsplit("/", 1)[-1]
            if gp is None:
                if low.startswith("sce_sys/") and (base in SCE_SYS_DROPPABLE or base.endswith(".json")):
                    changed.append(f"dropped {rel}")
                    continue
                problems.append(f"missing {rel}")
                continue
            if rp.stat().st_size == gp.stat().st_size and self.sha(rp) == self.sha(gp):
                continue
            if low == "sce_sys/keystone":
                problems.append("keystone differs (save-data key must be kept)")
            elif low.endswith(EXEC_SUFFIXES) and self.head(gp) == SELF_MAGIC and self.head(rp) in (ELF_MAGIC, SELF_MAGIC):
                changed.append(f"signed/flagged {rel}")
            elif low == "sce_sys/param.json":
                try:
                    ra, ga = json.loads(rp.read_text("utf-8")), json.loads(gp.read_text("utf-8"))
                    same = all(ra.get(k) == ga.get(k) for k in ("titleId", "contentId", "contentVersion", "masterVersion"))
                except Exception as e:
                    same = False; problems.append(f"param.json unreadable: {e}")
                (changed if same else problems).append("param.json canonicalised" if same else "param.json identity changed")
            elif low.startswith("sce_sys/playgo-"):
                changed.append(f"regenerated {rel}")
            elif ampr and low == "ampr_emu.index" and self.head(gp, 8) == b"AMPRIDX3":
                changed.append("ampr_emu.index rebuilt")
            else:
                problems.append(f"differs {rel}")
        for rel in sorted(set(b) - set(a)):
            low = rel.lower()
            if low.startswith("sce_sys/") and (low.endswith(".dds") or "/about/" in low or low.endswith(
                    ("playgo-chunk.dat", "playgo-hash-table.dat", "playgo-ficm.dat", "pfs-version.dat", "keystone",
                     "icon0.png", "pic0.png", "pic1.png", "param.json", "npbind.dat", "changeinfo.xml"))):
                changed.append(f"added {rel}")
            elif ampr and low == "ampr_emu.index":
                changed.append("added ampr_emu.index")
            else:
                problems.append(f"unexpected extra {rel}")
        self.check(f"{name}.expected-changes-only", not problems,
                   f"{len(a)} files; changes={len(changed)}; problems={problems[:8]}")
        (self.work / f"{name}-changes.txt").write_text("\n".join(changed + ["---"] + problems), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="End-to-end test on a real game")
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--app", type=Path, default=None, help="use this built .app's backend and tool")
    ap.add_argument("--keep", action="store_true", help="keep intermediate outputs")
    a = ap.parse_args()
    src = a.source.resolve(); work = a.work.resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    e = E2E(work, a.app.resolve() if a.app else None)
    tmp = work / "tmp"; tmp.mkdir()
    print(f"source: {src}\nwork:   {work}\nbackend: {'app ' + str(e.app) if e.app else 'source tree'}", flush=True)

    # R — the reference folder
    if src.is_dir():
        ref = src
    else:
        rc, log = e.backend([str(src), str(work / "R"), "--unpack", "--overwrite", "--temp-dir", str(tmp)], "R-unpack")
        ref = e.app0_in(work / "R") if rc == 0 else None
        if not e.check("R.unpack", ref is not None, log[-300:] if ref is None else str(ref)):
            return 1
    ampr = (ref / "fakelib" / "libSceAmpr.sprx").is_file()
    print(f"reference: {len(E2E.tree(ref))} files, AMPR emulator: {ampr}", flush=True)

    # C1 — container/folder → .pkg → validate → extract
    rc, log = e.backend([str(src), str(work / "C1"), "--fpkg-build", str(src), "--fpkg-inner", "kraken",
                         "--fpkg-kraken-backend", "builtin", "--compression-level", "7", "--temp-dir", str(tmp)], "C1-build")
    pkg = next((work / "C1").glob("*.pkg"), None)
    if e.check("C1.build", rc == 0 and pkg is not None, log[-300:]):
        e.check("C1.validate", "summary:" in log and " 0 failed" in log, next((l for l in log.splitlines() if "summary:" in l), ""))
        tool = e.tool()
        x = subprocess.run([str(tool), "extract-inner", str(pkg), str(work / "C1x")], capture_output=True, text=True, errors="replace")
        (work / "C1-extract.log").write_text((x.stdout or "") + (x.stderr or ""), encoding="utf-8")
        if e.check("C1.extract", x.returncode == 0, (x.stderr or "")[-300:]):
            e.expected_changes_only("C1", ref, work / "C1x", ampr)

    # C2 — R → .ffpfsc → unpack
    rc, log = e.backend([str(ref), str(work / "C2"), "--pack", "--overwrite", "--temp-dir", str(tmp)], "C2-pack")
    ff = next((work / "C2").glob("*.ffpfsc"), None)
    if e.check("C2.pack", rc == 0 and ff is not None, log[-300:]):
        rc, log = e.backend([str(ff), str(work / "C2u"), "--unpack", "--overwrite", "--temp-dir", str(tmp)], "C2-unpack")
        got = e.app0_in(work / "C2u")
        if e.check("C2.unpack", rc == 0 and got is not None, log[-300:]):
            e.identical("C2", ref, got)

    # C3 — R → .ffpfs (uncompressed) → unpack
    rc, log = e.backend([str(ref), str(work / "C3"), "--pack", "--no-compress", "--overwrite", "--temp-dir", str(tmp)], "C3-pack")
    fs = next((work / "C3").glob("*.ffpfs"), None)
    if e.check("C3.pack", rc == 0 and fs is not None, log[-300:]):
        rc, log = e.backend([str(fs), str(work / "C3u"), "--unpack", "--overwrite", "--temp-dir", str(tmp)], "C3-unpack")
        got = e.app0_in(work / "C3u")
        if e.check("C3.unpack", rc == 0 and got is not None, log[-300:]):
            e.identical("C3", ref, got)

    # C4 — extract of C1 → .ffpfsc → unpack == extract of C1
    if (work / "C1x").is_dir():
        rc, log = e.backend([str(work / "C1x"), str(work / "C4"), "--pack", "--overwrite", "--temp-dir", str(tmp)], "C4-pack")
        ff4 = next((work / "C4").glob("*.ffpfsc"), None)
        if e.check("C4.pack", rc == 0 and ff4 is not None, log[-300:]):
            rc, log = e.backend([str(ff4), str(work / "C4u"), "--unpack", "--overwrite", "--temp-dir", str(tmp)], "C4-unpack")
            got = e.app0_in(work / "C4u")
            if e.check("C4.unpack", rc == 0 and got is not None, log[-300:]):
                e.identical("C4", work / "C1x", got)

    # C5 — deterministic .pkg from the folder and from the container are the same bytes
    if not src.is_dir():
        shas = []
        for tag, s in (("C5a", ref), ("C5b", src)):
            rc, log = e.backend([str(s), str(work / tag), "--fpkg-build", str(s), "--fpkg-inner", "kraken",
                                 "--fpkg-kraken-backend", "builtin", "--compression-level", "7",
                                 "--fpkg-deterministic", "--temp-dir", str(tmp)], f"{tag}-build")
            p = next((work / tag).glob("*.pkg"), None)
            shas.append(E2E.sha(p) if (rc == 0 and p) else f"failed:{tag}")
        e.check("C5.folder-vs-container-identical", shas[0] == shas[1], f"{shas[0][:16]} vs {shas[1][:16]}")

    report = {"source": str(src), "passed": sum(r["ok"] for r in e.results),
              "failed": sum(not r["ok"] for r in e.results), "results": e.results}
    (work / "e2e-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n{report['passed']} passed, {report['failed']} failed — report: {work / 'e2e-report.json'}")
    if not a.keep:
        for d in ("C2", "C2u", "C3", "C3u", "C4", "C4u", "C5a", "C5b", "tmp"):
            shutil.rmtree(work / d, ignore_errors=True)
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
