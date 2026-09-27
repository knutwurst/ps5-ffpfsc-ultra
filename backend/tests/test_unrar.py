"""Unit tests for the bundled UnRAR module (backend/unrar: rarfile.py over the _unrar
C++ extension).

  /tmp/ps5venv/bin/python backend/tests/test_unrar.py -v

The extension must be built first (cd backend/unrar && python3 setup.py build_ext
--inplace); otherwise every test is skipped with that reason. The archive tests need a
fixture: backend/test_data/sample.rar (not in the repository) or, when the `rar` command
line tool is on PATH, a small archive built on the fly in a temp dir. Without either
they are skipped with a visible reason.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from unrar import rarfile  # noqa: E402
except ImportError as exc:                     # extension not built: skip loudly, do not crash
    rarfile = None
    IMPORT_ERROR: str | None = str(exc)
else:
    IMPORT_ERROR = None

SAMPLE_RAR = Path(__file__).resolve().parents[1] / "test_data" / "sample.rar"
RAR_CLI = shutil.which("rar")
NOT_BUILT = (f"unrar extension not importable ({IMPORT_ERROR}); build it with: "
             "cd backend/unrar && python3 setup.py build_ext --inplace")


def _skip(reason: str) -> None:
    print(f"SKIP test_unrar: {reason}", file=sys.stderr)
    raise unittest.SkipTest(reason)


@unittest.skipIf(rarfile is None, NOT_BUILT)
class RarfileApiTests(unittest.TestCase):
    """No fixture needed."""

    def test_api_surface(self):
        for name in ("RarFile", "RarInfo", "BadRarFile", "RarWrongPassword",
                     "NeedFirstVolume", "RarExtractionCancelled"):
            self.assertTrue(hasattr(rarfile, name), name)

    def test_bad_archive_raises_badrarfile(self):
        with tempfile.NamedTemporaryFile(suffix=".rar", delete=False) as f:
            f.write(b"not a rar file")
            bad_path = f.name
        try:
            with self.assertRaises(rarfile.BadRarFile):
                rarfile.RarFile(bad_path).infolist()
        finally:
            os.unlink(bad_path)


@unittest.skipIf(rarfile is None, NOT_BUILT)
class RarfileArchiveTests(unittest.TestCase):
    """Listing and extraction against a real archive: the shipped fixture, or one
    generated with the `rar` tool when that is available."""

    fixture: Path
    expected: dict[str, int]          # member -> size; known only for a generated fixture
    _tmp: tempfile.TemporaryDirectory | None = None

    @classmethod
    def setUpClass(cls):
        cls.expected = {}
        if SAMPLE_RAR.is_file():
            cls.fixture = SAMPLE_RAR
            return
        if RAR_CLI is None:
            _skip(f"no fixture: {SAMPLE_RAR} is absent and no `rar` command line tool is on "
                  "PATH to generate one")
        cls._tmp = tempfile.TemporaryDirectory(prefix="unrar_fixture_")
        root = Path(cls._tmp.name)
        src = root / "src"
        (src / "sub").mkdir(parents=True)
        files = {"hello.txt": b"hello, rar\n" * 100,
                 "sub/data.bin": bytes(range(256)) * 64,
                 "empty.txt": b""}
        for rel, blob in files.items():
            (src / rel).write_bytes(blob)
        cls.fixture = root / "sample.rar"
        proc = subprocess.run([RAR_CLI, "a", "-r", "-idq", str(cls.fixture), "hello.txt", "sub", "empty.txt"],
                              cwd=src, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not cls.fixture.is_file():
            cls._tmp.cleanup()
            cls._tmp = None
            _skip(f"`rar a` failed (rc={proc.returncode}): {(proc.stdout + proc.stderr)[-300:]}")
        cls.expected = {rel: len(blob) for rel, blob in files.items()}
        print(f"test_unrar: generated fixture {cls.fixture} with {RAR_CLI}", file=sys.stderr)

    @classmethod
    def tearDownClass(cls):
        if cls._tmp is not None:
            cls._tmp.cleanup()

    def test_list(self):
        rf = rarfile.RarFile(self.fixture)
        names = rf.namelist()
        infos = rf.infolist()
        self.assertGreater(len(names), 0)
        self.assertEqual(len(infos), len(names))
        for info in infos:
            self.assertIsInstance(info.filename, str)
            self.assertGreaterEqual(info.file_size, 0)
            self.assertIsInstance(info.isdir(), bool)
        if self.expected:
            got = {os.path.normpath(i.filename): i.file_size for i in infos if not i.isdir()}
            self.assertEqual(got, {os.path.normpath(k): v for k, v in self.expected.items()})

    def test_extractall(self):
        rf = rarfile.RarFile(self.fixture)
        with tempfile.TemporaryDirectory() as tmpdir:
            rf.extractall(tmpdir)
            for info in rf.infolist():
                if info.isdir():
                    continue
                extracted = Path(tmpdir) / info.filename
                self.assertTrue(extracted.is_file(), info.filename)
                self.assertEqual(extracted.stat().st_size, info.file_size, info.filename)


if __name__ == "__main__":
    unittest.main()
