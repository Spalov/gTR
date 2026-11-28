using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

namespace GAssets
{
    internal sealed class UpdateError : Exception
    {
        public readonly string Code;
        public UpdateError(string code) : base(code) { Code = code; }
        public static void Require(bool condition, string code)
        {
            if (!condition) throw new UpdateError(code);
        }
    }

    internal static class Json
    {
        public static readonly UTF8Encoding Utf8 = new UTF8Encoding(false, true);

        public static object Document(byte[] bytes)
        {
            try
            {
                return new JavaScriptSerializer { MaxJsonLength = 4 * 1024 * 1024, RecursionLimit = 24 }
                    .DeserializeObject(Utf8.GetString(bytes));
            }
            catch (Exception ex) when (ex is ArgumentException || ex is InvalidOperationException)
            {
                throw new UpdateError("JSON_INVALID");
            }
        }

        public static Dictionary<string, object> Read(byte[] bytes) =>
            Document(bytes) as Dictionary<string, object> ?? throw new UpdateError("JSON_INVALID");

        public static byte[] Write(object value) => Utf8.GetBytes(new JavaScriptSerializer
            { MaxJsonLength = 4 * 1024 * 1024, RecursionLimit = 24 }.Serialize(value) + "\n");

        public static string Text(this Dictionary<string, object> value, string key, int max = 256)
        {
            var text = value.TryGetValue(key, out var found) ? found as string : null;
            UpdateError.Require(text != null && text.Length <= max, "JSON_SCHEMA");
            return text;
        }

        public static long Int(this Dictionary<string, object> value, string key, long min = 0, long max = long.MaxValue)
        {
            UpdateError.Require(value.TryGetValue(key, out var found) && (found is int || found is long), "JSON_SCHEMA");
            long number = Convert.ToInt64(found, CultureInfo.InvariantCulture);
            UpdateError.Require(number >= min && number <= max, "JSON_SCHEMA");
            return number;
        }

        public static Dictionary<string, object> Obj(this Dictionary<string, object> value, string key)
        {
            UpdateError.Require(value.TryGetValue(key, out var found) && found is Dictionary<string, object>, "JSON_SCHEMA");
            return (Dictionary<string, object>)found;
        }

        public static object[] Array(this Dictionary<string, object> value, string key)
        {
            UpdateError.Require(value.TryGetValue(key, out var found) && found is object[], "JSON_SCHEMA");
            return (object[])found;
        }

        public static DateTimeOffset Date(this Dictionary<string, object> value, string key)
        {
            UpdateError.Require(DateTimeOffset.TryParseExact(value.Text(key, 40), "yyyy-MM-dd'T'HH:mm:ss'Z'",
                CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var date), "JSON_SCHEMA");
            return date.ToUniversalTime();
        }
    }

    internal sealed class ProductVersion : IComparable<ProductVersion>
    {
        private static readonly string[] Channels = { "experimental", "beta", "rc", "stable" };
        public readonly string Channel;
        public readonly int Number;
        public readonly int Fix;
        public readonly string Build;

        public ProductVersion(Dictionary<string, object> data)
        {
            Channel = data.Text("channel", 16);
            Number = (int)data.Int("number", 1, 100000);
            Fix = (int)data.Int("fix", 0, 100000);
            Build = data.Text("build", 64);
            UpdateError.Require(Channels.Contains(Channel) && Regex.IsMatch(Build, @"\A[A-Za-z0-9.-]+\z"), "VERSION_INVALID");
        }

        public Dictionary<string, object> Data => new Dictionary<string, object>
        {
            {"channel", Channel}, {"number", Number}, {"fix", Fix}, {"build", Build}
        };

        public int CompareTo(ProductVersion other)
        {
            if (other == null) return 1;
            int result = Number.CompareTo(other.Number);
            if (result == 0) result = Array.IndexOf(Channels, Channel).CompareTo(Array.IndexOf(Channels, other.Channel));
            return result != 0 ? result : Fix.CompareTo(other.Fix);
        }

        public bool Matches(ProductVersion other) => CompareTo(other) == 0 && Build == other.Build;

        public override string ToString()
        {
            var title = Channel == "rc" ? "RC" : char.ToUpperInvariant(Channel[0]) + Channel.Substring(1);
            return title + " " + Number + (Fix == 0 ? "" : Fix == 1 ? " (Fix)" : " (Fix " + Fix + ")");
        }
    }

    internal sealed class Product
    {
        public readonly string Id, Name, Repository, Entry, Folder, KeyId, Mutex;
        public readonly string[] Channels;
        public readonly bool SelfUpdate;

        public Product(string id, string name, string repository, string entry, string folder, string keyId,
                       string mutex, string[] channels, bool selfUpdate = false)
        {
            UpdateError.Require(Regex.IsMatch(id, @"\Agassets\.[a-z0-9-]+\z") &&
                Regex.IsMatch(repository, @"\A[A-Za-z0-9-]+/[A-Za-z0-9_.-]+\z") &&
                Regex.IsMatch(name, @"\A[A-Za-z0-9][A-Za-z0-9 .-]{0,59}\z") && !name.EndsWith(".") && !name.EndsWith(" "),
                "CATALOG_INVALID");
            Id = id; Name = name; Repository = repository; Entry = Locations.Relative(entry);
            Folder = Locations.Relative(folder); KeyId = keyId; Mutex = mutex; Channels = channels; SelfUpdate = selfUpdate;
        }
    }

    internal sealed class Publisher
    {
        private readonly byte[] key;
        public readonly string Id;

        public Publisher(byte[] publicKey)
        {
            // BCRYPT_ECCPUBLIC_BLOB, ECDSA P-256, followed by X and Y (big endian).
            UpdateError.Require(publicKey != null && publicKey.Length == 72 &&
                BitConverter.ToUInt32(publicKey, 0) == 0x31534345 && BitConverter.ToUInt32(publicKey, 4) == 32, "KEY_INVALID");
            key = (byte[])publicKey.Clone();
            Id = Hash.Bytes(key);
        }

        public Dictionary<string, object> Signed(byte[] data, byte[] signature)
        {
            UpdateError.Require(signature.Length == 64, "SIGNATURE_INVALID");
            using (var imported = CngKey.Import(key, CngKeyBlobFormat.EccPublicBlob))
            using (var verify = new ECDsaCng(imported) { HashAlgorithm = CngAlgorithm.Sha256 })
                UpdateError.Require(verify.VerifyData(data, signature), "SIGNATURE_INVALID");
            var parsed = Json.Read(data);
            UpdateError.Require(parsed.Text("signing_key_id", 64) == Id, "KEY_INVALID");
            return parsed;
        }
    }

    internal static class Hash
    {
        public static string Hex(byte[] value) => BitConverter.ToString(value).Replace("-", "").ToLowerInvariant();
        public static string Bytes(byte[] value) { using (var sha = SHA256.Create()) return Hex(sha.ComputeHash(value)); }
        public static string Stream(Stream value) { using (var sha = SHA256.Create()) return Hex(sha.ComputeHash(value)); }
        public static string Field(Dictionary<string, object> value, string key)
        {
            string hash = value.Text(key, 64);
            UpdateError.Require(Regex.IsMatch(hash, @"\A[0-9a-f]{64}\z"), "HASH_INVALID");
            return hash;
        }
    }

    internal static class Locations
    {
        public static string Relative(string path)
        {
            UpdateError.Require(path.Length > 0 && path.Length <= 230, "PATH_INVALID");
            foreach (var part in path.Split('/'))
            {
                UpdateError.Require(Regex.IsMatch(part, @"\A[A-Za-z0-9_+. -]{1,150}\z") &&
                    !part.EndsWith(".") && !part.EndsWith(" ") &&
                    !Regex.IsMatch(part, @"\A(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|\z)", RegexOptions.IgnoreCase),
                    "PATH_INVALID");
            }
            return path;
        }

        public static string Under(string root, string relative)
        {
            var safe = Path.GetFullPath(Path.Combine(root, Relative(relative).Replace('/', Path.DirectorySeparatorChar)));
            UpdateError.Require(safe.StartsWith(Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) +
                Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase), "PATH_INVALID");
            return safe;
        }

        public static string Local(string path)
        {
            UpdateError.Require(Path.IsPathRooted(path) && !path.StartsWith(@"\\"), "PATH_INVALID");
            var full = Path.GetFullPath(path);
            UpdateError.Require(new DriveInfo(Path.GetPathRoot(full)).DriveType == DriveType.Fixed, "PATH_INVALID");
            for (var part = new DirectoryInfo(full); part != null; part = part.Parent)
            {
                try
                {
                    UpdateError.Require((File.GetAttributes(part.FullName) & FileAttributes.ReparsePoint) == 0, "PATH_INVALID");
                }
                catch (FileNotFoundException) {}
                catch (DirectoryNotFoundException) {}
            }
            return full;
        }

        public static byte[] Read(string path, int max)
        {
            using (var stream = File.OpenRead(Local(path)))
                return Read(stream, max);
        }

        public static byte[] Read(Stream stream, int max)
        {
            using (var output = new MemoryStream())
            {
                var bytes = new byte[Math.Min(max + 1, 64 * 1024)];
                int got;
                while ((got = stream.Read(bytes, 0, bytes.Length)) > 0)
                {
                    UpdateError.Require(output.Length + got <= max, "SIZE_LIMIT");
                    output.Write(bytes, 0, got);
                }
                return output.ToArray();
            }
        }

        public static void Atomic(string path, byte[] bytes)
        {
            Local(path);
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            string next = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                using (var output = new FileStream(next, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    output.Write(bytes, 0, bytes.Length);
                    output.Flush(true);
                }
                if (File.Exists(path)) File.Replace(next, path, null);
                else File.Move(next, path);
            }
            finally { if (File.Exists(next)) File.Delete(next); }
        }
    }

    internal sealed class FileSpec
    {
        public readonly string Path, Sha256;
        public readonly long Size;
        public FileSpec(Dictionary<string, object> file)
        {
            Path = Locations.Relative(file.Text("path", 230));
            Size = file.Int("size", 0, Gsu.MaxFile);
            Sha256 = Hash.Field(file, "sha256");
        }
    }

    internal sealed class Gsu : IDisposable
    {
        public const long MaxPackage = 768L * 1024 * 1024;
        public const long MaxFile = 384L * 1024 * 1024;
        public const long MaxExpanded = 2L * 1024 * 1024 * 1024;
        public const int MaxEntries = 12000;
        public const int ManifestLimit = 2 * 1024 * 1024;
        public const int GauVersion = 1;

        private FileStream stream;
        private ZipArchive zip;
        private readonly Dictionary<string, ZipArchiveEntry> entries = new Dictionary<string, ZipArchiveEntry>(StringComparer.Ordinal);
        public readonly ProductVersion Version;
        public readonly string Id, Kind;
        public readonly long Space;
        public readonly List<FileSpec> Files = new List<FileSpec>();
        public readonly byte[] Manifest, Signature;

        public Gsu(string path, Product product, Publisher publisher, long expectedSize = 0, string expectedHash = null)
        {
            try
            {
                stream = new FileStream(Locations.Local(path), FileMode.Open, FileAccess.Read, FileShare.Read,
                    64 * 1024, FileOptions.SequentialScan);
                UpdateError.Require(stream.Length <= MaxPackage && stream.Length >= 22, "SIZE_LIMIT");
                if (expectedHash != null)
                {
                    UpdateError.Require(stream.Length == expectedSize && Hash.Stream(stream) == expectedHash, "PACKAGE_HASH");
                }
                int count = CheckDirectory(stream);
                stream.Position = 0;
                zip = new ZipArchive(stream, ZipArchiveMode.Read, true, Json.Utf8);
                UpdateError.Require(zip.Entries.Count == count, "ZIP_INVALID");
                var insensitive = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                foreach (var entry in zip.Entries)
                {
                    Locations.Relative(entry.FullName);
                    var type = (entry.ExternalAttributes >> 16) & 0xF000;
                    UpdateError.Require(insensitive.Add(entry.FullName) && (type == 0 || type == 0x8000) &&
                        (entry.ExternalAttributes & (int)(FileAttributes.Directory | FileAttributes.ReparsePoint)) == 0,
                        "ZIP_PATH");
                    UpdateError.Require(entry.Length >= 0 && entry.Length <= MaxFile &&
                        entry.CompressedLength >= 0 && entry.CompressedLength <= stream.Length &&
                        (entry.Length <= 2 * 1024 * 1024 || entry.Length <= entry.CompressedLength * 1000), "SIZE_LIMIT");
                    entries.Add(entry.FullName, entry);
                }
                Manifest = ReadEntry("manifest.json", ManifestLimit);
                Signature = ReadEntry("manifest.sig", 64);
                var data = publisher.Signed(Manifest, Signature);
                UpdateError.Require(data.Int("schema_version") == 1 && data.Int("min_gau", 1) <= GauVersion, "VERSION_UNSUPPORTED");
                UpdateError.Require(data.Text("product_id") == product.Id && data.Text("entry") == product.Entry &&
                    data.Text("signing_key_id") == product.KeyId, "PRODUCT_MISMATCH");
                UpdateError.Require(data.Text("platform") == "windows" && data.Text("architecture") == "x64" &&
                    Environment.Is64BitOperatingSystem, "PLATFORM_UNSUPPORTED");
                UpdateError.Require(data.Text("user_data") == "preserve", "JSON_SCHEMA");
                UpdateError.Require(Environment.OSVersion.Version.Build >= data.Int("windows_build", 10240, 1000000),
                    "PLATFORM_UNSUPPORTED");
                data.Int("data_schema", 1, 10000);
                Id = data.Text("package_id", 120);
                UpdateError.Require(Regex.IsMatch(Id, @"\A[A-Za-z0-9_.-]+\z"), "JSON_SCHEMA");
                Kind = data.Text("kind", 16);
                UpdateError.Require(Kind == "full" || Kind == "repair", "PACKAGE_KIND");
                Version = new ProductVersion(data.Obj("version"));
                UpdateError.Require(product.Channels.Contains(Version.Channel), "CHANNEL_UNSUPPORTED");
                var files = data.Array("files");
                var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                var directories = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                long expanded = 0;
                foreach (var raw in files)
                {
                    var file = new FileSpec(raw as Dictionary<string, object> ?? throw new UpdateError("JSON_SCHEMA"));
                    UpdateError.Require(!file.Path.StartsWith(".gassets-", StringComparison.OrdinalIgnoreCase) && names.Add(file.Path) &&
                        entries.TryGetValue("payload/" + file.Path, out var entry) && entry.Length == file.Size, "FILES_MISMATCH");
                    for (int slash = file.Path.IndexOf('/'); slash >= 0; slash = file.Path.IndexOf('/', slash + 1))
                        directories.Add(file.Path.Substring(0, slash));
                    Files.Add(file);
                    expanded += file.Size;
                    UpdateError.Require(expanded <= MaxExpanded, "SIZE_LIMIT");
                }
                UpdateError.Require(entries.Count == Files.Count + 2 && names.Contains(product.Entry) &&
                    !directories.Overlaps(names), "FILES_MISMATCH");
                Space = data.Int("disk_bytes", expanded, MaxExpanded + 512L * 1024 * 1024);
            }
            catch
            {
                Dispose();
                throw;
            }
        }

        private static int CheckDirectory(Stream stream)
        {
            stream.Seek(-22, SeekOrigin.End);
            using (var reader = new BinaryReader(stream, Json.Utf8, true))
            {
                UpdateError.Require(reader.ReadUInt32() == 0x06054b50 && reader.ReadUInt16() == 0 && reader.ReadUInt16() == 0,
                    "ZIP_INVALID");
                int disk = reader.ReadUInt16(), count = reader.ReadUInt16();
                long size = reader.ReadUInt32(), offset = reader.ReadUInt32();
                UpdateError.Require(disk == count && count >= 3 && count <= MaxEntries && size <= 8 * 1024 * 1024 &&
                    offset + size == stream.Length - 22 && reader.ReadUInt16() == 0, "ZIP_INVALID");
                return count;
            }
        }

        private byte[] ReadEntry(string name, int maximum)
        {
            UpdateError.Require(entries.TryGetValue(name, out var entry) && entry.Length <= maximum, "FILES_MISMATCH");
            using (var input = entry.Open()) return Locations.Read(input, maximum);
        }

        public void Extract(string stage, Action checkpoint)
        {
            var bytes = new byte[128 * 1024];
            foreach (var file in Files)
            {
                var destination = Locations.Local(Locations.Under(stage, file.Path));
                Directory.CreateDirectory(Path.GetDirectoryName(destination));
                using (var input = entries["payload/" + file.Path].Open())
                using (var output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                using (var sha = SHA256.Create())
                {
                    long size = 0;
                    int got;
                    while ((got = input.Read(bytes, 0, bytes.Length)) > 0)
                    {
                        checkpoint();
                        size += got;
                        UpdateError.Require(size <= file.Size, "FILES_MISMATCH");
                        sha.TransformBlock(bytes, 0, got, null, 0);
                        output.Write(bytes, 0, got);
                    }
                    sha.TransformFinalBlock(new byte[0], 0, 0);
                    UpdateError.Require(size == file.Size && Hash.Hex(sha.Hash) == file.Sha256, "FILE_HASH");
                    output.Flush(true);
                }
            }
        }

        public void Dispose()
        {
            zip?.Dispose();
            stream?.Dispose();
        }
    }
}
