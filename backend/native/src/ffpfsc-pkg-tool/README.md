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
  framework trimming (`TrimMode=partial`, so the referenced libraries are left alone),
  no Magick.NET (LibProsperoPkg references it for icon conversion; none of our code paths
  ever load it — verified by the test harness — and it was 28 MB of the binary).

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
