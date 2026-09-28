"""CHAIN MODE (--to): any source → [patch → backport → sign] → any output, in one call.

Runs the real CLI on a tiny synthetic game (a raw ELF eboot with an SCE param
segment, a param.json, one data file) through the vendored mkpfs, so the container
paths are exercised for real:

    folder → folder            no changes  → refused ("nothing to do")
    folder → folder + backport → in place, SDK lowered
    folder → .ffpfsc           pass-through to the pack path
    .ffpfsc → folder + backport → unpacked into scratch, lowered, moved to the output
    .ffpfsc → .ffpfsc + backport → the user's case: unpack, lower, repack; source untouched
    .ffpfsc → .ffpfsc no changes → copy job

    python3 -m unittest backend.tests.test_chain_mode
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "backend" / "cli.py"
sys.path.insert(0, str(REPO / "backend" / "tests"))
from test_backport import _elf_with_param   # noqa: E402
from backend import backport as bp         # noqa: E402

SDK_HIGH_PS5, SDK_HIGH_PS4 = 0x08000041, 0x11090001
SDK_761_PS5, SDK_761_PS4 = bp.SDK_TARGETS["7.61"]


def make_game(root: Path) -> Path:
    g = root / "Example [PPSA00001]"
    (g / "sce_sys").mkdir(parents=True)
    (g / "sce_sys" / "param.json").write_text(json.dumps({
        "titleId": "PPSA00001", "contentId": "UP0000-PPSA00001_00-EXAMPLE000000000",
        "contentVersion": "01.000.000", "attribute": 0,
        "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": "Example"}},
    }), encoding="utf-8")
    (g / "eboot.bin").write_bytes(_elf_with_param(bp.PT_SCE_PROCPARAM, 0x4942524F, SDK_HIGH_PS4, SDK_HIGH_PS5))
    (g / "data.bin").write_bytes(bytes(range(256)) * 64)
    return g


def sdk_of(eboot: Path) -> tuple[int, int]:
    d = eboot.read_bytes()
    base = 64 + 56          # ehdr + one phdr, as the fixture lays it out
    return struct.unpack_from("<I", d, base + 0x14)[0], struct.unpack_from("<I", d, base + 0x10)[0]


def run(*argv: str, timeout: int = 600) -> tuple[int, str]:
    p = subprocess.run([sys.executable, "-u", str(CLI), *argv], capture_output=True, text=True,
                       errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


class ChainMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.root = Path(cls.td.name)
        cls.game = make_game(cls.root / "src")
        cls.temp = cls.root / "temp"; cls.temp.mkdir()

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def test_1_folder_to_folder_without_changes_is_refused(self):
        rc, log = run(str(self.game), str(self.root / "o1"), "--to", "folder")
        self.assertEqual(rc, 1, log)
        self.assertIn("Nothing to do", log)

    def test_2_folder_to_folder_with_backport_changes_in_place(self):
        game = make_game(self.root / "src2")
        rc, log = run(str(game), str(self.root / "o2"), "--to", "folder", "--backport-target", "7.61")
        self.assertEqual(rc, 0, log)
        self.assertIn("Changed in place", log)
        self.assertEqual(sdk_of(game / "eboot.bin"), (SDK_761_PS5, SDK_761_PS4))

    def test_3_folder_to_ffpfsc_passes_through_to_pack(self):
        out = self.root / "o3"; out.mkdir()
        rc, log = run(str(self.game), str(out), "--to", "ffpfsc", "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-1500:])
        imgs = list(out.glob("*.ffpfsc"))
        self.assertEqual(len(imgs), 1, log[-800:])
        # The source folder was not changed (no transforms requested).
        self.assertEqual(sdk_of(self.game / "eboot.bin"), (SDK_HIGH_PS5, SDK_HIGH_PS4))
        type(self).ffpfsc = imgs[0]

    def test_4_ffpfsc_to_folder_with_backport(self):
        src = getattr(type(self), "ffpfsc", None) or self._pack_once()
        out = self.root / "o4"; out.mkdir()
        rc, log = run(str(src), str(out), "--to", "folder", "--backport-target", "7.61",
                      "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-1500:])
        dest = out / f"{src.stem}_extracted"
        eboot = next(dest.rglob("eboot.bin"), None)
        self.assertIsNotNone(eboot, f"no eboot under {dest}: {log[-800:]}")
        self.assertEqual(sdk_of(eboot), (SDK_761_PS5, SDK_761_PS4))
        self.assertTrue(src.is_file(), "the source image must be left in place")
        self.assertFalse(list((self.temp / "_ffpfsc_temp").glob("chain-*")), "scratch must be cleaned up")

    def test_5_ffpfsc_to_ffpfsc_with_backport_is_one_job(self):
        """The user's case: an existing .ffpfsc lowered to 7.61 and repacked, one call."""
        src = getattr(type(self), "ffpfsc", None) or self._pack_once()
        before = src.read_bytes()
        out = self.root / "o5"; out.mkdir()
        rc, log = run(str(src), str(out), "--to", "ffpfsc", "--backport-target", "7.61",
                      "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-1500:])
        built = list(out.glob("*.ffpfsc"))
        self.assertEqual(len(built), 1, log[-800:])
        self.assertEqual(src.read_bytes(), before, "the source image must be untouched")
        self.assertIn("CHAIN:", log); self.assertIn("backport 7.61", log)
        # Unpack the result and check the lowered SDK survived the repack.
        chk = self.root / "o5u"
        rc, log2 = run(str(built[0]), str(chk), "--unpack", "--overwrite", "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log2[-1000:])
        eboot = next(chk.rglob("eboot.bin"), None)
        self.assertIsNotNone(eboot, log2[-800:])
        self.assertEqual(sdk_of(eboot), (SDK_761_PS5, SDK_761_PS4))

    def test_6_same_format_without_changes_is_a_copy(self):
        """Same container format, nothing to change → the copy job. On the same drive
        that is an atomic rename (the source moves), exactly like --copy; --keep-source
        only matters for a cross-drive copy."""
        src = getattr(type(self), "ffpfsc", None) or self._pack_once()
        out = self.root / "o6"; out.mkdir()
        rc, log = run(str(src), str(out), "--to", "ffpfsc", "--keep-source")
        self.assertEqual(rc, 0, log[-1000:])
        self.assertTrue((out / src.name).is_file(), log[-800:])
        self.assertIn("move", log.lower())
        self.assertFalse(src.is_file(), "same-drive: the source is renamed into the output")

    def _pack_once(self) -> Path:
        out = self.root / "o3"; out.mkdir(exist_ok=True)
        rc, log = run(str(self.game), str(out), "--to", "ffpfsc", "--temp-dir", str(self.temp))
        self.assertEqual(rc, 0, log[-1500:])
        img = next(out.glob("*.ffpfsc"))
        type(self).ffpfsc = img
        return img


if __name__ == "__main__":
    unittest.main()
