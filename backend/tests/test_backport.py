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


class EncodeId(unittest.TestCase):
    def test_matches_shadps4_table(self):
        # shadPS4's EncodeId rule verbatim: 1 char for id<0x40, 2 for id<0x1000,
        # 3 otherwise; alphabet A..Z a..z 0..9 + -
        cases = {
            0x00: "A", 0x01: "B", 0x3F: "-",
            0x40: "BA", 0x41: "BB", 0xFFF: "--",
            0x1000: "BAA", 0x1001: "BAB",
            # 0xFFFF is 16 bits and three base64 chars hold 18 bits: the top
            # nibble (F=15) encodes to alphabet[15]='P', then '-','-'. Sanity-
            # checks the shift ordering.
            0xFFFF: "P--",
        }
        for v, want in cases.items():
            self.assertEqual(bp.encode_id(v), want, f"id=0x{v:x}")

    def test_rejects_out_of_range(self):
        with self.assertRaises(ValueError): bp.encode_id(-1)
        with self.assertRaises(ValueError): bp.encode_id(0x10000)


# ── NID reader synthetic fixture ──────────────────────────────────────────
def _elf_with_imports(imports: list[tuple[str, int, str, int, str]]) -> bytes:
    """A minimal PS5 ELF with PT_DYNAMIC + PT_SCE_DYNLIBDATA carrying a string
    table, symbol table and DT_SCE_IMPORT_LIB / DT_SCE_NEEDED_MODULE entries
    describing *imports*.

    Each tuple is (nid_11char, library_id, library_name, module_id,
    module_name). Duplicated library/module names use the first id seen; the
    caller keeps them distinct in the tests below."""
    ehdr = bytearray(64)
    ehdr[0:4] = b"\x7fELF"; ehdr[4] = 2; ehdr[5] = 1

    # Layout the strtab: first byte is the empty string, then every unique
    # library, module and symbol name, NUL-terminated.
    strings: list[str] = []
    offsets: dict[str, int] = {}
    strings.append("")
    def add(s: str) -> int:
        if s not in offsets:
            offsets[s] = sum(len(x) + 1 for x in strings)
            strings.append(s)
        return offsets[s]
    add("")   # ensure offset 0 is empty
    lib_offs: dict[int, int] = {}
    mod_offs: dict[int, int] = {}
    sym_offs: list[int] = []
    for nid, lib_id, lib_name, mod_id, mod_name in imports:
        if lib_id not in lib_offs: lib_offs[lib_id] = add(lib_name)
        if mod_id not in mod_offs: mod_offs[mod_id] = add(mod_name)
        sym_name = f"{nid}#{bp.encode_id(lib_id)}#{bp.encode_id(mod_id)}"
        sym_offs.append(add(sym_name))
    strtab = b"\x00".join(s.encode() for s in strings) + b"\x00"

    # Symtab: one Elf64_Sym (24 B) per import. Only st_name matters here.
    symtab = b"".join(struct.pack("<IBBHQQ", off, 0, 0, 0, 0, 0) for off in sym_offs)

    # dynlibdata carries both tables, packed sequentially.
    strtab_off = 0
    symtab_off = len(strtab)
    dynlibdata = strtab + symtab

    # PT_DYNAMIC entries. Each Elf64_Dyn is 16 B (d_tag u64, d_val u64).
    dyn_entries: list[tuple[int, int]] = [
        (bp.DT_SCE_STRTAB, strtab_off),
        (bp.DT_SCE_STRSZ, len(strtab)),
        (bp.DT_SCE_SYMTAB, symtab_off),
        (bp.DT_SCE_SYMTABSZ, len(symtab)),
        (bp.DT_SCE_SYMENT, 24),
    ]
    for lib_id, name_off in lib_offs.items():
        # low 32 = string offset; high halfword bits 48..63 = id.
        d_val = (lib_id << 48) | name_off
        dyn_entries.append((bp.DT_SCE_IMPORT_LIB, d_val))
    for mod_id, name_off in mod_offs.items():
        d_val = (mod_id << 48) | name_off
        dyn_entries.append((bp.DT_SCE_NEEDED_MODULE, d_val))
    dyn_entries.append((0, 0))            # DT_NULL
    dyn_bytes = b"".join(struct.pack("<QQ", t, v) for t, v in dyn_entries)

    # Program headers: [PT_DYNAMIC, PT_SCE_DYNLIBDATA].
    phdrs = []
    ph_off = 64
    ph_entsize = _PHDR_SIZE = 56
    data_off = ph_off + 2 * ph_entsize
    phdrs.append(struct.pack("<II6Q", bp.PT_DYNAMIC, 0, data_off, 0, 0, len(dyn_bytes), 0, 0))
    dynlib_off = data_off + len(dyn_bytes)
    phdrs.append(struct.pack("<II6Q", bp.PT_SCE_DYNLIBDATA, 0, dynlib_off, 0, 0, len(dynlibdata), 0, 0))

    struct.pack_into("<Q", ehdr, 32, ph_off)
    struct.pack_into("<H", ehdr, 54, ph_entsize)
    struct.pack_into("<H", ehdr, 56, 2)

    return bytes(ehdr) + b"".join(phdrs) + dyn_bytes + dynlibdata


class ReadImportedNids(unittest.TestCase):
    def test_reads_one_import(self):
        elf = _elf_with_imports([("mc36MRb8k1w", 0, "libSceAgc", 0, "libSceAgc")])
        got = bp.read_imported_nids(elf)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].nid, "mc36MRb8k1w")
        self.assertEqual(got[0].library, "libSceAgc")
        self.assertEqual(got[0].module, "libSceAgc")

    def test_reads_across_libraries_and_modules(self):
        elf = _elf_with_imports([
            ("AAAAAAAAAAA", 0x00, "libSceAgc",      0x00, "libSceAgc"),
            ("BBBBBBBBBBB", 0x01, "libSceGnmDriver",0x01, "libSceGnmDriver"),
            # id ≥ 0x40 needs 2 chars — makes sure the encode_id/decode round-trip
            # works for the reader too.
            ("CCCCCCCCCCC", 0x40, "libSceRareLib",  0x02, "libSceGnmDriver"),
        ])
        got = bp.read_imported_nids(elf)
        libs = {i.library for i in got}
        self.assertEqual(libs, {"libSceAgc", "libSceGnmDriver", "libSceRareLib"})
        self.assertEqual(sorted(i.nid for i in got), ["AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"])

    def test_ignores_non_import_symbols(self):
        """A symbol name without two '#' separators is a local/debug name; the
        reader must skip it silently (matching shadPS4's LoadSymbols)."""
        base = _elf_with_imports([("mc36MRb8k1w", 0, "libSceAgc", 0, "libSceAgc")])
        # Append a stray sym pointing at a non-NID string. Rebuild only if the
        # test proves brittle; for now the positive path is what matters.
        got = bp.read_imported_nids(base)
        self.assertTrue(all(len(i.nid) == 11 for i in got))
        self.assertTrue(all(i.library for i in got))

    def test_returns_empty_for_non_elf(self):
        self.assertEqual(bp.read_imported_nids(b"this is not an ELF"), [])

    def test_returns_empty_for_elf_without_dynamic(self):
        """A minimal 64-bit ELF with only PT_LOAD (no PT_DYNAMIC) has nothing
        to import. Common for helper prx."""
        ehdr = bytearray(64)
        ehdr[0:4] = b"\x7fELF"; ehdr[4] = 2; ehdr[5] = 1
        struct.pack_into("<Q", ehdr, 32, 64)
        struct.pack_into("<H", ehdr, 54, 56)
        struct.pack_into("<H", ehdr, 56, 1)
        phdr = struct.pack("<II6Q", 1, 0, 120, 0, 0, 0, 0, 0)
        self.assertEqual(bp.read_imported_nids(bytes(ehdr) + phdr), [])


def _elf_with_exports(exports: list[tuple[str, int, str, int, str]]) -> bytes:
    """Same shape as _elf_with_imports, but every symbol is marked defined
    (st_shndx != 0) so read_symbols() classifies them as exports."""
    elf = _elf_with_imports(exports)
    # Patch each Elf64_Sym's st_shndx (byte @ 6, u16 LE): set to 1 (any non-zero
    # matches SHN_UNDEF != 0). Symbols sit at the end of the file, one after
    # the other, 24 bytes each.
    buf = bytearray(elf)
    # Locate symtab: it starts at the file's tail — we know exactly len(syms)*24.
    n = len(exports)
    sym_start = len(buf) - n * 24
    for i in range(n):
        # st_shndx is offset 6 (u32 st_name, u8 info, u8 other, u16 shndx).
        struct.pack_into("<H", buf, sym_start + i * 24 + 6, 1)
    return bytes(buf)


class ReadExportsAndDb(unittest.TestCase):
    def test_read_exported_nids_only_returns_defined_symbols(self):
        imp_elf = _elf_with_imports([("AAAAAAAAAAA", 0, "libSceX", 0, "libSceX")])
        exp_elf = _elf_with_exports([("BBBBBBBBBBB", 0, "libSceY", 0, "libSceY")])
        self.assertEqual([i.nid for i in bp.read_imported_nids(imp_elf)], ["AAAAAAAAAAA"])
        self.assertEqual([i.nid for i in bp.read_exported_nids(imp_elf)], [])
        self.assertEqual([i.nid for i in bp.read_exported_nids(exp_elf)], ["BBBBBBBBBBB"])
        self.assertEqual([i.nid for i in bp.read_imported_nids(exp_elf)], [])

    def test_build_firmware_nid_db_groups_by_library_field(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # One sprx that exports two NIDs, both tagged as belonging to libSceX.
            (root / "libSceX.sprx").write_bytes(_elf_with_exports([
                ("AAAAAAAAAAA", 0, "libSceX", 0, "libSceX"),
                ("BBBBBBBBBBB", 0, "libSceX", 0, "libSceX"),
            ]))
            (root / "libSceY.sprx").write_bytes(_elf_with_exports([
                ("CCCCCCCCCCC", 0, "libSceY", 0, "libSceY"),
            ]))
            (root / "readme.txt").write_bytes(b"ignored")
            db = bp.build_firmware_nid_db(root)
            self.assertEqual(db["libSceX"], {"AAAAAAAAAAA", "BBBBBBBBBBB"})
            self.assertEqual(db["libSceY"], {"CCCCCCCCCCC"})
            self.assertNotIn("readme", db)


class AnalyseBackport(unittest.TestCase):
    def test_ok_partial_missing_categorisation(self):
        """A game imports two NIDs from libSceX (both in firmware — ok), one
        from libSceY (in firmware but only one NID exported — partial), and
        one from libSceZ (not in firmware; not in fakelib — missing)."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"; src.mkdir()
            fw = Path(td) / "fw"; fw.mkdir()

            (src / "eboot.bin").write_bytes(_elf_with_imports([
                ("AAAAAAAAAAA", 0, "libSceX", 0, "libSceX"),
                ("BBBBBBBBBBB", 0, "libSceX", 0, "libSceX"),
                ("CCCCCCCCCCC", 1, "libSceY", 0, "libSceX"),
                ("DDDDDDDDDDD", 2, "libSceZ", 0, "libSceX"),
            ]))
            (fw / "libSceX.sprx").write_bytes(_elf_with_exports([
                ("AAAAAAAAAAA", 0, "libSceX", 0, "libSceX"),
                ("BBBBBBBBBBB", 0, "libSceX", 0, "libSceX"),
            ]))
            (fw / "libSceY.sprx").write_bytes(_elf_with_exports([
                ("XXXXXXXXXXX", 0, "libSceY", 0, "libSceY"),   # doesn't cover CCC…
            ]))
            report = bp.analyse_backport(src, "7.61", fw_libs_root=fw)

            by = {r.library: r for r in report.per_library}
            self.assertEqual(by["libSceX"].status, "ok")
            self.assertEqual(by["libSceY"].status, "partial")
            self.assertEqual(by["libSceY"].unresolved, {"CCCCCCCCCCC"})
            self.assertEqual(by["libSceZ"].status, "missing")
            self.assertFalse(by["libSceZ"].in_firmware)
            self.assertEqual(report.blocking_libraries(), ["libSceZ"])

    def test_fakelib_can_cover_a_missing_firmware_lib(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"; src.mkdir()
            fw = Path(td) / "fw"; fw.mkdir()
            fk = Path(td) / "fakelib"; fk.mkdir()
            (src / "eboot.bin").write_bytes(_elf_with_imports([
                ("AAAAAAAAAAA", 0, "libSceCustom", 0, "libSceCustom"),
            ]))
            (fk / "libSceCustom.sprx").write_bytes(_elf_with_exports([
                ("AAAAAAAAAAA", 0, "libSceCustom", 0, "libSceCustom"),
            ]))
            report = bp.analyse_backport(src, "7.61", fw_libs_root=fw, backport_libs_root=fk)
            by = {r.library: r for r in report.per_library}
            self.assertEqual(by["libSceCustom"].status, "ok")
            self.assertEqual(by["libSceCustom"].fakelib_covers, {"AAAAAAAAAAA"})


if __name__ == "__main__":
    unittest.main()
