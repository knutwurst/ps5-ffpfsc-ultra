"""Backport engine — SDK version lowering, fakelib bundle assembly, NID
compatibility analysis.

Applied to the STAGING copy, never the source. Runs before fake_sign.

SDK constants and layout follow idlesauce's ps5_elf_sdk_downgrade.py (referenced by
BestPig/BackPork, Nazky/Auto-Backpork, PS5-BACKPORK-KITCHEN). Layout for both
PT_SCE_PROCPARAM (executables) and PT_SCE_MODULE_PARAM (prx/sprx) is:
    +0x00  Elf64_Xword  size
    +0x08  Elf32_Word   magic          (ORBI=0x4942524F proc, 0x3C13F4BF module)
    +0x0C  Elf32_Word   version
    +0x10  Elf32_Word   sdk_version    (PS4 form, still written on PS5 modules)
    +0x14  Elf32_Word   sdk_version    (PS5 form, 4-byte LE)
Only these two words are changed; every other byte in the ELF stays the same.

Public backport-target inventory as of 2026-09-28 (see scratchpad/backport-
landscape-2026-09-28.md for citations):
  7.61   sweet spot — SDK constants published, fakelib patches published
  6.02   experimental — SDK constants published, small library set (marked
         'not recommended' by rajeshca911/PS5-BACKPORK-KITCHEN fakelibs.json)
  10.xx  no bundle — SDK constants published, the fakelib content IS the
         10.01 originals; the console already carries them
Nothing above id 10 is used, because idlesauce's table stops there and any
value would be invented.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

# ── SDK constants (verbatim from idlesauce's gist) ────────────────────────
# Keyed by human label. Value: (ps5_sdk_word, ps4_sdk_word). Both are 4-byte
# little-endian integers written into the SCE param struct.
SDK_TARGETS: dict[str, tuple[int, int]] = {
    "7.61":  (0x07000038, 0x10590001),
    "6.02":  (0x06000038, 0x10090001),
    "10.xx": (0x10000040, 0x12090001),
}

# ── ELF constants ─────────────────────────────────────────────────────────
_ELF_MAGIC = b"\x7fELF"
_ELFCLASS64 = 2
_ELFDATA2LSB = 1

# Elf64_Ehdr offsets used here.
_EH_PHOFF = 32       # Elf64_Off  e_phoff
_EH_PHENTSIZE = 54   # Elf64_Half e_phentsize
_EH_PHNUM = 56       # Elf64_Half e_phnum

# Elf64_Phdr: p_type (u32), p_flags (u32), p_offset (u64), p_vaddr (u64),
# p_paddr (u64), p_filesz (u64), p_memsz (u64), p_align (u64) = 56 bytes.
_PHDR_FMT = "<2I6Q"
_PHDR_SIZE = struct.calcsize(_PHDR_FMT)

PT_SCE_PROCPARAM = 0x61000001    # /app0/eboot.bin
PT_SCE_MODULE_PARAM = 0x61000002  # every .prx / .sprx

# Magic values at +0x08 inside the param struct. The layout is otherwise
# identical between the two, and the writer treats them the same way.
_MAGIC_PROC = 0x4942524F           # b"ORBI"
_MAGIC_MODULE = 0x3C13F4BF
_ACCEPTED_MAGICS = (_MAGIC_PROC, _MAGIC_MODULE)

# Byte offsets of the two 4-byte SDK fields inside the param struct.
_SDK_PS4_OFFSET = 0x10
_SDK_PS5_OFFSET = 0x14


# ── result types ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SdkChange:
    """One SDK field the downgrade would write, and what is there today."""
    kind: str        # "proc" or "module"
    field: str       # "ps5" or "ps4"
    before: int
    after: int

    def unchanged(self) -> bool:
        return self.before == self.after


# ── ELF walking ───────────────────────────────────────────────────────────
def _is_ps5_elf64(data: bytes) -> bool:
    """True if the blob starts with a 64-bit little-endian ELF header. Everything
    the tool cares about is 64-bit LE; a stray 32-bit ELF is left alone."""
    return (
        len(data) >= 64
        and data[:4] == _ELF_MAGIC
        and data[4] == _ELFCLASS64
        and data[5] == _ELFDATA2LSB
    )


def _iter_phdrs(data: bytes):
    """Yield (ptype, p_offset, p_filesz) for each program header. Silently
    stops at the end of the file — a truncated ELF returns what it can."""
    if not _is_ps5_elf64(data):
        return
    e_phoff = struct.unpack_from("<Q", data, _EH_PHOFF)[0]
    e_phentsize = struct.unpack_from("<H", data, _EH_PHENTSIZE)[0]
    e_phnum = struct.unpack_from("<H", data, _EH_PHNUM)[0]
    if e_phentsize < _PHDR_SIZE:
        return
    for i in range(e_phnum):
        base = e_phoff + i * e_phentsize
        if base + _PHDR_SIZE > len(data):
            return
        p_type, _flags, p_offset, _vaddr, _paddr, p_filesz, _memsz, _align = \
            struct.unpack_from(_PHDR_FMT, data, base)
        yield p_type, p_offset, p_filesz


def _find_param_segment(data: bytes) -> tuple[str, int] | None:
    """Return ("proc"|"module", file offset of the param struct) or None.
    Prefers the first PT_SCE_PROCPARAM (eboot); a module has PT_SCE_MODULE_PARAM
    instead. A binary with both is unusual — the caller sees the first hit."""
    for p_type, p_offset, p_filesz in _iter_phdrs(data):
        if p_type not in (PT_SCE_PROCPARAM, PT_SCE_MODULE_PARAM):
            continue
        if p_filesz < _SDK_PS5_OFFSET + 4:
            continue
        magic = struct.unpack_from("<I", data, p_offset + 0x08)[0]
        if magic not in _ACCEPTED_MAGICS:
            continue
        return ("proc" if p_type == PT_SCE_PROCPARAM else "module", p_offset)
    return None


# ── the writes ────────────────────────────────────────────────────────────
def lower_sdk_version(elf: bytes, target: str) -> tuple[bytes, list[SdkChange]]:
    """Return a copy of *elf* with its SDK words lowered to *target*, plus one
    SdkChange per field considered (kept even when equal, so the caller can log
    a no-op).

    The rules match idlesauce's script:
      • only touches PT_SCE_PROCPARAM / PT_SCE_MODULE_PARAM, and only when the
        struct starts with the expected magic;
      • writes BOTH the PS4 and PS5 fields, because a PS4-derived module (some
        prx) still carries a PS4 SDK word the loader reads;
      • only lowers — a value equal to or below the target is left alone.
    A file without a param segment (not every prx has one) is returned as-is
    with an empty change list; the caller then knows the module is neutral.
    ValueError for an unknown target."""
    if target not in SDK_TARGETS:
        raise ValueError(f"unknown backport target {target!r}; expected one of {sorted(SDK_TARGETS)}")
    ps5_target, ps4_target = SDK_TARGETS[target]
    hit = _find_param_segment(elf)
    if hit is None:
        return elf, []
    kind, base = hit
    changes: list[SdkChange] = []
    buf = bytearray(elf)
    for field, offset, new_val in (("ps4", _SDK_PS4_OFFSET, ps4_target),
                                   ("ps5", _SDK_PS5_OFFSET, ps5_target)):
        cur = struct.unpack_from("<I", buf, base + offset)[0]
        after = min(cur, new_val)
        changes.append(SdkChange(kind=kind, field=field, before=cur, after=after))
        if after != cur:
            struct.pack_into("<I", buf, base + offset, after)
    return bytes(buf), changes


# ── file walking (for a game folder / staged /app0 root) ──────────────────
_ELF_SUFFIXES = (".prx", ".sprx", ".elf", ".self")


def iter_source_elfs(root: Path):
    """Yield every path under *root* that could carry SDK metadata: eboot.bin,
    plus every .prx / .sprx / .elf / .self. Symlinks are skipped (safety, and
    the staging step handles their contents already)."""
    root = Path(root)
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        name = p.name.lower()
        if name == "eboot.bin" or name.endswith(_ELF_SUFFIXES):
            yield p


@dataclass
class LowerReport:
    """What the downgrade pass did to a folder. `written` lists the files that
    actually changed (source path, effective changes). `skipped` lists files
    that carry no param segment or are not raw ELFs — the caller reports those
    but never treats them as an error."""
    written: list[tuple[Path, list[SdkChange]]]
    skipped_no_param: list[Path]
    skipped_not_elf: list[Path]

    def summary(self) -> str:
        return (f"lowered SDK in {len(self.written)} file(s); "
                f"{len(self.skipped_no_param)} without a param segment, "
                f"{len(self.skipped_not_elf)} not raw ELFs")


def lower_sdk_in_folder(root: Path, target: str) -> LowerReport:
    """Walk *root* and apply lower_sdk_version to every eboot/prx/sprx it holds,
    in place. The caller is expected to point this at the staging mirror.

    A file that is not a raw ELF (already fake-signed, or truly not an ELF) is
    left alone. A file with an ELF header but no param segment is left alone
    too — some helper prx have neither PT_SCE_PROCPARAM nor _MODULE_PARAM.

    Writes go through a temp file + os.replace so a crash never leaves a
    truncated ELF next to the source."""
    import os
    report = LowerReport(written=[], skipped_no_param=[], skipped_not_elf=[])
    for path in iter_source_elfs(root):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not _is_ps5_elf64(data):
            report.skipped_not_elf.append(path)
            continue
        new_data, changes = lower_sdk_version(data, target)
        effective = [c for c in changes if not c.unchanged()]
        if not changes:
            report.skipped_no_param.append(path)
            continue
        if not effective:
            report.written.append((path, changes))          # already at/below target
            continue
        tmp = path.with_suffix(path.suffix + ".backport-tmp")
        try:
            tmp.write_bytes(new_data)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try: tmp.unlink()
                except OSError: pass
        report.written.append((path, changes))
    return report
