# LibProsperoPkg.dll patches

`ffpfsc-pkg-tool` links against drakmor's LibProsperoPkg 1.2.0 (BSD-3, taken
compiled from the a53-fpkg 0.5 drop; `lib/` is not in git). Two of its
behaviours keep a retail-shape package from launching on a jailbroken PS5.
Both are fixed by rewriting IL in the assembly before the tool is compiled.

## Applying

`CecilPatch/` is a small .NET console project using Mono.Cecil. It patches
the DLL in place, writes a `.orig` backup next to it, finds both sites by
instruction pattern (not by file offset), and is idempotent — running it on
an already patched DLL reports "already patched" and changes nothing.

```bash
cd backend/native/src/ffpfsc-pkg-tool/patches/CecilPatch
dotnet run -- ../../lib/LibProsperoPkg.dll
```

Then `dotnet publish` the tool as usual. If upstream changes and a pattern
is not found, the patcher exits 3 and names the patch — do not ship a tool
built from an unpatched DLL.

## Patch 1 — `drm_type = 16` for Application volumes

`ProsperoPkgBuilder.BuildContainer` stamps the CNT header's `drm_type` with

```csharp
drm_type = (VolumeType != Application || applicationDrmType == "upgradable") ? 16u : 0u
```

so a normal `"standard"`-DRM game gets `drm_type = 0` (free). Sony's
publisher writes 16 for retail games (checked against two untouched retail
packages); the console's retail-DRM path expects 16. With 0 and real
license records the homescreen shows a padlock and the launch fails with
CE-100022-5.

The IL is

```
brtrue.s  L16        ; VolumeType != Application
ldloc.1              ; applicationDrmType == "upgradable"
brfalse.s L0
L16: ldc.i4.s 16
     br.s STORE
L0:  ldc.i4.0
STORE: stfld drm_type
```

The patcher retargets the `brfalse.s` to `L16`, so the "standard" path also
loads 16. The branch still consumes its operand, so the stack stays balanced
and the method verifies. `ldc.i4.0` becomes dead code. `applicationDrmType`
in param.json is untouched — flipping it to `"upgradable"` instead would
make the console look for an upgrade chain the package does not have.

## Patch 2 — keep `fakelib/libSceAmpr.sprx` and `libScePlayGo.sprx`

The local function `FilterFakeLibraryDirectory` in `BuildInnerTree` removes
exactly these two files from a source's `fakelib/` directory. They are the
AMPR and PlayGo emulators a backported dump ships so the title runs on
firmware older than the one it was built for; ShadowMountPlus overlays
`/app0/fakelib` into the sandbox before spawn. Without them the eboot's
module imports fail and the launch dies with CE-100022-5. Retail packages
built with Sony's own tools carry other emulators in `fakelib/` (libSceAgc,
libScePsml, …) and some ship an `ampr_emu.index` too, so keeping the
directory intact is the right shape.

The patcher replaces the function body with a single `ret`.

## What is deliberately not patched

- The PlayGo prepared-set handling (upstream preserves any 3/3 set without
  looking inside). That is handled in `Program.cs` before the build: the
  files are validated against their on-wire format and a corrupt set is
  dropped from the staged mirror so upstream regenerates it.
- Keystone generation. Upstream keeps a present `sce_sys/keystone` and only
  generates one when missing — which is correct: the keystone is the
  save-data key, and a regenerated one makes every existing save unreadable.
