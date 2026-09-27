using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading;
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
    const string ToolVersion = "1.1.16";

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
                "ampr-index" => CmdAmprIndex(args),
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
        Console.WriteLine("  ampr-index    <folder>          (re)write <folder>/ampr_emu.index (AMPRIDX3) over its files");
        Console.WriteLine("      One JSON object on stdout: the /app0 tree (files with logical sizes, every");
        Console.WriteLine("      directory, CNT-lifted sce_sys metadata tagged \"source\":\"cnt\") — read via");
        Console.WriteLine("      random access, the inner image is NOT decoded as a whole.");
        Console.WriteLine("  extract-inner <pkg> <out-dir> [--passcode P]     [--json]");
        Console.WriteLine("  extract-inner <pkg> <out-dir> --members <file> [--passcode P] [--json]");
        Console.WriteLine("      Selective: <file> lists one path per line (relative to /app0); a directory");
        Console.WriteLine("      means its whole subtree incl. empty folders. Prints '[####] NN% extract (path)'.");
        Console.WriteLine("  extract-outer <pkg> <out-dir> [--passcode P]     [--decompress|--no-decompress]");
        Console.WriteLine("  validate      <pkg> [--passcode P]               [--json]");
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
        Console.WriteLine("      --playgo-chunks <1..64>      PlayGo chunk count (auto-detected from source sce_sys/playgo-chunk.dat)");
        Console.WriteLine("      --parallelism <n> / -j <n>   accepted for compatibility; LibProsperoPkg 1.2.0's encoder is not");
        Console.WriteLine("                                   thread-safe, so every build runs single-threaded (a warning says so)");
        Console.WriteLine("      --fake-sign / --no-fake-sign fake-sign raw ELFs in source before packing (default ON; idempotent)");
        Console.WriteLine("      --no-ampr-index              keep the source's ampr_emu.index as it is (default: when fakelib/");
        Console.WriteLine("                                   libSceAmpr.sprx is shipped, rebuild the index over the packed files)");
        Console.WriteLine("      --regen-playgo               discard source sce_sys/playgo-*.dat and let the builder regenerate them");
        Console.WriteLine("                                   (a CORRUPT prepared set — wrong format, e.g. JSON under hash-table.dat — is");
        Console.WriteLine("                                   always discarded automatically; this forces it for valid-looking sets too)");
        Console.WriteLine("      --hdr-flag auto|on|off       param.json attribute bit 29 (HDR support). auto = keep what the source");
        Console.WriteLine("                                   declares (default; that is the publisher's intent), on = set it, off = clear");
        Console.WriteLine("                                   it. A console on \"HDR when supported\" switches output modes on this bit.");
        Console.WriteLine("      --retail-normalize / --no-retail-normalize");
        Console.WriteLine("                                   auto-upgrade a \"standard\" retail source (default ON):");
        Console.WriteLine("                                     staged param.json standard -> upgradable (drm_type=16),");
        Console.WriteLine("                                     replace placeholder license.dat/info with a valid debug license,");
        Console.WriteLine("                                     add CNT entries 0x0400/0x0401 via IProsperoLicenseProvider");
        Console.WriteLine("");
        Console.WriteLine("  Default passcode 32 x '0'. Default output is a finalized debug image.");
        Console.WriteLine("  Every build works on a hard-link mirror of the source (next to the source folder when");
        Console.WriteLine("  that is writable, else in --temp); the source itself is never modified. Auto-fake-sign");
        Console.WriteLine("  scans for raw ELF magic (0x7F454C46) in eboot.bin, *.elf, *.prx, *.sprx and rewrites");
        Console.WriteLine("  them as SCE fake-selves in that mirror. SIGTERM/SIGINT cancel the build and remove the");
        Console.WriteLine("  mirror, the library's temp files and any partial .pkg (exit 143/130).");
    }

    static int Bad(string m) { Console.Error.WriteLine("[usage] " + m); PrintUsage(); return 2; }

    /// <summary>The value of the option at args[i]; throws when the option is the last argument.
    /// Every value-taking option of every command goes through here so "--members" without a
    /// file cannot fall through to a full extract and "--mode" alone cannot NRE.</summary>
    static int CmdAmprIndex(string[] args)
    {
        if (args.Length != 2 || !Directory.Exists(args[1])) return Bad("usage: ampr-index <folder>");
        int rows = AmprIndex.Write(args[1]);
        Console.WriteLine(rows > 0 ? $"OK — wrote {AmprIndex.FileName} ({rows:N0} file(s))" : "nothing to index");
        return 0;
    }

    static string Need(string[] args, ref int i, string name)
    {
        if (i + 1 >= args.Length) throw new ArgumentException(name + " needs a value");
        return args[++i];
    }

    static int CmdVersion()
    {
        var asm = typeof(ProsperoPkgReader).Assembly;
        var name = asm.GetName();
        Console.WriteLine("ffpfsc-pkg-tool " + ToolVersion);
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
        bool json = false;
        for (int i = 2; i < args.Length; i++)
        {
            if (args[i] == "--json") json = true;
            else return Bad("unknown inspect flag: " + args[i]);
        }
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
            switch (args[i])
            {
                case "--passcode": passcode = Need(args, ref i, "--passcode"); break;
                case "--decompress": decompress = true; break;
                case "--no-decompress": decompress = false; break;
                case "--json": json = true; break;
                case "--no-merge-cnt": mergeCnt = false; break;
                case "--members": membersFile = Need(args, ref i, "--members"); break;
                default: return Bad($"unknown extract-{(inner ? "inner" : "outer")} flag: {args[i]}");
            }
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

    // Every --json document goes through the source-generated serializer (PkgToolJsonContext
    // below). It was introduced while the binary was still published trimmed (which switches
    // reflection-based System.Text.Json off); PublishTrimmed is false today, the context is kept
    // because it works regardless of the publish settings. The classes mirror the anonymous
    // types the commands used before: same field names, same number/bool/null shapes.
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
        {
            if (args[i] == "--passcode") passcode = Need(args, ref i, "--passcode");
            else return Bad("unknown list-inner flag: " + args[i]);
        }

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
                                        $"{img.RangeCalls} range decodes / {img.RangeBytes:N0} B, span decoder bound={NapsBlockReader.UsesSpanDecoder}");
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
        bool json = false;
        string passcode = new string('0', 32);
        for (int i = 2; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--json": json = true; break;
                case "--passcode": passcode = Need(args, ref i, "--passcode"); break;
                default: return Bad("unknown validate flag: " + args[i]);
            }
        }
        if (!File.Exists(pkg)) throw new FileNotFoundException("package not found", pkg);
        var checks = new System.Collections.Generic.List<ValidateResult>();
        void Ok(string k, string msg) => checks.Add(new ValidateResult { Check = k, Level = "pass", Message = msg });
        void Warn(string k, string msg) => checks.Add(new ValidateResult { Check = k, Level = "warn", Message = msg });
        void Fail(string k, string msg) => checks.Add(new ValidateResult { Check = k, Level = "fail", Message = msg });

        long sz = new FileInfo(pkg).Length;
        // A finalized image starts with a 64 KiB FIH block, so anything smaller cannot be one.
        if (sz >= 0x10000) Ok("file", $"{sz:N0} bytes on disk");
        else               Fail("file", $"{sz:N0} bytes on disk — smaller than one 64 KiB image block");

        // Magic
        using (var fs = new FileStream(pkg, FileMode.Open, FileAccess.Read, FileShare.Read))
        {
            var m = new byte[4]; int got = fs.Read(m, 0, 4);
            if (got < 4) { Fail("magic", "file shorter than 4 bytes"); Report(checks, json); return 1; }
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
                ProsperoPackageArchive.ExtractCntEntries(pkg, tmpCnt, passcode, includeEncrypted: true));
            var need = new[] { "param.json", "icon0.png" };
            foreach (var n in need)
            {
                var p = Path.Combine(tmpCnt, n);
                if (File.Exists(p)) Ok("cnt." + n, $"{new FileInfo(p).Length:N0} bytes present");
                else                Fail("cnt." + n, "MISSING from CNT — installer will reject");
            }
            // PlayGo prepared set: presence is not enough — a set under the wrong names (JSON text
            // as hash-table.dat, a hash table as ficm.dat) installs but dies at launch with
            // CE-100022-5. Check the on-wire format with the same helpers the build uses.
            foreach (var n in new[] { "playgo-chunk.dat", "playgo-hash-table.dat", "playgo-ficm.dat" })
            {
                var p = Path.Combine(tmpCnt, n);
                if (!File.Exists(p))
                {
                    if (n == "playgo-chunk.dat") Fail("cnt." + n, "MISSING from CNT — installer will reject");
                    else                         Warn("cnt." + n, "absent from CNT (the builder normally emits it)");
                    continue;
                }
                var b = File.ReadAllBytes(p);
                bool ok = n switch
                {
                    "playgo-chunk.dat"      => LooksLikePlayGoChunkDat(b),
                    "playgo-hash-table.dat" => LooksLikePlayGoHashTable(b),
                    _                       => LooksLikePlayGoFicm(b),
                };
                if (ok) Ok("cnt." + n, $"{b.Length:N0} bytes, on-wire format OK");
                else    Fail("cnt." + n, $"{b.Length:N0} bytes but NOT the {n} format" + (LooksLikeJsonText(b) ? " (it is JSON text)" : LooksLikePlayGoHashTable(b) ? " (it is a hash table)" : "") + " — launch will fail (CE-100022-5)");
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
                    else Fail("param.contentId", "param.json has no contentId");
                    // The title id is characters 7..15 of the content id (XX0000-TTTTNNNNN_00-...).
                    string headerTid = h.ContentId.Length >= 16 ? h.ContentId.Substring(7, 9) : "";
                    if (root.TryGetProperty("titleId", out var tid))
                    {
                        var v = tid.GetString() ?? "";
                        if (v == headerTid) Ok("param.titleId", $"{v} matches the content id");
                        else Fail("param.titleId", $"'{v}' != content id title '{headerTid}'");
                    }
                    else Fail("param.titleId", "param.json has no titleId");
                    if (root.TryGetProperty("applicationDrmType", out var drm))
                    {
                        var v = drm.GetString() ?? "";
                        if (v is "free" or "standard" or "upgradable") Ok("param.applicationDrmType", $"{v} (header drm_type=0x{h.DrmType:X})");
                        else Warn("param.applicationDrmType", $"'{v}' is not free/standard/upgradable (header drm_type=0x{h.DrmType:X})");
                    }
                    else Warn("param.applicationDrmType", "absent");
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
            var innerFiles = ProsperoPackageArchive.ExtractInnerFiles(pkg, tmpInner, passcode, decompressFiles: true);
            Ok("inner.pfs", $"decoded {innerFiles.Count} file(s) from inner PFS");
            var ebootPath = Path.Combine(tmpInner, "eboot.bin");
            if (File.Exists(ebootPath))
            {
                var head = new byte[4]; int got = 0; using (var f = File.OpenRead(ebootPath)) got = f.Read(head, 0, 4);
                uint magic = got < 4 ? 0u : (uint)(head[0] | (head[1] << 8) | (head[2] << 16) | (head[3] << 24));
                // 0xEEF51454 ("SCE" fake-self) is what LibProsperoPkg's MakeFself and Sony's SELFs
                // carry; 0x1D3D154F is the PS4 SELF magic and does not load on a PS5.
                if      (magic == 0xEEF51454u) Ok("eboot.magic", "SCE fake-self (0xEEF51454) — good");
                else if (magic == 0x1D3D154Fu) Fail("eboot.magic", "PS4 SELF magic (0x1D3D154F) — not a PS5 executable");
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

    /// <summary>Where the build's mirror of the source goes and whether it can be made of hard
    /// links. Hard links only work on the volume the source lives on, so the preferred place is
    /// a hidden directory next to the source folder (<c>&lt;parent&gt;/.ffpfsc-stage-&lt;id&gt;</c>);
    /// --temp is tried second (it may be the same volume). Each candidate is proven with one
    /// real <c>link()</c> of a source file — EXDEV (18) means another volume, EPERM (1) or
    /// ENOTSUP (45) a file system without hard links (exFAT/FAT). Only when no candidate
    /// takes a hard link does the mirror become a copy into --temp, and the caller says so
    /// before copying; the old code fell back to File.Copy per file silently, which turned a
    /// cross-volume build into an unannounced full copy of the game.</summary>
    static (string dir, bool hardLinks, string copyReason) ChooseStageDir(string source, string temp)
    {
        string id = Guid.NewGuid().ToString("N").Substring(0, 8);
        string sourceFull = Path.TrimEndingDirectorySeparator(Path.GetFullPath(source));
        string tempFull = Path.GetFullPath(temp);
        string? parent = Path.GetDirectoryName(sourceFull);
        string? probeSource = null;
        try { probeSource = Directory.EnumerateFiles(sourceFull, "*", SearchOption.AllDirectories).FirstOrDefault(f => !IsSymlink(f)); } catch { }
        bool parentWritable = false, parentSameVolume = false, noHardLinkSupport = false;

        var candidates = new List<(string dir, bool nextToSource)>();
        if (parent != null) candidates.Add((Path.Combine(parent, ".ffpfsc-stage-" + id), true));
        candidates.Add((Path.Combine(tempFull, "ffpfsc-stage-" + id), false));
        foreach (var (cand, nextToSource) in candidates)
        {
            // Never inside the source: the mirror would contain itself (the old --temp-inside-
            // source run recursed until PathTooLongException) and the library refuses it anyway.
            if (IsInside(cand, sourceFull)) continue;
            try { Directory.CreateDirectory(cand); } catch { continue; }
            if (nextToSource) parentWritable = true;
            if (probeSource == null) return (cand, true, "");   // nothing to link
            var probe = Path.Combine(cand, ".hardlink-probe");
            int rc = link(probeSource, probe);
            int errno = rc == 0 ? 0 : Marshal.GetLastPInvokeError();
            if (rc == 0)
            {
                try { File.Delete(probe); } catch { }
                return (cand, true, "");
            }
            try { Directory.Delete(cand, recursive: true); } catch { }
            if (errno == 1 || errno == 45) noHardLinkSupport = true;            // EPERM / ENOTSUP
            if (nextToSource && errno != 18) parentSameVolume = true;           // anything but EXDEV
        }
        string dir = Path.Combine(tempFull, "ffpfsc-stage-" + id);
        Directory.CreateDirectory(dir);
        string reason = noHardLinkSupport ? "source file system does not support hard links"
                      : !parentWritable   ? "source volume is read-only"
                      : parentSameVolume  ? "hard links refused on the source volume"
                                          : "no writable location on the source volume";
        return (dir, false, reason);
    }

    static bool IsInside(string path, string root)
    {
        var p = Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));
        var r = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        return p.Equals(r, StringComparison.Ordinal) || p.StartsWith(r + Path.DirectorySeparatorChar, StringComparison.Ordinal);
    }

    /// <summary>Mirror *src* into *dst*: real subdirectories and, per regular file, a hard link
    /// (or a copy when <paramref name="hardLinks"/> is false). Hard links, not symlinks —
    /// LibProsperoPkg stats the source path and expects a real file layout; some readers get
    /// the size right but read partial data through symlinks (verified: "ended after 6 of 84
    /// bytes"). A hard link that fails for one file (EMLINK and the like) is copied with a
    /// visible line, never silently.</summary>
    static void MirrorSource(string src, string dst, bool hardLinks)
    {
        foreach (var d in Directory.EnumerateDirectories(src, "*", SearchOption.AllDirectories))
            Directory.CreateDirectory(Path.Combine(dst, Path.GetRelativePath(src, d)));
        foreach (var f in Directory.EnumerateFiles(src, "*", SearchOption.AllDirectories))
        {
            var target = Path.Combine(dst, Path.GetRelativePath(src, f));
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            if (File.Exists(target) || IsSymlink(target)) File.Delete(target);
            if (hardLinks)
            {
                var rc = link(Path.GetFullPath(f), target);
                if (rc == 0) continue;
                int errno = Marshal.GetLastPInvokeError();
                Console.Error.WriteLine($"  [stage] link() failed for {Path.GetRelativePath(src, f)} (errno {errno}); copying that file");
            }
            File.Copy(f, target, overwrite: true);
        }
    }

    static long DirectorySize(string root)
    {
        long total = 0;
        try { foreach (var f in Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories)) { try { total += new FileInfo(f).Length; } catch { } } } catch { }
        return total;
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
        // Where a self-contained single-file app's natives can be (IncludeNativeLibrariesFor
        // SelfExtract=true): next to the exe, or extracted under
        // $DOTNET_BUNDLE_EXTRACT_BASE_DIR (default ~/.net)/<AppName>/<bundle hash>/runtimes/<RID>/native/.
        // The probe is limited to exactly that layout — one level of bundle hashes, one level
        // of RIDs — and never scans $TMPDIR: an earlier version enumerated the whole temp tree
        // (5.6 s with 200k directories, and the GUI points TMPDIR at the game temp drive) and
        // would have loaded any matching dylib it found there.
        var appName = Assembly.GetEntryAssembly()?.GetName().Name ?? "ffpfsc-pkg-tool";
        var extRoot = Environment.GetEnvironmentVariable("DOTNET_BUNDLE_EXTRACT_BASE_DIR")
                      ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".net");
        var extApp = Path.Combine(extRoot, appName);
        string[] Candidates()
        {
            var acc = new List<string>();
            void AddNativeDirs(string root)
            {
                var runtimes = Path.Combine(root, "runtimes");
                if (!Directory.Exists(runtimes)) return;
                try
                {
                    foreach (var rid in Directory.EnumerateDirectories(runtimes))
                    {
                        var native = Path.Combine(rid, "native");
                        if (Directory.Exists(native)) acc.Add(native);
                    }
                }
                catch { }
            }
            if (Directory.Exists(baseDir)) { acc.Add(baseDir); AddNativeDirs(baseDir); }
            if (Directory.Exists(extApp))
            {
                try { foreach (var hashDir in Directory.EnumerateDirectories(extApp)) AddNativeDirs(hashDir); } catch { }
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
        bool amprIndex = true;            // rebuild ampr_emu.index when the AMPR emulator is shipped
        string hdrFlag = "auto";          // param.json attribute bit 29 (HDR support): auto = as the source declares, on, off

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
            // Single-threaded, always. LibProsperoPkg 1.2.0's encoder is not thread-safe:
            // -j 4 crashed 4 of 6 runs with AccessViolationException — inside
            // OodleKrakenEncoder.Hash under the Task.Run workers of the inner-data pass, and
            // inside the XtsBlockTransform constructor reached from the ThreadLocal factory
            // in ProsperoNapsPhysicalIntegrityCollector.Observe — and each crash left a
            // partial .pkg under the final name. (An earlier note blamed the outer-PFS
            // AES-XTS worker; that pass no longer runs since PlaintextNoAuth.) --parallelism
            // is still accepted for compatibility and clamped to 1 with a warning.
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
            switch (a)
            {
                case "--content-id": opts.ContentId = Need(args, ref i, a); break;
                case "--title-id": opts.TitleId = Need(args, ref i, a); break;
                case "--title": opts.Title = Need(args, ref i, a); break;
                case "--version": opts.Version = Need(args, ref i, a); break;
                case "--passcode": opts.Passcode = Need(args, ref i, a); break;
                case "--mode":
                {
                    var v = Need(args, ref i, a);
                    opts.InnerCompression = v.ToLowerInvariant() switch
                    {
                        "none" => ProsperoInnerCompression.None,
                        "zlib" => ProsperoInnerCompression.Zlib,
                        "kraken" => ProsperoInnerCompression.Kraken,
                        _ => throw new ArgumentException($"unknown --mode: {v}")
                    };
                    break;
                }
                case "--kraken-backend":
                {
                    var v = Need(args, ref i, a);
                    opts.KrakenBackend = v.ToLowerInvariant() switch
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
                    break;
                }
                case "--pubtools-dll": opts.PublishingToolsLibraryPath = Need(args, ref i, a); break;
                case "--parallelism":
                case "-j":
                {
                    var v = Need(args, ref i, a);
                    if (!int.TryParse(v, out int j) || j < 1) throw new ArgumentException("--parallelism needs an integer >= 1");
                    // Never applied: see the KrakenMaxDegreeOfParallelism note above.
                    if (j > 1)
                        Console.Error.WriteLine($"[warn] --parallelism {j} ignored: LibProsperoPkg 1.2.0's encoder is not thread-safe (crashes observed); running single-threaded");
                    break;
                }
                case "--deterministic": opts.DeterministicBuild = true; break;
                case "--temp":
                {
                    var v = Need(args, ref i, a);
                    if (string.IsNullOrWhiteSpace(v)) throw new ArgumentException("--temp needs a directory");
                    Directory.CreateDirectory(v);
                    opts.TemporaryDirectory = v;
                    break;
                }
                case "--level":
                {
                    var v = Need(args, ref i, a);
                    if (!int.TryParse(v, out int lvl)) throw new ArgumentException("--level needs an integer");
                    // Kraken accepts -4..9 (drakmor's encoder), the legacy zlib path 0..9.
                    opts.KrakenCompressionLevel = Math.Clamp(lvl, -4, 9);
                    opts.LegacyZlibCompressionLevel = Math.Clamp(lvl, 0, 9);
                    break;
                }
                case "--playgo-chunks":
                {
                    // LibProsperoPkg defaults to 64 and spreads files over as many chunks as
                    // there are files; Sony's publisher packs every launch-time file into
                    // chunk 0 (default here). Auto-detected from source's playgo-chunk.dat
                    // when present; this override wins over the auto-detect. The library
                    // itself rejects anything outside 1..64.
                    var v = Need(args, ref i, a);
                    if (!int.TryParse(v, out int chunks) || chunks < 1 || chunks > 64)
                        throw new ArgumentException("--playgo-chunks needs an integer 1..64 (LibProsperoPkg's range)");
                    opts.PlayGoChunkCount = chunks;
                    playGoChunksExplicit = true;
                    break;
                }
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
                case "--no-ampr-index":
                    // Keep whatever ampr_emu.index the source ships (or none) instead of
                    // rebuilding it over the files that are actually packed.
                    amprIndex = false;
                    break;
                case "--hdr-flag":
                    // auto: keep the source's declaration (a console on "HDR when supported"
                    // follows bit 29 — set means it switches to HDR output for this title).
                    // on: set the bit even if the source lacks it. off: clear it.
                    hdrFlag = Need(args, ref i, a).ToLowerInvariant();
                    if (hdrFlag is not ("auto" or "on" or "off"))
                        throw new ArgumentException("--hdr-flag needs auto, on or off");
                    break;
                case "--no-hdr-flag": hdrFlag = "off"; break;   // 1.1.12/1.1.13 spelling
                default: return Bad("unknown build flag: " + a);
            }
        }
        if (string.IsNullOrWhiteSpace(opts.ContentId)) return Bad("--content-id required");
        if (string.IsNullOrWhiteSpace(opts.TitleId)) return Bad("--title-id required");
        Directory.CreateDirectory(opts.OutputFolder);

        // Stage: every build works on a mirror of the source (hard links where possible, no
        // gigabyte copy). The pre-build transforms run on that mirror:
        //   (a) auto-generate sce_sys/*.dds from PNGs the source only provides as PNG
        //       (LibProsperoPkg's builder moves sce_sys/*.dds into the outer CNT but does
        //       NOT generate the DDS itself; without them the .pkg installs but never
        //       launches — see the 1.1.7 CHANGELOG entry);
        //   (b) fake-sign raw ELFs (eboot.bin, *.elf, *.prx, *.sprx) that a retail-shape
        //       source ships unsigned — a jailbroken PS5's app loader rejects a package
        //       whose eboot is not a fake-self and kills the process immediately (short
        //       fan-spin then CE-100096-6). Idempotent — already-signed inputs are skipped.
        // Staging is unconditional because LibProsperoPkg's EnsureParamJson writes a generated
        // sce_sys/param.json into the folder it builds from when none exists — which used to
        // be the user's source folder whenever nothing else needed staging.
        // Auto-detect PlayGoChunkCount from the source's own sce_sys/playgo-chunk.dat is a
        // pure metadata read.
        string effectiveSource = opts.SourceFolder!;
        string? autoStage = null;
        var srcSceSys = Path.Combine(effectiveSource, "sce_sys");

        // -- PlayGoChunkCount auto-detect (unless the user pinned it explicitly) --
        bool playgoOutOfRange = false;   // the source's set declares a count the library rejects
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
                        if (detected > 64)
                        {
                            // LibProsperoPkg throws "PlayGo chunk count must be in the range 1..64"
                            // for such a set; discard it and regenerate with the default count.
                            Console.Error.WriteLine($"  [playgo] source declares {detected} chunks (> 64): regenerating the prepared set");
                            playgoOutOfRange = true;
                        }
                        else if (detected == 0)
                        {
                            Console.Error.WriteLine("  [playgo] source declares 0 chunks (invalid): regenerating the prepared set");
                            playgoOutOfRange = true;
                        }
                        else if (detected != opts.PlayGoChunkCount)
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
        //   - param.json attribute bit 29 (0x20000000) = HDR support flag. A console on
        //     "HDR when supported" switches to HDR output when it is set (verified: the
        //     build with it ran the console in HDR10, the build without in SDR). The
        //     source's own declaration is the publisher's intent, so --hdr-flag auto
        //     (default) keeps it; on/off override. Not launch-critical either way.
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
            bool discardPlayGo = playgoPresent.Length > 0 && (regenPlayGo || playgoBad.Count > 0 || playgoOutOfRange);
            if (discardPlayGo)
                Console.Error.WriteLine(playgoBad.Count > 0
                    ? $"  [playgo] prepared set is CORRUPT — {string.Join(", ", playgoBad)}; discarding all {playgoPresent.Length} file(s) so LibProsperoPkg regenerates a consistent {opts.PlayGoChunkCount}-chunk set"
                    : regenPlayGo
                    ? $"  [playgo] --regen-playgo: discarding prepared set ({playgoPresent.Length} file(s)); LibProsperoPkg will regenerate {opts.PlayGoChunkCount} chunk(s)"
                    : $"  [playgo] discarding prepared set ({playgoPresent.Length} file(s)); LibProsperoPkg will regenerate {opts.PlayGoChunkCount} chunk(s)");

            // Any sce_sys/*.json that is not JSON is a mislabeled dump artifact (same
            // shifted-name bug); it would land in the inner PFS as junk. Drop it.
            var badJson = Directory.Exists(srcSceSys)
                ? Directory.EnumerateFiles(srcSceSys, "*.json").Where(p =>
                    { try { return !LooksLikeJsonText(File.ReadAllBytes(p)); } catch { return false; } })
                  .Select(p => Path.GetFileName(p)!).ToArray()
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

            // Always stage (see the note above). Any failure in this block is fatal — a package
            // built from the raw source would silently lack the PlayGo validation, the license
            // entries, the fake-signing and the retail SELF flag. The one exception is the DDS
            // conversion, which runs LAST and is caught per icon.
            {
                string tempRoot = string.IsNullOrEmpty(opts.TemporaryDirectory) ? Path.GetTempPath() : opts.TemporaryDirectory!;
                var (stageDir, hardLinks, copyReason) = ChooseStageDir(effectiveSource, tempRoot);
                autoStage = stageDir;
                if (!hardLinks)
                    Console.Error.WriteLine($"[stage] {copyReason}: copying {DirectorySize(effectiveSource) / 1073741824.0:F1} GB into {tempRoot}");
                MirrorSource(effectiveSource, autoStage, hardLinks);
                Console.Error.WriteLine($"  [stage] mirrored source into {autoStage} ({(hardLinks ? "hard links" : "copy")})");

                // (0) Drop corrupt/mislabeled sce_sys metadata from the mirror (unlinks the
                //     hard link; the on-disk source is untouched). Shipping a corrupt PlayGo
                //     file means CE-100022-5 at launch, so a failed unlink is fatal.
                if (discardPlayGo || badJson.Length > 0)
                {
                    var stagedSceSys0 = Path.Combine(autoStage, "sce_sys");
                    foreach (var n in (discardPlayGo ? playgoPresent : Array.Empty<string>()).Concat(badJson))
                    {
                        var p = Path.Combine(stagedSceSys0, n);
                        if (File.Exists(p) || IsSymlink(p)) File.Delete(p);
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
                // LibProsperoPkg.dll (patches/README.md: the Mono.Cecil patcher retargets the
                // branch of the hardcoded 0-for-Application ternary to always-16). This
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

                    // (c.iii) Rewrite staged param.json: attribute bit 29 per --hdr-flag and a
                    //         "kernel" block when the source has none (see RewriteStagedParamJson).
                    RewriteStagedParamJson(srcSceSys, stagedSceSys, hdrFlag, addKernel: true, tag: "retail");
                    Console.Error.WriteLine("  [retail] dropped placeholder license.dat/info; installed DebugLicenseProvider (CNT entries 0x0400/0x0401); drm_type=16 via patched LibProsperoPkg.dll");
                }

                // (d) --hdr-flag on|off for every other source (free DRM, or --no-retail-
                //     normalize): the attribute rewrite used to live only inside the retail
                //     block, so the flag was silently ignored there. auto changes nothing.
                if (!doRetailNormalize && hdrFlag != "auto")
                    RewriteStagedParamJson(srcSceSys, Path.Combine(autoStage!, "sce_sys"), hdrFlag, addKernel: false, tag: "param");

                // (e) DDS icon CNT entries — LAST, and caught per icon: a PNG that Magick
                //     rejects must not abort the safety-critical steps above. The package then
                //     lacks that DDS (without icon0.dds it installs but never launches).
                if (pngsToConvert.Length > 0)
                {
                    var stagedSceSys = Path.Combine(autoStage, "sce_sys");
                    Directory.CreateDirectory(stagedSceSys);
                    foreach (var name in pngsToConvert)
                    {
                        try
                        {
                            var png = File.ReadAllBytes(Path.Combine(srcSceSys, name));
                            var dds = ProsperoDdsEncoder.EncodePngToDds(png, opts.TemporaryDirectory ?? Path.GetTempPath());
                            var ddsPath = Path.Combine(stagedSceSys, Path.ChangeExtension(name, ".dds"));
                            if (File.Exists(ddsPath) || IsSymlink(ddsPath)) File.Delete(ddsPath);
                            File.WriteAllBytes(ddsPath, dds);
                            Console.Error.WriteLine($"  [icon] generated sce_sys/{Path.ChangeExtension(name, ".dds")} ({dds.Length:N0} B) from {name}");
                        }
                        catch (Exception ex)
                        {
                            Console.Error.WriteLine($"[warn] icon conversion failed for {name}: {ex.GetType().Name}: {ex.Message}"
                                + (name == "icon0.png" ? " — the package will have no icon0.dds and will not launch on a console" : ""));
                        }
                    }
                }

                // (f) ampr_emu.index — the AMPR emulator (fakelib/libSceAmpr.sprx) resolves APR
                //     file ids through /app0/ampr_emu.index, so the index must describe the files
                //     that are actually packed: fake-signing above changes sizes, and a source may
                //     ship no index at all. Rebuilt over the staged tree, written by rename so a
                //     hard-linked index from the source is replaced, never written through.
                if (amprIndex && File.Exists(Path.Combine(autoStage, "fakelib", "libSceAmpr.sprx")))
                {
                    int rows = AmprIndex.Write(autoStage);
                    Console.Error.WriteLine(rows > 0
                        ? $"  [ampr] rebuilt ampr_emu.index over the packed files ({rows:N0} file(s))"
                        : "  [ampr] nothing to index; ampr_emu.index left as it is");
                }

                opts.SourceFolder = autoStage;
            }
        }
        catch (Exception ex)
        {
            var chain = ex.GetType().Name + ": " + ex.Message;
            for (var e = ex.InnerException; e != null; e = e.InnerException)
                chain += "  <- " + e.GetType().Name + ": " + e.Message;
            Console.Error.WriteLine("[error] source staging failed (" + chain + "); nothing was built. A package built from the raw source would lack PlayGo validation, license entries, fake-signing and the retail SELF flag.");
            if (Environment.GetEnvironmentVariable("PKG_TOOL_TRACE") == "1")
                Console.Error.WriteLine(ex.ToString());
            CleanupStage(autoStage);
            return 1;
        }

        Console.Error.WriteLine($"[info] build  {opts.SourceFolder} -> {opts.OutputFolder}  (inner={opts.InnerCompression}, backend={opts.KrakenBackend}, level={opts.KrakenCompressionLevel}, temp={opts.TemporaryDirectory ?? "$TMPDIR"}, chunks={opts.PlayGoChunkCount}, fake-sign={autoFakeSign}, retail-normalize={doRetailNormalize})");

        // Baselines for the cleanup after a failed or cancelled build: only files that did not
        // exist before the build are removed — a .pkg in the output folder (the library writes
        // the FIH image straight under its final name) and the library's intermediates in temp.
        string outFull = Path.GetFullPath(opts.OutputFolder);
        string tempFull = Path.GetFullPath(string.IsNullOrWhiteSpace(opts.TemporaryDirectory) ? Path.GetTempPath() : opts.TemporaryDirectory!);
        var pkgBefore = new HashSet<string>(SafeEnumerate(outFull, "*.pkg"), StringComparer.Ordinal);
        var tempBefore = new HashSet<string>(LibraryTempFiles(tempFull), StringComparer.Ordinal);
        void CleanupAfterFailure()
        {
            CleanupStage(autoStage);
            foreach (var p in SafeEnumerate(outFull, "*.pkg")) if (!pkgBefore.Contains(p)) TryDeleteFile(p, "partial package");
            foreach (var p in LibraryTempFiles(tempFull)) if (!tempBefore.Contains(p)) TryDeleteFile(p, "library temp file");
        }

        // SIGTERM (what the GUI's cancel sends to the process group) and SIGINT cancel the build
        // through the library's own CancellationToken, then everything this run created is
        // removed. The library polls the token per file and per block, so it normally unwinds
        // within a second; the GUI follows up with SIGKILL after 5 s, so if the build has not
        // unwound in 2.5 s the handler thread cleans up itself and exits.
        using var cts = new CancellationTokenSource();
        opts.CancellationToken = cts.Token;
        int signalExit = 0;
        using var buildDone = new ManualResetEventSlim(false);
        void OnSignal(PosixSignalContext ctx)
        {
            ctx.Cancel = true;   // the runtime must not terminate the process for us
            int code = ctx.Signal == PosixSignal.SIGINT ? 130 : 143;
            if (Interlocked.CompareExchange(ref signalExit, code, 0) != 0) return;
            try { Console.Error.WriteLine($"[cancel] {ctx.Signal} received: cancelling the build and cleaning up"); } catch { }
            cts.Cancel();
            if (!buildDone.Wait(TimeSpan.FromMilliseconds(2500)))
            {
                CleanupAfterFailure();
                Environment.Exit(code);
            }
        }
        using var onTerm = PosixSignalRegistration.Create(PosixSignal.SIGTERM, OnSignal);
        using var onInt = PosixSignalRegistration.Create(PosixSignal.SIGINT, OnSignal);

        ProsperoBuildResult result;
        try
        {
            result = ProsperoPackageBuilder.Build(opts, logger: s => Console.Error.WriteLine("  " + s));
        }
        catch (Exception) when (signalExit != 0)
        {
            buildDone.Set();
            CleanupAfterFailure();
            try { Console.Error.WriteLine($"[cancel] build cancelled; mirror, temp files and partial output removed (exit {signalExit})"); } catch { }
            return signalExit;
        }
        catch
        {
            buildDone.Set();
            CleanupAfterFailure();
            throw;
        }
        finally
        {
            buildDone.Set();
            CleanupStage(autoStage);
        }
        Console.WriteLine($"OK — wrote {result.OutputPath} ({new FileInfo(result.OutputPath).Length:N0} B)");
        if (result.Warnings != null)
            foreach (var w in result.Warnings) Console.Error.WriteLine("[warn] " + w);
        return 0;
    }

    /// <summary>Rewrite the staged sce_sys/param.json from the source's copy: attribute bit 29
    /// (0x20000000, HDR support) per --hdr-flag — auto keeps the source's declaration, the
    /// publisher's intent, which a console on "HDR when supported" follows; on/off set/clear
    /// it — and, with <paramref name="addKernel"/> (retail-normalize), a "kernel" block (a
    /// retail reference package's cpu/gpu page-table + flexible-memory sizes) when the source
    /// has none; not launch-critical, kept to match retail packages, a source's own block wins.
    /// The staged file is only replaced when something changes, so the hard link to the source
    /// stays in place otherwise.</summary>
    static void RewriteStagedParamJson(string srcSceSys, string stagedSceSys, string hdrFlag, bool addKernel, string tag)
    {
        var srcParam = Path.Combine(srcSceSys, "param.json");
        var stagedParam = Path.Combine(stagedSceSys, "param.json");
        if (!File.Exists(srcParam))
        {
            if (hdrFlag != "auto")
                Console.Error.WriteLine($"[warn] --hdr-flag {hdrFlag} ignored: the source has no sce_sys/param.json (the builder generates a minimal one)");
            return;
        }
        using var pdoc = JsonDocument.Parse(File.ReadAllBytes(srcParam));
        long currentAttr = 0;
        bool hasAttribute = pdoc.RootElement.TryGetProperty("attribute", out var at);
        if (hasAttribute) currentAttr = at.GetInt64();
        const long HdrBit = 0x20000000L;
        bool srcHdr = (currentAttr & HdrBit) != 0;
        long newAttr = hdrFlag switch
        {
            "on"  => currentAttr | HdrBit,
            "off" => currentAttr & ~HdrBit,
            _     => currentAttr,
        };
        string hdrNote = hdrFlag switch
        {
            "on"  => srcHdr ? "HDR support flag already set in source (--hdr-flag on)" : "HDR support flag set (--hdr-flag on; the source did not declare it)",
            "off" => srcHdr ? "HDR support flag CLEARED (--hdr-flag off; the source declared it)" : "HDR support flag absent in source, left absent (--hdr-flag off)",
            _     => srcHdr ? "source declares HDR support (attribute bit 29) — kept" : "source does not declare HDR support (attribute bit 29) — kept as is; pass --hdr-flag on to force HDR output",
        };
        Console.Error.WriteLine($"  [{tag}] {hdrNote}");
        bool hasKernel = pdoc.RootElement.TryGetProperty("kernel", out _);
        bool wantKernel = addKernel && !hasKernel;
        bool addAttr = !hasAttribute && newAttr != currentAttr;   // the source has no "attribute" at all
        if (newAttr == currentAttr && !wantKernel) return;

        var buf = new MemoryStream();
        using (var w = new Utf8JsonWriter(buf, new JsonWriterOptions { Indented = true }))
        {
            void WriteKernel()
            {
                w.WriteStartObject("kernel");
                w.WriteNumber("cpuPageTableSize", 67108864);       // 64 MiB
                w.WriteNumber("flexibleMemorySize", 272629760);    // ~260 MiB (retail reference value)
                w.WriteNumber("gpuPageTableSize", 67108864);       // 64 MiB
                w.WriteEndObject();
            }
            w.WriteStartObject();
            bool kernelWritten = false, attrWritten = false;
            foreach (var prop in pdoc.RootElement.EnumerateObject())
            {
                // Keep source order; the kernel block goes just before "localizedParameters"
                // (typical Sony ordering) when it is missing.
                if (wantKernel && !kernelWritten && prop.Name == "localizedParameters") { WriteKernel(); kernelWritten = true; }
                if (prop.Name == "attribute") { w.WriteNumber("attribute", newAttr); attrWritten = true; }
                else prop.WriteTo(w);
                // A source without "attribute": add it after applicationDrmType (Sony's ordering).
                if (addAttr && !attrWritten && prop.Name == "applicationDrmType") { w.WriteNumber("attribute", newAttr); attrWritten = true; }
            }
            if (addAttr && !attrWritten) w.WriteNumber("attribute", newAttr);
            if (wantKernel && !kernelWritten) WriteKernel();
            w.WriteEndObject();
        }
        if (File.Exists(stagedParam) || IsSymlink(stagedParam)) File.Delete(stagedParam);
        Directory.CreateDirectory(stagedSceSys);
        File.WriteAllBytes(stagedParam, buf.ToArray());
        var kernelNote = wantKernel ? "; added kernel{cpuPageTable=64Mi, flex=260Mi, gpuPageTable=64Mi}" : "";
        var attrNote = newAttr != currentAttr ? $"param.json attribute 0x{currentAttr:X8} -> 0x{newAttr:X8}" : $"param.json attribute 0x{currentAttr:X8} unchanged";
        Console.Error.WriteLine($"  [{tag}] {attrNote}{kernelNote}");
    }

    static void CleanupStage(string? stage)
    {
        if (stage == null) return;
        try { if (Directory.Exists(stage)) Directory.Delete(stage, recursive: true); } catch { }
    }

    static List<string> SafeEnumerate(string dir, string pattern)
    {
        try { return Directory.Exists(dir) ? Directory.EnumerateFiles(dir, pattern).ToList() : new List<string>(); }
        catch { return new List<string>(); }
    }

    /// <summary>LibProsperoPkg's intermediates in the temp directory: libprospero-publisher-&lt;guid&gt;.*
    /// (pfs_image.dat, naps_pkg_layout.dat, outer.pfs) and .&lt;pkg name&gt;.&lt;guid&gt;.cnt.tmp.</summary>
    static IEnumerable<string> LibraryTempFiles(string tempDir)
        => SafeEnumerate(tempDir, "libprospero-publisher-*").Concat(SafeEnumerate(tempDir, ".*.cnt.tmp"));

    static void TryDeleteFile(string path, string what)
    {
        try
        {
            if (!File.Exists(path)) return;
            File.Delete(path);
            Console.Error.WriteLine($"  [cleanup] removed {what}: {path}");
        }
        catch { }
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
