# lib/ — LibProsperoPkg 1.2.0

The one dependency of `ffpfsc-pkg-tool` that is not a NuGet package.

| File | What it is |
|---|---|
| `LibProsperoPkg.dll.orig` | The pristine upstream assembly, v1.2.0.0, as compiled by its author. Never modified here. |
| `LibProsperoPkg.dll` | The same assembly with the two IL patches applied. This is what `PkgTool.csproj` links against and what ships inside the tool binary. |
| `SHA256SUMS` | Sums of both files: `shasum -a 256 -c SHA256SUMS`. |

## Provenance

- License: **GPL-3.0-or-later**. Text: `../../../LICENSE.LibProsperoPkg`; upstream notice:
  `../../../NOTICE.LibProsperoPkg`; how it is bundled: `../../../NOTICE.fpkg`.
- Source: drakmor's fork <https://github.com/drakmor/LibProsperoPkg>, commit `8551d2f`
  ("Version 1.2.0", 2026-07-02); upstream <https://github.com/SvenGDK/LibProsperoPKG>, tag `v1.2`.
  The binary was taken from the author's a53-fpkg 0.5 release, unmodified, and is kept here
  because the console behaviour of this tool was verified against exactly these bytes.

## Modification notice (GPL-3 section 5a)

`LibProsperoPkg.dll` was modified on 2026-09-25 by this project:

1. `ProsperoPkgBuilder.BuildContainer`: the `drm_type` ternary is retargeted so an
   Application volume with `"standard"` DRM is stamped `drm_type = 16`.
2. `ProsperoPkgBuilder.BuildInnerTree`: the local function `FilterFakeLibraryDirectory`
   is replaced by a single `ret`, so a source's `fakelib/` directory is kept intact.

Both patches are applied by `../patches/CecilPatch` (Mono.Cecil, pattern-based, idempotent);
the reasoning is in `../patches/README.md`. To reproduce the patched file from the pristine one:

```bash
cp LibProsperoPkg.dll.orig LibProsperoPkg.dll
cd ../patches/CecilPatch && dotnet run -- ../../lib/LibProsperoPkg.dll
```
