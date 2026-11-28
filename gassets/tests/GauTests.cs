using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Threading;
using GAssets;

internal static class GauTests
{
    private static ECDsaCng signer;
    private static Publisher publisher;
    private static Product product;
    private static string root;
    private static int passed;

    private static Dictionary<string, object> Obj(params object[] args)
    {
        var result = new Dictionary<string, object>();
        for (int i = 0; i < args.Length; i += 2)
            result.Add((string)args[i], args[i + 1]);
        return result;
    }

    private static Dictionary<string, object> Version(int number, int fix) =>
        Obj("channel", "beta", "number", number, "fix", fix, "build", number + "." + fix);

    private static void Require(bool condition, string message)
    {
        if (!condition) throw new Exception(message);
    }

    private static void Fails(string code, Action action)
    {
        try { action(); }
        catch (UpdateError error) when (error.Code == code) { return; }
        throw new Exception("Expected " + code);
    }

    private static void Test(string name, Action action)
    {
        action();
        passed++;
        Console.WriteLine("PASS " + name);
    }

    private static void Write(ZipArchive zip, string file, byte[] data)
    {
        using (var stream = zip.CreateEntry(file).Open()) stream.Write(data, 0, data.Length);
    }

    private static string Package(string name, int version, int fix = 0, string file = "gTR.exe",
                                  string productId = null, bool tamper = false, bool wrongArch = false, bool badSignature = false)
    {
        var path = Path.Combine(root, name + ".gsu");
        var test = Json.Utf8.GetBytes("synthetic application " + version + "." + fix);
        var manifest = Json.Write(Obj("schema_version", 1, "package_id", name, "product_id", productId ?? product.Id,
            "version", Version(version, fix), "platform", "windows", "architecture", wrongArch ? "arm64" : "x64",
            "entry", product.Entry, "kind", "full", "min_gau", 1, "windows_build", 10240,
            "user_data", "preserve", "disk_bytes", 256, "data_schema", 1, "signing_key_id", publisher.Id,
            "files", new[] { Obj("path", file, "size", test.Length, "sha256", Hash.Bytes(test)) }));
        using (var fileStream = File.Open(path, FileMode.CreateNew))
        using (var zip = new ZipArchive(fileStream, ZipArchiveMode.Create))
        {
            Write(zip, "manifest.json", manifest);
            var signature = signer.SignData(manifest);
            if (badSignature) signature[5] ^= 2;
            Write(zip, "manifest.sig", signature);
            if (tamper) test[0] ^= 2;
            Write(zip, "payload/" + file, test);
        }
        return path;
    }

    private static byte[] Metadata(int version, int fix, byte[] payload, string tag, int sequence = 1,
                                   int release = 11, int asset = 100, bool expired = false)
    {
        string Date(DateTimeOffset value) => value.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'");
        return Json.Write(Obj("schema_version", 1, "product_id", product.Id, "repository", product.Repository,
            "version", Version(version, fix), "platform", "windows", "architecture", "x64", "min_gau", 1,
            "windows_build", 10240, "sequence", sequence, "delivery", "github", "disk_bytes", 512,
            "published_at", Date(DateTimeOffset.UtcNow.AddDays(-1)), "expires_at", Date(DateTimeOffset.UtcNow.AddHours(expired ? -1 : 1)),
            "github_release_id", release, "github_tag", tag, "signing_key_id", publisher.Id,
            "package", Obj("name", "sample.gsu", "asset_id", asset, "size_bytes", payload.LongLength, "sha256", Hash.Bytes(payload)),
            "changelog", Obj("ru", new[] { "verified text" })));
    }

    private class Fake : ITransport
    {
        public Func<Uri, bool, string, long, HttpResponseMessage> Respond;
        public HttpResponseMessage Get(Uri uri, bool asset, string etag, long offset, CancellationToken token)
        {
            token.ThrowIfCancellationRequested();
            return Respond(uri, asset, etag, offset);
        }
    }

    private static HttpResponseMessage Response(byte[] bytes, HttpStatusCode status = HttpStatusCode.OK, string etag = null)
    {
        var reply = new HttpResponseMessage(status) { Content = new ByteArrayContent(bytes) };
        if (etag != null) reply.Headers.ETag = EntityTagHeaderValue.Parse(etag);
        return reply;
    }

    private static void Registry()
    {
        var program = Path.Combine(root, "programs");
        var user = Path.Combine(root, "userdata");
        var state = Path.Combine(root, "state");
        Directory.CreateDirectory(user);
        var privateFile = Path.Combine(user, "synthetic-work.json");
        File.WriteAllText(privateFile, "existing local projects");
        var gau = new Gau(program, state, user, publisher);
        var first = Package("first", 7);
        var second = Package("next", 7, 1);
        var newer = Package("newer", 10);
        var wrongZip = Package("path", 7, file: "../outside");
        var wrongId = Package("other", 7, productId: "gassets.other");
        var wrongArch = Package("arm", 7, wrongArch: true);
        var wrongHash = Package("hash", 7, tamper: true);
        var wrongSign = Package("signature", 7, badSignature: true);
        var token = CancellationToken.None;
        Func<string, bool> check = File.Exists;
        Test("signed full install", () =>
        {
            gau.Install(product, first, null, token, check);
            Require(gau.Installed(product, true).Version.Number == 7, "installed version");
            Require(File.Exists(gau.Entry(product)), "stable entry");
        });
        Test("wrong package product and architecture", () =>
        {
            Fails("PRODUCT_MISMATCH", () => gau.Install(product, wrongId, null, token, check));
            Fails("PLATFORM_UNSUPPORTED", () => gau.Install(product, wrongArch, null, token, check));
        });
        Test("unsafe path, wrong signature and file hash", () =>
        {
            Fails("PATH_INVALID", () => gau.Install(product, wrongZip, null, token, check));
            Fails("SIGNATURE_INVALID", () => gau.Install(product, wrongSign, null, token, check));
            Fails("FILE_HASH", () => gau.Install(product, wrongHash, null, token, check));
            Require(gau.Installed(product, true).Version.Fix == 0, "working version");
        });
        Test("health failure leaves working version", () =>
        {
            Fails("HEALTH_FAILED", () => gau.Install(product, second, null, token, _ => false));
            Require(gau.Installed(product).Version.Fix == 0, "restored after health failure");
        });
        Test("update and one backup", () =>
        {
            var stable = gau.Entry(product);
            gau.Install(product, second, null, token, check);
            Require(gau.Entry(product) == stable && gau.Installed(product).Version.Fix == 1, "stable entry after update");
            gau.Install(product, newer, null, token, check);
            Require(new InstallRecord(Path.Combine(gau.ProgramRoot(product), "previous"), product, publisher, true)
                .Version.Fix == 1, "one previous");
        });
        Test("manual downgrade rejected and explicit rollback available", () =>
        {
            Fails("VERSION_DOWNGRADE", () => gau.Install(product, first, null, token, check));
            gau.Rollback(product, check);
            Require(gau.Installed(product).Version.Fix == 1 && gau.HighWater(product).Number == 10, "rollback high water");
        });
        Test("mutex blocks installing while running", () =>
        {
            using (var entered = new ManualResetEvent(false))
            using (var release = new ManualResetEvent(false))
            {
                var thread = new Thread(() =>
                {
                    using (var lockFile = new Mutex(true, product.Mutex))
                    {
                        entered.Set();
                        release.WaitOne();
                        lockFile.ReleaseMutex();
                    }
                });
                thread.Start();
                entered.WaitOne();
                try { Fails("PRODUCT_RUNNING", () => gau.Install(product, newer, null, token, check)); }
                finally { release.Set(); thread.Join(); }
            }
        });
        Test("interrupted switch restores old directory", () =>
        {
            string dir = gau.ProgramRoot(product);
            Directory.Move(Path.Combine(dir, "current"), Path.Combine(dir, ".old"));
            Directory.CreateDirectory(Path.Combine(dir, "current"));
            File.WriteAllText(Path.Combine(dir, "current", "gTR.exe"), "incomplete synthetic stage");
            Locations.Atomic(Path.Combine(state, "transactions", product.Id + ".json"),
                Json.Write(Obj("phase", "HEALTH_CHECK", "had_current", true, "operation", "UPDATE", "root", dir)));
            gau.Recover(product);
            Require(gau.Installed(product, true).Version.Fix == 1, "recovered version");
        });
        Test("uninstall preserves user files and monotonic guard", () =>
        {
            gau.Uninstall(product);
            Require(gau.Installed(product) == null && File.ReadAllText(privateFile) == "existing local projects", "data retained");
            Fails("VERSION_DOWNGRADE", () => gau.Install(product, first, null, token, check));
        });
    }

    private static void Releases()
    {
        var token = CancellationToken.None;
        var payload = Enumerable.Range(0, 4000).Select(i => (byte)i).ToArray();
        string tag = "deliberately-custom";
        var meta = Metadata(10, 1, payload, tag);
        var row = Obj("draft", false, "prerelease", true, "id", 11, "tag_name", tag, "assets", new[]
        {
            Obj("id", 20, "name", "gassets-release.json"),
            Obj("id", 21, "name", "gassets-release.sig"),
            Obj("id", 100, "name", "sample.gsu", "size", payload.Length, "digest", "sha256:" + Hash.Bytes(payload)),
        });
        var empty = Obj("draft", true);
        var first = Json.Write(Enumerable.Repeat(empty, 20).ToArray());
        var second = Json.Write(new[] { row });
        int pass = 0;
        var fake = new Fake();
        fake.Respond = (uri, asset, etag, offset) =>
        {
            if (!asset)
            {
                if (etag != null) return Response(new byte[0], HttpStatusCode.NotModified);
                return Response(uri.Query.EndsWith("page=1") ? first : second, etag: "\"stable-etag\"");
            }
            return uri.AbsolutePath.EndsWith("/20") ? Response(meta) : Response(signer.SignData(meta));
        };
        var connector = new GitHub(fake, Path.Combine(root, "cache"), publisher);
        UpdateRelease release = null;
        Test("GitHub pages, signed Beta 10 Fix and ETag 304", () =>
        {
            release = connector.Check(product, "beta", new ProductVersion(Version(7, 2)), token).Release;
            Require(release.Version.Number == 10 && release.Version.Fix == 1, "signed version wins over tag");
            var cached = connector.Check(product, "beta", null, token).Release;
            Require(cached.MetadataHash == release.MetadataHash && cached.Notes("ru") == "verified text", "consistent cached metadata");
        });
        Test("restrict HTTPS sources and block draft or wrong channel", () =>
        {
            Require(!GitHubTransport.Allowed(new Uri("http://api.github.com"), false) &&
                !GitHubTransport.Allowed(new Uri("https://example.invalid"), true) &&
                GitHubTransport.Allowed(new Uri("https://release-assets.githubusercontent.com/archive"), true), "redirect rules");
            Require(connector.Check(product, "stable", null, token).Release == null, "beta hidden on stable");
        });
        Test("archival failed releases are excluded from update results", () =>
        {
            var archive = Json.Write(new[] {Obj("draft", false, "prerelease", true, "tag_name", "beta-7-failed",
                "assets", new[] {Obj("id", 39, "name", "gTR-Beta-7-FAILED-Windows-x64.zip")})});
            var archives = new GitHub(new Fake { Respond = (_, asset, __, ___) =>
            {
                Require(!asset, "archive download must not be requested");
                return Response(archive);
            }}, Path.Combine(root, "archive"), publisher);
            var result = archives.Check(product, "beta", null, token);
            Require(result.Release == null && !result.Unavailable, "archive has no installable update");
        });
        Test("replayed signed metadata rejected", () =>
        {
            meta = Metadata(9, 0, payload, tag, sequence: 1);
            Fails("METADATA_ROLLBACK", () => connector.Check(product, "beta", null, token));
            meta = Metadata(10, 1, payload, tag);
        });
        Test("expired, modified and replaced assets rejected", () =>
        {
            var expired = Metadata(10, 1, payload, tag, expired: true);
            Fails("METADATA_EXPIRED", () => new UpdateRelease(product, publisher, expired, signer.SignData(expired)));
            var signature = signer.SignData(meta);
            signature[8] ^= 1;
            Fails("SIGNATURE_INVALID", () => new UpdateRelease(product, publisher, meta, signature));
            row["id"] = 999;
            second = Json.Write(new[] { row });
            var stale = new GitHub(new Fake { Respond = (uri, asset, _, __) =>
                asset ? uri.AbsolutePath.EndsWith("/20") ? Response(meta) : Response(signer.SignData(meta))
                : Response(second) }, Path.Combine(root, "replace"), publisher);
            Require(stale.Check(product, "beta", null, token).Unavailable, "wrong release id");
        });
        Test("rate limit defers a second request", () =>
        {
            var limit = new GitHub(new Fake { Respond = (_, __, ___, ____) =>
            {
                pass++;
                return Response(new byte[0], (HttpStatusCode)429);
            } }, Path.Combine(root, "rate"), publisher);
            Fails("NETWORK_LIMIT", () => limit.Check(product, "beta", null, token));
            Fails("NETWORK_LIMIT", () => limit.Check(product, "beta", null, token));
            Require(pass == 1, "no extra request during backoff");
        });
        Test("partial resume verifies ETag, Content-Range and full SHA-256", () =>
        {
            var cache = Path.Combine(root, "cache");
            File.WriteAllBytes(Path.Combine(cache, release.Sha256 + ".part"), payload.Take(141).ToArray());
            Locations.Atomic(Path.Combine(cache, release.Sha256 + ".resume.json"),
                Json.Write(Obj("etag", "\"matching\"", "asset_id", release.AssetId)));
            fake.Respond = (_, __, etag, offset) =>
            {
                Require(offset == 141 && etag == "\"matching\"", "resume request");
                var message = Response(payload.Skip(141).ToArray(), HttpStatusCode.PartialContent, "\"matching\"");
                message.Content.Headers.ContentRange = new ContentRangeHeaderValue(141, payload.Length - 1, payload.Length);
                return message;
            };
            var path = connector.Download(release, _ => {}, token);
            Require(File.ReadAllBytes(path).SequenceEqual(payload), "verified complete file");
        });
        Test("different ETag or unsupported Range restarts safely", () =>
        {
            var cache = Path.Combine(root, "fresh-cache");
            Directory.CreateDirectory(cache);
            File.WriteAllBytes(Path.Combine(cache, release.Sha256 + ".part"), new byte[50]);
            Locations.Atomic(Path.Combine(cache, release.Sha256 + ".resume.json"),
                Json.Write(Obj("etag", "\"obsolete\"", "asset_id", release.AssetId)));
            var fresh = new GitHub(new Fake { Respond = (_, __, ___, ____) => Response(payload, etag: "\"replaced\"") },
                cache, publisher);
            var path = fresh.Download(release, _ => {}, token);
            Require(File.ReadAllBytes(path).SequenceEqual(payload), "no old bytes appended");
        });
        Test("bad payload removed after SHA-256 mismatch", () =>
        {
            var fresh = Path.Combine(root, "bad-cache");
            var broken = new GitHub(new Fake { Respond = (_, __, ___, ____) => Response(new byte[payload.Length]) }, fresh, publisher);
            Fails("PACKAGE_HASH", () => broken.Download(release, _ => {}, token));
            Require(!File.Exists(Path.Combine(fresh, release.Sha256 + ".part")), "bad temporary file removed");
        });
    }

    private static void Catalogs()
    {
        Test("catalog verifies added product and rejects replay", () =>
        {
            var bundled = Path.Combine(root, "catalog");
            var state = Path.Combine(root, "catalog-state");
            Directory.CreateDirectory(bundled);
            var manifest = Path.Combine(bundled, "gassets-catalog.json");
            var signature = Path.Combine(bundled, "gassets-catalog.sig");
            void WriteCatalog(int sequence)
            {
                var data = Json.Write(Obj("schema_version", 1, "sequence", sequence,
                    "expires_at", DateTimeOffset.UtcNow.AddDays(1).ToString("yyyy-MM-dd'T'HH:mm:ss'Z'"),
                    "signing_key_id", publisher.Id, "products", new[]
                    {
                        Obj("product_id", "gassets.sample", "name", "Other", "folder", "Other",
                            "repository", "example/tool", "entry", "other.exe", "platforms", new[] {"windows-x64"},
                            "channels", new[] {"beta"}, "package_format", "GSU-1", "signing_key_id", publisher.Id)
                    }));
                File.WriteAllBytes(manifest, data);
                File.WriteAllBytes(signature, signer.SignData(data));
            }
            WriteCatalog(2);
            var list = Catalog.Open(product, publisher, bundled, state);
            Require(list.Count == 2 && list["gassets.sample"].Repository == "example/tool", "catalog contents");
            WriteCatalog(1);
            Fails("CATALOG_ROLLBACK", () => Catalog.Open(product, publisher, bundled, state));
            var bad = File.ReadAllBytes(signature);
            bad[17] ^= 2;
            File.WriteAllBytes(signature, bad);
            Fails("SIGNATURE_INVALID", () => Catalog.Open(product, publisher, bundled, state));
        });
    }

    public static int Main(string[] args)
    {
        root = Locations.Local(args[0]);
        Directory.CreateDirectory(root);
        using (signer = new ECDsaCng(256) { HashAlgorithm = CngAlgorithm.Sha256 })
        {
            publisher = new Publisher(signer.Key.Export(CngKeyBlobFormat.EccPublicBlob));
            product = new Product("gassets.gtr", "gTR", "Spalov/gTR", "gTR.exe", "gTR", publisher.Id,
                @"Local\gAssets.GauTests." + Guid.NewGuid().ToString("N"), new[] { "beta", "stable" });
            Registry();
            Releases();
            Catalogs();
            if (args.Contains("--public-feed"))
            {
                using (var request = new GitHubTransport())
                {
                    var connector = new GitHub(request, Path.Combine(root, "public-feed"), publisher);
                    var result = connector.Check(product, "beta", null, CancellationToken.None);
                    Console.WriteLine("Public releases API: read completed. Verified candidate " +
                        (result.Release != null) + ". Unsigned releases present " + result.Unavailable + ".");
                }
            }
        }
        Console.WriteLine("GAU / GSU / GitHub mock: " + passed + " passed.");
        return 0;
    }
}
