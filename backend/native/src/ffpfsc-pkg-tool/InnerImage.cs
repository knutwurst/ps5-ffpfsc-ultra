using System;
using System.Buffers.Binary;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using LibProsperoPkg.PFS;
using LibProsperoPkg.PFS.Compression;
using LibProsperoPkg.PKG;
using LibProsperoPkg.Util;

namespace PkgTool;

// Random access into the inner PPR-PFS of a finalized fPKG without decoding the whole image.
//
// LibProsperoPkg's own ExtractInnerFiles first materializes the complete logical inner image
// (decrypt outer -> NAPS-decode everything to a temp file) and then walks it. For "list" and
// "extract a few members" that is the wrong cost model: a 100 GB game would be decoded twice
// to read a 1 MB directory tree. This class chains the library's public random-access pieces
// instead:
//
//   package file
//     -> ProsperoOuterPfsDecryptReader   (AES-XTS per 64 KiB outer block, on demand)
//     -> PfsReader (outer)               (finds pfs_image.dat + naps_pkg_layout.dat)
//     -> ProsperoNapsImage.DecompressRange (Kraken/stored NAPS spans, only the touched ones)
//     -> NapsBlockReader                 (aligned block cache, IMemoryReader)
//     -> PfsReader (inner)               (the /app0 tree: names, sizes, offsets)
//
// Note: DecodePlaintextInnerPfsRange exists in the library but refuses every package this
// tool builds — the builder always encrypts the outer PFS (mode 0x000D with a real seed), and
// that method demands the plaintext/no-auth marker. Both layouts are handled here.
internal sealed class InnerImage : IDisposable
{
    public const int OuterBlockSize = 65536;

    readonly FileStream _fs;
    readonly IMemoryReader _outerReader;
    readonly IMemoryReader _imageView;
    readonly StreamWrapper _pfsImage;
    readonly NapsLayoutDocument _layout;
    readonly ProsperoNapsPlan _plan;
    readonly NapsBlockReader _blocks;

    public string PackagePath { get; }
    public long LogicalSize => _plan.UncompressedSize;
    public long SuperblockOffset { get; }
    public PfsReader Pfs { get; }
    /// <summary>Number of DecompressRange calls made so far (diagnostics).</summary>
    public int RangeCalls => _blocks.RangeCalls;
    public long RangeBytes => _blocks.RangeBytes;

    public InnerImage(string packagePath, string passcode, int cacheBlockSize = 1 << 20, int cacheBlocks = 32)
    {
        PackagePath = packagePath;
        _fs = File.OpenRead(packagePath);
        try
        {
            var pkg = ProsperoPkgReader.Read(_fs);
            var map = ProsperoPackageArchive.Inspect(_fs);
            if (pkg.Fih == null || map.OuterSuperblockIndex < 0)
                throw new InvalidDataException("package is not a finalized image (no FIH header) — it has no inner PFS to read.");
            var header = pkg.Header ?? throw new InvalidDataException("package has no CNT header.");
            if (map.OuterPfsSize <= 0 || map.OuterPfsSize % OuterBlockSize != 0)
                throw new InvalidDataException("outer PFS is empty or not 64 KiB aligned.");
            long blockCountL = map.OuterPfsSize / OuterBlockSize;
            if (blockCountL > int.MaxValue) throw new InvalidDataException("outer PFS block count exceeds Int32.");
            int blockCount = (int)blockCountL;

            // Outer superblock: mode + seed decide between plaintext/no-auth and encrypted layouts.
            var sb = new byte[OuterBlockSize];
            _fs.Position = map.OuterPfsOffset + (long)map.OuterSuperblockIndex * OuterBlockSize;
            _fs.ReadExactly(sb);
            ushort mode = BinaryPrimitives.ReadUInt16LittleEndian(sb.AsSpan(28, 2));
            const ushort ExpectedMode = 0x000D; // Signed | Encrypted | UnknownFlagAlwaysSet
            if (mode != ExpectedMode)
                throw new InvalidDataException($"unsupported outer PFS mode 0x{mode:X4}; expected 0x000D.");
            var icv = sb.AsSpan(896, 32);
            if (!ProsperoOuterPfsSignature.ComputeSuperblockIcv(sb).AsSpan().SequenceEqual(icv))
                throw new InvalidDataException("outer PFS superblock ICV mismatch — the package is damaged.");
            var seed = sb.AsSpan(880, 16).ToArray();

            if (seed.AsSpan().SequenceEqual(ProsperoOuterPfsBuilder.PlaintextNoAuthSeedMarker))
            {
                _outerReader = new LibProsperoPkg.Util.StreamReader(new SubStream(_fs, map.OuterPfsOffset, map.OuterPfsSize), 0);
            }
            else
            {
                var (tweakKey, dataKey) = ProsperoPfsKeys.DeriveImageEncryptionKeys(
                    ProsperoPfsKeys.DeriveEkpfs(header.ContentId, passcode), seed);
                int innerBlocks = checked((int)pkg.Fih.InnerImageBlockCount);
                if (innerBlocks > map.OuterSuperblockIndex)
                    throw new InvalidDataException("FIH inner-image block count crosses the outer superblock.");
                var kinds = new ProsperoOuterBlockKind[blockCount];
                Array.Fill(kinds, ProsperoOuterBlockKind.Signed);
                for (int i = 0; i < innerBlocks; i++) kinds[i] = ProsperoOuterBlockKind.Data;
                kinds[map.OuterSuperblockIndex] = ProsperoOuterBlockKind.Plaintext;
                _outerReader = new ProsperoOuterPfsDecryptReader(_fs, map.OuterPfsSize, tweakKey, dataKey, kinds,
                                                                 OuterBlockSize, map.OuterPfsOffset);
            }

            PfsReader outer;
            try
            {
                outer = new PfsReader(_outerReader, 0, null, null, null,
                                      (long)map.OuterSuperblockIndex * OuterBlockSize, encryptedDataAlreadyDecrypted: true);
            }
            catch (Exception ex)
            {
                throw new InvalidDataException("outer PFS did not parse (" + ex.Message + ") — wrong --passcode?", ex);
            }
            var imageFile = FindOuterFile(outer, "pfs_image.dat");
            var layoutFile = FindOuterFile(outer, ProsperoNapsLayout.FileName);
            _layout = ProsperoNapsLayout.Parse(layoutFile.ReadAllBytes());
            _plan = ProsperoNapsImage.BuildPlan(_layout);
            _imageView = imageFile.GetView();
            _pfsImage = new StreamWrapper(_imageView, imageFile.size);
            _blocks = new NapsBlockReader(_pfsImage, _layout, _plan.UncompressedSize, cacheBlockSize, cacheBlocks);

            SuperblockOffset = LocateInnerSuperblock();
            if (SuperblockOffset < 0)
                throw new InvalidDataException("the NAPS logical stream does not contain an inner PPR-PFS superblock.");
            Pfs = new PfsReader(_blocks, 0, null, null, null, SuperblockOffset, encryptedDataAlreadyDecrypted: true);
        }
        catch
        {
            Dispose();
            throw;
        }
    }

    static PfsReader.File FindOuterFile(PfsReader pfs, string name)
        => pfs.GetAllFiles().FirstOrDefault(f => string.Equals(Path.GetFileName(f.FullName), name, StringComparison.Ordinal))
           ?? throw new InvalidDataException("outer PFS does not contain " + name + ".");

    // The library scans the decoded image forward in 64 KiB steps and takes the first hit. Our
    // images are data-first, so that scan would decode everything. Try the cheap candidates
    // first (they cost one cache block each) and only fall back to the library's full scan.
    long LocateInnerSuperblock()
    {
        long total = _plan.UncompressedSize;
        var probe = new byte[12];
        bool Hit(long off)
        {
            if (off < 0 || off % OuterBlockSize != 0 || off + probe.Length > total) return false;
            _blocks.Read(off, probe, 0, probe.Length);
            return IsSuperblock(probe);
        }
        var tried = new HashSet<long>();
        bool Try(long off) => tried.Add(off) && Hit(off);

        // 1. Data-first layout: metadata is the last NAPS logical file, superblock at its start.
        foreach (var f in _plan.Files.Reverse().Take(8))
            if (Try(f.UncompressedOffset)) return f.UncompressedOffset;
        // 2. Metadata-first layout.
        if (Try(0)) return 0;
        // 3. Backwards from the end (bounded), then the library's forward scan as a last resort.
        long lastBlock = (total / OuterBlockSize - 1) * OuterBlockSize;
        long backLimit = Math.Max(0, lastBlock - 4096L * OuterBlockSize); // 256 MiB
        for (long off = lastBlock; off >= backLimit; off -= OuterBlockSize)
            if (Try(off)) return off;
        Console.Error.WriteLine("[warn] inner superblock not at a NAPS file boundary — falling back to a full forward scan.");
        for (long off = 0; off + OuterBlockSize <= total; off += OuterBlockSize)
            if (Try(off)) return off;
        return -1;
    }

    static bool IsSuperblock(ReadOnlySpan<byte> b)
        => BinaryPrimitives.ReadUInt64LittleEndian(b) == 2 && b[8] == 0x0B && b[9] == 0x2A && b[10] == 0x33 && b[11] == 0x01;

    /// <summary>Path of a node relative to the user root, forward slashes, no leading slash.</summary>
    public string RelativePath(PfsReader.Node node)
    {
        var uroot = Pfs.GetURoot();
        var parts = new List<string>();
        for (PfsReader.Node? n = node; n != null && !ReferenceEquals(n, uroot); n = n.parent)
            parts.Add(n.name);
        parts.Reverse();
        return string.Join('/', parts);
    }

    /// <summary>All directories below the user root (recursive), excluding the root itself.</summary>
    public IEnumerable<PfsReader.Dir> AllDirs()
    {
        var stack = new Stack<PfsReader.Dir>();
        stack.Push(Pfs.GetURoot());
        while (stack.Count > 0)
        {
            var d = stack.Pop();
            foreach (var c in d.children)
                if (c is PfsReader.Dir dd) { yield return dd; stack.Push(dd); }
        }
    }

    /// <summary>
    /// Writes the logical (decompressed) content of <paramref name="file"/> to <paramref name="dest"/>,
    /// exactly as the library's File.Save(path, decompress: true) would. Contiguous plain files
    /// (every file a PPR direct-offset image holds) take a chunked fast path straight through
    /// DecompressRange so the per-call NAPS plan rebuild is amortized over 4 MiB instead of 64 KiB.
    /// </summary>
    public void CopyFile(PfsReader.File file, Stream dest, Action<long>? progress = null, int chunkSize = 4 << 20)
    {
        bool compressed = file.flags.HasFlag(InodeFlags.compressed);
        if (compressed || file.blocks != null)
        {
            // Library path (legacy zlib PFSC inode or a non-contiguous extent). Same code the
            // full extraction runs, just over the cached NAPS reader.
            file.CopyTo(dest, decompress: true);
            progress?.Invoke(file.size);
            return;
        }
        if (dest.CanSeek) dest.SetLength(file.size);
        long remaining = file.size, pos = file.offset;
        if (pos < 0 || pos + remaining > LogicalSize)
            throw new InvalidDataException($"file extent [0x{pos:X},+0x{remaining:X}) lies outside the logical image (0x{LogicalSize:X}).");
        while (remaining > 0)
        {
            int n = (int)Math.Min(remaining, chunkSize);
            var chunk = _blocks.DecompressRange(pos, n);
            dest.Write(chunk, 0, n);
            pos += n; remaining -= n;
            progress?.Invoke(n);
        }
    }

    public void Dispose()
    {
        _pfsImage?.Dispose();
        _imageView?.Dispose();
        _outerReader?.Dispose();
        _fs?.Dispose();
    }
}

/// <summary>
/// IMemoryReader over the NAPS-decoded logical inner image. Serves reads from an LRU cache of
/// aligned blocks; each miss decodes exactly one block through ProsperoNapsImage.DecompressRange,
/// so directory walks touch a few hundred KiB of a multi-GB image.
/// </summary>
internal sealed class NapsBlockReader : IMemoryReader
{
    readonly Stream _pfsImage;
    readonly NapsLayoutDocument _layout;
    readonly long _total;
    readonly int _blockSize;
    readonly int _capacity;
    readonly Dictionary<long, LinkedListNode<(long index, byte[] data)>> _map = new();
    readonly LinkedList<(long index, byte[] data)> _lru = new();

    public int RangeCalls { get; private set; }
    public long RangeBytes { get; private set; }

    public NapsBlockReader(Stream pfsImage, NapsLayoutDocument layout, long total, int blockSize, int capacity)
    {
        if (blockSize <= 0 || (blockSize & (blockSize - 1)) != 0) throw new ArgumentOutOfRangeException(nameof(blockSize), "power of two required");
        _pfsImage = pfsImage; _layout = layout; _total = total; _blockSize = blockSize; _capacity = Math.Max(1, capacity);
    }

    public byte[] DecompressRange(long offset, int length)
    {
        if (offset < 0 || offset + length > _total) throw new EndOfStreamException("read past the end of the inner image.");
        RangeCalls++; RangeBytes += length;
        return ProsperoNapsImage.DecompressRange(_pfsImage, _layout, offset, length);
    }

    byte[] Block(long index)
    {
        if (_map.TryGetValue(index, out var node))
        {
            _lru.Remove(node); _lru.AddFirst(node);
            return node.Value.data;
        }
        long off = index * _blockSize;
        int len = (int)Math.Min(_blockSize, _total - off);
        var data = DecompressRange(off, len);
        node = new LinkedListNode<(long, byte[])>((index, data));
        _lru.AddFirst(node); _map[index] = node;
        while (_lru.Count > _capacity)
        {
            var last = _lru.Last!;
            _map.Remove(last.Value.index);
            _lru.RemoveLast();
        }
        return data;
    }

    public void Read(long pos, byte[] buf, int offset, int count)
    {
        ArgumentNullException.ThrowIfNull(buf);
        if (pos < 0 || offset < 0 || count < 0 || offset > buf.Length - count) throw new ArgumentOutOfRangeException(nameof(pos));
        if (count > 0 && pos + count > _total) throw new EndOfStreamException("read past the end of the inner image.");
        while (count > 0)
        {
            long index = pos / _blockSize;
            int inBlock = (int)(pos - index * _blockSize);
            var data = Block(index);
            int n = Math.Min(count, data.Length - inBlock);
            if (n <= 0) throw new EndOfStreamException("read reached a truncated inner block.");
            Buffer.BlockCopy(data, inBlock, buf, offset, n);
            pos += n; offset += n; count -= n;
        }
    }

    public void Dispose() { _map.Clear(); _lru.Clear(); }
}
