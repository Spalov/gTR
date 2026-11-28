using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace GAssets
{
    internal static class Catalog
    {
        public static IReadOnlyDictionary<string, Product> Open(Product builtIn, Publisher publisher, string bundled, string state)
        {
            var products = new Dictionary<string, Product>(StringComparer.Ordinal) { { builtIn.Id, builtIn } };
            var source = Path.Combine(bundled, "gassets-catalog.json");
            var signature = Path.Combine(bundled, "gassets-catalog.sig");
            if (!File.Exists(source) || !File.Exists(signature)) return products;
            var bytes = Locations.Read(source, 128 * 1024);
            var verified = publisher.Signed(bytes, Locations.Read(signature, 64));
            UpdateError.Require(verified.Int("schema_version") == 1, "CATALOG_INVALID");
            if (verified.Date("expires_at") <= DateTimeOffset.UtcNow) return products;
            var hash = Hash.Bytes(bytes);
            var sequence = verified.Int("sequence", 1);
            var receipt = Path.Combine(state, "catalog-version.json");
            if (File.Exists(receipt))
            {
                var old = Json.Read(Locations.Read(receipt, 4096));
                UpdateError.Require(sequence >= old.Int("sequence", 1) &&
                    (sequence != old.Int("sequence", 1) || hash == old.Text("sha256", 64)), "CATALOG_ROLLBACK");
            }
            var folders = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { builtIn.Folder, "UpdateCenter" };
            var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { builtIn.Name };
            var listed = verified.Array("products");
            UpdateError.Require(listed.Length >= 1 && listed.Length <= 40, "CATALOG_INVALID");
            foreach (var item in listed)
            {
                var row = item as Dictionary<string, object> ?? throw new UpdateError("CATALOG_INVALID");
                var id = row.Text("product_id", 64);
                var folder = row.Text("folder", 80);
                var repo = row.Text("repository");
                var entry = row.Text("entry", 100);
                var channels = row.Array("channels").OfType<string>().Distinct().ToArray();
                UpdateError.Require(row.Text("package_format") == "GSU-1" &&
                    row.Array("platforms").OfType<string>().Contains("windows-x64") &&
                    row.Text("signing_key_id") == publisher.Id && channels.Length > 0 &&
                    channels.All(channel => new[] { "stable", "rc", "beta", "experimental" }.Contains(channel)) &&
                    !folder.Contains("/") && entry.EndsWith(".exe", StringComparison.OrdinalIgnoreCase), "CATALOG_INVALID");
                if (id == builtIn.Id)
                {
                    UpdateError.Require(folder == builtIn.Folder && repo == builtIn.Repository && entry == builtIn.Entry, "CATALOG_INVALID");
                    continue;
                }
                var name = row.Text("name", 60);
                UpdateError.Require(!products.ContainsKey(id) && folders.Add(folder) && names.Add(name), "CATALOG_INVALID");
                products[id] = new Product(id, name, repo, entry, folder, publisher.Id,
                    @"Local\" + id + ".install", channels);
            }
            Locations.Atomic(receipt, Json.Write(new Dictionary<string, object> { {"sequence", sequence}, {"sha256", hash} }));
            return products;
        }
    }
}
