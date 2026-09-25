using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using LibProsperoPkg;
using LibProsperoPkg.Content;
using LibProsperoPkg.PFS;
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
            RedirectMagickNative();                    // must run before ANY Magick.NET call
            if (args.Length == 0) { PrintUsage(); return 2; }
            return args[0].ToLowerInvariant() switch
            {
                "version" => CmdVersion(),
                "inspect" => CmdInspect(args),
                "list-inner" => CmdListInner(args),
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
        Console.WriteLine("  list-inner    <pkg> [--passcode P]");
        Console.WriteLine("      One JSON object on stdout: the /app0 tree (files with logical sizes, every");
        Console.WriteLine("      directory, CNT-lifted sce_sys metadata tagged \"source\":\"cnt\") — read via");
        Console.WriteLine("      random access, the inner image is NOT decoded as a whole.");
        Console.WriteLine("  extract-inner <pkg> <out-dir> [--passcode P]     [--json]");
        Console.WriteLine("  extract-inner <pkg> <out-dir> --members <file> [--passcode P] [--json]");
        Console.WriteLine("      Selective: <file> lists one path per line (relative to /app0); a directory");
        Console.WriteLine("      means its whole subtree incl. empty folders. Prints '[####] NN% extract (path)'.");
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
        Console.WriteLine("      --playgo-chunks <1..255>     PlayGo chunk count (auto-detected from source sce_sys/playgo-chunk.dat)");
        Console.WriteLine("      --fake-sign / --no-fake-sign fake-sign raw ELFs in source before packing (default ON; idempotent)");
        Console.WriteLine("      --regen-playgo               discard source sce_sys/playgo-*.dat and let the builder regenerate them");
        Console.WriteLine("                                   (a CORRUPT prepared set — wrong format, e.g. JSON under hash-table.dat — is");
        Console.WriteLine("                                   always discarded automatically; this forces it for valid-looking sets too)");
        Console.WriteLine("      --hdr-flag / --no-hdr-flag   set param.json attribute bit 29 (HDR support) — default ON; without it");
        Console.WriteLine("                                   a console on \"HDR when supported\" runs the title in SDR (verified)");
        Console.WriteLine("      --retail-normalize / --no-retail-normalize");
        Console.WriteLine("                                   auto-upgrade a \"standard\" retail source (default ON):");
        Console.WriteLine("                                     staged param.json standard -> upgradable (drm_type=16),");
        Console.WriteLine("                                     replace placeholder license.dat/info with a valid debug license,");
        Console.WriteLine("                                     add CNT entries 0x0400/0x0401 via IProsperoLicenseProvider");
        Console.WriteLine("");
        Console.WriteLine("  Default passcode 32 x '0'. Default output is a finalized debug image.");
        Console.WriteLine("  Auto-fake-sign scans for raw ELF magic (0x7F454C46) in eboot.bin, *.elf, *.prx, *.sprx");
        Console.WriteLine("  and rewrites them as SCE fake-selves in a hardlink mirror; source is never modified.");
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
        var info = new InspectDoc
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
            fih = pkgObj.Fih == null ? null : new InspectFihDoc {
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
        if (json) Console.WriteLine(JsonSerializer.Serialize(info, PkgToolJsonContext.Indented.InspectDoc));
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
        string? membersFile = null;
        for (int i = 3; i < args.Length; i++)
        {
            if (args[i] == "--passcode" && i + 1 < args.Length) passcode = args[++i];
            else if (args[i] == "--decompress") decompress = true;
            else if (args[i] == "--no-decompress") decompress = false;
            else if (args[i] == "--json") json = true;
            else if (args[i] == "--no-merge-cnt") mergeCnt = false;
            else if (args[i] == "--members" && i + 1 < args.Length) membersFile = args[++i];
        }
        if (membersFile != null)
        {
            if (!inner) return Bad("--members is only supported by extract-inner");
            return CmdExtractMembers(pkg, outDir, passcode, membersFile, json);
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
                var wanted = CntSceSysNames;
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
            Console.WriteLine(JsonSerializer.Serialize(new ExtractResultDoc {
                extracted = files.Count,
                cnt_merged = mergedCount,
                output = Path.GetFullPath(outDir), files = files.ToList()
            }, PkgToolJsonContext.Indented.ExtractResultDoc));
        }
        else
        {
            string extra = (inner && mergedCount > 0) ? $" (+{mergedCount} sce_sys metadata)" : "";
            Console.WriteLine($"OK — extracted {files.Count} file(s){extra} to {Path.GetFullPath(outDir)}");
        }
        return 0;
    }

    // CNT entry names that belong in /app0/sce_sys/ but live in the CNT table, not the inner PFS.
    // Shared by the full extract (merge step), list-inner and the selective extract so the three
    // agree on what "the inner tree" contains.
    static readonly string[] CntSceSysNames = { "param.json", "icon0.png", "pic0.png", "pic1.png",
                                                "playgo-chunk.dat", "playgo-ficm.dat", "playgo-hash-table.dat",
                                                "playgo-manifest.xml", "npbind.dat", "changeinfo.xml" };

    /// <summary>
    /// Extracts the CNT entries into <paramref name="tmpDir"/> and returns "sce_sys/&lt;name&gt;" -> temp
    /// file for every lifted name that exists. Failures are reported into <paramref name="errors"/>
    /// (the inner tree is still usable without them, matching the full extract's [warn] behaviour).
    /// </summary>
    static Dictionary<string, string> LiftCntSceSys(string pkg, string passcode, string tmpDir, List<string> errors)
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        try
        {
            Directory.CreateDirectory(tmpDir);
            ProsperoPackageArchive.ExtractCntEntries(pkg, tmpDir, passcode, includeEncrypted: true);
            foreach (var name in CntSceSysNames)
            {
                var src = Path.Combine(tmpDir, name);
                if (File.Exists(src)) result["sce_sys/" + name] = src;
            }
        }
        catch (Exception ex)
        {
            errors.Add("CNT metadata unavailable: " + ex.Message);
        }
        return result;
    }

    // The binary is published trimmed, which turns reflection-based System.Text.Json off, so every
    // --json document goes through the source-generated serializer (PkgToolJsonContext below).
    // The classes mirror the anonymous types the commands used before: same field names, same
    // number/bool/null shapes.
    internal sealed class InspectDoc
    {
        public string path { get; set; } = "";
        public long size_bytes { get; set; }
        public string? package_type { get; set; }
        public string? content_id { get; set; }
        public string? title_id { get; set; }
        public uint? drm_type { get; set; }
        public uint? content_type { get; set; }
        public uint? entry_count { get; set; }
        public ushort? sc_entry_count { get; set; }
        public bool finalized { get; set; }
        public InspectFihDoc? fih { get; set; }
    }

    internal sealed class InspectFihDoc
    {
        public bool is_official { get; set; }
        public byte signed_byte { get; set; }
        public ulong pfs_image_offset { get; set; }
        public ulong pfs_image_size { get; set; }
        public ulong embedded_cnt_off { get; set; }
        public uint inner_blocks { get; set; }
        public uint metadata_blocks { get; set; }
        public ulong naps_layout_size { get; set; }
    }

    internal sealed class ExtractResultDoc
    {
        public int extracted { get; set; }
        public int cnt_merged { get; set; }
        public string output { get; set; } = "";
        public List<string> files { get; set; } = new();
    }

    internal sealed class ValidateReportDoc
    {
        public int pass { get; set; }
        public int warn { get; set; }
        public int fail { get; set; }
        public List<ValidateResult> results { get; set; } = new();
    }

    internal sealed class InnerEntry
    {
        public string path { get; set; } = "";
        public string type { get; set; } = "file";   // "file" | "dir"
        [System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
        public long? size { get; set; }
        [System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
        public string? source { get; set; }             // "pfs" | "cnt" (files only)
    }

    internal sealed class ListInnerDoc
    {
        public string root { get; set; } = "";
        public List<InnerEntry> entries { get; set; } = new();
        public int file_count { get; set; }
        public int dir_count { get; set; }
        public List<string> errors { get; set; } = new();
    }

    internal sealed class MembersResultDoc
    {
        public int extracted { get; set; }
        public int dirs { get; set; }
        public int cnt_extracted { get; set; }
        public string output { get; set; } = "";
        public List<string> files { get; set; } = new();
        public List<string> errors { get; set; } = new();
    }

    /// <summary>Rejects the path shapes the library rejects (empty, '.', '..' segments).</summary>
    static string SafeRelative(string rel)
    {
        var norm = rel.Replace('\\', '/').Trim().Trim('/');
        if (norm.Length == 0 || norm.Split('/').Any(p => p.Length == 0 || p == "." || p == ".."))
            throw new InvalidDataException("unsafe package path: " + rel);
        return norm;
    }

    static string SafeTarget(string root, string rel)
    {
        var rootFull = Path.GetFullPath(root) + Path.DirectorySeparatorChar;
        var full = Path.GetFullPath(Path.Combine(root, rel.Replace('/', Path.DirectorySeparatorChar)));
        if (!full.StartsWith(rootFull, StringComparison.Ordinal))
            throw new InvalidDataException("package path escapes output directory: " + rel);
        return full;
    }

    /// <summary>
    /// The /app0 tree as list-inner reports it and as extract-inner --members resolves it:
    /// inner-PFS files + dirs, plus the CNT-lifted sce_sys files (which win over a same-named
    /// PFS file, exactly like the full extract's overwrite-merge).
    /// </summary>
    static SortedDictionary<string, InnerEntry> BuildInnerTree(InnerImage img, IReadOnlyDictionary<string, string> cnt)
    {
        var tree = new SortedDictionary<string, InnerEntry>(StringComparer.Ordinal);
        void AddDirs(string filePath)
        {
            int idx = -1;
            while ((idx = filePath.IndexOf('/', idx + 1)) >= 0)
            {
                var d = filePath.Substring(0, idx);
                if (!tree.ContainsKey(d)) tree[d] = new InnerEntry { path = d, type = "dir" };
            }
        }
        foreach (var d in img.AllDirs())
        {
            var rel = SafeRelative(img.RelativePath(d));
            tree[rel] = new InnerEntry { path = rel, type = "dir" };
        }
        foreach (var f in img.Pfs.GetAllFiles())
        {
            var rel = SafeRelative(img.RelativePath(f));
            AddDirs(rel);
            tree[rel] = new InnerEntry { path = rel, type = "file", size = f.size, source = "pfs" };
        }
        foreach (var kv in cnt)
        {
            AddDirs(kv.Key);
            tree[kv.Key] = new InnerEntry { path = kv.Key, type = "file", size = new FileInfo(kv.Value).Length, source = "cnt" };
        }
        return tree;
    }

    static int CmdListInner(string[] args)
    {
        if (args.Length < 2) return Bad("list-inner needs <pkg>");
        var pkg = args[1];
        if (!File.Exists(pkg)) throw new FileNotFoundException("package not found", pkg);
        string passcode = new string('0', 32);
        for (int i = 2; i < args.Length; i++)
            if (args[i] == "--passcode" && i + 1 < args.Length) passcode = args[++i];

        var errors = new List<string>();
        var tmpCnt = Path.Combine(Path.GetTempPath(), "fpkg-list-cnt-" + Guid.NewGuid().ToString("N"));
        try
        {
            using var img = new InnerImage(pkg, passcode);
            var cnt = LiftCntSceSys(pkg, passcode, tmpCnt, errors);
            var tree = BuildInnerTree(img, cnt);
            int files = 0, dirs = 0;
            foreach (var e in tree.Values) { if (e.type == "dir") dirs++; else files++; }
            var doc = new ListInnerDoc
            {
                root = Path.GetFileName(pkg),
                entries = tree.Values.ToList(),
                file_count = files,
                dir_count = dirs,
                errors = errors,
            };
            Console.WriteLine(JsonSerializer.Serialize(doc, PkgToolJsonContext.Default.ListInnerDoc));
            if (Environment.GetEnvironmentVariable("PKG_TOOL_TRACE") == "1")
                Console.Error.WriteLine($"[trace] list-inner: logical image {img.LogicalSize:N0} B, superblock @0x{img.SuperblockOffset:X}, " +
                                        $"{img.RangeCalls} range decodes / {img.RangeBytes:N0} B");
            return 0;
        }
        finally
        {
            try { if (Directory.Exists(tmpCnt)) Directory.Delete(tmpCnt, recursive: true); } catch { }
        }
    }

    static int CmdExtractMembers(string pkg, string outDir, string passcode, string membersFile, bool json)
    {
        if (!File.Exists(pkg)) throw new FileNotFoundException("package not found", pkg);
        if (!File.Exists(membersFile)) throw new FileNotFoundException("members file not found", membersFile);
        var wanted = new List<string>();
        foreach (var raw in File.ReadAllLines(membersFile, System.Text.Encoding.UTF8))
        {
            var m = raw.Replace('\\', '/').Trim().Trim('/');
            if (m.Length > 0 && !wanted.Contains(m)) wanted.Add(m);
        }
        if (wanted.Count == 0) return Bad("--members file lists no paths");
        bool Under(string rel) => wanted.Any(m => rel == m || rel.StartsWith(m + "/", StringComparison.Ordinal));

        Directory.CreateDirectory(outDir);
        Console.Error.WriteLine($"[info] extract-inner (members)  {pkg} -> {outDir}  ({wanted.Count} member(s))");
        var errors = new List<string>();
        var tmpCnt = Path.Combine(Path.GetFullPath(outDir), ".cnt-tmp-" + Guid.NewGuid().ToString("N"));
        try
        {
            // 4 MiB cache blocks: metadata walks need few of them and file data streams through
            // the chunked fast path anyway, so the plan-rebuild cost per DecompressRange amortizes.
            using var img = new InnerImage(pkg, passcode, cacheBlockSize: 4 << 20, cacheBlocks: 8);
            var cnt = LiftCntSceSys(pkg, passcode, tmpCnt, errors);
            foreach (var e in errors) Console.Error.WriteLine("[warn] " + e);
            var tree = BuildInnerTree(img, cnt);

            var dirTargets = tree.Values.Where(e => e.type == "dir" && Under(e.path)).Select(e => e.path).ToList();
            var fileTargets = tree.Values.Where(e => e.type == "file" && Under(e.path)).ToList();
            foreach (var m in wanted)
                if (!tree.ContainsKey(m)) Console.Error.WriteLine($"[warn] member not found in the image: {m}");
            if (dirTargets.Count == 0 && fileTargets.Count == 0)
            {
                Console.Error.WriteLine("[ERROR] None of the requested items were found in the image.");
                return 1;
            }

            // Directories first (parent-first by sort order) so empty ones survive.
            foreach (var d in dirTargets) Directory.CreateDirectory(SafeTarget(outDir, d));
            // Parents of selected files that were not themselves selected.
            foreach (var f in fileTargets)
                Directory.CreateDirectory(Path.GetDirectoryName(SafeTarget(outDir, f.path))!);

            long total = fileTargets.Sum(e => e.size ?? 0), done = 0;
            int lastPct = -1;
            var written = new List<string>();
            int cntCount = 0;
            void Progress(long delta, string rel)
            {
                done += delta;
                int pct = total > 0 ? (int)Math.Min(99, done * 100 / total) : 99;
                if (pct != lastPct)
                {
                    lastPct = pct;
                    Console.WriteLine($"[####] {pct}% extract ({rel})");
                }
            }
            foreach (var e in fileTargets)
            {
                var dst = SafeTarget(outDir, e.path);
                if (e.source == "cnt")
                {
                    File.Copy(cnt[e.path], dst, overwrite: true);
                    cntCount++;
                    Progress(e.size ?? 0, e.path);
                }
                else
                {
                    var node = img.Pfs.GetFile(e.path) ?? throw new InvalidDataException("inner file vanished: " + e.path);
                    using var fs = new FileStream(dst, FileMode.Create, FileAccess.Write, FileShare.None, 1 << 20);
                    img.CopyFile(node, fs, n => Progress(n, e.path));
                }
                written.Add(e.path);
            }
            Console.WriteLine("[####] 100% extract");
            if (json)
            {
                Console.WriteLine(JsonSerializer.Serialize(new MembersResultDoc
                {
                    extracted = written.Count,
                    dirs = dirTargets.Count,
                    cnt_extracted = cntCount,
                    output = Path.GetFullPath(outDir),
                    files = written,
                    errors = errors,
                }, PkgToolJsonContext.Indented.MembersResultDoc));
            }
            else
            {
                string extra = cntCount > 0 ? $" (+{cntCount} sce_sys metadata)" : "";
                Console.WriteLine($"OK — extracted {written.Count} file(s) and {dirTargets.Count} folder(s){extra} to {Path.GetFullPath(outDir)}");
            }
            if (Environment.GetEnvironmentVariable("PKG_TOOL_TRACE") == "1")
                Console.Error.WriteLine($"[trace] members: {img.RangeCalls} range decodes / {img.RangeBytes:N0} B for {done:N0} B of output");
            return 0;
        }
        finally
        {
            try { if (Directory.Exists(tmpCnt)) Directory.Delete(tmpCnt, recursive: true); } catch { }
        }
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

    internal sealed class ValidateResult
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
            Console.WriteLine(JsonSerializer.Serialize(new ValidateReportDoc
            {
                pass = p, warn = w, fail = f,
                results = checks,
            }, PkgToolJsonContext.Indented.ValidateReportDoc));
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

    /// <summary>Mirror *src* into *dst* using hard links for every regular file and real
    /// subdirectories, so the build sees the tree unchanged without copying gigabytes.
    /// Hard links (not symlinks) — LibProsperoPkg stats the source path and expects a real
    /// file layout; some readers get the size right but read partial data through symlinks
    /// (verified: "ended after 6 of 84 bytes"). Falls back to a copy when hardlinking is
    /// refused (source across filesystems, or FS without hardlink support).</summary>
    static void MirrorAsHardLinks(string src, string dst)
    {
        foreach (var d in Directory.EnumerateDirectories(src, "*", SearchOption.AllDirectories))
            Directory.CreateDirectory(Path.Combine(dst, Path.GetRelativePath(src, d)));
        foreach (var f in Directory.EnumerateFiles(src, "*", SearchOption.AllDirectories))
        {
            var target = Path.Combine(dst, Path.GetRelativePath(src, f));
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            if (File.Exists(target) || IsSymlink(target)) File.Delete(target);
            try
            {
                var rc = link(Path.GetFullPath(f), target);
                if (rc != 0) throw new System.ComponentModel.Win32Exception(Marshal.GetLastPInvokeError(), "link() failed");
            }
            catch { File.Copy(f, target, overwrite: true); }
        }
    }

    [DllImport("libc", SetLastError = true)]
    static extern int link(string source, string target);

    static bool IsSymlink(string p)
    {
        try { return File.Exists(p) && new FileInfo(p).LinkTarget != null; } catch { return false; }
    }

    /// <summary>Direct Magick.NET's DllImport lookups for <c>Magick.Native-Q8-arm64.dll</c>
    /// at the runtimes/osx-arm64/native/ file the runtime extracts alongside the exe.
    /// Magick's P/Invoke uses the bare "Magick.Native-Q8-arm64.dll" name (Windows-style),
    /// which .NET on macOS maps to Magick.Native-Q8-arm64.dll.dylib — but only when the
    /// file lives on the default probing path. Inside a self-contained single-file exe
    /// the native asset lands at <c>&lt;exe base&gt;/runtimes/osx-arm64/native/</c>, which
    /// is not on that path. We resolve it once, ourselves.</summary>
    static bool _magickRedirected;
    static void RedirectMagickNative()
    {
        if (_magickRedirected) return;
        _magickRedirected = true;
        bool trace = Environment.GetEnvironmentVariable("PKG_TOOL_TRACE") == "1";
        var baseDir = AppContext.BaseDirectory ?? Environment.CurrentDirectory;
        // Runtime asset paths for a self-contained single-file app: with
        // IncludeNativeLibrariesForSelfExtract=true the runtime extracts natives to
        // ~/.net/<AppName>/<hash>/runtimes/<RID>/native/ (also under $TMPDIR).
        // Include the exe dir, the extraction cache, and TMPDIR — resolve every
        // Magick.Native*.dylib we can find under them.
        var appName = Assembly.GetEntryAssembly()?.GetName().Name ?? "ffpfsc-pkg-tool";
        var extRoot = Environment.GetEnvironmentVariable("DOTNET_BUNDLE_EXTRACT_BASE_DIR")
                      ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".net");
        var searchRoots = new List<string> { baseDir, Path.Combine(extRoot, appName), Path.GetTempPath() };
        string[] Candidates()
        {
            var acc = new List<string>();
            foreach (var root in searchRoots)
            {
                if (!Directory.Exists(root)) continue;
                acc.Add(root);
                try
                {
                    foreach (var sub in Directory.EnumerateDirectories(root, "runtimes", SearchOption.AllDirectories))
                        foreach (var native in Directory.EnumerateDirectories(sub, "native", SearchOption.AllDirectories))
                            acc.Add(native);
                }
                catch { }
            }
            return acc.ToArray();
        }
        string[] candidates = Candidates();
        if (trace) Console.Error.WriteLine("[trace] Magick resolver: baseDir=" + baseDir + "  candidates=" + string.Join(":", candidates));
        // Every Magick assembly has its own DllImport stubs — register the resolver on
        // ALL of them (Magick.NET, Magick.NET.Core, and any *.NativeInteropGenerator
        // source-generated assemblies).
        int hooked = 0;
        foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
        {
            var n = asm.GetName().Name ?? "";
            if (!n.StartsWith("Magick", StringComparison.OrdinalIgnoreCase)) continue;
            try { NativeLibrary.SetDllImportResolver(asm, MagickResolver); hooked++; if (trace) Console.Error.WriteLine("[trace] Magick resolver hooked on: " + n); }
            catch (Exception ex) { if (trace) Console.Error.WriteLine("[trace] Magick resolver skip " + n + ": " + ex.Message); }
        }
        // Hook every assembly loaded later, too — Magick.NET has multiple.
        AppDomain.CurrentDomain.AssemblyLoad += (_, e) =>
        {
            var n = e.LoadedAssembly.GetName().Name ?? "";
            if (n.StartsWith("Magick", StringComparison.OrdinalIgnoreCase))
                try { NativeLibrary.SetDllImportResolver(e.LoadedAssembly, MagickResolver); if (trace) Console.Error.WriteLine("[trace] Magick resolver hooked on late: " + n); }
                catch { }
        };
        if (trace) Console.Error.WriteLine("[trace] Magick resolver initial hooks: " + hooked);

        IntPtr MagickResolver(string name, Assembly _, DllImportSearchPath? __)
        {
            if (!name.StartsWith("Magick.Native", StringComparison.OrdinalIgnoreCase)) return IntPtr.Zero;
            var probe = Candidates();     // rescan — the runtime extracts natives lazily
            if (trace) Console.Error.WriteLine("[trace] Magick resolver: probing " + probe.Length + " dirs for " + name);
            foreach (var dir in probe)
                foreach (var suffix in new[] { ".dylib", ".dll.dylib", "" })
                {
                    var path = Path.Combine(dir, name + suffix);
                    if (File.Exists(path))
                    {
                        try { var h = NativeLibrary.Load(path); if (trace) Console.Error.WriteLine("[trace] Magick resolver: loaded " + path); return h; }
                        catch (Exception ex) { if (trace) Console.Error.WriteLine("[trace] Magick resolver: dlopen failed " + path + ": " + ex.Message); }
                    }
                }
            if (trace) Console.Error.WriteLine("[trace] Magick resolver: no candidate for " + name);
            return IntPtr.Zero;
        }
    }

        static int CmdBuild(string[] args)
    {
        if (args.Length < 3) return Bad("build needs <src-dir> <out-dir>");
        bool autoFakeSign = true;         // fake-sign raw ELFs in source before build (idempotent)
        bool playGoChunksExplicit = false; // did the user pass --playgo-chunks?
        bool retailNormalize = true;      // auto-upgrade a "standard" retail source to a shape the console launches
        bool regenPlayGo = false;         // force-discard a prepared PlayGo set even if it validates
        bool hdrFlag = true;              // set param.json attribute bit 29 (HDR support) in retail-normalize

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
            // PS5 debug-image loader only accepts the plaintext-no-auth outer PFS
            // (mode 0x000D with the "PPPLAIN-NOAUTH!" seed marker) — a random-seed
            // AES-XTS wrap validates structurally and extract-inner reads it fine,
            // but the console-loader rejects it with CE-100096-6 at launch time
            // (verified on FW 11.60 + kstuff-1.13-dr-test3, retail PS5, 2026-09-24).
            // Sony's Publishing Tools DLL always uses this mode for debug images;
            // a diff of pfs-dump showed the inner PFS is byte-identical to a
            // reference build made with libScePubTools.dll — only the outer PFS
            // wrap differed. Setting PlaintextNoAuth makes our output byte-exact.
            PublisherImageMode = ProsperoPublisherImageMode.PlaintextNoAuth,
            // Sony's publisher packs every launch-time file into PlayGo chunk 0;
            // LibProsperoPkg's default of 64 spreads them out. Both boot, but the
            // 1-chunk layout matches every known-working reference package. Auto-
            // detected from the source's own sce_sys/playgo-chunk.dat later.
            PlayGoChunkCount = 1,
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
                    if (opts.KrakenBackend is ProsperoKrakenBackend.Uncompressed or ProsperoKrakenBackend.Automatic)
                        // Verified on a retail PS5 (FW 11.60, kstuff-lite 1.13): the stored/
                        // automatic path installs and shows its icon but the launch fails with
                        // CE-100096-6. Only the built-in Kraken encoder is console-launchable.
                        Console.Error.WriteLine($"[warn] --kraken-backend {v}: this output is NOT launchable on a console (CE-100096-6 verified); use 'builtin'");
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
                case "--playgo-chunks":
                    // LibProsperoPkg defaults to 64 and spreads files over as many chunks as
                    // there are files; Sony's publisher packs every launch-time file into
                    // chunk 0 (default here). Auto-detected from source's playgo-chunk.dat
                    // when present; this override wins over the auto-detect.
                    if (!int.TryParse(v, out int chunks) || chunks < 1 || chunks > 255)
                        throw new ArgumentException("--playgo-chunks needs an integer 1..255");
                    opts.PlayGoChunkCount = chunks;
                    playGoChunksExplicit = true;
                    i++;
                    break;
                case "--no-fake-sign":
                    // Retail games have raw ELFs that must be fake-signed to boot on a
                    // jailbroken console. Default is ON so a retail-shaped source (with
                    // eboot.bin + fakelib SPRXes) becomes launch-ready without a separate
                    // Sign pass. Use this flag to skip if the source is already fake-signed
                    // and you want to keep bytes identical, or if signing would corrupt an
                    // exotic SELF variant.
                    autoFakeSign = false;
                    break;
                case "--fake-sign":
                    // Explicit opt-in (redundant with the default). Kept for clarity.
                    autoFakeSign = true;
                    break;
                case "--no-retail-normalize":
                    // Retail-normalize replaces placeholder license.dat/info from the source
                    // with a valid debug license issued by LibProsperoPkg (fixes "bad RIF
                    // magic" skip) and flips staged applicationDrmType "standard" to
                    // "upgradable" so LibProsperoPkg writes drm_type=16 into the CNT header
                    // instead of its Application/standard-hardcoded 0 (see 1.1.12 changelog).
                    // Turn OFF for byte-exact re-packs or when packing something already
                    // finalized by Sony's tools.
                    retailNormalize = false;
                    break;
                case "--retail-normalize":
                    retailNormalize = true;
                    break;
                case "--regen-playgo":
                    // Discard the source's sce_sys/playgo-*.dat even when they validate, so
                    // LibProsperoPkg generates a set that matches the inner tree it builds.
                    regenPlayGo = true;
                    break;
                case "--hdr-flag": hdrFlag = true; break;
                case "--no-hdr-flag":
                    // Leave param.json "attribute" exactly as the source has it. With the
                    // console on "HDR when supported" the title then runs in SDR unless the
                    // source already carries bit 29.
                    hdrFlag = false;
                    break;
                default: return Bad("unknown build flag: " + a);
            }
        }
        if (string.IsNullOrWhiteSpace(opts.ContentId)) return Bad("--content-id required");
        if (string.IsNullOrWhiteSpace(opts.TitleId)) return Bad("--title-id required");
        Directory.CreateDirectory(opts.OutputFolder);

        // Stage: mirror the source into a temp folder (hard links, no gigabyte copy) if
        // ANY of these pre-build transforms need to run on the source:
        //   (a) auto-generate sce_sys/*.dds from PNGs the source only provides as PNG
        //       (LibProsperoPkg's builder moves sce_sys/*.dds into the outer CNT but does
        //       NOT generate the DDS itself; without them the .pkg installs but never
        //       launches — see the 1.1.7 CHANGELOG entry);
        //   (b) fake-sign raw ELFs (eboot.bin, *.elf, *.prx, *.sprx) that a retail-shape
        //       source ships unsigned — a jailbroken PS5's app loader rejects a package
        //       whose eboot is not a fake-self and kills the process immediately (short
        //       fan-spin then CE-100096-6). Idempotent — already-signed inputs are skipped.
        // Auto-detect PlayGoChunkCount from the source's own sce_sys/playgo-chunk.dat is a
        // pure metadata read and runs regardless of whether we stage a mirror.
        string effectiveSource = opts.SourceFolder!;
        string? autoStage = null;
        var srcSceSys = Path.Combine(effectiveSource, "sce_sys");

        // -- PlayGoChunkCount auto-detect (unless the user pinned it explicitly) --
        if (!playGoChunksExplicit)
        {
            try
            {
                var pgChunk = Path.Combine(srcSceSys, "playgo-chunk.dat");
                if (File.Exists(pgChunk))
                {
                    var bytes = File.ReadAllBytes(pgChunk);
                    // Layout probed against known good sources (Sony DLL builds):
                    //   0x00  magic 'plgx'
                    //   0x08  u16 attribute count
                    //   0x0A  u16 chunk count  <-- this is what LibProsperoPkg uses
                    if (bytes.Length >= 12 &&
                        bytes[0] == (byte)'p' && bytes[1] == (byte)'l' && bytes[2] == (byte)'g' && bytes[3] == (byte)'x')
                    {
                        int detected = bytes[0x0A] | (bytes[0x0B] << 8);
                        if (detected >= 1 && detected <= 255 && detected != opts.PlayGoChunkCount)
                        {
                            Console.Error.WriteLine($"  [playgo] source declares {detected} chunk(s); using that (was default {opts.PlayGoChunkCount})");
                            opts.PlayGoChunkCount = detected;
                        }
                    }
                }
            }
            catch (Exception ex) { Console.Error.WriteLine($"[warn] could not read source playgo-chunk.dat ({ex.GetType().Name}); using PlayGoChunkCount={opts.PlayGoChunkCount}"); }
        }

        // -- Retail-normalize discovery (read source param.json once) --
        // A "standard"-DRM retail dump needs four things LibProsperoPkg 1.2.0 does not do
        // on its own before a JB PS5 (FW 11.60, kstuff-lite 1.13+) launches the result.
        // All are applied in the staged mirror; the on-disk source is never modified.
        // Verified 2026-09-25 on a retail PS5 with a retail title (A/B builds L vs M):
        //   - drm_type=16 in the CNT header. Upstream hard-codes 0 for any Application
        //     whose applicationDrmType is not "upgradable"; retail packages carry 16.
        //     Fixed upstream of this code by the IL patch in patches/ (no param.json
        //     change needed, so applicationDrmType stays "standard" like Sony's own).
        //   - Valid license.dat/license.info CNT entries (0x0400/0x0401). Dumps ship
        //     placeholder bytes that upstream validates and silently drops; without the
        //     entries the homescreen shows a padlock. We drop the placeholders and hand
        //     upstream a DebugLicenseProvider (its own BuildDebugLicense bytes).
        //   - The retail SELF flavour on eboot.bin and every prx/sprx (ProgramType bit
        //     0x10000000, byte 0x0B of the SELF header). Sony-signed retail SELFs carry
        //     it; older fake-signers used the dev flavour.
        //   - Optional: param.json attribute bit 29 (0x20000000) = HDR support flag.
        //     Build M (bit unset) launched fine but the console stayed in SDR; build L
        //     (bit set) launched in HDR10. Sony's own packages set it for HDR titles.
        //     Switch: --hdr-flag / --no-hdr-flag (default on).
        //   Also injected: a "kernel" block with a retail reference package's values when the source has none.
        //   Verified NOT launch-critical (build M had none and launched); kept because
        //   every Sony retail package carries one and L is the validated configuration.
        // Everything the loader actually rejected the launch on turned out to be the
        // PlayGo prepared set (see the sanity block above) — not any of these.
        bool sourceIsStandardApp = false;
        try
        {
            var srcParam = Path.Combine(srcSceSys, "param.json");
            if (File.Exists(srcParam))
            {
                using var doc = JsonDocument.Parse(File.ReadAllBytes(srcParam));
                if (doc.RootElement.TryGetProperty("applicationDrmType", out var dt))
                    sourceIsStandardApp = string.Equals(dt.GetString(), "standard", StringComparison.OrdinalIgnoreCase);
            }
        }
        catch (Exception ex) { Console.Error.WriteLine($"[warn] could not read source param.json for retail-normalize check ({ex.GetType().Name})"); }
        bool doRetailNormalize = retailNormalize && sourceIsStandardApp;

        try
        {
            // Discover work first (so we can stage exactly once).
            var iconNames = new[] { "icon0.png", "pic0.png", "pic1.png", "pic2.png" };
            var pngsToConvert = Directory.Exists(srcSceSys)
                ? iconNames.Where(n => File.Exists(Path.Combine(srcSceSys, n))
                                       && !File.Exists(Path.Combine(srcSceSys, Path.ChangeExtension(n, ".dds")))).ToArray()
                : Array.Empty<string>();
            var elfsToSign = autoFakeSign ? FindRawElfs(effectiveSource) : Array.Empty<string>();

            // -- PlayGo prepared-set sanity --
            // LibProsperoPkg only COUNTS sce_sys/playgo-{chunk,hash-table,ficm}.dat: 3/3
            // present → "complete prepared set found; its layout will be preserved" and the
            // bytes go verbatim into CNT entries 0x1001/0x2010/0x2011, which ShellCore and
            // the console parses at install/launch. Some containers carry these under the
            // WRONG names (seen in practice: hash-table.dat = param.json text, ficm.dat =
            // the real hash table, origin-param.json = a DDS). Such a package installs
            // and shows its icon but the launch dies with CE-100022-5, while the same
            // content runs from a mounted image because nothing reads those files
            // there. Validate each file against the on-wire format LibProsperoPkg itself
            // emits (ProsperoPlayGo.BuildChunkDat/BuildHashTable/BuildFicm); if any present
            // file fails, drop the whole set from the staged mirror so the builder
            // regenerates a consistent one from the actual inner tree.
            var playgoNames = new[] { "playgo-chunk.dat", "playgo-hash-table.dat", "playgo-ficm.dat" };
            var playgoPresent = Directory.Exists(srcSceSys)
                ? playgoNames.Where(n => File.Exists(Path.Combine(srcSceSys, n))).ToArray()
                : Array.Empty<string>();
            var playgoBad = new List<string>();
            foreach (var n in playgoPresent)
            {
                byte[] b;
                try { b = File.ReadAllBytes(Path.Combine(srcSceSys, n)); } catch { playgoBad.Add(n + " (unreadable)"); continue; }
                bool ok = n switch
                {
                    "playgo-chunk.dat"      => LooksLikePlayGoChunkDat(b),
                    "playgo-hash-table.dat" => LooksLikePlayGoHashTable(b),
                    _                       => LooksLikePlayGoFicm(b),
                };
                if (!ok) playgoBad.Add(n + (LooksLikeJsonText(b) ? " (is JSON text)" : LooksLikePlayGoHashTable(b) ? " (is a hash table)" : " (bad format)"));
            }
            bool discardPlayGo = playgoPresent.Length > 0 && (regenPlayGo || playgoBad.Count > 0);
            if (discardPlayGo)
                Console.Error.WriteLine(regenPlayGo && playgoBad.Count == 0
                    ? $"  [playgo] --regen-playgo: discarding prepared set ({playgoPresent.Length} file(s)); LibProsperoPkg will regenerate {opts.PlayGoChunkCount} chunk(s)"
                    : $"  [playgo] prepared set is CORRUPT — {string.Join(", ", playgoBad)}; discarding all {playgoPresent.Length} file(s) so LibProsperoPkg regenerates a consistent {opts.PlayGoChunkCount}-chunk set");

            // Any sce_sys/*.json that is not JSON is a mislabeled dump artifact (same
            // shifted-name bug); it would land in the inner PFS as junk. Drop it.
            var badJson = Directory.Exists(srcSceSys)
                ? Directory.EnumerateFiles(srcSceSys, "*.json").Where(p =>
                    { try { return !LooksLikeJsonText(File.ReadAllBytes(p)); } catch { return false; } })
                  .Select(Path.GetFileName).ToArray()
                : Array.Empty<string>();
            if (badJson.Length > 0)
                Console.Error.WriteLine($"  [sce_sys] dropping mislabeled non-JSON file(s): {string.Join(", ", badJson)}");

            // sce_sys/keystone is the save-data key material: a package built with a
            // regenerated keystone (different passcode) reports every existing save of
            // the title as corrupt (verified, build N). LibProsperoPkg keeps a present
            // keystone and only generates one when it is missing — say which happened.
            if (File.Exists(Path.Combine(srcSceSys, "keystone")))
                Console.Error.WriteLine("  [keystone] source keystone preserved (save-data key; saves stay compatible with the original dump)");
            else
                Console.Error.WriteLine($"  [keystone] source has no sce_sys/keystone — the builder generates one for passcode {opts.Passcode}; saves from other builds of this title will NOT be readable");

            bool needStage = pngsToConvert.Length > 0 || elfsToSign.Length > 0 || doRetailNormalize || discardPlayGo || badJson.Length > 0;

            if (needStage)
            {
                autoStage = Path.Combine(
                    string.IsNullOrEmpty(opts.TemporaryDirectory) ? Path.GetTempPath() : opts.TemporaryDirectory!,
                    "ffpfsc-stage-" + Guid.NewGuid().ToString("N").Substring(0, 8));
                Directory.CreateDirectory(autoStage);
                MirrorAsHardLinks(effectiveSource, autoStage);
                Console.Error.WriteLine($"  [stage] mirrored source into {autoStage}");

                // (0) Drop corrupt/mislabeled sce_sys metadata from the mirror (unlinks the
                //     hardlink; the on-disk source is untouched).
                if (discardPlayGo || badJson.Length > 0)
                {
                    var stagedSceSys0 = Path.Combine(autoStage, "sce_sys");
                    foreach (var n in (discardPlayGo ? playgoPresent : Array.Empty<string>()).Concat(badJson))
                    {
                        var p = Path.Combine(stagedSceSys0, n);
                        try { if (File.Exists(p) || IsSymlink(p)) File.Delete(p); } catch (Exception ex) { Console.Error.WriteLine($"  [stage] could not drop sce_sys/{n}: {ex.GetType().Name}"); }
                    }
                }

                // (a) DDS icon CNT entries
                if (pngsToConvert.Length > 0)
                {
                    var stagedSceSys = Path.Combine(autoStage, "sce_sys");
                    Directory.CreateDirectory(stagedSceSys);
                    foreach (var name in pngsToConvert)
                    {
                        var png = File.ReadAllBytes(Path.Combine(srcSceSys, name));
                        var dds = ProsperoDdsEncoder.EncodePngToDds(png, opts.TemporaryDirectory ?? Path.GetTempPath());
                        var ddsPath = Path.Combine(stagedSceSys, Path.ChangeExtension(name, ".dds"));
                        if (File.Exists(ddsPath) || IsSymlink(ddsPath)) File.Delete(ddsPath);
                        File.WriteAllBytes(ddsPath, dds);
                        Console.Error.WriteLine($"  [icon] generated sce_sys/{Path.ChangeExtension(name, ".dds")} ({dds.Length:N0} B) from {name}");
                    }
                }

                // (b) Fake-sign raw ELFs. LibProsperoPkg's own MakeFself does the byte-
                // faithful conversion; skips SELFs and non-ELFs.
                if (elfsToSign.Length > 0)
                {
                    int signed = 0, skipped = 0;
                    foreach (var relPath in elfsToSign)
                    {
                        var srcFile = Path.Combine(effectiveSource, relPath);
                        var stagedFile = Path.Combine(autoStage, relPath);
                        try
                        {
                            var bytes = File.ReadAllBytes(srcFile);
                            if (!ProsperoFself.IsElf(bytes) || ProsperoFself.IsSelf(bytes)) { skipped++; continue; }
                            var fself = ProsperoFself.MakeFself(bytes, new FselfOptions());
                            // The staged file is a hardlink to the source — unlink it and
                            // write the new bytes into the staged path so the source stays untouched.
                            if (File.Exists(stagedFile) || IsSymlink(stagedFile)) File.Delete(stagedFile);
                            Directory.CreateDirectory(Path.GetDirectoryName(stagedFile)!);
                            File.WriteAllBytes(stagedFile, fself);
                            signed++;
                        }
                        catch (Exception ex)
                        {
                            skipped++;
                            Console.Error.WriteLine($"  [sign] skipped {relPath}: {ex.GetType().Name}: {ex.Message}");
                        }
                    }
                    Console.Error.WriteLine($"  [sign] fake-signed {signed} ELF(s); skipped {skipped} (already-SELF, non-ELF, or errors)");
                }

                // (c) Retail-normalize: two moves to make a "standard"-DRM retail dump
                // produce a package that a JB PS5 with kstuff-lite 1.13+ launches.
                //   (c.i) Drop placeholder license.dat/info from the staged mirror so
                //         LibProsperoPkg's per-file iterator does not warn+skip on them.
                //         The unlink is against the hardlink; source stays untouched.
                //   (c.ii) Install a LicenseProvider that returns a valid debug license
                //         for this contentId. LibProsperoPkg calls GetLicense() during
                //         CollectMediaEntries and yields the returned bytes as CNT
                //         entries 0x0400 (license.dat) and 0x0401 (license.info) — kills
                //         the "License missing" padlock on the PS5 homescreen.
                // drm_type=16 in the CNT header is handled UPSTREAM by our IL-patched
                // LibProsperoPkg.dll (patches/README.md documents the one-byte hex patch
                // that flips the hardcoded 0-for-Application ternary to always-16). This
                // lets us keep applicationDrmType="standard" verbatim in the embedded
                // param.json — flipping it to "upgradable" inside the pkg breaks games
                // whose source metadata declares an upgrade chain (originContentVersion,
                // targetContentVersion) because LibProsperoPkg strips those fields on
                // build → the console sees an "upgradable" app without a target and
                // refuses to launch with CE-100022-5.
                if (doRetailNormalize)
                {
                    var stagedSceSys = Path.Combine(autoStage, "sce_sys");
                    Directory.CreateDirectory(stagedSceSys);
                    foreach (var badFile in new[] { "license.dat", "license.info" })
                    {
                        var p = Path.Combine(stagedSceSys, badFile);
                        try { if (File.Exists(p) || IsSymlink(p)) File.Delete(p); } catch { }
                    }
                    opts.LicenseProvider = new DebugLicenseProvider(opts.ContentId);

                    // (c.iii-a) Patch SELF headers of eboot.bin and every prx to the retail
                    //           SELF pattern Sony's own toolchain stamps. Diffed the SELF
                    //           header (first 16 bytes) between a retail reference package's
                    //           eboot + libc.prx (launches on FW 11.60) and a source whose
                    //           executables were fake-signed by an older tool (fails with
                    //           CE-100022-5 post-icon):
                    //             retail:  ver=0x0110  ProgramType=0x10000101  info=0x05100530
                    //             source:  ver=0x0100  ProgramType=0x00000101  info=0x05100560
                    //           The ProgramType bit 0x10000000 is the "retail application"
                    //           flag the console loader gates launch on. Older fake-signers
                    //           stamp dev-flavor headers (0x00000101). We patch to retail
                    //           flavor in place — the fake signature is not verified on the
                    //           console anyway, and the patched bytes stay within the
                    //           SELF-header range the loader reads for its retail check.
                    //           sce_sys/about/right.sprx keeps the dev pattern: retail
                    //           packages have ProgramType=0x00000101 on that one file too —
                    //           it's a metadata SPRX that never executes.
                    // Surgical patch: only flip byte 0x0B (dev 0x00 → retail 0x10) to set
                    // ProgramType bit 0x10000000. Leaves HeaderSize/MetaSize + all other SELF
                    // header offsets untouched so the loader's parser reads exactly the
                    // structure the file actually has (patching version + info_field earlier
                    // caused HeaderSize/MetaSize to reparse to smaller values, which put the
                    // SDK-version record past the new-declared metadata boundary and confused
                    // the loader).
                    int selfPatched = 0, selfLeft = 0;
                    foreach (var relPath in Directory.EnumerateFiles(autoStage, "*", SearchOption.AllDirectories))
                    {
                        var name = Path.GetFileName(relPath);
                        var ext = Path.GetExtension(relPath).ToLowerInvariant();
                        var relFromRoot = Path.GetRelativePath(autoStage, relPath).Replace('\\','/');
                        bool isLoadable = name == "eboot.bin" || ext == ".prx" || ext == ".sprx";
                        if (!isLoadable) continue;
                        if (relFromRoot.StartsWith("sce_sys/about/", StringComparison.OrdinalIgnoreCase)) continue;
                        try
                        {
                            byte[] head = new byte[16];
                            using (var fs = new FileStream(relPath, FileMode.Open, FileAccess.Read))
                            {
                                int n = fs.Read(head, 0, 16);
                                if (n < 16 || head[0] != 0x54 || head[1] != 0x14 || head[2] != 0xF5 || head[3] != 0xEE)
                                { selfLeft++; continue; }
                            }
                            if (head[0x0B] == 0x10) { selfLeft++; continue; }
                            var backup = relPath + ".pre-retail";
                            File.Copy(relPath, backup, overwrite: true);
                            File.Delete(relPath);
                            File.Move(backup, relPath);
                            using (var fs = new FileStream(relPath, FileMode.Open, FileAccess.Write))
                            {
                                fs.Seek(0x0B, SeekOrigin.Begin);
                                fs.WriteByte(0x10);
                            }
                            selfPatched++;
                        }
                        catch (Exception ex) { Console.Error.WriteLine($"  [self-hdr] skipped {relFromRoot}: {ex.GetType().Name}: {ex.Message}"); selfLeft++; }
                    }
                    Console.Error.WriteLine($"  [self-hdr] set retail bit (byte 0x0B: 0x10) on {selfPatched} eboot.bin/*.prx; left {selfLeft} untouched");

                    // (c.iii) Rewrite staged param.json:
                    //   (a) attribute |= 0x20000000 when --hdr-flag (default). Bit 29 is the
                    //       HDR support flag: with the console on "HDR when supported", the
                    //       build without it (M) ran in SDR, the build with it (L) in HDR10.
                    //       Retail reference packages carry it; some sources ship 0.
                    //   (b) a "kernel" block (a retail reference package's cpu/gpu page-table + flexible-memory
                    //       sizes) when the source has none. Not launch-critical (M had none
                    //       and launched), kept to match Sony retail packages and the
                    //       validated L configuration. A source's own block always wins.
                    var stagedParam = Path.Combine(stagedSceSys, "param.json");
                    var srcParam = Path.Combine(srcSceSys, "param.json");
                    if (File.Exists(srcParam))
                    {
                        try
                        {
                            using var pdoc = JsonDocument.Parse(File.ReadAllBytes(srcParam));
                            long currentAttr = 0;
                            if (pdoc.RootElement.TryGetProperty("attribute", out var at)) currentAttr = at.GetInt64();
                            long newAttr = hdrFlag ? (currentAttr | 0x20000000L) : currentAttr;
                            bool hasKernel = pdoc.RootElement.TryGetProperty("kernel", out _);
                            bool changed = (newAttr != currentAttr) || !hasKernel;
                            if (changed)
                            {
                                var buf = new System.IO.MemoryStream();
                                using (var w = new Utf8JsonWriter(buf, new JsonWriterOptions { Indented = true }))
                                {
                                    w.WriteStartObject();
                                    bool kernelWritten = false;
                                    foreach (var prop in pdoc.RootElement.EnumerateObject())
                                    {
                                        // Keep source order; write kernel just before "localizedParameters"
                                        // (typical Sony ordering) if missing.
                                        if (!hasKernel && !kernelWritten && prop.Name == "localizedParameters")
                                        {
                                            w.WriteStartObject("kernel");
                                            w.WriteNumber("cpuPageTableSize", 67108864);       // 64 MiB
                                            w.WriteNumber("flexibleMemorySize", 272629760);    // ~260 MiB (retail reference value)
                                            w.WriteNumber("gpuPageTableSize", 67108864);       // 64 MiB
                                            w.WriteEndObject();
                                            kernelWritten = true;
                                        }
                                        if (prop.Name == "attribute")
                                            w.WriteNumber("attribute", newAttr);
                                        else
                                            prop.WriteTo(w);
                                    }
                                    // Fallback: if we didn't find localizedParameters, append kernel at end
                                    if (!hasKernel && !kernelWritten)
                                    {
                                        w.WriteStartObject("kernel");
                                        w.WriteNumber("cpuPageTableSize", 67108864);
                                        w.WriteNumber("flexibleMemorySize", 272629760);
                                        w.WriteNumber("gpuPageTableSize", 67108864);
                                        w.WriteEndObject();
                                    }
                                    w.WriteEndObject();
                                }
                                if (File.Exists(stagedParam) || IsSymlink(stagedParam)) File.Delete(stagedParam);
                                File.WriteAllBytes(stagedParam, buf.ToArray());
                                var kernelNote = hasKernel ? "" : ", added kernel{cpuPageTable=64Mi, flex=260Mi, gpuPageTable=64Mi}";
                                var attrNote = newAttr != currentAttr ? $"set param.json attribute 0x{currentAttr:X8} -> 0x{newAttr:X8} (HDR support flag, bit 29)" : $"param.json attribute 0x{currentAttr:X8} kept";
                                Console.Error.WriteLine($"  [retail] {attrNote}{kernelNote}");
                            }
                        }
                        catch (Exception ex) { Console.Error.WriteLine($"[warn] retail-normalize: param.json rewrite failed ({ex.GetType().Name})"); }
                    }
                    Console.Error.WriteLine("  [retail] dropped placeholder license.dat/info; installed DebugLicenseProvider (CNT entries 0x0400/0x0401); drm_type=16 via patched LibProsperoPkg.dll");
                }

                opts.SourceFolder = autoStage;
            }
        }
        catch (Exception ex)
        {
            var chain = ex.GetType().Name + ": " + ex.Message;
            for (var e = ex.InnerException; e != null; e = e.InnerException)
                chain += "  <- " + e.GetType().Name + ": " + e.Message;
            Console.Error.WriteLine("[warn] source auto-stage failed (" + chain + "). The .pkg will be built from the source as-is, which may lack DDS CNT entries and/or unsigned executables.");
            if (Environment.GetEnvironmentVariable("PKG_TOOL_TRACE") == "1")
                Console.Error.WriteLine(ex.ToString());
        }

        Console.Error.WriteLine($"[info] build  {opts.SourceFolder} -> {opts.OutputFolder}  (inner={opts.InnerCompression}, backend={opts.KrakenBackend}, level={opts.KrakenCompressionLevel}, temp={opts.TemporaryDirectory ?? "$TMPDIR"}, chunks={opts.PlayGoChunkCount}, fake-sign={autoFakeSign}, retail-normalize={doRetailNormalize})");
        ProsperoBuildResult result;
        try { result = ProsperoPackageBuilder.Build(opts, logger: s => Console.Error.WriteLine("  " + s)); }
        finally { if (autoStage != null && Directory.Exists(autoStage)) { try { Directory.Delete(autoStage, recursive: true); } catch { } } }
        Console.WriteLine($"OK — wrote {result.OutputPath} ({new FileInfo(result.OutputPath).Length:N0} B)");
        if (result.Warnings != null)
            foreach (var w in result.Warnings) Console.Error.WriteLine("[warn] " + w);
        return 0;
    }

    /// <summary>LicenseProvider that issues a valid debug license.dat/info pair for a given
    /// content-id. Wraps LibProsperoPkg.PKG.ProsperoSystemFiles.BuildDebugLicense — the exact
    /// generator LibProsperoPkg itself uses at PackageBuilder.cs when a "standard" Application
    /// source has no license files. We hand it back for the "upgradable" trick path where
    /// the source-file iterator would otherwise emit nothing, so the console gets
    /// well-formed CNT entries 0x0400 (license.dat) + 0x0401 (license.info).</summary>
    sealed class DebugLicenseProvider : IProsperoLicenseProvider
    {
        private readonly string _contentId;
        public DebugLicenseProvider(string contentId) { _contentId = contentId; }
        public ProsperoLicenseArtifacts GetLicense(ProsperoLicenseRequest request)
        {
            var key = request.EntitlementKey ?? Array.Empty<byte>();
            return ProsperoSystemFiles.BuildDebugLicense(request.VolumeType, _contentId, key);
        }
    }

    // PlayGo on-wire formats, mirrored from LibProsperoPkg.ProsperoPlayGo (which is what
    // Sony's publisher emits too — verified against an untouched retail package).
    static uint U32(byte[] b, int o) => (uint)(b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24));

    /// <summary>playgo-chunk.dat: "plgx" magic, u32 total size at 0x10 equals the length.</summary>
    static bool LooksLikePlayGoChunkDat(byte[] b)
        => b.Length >= 0x40 && b[0] == (byte)'p' && b[1] == (byte)'l' && b[2] == (byte)'g' && b[3] == (byte)'x'
           && U32(b, 0x10) == (uint)b.Length;

    /// <summary>playgo-hash-table.dat: u32 1, u32 0x08000000, u32 56 (header), u32 table
    /// size, "\x7fFLT" at 0x18; 56 + table size equals the length.</summary>
    static bool LooksLikePlayGoHashTable(byte[] b)
        => b.Length >= 56 && U32(b, 0) == 1u && U32(b, 4) == 0x08000000u && U32(b, 8) == 56u
           && b[24] == 0x7F && b[25] == (byte)'F' && b[26] == (byte)'L' && b[27] == (byte)'T'
           && 56u + U32(b, 12) == (uint)b.Length;

    /// <summary>playgo-ficm.dat: u32 1, u32 16 (header) at 0x08, u32 payload size at 0x0C;
    /// 16 + payload equals the length.</summary>
    static bool LooksLikePlayGoFicm(byte[] b)
        => b.Length >= 16 && U32(b, 0) == 1u && U32(b, 8) == 16u && 16u + U32(b, 12) == (uint)b.Length;

    static bool LooksLikeJsonText(byte[] b)
    {
        try { using var d = JsonDocument.Parse(b); return d.RootElement.ValueKind == JsonValueKind.Object || d.RootElement.ValueKind == JsonValueKind.Array; }
        catch { return false; }
    }

    /// <summary>Return relative paths (from *root*) of every regular file whose first bytes
    /// look like a raw ELF (magic 0x7F 'E' 'L' 'F'). Only files with plausible extensions
    /// (eboot.bin, .elf, .prx, .sprx) are checked so a huge blob is not sniffed unnecessarily.</summary>
    static string[] FindRawElfs(string root)
    {
        var result = new List<string>();
        var head = new byte[4];
        foreach (var f in Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories))
        {
            var name = Path.GetFileName(f);
            var ext = Path.GetExtension(f).ToLowerInvariant();
            if (name != "eboot.bin" && ext != ".elf" && ext != ".prx" && ext != ".sprx") continue;
            try
            {
                using var fs = File.OpenRead(f);
                if (fs.Read(head, 0, 4) != 4) continue;
                if (head[0] == 0x7F && head[1] == (byte)'E' && head[2] == (byte)'L' && head[3] == (byte)'F')
                    result.Add(Path.GetRelativePath(root, f));
            }
            catch { }
        }
        return result.ToArray();
    }
}

[System.Text.Json.Serialization.JsonSourceGenerationOptions(WriteIndented = false)]
[System.Text.Json.Serialization.JsonSerializable(typeof(Program.ListInnerDoc))]
[System.Text.Json.Serialization.JsonSerializable(typeof(Program.MembersResultDoc))]
[System.Text.Json.Serialization.JsonSerializable(typeof(Program.InspectDoc))]
[System.Text.Json.Serialization.JsonSerializable(typeof(Program.ExtractResultDoc))]
[System.Text.Json.Serialization.JsonSerializable(typeof(Program.ValidateReportDoc))]
internal sealed partial class PkgToolJsonContext : System.Text.Json.Serialization.JsonSerializerContext
{
    static PkgToolJsonContext? _indented;
    /// <summary>Same contract, pretty-printed — for the human-facing --json outputs.</summary>
    public static PkgToolJsonContext Indented => _indented ??= new PkgToolJsonContext(new JsonSerializerOptions { WriteIndented = true });
}
