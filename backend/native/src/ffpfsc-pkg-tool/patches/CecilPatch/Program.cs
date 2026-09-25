// Applies the ps5-ffpfsc-ultra patches to a pristine LibProsperoPkg 1.2.0 assembly.
// Usage: dotnet run -- <path/to/LibProsperoPkg.dll>   (patched in place; a .orig backup is written)
// See ../README.md for what each patch does and why it is required on a retail PS5.
using System;
using System.IO;
using System.Linq;
using Mono.Cecil;
using Mono.Cecil.Cil;

if (args.Length != 1) { Console.Error.WriteLine("usage: CecilPatch <LibProsperoPkg.dll>"); return 2; }
var dll = Path.GetFullPath(args[0]);
if (!File.Exists(dll)) { Console.Error.WriteLine($"not found: {dll}"); return 2; }
var orig = dll + ".orig";
if (!File.Exists(orig)) File.Copy(dll, orig);

int applied = 0;
using (var asm = AssemblyDefinition.ReadAssembly(dll, new ReaderParameters { ReadWrite = true }))
{
    var builder = asm.MainModule.GetType("LibProsperoPkg.PKG.ProsperoPkgBuilder")
                  ?? throw new InvalidOperationException("LibProsperoPkg.PKG.ProsperoPkgBuilder not found — wrong assembly?");

    // ---- Patch 1: drm_type = 16 for Application volumes -------------------------------
    // Upstream: drm_type = (VolumeType != Application || applicationDrmType == "upgradable") ? 16 : 0
    // IL:  brtrue.s L16 ; ldloc.1 ; brfalse.s L0 ; L16: ldc.i4.s 16 ; br.s STORE ; L0: ldc.i4.0 ; STORE: stfld drm_type
    // Retarget the brfalse.s at L16 so the "standard" path also loads 16. Stack stays balanced.
    bool p1 = false;
    foreach (var m in builder.Methods.Where(m => m.HasBody))
    {
        var il = m.Body.Instructions;
        for (int i = 4; i < il.Count; i++)
        {
            if (il[i].OpCode != OpCodes.Stfld || il[i].Operand is not FieldReference fr || fr.Name != "drm_type") continue;
            var ldc0 = il[i - 1]; var brs = il[i - 2]; var ldc16 = il[i - 3]; var brf = il[i - 4];
            if (ldc0.OpCode != OpCodes.Ldc_I4_0 || brs.OpCode != OpCodes.Br_S || ldc16.OpCode != OpCodes.Ldc_I4_S
                || (sbyte)ldc16.Operand != 16 || brf.OpCode != OpCodes.Brfalse_S) continue;
            if (brf.Operand == ldc0) { brf.Operand = ldc16; p1 = true; Console.WriteLine($"[1] drm_type ternary retargeted in {m.Name}"); }
            else if (brf.Operand == ldc16) { p1 = true; Console.WriteLine($"[1] drm_type ternary already patched in {m.Name}"); }
        }
    }
    if (!p1) { Console.Error.WriteLine("[1] FAILED: drm_type ternary pattern not found (upstream changed?)"); return 3; }
    applied++;

    // ---- Patch 2: keep fakelib/libSceAmpr.sprx + libScePlayGo.sprx ------------------------
    // Upstream: local function FilterFakeLibraryDirectory() removes exactly these two files from
    // fakelib/. They are the AMPR/PlayGo backport emulators a scene dump ships for older firmware;
    // without them the eboot's module imports fail at launch (CE-100022-5). Make it a no-op.
    var f = builder.Methods.FirstOrDefault(m => m.Name.Contains("FilterFakeLibraryDirectory") && m.HasBody);
    if (f == null) { Console.Error.WriteLine("[2] FAILED: FilterFakeLibraryDirectory not found (upstream changed?)"); return 3; }
    if (f.Body.Instructions.Count == 1 && f.Body.Instructions[0].OpCode == OpCodes.Ret)
        Console.WriteLine("[2] FilterFakeLibraryDirectory already a no-op");
    else
    {
        var ilp = f.Body.GetILProcessor();
        f.Body.Instructions.Clear(); f.Body.ExceptionHandlers.Clear(); f.Body.Variables.Clear();
        ilp.Append(ilp.Create(OpCodes.Ret));
        Console.WriteLine("[2] FilterFakeLibraryDirectory -> ret (fakelib emulators are kept)");
    }
    applied++;

    asm.Write();
}
Console.WriteLine($"OK: {applied} patch(es) applied to {dll}  (backup: {orig})");
return 0;
