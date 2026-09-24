# ffpfsc-pkg-tool — source

`backend/native/ffpfsc-pkg-tool` is a self-contained, single-file .NET 9 executable
(macOS arm64) that the app calls for everything fPKG: `build`, `extract-inner`,
`extract-outer`, `list-inner`, `inspect`, `validate`, `version`. This folder holds the part
we wrote:

- `Program.cs` — the command-line wrapper around drakmor's LibProsperoPkg 1.2.0: argument
  parsing, the `[PHASE]`/progress lines the GUI translates, the 17-point `validate`
  checklist, and the pin `KrakenMaxDegreeOfParallelism = 1` (LibProsperoPkg 1.2.0's
  outer-PFS AES-XTS worker crashes above that).
- `InnerImage.cs` — random access into the inner PFS for `list-inner` and
  `extract-inner --members` (see below).

Every `--json` document is serialized through the source-generated `PkgToolJsonContext` at
the end of `Program.cs`. The trimmed publish switches reflection-based System.Text.Json off,
and the anonymous types the commands used before crashed on the shipped binary with
"Reflection-based serialization has been disabled" — the harness now parses all of them.
- `PkgTool.csproj` — the publish settings: self-contained single file, ReadyToRun,
  no trimming (Magick.NET's Prospero-facing path is reflection-driven; trimming pruned
  the types the DDS conversion needs). Magick.NET is required — LibProsperoPkg calls it
  to convert `sce_sys/icon0.png` into the `sce_sys/icon0.dds` CNT entry the console needs
  to launch the app; without the DDS, an fPKG installs but never starts (CE-100096-6 /
  CE-100022-5). Adds ~30 MB to the binary, unavoidable.

The fPKG logic itself is LibProsperoPkg, which is GPL-3.0-or-later; see
`../../LICENSE.LibProsperoPkg`, `../../NOTICE.LibProsperoPkg` and `../../NOTICE.fpkg`.
Because the published binary contains it, the binary is a GPL-3 work — which is why this
source is kept in the repo.

## Rebuilding

1. Install a .NET SDK (9 or newer; 10.0.400 was used).
2. Put the referenced libraries into `lib/` next to the project file. They are not in
   the repo; take them, unmodified, from the a53-fpkg 0.5 release archive
   (`fpkg-gui-0.5.zip`):
   - `lib/LibProsperoPkg.dll` (v1.2.0)
   - `lib/BCnEncoder.dll`
   - `lib/CommunityToolkit.HighPerformance.dll`
   - `lib/Magick.NET-Q8-AnyCPU.dll`
   - `lib/Magick.NET.Core.dll`
   - `lib/runtimes/osx-arm64/native/Magick.Native-Q8-arm64.dll.dylib`
   After extracting the native dylib, clear its quarantine flag and ad-hoc-sign it —
   otherwise Gatekeeper blocks the extracted copy at run time:

   ```bash
   xattr -c lib/runtimes/osx-arm64/native/Magick.Native-Q8-arm64.dll.dylib
   codesign --force --sign - lib/runtimes/osx-arm64/native/Magick.Native-Q8-arm64.dll.dylib
   ```
3. Publish:

   ```bash
   dotnet publish PkgTool.csproj -c Release -o out
   ```

4. Copy `out/ffpfsc-pkg-tool` to `backend/native/ffpfsc-pkg-tool` (keep it executable).
5. Prove it before shipping — the whole harness can run against any candidate binary:

   ```bash
   FFPFSC_PKG_TOOL=/path/to/out/ffpfsc-pkg-tool python backend/tests/test_fpkg_pipelines.py
   ```

`lib/`, `bin/`, `obj/` and `out/` are ignored by git.

## Browsing a package: `list-inner` and `extract-inner --members`

The PFS browser needs the tree of a package and a few members out of it without unpacking
100 GB first. LibProsperoPkg's `ExtractInnerFiles` cannot do that: it decrypts the outer
PFS to a temp file, NAPS-decodes the complete inner image to a second temp file, and only
then reads the directory. The two commands below chain the library's public random-access
pieces instead (`InnerImage.cs`):

    package -> ProsperoOuterPfsDecryptReader (AES-XTS, one 64 KiB outer block at a time)
            -> PfsReader (outer: pfs_image.dat + naps_pkg_layout.dat)
            -> ProsperoNapsImage.DecompressRange (only the touched Kraken/stored spans)
            -> NapsBlockReader (LRU cache of aligned blocks, 1 MiB x 32 or 4 MiB x 8)
            -> PfsReader (inner: the /app0 tree)

Two facts about the packages this tool builds shaped the code. The outer PFS is always
encrypted (mode 0x000D with a real seed), so the library's `DecodePlaintextInnerPfsRange`
refuses them — it demands the plaintext/no-auth marker; the decrypting reader covers both
layouts. And the inner image is data-first: the metadata (superblock, inodes, dirents) is
the *last* NAPS logical file, so the superblock is found by probing that boundary (one
block decode) rather than the library's forward 64 KiB scan, which would decode everything.

- `list-inner <pkg> [--passcode P]` prints one JSON object on stdout:
  `{"root": "<pkg file name>", "entries": [...], "file_count": N, "dir_count": N, "errors": []}`.
  Entries are sorted by path (relative to /app0, forward slashes); files carry the logical
  `size` and a `source` of `pfs` or `cnt`, directories are `{"path", "type": "dir"}` — every
  directory in the image, including empty ones. The `cnt` files are the sce_sys metadata
  that lives in the CNT table (param.json, icon0.png, pic0/pic1.png, playgo-*.dat,
  npbind.dat, changeinfo.xml), the same set `extract-inner` merges into `sce_sys/`.
  Measured on a 241.6 MB package (252 MB logical inner image, 18 files): 0.12 s warm /
  0.8 s cold, 75 MB peak RSS — of which 74 MB is the runtime floor, `version` alone shows
  it — and 2 range decodes reading 1.5 MiB of the image.
- `extract-inner <pkg> <out-dir> --members <file> [--passcode P] [--json]` extracts only the
  paths listed in `<file>` (one per line). A directory means its whole subtree, recreating
  directories including empty ones; a file name means that file; CNT files are selectable
  too. Progress goes to stdout as `[####] NN% extract (path)` (the GUI regex is
  `\[#{2,}\]\s*(\d{1,3})%`), then `[####] 100% extract` and an `OK — extracted N file(s) and
  M folder(s) to <dir>` line. Exit 0 on success, 1 when none of the members exist (each
  unknown member is a `[warn]` on stderr), 2 on usage errors. File data streams in 4 MiB
  chunks straight from `DecompressRange`, so a single large file never has to fit in RAM.

One cost the library imposes: `DecompressRange` rebuilds the NAPS plan on every call
(about 0.2 ms per 1000 cblocks, i.e. per ~256 MB of game). The 4 MiB chunking keeps that
below the decode time for the packages measured here; on a 100 GB game it adds roughly
70 ms per chunk.

## Why not NativeAOT

Measured on 240 MB of mixed data: a NativeAOT build is 16–18 MB instead of 25 MB but
10–23 % slower on the Kraken/AES path (the JIT's tiered compilation wins there). For
100 GB games that is a quarter of an hour, so the JIT build ships.

## Firmware compatibility

fPKG install-and-launch works on jailbroken PS5 firmware **up to at least 11.60** when
the console runs kstuff-lite 1.13+ (Drakmor's PPR-A53 patch, released 2026-09; earlier
kstuff builds top out at 11.40). The console-side ceiling is the jailbreak stack, not
the package format — as soon as a newer kstuff exists for 11.7x/12.xx, packages this
tool produces are expected to install and launch there too.

Once-critical builder detail, kept for future readers who might hit the same wall:
LibProsperoPkg 1.2.0's default outer PFS wrap is a random-seed AES-XTS envelope which
validates structurally and round-trips through `extract-inner` but is rejected by the
PS5 debug loader with `CE-100096-6` at launch (verified 2026-09-24 on FW 11.60). Sony's
Publishing Tools DLL always writes the outer PFS with `ProsperoPublisherImageMode.
PlaintextNoAuth` (mode 0x000D + the `PPPLAIN-NOAUTH!` seed marker — no actual wrap).
`Program.cs` now forces that mode; a `pfs-dump` diff against a `libScePubTools.dll`
reference build shows layer B/L/C are byte-identical, only the outer PFS wrap remained
non-deterministic (timestamp/ICV bytes, harmless).
