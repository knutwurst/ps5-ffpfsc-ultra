# ffpfsc-pkg-tool — source

`backend/native/ffpfsc-pkg-tool` is a self-contained, single-file .NET 9 executable
(macOS arm64) that the app calls for everything fPKG: `build`, `extract-inner`,
`extract-outer`, `inspect`, `validate`, `version`. This folder holds the part we wrote:

- `Program.cs` — the command-line wrapper around drakmor's LibProsperoPkg 1.2.0: argument
  parsing, the `[PHASE]`/progress lines the GUI translates, the 17-point `validate`
  checklist, and the pin `KrakenMaxDegreeOfParallelism = 1` (LibProsperoPkg 1.2.0's
  outer-PFS AES-XTS worker crashes above that).
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

## Why not NativeAOT

Measured on 240 MB of mixed data: a NativeAOT build is 16–18 MB instead of 25 MB but
10–23 % slower on the Kraken/AES path (the JIT's tiered compilation wins there). For
100 GB games that is a quarter of an hour, so the JIT build ships.
