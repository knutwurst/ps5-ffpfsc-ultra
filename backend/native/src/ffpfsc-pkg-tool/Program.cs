using System;
using System.IO;
using System.Text.Json;
using LibProsperoPkg;
using LibProsperoPkg.PKG;

namespace PkgTool;

// ffpfsc-pkg-tool: thin CLI over drakmor's LibProsperoPkg 1.2.0 (a53 fpkg-gui 0.5).
// Purpose: fPKG extract / inspect / build for ps5-ffpfsc-ultra.
// Ships as a self-contained osx-arm64 binary under backend/native/.

internal static class Program
{
    static int Main(string[] args)
    {
        try
        {
            if (args.Length == 0) { PrintUsage(); return 2; }
            return args[0].ToLowerInvariant() switch
            {
                "version" => CmdVersion(),
                "inspect" => CmdInspect(args),
                "extract-inner" => CmdExtract(args, inner: true),
                "extract-outer" => CmdExtract(args, inner: false),
                "build" => CmdBuild(args),
                "validate" => CmdValidate(args),
                _ => Bad("unknown command: " + args[0]),
            };
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("[error] " + ex.GetType().Name + ": " + ex.Message);
            if (Environment.GetEnvironmentVariable("PKG_TOOL_TRACE") == "1")
                Console.Error.WriteLine(ex.StackTrace);
            return 1;
        }
    }

    static void PrintUsage()
    {
        Console.WriteLine("ffpfsc-pkg-tool <command> [args]");
        Console.WriteLine("  version");
        Console.WriteLine("  inspect       <pkg>                              [--json]");
        Console.WriteLine("  extract-inner <pkg> <out-dir> [--passcode P]     [--json]");
        Console.WriteLine("  extract-outer <pkg> <out-dir> [--passcode P]     [--decompress|--no-decompress]");
        Console.WriteLine("  validate      <pkg>                              [--json]");
        Console.WriteLine("      Diagnostic checklist: header magic + fields, CNT wrap, PFS bounds,");
        Console.WriteLine("      required sce_sys entries, param.json coherence, eboot fake-self magic.");
        Console.WriteLine("  build         <src-dir> <out-dir>");
        Console.WriteLine("      --content-id <36-char>       (required)");
        Console.WriteLine("      --title-id   <9-char>        (required, e.g. PPSA00000)");
        Console.WriteLine("      --title      <text>          (written to param.json if generated)");
        Console.WriteLine("      --version    <NN.NNN.NNN>    (default 01.000.000)");
        Console.WriteLine("      --passcode   <32-char>       (default 32 zeros)");
        Console.WriteLine("      --mode       none|zlib|kraken   inner-image codec (default none)");
        Console.WriteLine("      --kraken-backend automatic|builtin|publishingtools|uncompressed (default builtin)");
        Console.WriteLine("      --pubtools-dll <path>        libScePubTools.dll for kraken/publishingtools");
        Console.WriteLine("      --deterministic              byte-reproducible build");
        Console.WriteLine("      --temp <dir>                 intermediate files go here (default: $TMPDIR)");
        Console.WriteLine("      --level <n>                  compression level: Kraken -4..9, zlib 0..9 (default 7)");
        Console.WriteLine("");
        Console.WriteLine("  Default passcode 32 x '0'. Default output is a finalized debug image.");
    }

    static int Bad(string m) { Console.Error.WriteLine("[usage] " + m); PrintUsage(); return 2; }

    static int CmdVersion()
    {
        var asm = typeof(ProsperoPkgReader).Assembly;
        var name = asm.GetName();
        Console.WriteLine("ffpfsc-pkg-tool 0.1.0");
        Console.WriteLine($"LibProsperoPkg: {name.Name} v{name.Version}");
        Console.WriteLine($".NET: {Environment.Version}");
        Console.WriteLine($"Host: {Environment.OSVersion.Platform} {Environment.OSVersion.Version} ({System.Runtime.InteropServices.RuntimeInformation.OSArchitecture})");
        Console.WriteLine($"SHA3-256 (System): {System.Security.Cryptography.SHA3_256.IsSupported}");
        return 0;
    }

    static int CmdInspect(string[] args)
    {
        if (args.Length < 2) return Bad("inspect needs <pkg>");
        var pkg = args[1];
        if (!File.Exists(pkg)) throw new FileNotFoundException("package not found", pkg);
        bool json = Array.Exists(args, a => a == "--json");
        var type = ProsperoPkgReader.DetectType(pkg);
        var pkgObj = ProsperoPkgReader.Read(pkg);
        var info = new
        {
            path = Path.GetFullPath(pkg),
            size_bytes = new FileInfo(pkg).Length,
            package_type = type?.ToString(),
            content_id = pkgObj.Header?.ContentId,
            title_id = pkgObj.Header?.ContentId != null && pkgObj.Header.ContentId.Length >= 16
                ? pkgObj.Header.ContentId.Substring(7, 9) : null,
            drm_type = pkgObj.Header?.DrmType,
            content_type = pkgObj.Header?.ContentType,
            entry_count = pkgObj.Header?.EntryCount,
            sc_entry_count = pkgObj.Header?.ScEntryCount,
            finalized = pkgObj.Fih != null,
            fih = pkgObj.Fih == null ? null : new {
                is_official = pkgObj.Fih.IsOfficial,
                signed_byte = pkgObj.Fih.SignedByte,
                pfs_image_offset = pkgObj.Fih.PfsImageOffset,
                pfs_image_size = pkgObj.Fih.PfsImageSize,
                embedded_cnt_off = pkgObj.Fih.EmbeddedCntOffset,
                inner_blocks = pkgObj.Fih.InnerImageBlockCount,
                metadata_blocks = pkgObj.Fih.MetadataBlockCount,
                naps_layout_size = pkgObj.Fih.NapsLayoutSize,
            },
        };
        if (json) Console.WriteLine(JsonSerializer.Serialize(info, new JsonSerializerOptions { WriteIndented = true }));
        else
        {
            Console.WriteLine($"path         : {info.path}");
            Console.WriteLine($"size         : {info.size_bytes:N0} bytes");
            Console.WriteLine($"package type : {info.package_type}");
            Console.WriteLine($"content id   : {info.content_id}");
            Console.WriteLine($"title id     : {info.title_id}");
            Console.WriteLine($"entries      : {info.entry_count} ({info.sc_entry_count} system)");
            Console.WriteLine($"finalized    : {info.finalized}");
            if (info.fih != null)
            {
                Console.WriteLine($"fih.official : {info.fih.is_official}");
                Console.WriteLine($"fih.pfs off  : 0x{info.fih.pfs_image_offset:X}");
                Console.WriteLine($"fih.pfs size : {info.fih.pfs_image_size:N0}");
                Console.WriteLine($"fih.naps len : {info.fih.naps_layout_size:N0}");
            }
        }
        return 0;
    }

    static int CmdExtract(string[] args, bool inner)
    {
        if (args.Length < 3) return Bad("extract needs <pkg> <out-dir>");
        var pkg = args[1]; var outDir = args[2];
        string passcode = new string('0', 32);
        bool decompress = true;
        bool json = false;
        bool mergeCnt = true;   // for extract-inner: also drop param.json/icon0/playgo-* into sce_sys/
        for (int i = 3; i < args.Length; i++)
        {
            if (args[i] == "--passcode" && i + 1 < args.Length) passcode = args[++i];
            else if (args[i] == "--decompress") decompress = true;
            else if (args[i] == "--no-decompress") decompress = false;
            else if (args[i] == "--json") json = true;
            else if (args[i] == "--no-merge-cnt") mergeCnt = false;
        }
        Directory.CreateDirectory(outDir);
        Console.Error.WriteLine($"[info] extract-{(inner ? "inner" : "outer")}  {pkg} -> {outDir}");
        var files = inner
            ? ProsperoPackageArchive.ExtractInnerFiles(pkg, outDir, passcode, decompressFiles: decompress)
            : ProsperoPackageArchive.ExtractOuterFiles(pkg, outDir, passcode, decompress: decompress);

        // For inner-image extraction: sce_sys/param.json, sce_sys/icon0.png and the
        // sce_sys/playgo-*.dat files live in the CNT entry table, not the inner PFS.
        // Without them, mkpfs pack-to-ffpfsc would refuse the /app0 folder. Merge them
        // into the extracted tree so downstream tools see a complete PS5 game folder.
        int mergedCount = 0;
        if (inner && mergeCnt)
        {
            var tmpCnt = Path.Combine(outDir, ".cnt-tmp-" + Guid.NewGuid().ToString("N"));
            try
            {
                Directory.CreateDirectory(tmpCnt);
                ProsperoPackageArchive.ExtractCntEntries(pkg, tmpCnt, passcode, includeEncrypted: true);
                var sceSys = Path.Combine(outDir, "sce_sys");
                Directory.CreateDirectory(sceSys);
                // CNT filenames we lift into sce_sys/ (the console's /app0 layout).
                var wanted = new[] { "param.json", "icon0.png", "pic0.png", "pic1.png",
                                     "playgo-chunk.dat", "playgo-ficm.dat", "playgo-hash-table.dat",
                                     "playgo-manifest.xml", "npbind.dat", "changeinfo.xml" };
                foreach (var name in wanted)
                {
                    var src = Path.Combine(tmpCnt, name);
                    if (!File.Exists(src)) continue;
                    var dst = Path.Combine(sceSys, name);
                    File.Copy(src, dst, overwrite: true);
                    mergedCount++;
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[warn] CNT metadata merge failed: " + ex.Message + " — extract still succeeded but /app0 may be incomplete.");
            }
            finally
            {
                try { Directory.Delete(tmpCnt, recursive: true); } catch { }
            }
        }

        if (json)
        {
            Console.WriteLine(JsonSerializer.Serialize(new {
                extracted = files.Count,
                cnt_merged = mergedCount,
                output = Path.GetFullPath(outDir), files
            }, new JsonSerializerOptions { WriteIndented = true }));
        }
        else
        {
            string extra = (inner && mergedCount > 0) ? $" (+{mergedCount} sce_sys metadata)" : "";
            Console.WriteLine($"OK — extracted {files.Count} file(s){extra} to {Path.GetFullPath(outDir)}");
        }
        return 0;
    }

    static int CmdValidate(string[] args)
    {
        if (args.Length < 2) return Bad("validate needs <pkg>");
        var pkg = args[1];
        bool json = Array.Exists(args, a => a == "--json");
        if (!File.Exists(pkg)) throw new FileNotFoundException("package not found", pkg);
        var checks = new System.Collections.Generic.List<ValidateResult>();
        void Ok(string k, string msg) => checks.Add(new ValidateResult { Check = k, Level = "pass", Message = msg });
        void Warn(string k, string msg) => checks.Add(new ValidateResult { Check = k, Level = "warn", Message = msg });
        void Fail(string k, string msg) => checks.Add(new ValidateResult { Check = k, Level = "fail", Message = msg });

        long sz = new FileInfo(pkg).Length;
        Ok("file", $"{sz:N0} bytes on disk");

        // Magic
        using (var fs = new FileStream(pkg, FileMode.Open, FileAccess.Read, FileShare.Read))
        {
            var m = new byte[4]; fs.Read(m, 0, 4);
            string magic = (m[0] == 0x7F && m[1] == 'C' && m[2] == 'N' && m[3] == 'T') ? "CNT"
                         : (m[0] == 0x7F && m[1] == 'F' && m[2] == 'I' && m[3] == 'H') ? "FIH"
                         : $"UNKNOWN ({m[0]:X2} {m[1]:X2} {m[2]:X2} {m[3]:X2})";
            if (magic == "FIH")      Ok("magic", "\x7FFIH (finalized image, installable)");
            else if (magic == "CNT") Fail("magic", "\x7FCNT metadata container only — NOT installable, needs finalization");
            else                     Fail("magic", magic);
        }

        // Parse the container
        ProsperoPkg pkgObj;
        try { pkgObj = ProsperoPkgReader.Read(pkg); }
        catch (Exception ex) { Fail("parse", "reader threw: " + ex.Message); Report(checks, json); return 1; }
        var h = pkgObj.Header;
        var fih = pkgObj.Fih;
        if (h == null) { Fail("header", "no CNT header parsed"); Report(checks, json); return 1; }
        Ok("header", $"content_id={h.ContentId} entries={h.EntryCount} sc_entries={h.ScEntryCount} drm=0x{h.DrmType:X} content_type=0x{h.ContentType:X}");

        // Content id format
        var cidRe = new System.Text.RegularExpressions.Regex("^[A-Z]{2}[0-9]{4}-[A-Z]{4}[0-9]{5}_00-[A-Z0-9]{16}$");
        if (h.ContentId.Length == 36 && cidRe.IsMatch(h.ContentId))
            Ok("content_id", "matches XX0000-XXXX00000_00-XXXXXXXXXXXXXXXX");
        else
            Fail("content_id", $"'{h.ContentId}' is not the expected 36-char pattern");

        // FIH
        if (fih == null) { Fail("finalized", "no FIH header — this is a metadata-only CNT, not installable"); }
        else
        {
            if (fih.SignedByte == 0x00)      Ok("fih.signed_byte", "0x00 (debug image, installable on debug consoles)");
            else if (fih.SignedByte == 0x80) Warn("fih.signed_byte", "0x80 (retail image — needs the console-provisioned image key)");
            else                             Fail("fih.signed_byte", $"unexpected value 0x{fih.SignedByte:X}");
            long imgEnd = checked((long)fih.PfsImageOffset + (long)fih.PfsImageSize);
            if (imgEnd <= sz && fih.PfsImageOffset >= 0x10000)
                Ok("fih.pfs_bounds", $"pfs @0x{fih.PfsImageOffset:X}..0x{imgEnd:X} inside file");
            else
                Fail("fih.pfs_bounds", $"pfs @0x{fih.PfsImageOffset:X} + 0x{fih.PfsImageSize:X} = 0x{imgEnd:X} vs file 0x{sz:X}");
            if ((long)fih.EmbeddedCntOffset > 0 && (long)fih.EmbeddedCntOffset < sz)
                Ok("fih.cnt_bounds", $"embedded CNT @0x{fih.EmbeddedCntOffset:X}");
            else
                Fail("fih.cnt_bounds", $"embedded CNT offset 0x{fih.EmbeddedCntOffset:X} outside file");
            if (fih.NapsLayoutSize > 0) Ok("fih.naps_layout", $"{fih.NapsLayoutSize:N0} bytes");
            else                        Warn("fih.naps_layout", "size = 0 (unusual for a data-first inner)");
        }

        // Signature wrap over CNT
        try
        {
            bool okSig = ProsperoPackageArchive.VerifyCntMetadataSignature(pkg);
            if (okSig) Ok("cnt.wrap", "RSA-3072 public wrap verifies");
            else       Fail("cnt.wrap", "RSA-3072 public wrap DID NOT verify — CNT tampered or wrong keys");
        }
        catch (Exception ex) { Fail("cnt.wrap", "check threw: " + ex.Message); }

        // Required CNT entries
        var tmpCnt = Path.Combine(Path.GetTempPath(), "fpkg-validate-" + Guid.NewGuid().ToString("N"));
        System.Collections.Generic.List<string> cntFiles = new();
        try
        {
            Directory.CreateDirectory(tmpCnt);
            cntFiles = new System.Collections.Generic.List<string>(
                ProsperoPackageArchive.ExtractCntEntries(pkg, tmpCnt, new string('0', 32), includeEncrypted: true));
            var need = new[] { "param.json", "icon0.png", "playgo-chunk.dat" };
            foreach (var n in need)
            {
                var p = Path.Combine(tmpCnt, n);
                if (File.Exists(p)) Ok("cnt." + n, $"{new FileInfo(p).Length:N0} bytes present");
                else                Fail("cnt." + n, "MISSING from CNT — installer will reject");
            }
            // param.json coherence with header
            var pj = Path.Combine(tmpCnt, "param.json");
            if (File.Exists(pj))
            {
                try
                {
                    var doc = System.Text.Json.JsonDocument.Parse(File.ReadAllText(pj));
                    var root = doc.RootElement;
                    if (root.TryGetProperty("contentId", out var cid))
                    {
                        var v = cid.GetString() ?? "";
                        if (v == h.ContentId) Ok("param.contentId", "matches CNT header");
                        else Fail("param.contentId", $"'{v}' != CNT header '{h.ContentId}'");
                    }
                    if (root.TryGetProperty("titleId", out var tid))
                    {
                        var v = tid.GetString() ?? "";
                        Ok("param.titleId", v);
                    }
                    if (root.TryGetProperty("applicationDrmType", out var drm))
                        Ok("param.applicationDrmType", drm.GetString() ?? "");
                }
                catch (Exception ex) { Fail("param.parse", ex.Message); }
            }
        }
        catch (Exception ex) { Fail("cnt.entries", "extract threw: " + ex.Message); }
        finally { try { Directory.Delete(tmpCnt, recursive: true); } catch { } }

        // Try extracting inner /app0 to check the PFS decodes
        var tmpInner = Path.Combine(Path.GetTempPath(), "fpkg-validate-inner-" + Guid.NewGuid().ToString("N"));
        try
        {
            Directory.CreateDirectory(tmpInner);
            var innerFiles = ProsperoPackageArchive.ExtractInnerFiles(pkg, tmpInner, new string('0', 32), decompressFiles: true);
            Ok("inner.pfs", $"decoded {innerFiles.Count} file(s) from inner PFS");
            var ebootPath = Path.Combine(tmpInner, "eboot.bin");
            if (File.Exists(ebootPath))
            {
                var head = new byte[4]; using (var f = File.OpenRead(ebootPath)) f.Read(head, 0, 4);
                uint magic = (uint)(head[0] | (head[1] << 8) | (head[2] << 16) | (head[3] << 24));
                if      (magic == 0x1D3D154Fu) Ok("eboot.magic", "SCE fake-self (0x1D3D154F) — good");
                else if (magic == 0xEEF51454u) Ok("eboot.magic", "SCE encrypted fake-self (0xEEF51454) — good (v1.2.0 default)");
                else if (magic == 0x464C457Fu) Fail("eboot.magic", "raw ELF (0x7F454C46) — not fake-signed; kstuff-fpkg install path will refuse");
                else Warn("eboot.magic", $"unrecognized 0x{magic:X8}");
            }
            else
            {
                Fail("inner.eboot", "eboot.bin not present in inner PFS");
            }
        }
        catch (Exception ex) { Fail("inner.pfs", "extract threw: " + ex.Message); }
        finally { try { Directory.Delete(tmpInner, recursive: true); } catch { } }

        Report(checks, json);
        int fails = 0;
        foreach (var r in checks) if (r.Level == "fail") fails++;
        return fails == 0 ? 0 : 1;
    }

    sealed class ValidateResult
    {
        public string Check { get; set; } = "";
        public string Level { get; set; } = "pass"; // pass / warn / fail
        public string Message { get; set; } = "";
    }

    static void Report(System.Collections.Generic.List<ValidateResult> checks, bool json)
    {
        int p = 0, w = 0, f = 0;
        foreach (var r in checks) { if (r.Level == "pass") p++; else if (r.Level == "warn") w++; else f++; }
        if (json)
        {
            Console.WriteLine(JsonSerializer.Serialize(new
            {
                pass = p, warn = w, fail = f,
                results = checks,
            }, new JsonSerializerOptions { WriteIndented = true }));
            return;
        }
        foreach (var r in checks)
        {
            string tag = r.Level switch { "pass" => "[ok  ]", "warn" => "[WARN]", _ => "[FAIL]" };
            Console.WriteLine($"  {tag}  {r.Check,-24}  {r.Message}");
        }
        Console.WriteLine();
        Console.WriteLine($"summary: {p} passed, {w} warned, {f} failed");
    }

        static int CmdBuild(string[] args)
    {
        if (args.Length < 3) return Bad("build needs <src-dir> <out-dir>");
        var opts = new ProsperoBuildOptions
        {
            SourceFolder = args[1],
            OutputFolder = args[2],
            Mode = ProsperoPackageMode.Application,
            OutputFormat = ProsperoOutputFormat.DebugImage,
            InnerCompression = ProsperoInnerCompression.None,
            KrakenBackend = ProsperoKrakenBackend.BuiltIn,
            Passcode = new string('0', 32),
            Version = "01.000.000",
            // Force single-threaded worker across the outer-PFS AES-XTS pass —
            // v1.2.0's OuterBlockWorker races (SIGSEGV in BlockSector) when this
            // is >1. The build path also uses this value for its outer parallelism.
            KrakenMaxDegreeOfParallelism = 1,
            LegacyZlibMaxDegreeOfParallelism = 1,
        };
        for (int i = 3; i < args.Length; i++)
        {
            string a = args[i];
            string? v = i + 1 < args.Length ? args[i + 1] : null;
            switch (a)
            {
                case "--content-id": opts.ContentId = v!; i++; break;
                case "--title-id": opts.TitleId = v!; i++; break;
                case "--title": opts.Title = v!; i++; break;
                case "--version": opts.Version = v!; i++; break;
                case "--passcode": opts.Passcode = v!; i++; break;
                case "--mode":
                    opts.InnerCompression = (v!.ToLowerInvariant()) switch
                    {
                        "none" => ProsperoInnerCompression.None,
                        "zlib" => ProsperoInnerCompression.Zlib,
                        "kraken" => ProsperoInnerCompression.Kraken,
                        _ => throw new ArgumentException($"unknown --mode: {v}")
                    };
                    i++;
                    break;
                case "--kraken-backend":
                    opts.KrakenBackend = (v!.ToLowerInvariant()) switch
                    {
                        "automatic" => ProsperoKrakenBackend.Automatic,
                        "builtin" => ProsperoKrakenBackend.BuiltIn,
                        "publishingtools" => ProsperoKrakenBackend.PublishingToolsRequired,
                        "uncompressed" => ProsperoKrakenBackend.Uncompressed,
                        _ => throw new ArgumentException($"unknown --kraken-backend: {v}")
                    };
                    i++;
                    break;
                case "--pubtools-dll": opts.PublishingToolsLibraryPath = v!; i++; break;
                case "--parallelism":
                case "-j":
                    if (int.TryParse(v, out int j) && j >= 1) { opts.KrakenMaxDegreeOfParallelism = j; opts.LegacyZlibMaxDegreeOfParallelism = j; }
                    else throw new ArgumentException("--parallelism needs integer >= 1");
                    i++;
                    break;
                case "--deterministic": opts.DeterministicBuild = true; break;
                case "--temp":
                    if (string.IsNullOrWhiteSpace(v)) throw new ArgumentException("--temp needs a directory");
                    Directory.CreateDirectory(v!);
                    opts.TemporaryDirectory = v;
                    i++;
                    break;
                case "--level":
                    if (!int.TryParse(v, out int lvl)) throw new ArgumentException("--level needs an integer");
                    // Kraken accepts -4..9 (drakmor's encoder), the legacy zlib path 0..9.
                    opts.KrakenCompressionLevel = Math.Clamp(lvl, -4, 9);
                    opts.LegacyZlibCompressionLevel = Math.Clamp(lvl, 0, 9);
                    i++;
                    break;
                default: return Bad("unknown build flag: " + a);
            }
        }
        if (string.IsNullOrWhiteSpace(opts.ContentId)) return Bad("--content-id required");
        if (string.IsNullOrWhiteSpace(opts.TitleId)) return Bad("--title-id required");
        Directory.CreateDirectory(opts.OutputFolder);
        Console.Error.WriteLine($"[info] build  {opts.SourceFolder} -> {opts.OutputFolder}  (inner={opts.InnerCompression}, backend={opts.KrakenBackend}, level={opts.KrakenCompressionLevel}, temp={opts.TemporaryDirectory ?? "$TMPDIR"})");
        var result = ProsperoPackageBuilder.Build(opts, logger: s => Console.Error.WriteLine("  " + s));
        Console.WriteLine($"OK — wrote {result.OutputPath} ({new FileInfo(result.OutputPath).Length:N0} B)");
        if (result.Warnings != null)
            foreach (var w in result.Warnings) Console.Error.WriteLine("[warn] " + w);
        return 0;
    }
}
