using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Threading;

namespace GAssets
{
    internal sealed class InstallRecord
    {
        public readonly ProductVersion Version;
        public readonly Dictionary<string, object> Manifest;
        public readonly string Path;

        public InstallRecord(string path, Product product, Publisher publisher, bool checkFiles)
        {
            Path = path;
            Manifest = ReadManifest(path, product, publisher);
            Version = new ProductVersion(Manifest.Obj("version"));
            foreach (var item in Manifest.Array("files"))
            {
                var entry = new FileSpec((Dictionary<string, object>)item);
                var name = new FileInfo(Locations.Under(path, entry.Path));
                UpdateError.Require(name.Exists && name.Length == entry.Size &&
                    (name.Attributes & FileAttributes.ReparsePoint) == 0, "INSTALL_DAMAGED");
                if (checkFiles || entry.Path == product.Entry)
                {
                    using (var input = File.OpenRead(Locations.Local(name.FullName)))
                        UpdateError.Require(Hash.Stream(input) == entry.Sha256, "INSTALL_DAMAGED");
                }
            }
        }

        public static Dictionary<string, object> ReadManifest(string path, Product product, Publisher publisher)
        {
            var value = publisher.Signed(Locations.Read(Locations.Under(path, ".gassets-manifest.json"), Gsu.ManifestLimit),
                                        Locations.Read(Locations.Under(path, ".gassets-manifest.sig"), 64));
            UpdateError.Require(value.Int("schema_version") == 1 && value.Text("product_id") == product.Id &&
                value.Text("architecture") == "x64" && value.Text("entry") == product.Entry &&
                value.Text("signing_key_id") == product.KeyId, "PRODUCT_MISMATCH");
            return value;
        }
    }

    internal sealed class Gau
    {
        public const int AppControlExit = 43;
        private readonly Publisher publisher;
        private readonly string programs;
        public readonly string StateRoot, DataRoot;

        public Gau(string programs, string state, string data, Publisher publisher)
        {
            this.publisher = publisher;
            this.programs = Locations.Local(programs);
            StateRoot = Locations.Local(state);
            DataRoot = Locations.Local(data);
            UpdateError.Require(!Contains(this.programs, DataRoot) && !Contains(DataRoot, this.programs) &&
                                !Contains(this.programs, StateRoot) && !Contains(StateRoot, this.programs), "PATH_INVALID");
            Directory.CreateDirectory(StateRoot);
        }

        private static bool Contains(string root, string path) =>
            (path.TrimEnd(Path.DirectorySeparatorChar) + "\\").StartsWith(
                root.TrimEnd(Path.DirectorySeparatorChar) + "\\", StringComparison.OrdinalIgnoreCase);

        public string ProgramRoot(Product product) => Locations.Local(Locations.Under(programs, product.Folder));
        public string Entry(Product product) => Locations.Local(Locations.Under(ProgramRoot(product), "current/" + product.Entry));
        private string Receipt(Product product) => Locations.Under(StateRoot, "products/" + product.Id + ".json");
        private string Transaction(Product product) => Locations.Under(StateRoot, "transactions/" + product.Id + ".json");
        private string History(Product product) => Locations.Under(StateRoot, "history/" + product.Id + ".json");
        private static string At(string root, string name) => Locations.Local(Locations.Under(root, name));
        private static Dictionary<string, object> Read(string path) => Json.Read(Locations.Read(path, 1024 * 1024));

        public InstallRecord Installed(Product product, bool allFiles = false)
        {
            var root = ProgramRoot(product);
            if (!Directory.Exists(At(root, "current"))) return null;
            RequireOwned(root, product);
            return new InstallRecord(At(root, "current"), product, publisher, allFiles);
        }

        public Dictionary<string, object> Registration(Product product) =>
            File.Exists(Receipt(product)) ? Read(Receipt(product)) : new Dictionary<string, object>();

        public ProductVersion HighWater(Product product)
        {
            var entry = Registration(product);
            return entry.ContainsKey("highest_version") ? new ProductVersion(entry.Obj("highest_version")) : null;
        }

        public void Record(Product product, string state, string version, string result)
        {
            var path = History(product);
            var history = File.Exists(path) ? Read(path).Array("events") : new object[0];
            Locations.Atomic(path, Json.Write(new Dictionary<string, object>
            {
                {"events", history.Skip(Math.Max(0, history.Length - 199)).Concat(new object[]
                {
                    new Dictionary<string, object>
                    {
                        {"product", product.Id}, {"state", state}, {"time", Stamp()},
                        {"version", version}, {"result", result}
                    }
                }).ToArray()}
            }));
        }

        public object[] Events(Product product) => File.Exists(History(product)) ? Read(History(product)).Array("events") : new object[0];

        private static string Stamp() => DateTimeOffset.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", CultureInfo.InvariantCulture);

        private void Register(Product product, ProductVersion version, bool installed, bool backup)
        {
            var high = HighWater(product);
            if (high == null || version.CompareTo(high) > 0)
                high = version;
            Locations.Atomic(Receipt(product), Json.Write(new Dictionary<string, object>
            {
                {"schema_version", 1}, {"product_id", product.Id}, {"name", product.Name}, {"version", version.Data},
                {"highest_version", high.Data}, {"architecture", "x64"}, {"directory", ProgramRoot(product)},
                {"entry", Entry(product)}, {"user_data", DataRoot}, {"repository", product.Repository},
                {"signing_key_id", product.KeyId}, {"state", installed ? "installed" : "removed"},
                {"rollback", backup}, {"updated", Stamp()}
            }));
        }

        private static void RequireOwned(string root, Product product)
        {
            var marker = At(root, ".gassets-product");
            UpdateError.Require(File.Exists(marker) && Json.Utf8.GetString(Locations.Read(marker, 90)) == product.Id, "INSTALL_LOCATION");
        }

        private static void EnsureOwned(string root, Product product)
        {
            if (!Directory.Exists(root))
                Directory.CreateDirectory(root);
            if (!Directory.EnumerateFileSystemEntries(root).Any())
                Locations.Atomic(At(root, ".gassets-product"), Json.Utf8.GetBytes(product.Id));
            RequireOwned(root, product);
        }

        private void Phase(Product product, string phase, bool hadCurrent, string action)
        {
            Locations.Atomic(Transaction(product), Json.Write(new Dictionary<string, object>
            {
                {"phase", phase}, {"had_current", hadCurrent}, {"operation", action}, {"updated", Stamp()},
                {"root", ProgramRoot(product)}
            }));
        }

        // Called only under the product mutex. Reparse points in an owned tree
        // block the operation instead of redirecting a deletion.
        private static void RemoveTree(string root)
        {
            Locations.Local(root);
            if (!Directory.Exists(root)) return;
            foreach (var path in Directory.EnumerateFileSystemEntries(root))
            {
                Locations.Local(path);
                var attributes = File.GetAttributes(path);
                if ((attributes & FileAttributes.Directory) != 0)
                    RemoveTree(path);
                else
                    File.Delete(path);
            }
            Directory.Delete(root);
        }

        private void CheckOnlyProgramFiles(string root, Product product)
        {
            if (!Directory.Exists(root)) return;
            var manifest = InstallRecord.ReadManifest(root, product, publisher);
            var names = new HashSet<string>(manifest.Array("files")
                .Select(item => new FileSpec((Dictionary<string, object>)item).Path), StringComparer.OrdinalIgnoreCase)
            {
                ".gassets-manifest.json", ".gassets-manifest.sig"
            };
            var folders = new Stack<string>();
            folders.Push(root);
            while (folders.Count > 0)
            {
                foreach (var path in Directory.EnumerateFileSystemEntries(folders.Pop()))
                {
                    Locations.Local(path);
                    if (Directory.Exists(path)) folders.Push(path);
                    else UpdateError.Require(names.Contains(path.Substring(root.Length + 1).Replace('\\', '/')), "UNMANAGED_FILES");
                }
            }
        }

        private static void WithProduct(Product product, Action work)
        {
            using (var mutex = new Mutex(false, product.Mutex))
            {
                bool locked;
                try { locked = mutex.WaitOne(0); }
                catch (AbandonedMutexException) { locked = true; }
                UpdateError.Require(locked, "PRODUCT_RUNNING");
                try { work(); }
                finally { mutex.ReleaseMutex(); }
            }
        }

        public void Recover(Product product)
        {
            if (!File.Exists(Transaction(product))) return;
            WithProduct(product, () => RecoverLocked(product));
        }

        private void RecoverLocked(Product product)
        {
            var journal = Transaction(product);
            if (!File.Exists(journal)) return;
            var root = ProgramRoot(product);
            RequireOwned(root, product);
            var state = Read(journal);
            UpdateError.Require(string.Equals(state.Text("root", 32767), root, StringComparison.OrdinalIgnoreCase), "INSTALL_LOCATION");
            var phase = state.Text("phase", 40);
            var operation = state.Text("operation", 40);
            UpdateError.Require(state.TryGetValue("had_current", out var had) && had is bool, "JOURNAL_INVALID");
            var current = At(root, "current");
            var old = At(root, ".old");
            var stage = At(root, ".stage");
            if (phase == "COMMITTING")
            {
                // A health check was durably acknowledged. Complete the commit.
                Commit(product, root, operation);
                return;
            }
            UpdateError.Require(new[] { "VERIFIED", "STAGED", "INSTALLING", "SWITCHED", "HEALTH_CHECK" }.Contains(phase),
                "JOURNAL_INVALID");
            if (Directory.Exists(old))
            {
                RemoveTree(current);
                Directory.Move(old, current);
            }
            else if (!(bool)had)
            {
                RemoveTree(current);
            }
            RemoveTree(stage);
            File.Delete(journal);
            Record(product, "ROLLBACK_REQUIRED", Installed(product)?.Version.ToString() ?? "", "RESTORED");
        }

        private void Commit(Product product, string root, string operation)
        {
            var old = At(root, ".old");
            var previous = At(root, "previous");
            if (Directory.Exists(old))
            {
                RemoveTree(previous);
                Directory.Move(old, previous);
            }
            var verified = Installed(product);
            UpdateError.Require(verified != null, "INSTALL_DAMAGED");
            Register(product, verified.Version, true, Directory.Exists(previous));
            Record(product, operation, verified.Version.ToString(), "COMMITTED");
            File.Delete(Transaction(product));
        }

        private void Switch(Product product, string root, bool existing, string action, Func<string, bool> health)
        {
            string current = At(root, "current"), old = At(root, ".old"), stage = At(root, ".stage");
            Phase(product, "INSTALLING", existing, action);
            if (existing)
                Directory.Move(current, old);
            Directory.Move(stage, current);
            Phase(product, "SWITCHED", existing, action);
            Phase(product, "HEALTH_CHECK", existing, action);
            UpdateError.Require(health(Entry(product)), "HEALTH_FAILED");
            Phase(product, "COMMITTING", existing, action);
            Commit(product, root, action);
        }

        public void Install(Product product, string source, UpdateRelease release, CancellationToken cancellation,
                            Func<string, bool> health = null)
        {
            UpdateError.Require(!product.SelfUpdate, "BOOTSTRAP_REQUIRED");
            WithProduct(product, () =>
            {
                RecoverLocked(product);
                var root = ProgramRoot(product);
                var existingRegistration = Registration(product);
                if (existingRegistration.TryGetValue("state", out var state) && (string)state == "installed")
                    UpdateError.Require(string.Equals(existingRegistration.Text("directory", 32767), root,
                        StringComparison.OrdinalIgnoreCase), "INSTALL_LOCATION");
                using (var package = new Gsu(source, product, publisher, release?.Size ?? 0, release?.Sha256))
                {
                    if (release != null)
                        UpdateError.Require(release.Product.Id == product.Id && release.Version.Matches(package.Version) &&
                            release.Expires > DateTimeOffset.UtcNow, "RELEASE_MISMATCH");
                    var high = HighWater(product);
                    try
                    {
                        var active = Installed(product)?.Version;
                        if (active != null && (high == null || active.CompareTo(high) > 0)) high = active;
                    }
                    catch (UpdateError ex) when (ex.Code == "INSTALL_DAMAGED" && high != null) {}
                    UpdateError.Require(high == null || package.Version.CompareTo(high) >= 0, "VERSION_DOWNGRADE");
                    UpdateError.Require(high == null || package.Version.CompareTo(high) != 0 || package.Version.Matches(high),
                        "VERSION_REPLACED");
                    if (package.Kind == "repair")
                        UpdateError.Require(high != null && package.Version.Matches(high), "PACKAGE_KIND");
                    UpdateError.Require(new DriveInfo(Path.GetPathRoot(root)).AvailableFreeSpace >= package.Space, "DISK_SPACE");
                    cancellation.ThrowIfCancellationRequested();
                    EnsureOwned(root, product);
                    var existing = Directory.Exists(At(root, "current"));
                    CheckOnlyProgramFiles(At(root, "previous"), product);
                    var stage = At(root, ".stage");
                    var action = !existing ? "INSTALL" : high != null && package.Version.Matches(high) ? "REPAIR" : "UPDATE";
                    UpdateError.Require(!Directory.Exists(At(root, ".old")) && !Directory.Exists(stage), "JOURNAL_INVALID");
                    Phase(product, "VERIFIED", existing, action);
                    try
                    {
                        Directory.CreateDirectory(stage);
                        package.Extract(stage, cancellation.ThrowIfCancellationRequested);
                        Locations.Atomic(At(stage, ".gassets-manifest.json"), package.Manifest);
                        Locations.Atomic(At(stage, ".gassets-manifest.sig"), package.Signature);
                        Phase(product, "STAGED", existing, action);
                        cancellation.ThrowIfCancellationRequested();
                        Switch(product, root, existing, action, health ?? Health);
                    }
                    catch
                    {
                        RecoverLocked(product);
                        throw;
                    }
                }
            });
        }

        public void Rollback(Product product, Func<string, bool> health = null)
        {
            WithProduct(product, () =>
            {
                RecoverLocked(product);
                var root = ProgramRoot(product);
                RequireOwned(root, product);
                var previous = At(root, "previous");
                var stage = At(root, ".stage");
                var backup = new InstallRecord(previous, product, publisher, true);
                var current = Installed(product);
                UpdateError.Require(current != null && current.Manifest.Int("data_schema", 1) ==
                    backup.Manifest.Int("data_schema", 1), "ROLLBACK_INCOMPATIBLE");
                CheckOnlyProgramFiles(previous, product);
                Phase(product, "VERIFIED", true, "ROLLBACK");
                try
                {
                    Directory.CreateDirectory(stage);
                    foreach (var file in backup.Manifest.Array("files").Select(raw => new FileSpec((Dictionary<string, object>)raw))
                                 .Select(file => file.Path).Concat(new[] { ".gassets-manifest.json", ".gassets-manifest.sig" }))
                    {
                        var target = At(stage, file);
                        Directory.CreateDirectory(Path.GetDirectoryName(target));
                        File.Copy(At(previous, file), target, false);
                    }
                    new InstallRecord(stage, product, publisher, true);
                    Switch(product, root, true, "ROLLBACK", health ?? Health);
                }
                catch
                {
                    RecoverLocked(product);
                    throw;
                }
            });
        }

        public void Uninstall(Product product)
        {
            WithProduct(product, () =>
            {
                RecoverLocked(product);
                var root = ProgramRoot(product);
                RequireOwned(root, product);
                var version = HighWater(product);
                UpdateError.Require(version != null, "INSTALL_LOCATION");
                CheckOnlyProgramFiles(At(root, "current"), product);
                CheckOnlyProgramFiles(At(root, "previous"), product);
                RemoveTree(At(root, "current"));
                RemoveTree(At(root, "previous"));
                Register(product, version, false, false);
                Record(product, "REMOVE", version.ToString(), "COMMITTED");
            });
        }

        private static bool Health(string executable)
        {
            try
            {
                using (var process = Process.Start(new ProcessStartInfo(executable, "--health-check")
                {
                    WorkingDirectory = Path.GetDirectoryName(executable), UseShellExecute = false, CreateNoWindow = true
                }))
                {
                    if (process == null) return false;
                    if (process.WaitForExit(25000))
                    {
                        UpdateError.Require(process.ExitCode != AppControlExit, "APP_CONTROL");
                        return process.ExitCode == 0;
                    }
                    process.Kill();
                    process.WaitForExit(3000);
                    return false;
                }
            }
            catch (Win32Exception error) when (error.NativeErrorCode == 4551 || error.NativeErrorCode == 577)
            {
                throw new UpdateError("APP_CONTROL");
            }
        }
    }
}
