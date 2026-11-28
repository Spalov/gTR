using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace GAssets
{
    internal sealed class Center : Form
    {
        private readonly Runtime runtime;
        private readonly bool offline, ukrainian;
        private readonly Label status = new Label { Dock = DockStyle.Fill, AutoEllipsis = true, TextAlign = ContentAlignment.MiddleLeft };
        private readonly Label library = new Label { Dock = DockStyle.Top, Height = 180 };
        private readonly Label offer = new Label { Dock = DockStyle.Top, Height = 122 };
        private readonly TextBox notes = new TextBox { Dock = DockStyle.Fill, ReadOnly = true, Multiline = true, ScrollBars = ScrollBars.Vertical };
        private readonly ProgressBar progress = new ProgressBar { Dock = DockStyle.Top, Height = 18, Maximum = 1000 };
        private readonly Label size = new Label { Dock = DockStyle.Top, Height = 40 };
        private readonly ListView history = new ListView { Dock = DockStyle.Fill, View = View.Details, FullRowSelect = true };
        private readonly TabControl tabs = new TabControl { Dock = DockStyle.Fill };
        private readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        private readonly List<Button> regular = new List<Button>();
        private Button pause, cancel, resume, update, download, skip, defer, launch, repair, rollback, remove;
        private TabPage updatePage, downloadsPage;
        private UpdateRelease release, pending;
        private InstallRecord installed;
        private CancellationTokenSource work;
        private bool removePartial;
        private bool installAfterDownload;
        private DateTimeOffset checking;
        private string channel;
        private readonly Color background = Color.FromArgb(20, 26, 30);
        private readonly Color surface = Color.FromArgb(30, 40, 46);
        private readonly Color foreground = Color.FromArgb(225, 234, 237);

        public Center(Runtime runtime, bool offline, bool deferFirstCheck = false)
        {
            this.runtime = runtime;
            this.offline = offline;
            ukrainian = runtime.Language == "uk";
            channel = runtime.Setting("channel", "beta");
            if (!runtime.Main.Channels.Contains(channel)) channel = "beta";
            Text = Runtime.Test ? T("gAssets · тестовая поставка", "gAssets · тестовий комплект") : "gAssets Update Center";
            Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            Font = new Font("Segoe UI", 10);
            AutoScaleMode = AutoScaleMode.Dpi;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(850, 540);
            MinimumSize = new Size(690, 490);
            BackColor = background;
            ForeColor = foreground;
            AllowDrop = true;
            DragEnter += (_, e) =>
            {
                if (work == null && e.Data.GetData(DataFormats.FileDrop) is string[] files && files.Length == 1 &&
                    files[0].EndsWith(".gsu", StringComparison.OrdinalIgnoreCase))
                    e.Effect = DragDropEffects.Copy;
            };
            DragDrop += (_, e) =>
            {
                if (e.Data.GetData(DataFormats.FileDrop) is string[] paths && paths.Length == 1) OfferPackage(paths[0]);
            };
            var grid = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 3, ColumnCount = 1, Padding = new Padding(14) };
            grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            grid.RowStyles.Add(new RowStyle(SizeType.Absolute, 50));
            grid.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            grid.RowStyles.Add(new RowStyle(SizeType.Absolute, 32));
            var heading = new Label
            {
                Text = Text, Dock = DockStyle.Fill, ForeColor = Color.FromArgb(126, 208, 231),
                Font = new Font(Font.FontFamily, 18, FontStyle.Bold)
            };
            grid.Controls.Add(heading, 0, 0);
            grid.Controls.Add(tabs, 0, 1);
            grid.Controls.Add(status, 0, 2);
            Controls.Add(grid);

            var products = Page(T("Библиотека", "Бібліотека"));
            var libraryButtons = Buttons();
            launch = Button(libraryButtons, T("Запустить gTR", "Запустити gTR"), Launch);
            Button(libraryButtons, T("Папка установки", "Тека встановлення"), OpenFolder);
            Button(libraryButtons, T("Открыть GSU…", "Відкрити GSU…"), ChoosePackage);
            repair = Button(libraryButtons, T("Восстановить", "Відновити"), () =>
            {
                if (release != null) BeginDownload(true);
                else ChoosePackage();
            });
            rollback = Button(libraryButtons, T("Откатить", "Відкотити"), OfferRollback);
            remove = Button(libraryButtons, T("Удалить приложение", "Видалити застосунок"), OfferRemove);
            products.Controls.Add(libraryButtons);
            products.Controls.Add(library);

            updatePage = Page(T("Обновления", "Оновлення"));
            var updateButtons = Buttons();
            Button(updateButtons, T("Проверить", "Перевірити"), Check);
            update = Button(updateButtons, T("Скачать и установить", "Завантажити й установити"), () => BeginDownload(true));
            download = Button(updateButtons, T("Только скачать", "Лише завантажити"), () => BeginDownload(false));
            skip = Button(updateButtons, T("Пропустить", "Пропустити"), () => Postpone(true));
            defer = Button(updateButtons, T("Отложить на сутки", "Відкласти на добу"), () => Postpone(false));
            updatePage.Controls.Add(notes);
            updatePage.Controls.Add(updateButtons);
            updatePage.Controls.Add(offer);

            downloadsPage = Page(T("Загрузки", "Завантаження"));
            var downloads = Buttons();
            pause = Button(downloads, T("Приостановить", "Призупинити"), () => work?.Cancel(), false);
            cancel = Button(downloads, T("Отменить", "Скасувати"), () =>
            {
                removePartial = true;
                if (work != null) work.Cancel();
                else ClearPending();
            }, false);
            resume = Button(downloads, T("Продолжить", "Продовжити"), () =>
            {
                if (pending != null) DownloadPending();
            }, false);
            downloadsPage.Controls.Add(downloads);
            downloadsPage.Controls.Add(size);
            downloadsPage.Controls.Add(progress);

            var past = Page(T("История", "Історія"));
            foreach (var column in new[] { T("Дата", "Дата"), T("Версия", "Версія"), T("Операция", "Операція"), T("Результат", "Результат") })
                history.Columns.Add(column, 162);
            var historyButtons = Buttons();
            Button(historyButtons, T("Сохранить обезличенный отчёт…", "Зберегти знеособлений звіт…"), ExportHistory);
            past.Controls.Add(history);
            past.Controls.Add(historyButtons);
            Settings(Page(T("Настройки", "Налаштування")));

            timer.Interval = Interval();
            timer.Tick += (_, __) => Check(false);
            Shown += (_, __) =>
            {
                RefreshState();
                if (!offline)
                {
                    timer.Start();
                    if (!deferFirstCheck) Check(false);
                }
            };
            FormClosing += (_, e) =>
            {
                if (work == null) return;
                e.Cancel = true;
                status.Text = T("Дождитесь завершения текущего действия.", "Дочекайтеся завершення поточної дії.");
            };
        }

        private string T(string ru, string uk) => ukrainian ? uk : ru;

        private TabPage Page(string text)
        {
            var page = new TabPage(text) { BackColor = background, ForeColor = foreground, Padding = new Padding(14) };
            tabs.TabPages.Add(page);
            return page;
        }

        private static FlowLayoutPanel Buttons() => new FlowLayoutPanel
        {
            Dock = DockStyle.Top, Height = 76, WrapContents = true, Padding = new Padding(0, 5, 0, 4)
        };

        private Button Button(FlowLayoutPanel panel, string text, Action run, bool normal = true)
        {
            var button = new Button
            {
                Text = text, AutoSize = true, MinimumSize = new Size(86, 31), FlatStyle = FlatStyle.Flat,
                BackColor = surface, ForeColor = foreground, Margin = new Padding(0, 1, 8, 5), Padding = new Padding(6, 0, 6, 0)
            };
            button.FlatAppearance.BorderColor = Color.FromArgb(67, 82, 90);
            button.Click += (_, __) => run();
            panel.Controls.Add(button);
            if (normal) regular.Add(button);
            return button;
        }

        private int Interval() => int.TryParse(runtime.Setting("interval", "60"), out int value)
            ? Math.Max(15, Math.Min(1440, value)) * 60000 : 3600000;

        private void Settings(TabPage page)
        {
            var box = new FlowLayoutPanel { Dock = DockStyle.Top, Height = 250, FlowDirection = FlowDirection.TopDown, WrapContents = false };
            var channels = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 240 };
            channels.Items.AddRange(new object[] { "Stable", "Release Candidate", "Beta" });
            channels.SelectedIndex = Math.Max(0, Array.IndexOf(runtime.Main.Channels, channel));
            var interval = new NumericUpDown { Width = 120, Minimum = 15, Maximum = 1440, Value = Interval() / 60000 };
            var language = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 240 };
            language.Items.AddRange(new object[] { "Русский", "Українська" });
            language.SelectedIndex = ukrainian ? 1 : 0;
            var autoDownload = new CheckBox
            {
                AutoSize = true, Text = T("Скачивать после проверки подписи", "Завантажувати після перевірки підпису"),
                Checked = runtime.Setting("auto_download", "no") == "yes"
            };
            box.Controls.Add(new Label { Text = T("Канал gTR", "Канал gTR"), AutoSize = true });
            box.Controls.Add(channels);
            box.Controls.Add(new Label { Text = T("Интервал проверки, минут", "Інтервал перевірки, хвилин"), AutoSize = true });
            box.Controls.Add(interval);
            box.Controls.Add(new Label { Text = T("Язык", "Мова"), AutoSize = true });
            box.Controls.Add(language);
            box.Controls.Add(autoDownload);
            var buttons = Buttons();
            Button(buttons, T("Сохранить", "Зберегти"), () =>
            {
                var selected = runtime.Main.Channels[channels.SelectedIndex];
                if (selected != channel && !Confirm(T("Перейти на другой канал? Понижение версии требует отдельного отката.",
                                                      "Перейти на інший канал? Пониження версії потребує окремого відкоту.")))
                    return;
                channel = selected;
                var settings = runtime.Settings();
                settings["channel"] = channel;
                settings["interval"] = interval.Value.ToString(System.Globalization.CultureInfo.InvariantCulture);
                settings["language"] = language.SelectedIndex == 1 ? "uk" : "ru";
                settings["auto_download"] = autoDownload.Checked ? "yes" : "no";
                runtime.Save(settings);
                timer.Interval = Interval();
                release = null;
                RefreshState();
                status.Text = T("Сохранено. Язык применяется при следующем запуске.", "Збережено. Мова застосовується під час наступного запуску.");
            });
            Button(buttons, T("Прежние установки…", "Попередні встановлення…"), Legacy);
            page.Controls.Add(new Label
            {
                Dock = DockStyle.Bottom, Height = 85, ForeColor = foreground,
                Text = "gAssets Update Center 1.0 · GAU 1 · GSU 1\nGruzin\n" +
                    (Runtime.Test ? T("Изолированное тестовое доверие. Подпись издателя не настроена.",
                                      "Ізольована тестова довіра. Підпис видавця не налаштований.") : "")
            });
            page.Controls.Add(buttons);
            page.Controls.Add(box);
        }

        private async Task Run(Func<CancellationToken, Task> operation)
        {
            if (work != null) return;
            work = new CancellationTokenSource();
            Enable();
            try { await operation(work.Token); }
            catch (OperationCanceledException)
            { status.Text = removePartial ? T("Загрузка отменена.", "Завантаження скасовано.") : T("Загрузка приостановлена.", "Завантаження призупинено."); }
            catch (Exception error)
            { status.Text = Error(error, ukrainian); }
            finally
            {
                work.Dispose();
                work = null;
                if (removePartial) ClearPending();
                RefreshState();
            }
        }

        private void Check() => Check(true);

        private void Check(bool showTab)
        {
            if (offline) { status.Text = T("Локальный режим. Доступна установка GSU с диска.", "Локальний режим. Доступне встановлення GSU з диска."); return; }
            if (work != null) return;
            if (showTab) tabs.SelectedTab = updatePage;
            status.Text = T("Проверка подписанных выпусков GitHub…", "Перевірка підписаних випусків GitHub…");
            _ = Run(async token =>
            {
                var result = await Task.Run(() => runtime.Connector.Check(runtime.Main, channel, runtime.Gau.HighWater(runtime.Main), token), token);
                checking = DateTimeOffset.Now;
                release = result.Release;
                bool newVersion = release != null && (installed == null || release.Version.CompareTo(installed.Version) > 0);
                status.Text = newVersion ? T("Доступно обновление.", "Доступне оновлення.")
                    : result.Unavailable && release == null
                    ? T("Выпуск недоступен для автоматической установки.", "Випуск недоступний для автоматичного встановлення.")
                    : T("Совместимых обновлений нет.", "Сумісних оновлень немає.");
                Locations.Atomic(Path.Combine(runtime.State, "gtr-update-status.json"), Json.Write(new Dictionary<string, object>
                {
                    {"version", release?.Version.ToString() ?? ""}, {"checked", checking.ToUniversalTime().ToString("o")},
                    {"available", newVersion}, {"trust", Runtime.Test ? "test" : "publisher"}
                }));
            }).ContinueWith(_ =>
            {
                if (!IsDisposed && IsHandleCreated)
                    BeginInvoke(new Action(() =>
                    {
                        if (release != null && Available() && pending == null && runtime.Setting("auto_download", "no") == "yes")
                            BeginDownload(false, false);
                    }));
            }, TaskScheduler.Default);
        }

        private void RefreshState()
        {
            string state = T("Не установлено", "Не встановлено");
            installed = null;
            try
            {
                installed = runtime.Gau.Installed(runtime.Main);
                if (installed != null) state = T("Готово к запуску", "Готово до запуску");
            }
            catch (Exception error) when (error is UpdateError || error is IOException)
            { state = T("Требуется восстановление", "Потрібне відновлення"); }
            var reg = runtime.Gau.Registration(runtime.Main);
            var last = reg.TryGetValue("updated", out var stamp) ? stamp as string : "";
            library.Text = "gTR · Gruzin’s Telegram Recovery\nGruzin\n\n" + state + "\n" +
                T("Установлено · ", "Встановлено · ") + (installed?.Version.ToString() ?? "—") +
                T("    Доступно · ", "    Доступно · ") + (release?.Version.ToString() ?? "—") +
                T("    Канал · ", "    Канал · ") + channel +
                "\n" + runtime.Gau.ProgramRoot(runtime.Main) + "\n" +
                T("Последнее изменение · ", "Остання зміна · ") + (last ?? "");
            updatePage.Text = T("Обновления", "Оновлення") + (Available() ? " (1)" : "");
            offer.Text = release == null ? T("Подписанный выпуск пока недоступен.\nОфициальный источник · Spalov/gTR",
                                             "Підписаний випуск поки недоступний.\nОфіційне джерело · Spalov/gTR")
                : "gTR " + release.Version + " · " + release.Published.LocalDateTime.ToString("g") + "\n" +
                    T("Подпись проверена · ", "Підпис перевірено · ") + (Runtime.Test ? T("тестовый ключ", "тестовий ключ") : "Gruzin") +
                    "\n" + T("Загрузка · ", "Завантаження · ") + Bytes(release.Size) +
                    T("    Место на диске · ", "    Місце на диску · ") + Bytes(release.Space) + "\nWindows x64 · Spalov/gTR";
            notes.Text = release?.Notes(ukrainian ? "uk" : "ru") ?? "";
            history.Items.Clear();
            foreach (var value in runtime.Gau.Events(runtime.Main).Reverse().OfType<Dictionary<string, object>>())
                history.Items.Add(new ListViewItem(new[] { value.Text("time"), value.Text("version"),
                    HistoryText(value.Text("state")), HistoryText(value.Text("result")) }));
            Enable();
        }

        private string HistoryText(string value)
        {
            switch (value)
            {
                case "INSTALL": return T("Установка", "Встановлення");
                case "UPDATE": return T("Обновление", "Оновлення");
                case "REPAIR": return T("Восстановление", "Відновлення");
                case "REMOVE": return T("Удаление", "Видалення");
                case "ROLLBACK":
                case "ROLLBACK_REQUIRED": return T("Откат", "Відкіт");
                case "DOWNLOAD": return T("Загрузка", "Завантаження");
                case "COMMITTED":
                case "VERIFIED":
                case "RESTORED": return T("Успешно", "Успішно");
                default: return value;
            }
        }

        private bool Available() => release != null && (installed == null || release.Version.CompareTo(installed.Version) > 0)
            && release.Version.Build != runtime.Setting("skip", "")
            && (!DateTimeOffset.TryParse(runtime.Setting("defer", ""), out var when) || when <= DateTimeOffset.Now);

        private void Enable()
        {
            bool idle = work == null;
            foreach (var button in regular) button.Enabled = idle;
            launch.Enabled = idle && installed != null;
            remove.Enabled = idle && Directory.Exists(Path.Combine(runtime.Gau.ProgramRoot(runtime.Main), "current"));
            rollback.Enabled = idle && Directory.Exists(Path.Combine(runtime.Gau.ProgramRoot(runtime.Main), "previous"));
            repair.Enabled = idle;
            update.Enabled = download.Enabled = skip.Enabled = defer.Enabled = idle && Available();
            pause.Enabled = !idle && pending != null;
            cancel.Enabled = pending != null;
            resume.Enabled = idle && pending != null;
        }

        private void Postpone(bool ignored)
        {
            var settings = runtime.Settings();
            settings[ignored ? "skip" : "defer"] = ignored ? release.Version.Build : DateTimeOffset.Now.AddDays(1).ToString("o");
            runtime.Save(settings);
            RefreshState();
        }

        private void BeginDownload(bool install, bool openTab = true)
        {
            if (release == null || work != null) return;
            if (install && !Confirm(T("Скачать и установить gTR ", "Завантажити й установити gTR ") + release.Version + "?"))
                return;
            if (pending != null && pending.Sha256 != release.Sha256)
                runtime.Connector.Forget(pending);
            pending = release;
            installAfterDownload = install;
            DownloadPending(openTab);
        }

        private void DownloadPending(bool openTab = true)
        {
            if (openTab) tabs.SelectedTab = downloadsPage;
            var item = pending;
            var clock = Stopwatch.StartNew();
            long start = -1, last = -1000;
            status.Text = T("Загрузка GSU…", "Завантаження GSU…");
            _ = Run(async token =>
            {
                string path;
                try
                {
                    path = await Task.Run(() => runtime.Connector.Download(item, amount =>
                    {
                        if (start < 0) start = amount;
                        if (clock.ElapsedMilliseconds - last < 220 && amount != item.Size) return;
                        last = clock.ElapsedMilliseconds;
                        var rate = (amount - start) / Math.Max(1, clock.Elapsed.TotalSeconds);
                        BeginInvoke(new Action(() =>
                        {
                            progress.Value = (int)Math.Min(1000, amount * 1000 / item.Size);
                            size.Text = "gTR " + item.Version + " · " + Bytes(amount) + " / " + Bytes(item.Size) +
                                (rate > 0 ? " · " + Bytes((long)rate) + T("/с · осталось ", "/с · залишилось ") +
                                    TimeSpan.FromSeconds(Math.Min(604800, (item.Size - amount) / rate)).ToString(@"hh\:mm\:ss") : "");
                        }));
                    }, token), token);
                }
                catch (Exception error)
                {
                    if (!(error is OperationCanceledException))
                        runtime.Gau.Record(runtime.Main, "DOWNLOAD", item.Version.ToString(), ErrorCode(error));
                    throw;
                }
                pending = null;
                progress.Value = 1000;
                runtime.Gau.Record(runtime.Main, "DOWNLOAD", item.Version.ToString(), "VERIFIED");
                status.Text = T("Загрузка завершена. SHA-256 совпадает.", "Завантаження завершено. SHA-256 збігається.");
                if (installAfterDownload)
                    await Install(path, item, token);
                else
                    size.Text = Path.GetFileName(path) + " · " + T("Готов к установке", "Готовий до встановлення");
            });
        }

        private void ClearPending()
        {
            if (pending != null) runtime.Connector.Forget(pending);
            pending = null;
            removePartial = false;
            progress.Value = 0;
            Enable();
        }

        private async Task Install(string path, UpdateRelease releaseInfo, CancellationToken token)
        {
            status.Text = T("Проверка пакета и установка…", "Перевірка пакета й встановлення…");
            await Task.Run(() => runtime.Gau.Install(runtime.Main, path, releaseInfo, token), token);
            runtime.LinkApplication(false);
            status.Text = T("Установка завершена.", "Встановлення завершено.");
        }

        private void ChoosePackage()
        {
            using (var picker = new OpenFileDialog { Filter = "Gruzin Software Update (*.gsu)|*.gsu", CheckFileExists = true })
                if (picker.ShowDialog(this) == DialogResult.OK) OfferPackage(picker.FileName);
        }

        public void OfferPackage(string path)
        {
            _ = Run(async token =>
            {
                var info = await Task.Run(() =>
                {
                    using (var package = new Gsu(path, runtime.Main, runtime.Publisher))
                        return (package.Version, package.Space);
                }, token);
                if (Confirm("gTR " + info.Version + "\n" + T("Подпись проверена. Требуется ", "Підпис перевірено. Потрібно ") +
                    Bytes(info.Space) + "\n\n" + T("Установить пакет?", "Встановити пакет?")))
                    await Install(path, null, token);
            });
        }

        private void OfferRollback()
        {
            if (!Confirm(T("Вернуть предыдущую рабочую версию? Проекты и сессии сохранятся.",
                            "Повернути попередню робочу версію? Проєкти й сеанси збережуться."))) return;
            _ = Run(async token =>
            {
                await Task.Run(() => runtime.Gau.Rollback(runtime.Main), token);
                runtime.LinkApplication(false);
                status.Text = T("Предыдущая версия восстановлена.", "Попередню версію відновлено.");
            });
        }

        public void OfferRemove()
        {
            if (!Confirm(T("Удалить программные файлы gTR? Проекты и сессии сохранятся.",
                            "Видалити програмні файли gTR? Проєкти й сеанси збережуться."))) return;
            _ = Run(async token =>
            {
                await Task.Run(() => runtime.Gau.Uninstall(runtime.Main), token);
                runtime.UnlinkApplication();
                status.Text = T("Приложение удалено.", "Застосунок видалено.");
            });
        }

        private void Launch()
        {
            if (installed == null) return;
            Process.Start(new ProcessStartInfo(runtime.Gau.Entry(runtime.Main), Runtime.Diagnostic)
                { UseShellExecute = false, WorkingDirectory = installed.Path });
        }

        private void OpenFolder()
        {
            if (Directory.Exists(runtime.Gau.ProgramRoot(runtime.Main)))
                Process.Start(new ProcessStartInfo(runtime.Gau.ProgramRoot(runtime.Main)) { UseShellExecute = true });
        }

        private void ExportHistory()
        {
            using (var picker = new SaveFileDialog { Filter = "JSON (*.json)|*.json", FileName = "gassets-diagnostics.json" })
            {
                if (picker.ShowDialog(this) == DialogResult.OK)
                    Locations.Atomic(picker.FileName, Json.Write(new Dictionary<string, object>
                    {
                        {"schema", 1}, {"events", runtime.Gau.Events(runtime.Main)}, {"test", Runtime.Test}
                    }));
            }
        }

        private void Legacy()
        {
            using (var folder = new FolderBrowserDialog { Description = T("Выберите папку старой сборки", "Виберіть теку старої збірки") })
            {
                if (folder.ShowDialog(this) != DialogResult.OK) return;
                _ = Run(async token =>
                {
                    var result = await Task.Run(() =>
                    {
                        var root = Locations.Local(folder.SelectedPath);
                        if (!File.Exists(Locations.Under(root, "gTR.exe"))) throw new UpdateError("LEGACY_INVALID");
                        var source = Json.Utf8.GetString(Locations.Read(Locations.Under(root, "gtr/__init__.py"), 16384));
                        var match = System.Text.RegularExpressions.Regex.Match(source, @"(?m)^BETA_NUMBER = (3|4|5|6)\s*$");
                        if (!match.Success && !System.Text.RegularExpressions.Regex.IsMatch(source, @"\bBeta [3-6]\b")) throw new UpdateError("LEGACY_INVALID");
                        return match.Success ? "Beta " + match.Groups[1].Value : T("Старая Beta", "Попередня Beta");
                    }, token);
                    MessageBox.Show(this, result + "\n" + T("Установите проверенный GSU. gTR использует существующий профиль Windows. Старую папку можно проверить и убрать отдельно.",
                        "Встановіть перевірений GSU. gTR використовує наявний профіль Windows. Попередню теку можна перевірити й прибрати окремо."), Text);
                });
            }
        }

        private bool Confirm(string message) => MessageBox.Show(this, message, Text, MessageBoxButtons.YesNo,
            MessageBoxIcon.Question, MessageBoxDefaultButton.Button2) == DialogResult.Yes;

        private static string Bytes(long count) => count >= 1024 * 1024 ? (count / 1048576.0).ToString("0.0") + " MiB"
            : (count / 1024.0).ToString("0.0") + " KiB";

        private static string ErrorCode(Exception error) => error is UpdateError update ? update.Code :
            error is HttpRequestException ? "NETWORK_UNAVAILABLE" : error is IOException ? "FILE_IO" : "UPDATE_FAILED";

        public static string Error(Exception error, bool uk)
        {
            var code = ErrorCode(error);
            string text;
            switch (code)
            {
                case "PRODUCT_RUNNING": text = uk ? "Закрийте gTR після завершення поточної роботи." : "Закройте gTR после завершения текущей работы."; break;
                case "SIGNATURE_INVALID":
                case "KEY_INVALID": text = uk ? "Підпис пакета не підтверджено." : "Подпись пакета не подтверждена."; break;
                case "DISK_SPACE": text = uk ? "Недостатньо місця на диску." : "Недостаточно места на диске."; break;
                case "NETWORK_LIMIT": text = uk ? "GitHub обмежив запити. Повторіть пізніше." : "GitHub ограничил запросы. Повторите позже."; break;
                case "NETWORK_UNAVAILABLE": text = uk ? "GitHub недоступний." : "GitHub недоступен."; break;
                case "HEALTH_FAILED": text = uk ? "Нова версія не пройшла перевірку запуску." : "Новая версия не прошла проверку запуска."; break;
                case "APP_CONTROL": text = uk ? "Політика Windows заблокувала запуск нової версії." : "Политика Windows заблокировала запуск новой версии."; break;
                default: text = uk ? "Дію не завершено." : "Действие не завершено."; break;
            }
            return text + " " + code;
        }
    }
}
