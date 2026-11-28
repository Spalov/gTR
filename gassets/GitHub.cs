using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Security.Authentication;
using System.Text.RegularExpressions;
using System.Threading;

namespace GAssets
{
    internal sealed class UpdateRelease
    {
        public readonly Product Product;
        public readonly ProductVersion Version;
        public readonly long Sequence, ReleaseId, AssetId, Size, Space;
        public readonly string Tag, Name, Sha256;
        public readonly DateTimeOffset Published, Expires;
        public readonly Dictionary<string, object> Changelog;
        public readonly string MetadataHash;

        public UpdateRelease(Product product, Publisher publisher, byte[] metadata, byte[] signature, bool offline = false)
        {
            var body = publisher.Signed(metadata, signature);
            UpdateError.Require(body.Int("schema_version") == 1 && body.Int("min_gau", 1) <= Gsu.GauVersion, "VERSION_UNSUPPORTED");
            UpdateError.Require(body.Text("product_id") == product.Id && body.Text("repository") == product.Repository &&
                body.Text("signing_key_id") == product.KeyId, "PRODUCT_MISMATCH");
            UpdateError.Require(body.Text("platform") == "windows" && body.Text("architecture") == "x64" &&
                Environment.Is64BitOperatingSystem && Environment.OSVersion.Version.Build >= body.Int("windows_build", 10240, 1000000),
                "PLATFORM_UNSUPPORTED");
            Product = product;
            Version = new ProductVersion(body.Obj("version"));
            UpdateError.Require(product.Channels.Contains(Version.Channel), "CHANNEL_UNSUPPORTED");
            Sequence = body.Int("sequence", 1);
            Published = body.Date("published_at");
            Expires = body.Date("expires_at");
            var now = DateTimeOffset.UtcNow;
            UpdateError.Require(Published <= now.AddMinutes(10) && Expires > now && Expires > Published &&
                Expires - Published < TimeSpan.FromDays(400), "METADATA_EXPIRED");
            var delivery = body.Text("delivery", 12);
            UpdateError.Require(delivery == "github" || offline && delivery == "local", "RELEASE_MISMATCH");
            var files = body.Obj("package");
            ReleaseId = delivery == "github" ? body.Int("github_release_id", 1) : 0;
            AssetId = delivery == "github" ? files.Int("asset_id", 1) : 0;
            Tag = delivery == "github" ? body.Text("github_tag", 120) : "";
            Name = Locations.Relative(files.Text("name", 150));
            UpdateError.Require(!Name.Contains("/") && Name.EndsWith(".gsu", StringComparison.OrdinalIgnoreCase), "PACKAGE_KIND");
            Size = files.Int("size_bytes", 22, Gsu.MaxPackage);
            Sha256 = Hash.Field(files, "sha256");
            Space = body.Int("disk_bytes", 0, Gsu.MaxExpanded + 512L * 1024 * 1024);
            Changelog = body.Obj("changelog");
            MetadataHash = Hash.Bytes(metadata);
        }

        public string Notes(string language)
        {
            if (!Changelog.TryGetValue(language, out var notes) && !Changelog.TryGetValue("en", out notes))
                return "";
            if (notes is string text)
                return Clean(text);
            if (notes is object[] lines)
                return Clean(string.Join("\n", lines.OfType<string>().Take(40)));
            return "";
        }

        private static string Clean(string text) => new string(text.Take(12000)
            .Where(c => c == '\n' || c == '\t' || !char.IsControl(c)).ToArray());
    }

    internal sealed class ReleaseSearch
    {
        public UpdateRelease Release;
        public bool Unavailable;
    }

    internal interface ITransport
    {
        HttpResponseMessage Get(Uri uri, bool asset, string etag, long offset, CancellationToken cancellation);
    }

    internal sealed class GitHubTransport : ITransport, IDisposable
    {
        private readonly HttpClient client = new HttpClient(new HttpClientHandler
        {
            AllowAutoRedirect = false, UseCookies = false, UseDefaultCredentials = false,
            AutomaticDecompression = DecompressionMethods.None, SslProtocols = SslProtocols.Tls12
        }) { Timeout = TimeSpan.FromSeconds(30) };

        public HttpResponseMessage Get(Uri uri, bool asset, string etag, long offset, CancellationToken cancellation)
        {
            for (int hop = 0; hop < 5; hop++)
            {
                UpdateError.Require(Allowed(uri, asset && hop > 0), "NETWORK_SOURCE");
                using (var request = new HttpRequestMessage(HttpMethod.Get, uri))
                {
                    request.Headers.UserAgent.ParseAdd("gAssets-Update-Center/1.0");
                    request.Headers.Accept.ParseAdd(asset ? "application/octet-stream" : "application/vnd.github+json");
                    if (uri.Host == "api.github.com") request.Headers.Add("X-GitHub-Api-Version", "2026-03-10");
                    if (offset > 0)
                    {
                        request.Headers.Range = new RangeHeaderValue(offset, null);
                        request.Headers.IfRange = new RangeConditionHeaderValue(EntityTagHeaderValue.Parse(etag));
                    }
                    else if (!asset && !string.IsNullOrEmpty(etag))
                        request.Headers.IfNoneMatch.Add(EntityTagHeaderValue.Parse(etag));
                    var response = client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellation)
                        .GetAwaiter().GetResult();
                    var code = (int)response.StatusCode;
                    if (code != 301 && code != 302 && code != 303 && code != 307 && code != 308)
                        return response;
                    using (response)
                    {
                        UpdateError.Require(asset && response.Headers.Location != null, "NETWORK_REDIRECT");
                        uri = new Uri(uri, response.Headers.Location);
                    }
                }
            }
            throw new UpdateError("NETWORK_REDIRECT");
        }

        internal static bool Allowed(Uri uri, bool redirect) => uri.IsAbsoluteUri && uri.Scheme == "https" &&
            uri.IsDefaultPort && uri.UserInfo.Length == 0 && uri.Fragment.Length == 0 &&
            (uri.Host == "api.github.com" || redirect &&
                (uri.Host == "release-assets.githubusercontent.com" || uri.Host == "objects.githubusercontent.com"));

        public void Dispose() => client.Dispose();
    }

    internal sealed class GitHub
    {
        private const int Pages = 5;
        private const int PageSize = 20;
        private const int PageLimit = 1024 * 1024;
        private readonly ITransport transport;
        private readonly string cache;
        private readonly Publisher publisher;

        public GitHub(ITransport transport, string cache, Publisher publisher)
        {
            this.transport = transport;
            this.cache = Locations.Local(cache);
            this.publisher = publisher;
        }

        private string Cache(string name) => Locations.Local(Locations.Under(cache, name));
        private static Uri Endpoint(Product p, string resource) => new Uri("https://api.github.com/repos/" + p.Repository + "/" + resource);
        private static Uri Asset(Product p, long id) => Endpoint(p, "releases/assets/" + id.ToString(CultureInfo.InvariantCulture));

        private void CheckThrottle()
        {
            var file = Cache("api-throttle.json");
            if (File.Exists(file))
            {
                var limit = Json.Read(Locations.Read(file, 500)).Int("after");
                UpdateError.Require(DateTimeOffset.UtcNow.ToUnixTimeSeconds() >= limit, "NETWORK_LIMIT");
            }
        }

        private void Status(HttpResponseMessage response)
        {
            int status = (int)response.StatusCode;
            if (status == 403 || status == 429)
            {
                var now = DateTimeOffset.UtcNow;
                long delay = Math.Max(60, (long)(response.Headers.RetryAfter?.Delta?.TotalSeconds ?? 0));
                if (response.Headers.RetryAfter?.Date is DateTimeOffset date)
                    delay = Math.Max(delay, (long)(date - now).TotalSeconds);
                if (response.Headers.TryGetValues("X-RateLimit-Reset", out var reset) &&
                    long.TryParse(reset.FirstOrDefault(), out long until))
                    delay = Math.Max(delay, until - now.ToUnixTimeSeconds());
                Locations.Atomic(Cache("api-throttle.json"), Json.Write(new Dictionary<string, object>
                {
                    {"after", now.ToUnixTimeSeconds() + Math.Min(delay, 86400)}
                }));
                throw new UpdateError("NETWORK_LIMIT");
            }
            UpdateError.Require(response.IsSuccessStatusCode, "HTTP_" + status);
        }

        private static byte[] ReadBody(HttpResponseMessage response, int maximum, CancellationToken cancellation)
        {
            UpdateError.Require(response.Content.Headers.ContentLength.GetValueOrDefault() <= maximum, "SIZE_LIMIT");
            using (var content = response.Content.ReadAsStreamAsync().GetAwaiter().GetResult())
            using (var output = new MemoryStream())
            {
                var buffer = new byte[Math.Min(65536, maximum + 1)];
                int count;
                while ((count = ReadBlock(content, buffer, cancellation)) > 0)
                {
                    UpdateError.Require(output.Length + count <= maximum, "SIZE_LIMIT");
                    output.Write(buffer, 0, count);
                }
                return output.ToArray();
            }
        }

        private static int ReadBlock(Stream stream, byte[] buffer, CancellationToken cancellation)
        {
            using (var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellation))
            {
                deadline.CancelAfter(TimeSpan.FromSeconds(25));
                try { return stream.ReadAsync(buffer, 0, buffer.Length, deadline.Token).GetAwaiter().GetResult(); }
                catch (OperationCanceledException) when (!cancellation.IsCancellationRequested)
                { throw new UpdateError("NETWORK_TIMEOUT"); }
            }
        }

        private object[] Page(Product product, int number, CancellationToken token)
        {
            var path = Cache(product.Id + "-page-" + number + ".json");
            string etag = null;
            byte[] bytes = null;
            if (File.Exists(path))
            {
                try
                {
                    var saved = Json.Read(Locations.Read(path, 2 * PageLimit));
                    if (saved.Text("repository") == product.Repository)
                    {
                        etag = saved.Text("etag", 256);
                        bytes = Convert.FromBase64String(saved.Text("body", 2 * PageLimit));
                        if (!EntityTagHeaderValue.TryParse(etag, out _)) etag = null;
                    }
                }
                catch (Exception ex) when (ex is UpdateError || ex is FormatException)
                { etag = null; }
            }
            CheckThrottle();
            using (var response = transport.Get(Endpoint(product, "releases?per_page=" + PageSize + "&page=" + number),
                false, etag, 0, token))
            {
                if (response.StatusCode == HttpStatusCode.NotModified)
                    UpdateError.Require(etag != null && bytes != null, "NETWORK_CACHE");
                else
                {
                    Status(response);
                    bytes = ReadBody(response, PageLimit, token);
                    UpdateError.Require(Json.Document(bytes) is object[], "JSON_INVALID");
                    if (response.Headers.ETag != null)
                        Locations.Atomic(path, Json.Write(new Dictionary<string, object>
                        {
                            {"etag", response.Headers.ETag.ToString()}, {"body", Convert.ToBase64String(bytes)},
                            {"repository", product.Repository}
                        }));
                }
            }
            return Json.Document(bytes) as object[] ?? throw new UpdateError("JSON_INVALID");
        }

        private byte[] Metadata(Product product, long asset, int maximum, CancellationToken token)
        {
            CheckThrottle();
            using (var response = transport.Get(Asset(product, asset), true, null, 0, token))
            {
                Status(response);
                return ReadBody(response, maximum, token);
            }
        }

        public ReleaseSearch Check(Product product, string channel, ProductVersion high, CancellationToken token)
        {
            UpdateError.Require(product.Channels.Contains(channel), "CHANNEL_UNSUPPORTED");
            var search = new ReleaseSearch();
            var eligible = new List<UpdateRelease>();
            int pairs = 0;
            for (int page = 1; page <= Pages; page++)
            {
                token.ThrowIfCancellationRequested();
                var rows = Page(product, page, token);
                foreach (var raw in rows)
                {
                    if (!(raw is Dictionary<string, object> row) || !row.TryGetValue("draft", out var draft) ||
                        !(draft is bool isDraft) || isDraft)
                        continue;
                    try
                    {
                        var assets = row.Array("assets").OfType<Dictionary<string, object>>().ToList();
                        var manifest = assets.SingleOrDefault(asset => asset.Text("name") == "gassets-release.json");
                        var signature = assets.SingleOrDefault(asset => asset.Text("name") == "gassets-release.sig");
                        if (manifest == null || signature == null)
                        {
                            search.Unavailable = true;
                            continue;
                        }
                        if (++pairs > 12) break;
                        var info = new UpdateRelease(product, publisher,
                            Metadata(product, manifest.Int("id", 1), 96 * 1024, token),
                            Metadata(product, signature.Int("id", 1), 64, token));
                        var package = assets.SingleOrDefault(asset => asset.Int("id", 1) == info.AssetId);
                        UpdateError.Require(info.ReleaseId == row.Int("id", 1) && info.Tag == row.Text("tag_name") &&
                            package != null && package.Text("name") == info.Name && package.Int("size") == info.Size &&
                            (!package.TryGetValue("digest", out var digest) || digest == null ||
                             (string)digest == "sha256:" + info.Sha256), "RELEASE_MISMATCH");
                        if (info.Version.Channel == "stable" && row.TryGetValue("prerelease", out var pre) && pre is bool flag && flag)
                            throw new UpdateError("RELEASE_MISMATCH");
                        if (info.Version.Channel == channel)
                            eligible.Add(info);
                    }
                    catch (UpdateError ex) when (ex.Code != "NETWORK_LIMIT" && ex.Code != "NETWORK_TIMEOUT")
                    { search.Unavailable = true; }
                    catch (InvalidOperationException) { search.Unavailable = true; }
                }
                if (rows.Length < PageSize || pairs > 12) break;
            }
            var best = eligible.OrderByDescending(release => release.Version).ThenByDescending(release => release.Sequence).FirstOrDefault();
            if (best != null)
            {
                Remember(best);
                if (high == null || best.Version.CompareTo(high) >= 0)
                    search.Release = best;
            }
            return search;
        }

        private void Remember(UpdateRelease release)
        {
            string path = Cache(release.Product.Id + "-" + release.Version.Channel + "-trusted.json");
            if (File.Exists(path))
            {
                var old = Json.Read(Locations.Read(path, 4096));
                var version = new ProductVersion(old.Obj("version"));
                UpdateError.Require(release.Version.CompareTo(version) >= 0 && release.Sequence >= old.Int("sequence", 1),
                    "METADATA_ROLLBACK");
                UpdateError.Require(release.Sequence != old.Int("sequence", 1) || release.MetadataHash == old.Text("hash", 64),
                    "METADATA_REPLACED");
            }
            Locations.Atomic(path, Json.Write(new Dictionary<string, object>
            {
                {"version", release.Version.Data}, {"sequence", release.Sequence}, {"hash", release.MetadataHash}
            }));
        }

        private static bool Strong(string value) => EntityTagHeaderValue.TryParse(value, out var parsed) &&
            parsed != EntityTagHeaderValue.Any && !parsed.IsWeak;

        public string Download(UpdateRelease release, Action<long> progress, CancellationToken token)
        {
            UpdateError.Require(release.AssetId > 0 && release.Expires > DateTimeOffset.UtcNow, "METADATA_EXPIRED");
            Directory.CreateDirectory(Locations.Local(cache));
            var file = Cache(release.Sha256 + ".gsu");
            var part = Cache(release.Sha256 + ".part");
            var receipt = Cache(release.Sha256 + ".resume.json");
            if (File.Exists(file))
            {
                if (Matches(file, release)) return file;
                File.Delete(file);
            }
            long offset = File.Exists(part) ? new FileInfo(part).Length : 0;
            if (offset == release.Size && Matches(part, release))
            {
                File.Move(part, file);
                File.Delete(receipt);
                return file;
            }
            string etag = null;
            if (offset > 0 && offset < release.Size && File.Exists(receipt))
            {
                var saved = Json.Read(Locations.Read(receipt, 1024));
                if (saved.Int("asset_id") == release.AssetId && Strong(saved.Text("etag")))
                    etag = saved.Text("etag");
            }
            if (etag == null) offset = 0;
            UpdateError.Require(new DriveInfo(Path.GetPathRoot(part)).AvailableFreeSpace >= release.Size - offset, "DISK_SPACE");
            CheckThrottle();
            try
            {
                using (var response = transport.Get(Asset(release.Product, release.AssetId), true, etag, offset, token))
                {
                    Status(response);
                    var returned = response.Headers.ETag?.ToString();
                    if (response.StatusCode == HttpStatusCode.PartialContent)
                    {
                        var range = response.Content.Headers.ContentRange;
                        UpdateError.Require(offset > 0 && returned == etag && range?.Unit == "bytes" && range.From == offset &&
                            range.To == release.Size - 1 && range.Length == release.Size, "NETWORK_RANGE");
                    }
                    else
                    {
                        UpdateError.Require(response.StatusCode == HttpStatusCode.OK, "NETWORK_RANGE");
                        offset = 0;
                        // Never keep bytes from one response paired with the ETag of another.
                        File.Delete(receipt);
                        File.Delete(part);
                    }
                    UpdateError.Require(!response.Content.Headers.ContentLength.HasValue ||
                        response.Content.Headers.ContentLength.Value == release.Size - offset, "PACKAGE_HASH");
                    if (Strong(returned))
                        Locations.Atomic(receipt, Json.Write(new Dictionary<string, object>
                        { {"etag", returned}, {"asset_id", release.AssetId} }));
                    using (var content = response.Content.ReadAsStreamAsync().GetAwaiter().GetResult())
                    using (var output = new FileStream(part, offset > 0 ? FileMode.Append : FileMode.CreateNew,
                        FileAccess.Write, FileShare.Read, 128 * 1024, FileOptions.SequentialScan))
                    {
                        var buffer = new byte[128 * 1024];
                        int count;
                        progress(offset);
                        while ((count = ReadBlock(content, buffer, token)) > 0)
                        {
                            token.ThrowIfCancellationRequested();
                            offset += count;
                            UpdateError.Require(offset <= release.Size, "PACKAGE_HASH");
                            output.Write(buffer, 0, count);
                            progress(offset);
                        }
                        output.Flush(true);
                    }
                }
                UpdateError.Require(offset == release.Size && Matches(part, release), "PACKAGE_HASH");
                File.Move(part, file);
                File.Delete(receipt);
                return file;
            }
            catch (UpdateError ex) when (ex.Code == "NETWORK_RANGE" || ex.Code == "HTTP_416" || ex.Code == "PACKAGE_HASH")
            {
                Forget(release);
                throw;
            }
        }

        private static bool Matches(string path, UpdateRelease release)
        {
            using (var stream = File.OpenRead(Locations.Local(path)))
                return stream.Length == release.Size && Hash.Stream(stream) == release.Sha256;
        }

        public void Forget(UpdateRelease release)
        {
            foreach (var name in new[] {release.Sha256 + ".part", release.Sha256 + ".resume.json"})
            {
                var path = Cache(name);
                if (File.Exists(path)) File.Delete(path);
            }
        }
    }
}
