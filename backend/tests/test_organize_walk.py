"""Walker used by Organize Folder…: rglob across a mixed tree, dedup, sort.

The GUI helper wraps this into copy jobs. We test the walk itself here — pure
Python, no GUI dependency — so the batching stays regression-checked in CI.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def organize_scan(root: Path) -> list[Path]:
    """Mirror the app's Organize walk: recursively enumerate .ffpfsc/.ffpfs/.pkg
    under *root*, dedup by resolved path, sort lower-case for a stable order."""
    hits: list[Path] = []
    for suf in (".ffpfsc", ".ffpfs", ".pkg"):
        hits.extend(root.rglob(f"*{suf}"))
    seen: set[str] = set()
    picked: list[Path] = []
    for p in sorted(hits, key=lambda q: str(q).lower()):
        try:
            key = str(p.resolve()).lower()
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        picked.append(p)
    return picked


class OrganizeScanTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="organize_walk_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _touch(self, rel: str, data: bytes = b"x") -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_finds_all_three_suffixes_recursively(self):
        self._touch("A/game1.ffpfsc")
        self._touch("B/inner/game2.ffpfs")
        self._touch("C/game3.pkg")
        self._touch("noise/readme.txt")
        self._touch("noise/game.zip")

        hits = organize_scan(self.root)
        names = sorted(h.name for h in hits)
        self.assertEqual(names, ["game1.ffpfsc", "game2.ffpfs", "game3.pkg"])

    def test_ignores_unrelated_extensions(self):
        self._touch("game.txt")
        self._touch("game.rar")
        self._touch("cover.jpg")
        self.assertEqual(organize_scan(self.root), [])

    def test_deep_nesting_still_reached(self):
        deep = self._touch("a/b/c/d/e/f/g/h/i/j/deep.ffpfsc")
        hits = organize_scan(self.root)
        self.assertEqual(hits, [deep])

    def test_dedup_via_resolve(self):
        # Two paths that resolve to the same file (a symlink) → one hit only.
        real = self._touch("real/game.ffpfsc")
        link_dir = self.root / "link"
        link_dir.mkdir()
        try:
            (link_dir / "same.ffpfsc").symlink_to(real)
        except OSError:
            self.skipTest("symlinks not supported here")
        hits = organize_scan(self.root)
        # Both spellings appear in rglob, but the resolved-path dedup keeps one.
        self.assertEqual(len(hits), 1)

    def test_sorted_case_insensitive(self):
        # Emit in an order that would sort DIFFERENTLY under case-sensitive vs
        # case-insensitive rules — the app uses case-insensitive.
        self._touch("Z/zebra.ffpfsc")
        self._touch("a/alpha.ffpfsc")
        self._touch("M/middle.ffpfsc")
        hits = [p.name for p in organize_scan(self.root)]
        self.assertEqual(hits, ["alpha.ffpfsc", "middle.ffpfsc", "zebra.ffpfsc"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
