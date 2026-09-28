"""Chain job items (Tk-free): GameItem.from_chain and the derived summary sentence the
job dialog shows and the queue row carries.

    python3 -m unittest backend.tests.test_chain_items
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import ultra_core as uc  # noqa: E402


def make_folder(root: Path) -> Path:
    g = root / "Example [PPSA00001]"
    (g / "sce_sys").mkdir(parents=True)
    (g / "sce_sys" / "param.json").write_text(json.dumps({
        "titleId": "PPSA00001", "contentVersion": "01.000.000",
        "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": "Example"}}}))
    (g / "eboot.bin").write_bytes(b"\x7fELF" + b"\0" * 60)
    return g


class FromChain(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_folder_source_carries_stats_and_chain_fields(self):
        g = make_folder(self.root)
        it = uc.GameItem.from_chain(g, to="ffpfsc", sign=True, backport_target="7.61",
                                    backport_libs_root=self.root / "libs", output_path=self.root / "out")
        self.assertEqual(it.operation, "chain")
        self.assertEqual(it.chain_to, "ffpfsc")
        self.assertTrue(it.chain_sign)
        self.assertEqual(it.backport_target, "7.61")
        self.assertEqual(it.title_id, "PPSA00001")
        self.assertEqual(it.source_kind, "inplace")
        self.assertEqual(uc.chain_source_kind(it), "folder")
        self.assertEqual(uc.chain_changes(it), ["backport 7.61", "sign"])
        self.assertEqual(uc.chain_summary(it), "Backport to 7.61, sign, then build .ffpfsc")

    def test_container_source(self):
        img = self.root / "Example [PPSA00002].ffpfsc"
        img.write_bytes(b"\0" * 4096)
        it = uc.GameItem.from_chain(img, to="ffpfsc", backport_target="7.61")
        self.assertEqual(uc.chain_source_kind(it), "ffpfsc")
        self.assertEqual(it.size, 4096)
        self.assertTrue(uc.chain_needs_unpack(it), "a change on a container means unpack into scratch")
        self.assertEqual(uc.chain_summary(it), "Backport to 7.61, then build .ffpfsc")
        self.assertEqual(uc._peak_factor_for(it), uc.PATCH_PEAK_FACTOR)

    def test_pkg_title_id_from_content_id_name(self):
        pkg = self.root / "UP0000-PPSA00003_00-EXAMPLE000000000-A0100-V0100.pkg"
        pkg.write_bytes(b"\0" * 64)
        it = uc.GameItem.from_chain(pkg, to="folder")
        self.assertEqual(it.title_id, "PPSA00003")
        self.assertEqual(uc.chain_summary(it), "Unpack to folder")
        self.assertTrue(uc.chain_needs_unpack(it))

    def test_pass_through_needs_no_unpack(self):
        ff = self.root / "a.ffpfs"; ff.write_bytes(b"\0" * 64)
        it = uc.GameItem.from_chain(ff, to="ffpfsc")
        self.assertFalse(uc.chain_needs_unpack(it), ".ffpfs → .ffpfsc is native to the pack path")
        self.assertEqual(uc.chain_summary(it), "Build .ffpfsc")

    def test_summary_edge_cases(self):
        g = make_folder(self.root)
        self.assertEqual(uc.chain_summary(uc.GameItem.from_chain(g, to="folder")), "Nothing to do")
        self.assertEqual(uc.chain_summary(uc.GameItem.from_chain(g, to="folder", sign=True)), "Sign in place")
        self.assertEqual(uc.chain_summary(uc.GameItem.from_chain(g, to="pkg")), "Build .pkg")
        img = self.root / "b.ffpfsc"; img.write_bytes(b"\0" * 64)
        self.assertEqual(uc.chain_summary(uc.GameItem.from_chain(img, to="ffpfsc")), "Copy or move (same format)")
        self.assertEqual(uc.chain_summary(uc.GameItem.from_chain(img, to="folder", sign=True)),
                         "Sign, then unpack to folder")
        self.assertEqual(uc.chain_summary(uc.GameItem.from_chain(img, to="pkg", patch_source=self.root / "p")),
                         "Integrate patch, then build .pkg")

    def test_archive_source_is_a_placeholder(self):
        z = self.root / "game.zip"
        import zipfile
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("x/eboot.bin", b"\x7fELF")
        it = uc.GameItem.from_chain(z, to="ffpfsc")
        self.assertEqual(it.operation, "chain")
        self.assertEqual(uc.chain_source_kind(it), "archive")
        self.assertEqual(it.status, "Pending Extract")
        self.assertIsNotNone(it.archive_path)


if __name__ == "__main__":
    unittest.main()
