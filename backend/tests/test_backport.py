"""SDK-lowering unit tests. The ELF fixtures are hand-crafted so every byte we
depend on is under test — the SDK constants at +0x10/+0x14, the two accepted
magics, and that everything outside the param struct comes through untouched.

    python3 -m unittest backend.tests.test_backport
"""
from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from backend import backport as bp


def _elf_with_param(param_type: int, magic: int, sdk_ps4: int, sdk_ps5: int) -> bytes:
    """A minimal 64-bit LE ELF64 with one PHDR pointing at a 40-byte param
    struct laid out like Sony's — enough for the downgrader to recognise. The
    ELF header itself is otherwise zero, so a change outside the param struct
    is easy to spot."""
    ehdr = bytearray(64)
    ehdr[0:4] = b"\x7fELF"
    ehdr[4] = 2                       # ELFCLASS64
    ehdr[5] = 1                       # ELFDATA2LSB
    e_phoff = 64
    e_phentsize = 56
    e_phnum = 1
    struct.pack_into("<Q", ehdr, 32, e_phoff)
    struct.pack_into("<H", ehdr, 54, e_phentsize)
    struct.pack_into("<H", ehdr, 56, e_phnum)

    param = bytearray(40)
    struct.pack_into("<Q", param, 0x00, 40)              # size
    struct.pack_into("<I", param, 0x08, magic)
    struct.pack_into("<I", param, 0x0C, 1)               # version
    struct.pack_into("<I", param, 0x10, sdk_ps4)
    struct.pack_into("<I", param, 0x14, sdk_ps5)
    struct.pack_into("<I", param, 0x18, 0xdeadbeef)      # anything past the SDK words stays
    struct.pack_into("<I", param, 0x1C, 0xbadf00d0)

    p_offset = 64 + e_phentsize
    p_filesz = len(param)
    phdr = struct.pack("<II6Q", param_type, 0, p_offset, 0, 0, p_filesz, 0, 0)
    return bytes(ehdr) + phdr + bytes(param)


class LowerSdkVersion(unittest.TestCase):
    def test_lowers_both_fields_when_source_is_newer(self):
        elf = _elf_with_param(bp.PT_SCE_PROCPARAM, 0x4942524F, 0x11090001, 0x08000041)
        out, changes = bp.lower_sdk_version(elf, "7.61")
        # PS4 field went 11.09 → 7.59, PS5 went 8.00 → 7.00 (both from idlesauce's table)
        self.assertEqual(struct.unpack_from("<I", out, 64 + 56 + 0x10)[0], 0x10590001)
        self.assertEqual(struct.unpack_from("<I", out, 64 + 56 + 0x14)[0], 0x07000038)
        self.assertEqual([(c.field, c.before, c.after) for c in changes],
                         [("ps4", 0x11090001, 0x10590001),
                          ("ps5", 0x08000041, 0x07000038)])
        # Bytes past the SDK words are unchanged.
        self.assertEqual(struct.unpack_from("<I", out, 64 + 56 + 0x18)[0], 0xdeadbeef)

    def test_leaves_lower_source_alone(self):
        """A 6.00 module targeted at 7.61 stays 6.00 — never raises the SDK."""
        elf = _elf_with_param(bp.PT_SCE_MODULE_PARAM, 0x3C13F4BF, 0x10090001, 0x06000038)
        out, changes = bp.lower_sdk_version(elf, "7.61")
        self.assertEqual(out, elf)
        self.assertTrue(all(c.unchanged() for c in changes))

    def test_returns_no_change_for_elf_without_param(self):
        """An ELF with a PT_LOAD but no SCE param segment is a valid ELF that
        happens to carry no SDK metadata (some helper prx). The function must
        return the input untouched and no change list."""
        ehdr = bytearray(64)
        ehdr[0:4] = b"\x7fELF"
        ehdr[4] = 2; ehdr[5] = 1
        struct.pack_into("<Q", ehdr, 32, 64)
        struct.pack_into("<H", ehdr, 54, 56)
        struct.pack_into("<H", ehdr, 56, 1)
        phdr = struct.pack("<II6Q", 1, 0, 120, 0, 0, 16, 0, 0)   # PT_LOAD, 16 bytes
        payload = bytes(16)
        elf = bytes(ehdr) + phdr + payload
        out, changes = bp.lower_sdk_version(elf, "7.61")
        self.assertEqual(out, elf)
        self.assertEqual(changes, [])

    def test_ignores_param_with_wrong_magic(self):
        """A dump where the segment type says PROC_PARAM but the magic is not
        ORBI/0x3C13F4BF is treated as no param segment — silently untouched."""
        elf = _elf_with_param(bp.PT_SCE_PROCPARAM, 0xDEADBEEF, 0x11090001, 0x08000041)
        out, changes = bp.lower_sdk_version(elf, "7.61")
        self.assertEqual(out, elf)
        self.assertEqual(changes, [])

    def test_all_three_targets_write_the_expected_words(self):
        for label, (ps5, ps4) in bp.SDK_TARGETS.items():
            with self.subTest(target=label):
                elf = _elf_with_param(bp.PT_SCE_PROCPARAM, 0x4942524F, 0xFFFFFFFF, 0xFFFFFFFF)
                out, _ = bp.lower_sdk_version(elf, label)
                self.assertEqual(struct.unpack_from("<I", out, 64 + 56 + 0x10)[0], ps4)
                self.assertEqual(struct.unpack_from("<I", out, 64 + 56 + 0x14)[0], ps5)

    def test_rejects_unknown_target(self):
        with self.assertRaises(ValueError):
            bp.lower_sdk_version(b"\x7fELF" + b"\x00" * 60, "8.60")

    def test_ignores_a_non_elf(self):
        """A staging step that mislabelled a data blob as .prx must not be
        rewritten. The header check is the guard."""
        out, changes = bp.lower_sdk_version(b"not an ELF at all" + b"\x00" * 64, "7.61")
        self.assertEqual(out, b"not an ELF at all" + b"\x00" * 64)
        self.assertEqual(changes, [])


class WalkAndWrite(unittest.TestCase):
    def test_lower_sdk_in_folder_rewrites_only_the_changed_files(self):
        """A tree with one high-SDK eboot, one prx already low, and one non-ELF.
        Only the eboot is rewritten; the non-ELF is left as-is; the low prx is
        reported as considered-but-unchanged."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            eboot = _elf_with_param(bp.PT_SCE_PROCPARAM, 0x4942524F, 0x11090001, 0x08000041)
            low = _elf_with_param(bp.PT_SCE_MODULE_PARAM, 0x3C13F4BF, 0x10090001, 0x06000038)
            (root / "eboot.bin").write_bytes(eboot)
            (root / "libSceExample.prx").write_bytes(low)
            # A .txt is never iterated (filename filter); a .prx that is NOT a
            # raw ELF (say, already fake-signed and thus a SELF) reaches the
            # walker but is refused by the ELF header check.
            (root / "libSceAlreadySelf.prx").write_bytes(b"\x54\x14\xF5\xEE" + b"\x00" * 60)
            (root / "readme.txt").write_bytes(b"not an ELF")

            report = bp.lower_sdk_in_folder(root, "7.61")

            self.assertEqual({p.name for p, _ in report.written}, {"eboot.bin", "libSceExample.prx"})
            self.assertEqual({p.name for p in report.skipped_not_elf}, {"libSceAlreadySelf.prx"})

            # The eboot on disk now carries the lowered words.
            after = (root / "eboot.bin").read_bytes()
            self.assertEqual(struct.unpack_from("<I", after, 64 + 56 + 0x14)[0], 0x07000038)
            # The low prx bytes are unchanged.
            self.assertEqual((root / "libSceExample.prx").read_bytes(), low)


if __name__ == "__main__":
    unittest.main()
