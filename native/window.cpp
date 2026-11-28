#include "window.hpp"
#include "release.hpp"
#include "translation.hpp"

#include <QApplication>
#include <QAudioOutput>
#include <QCheckBox>
#include <QCloseEvent>
#include <QComboBox>
#include <QDesktopServices>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDir>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QFrame>
#include <QGridLayout>
#include <QHeaderView>
#include <QImageReader>
#include <QInputDialog>
#include <QJsonDocument>
#include <QLabel>
#include <QLineEdit>
#include <QListView>
#include <QListWidget>
#include <QMediaPlayer>
#include <QMenu>
#include <QMessageBox>
#include <QProgressBar>
#include <QPushButton>
#include <QSaveFile>
#include <QScrollArea>
#include <QSettings>
#include <QSignalBlocker>
#include <QSlider>
#include <QSplitter>
#include <QStackedWidget>
#include <QStandardItemModel>
#include <QTableWidget>
#include <QTimeZone>
#include <QVideoWidget>

#include <algorithm>
#include <limits>
#include <utility>

namespace {
constexpr int RecordData = Qt::UserRole + 1;

const auto shellStyle = R"(
QWidget#root { background:#151a1d; color:#e6edeb; font-family:"Segoe UI"; font-size:10pt; }
QWidget { color:#e6edeb; font-family:"Segoe UI"; font-size:10pt; }
QLabel[muted="true"] { color:#a5b6bd; }
QLabel#title { font-weight:650; font-size:22pt; }
QLabel#subtitle { font-weight:650; font-size:13pt; }
QFrame#card, QFrame#footer { background:#1d2528; border:1px solid #344045; border-radius:9px; }
QFrame#top { border-bottom:1px solid #344045; }
QListWidget, QListView, QTableView, QLineEdit, QComboBox { background:#1b2225; border:1px solid #3d4b51; border-radius:6px; }
QListWidget#nav, QListWidget#steps { background:transparent; border:0; }
QListWidget::item, QListView::item { padding:8px; }
QListWidget#nav::item { margin:1px 0; border-radius:6px; }
QListWidget::item:selected, QListView::item:selected, QTableView::item:selected { background:#345166; color:#fff; }
QHeaderView::section { background:#263137; color:#e6edeb; padding:5px; }
QPushButton { background:#29353a; border:1px solid #47595f; padding:6px 10px; border-radius:5px; }
QPushButton:hover { background:#35454c; }
QPushButton:focus { border:1px solid #e7b66c; }
QPushButton[primary="true"] { background:#edbf78; color:#22180d; border-color:#edbf78; font-weight:600; }
QPushButton[primary="true"]:hover { background:#f6cc8b; }
QPushButton:disabled { background:#21292e; color:#87979e; border-color:#303b42; }
QMenu { background:#29353a; border:1px solid #47595f; }
QMenu::item { padding:7px 14px; }
QMenu::item:selected { background:#345166; }
QLineEdit, QComboBox { padding:5px 8px; selection-background-color:#385971; }
QProgressBar { background:#141a1d; border:1px solid #3a494f; border-radius:5px; height:11px; text-align:center; }
QProgressBar::chunk { background:#e6b877; border-radius:4px; }
QToolTip { color:#e6edeb; background:#253238; border:1px solid #53666d; }
)";

QLabel *label(const QString &text = {}, const char *role = nullptr) {
    auto *value = new QLabel(text);
    value->setTextFormat(Qt::PlainText);
    value->setWordWrap(true);
    if (role)
        value->setObjectName(role);
    return value;
}

QLabel *muted(const QString &text = {}) {
    auto *value = label(text);
    value->setProperty("muted", true);
    return value;
}

QVBoxLayout *card() {
    auto *frame = new QFrame;
    frame->setObjectName("card");
    auto *layout = new QVBoxLayout(frame);
    layout->setContentsMargins(16, 13, 16, 13);
    layout->setSpacing(9);
    return layout;
}

QVBoxLayout *column(QWidget *target) {
    auto *layout = new QVBoxLayout(target);
    layout->setContentsMargins(14, 12, 14, 12);
    layout->setSpacing(10);
    return layout;
}

QPushButton *button(const QString &name, QObject *parent, std::function<void()> callback, bool primary = false) {
    auto *result = new QPushButton(name);
    result->setProperty("primary", primary);
    QObject::connect(result, &QPushButton::clicked, parent, std::move(callback));
    return result;
}

QScrollArea *scrollable(QWidget *child) {
    auto *area = new QScrollArea;
    area->setFrameShape(QFrame::NoFrame);
    area->setWidgetResizable(true);
    area->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    area->setWidget(child);
    return area;
}

QString count(qint64 n) { return QLocale().toString(n); }
QString amount(qint64 n) { return QLocale().formattedDataSize(n, 1); }

QString elapsed(qint64 milliseconds) {
    const qint64 seconds = milliseconds / 1000;
    return QStringLiteral("%1:%2").arg(seconds / 60).arg(seconds % 60, 2, 10, QLatin1Char('0'));
}

QString date(const QJsonValue &value) {
    return value.toString().replace('T', ' ');
}

QString planText(const QJsonObject &info) {
    const auto services = info["losses"].toObject()["service"].toInteger();
    const auto messages = info["source_records"].toInteger() - services;
    const auto selected = info["selected"].toInteger();
    const auto source = B("Сообщений в архиве %1 · вне выборки %2 · служебных событий %3\n",
                          "Повідомлень в архіві %1 · поза вибіркою %2 · службових подій %3\n")
        .arg(count(messages), count(qMax(qint64{0}, messages - selected)), count(services));
    return source + B("Сообщений в пакете %1 из %2 выбранных\nВложений %3 · %4\nПропущено записей %5 · отсутствует файлов %6\n"
             "Пересылок %7 · исторических пометок %8",
             "Повідомлень у пакеті %1 із %2 вибраних\nВкладень %3 · %4\nПропущено записів %5 · відсутньо файлів %6\n"
             "Пересилань %7 · історичних позначок %8")
        .arg(count(info["included"].toInteger()), count(info["selected"].toInteger()),
             count(info["attachments"].toInteger()), amount(info["media_bytes"].toInteger()),
             count(info["skipped"].toInteger()), count(info["missing_assets"].toInteger()),
             count(info["forwards"].toInteger()), count(info["annotated_records"].toInteger()));
}

QString optionName(const QString &option) {
    if (option == "compatible")
        return B("Совместимая копия", "Сумісна копія");
    if (option == "remux")
        return B("Сменить контейнер", "Змінити контейнер");
    if (option == "fidelity")
        return B("Максимальная точность", "Максимальна точність");
    if (option == "compact")
        return B("Уменьшенная копия", "Зменшена копія");
    if (option == "document")
        return B("Документ", "Документ");
    if (option == "image")
        return B("Копия PNG", "Копія PNG");
    if (option == "local")
        return B("Только локально", "Лише локально");
    if (option == "skip")
        return B("Пропустить вложение", "Пропустити вкладення");
    return B("Оригинал", "Оригінал");
}

QString mediaReason(const QString &code) {
    if (code == "alpha_requires_original")
        return B("Видео с прозрачностью", "Відео з прозорістю");
    if (code == "ffmpeg_unavailable" || code == "ffprobe_unavailable")
        return B("Нужны FFmpeg и FFprobe", "Потрібні FFmpeg і FFprobe");
    if (code == "conversion_failed")
        return B("Копия не прошла проверку", "Копія не пройшла перевірку");
    if (code == "transcode_required")
        return B("Требуется совместимая копия", "Потрібна сумісна копія");
    if (code == "disk_space")
        return B("Мало места на диске", "Мало місця на диску");
    if (code == "webp_photo")
        return B("Для WebP нужна совместимая копия", "Для WebP потрібна сумісна копія");
    if (code == "sticker_unconfirmed")
        return B("Формат стикера не подтверждён", "Формат стікера не підтверджено");
    if (code == "sticker_legacy_document")
        return B("Стикер был подготовлен как документ", "Стікер був підготовлений як документ");
    if (code == "attachment_format" || code == "corrupt_container" || code == "audio_unreadable")
        return B("Файл не прошёл проверку формата", "Файл не пройшов перевірку формату");
    if (code == "audio_unconfirmed")
        return B("Тип или длительность аудио не подтверждены", "Тип або тривалість аудіо не підтверджено");
    if (code == "multiple_attachments")
        return B("Несколько файлов в одной записи", "Кілька файлів в одному записі");
    if (code == "image_copy_failed")
        return B("Не удалось создать PNG", "Не вдалося створити PNG");
    return B("Нужно выбрать способ подготовки", "Потрібно вибрати спосіб підготовки");
}

QString mediaKind(const QString &code) {
    if (code == "webp_sticker")
        return B("Стикер WebP", "Стікер WebP");
    if (code == "tgs_sticker")
        return B("Стикер TGS", "Стікер TGS");
    if (code == "webm_sticker")
        return B("Стикер WebM", "Стікер WebM");
    if (code == "sticker")
        return B("Стикер", "Стікер");
    if (code == "webp_image")
        return B("Изображение WebP", "Зображення WebP");
    if (code == "photo")
        return B("Фото", "Фото");
    if (code == "round_video")
        return B("Кружок", "Відеокружечок");
    if (code == "video")
        return B("Видео", "Відео");
    if (code == "animation")
        return B("Анимация", "Анімація");
    if (code == "voice")
        return B("Голосовое", "Голосове");
    if (code == "audio")
        return B("Аудио", "Аудіо");
    if (code == "document")
        return B("Документ", "Документ");
    return QStringLiteral("—");
}

QString operationState(const QString &code) {
    if (code == "COMPLETED")
        return B("Подтверждено", "Підтверджено");
    if (code == "START_ACCEPTED" || code == "AWAITING_VERIFICATION")
        return B("Ожидает сверки", "Очікує звіряння");
    if (code.contains("UNKNOWN") || code.contains("UNCERTAIN"))
        return B("Требуется сверка", "Потрібне звіряння");
    if (code.contains("STAGED") || code == "VALIDATED" || code == "PREPARED")
        return B("Подготовка", "Підготовка");
    return B("См. журнал", "Див. журнал");
}
}

Window::Window(QString root, QString python, const QString &initialProject)
    : root_(std::move(root)), python_(std::move(python)), service_(root_, python_, this) {
    setWindowTitle("gTR " GTR_RELEASE_LABEL);
    resize(1120, 720);
    setMinimumSize(640, 342);
    auto *base = new QWidget;
    base->setObjectName("root");
    base->setStyleSheet(QString::fromUtf8(shellStyle));
    setCentralWidget(base);
    auto *outer = new QVBoxLayout(base);
    outer->setContentsMargins(14, 8, 14, 9);
    outer->setSpacing(8);
    auto *top = new QFrame;
    top->setObjectName("top");
    auto *head = new QHBoxLayout(top);
    head->setContentsMargins(4, 0, 4, 7);
    auto *brand = label();
    brand->setPixmap(QPixmap(QDir(root_).filePath("gtr/assets/cat.png"))
        .scaled(38, 38, Qt::KeepAspectRatio, Qt::SmoothTransformation));
    brand->setAccessibleName("gTR");
    head->addWidget(brand);
    head->addWidget(label("gTR", "title"));
    head->addStretch();
    account_ = muted(B("Telegram · локальный режим", "Telegram · локальний режим"));
    head->addWidget(account_, 0, Qt::AlignRight);
    update_ = button(B("Обновление…", "Оновлення…"), this, [this] { updates(true); });
    update_->hide();
    head->addWidget(update_);
    head->addWidget(button(B("Аккаунт", "Обліковий запис"), this, [this] { showAccount(); }));
    outer->addWidget(top);

    auto *main = new QHBoxLayout;
    nav_ = new QListWidget;
    nav_->setObjectName("nav");
    nav_->setAccessibleName(B("Разделы", "Розділи"));
    nav_->setFixedWidth(138);
    nav_->addItems({B("Главная", "Головна"), B("Архив", "Архів"), B("Восстановление", "Відновлення"),
                    B("История", "Історія"), B("Настройки", "Налаштування")});
    main->addWidget(nav_);
    pages_ = new QStackedWidget;
    pages_->setObjectName("pages");
    auto *home = homePage();
    auto *archive = archivePage();
    auto *recovery = recoveryPage();
    auto *history = historyPage();
    auto *settings = settingsPage();
    for (auto *page : {home, archive, recovery, history, settings})
        pages_->addWidget(scrollable(page));
    main->addWidget(pages_, 1);
    outer->addLayout(main, 1);

    auto *footer = new QFrame;
    footer->setObjectName("footer");
    auto *work = new QHBoxLayout(footer);
    work->setContentsMargins(10, 5, 10, 5);
    status_ = muted(B("Запуск локальной службы…", "Запуск локальної служби…"));
    work->addWidget(status_, 2);
    progress_ = new QProgressBar;
    progress_->setTextVisible(false);
    progress_->setAccessibleName(B("Прогресс задачи", "Перебіг завдання"));
    progress_->hide();
    work->addWidget(progress_, 1);
    cancel_ = button(B("Отменить задачу", "Скасувати завдання"), this, [this] {
        service_.cancel();
        cancel_->setEnabled(false);
        status(B("Останавливаем задачу…", "Зупиняємо завдання…"));
    });
    work->addWidget(cancel_);
    cancel_->hide();
    restart_ = button(B("Перезапустить службу", "Перезапустити службу"), this, [this] {
        status(B("Запуск локальной службы…", "Запуск локальної служби…"));
        service_.start();
    });
    restart_->hide();
    work->addWidget(restart_);
    saveDiagnostic_ = button(B("Сохранить диагностический отчёт…", "Зберегти діагностичний звіт…"),
                             this, [this] { saveValidationDiagnostic(); });
    saveDiagnostic_->hide();
    work->addWidget(saveDiagnostic_);
    outer->addWidget(footer);
    connect(nav_, &QListWidget::currentRowChanged, this, [this](int row) { go(row); });
    nav_->setCurrentRow(Home);
    step(Source);
    project_ = initialProject.isEmpty() ? QSettings().value("native/project").toString() : initialProject;
    service_.onReady = [this] {
        restart_->hide();
        status(B("Локальная служба готова", "Локальна служба готова"));
        service_.send("ui.ready", {}, [this](const QJsonObject &, const QString &error) {
            if (!error.isEmpty()) {
                status(fault(error));
                return;
            }
            refreshProjects();
        });
    };
    service_.onFailure = [this](const QString &error) {
        status(fault(error));
        account_->setText(B("Telegram · локальный режим", "Telegram · локальний режим"));
        testTarget_.clear();
        if (lastDiagnostic_.isEmpty()) {
            lastDiagnostic_ = {{"schema", 1}, {"redacted", true}, {"component", "ipc"}, {"code", error}};
            saveDiagnostic_->show();
        }
        cancel_->hide();
        progress_->hide();
        restart_->show();
    };
    auto *updateTimer = new QTimer(this);
    connect(updateTimer, &QTimer::timeout, this, [this] { updates(false); });
    updateTimer->start(15000);
    service_.onProgress = [this](const QString &stage, qint64 done, qint64 total) {
        if (!cancel_->isEnabled())
            return;
        progress_->setRange(0, total > 0 ? 1000 : 0);
        if (total > 0) {
            progress_->setValue(static_cast<int>(std::clamp(1000.0 * done / total, 0.0, 1000.0)));
            progress_->setToolTip(stage == "upload_bytes"
                ? QStringLiteral("%1 / %2").arg(amount(done), amount(total))
                : QStringLiteral("%1 / %2").arg(count(done), count(total)));
        } else {
            progress_->setToolTip({});
        }
        if (stage == "convert_video")
            status(B("Подготовка и проверка видео", "Підготовка й перевірка відео"));
        else if (stage == "media")
            status(B("Вложения %1 из %2", "Вкладення %1 із %2").arg(count(done), count(total)));
        else if (stage == "upload_item")
            status(B("Загрузка вложения %1 из %2", "Завантаження вкладення %1 із %2").arg(count(done), count(total)));
        else if (stage == "bind_media")
            status(B("Telegram обрабатывает вложение %1 из %2", "Telegram обробляє вкладення %1 із %2")
                .arg(count(done), count(total)));
        else if (stage == "records" || stage == "convert" || stage == "manifest")
            status(B("Записи %1 из %2", "Записи %1 із %2").arg(count(done), count(total)));
    };
    service_.start();
}

QWidget *Window::homePage() {
    auto *page = new QWidget;
    auto *layout = column(page);
    layout->addWidget(label(B("Главная", "Головна"), "title"));
    auto *active = card();
    active->addWidget(label(B("Текущий проект", "Поточний проєкт"), "subtitle"));
    overview_ = muted(B("Откройте экспорт истории из Telegram Desktop.", "Відкрийте експорт історії з Telegram Desktop."));
    active->addWidget(overview_);
    active->addWidget(button(B("Открыть архив…", "Відкрити архів…"), this, [this] { chooseSource(false); }, true),
                      0, Qt::AlignLeft);
    active->addWidget(button(B("Продолжить восстановление", "Продовжити відновлення"), this,
                             [this] { nav_->setCurrentRow(Recovery); }), 0, Qt::AlignLeft);
    layout->addWidget(active->parentWidget());
    layout->addWidget(label(B("Последние проекты", "Останні проєкти"), "subtitle"));
    recent_ = new QListWidget;
    recent_->setAccessibleName(B("Последние проекты", "Останні проєкти"));
    layout->addWidget(recent_, 1);
    connect(recent_, &QListWidget::itemActivated, this, [this](QListWidgetItem *item) {
        readProject(item->data(Qt::UserRole).toString());
    });
    layout->addWidget(button(B("Открыть выбранный проект", "Відкрити вибраний проєкт"), this, [this] {
        if (auto *item = recent_->currentItem())
            readProject(item->data(Qt::UserRole).toString());
    }), 0, Qt::AlignLeft);
    return page;
}

QWidget *Window::archivePage() {
    auto *page = new QWidget;
    auto *layout = column(page);
    layout->addWidget(label(B("Архив", "Архів"), "title"));
    auto *open = new QPushButton(B("Открыть…", "Відкрити…"));
    open->setProperty("primary", true);
    auto *formats = new QMenu(open);
    formats->addAction(B("Файл экспорта…", "Файл експорту…"), this, [this] { chooseSource(false); });
    formats->addAction(B("Папка экспорта…", "Тека експорту…"), this, [this] { chooseSource(true); });
    formats->addAction(B("Папка проекта…", "Тека проєкту…"), this, [this] {
        const auto path = QFileDialog::getExistingDirectory(this, B("Открыть проект gTR", "Відкрити проєкт gTR"));
        if (!path.isEmpty())
            readProject(path);
    });
    open->setMenu(formats);
    chat_ = new QComboBox;
    chat_->setObjectName("archive-chat");
    chat_->setAccessibleName(B("Диалог из архива", "Діалог з архіву"));
    chat_->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    chat_->setMinimumContentsLength(10);
    auto *projectControls = new QHBoxLayout;
    projectControls->addWidget(open);
    projectControls->addWidget(chat_, 1);
    layout->addLayout(projectControls);
    auto *filters = new QHBoxLayout;
    query_ = new QLineEdit;
    query_->setMaxLength(160);
    query_->setPlaceholderText(B("Поиск по сообщениям", "Пошук у повідомленнях"));
    query_->setAccessibleName(query_->placeholderText());
    filters->addWidget(query_, 2);
    filters->addWidget(button(B("Найти", "Знайти"), this, [this] { messages(); }));
    filters->addWidget(button(B("Фильтры…", "Фільтри…"), this, [this] { selectionFilterDialog(); }));
    layout->addLayout(filters);
    auto *editor = new QGridLayout;
    auto *edit = new QPushButton(B("Редактор архива", "Редактор архіву"));
    auto *commands = new QMenu(edit);
    const auto change = [this](const QString &action) {
        QList<int> ordinals;
        for (const auto &index : recordList_->selectionModel()->selectedRows()) {
            const int ordinal = index.data(RecordData).toJsonObject()["ordinal"].toInt();
            if (ordinal > 0)
                ordinals.append(ordinal);
        }
        std::sort(ordinals.begin(), ordinals.end());
        QJsonArray ranges;
        for (const int ordinal : std::as_const(ordinals))
            ranges.append(QJsonArray{ordinal, ordinal});
        if (!ranges.isEmpty())
            editSelection(action, {{"ranges", ranges}});
    };
    commands->addAction(B("Исключить выбранные", "Виключити вибрані"), this, [change] { change("exclude"); });
    commands->addAction(B("Включить выбранные", "Включити вибрані"), this, [change] { change("include"); });
    commands->addAction(B("Исключить диапазон…", "Виключити діапазон…"), this, [this] {
        bool accepted = false;
        const auto maximum = std::numeric_limits<int>::max();
        const auto first = QInputDialog::getInt(this, "gTR", B("Первый номер записи", "Перший номер запису"),
            std::max(1, selectedRecord_["ordinal"].toInt()), 1, maximum, 1, &accepted);
        if (!accepted)
            return;
        const auto last = QInputDialog::getInt(this, "gTR", B("Последний номер записи", "Останній номер запису"),
            first, first, maximum, 1, &accepted);
        if (accepted)
            editSelection("exclude", {{"ranges", QJsonArray{QJsonArray{first, last}}}});
    });
    commands->addAction(B("Исключить все совпадения…", "Виключити всі збіги…"), this, [this] {
        if (QMessageBox::question(this, "gTR",
                B("Исключить все записи, соответствующие текущим фильтрам, включая другие страницы?",
                  "Виключити всі записи, що відповідають поточним фільтрам, включно з іншими сторінками?"),
                QMessageBox::Yes | QMessageBox::No, QMessageBox::No) == QMessageBox::Yes)
            editSelection("exclude", {{"filters", selectionFilters()}});
    });
    commands->addAction(B("Локальная отметка…", "Локальна позначка…"), this, [this] {
        const auto ordinal = selectedRecord_["ordinal"].toInt();
        if (ordinal <= 0)
            return;
        bool accepted = false;
        const auto note = QInputDialog::getText(this, "gTR", B("Заметка для этой записи", "Нотатка для цього запису"),
            QLineEdit::Normal, selectedRecord_["note"].toString(), &accepted);
        if (accepted)
            editSelection("mark", {{"ranges", QJsonArray{QJsonArray{ordinal, ordinal}}}, {"note", note}});
    });
    commands->addAction(B("Сохранить отдельную выборку…", "Зберегти окрему вибірку…"), this, [this] {
        bool accepted = false;
        const auto name = QInputDialog::getText(this, "gTR", B("Название выборки", "Назва вибірки"),
                                               QLineEdit::Normal, {}, &accepted);
        if (accepted)
            editSelection("save_as", {{"name", name}});
    });
    commands->addAction(B("Вернуть исходный состав", "Повернути вихідний склад"), this,
                        [this] { editSelection("reset"); });
    commands->addAction(B("Состав будущего импорта…", "Склад майбутнього імпорту…"), this,
                        [this] { selectionPreview(); });
    edit->setMenu(commands);
    editor->addWidget(edit, 0, 0);
    selectionPreset_ = new QComboBox;
    selectionPreset_->setObjectName("selection-preset");
    selectionPreset_->setMinimumContentsLength(8);
    selectionPreset_->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    selectionPreset_->setAccessibleName(B("Выборка", "Вибірка"));
    selectionPreset_->addItem(B("Основная выборка", "Основна вибірка"), "");
    editor->addWidget(selectionPreset_, 0, 1, 1, 2);
    selectionView_ = new QComboBox;
    selectionView_->setAccessibleName(B("Показать записи", "Показати записи"));
    selectionView_->addItem(B("Исходная история", "Вихідна історія"), "all");
    selectionView_->addItem(B("Будущий импорт", "Майбутній імпорт"), "included");
    selectionView_->addItem(B("Исключённые", "Виключені"), "excluded");
    editor->addWidget(selectionView_, 1, 0);
    undoSelection_ = button(QStringLiteral("↶"), this, [this] { editSelection("undo"); });
    redoSelection_ = button(QStringLiteral("↷"), this, [this] { editSelection("redo"); });
    undoSelection_->setAccessibleName(B("Отменить изменение выборки", "Скасувати зміну вибірки"));
    redoSelection_->setAccessibleName(B("Повторить изменение выборки", "Повторити зміну вибірки"));
    for (auto *control : {undoSelection_, redoSelection_}) {
        control->setToolTip(control->accessibleName());
        control->setFixedWidth(39);
    }
    undoSelection_->setEnabled(false);
    redoSelection_->setEnabled(false);
    editor->addWidget(undoSelection_, 1, 1);
    editor->addWidget(redoSelection_, 1, 2);
    layout->addLayout(editor);
    connect(selectionPreset_, &QComboBox::activated, this, [this] {
        selectionRevision_.clear();
        plan_ = {};
        bundle_.clear();
        step(Source);
        messages();
    });
    connect(selectionView_, &QComboBox::activated, this, [this] { messages(); });
    connect(chat_, &QComboBox::currentIndexChanged, this, [this] { selectChat(); });
    connect(query_, &QLineEdit::returnPressed, this, [this] { messages(); });
    connect(query_, &QLineEdit::textEdited, this, [this] {
        cursor_ = 0;
        older_->setEnabled(false);
    });
    auto *split = new QSplitter(Qt::Horizontal);
    auto *list = recordList_ = new QListView;
    list->setObjectName("archive-records");
    list->setWordWrap(true);
    list->setTextElideMode(Qt::ElideRight);
    list->setAccessibleName(B("Записи диалога", "Записи діалогу"));
    list->setSelectionMode(QAbstractItemView::ExtendedSelection);
    records_ = new QStandardItemModel(this);
    list->setModel(records_);
    split->addWidget(list);

    auto *details = card();
    message_ = label(B("Выберите сообщение", "Виберіть повідомлення"));
    message_->setTextInteractionFlags(Qt::TextSelectableByMouse | Qt::TextSelectableByKeyboard);
    details->addWidget(message_);
    picture_ = label();
    picture_->setAlignment(Qt::AlignCenter);
    picture_->hide();
    details->addWidget(picture_);
    video_ = new QVideoWidget;
    video_->setMinimumSize(120, 112);
    video_->hide();
    details->addWidget(video_, 1);
    player_ = new QMediaPlayer(this);
    auto *audio = new QAudioOutput(this);
    player_->setAudioOutput(audio);
    player_->setVideoOutput(video_);
    player_->audioOutput()->setVolume(0.55f);
    auto *mediaControls = new QHBoxLayout;
    preview_ = button(B("Открыть вложение", "Відкрити вкладення"), this, [this] { preview(); });
    preview_->hide();
    details->addWidget(preview_);
    play_ = button(B("Играть", "Відтворити"), this, [this] {
        if (player_->playbackState() == QMediaPlayer::PlayingState)
            player_->pause();
        else
            player_->play();
    });
    mediaControls->addWidget(play_);
    seek_ = new QSlider(Qt::Horizontal);
    seek_->setAccessibleName(B("Позиция воспроизведения", "Позиція відтворення"));
    seek_->setRange(0, 0);
    mediaControls->addWidget(seek_, 1);
    duration_ = muted();
    mediaControls->addWidget(duration_);
    play_->hide();
    seek_->hide();
    duration_->hide();
    details->addLayout(mediaControls);
    auto *openReply = button(B("Показать исходный ответ", "Показати вихідну відповідь"), this, [this, list] {
        const auto reply = selectedRecord_["reply"].toInteger();
        for (int i = 0; i < records_->rowCount(); ++i) {
            const auto index = records_->index(i, 0);
            if (index.data(RecordData).toJsonObject()["id"].toInteger() == reply) {
                list->setCurrentIndex(index);
                list->scrollTo(index);
                return;
            }
        }
        if (reply > 0) {
            task(B("Переход к записи", "Перехід до запису"), "chat.page",
                 {{"project", project_}, {"chat", chat_->currentData().toInt()}, {"before", reply + 1}, {"limit", 1}},
                 [this](const QJsonObject &value) {
                     const auto matches = value["records"].toArray();
                     if (matches.isEmpty())
                         return;
                     auto *item = new QStandardItem;
                     item->setData(matches.first().toObject(), RecordData);
                     records_->appendRow(item);
                     showMessage(item->index());
                     records_->removeRow(item->row());
                 });
        }
    });
    openReply->hide();
    details->addWidget(openReply);
    details->addStretch();
    connect(list->selectionModel(), &QItemSelectionModel::currentChanged, this,
            [this, openReply](const QModelIndex &index) {
                showMessage(index);
                openReply->setVisible(selectedRecord_["reply"].toInteger() > 0);
            });
    connect(player_, &QMediaPlayer::durationChanged, this, [this](qint64 ms) {
        seek_->setRange(0, static_cast<int>(std::min<qint64>(ms, 604800000)));
        duration_->setText(elapsed(player_->position()) + " / " + elapsed(ms));
    });
    connect(player_, &QMediaPlayer::positionChanged, this, [this](qint64 ms) {
        if (!seek_->isSliderDown())
            seek_->setValue(static_cast<int>(std::min<qint64>(ms, seek_->maximum())));
        duration_->setText(elapsed(ms) + " / " + elapsed(player_->duration()));
    });
    connect(seek_, &QSlider::sliderMoved, player_, &QMediaPlayer::setPosition);
    connect(player_, &QMediaPlayer::playbackStateChanged, this, [this](QMediaPlayer::PlaybackState state) {
        play_->setText(state == QMediaPlayer::PlayingState ? B("Пауза", "Пауза") : B("Играть", "Відтворити"));
    });
    connect(player_, &QMediaPlayer::errorOccurred, this, [this](QMediaPlayer::Error, const QString &) {
        status(B("Windows не может воспроизвести этот формат.", "Windows не може відтворити цей формат."));
    });
    auto *detailsScroll = scrollable(details->parentWidget());
    detailsScroll->setMinimumWidth(165);
    split->addWidget(detailsScroll);
    split->setChildrenCollapsible(false);
    split->setStretchFactor(0, 1);
    split->setStretchFactor(1, 1);
    layout->addWidget(split, 1);
    older_ = button(B("Более ранние сообщения", "Попередні повідомлення"), this, [this] { messages(true); });
    older_->setEnabled(false);
    layout->addWidget(older_, 0, Qt::AlignLeft);
    return page;
}

QWidget *Window::recoveryPage() {
    auto *page = new QWidget;
    auto *layout = column(page);
    layout->addWidget(label(B("Восстановление", "Відновлення"), "title"));
    steps_ = new QListWidget;
    steps_->setObjectName("steps");
    steps_->setFlow(QListView::LeftToRight);
    steps_->setResizeMode(QListView::Adjust);
    steps_->setFixedHeight(43);
    steps_->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    steps_->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    steps_->setFocusPolicy(Qt::NoFocus);
    steps_->setAttribute(Qt::WA_TransparentForMouseEvents);
    steps_->addItems({B("Источник", "Джерело"), B("Диалог", "Діалог"), B("Анализ", "Аналіз"),
                      B("Подготовка", "Підготовка"), B("Результат", "Результат")});
    layout->addWidget(steps_);
    wizard_ = new QStackedWidget;
    wizard_->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::MinimumExpanding);
    layout->addWidget(wizard_, 1);
    auto *source = card();
    source_ = label();
    source->addWidget(source_);
    source->addWidget(button(B("Выбрать файл экспорта…", "Вибрати файл експорту…"), this, [this] { chooseSource(false); }),
                      0, Qt::AlignLeft);
    source->addWidget(button(B("Выбрать папку экспорта…", "Вибрати теку експорту…"), this, [this] { chooseSource(true); }),
                      0, Qt::AlignLeft);
    source->addStretch();
    wizard_->addWidget(source->parentWidget());

    auto *dialog = card();
    dialog_ = label();
    dialog->addWidget(dialog_);
    dialog->addWidget(button(B("Выбрать диалог в архиве", "Вибрати діалог в архіві"), this,
                             [this] { nav_->setCurrentRow(Archive); }), 0, Qt::AlignLeft);
    dialog->addStretch();
    wizard_->addWidget(dialog->parentWidget());

    auto *analysis = card();
    analysis_ = label();
    analysis->addWidget(analysis_);
    advancedOptions_ = new QWidget;
    auto *options = new QFormLayout(advancedOptions_);
    options->setFieldGrowthPolicy(QFormLayout::ExpandingFieldsGrow);
    videoPolicy_ = new QComboBox;
    for (const auto &key : {"original", "remux", "fidelity", "compatible", "compact", "document", "skip"})
        videoPolicy_->addItem(optionName(key), key);
    noteMode_ = new QComboBox;
    noteMode_->addItem(B("Только в архиве", "Лише в архіві"), "local");
    noteMode_->addItem(B("Краткая пометка", "Коротка позначка"), "compact");
    noteMode_->addItem(B("Подробная пометка", "Докладна позначка"), "detailed");
    mediaMode_ = new QComboBox;
    mediaMode_->addItem(B("Текст и вложения", "Текст і вкладення"), "text_media");
    mediaMode_->addItem(B("Только текст", "Лише текст"), "text_only");
    mediaMode_->addItem(B("Текст и вложения как документы", "Текст і вкладення як документи"), "document");
    options->addRow(B("Профиль видео", "Профіль відео"), videoPolicy_);
    options->addRow(B("Пересланные сообщения", "Переслані повідомлення"), noteMode_);
    options->addRow(B("Включить в пакет", "Включити в пакет"), mediaMode_);
    analysis->addWidget(advancedOptions_);
    const auto invalidates = [this](int) { plan_ = {}; decisions_ = {}; setPlan({}); };
    for (auto *control : {videoPolicy_, noteMode_, mediaMode_})
        connect(control, &QComboBox::currentIndexChanged, this, invalidates);

    issues_ = new QTableWidget;
    issues_->setColumnCount(4);
    issues_->setHorizontalHeaderLabels({B("Вложение", "Вкладення"), B("Типы", "Типи"),
                                        B("Причина", "Причина"), B("Что сделать", "Що зробити")});
    issues_->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
    issues_->verticalHeader()->hide();
    issues_->setEditTriggers(QAbstractItemView::NoEditTriggers);
    issues_->setSelectionBehavior(QAbstractItemView::SelectRows);
    issues_->hide();
    analysis->addWidget(issues_, 1);
    applyMediaGroup_ = button(B("Применить к аналогичным файлам", "Застосувати до подібних файлів"), this, [this] {
        const int selected = issues_->currentRow();
        if (selected < 0)
            return;
        const auto group = issues_->item(selected, 0)->data(Qt::UserRole);
        const auto *source = qobject_cast<QComboBox *>(issues_->cellWidget(selected, 3));
        const auto action = source->currentData();
        if (action.toString().isEmpty())
            return;
        for (int row = 0; row < issues_->rowCount(); ++row)
            if (issues_->item(row, 0)->data(Qt::UserRole) == group) {
                auto *choice = qobject_cast<QComboBox *>(issues_->cellWidget(row, 3));
                const int option = choice->findData(action);
                if (option >= 0)
                    choice->setCurrentIndex(option);
            }
    });
    applyMediaGroup_->hide();
    analysis->addWidget(applyMediaGroup_, 0, Qt::AlignLeft);
    analysis->addWidget(button(B("Проверить выбранные параметры", "Перевірити вибрані параметри"), this,
                                [this] { plan(); }), 0, Qt::AlignLeft);
    wizard_->addWidget(analysis->parentWidget());

    auto *prepareStep = card();
    prepareStep->addWidget(label(B("Подготовьте пакет на этом компьютере. Выбор аккаунта и диалога Telegram откроется "
                                    "на следующем шаге.",
                                    "Підготуйте пакет на цьому комп’ютері. Вибір облікового запису та діалогу Telegram "
                                    "відкриється на наступному кроці.")));
    auto *prepareButton = button(B("Подготовить пакет", "Підготувати пакет"), this, [this] { prepare(); }, true);
    prepareButton->setObjectName("prepare-bundle");
    prepareStep->addWidget(prepareButton, 0, Qt::AlignLeft);
    prepareStep->addStretch();
    wizard_->addWidget(prepareStep->parentWidget());

    auto *result = card();
    result_ = label();
    result->addWidget(result_);
    result->addWidget(button(B("Проверить пакет", "Перевірити пакет"), this, [this] { verify(false); }), 0, Qt::AlignLeft);
    result->addWidget(button(B("Открыть серверный импорт…", "Відкрити серверний імпорт…"), this,
                              [this] { if (!bundle_.isEmpty()) selectTarget(); }, true), 0, Qt::AlignLeft);
    result->addStretch();
    wizard_->addWidget(result->parentWidget());

    auto *actions = new QHBoxLayout;
    actions->addWidget(button(B("Назад", "Назад"), this, [this] {
        if (currentStep_ > Source)
            step(currentStep_ - 1);
    }));
    continue_ = button(B("Далее", "Далі"), this, [this] {
        if (currentStep_ == Source && projectData_.isEmpty())
            return;
        if (currentStep_ == Dialog && !chat_->currentData().isValid())
            return;
        if (currentStep_ == Analysis && (plan_.isEmpty() || plan_["decisions_total"].toInt() > 0
                                         || plan_["summary"].toObject()["included"].toInteger() == 0))
            return;
        step(currentStep_ + 1);
        if (currentStep_ == Analysis)
            plan();
    }, true);
    continue_->setObjectName("wizard-next");
    actions->addWidget(continue_);
    actions->addStretch();
    layout->addLayout(actions);
    return page;
}

QWidget *Window::historyPage() {
    auto *page = new QWidget;
    auto *layout = column(page);
    layout->addWidget(label(B("История", "Історія"), "title"));
    auto *actions = new QHBoxLayout;
    actions->addWidget(button(B("Обновить", "Оновити"), this, [this] { showHistory(); }));
    auto *menuButton = new QPushButton(B("Действия…", "Дії…"));
    auto *menu = new QMenu(menuButton);
    menu->addAction(B("Обезличенный отчёт…", "Знеособлений звіт…"), this, [this] { operationDiagnostic(false); });
    menu->addAction(B("Локальный отчёт…", "Локальний звіт…"), this, [this] { operationDiagnostic(true); });
    menu->addAction(B("Открыть пакет операции…", "Відкрити пакет операції…"), this, [this] { operationBundle(); });
    menuButton->setMenu(menu);
    actions->addWidget(menuButton);
    actions->addStretch();
    layout->addLayout(actions);
    history_ = new QTableWidget;
    history_->setColumnCount(4);
    history_->setHorizontalHeaderLabels({B("Дата", "Дата"), B("Состояние", "Стан"),
                                         B("Записи", "Записи"), B("Медиа принято", "Медіа прийнято")});
    history_->setEditTriggers(QAbstractItemView::NoEditTriggers);
    history_->verticalHeader()->hide();
    history_->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
    layout->addWidget(history_, 1);
    auto *checks = card();
    checks->addWidget(label(B("Верификация", "Верифікація"), "subtitle"));
    diagnosis_ = muted();
    checks->addWidget(diagnosis_);
    checks->addWidget(button(B("Сверить с локальными снимками…", "Звірити з локальними знімками…"), this,
                              [this] { verify(true); }), 0, Qt::AlignLeft);
    layout->addWidget(checks->parentWidget());
    return page;
}

QWidget *Window::settingsPage() {
    auto *page = new QWidget;
    auto *layout = column(page);
    layout->addWidget(label(B("Настройки", "Налаштування"), "title"));
    auto *preferences = card();
    preferences->addWidget(label(B("Интерфейс", "Інтерфейс"), "subtitle"));
    advanced_ = new QCheckBox(B("Расширенный режим восстановления", "Розширений режим відновлення"));
    advanced_->setChecked(QSettings().value("native/advanced", false).toBool());
    advancedOptions_->setVisible(advanced_->isChecked());
    connect(advanced_, &QCheckBox::toggled, this, [this](bool enabled) {
        QSettings().setValue("native/advanced", enabled);
        advancedOptions_->setVisible(enabled);
    });
    preferences->addWidget(advanced_);
    auto *language = new QComboBox;
    language->addItem("Русский", "ru");
    language->addItem("Українська", "uk");
    language->setCurrentIndex(QSettings().value("language", "ru").toString() == "uk" ? 1 : 0);
    auto *form = new QFormLayout;
    form->setRowWrapPolicy(QFormLayout::WrapLongRows);
    form->addRow(B("Язык", "Мова"), language);
    auto *zone = new QComboBox;
    zone->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    zone->setMinimumContentsLength(14);
    const auto zones = QTimeZone::availableTimeZoneIds();
    zone->addItem(B("Часовой пояс Windows", "Часовий пояс Windows"), QString());
    const auto storedZone = QSettings().value("native/archive_zone").toString();
    for (const auto &id : zones)
        zone->addItem(QString::fromLatin1(id), QString::fromLatin1(id));
    zone->setCurrentIndex(std::max(0, zone->findData(storedZone)));
    connect(zone, &QComboBox::currentIndexChanged, this, [zone] {
        QSettings().setValue("native/archive_zone", zone->currentData());
    });
    form->addRow(B("Даты без часового пояса", "Дати без часового поясу"), zone);
    preferences->addLayout(form);
    auto *applyLanguage = button(B("Сохранить язык", "Зберегти мову"), this, [this, language] {
        if (service_.busy())
            return;
        QSettings settings;
        settings.setValue("language", language->currentData());
        settings.sync();
        status(B("Язык изменится при следующем запуске.", "Мова зміниться під час наступного запуску."));
    });
    preferences->addWidget(applyLanguage, 0, Qt::AlignLeft);
    layout->addWidget(preferences->parentWidget());
    auto *disk = card();
    disk->addWidget(label(B("Хранилище", "Сховище"), "subtitle"));
    storage_ = muted();
    disk->addWidget(storage_);
    auto *diskActions = new QHBoxLayout;
    diskActions->addWidget(button(B("Обновить размер", "Оновити розмір"), this, [this] { storage(); }));
    diskActions->addWidget(button(B("Очистить кеш…", "Очистити кеш…"), this, [this] {
        if (QMessageBox::question(this, "gTR",
            B("Удалить временные копии видео из кеша?", "Видалити тимчасові копії відео з кешу?"),
            QMessageBox::Yes | QMessageBox::No, QMessageBox::No) == QMessageBox::Yes) {
            task(B("Очистка кеша", "Очищення кешу"), "cache.clear", {{"confirmed", true}},
                 [this](const QJsonObject &) { storage(); });
        }
    }));
    diskActions->addStretch();
    disk->addLayout(diskActions);
    layout->addWidget(disk->parentWidget());
    auto *about = card();
    about->addWidget(label(B("О программе", "Про програму"), "subtitle"));
    about->addWidget(muted("gTR " GTR_RELEASE_LABEL " · Gruzin · Apache 2.0"));
    about->addWidget(button(B("Проверить обновления в gAssets…", "Перевірити оновлення в gAssets…"), this,
                             [this] { updates(true); }), 0, Qt::AlignLeft);
    about->addWidget(button(B("Открыть выпуски на GitHub…", "Відкрити випуски на GitHub…"), this, [] {
        QDesktopServices::openUrl(QUrl(QStringLiteral("https://github.com/Spalov/gTR/releases")));
    }), 0, Qt::AlignLeft);
    about->addWidget(button(B("Как подготовить экспорт…", "Як підготувати експорт…"), this, [this] {
        QMessageBox::information(this, "gTR",
            B("В Telegram Desktop откройте диалог, нажмите ⋮ → Экспорт истории чата. Выберите JSON или HTML "
              "и отметьте нужные вложения. Затем в разделе «Архив» откройте папку экспорта.",
              "У Telegram Desktop відкрийте діалог, натисніть ⋮ → Експорт історії чату. Виберіть JSON або HTML "
              "і позначте потрібні вкладення. Потім у розділі «Архів» відкрийте теку експорту."));
    }), 0, Qt::AlignLeft);
    layout->addWidget(about->parentWidget());
    layout->addStretch();
    return page;
}

void Window::go(int page) {
    if (page < 0)
        return;
    pages_->setCurrentIndex(page);
    if (page != Archive)
        stopPlayer();
    if (page == History && service_.available() && !service_.busy())
        showHistory();
    if (page == Settings && service_.available() && !service_.busy())
        storage();
}

void Window::step(int next) {
    currentStep_ = std::clamp(next, static_cast<int>(Source), static_cast<int>(Result));
    wizard_->setCurrentIndex(currentStep_);
    steps_->setCurrentRow(currentStep_);
    source_->setText(projectData_.isEmpty()
        ? B("Выберите экспорт или откройте готовый проект.", "Виберіть експорт або відкрийте готовий проєкт.")
        : projectData_["name"].toString());
    dialog_->setText(chat_->currentData().isValid() ? chat_->currentText()
                                                  : B("Выберите диалог", "Виберіть діалог"));
    continue_->setVisible(currentStep_ < Preparation);
    continue_->setEnabled((currentStep_ == Source && !projectData_.isEmpty())
        || (currentStep_ == Dialog && chat_->currentData().isValid()));
    if (currentStep_ == Analysis)
        setPlan(plan_);
}

void Window::refreshProjects() {
    task(B("Открытие проектов", "Відкриття проєктів"), "project.list", {}, [this](const QJsonObject &value) {
        recent_->clear();
        for (const auto &entry : value["projects"].toArray()) {
            const auto item = entry.toObject();
            auto *row = new QListWidgetItem(item["name"].toString(), recent_);
            row->setData(Qt::UserRole, item["path"].toString());
        }
        account_->setText(value["saved_session"].toBool()
            ? B("Telegram · сохранена сессия", "Telegram · збережено сесію")
            : B("Telegram · локальный режим", "Telegram · локальний режим"));
        if (!project_.isEmpty() && projectData_.isEmpty())
            task(B("Открытие проекта", "Відкриття проєкту"), "project.open", {{"project", project_}},
                 [this](const QJsonObject &project) { adoptProject(project); });
    });
}

void Window::readProject(const QString &path) {
    task(B("Открытие проекта", "Відкриття проєкту"), "project.open",
         {{"project", QDir(path).absolutePath()}}, [this](const QJsonObject &value) {
             adoptProject(value);
             nav_->setCurrentRow(Archive);
         });
}

void Window::chooseSource(bool folder) {
    const auto caption = B("Выбрать экспорт Telegram", "Вибрати експорт Telegram");
    const auto path = folder ? QFileDialog::getExistingDirectory(this, caption)
        : QFileDialog::getOpenFileName(this, caption, {}, B("Экспорты (*.zip *.rar *.html *.json)",
                                                         "Експорти (*.zip *.rar *.html *.json)"));
    if (path.isEmpty())
        return;
    const bool wizard = nav_->currentRow() == Recovery;
    auto zone = QSettings().value("native/archive_zone").toString();
    if (zone.isEmpty())
        zone = QString::fromLatin1(QTimeZone::systemTimeZoneId());
    if (zone.isEmpty())
        zone = "UTC";
    task(B("Анализ архива", "Аналіз архіву"), "archive.analyze",
         {{"source", path}, {"timezone", zone}, {"format", "auto"}}, [this, wizard](const QJsonObject &value) {
             adoptProject(value);
             const auto title = value["name"].toString();
             auto *entry = new QListWidgetItem(title);
             entry->setData(Qt::UserRole, project_);
             recent_->insertItem(0, entry);
             if (wizard)
                 step(Dialog);
             else
                 nav_->setCurrentRow(Archive);
         });
}

void Window::adoptProject(const QJsonObject &value) {
    projectData_ = value;
    project_ = value["path"].toString();
    QSettings().setValue("native/project", project_);
    const auto stats = value["summary"].toObject();
    overview_->setText(B("%1\nСообщений %2 · Вложений %3 · %4\n%5 — %6",
                         "%1\nПовідомлень %2 · Вкладень %3 · %4\n%5 — %6")
        .arg(value["name"].toString(), count(stats["messages"].toInteger()),
             count(stats["unique_media_files"].toInteger()), amount(stats["input_bytes"].toInteger()),
             date(stats["first"]), date(stats["last"])));
    {
        const QSignalBlocker block(chat_);
        chat_->clear();
        for (const auto &entry : value["chats"].toArray()) {
            const auto row = entry.toObject();
            chat_->addItem(row["title"].toString(), row["id"].toInt());
        }
    }
    selectChat();
}

void Window::selectChat() {
    plan_ = {};
    bundle_.clear();
    decisions_ = {};
    cursor_ = 0;
    selectionRevision_.clear();
    selectionFilters_ = {};
    if (selectionPreset_) {
        const QSignalBlocker block(selectionPreset_);
        selectionPreset_->setCurrentIndex(0);
    }
    {
        const QSignalBlocker block(query_);
        query_->clear();
    }
    records_->clear();
    showMessage({});
    step(Source);
    if (chat_->currentData().isValid())
        messages();
}

void Window::messages(bool next) {
    if (project_.isEmpty() || !chat_->currentData().isValid() || (next && cursor_ <= 0))
        return;
    auto request = selectionScope();
    request["filters"] = selectionFilters();
    if (next)
        request["before"] = cursor_;
    task(B("Чтение архива", "Читання архіву"), "selection.page", request, [this](const QJsonObject &value) {
        const auto preset = selectionPreset_->currentData().toString();
        const QSignalBlocker block(selectionPreset_);
        selectionPreset_->clear();
        selectionPreset_->addItem(B("Основная выборка", "Основна вибірка"), "");
        for (const auto &name : value["presets"].toArray())
            selectionPreset_->addItem(name.toString(), name.toString());
        selectionPreset_->setCurrentIndex(std::max(0, selectionPreset_->findData(preset)));
        selectionRevision_ = value["revision"].toString();
        undoSelection_->setEnabled(value["undo"].toBool());
        redoSelection_->setEnabled(value["redo"].toBool());
        records_->clear();
        QString lastDay;
        for (const auto &entry : value["records"].toArray()) {
            const auto row = entry.toObject();
            const auto when = date(row["date"]);
            const auto day = when.left(10);
            if (day != lastDay) {
                auto *divider = new QStandardItem(day);
                divider->setFlags(Qt::ItemIsEnabled);
                divider->setData(QColor("#a5b6bd"), Qt::ForegroundRole);
                records_->appendRow(divider);
                lastDay = day;
            }
            const auto sender = row["sender"].toString().isEmpty() ? B("Автор неизвестен", "Автор невідомий")
                                                                   : row["sender"].toString();
            QString text = row["text"].toString().simplified().left(190);
            if (text.isEmpty() && row["attachments"].toInt())
                text = B("Вложение", "Вкладення");
            if (row["is_forward"].toInt())
                text = B("Переслано · %1", "Переслано · %1").arg(text);
            auto *item = new QStandardItem(sender + "  ·  " + when.mid(11, 8) + "\n" + text);
            if (row["excluded"].toInt()) {
                auto style = item->font();
                style.setStrikeOut(true);
                item->setFont(style);
            }
            item->setEditable(false);
            item->setData(row, RecordData);
            records_->appendRow(item);
        }
        cursor_ = value["before"].toInteger();
        older_->setEnabled(cursor_ > 0);
    });
}

QJsonObject Window::selectionScope() const {
    return {{"project", project_}, {"chat", chat_->currentData().toInt()},
            {"preset", selectionPreset_->currentData().toString()}};
}

QJsonObject Window::selectionFilters() const {
    auto filters = selectionFilters_;
    filters["query"] = query_->text().trimmed();
    filters["view"] = selectionView_->currentData().toString();
    return filters;
}

void Window::editSelection(const QString &action, QJsonObject params) {
    if (selectionRevision_.isEmpty())
        return;
    const auto scope = selectionScope();
    for (auto it = scope.begin(); it != scope.end(); ++it)
        params[it.key()] = it.value();
    params["action"] = action;
    params["revision"] = selectionRevision_;
    task(B("Сохранение выборки", "Збереження вибірки"), "selection.edit", params, [this, action](const QJsonObject &value) {
        if (action != "mark") {
            const auto name = value["preset"].toString();
            if (selectionPreset_->findData(name) < 0)
                selectionPreset_->addItem(name, name);
            selectionPreset_->setCurrentIndex(selectionPreset_->findData(name));
            plan_ = {};
            bundle_.clear();
            step(Source);
        }
        messages();
    });
}

void Window::selectionFilterDialog() {
    QDialog dialog(this);
    dialog.setWindowTitle(B("Фильтры архива", "Фільтри архіву"));
    QFormLayout form(&dialog);
    QComboBox field, kind;
    field.addItem(B("Все поля", "Усі поля"), "all");
    field.addItem(B("Текст", "Текст"), "text");
    field.addItem(B("Автор", "Автор"), "sender");
    field.addItem(B("Исходный ID", "Вихідний ID"), "id");
    field.addItem(B("Имя файла", "Назва файла"), "file");
    kind.addItem(B("Все записи", "Усі записи"), "");
    kind.addItem(B("Текст", "Текст"), "text");
    kind.addItem(B("Пересланные", "Переслані"), "forward");
    kind.addItem(B("Служебные", "Службові"), "service");
    for (const auto &type : {"photo", "video", "sticker", "voice", "audio", "document"})
        kind.addItem(mediaKind(type), type);
    field.setCurrentIndex(std::max(0, field.findData(selectionFilters_["field"].toString())));
    kind.setCurrentIndex(std::max(0, kind.findData(selectionFilters_["kind"].toString())));
    QLineEdit from(selectionFilters_["start"].toString()), to(selectionFilters_["end"].toString());
    for (auto *edit : {&from, &to}) {
        edit->setPlaceholderText("YYYY-MM-DD");
        edit->setMaxLength(10);
    }
    form.addRow(B("Искать по", "Шукати за"), &field);
    form.addRow(B("Тип записи", "Тип запису"), &kind);
    form.addRow(B("С даты включительно", "Від дати включно"), &from);
    form.addRow(B("По дату включительно", "До дати включно"), &to);
    QDialogButtonBox controls(QDialogButtonBox::Ok | QDialogButtonBox::Cancel);
    form.addRow(&controls);
    connect(&controls, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
    connect(&controls, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    if (dialog.exec() == QDialog::Accepted) {
        selectionFilters_ = {{"field", field.currentData().toString()}, {"kind", kind.currentData().toString()},
                             {"start", from.text().trimmed()}, {"end", to.text().trimmed()}};
        messages();
    }
}

void Window::selectionPreview() {
    if (project_.isEmpty() || !chat_->currentData().isValid())
        return;
    task(B("Подсчёт выборки", "Підрахунок вибірки"), "selection.preview", selectionScope(), [this](const QJsonObject &value) {
        QMessageBox box(QMessageBox::Information, B("Будущий импорт", "Майбутній імпорт"),
            B("Включено сообщений %1 из %2 · исключено записей %3\nВложения %4 · исходный объём %5\n%6 — %7\n"
              "Разрывы %8 · отсутствующие адресаты ответов %9 · разделённые альбомы %10\n"
              "Служебных записей только локально %11. Telegram может изменить отображение медиа и связей.",
              "Включено повідомлень %1 із %2 · виключено записів %3\nВкладення %4 · вихідний обсяг %5\n%6 — %7\n"
              "Розриви %8 · відсутні адресати відповідей %9 · розділені альбоми %10\n"
              "Службових записів лише локально %11. Telegram може змінити відображення медіа й зв’язків.")
            .arg(count(value["selected"].toInteger()), count(value["total"].toInteger()),
                 count(value["excluded"].toInteger()), count(value["attachments"].toInteger()),
                 amount(value["source_bytes"].toInteger()), date(value["first"]), date(value["last"]),
                 count(value["gaps"].toInteger()), count(value["broken_replies"].toInteger()),
                 count(value["split_albums"].toInteger()), count(value["service"].toInteger())), QMessageBox::Ok, this);
        box.setTextFormat(Qt::PlainText);
        if (value["partial"].toBool())
            box.setInformativeText(B("Выбрана часть истории.", "Вибрано частину історії."));
        box.exec();
    });
}

void Window::showMessage(const QModelIndex &index) {
    stopPlayer();
    picture_->clear();
    picture_->hide();
    selectedRecord_ = index.data(RecordData).toJsonObject();
    if (selectedRecord_.isEmpty()) {
        message_->setText(B("Выберите сообщение", "Виберіть повідомлення"));
        preview_->hide();
        return;
    }
    QString text = selectedRecord_["sender"].toString() + "\n" + date(selectedRecord_["date"]);
    if (const auto ordinal = selectedRecord_["ordinal"].toInt(); ordinal > 0)
        text += "\n" + B("Запись %1 · исходный ID %2", "Запис %1 · вихідний ID %2")
            .arg(ordinal).arg(selectedRecord_["source_id"].toString());
    if (const auto note = selectedRecord_["note"].toString(); !note.isEmpty())
        text += "\n" + B("Локально · %1", "Локально · %1").arg(note);
    if (selectedRecord_["is_forward"].toInt()) {
        auto source = selectedRecord_["forwarded"].toString();
        if (source.isEmpty())
            source = B("источник неизвестен", "джерело невідоме");
        text += "\n" + B("Переслано от %1", "Переслано від %1").arg(source);
        if (selectedRecord_["forward_privacy"].toString() == "hidden")
            text += " · " + B("профиль скрыт", "профіль приховано");
    }
    text += "\n\n" + selectedRecord_["text"].toString();
    if (selectedRecord_["attachments"].toInt())
        text += "\n\n" + B("Вложений %1", "Вкладень %1").arg(count(selectedRecord_["attachments"].toInt()));
    message_->setText(text.trimmed());
    preview_->setVisible(selectedRecord_["attachment"].toInteger() > 0);
}

void Window::stopPlayer() {
    player_->stop();
    player_->setSource({});
    video_->hide();
    play_->hide();
    seek_->hide();
    duration_->hide();
}

void Window::preview() {
    const auto asset = selectedRecord_["attachment"].toInteger();
    task(B("Проверка вложения", "Перевірка вкладення"), "media.inspect", {{"project", project_}, {"asset", asset}},
         [this, asset](const QJsonObject &result) {
             if (asset != selectedRecord_["attachment"].toInteger())
                 return;
             const auto path = result["path"].toString();
             if (path.isEmpty()) {
                 status(B("Файл отсутствует, изменён или повреждён.", "Файл відсутній, змінений або пошкоджений."));
                 return;
             }
             if (result["image"].toBool()) {
                 QImageReader reader(path);
                 reader.setDecideFormatFromContent(true);
                 reader.setAutoTransform(true);
                 const auto size = reader.size();
                 if (size.isValid() && qint64(size.width()) * size.height() <= 16'000'000) {
                     reader.setScaledSize(size.scaled(QSize(245, 220), Qt::KeepAspectRatio));
                     const auto image = reader.read();
                     if (!image.isNull()) {
                         picture_->setPixmap(QPixmap::fromImage(image));
                         picture_->show();
                         return;
                     }
                 }
                 status(B("Предпросмотр этого изображения недоступен.", "Попередній перегляд цього зображення недоступний."));
             } else if (result["playback"].toBool()) {
                 player_->setSource(QUrl::fromLocalFile(path));
                 video_->setVisible(result["mime"].toString().startsWith("video/"));
                 play_->show();
                 seek_->show();
                 duration_->show();
             } else {
                 status(B("Документ сохранён в архиве · %1 · %2", "Документ збережено в архіві · %1 · %2")
                     .arg(QFileInfo(path).fileName(), amount(result["size"].toInteger())));
             }
         });
}

QJsonObject Window::recoveryOptions() const {
    return {{"project", project_}, {"chat", chat_->currentData().toInt()}, {"media", mediaMode_->currentData().toString()},
            {"video", videoPolicy_->currentData().toString()}, {"notes", noteMode_->currentData().toString()},
            {"language", QSettings().value("language", "ru").toString()}, {"decisions", decisions_},
            {"preset", selectionPreset_->currentData().toString()}};
}

void Window::plan() {
    task(B("Проверка истории и вложений", "Перевірка історії та вкладень"), "recovery.plan", recoveryOptions(),
         [this](const QJsonObject &value) { setPlan(value); });
}

void Window::setPlan(const QJsonObject &value) {
    plan_ = value;
    const auto totals = value["summary"].toObject();
    const auto entries = value["decisions"].toArray();
    const auto waiting = value["decisions_total"].toInt();
    QString report = value.isEmpty() ? B("Проверьте выбранные параметры.", "Перевірте вибрані параметри.")
                                    : planText(totals);
    if (value["selection"].toObject()["partial"].toBool())
        report.prepend(B("Выборочное восстановление · часть истории\n", "Вибіркове відновлення · частина історії\n"));
    if (waiting > 0)
        report += "\n" + B("Вложений с выбором %1", "Вкладень із вибором %1").arg(count(waiting));
    if (waiting > entries.size())
        report += "\n" + B("Показаны первые %1. После выбора повторите проверку.",
                            "Показано перші %1. Після вибору повторіть перевірку.").arg(count(entries.size()));
    analysis_->setText(report);
    issues_->setRowCount(0);
    issues_->setVisible(!entries.isEmpty());
    applyMediaGroup_->setVisible(entries.size() > 1);
    for (const auto &issue : entries) {
        const auto entry = issue.toObject();
        const auto index = issues_->rowCount();
        issues_->insertRow(index);
        issues_->setItem(index, 0, new QTableWidgetItem(entry["name"].toString()));
        issues_->item(index, 0)->setData(Qt::UserRole, entry["group"].toString());
        const auto types = B("Архив · %1\nПакет · %2\nTelegram · %3", "Архів · %1\nПакет · %2\nTelegram · %3")
            .arg(mediaKind(entry["source_kind"].toString()), mediaKind(entry["prepared_kind"].toString()),
                 mediaKind(entry["received_kind"].toString()));
        issues_->setItem(index, 1, new QTableWidgetItem(types));
        const auto reason = mediaReason(entry["reason"].toString());
        issues_->setItem(index, 2, new QTableWidgetItem(reason));
        issues_->item(index, 2)->setToolTip(reason);
        auto *choice = new QComboBox;
        choice->addItem(B("Выберите действие", "Виберіть дію"), QString());
        for (const auto &option : entry["options"].toArray())
            choice->addItem(optionName(option.toString()), option.toString());
        const auto key = entry["key"].toString();
        if (const auto recommendation = entry["recommended"].toString(); !recommendation.isEmpty())
            choice->setToolTip(B("Рекомендуется %1", "Рекомендовано %1").arg(optionName(recommendation)));
        connect(choice, &QComboBox::currentIndexChanged, this, [this, key, choice] {
            const auto action = choice->currentData().toString();
            if (action.isEmpty())
                decisions_.remove(key);
            else
                decisions_[key] = action;
        });
        issues_->setCellWidget(index, 3, choice);
    }
    issues_->resizeRowsToContents();
    if (currentStep_ == Analysis)
        continue_->setEnabled(waiting == 0 && totals["included"].toInteger() > 0);
}

void Window::prepare() {
    if (plan_.isEmpty())
        return;
    const auto totals = plan_["summary"].toObject();
    const auto question = chat_->currentText() + "\n\n" + planText(totals) + "\n\n" +
        B("Подготовить пакет на этом компьютере?", "Підготувати пакет на цьому комп’ютері?");
    if (QMessageBox::question(this, "gTR", question, QMessageBox::Yes | QMessageBox::No, QMessageBox::No)
        != QMessageBox::Yes)
        return;
    auto params = recoveryOptions();
    params["confirmed"] = true;
    params["snapshot"] = plan_["snapshot"];
    task(B("Подготовка пакета", "Підготовка пакета"), "recovery.prepare", params, [this](const QJsonObject &value) {
        bundle_ = value["path"].toString();
        manifest_ = value["manifest"].toString();
        result_->setText(planText(value["summary"].toObject()));
        step(Result);
    });
}

void Window::showHistory() {
    task(B("Чтение журнала", "Читання журналу"), "operation.list", {}, [this](const QJsonObject &value) {
        const auto entries = value["operations"].toArray();
        history_->setRowCount(static_cast<int>(entries.size()));
        for (qsizetype index = 0; index < entries.size(); ++index) {
            const auto row = entries[index].toObject();
            const QStringList columns{date(row["created"]), operationState(row["state"].toString()),
                                      count(row["messages"].toInteger()),
                                      QStringLiteral("%1 / %2").arg(row["media_done"].toInt()).arg(row["media"].toInt())};
            for (int col = 0; col < columns.size(); ++col) {
                auto *cell = new QTableWidgetItem(columns[col]);
                cell->setData(Qt::UserRole, row["id"].toString());
                history_->setItem(static_cast<int>(index), col, cell);
            }
        }
    });
}

void Window::storage() {
    task(B("Проверка хранилища", "Перевірка сховища"), "storage.info", {}, [this](const QJsonObject &value) {
        storage_->setText(B("Проекты %1 · Исходные файлы %2\nПакеты %3 · Кеш %4 · Временные файлы %5\nСвободно %6",
                            "Проєкти %1 · Вихідні файли %2\nПакети %3 · Кеш %4 · Тимчасові файли %5\nВільно %6")
            .arg(amount(value["projects"].toInteger()), amount(value["originals"].toInteger()),
                 amount(value["packages"].toInteger()), amount(value["cache"].toInteger()),
                 amount(value["temporary"].toInteger()), amount(value["free"].toInteger())));
    });
}

void Window::verify(bool snapshots) {
    QString root = snapshots && !historyBundle_.isEmpty() ? historyBundle_ : bundle_;
    if (root.isEmpty())
        root = QFileDialog::getExistingDirectory(this, B("Выберите папку пакета", "Виберіть теку пакета"));
    if (root.isEmpty())
        return;
    QJsonObject request{{"bundle", root}};
    if (!project_.isEmpty() && (!snapshots || historyBundle_.isEmpty()))
        request["project"] = project_;
    if (snapshots) {
        const auto captures = QFileDialog::getOpenFileNames(this, B("Выберите один или два снимка",
                                                                   "Виберіть один або два знімки"), {}, "JSON (*.json)");
        if (captures.size() < 1 || captures.size() > 2)
            return;
        request["captures"] = QJsonArray::fromStringList(captures);
        if (captures.size() == 2) {
            request["reconcile"] = QMessageBox::question(this, "gTR",
                B("После сверки записать подтверждённые совпадения в локальный реестр операций?",
                  "Після звірки записати підтверджені збіги до локального реєстру операцій?"),
                QMessageBox::Yes | QMessageBox::No, QMessageBox::No) == QMessageBox::Yes;
        }
    }
    task(B("Верификация", "Верифікація"), "verify.local", request, [this, snapshots](const QJsonObject &value) {
        if (snapshots) {
            diagnosis_->setText(B("На сервере найдено записей %1 · Проверено с двух сторон %2 · Вложений без привязки %3",
                                  "На сервері знайдено записів %1 · Перевірено з двох боків %2 · Вкладень без прив’язки %3")
                .arg(count(value["server_messages_observed"].toInteger()), count(value["client_messages_verified"].toInteger()),
                     count(value["unresolved_attachments"].toInteger())));
        } else {
            status(value["status"].toString() == "verified" ? B("Пакет совпадает с локальным проектом.",
                                                                "Пакет збігається з локальним проєктом.")
                : B("Хеши пакета проверены. Сверка с проектом не подтверждена.",
                    "Хеші пакета перевірено. Звіряння з проєктом не підтверджено."));
        }
    });
}

void Window::updates(bool prompt) {
    const auto local = qEnvironmentVariable("LOCALAPPDATA");
    if (local.isEmpty())
        return;
    const bool test = QCoreApplication::applicationName().endsWith("beta7-gassets");
    QString systemLocal = local;
    if (test) {
        QDir dir(local);
        for (int i = 0; i < 3; ++i)
            dir.cdUp();
        systemLocal = dir.absolutePath();
    }
    const auto key = QStringLiteral("HKEY_CURRENT_USER\\Software\\gAssets\\UpdateCenter") + (test ? ".test" : "");
    QSettings registry(key, QSettings::NativeFormat);
    const auto folder = test ? "gAssets-Test" : "gAssets";
    const auto programRoot = registry.value("ProgramRoot", QDir(systemLocal).filePath(QString("Programs/") + folder)).toString();
    const auto path = QDir(programRoot).filePath("UpdateCenter/gAssets.exe");
    const bool installed = QFileInfo(path).isFile();
    const auto stateRoot = QDir(systemLocal).filePath(QString(folder) + "/UpdateCenter/gtr-update-status.json");
    QFile cache(stateRoot);
    QJsonObject info;
    if (installed && cache.open(QIODevice::ReadOnly) && cache.size() <= 16 * 1024)
        info = QJsonDocument::fromJson(cache.readAll()).object();
    update_->setVisible(info["available"].toBool());
    if (info["available"].toBool())
        update_->setText(B("Открыть обновление", "Відкрити оновлення"));
    if (!prompt)
        return;
    if (!installed) {
        status(B("gAssets доступен в общем установщике gTR. Для портативной копии откройте выпуски на GitHub.",
                 "gAssets доступний у спільному інсталяторі gTR. Для портативної копії відкрийте випуски на GitHub."));
    } else if (!QFileInfo(programRoot).isAbsolute() || !QProcess::startDetached(path, {}, QFileInfo(path).absolutePath())) {
        status(B("Не удалось открыть gAssets. Новую версию можно скачать со страницы выпусков на GitHub.",
                 "Не вдалося відкрити gAssets. Нову версію можна завантажити зі сторінки випусків на GitHub."));
    }
}

void Window::task(const QString &name, const QString &method, const QJsonObject &params,
                  std::function<void(const QJsonObject &)> done) {
    if (!service_.available() || service_.busy())
        return;
    progress_->setRange(0, 0);
    progress_->show();
    cancel_->setEnabled(true);
    cancel_->show();
    pages_->setEnabled(false);
    status(name);
    service_.send(method, params, [this, method, done = std::move(done)](const QJsonObject &value, const QString &error) {
        progress_->hide();
        cancel_->hide();
        pages_->setEnabled(true);
        if (error.isEmpty()) {
            status(B("Готово", "Готово"));
            done(value);
        } else if (error == "MEDIA_DECISION_REQUIRED" && wizard_ && !plan_.isEmpty()) {
            auto revised = plan_;
            revised["decisions"] = value["decisions"];
            revised["decisions_total"] = value["decisions_total"];
            step(Analysis);
            setPlan(revised);
            status(fault(error));
        } else {
            const auto issue = value["package_issue"].toObject();
            const auto reason = issue.isEmpty() ? fault(error) :
                (QSettings().value("language").toString() == "uk" ? fault(error) : issue["reason"].toString());
            status(reason);
            if (error != "CANCELLED") {
                if (method.startsWith("import.")) {
                    rejectedMedia_ = error == "MEDIA_TYPE_CHANGED" || error == "MEDIA_INVALID" || error == "MEDIA_LOCAL_INVALID"
                        ? value : QJsonObject{};
                }
                const auto stage = value["stage"].toString();
                lastDiagnostic_ = {{"schema", 1}, {"redacted", false},
                                   {"failure", QJsonObject{{"code", error}, {"stage", stage.isEmpty() ? method : stage},
                                                           {"method", method}, {"reason", reason}}}};
                if (value["progress"].isObject())
                    lastDiagnostic_["progress"] = value["progress"];
                saveDiagnostic_->show();
                if (!issue.isEmpty()) {
                    lastDiagnostic_["package_issue"] = issue;
                    QStringList details{reason};
                    for (const auto &[field, title] : {
                             std::pair{"stage", B("Этап", "Етап")}, std::pair{"source_id", B("ID сообщения", "ID повідомлення")},
                             std::pair{"file", B("Файл", "Файл")}, std::pair{"field", B("Поле", "Поле")},
                             std::pair{"expected", B("Ожидалось", "Очікувалося")}, std::pair{"actual", B("Получено", "Отримано")},
                         }) {
                        const auto entry = issue[field].toVariant().toString();
                        if (!entry.isEmpty())
                            details.append(title + " · " + entry);
                    }
                    details.append(issue["action"].toString());
                    diagnosis_->setText(details.join("\n"));
                    status_->setToolTip(details.join("\n"));
                    status(error + " · " + reason);
                }
                if (error == "PHONE_CODE_INVALID" || error == "PASSWORD_HASH_INVALID") {
                    const auto password = error == "PASSWORD_HASH_INVALID";
                    const auto retry = password ? B("Пароль отклонён. Ввести ещё раз?", "Пароль відхилено. Ввести ще раз?")
                                                : B("Код отклонён. Ввести ещё раз?", "Код відхилено. Ввести ще раз?");
                    if (QMessageBox::question(this, "Telegram", retry, QMessageBox::Yes | QMessageBox::No,
                                              QMessageBox::No) == QMessageBox::Yes)
                        answerLogin(method.startsWith("test.") ? "test" : "account",
                                    {{"challenge", password ? "password" : "code"}});
                }
            }
        }
    });
}

void Window::saveValidationDiagnostic() {
    if (lastDiagnostic_.isEmpty())
        return;
    auto path = QFileDialog::getSaveFileName(this, B("Сохранить диагностический отчёт", "Зберегти діагностичний звіт"),
                                            "gTR-diagnostic.json", B("Отчёты JSON (*.json)", "Звіти JSON (*.json)"));
    if (path.isEmpty())
        return;
    if (!path.endsWith(".json", Qt::CaseInsensitive))
        path += ".json";
    const auto absolute = QDir::cleanPath(QFileInfo(path).absoluteFilePath());
    for (const auto &root : {project_, bundle_}) {
        if (!root.isEmpty() && absolute.startsWith(QDir::cleanPath(QDir(root).absolutePath()) + "/", Qt::CaseInsensitive)) {
            status(B("Сохраните отчёт отдельно от проекта и пакета.", "Збережіть звіт окремо від проєкту і пакета."));
            return;
        }
    }
    QSaveFile output(path);
    const auto contents = QJsonDocument(lastDiagnostic_).toJson();
    if (!output.open(QIODevice::WriteOnly) || output.write(contents) != contents.size() || !output.commit()) {
        status(B("Не удалось сохранить диагностический отчёт.", "Не вдалося зберегти діагностичний звіт."));
        return;
    }
    status(B("Диагностический отчёт сохранён.", "Діагностичний звіт збережено."));
}

void Window::status(const QString &message) { status_->setText(message); }

QString Window::fault(const QString &code) const {
    if (code == "CANCELLED")
        return B("Задача отменена", "Завдання скасовано");
    if (code == "MEDIA_DECISION_REQUIRED")
        return B("Откройте анализ и выберите способ подготовки вложений.", "Відкрийте аналіз і виберіть спосіб підготовки вкладень.");
    if (code == "UNRESOLVED_ATTACHMENT")
        return B("Не удалось связать вложение. Подготовьте пакет заново.", "Не вдалося зв’язати вкладення. Підготуйте пакет заново.");
    if (code == "DISK_SPACE")
        return B("Недостаточно места на диске.", "Недостатньо місця на диску.");
    if (code == "MEMORY_LIMIT")
        return B("Недостаточно памяти для задачи.", "Недостатньо пам’яті для завдання.");
    if (code == "SELECTION_STALE")
        return B("Выборка изменилась. Проверьте её и подготовьте новый пакет.", "Вибірка змінилася. Перевірте її та підготуйте новий пакет.");
    if (code == "REPLAY_BLOCKED")
        return B("Откройте серверный импорт. Закройте прерванную подготовку, если этот пункт доступен, или проверьте историю.",
                 "Відкрийте серверний імпорт. Закрийте перервану підготовку, якщо цей пункт доступний, або перевірте історію.");
    if (code == "TEST_RESET_REQUIRED")
        return B("Выберите «Сбросить реестр тестовых импортов…» и подтвердите пустую историю на обоих аккаунтах.",
                 "Виберіть «Скинути реєстр тестових імпортів…» і підтвердьте порожню історію на обох облікових записах.");
    if (code == "TEST_PACKAGE_REQUIRED")
        return B("Выберите «Подготовить тестовый пакет» для текущего тестового цикла.",
                 "Виберіть «Підготувати тестовий пакет» для поточного тестового циклу.");
    if (code == "TEST_HISTORY_NOT_EMPTY")
        return B("У одного из аккаунтов в истории диалога ещё есть сообщения. Проверьте историю с обеих сторон.",
                 "В одного з облікових записів в історії діалогу ще є повідомлення. Перевірте історію з обох сторін.");
    if (code == "SAVED_DIALOG_UNAVAILABLE")
        return B("Сохранённый диалог недоступен. Добавьте второй аккаунт в контакты Telegram и обновите список диалогов.",
                 "Збережений діалог недоступний. Додайте другий обліковий запис до контактів Telegram і оновіть список діалогів.");
    if (code == "START_UNCERTAIN")
        return B("Ответ о запуске потерян. Сверьте историю обоих участников перед дальнейшими действиями.",
                 "Відповідь про запуск втрачено. Звірте історію обох учасників перед наступними діями.");
    if (code == "IPC_TIMEOUT")
        return B("Движок не завершил этап вовремя. Проверьте журнал перед продолжением.",
                 "Рушій не завершив етап вчасно. Перевірте журнал перед продовженням.");
    if (code == "TIMEOUT" || code == "CONNECTION_LOST")
        return B("Связь с Telegram прервана. Проверьте состояние операции перед новой попыткой.",
                 "Зв’язок із Telegram перервано. Перевірте стан операції перед новою спробою.");
    if (code == "FILE_NOT_FOUND")
        return B("Файл пакета недоступен. Проверьте путь и при необходимости подготовьте пакет из проекта.",
                 "Файл пакета недоступний. Перевірте шлях і за потреби підготуйте пакет із проєкту.");
    if (code == "LOCAL_VALIDATION_FAILED")
        return B("Движок остановил проверку пакета. Сохраните отчёт.", "Рушій зупинив перевірку пакета. Збережіть звіт.");
    if (code == "MEDIA_TYPE_CHANGED" || code == "MEDIA_INVALID" || code == "MEDIA_LOCAL_INVALID")
        return B("Вложение отклонено. Сохраните отчёт и проверьте способ подготовки.", "Вкладення відхилено. Збережіть звіт і перевірте спосіб підготовки.");
    if (code == "FLOOD_WAIT")
        return B("Telegram ограничил частоту запросов. Интервал ожидания указан в отчёте.",
                 "Telegram обмежив частоту запитів. Інтервал очікування вказано у звіті.");
    if (code == "VERSION_MISMATCH")
        return B("Версии оболочки и службы различаются. Распакуйте полный комплект.", "Версії оболонки й служби різняться. Розпакуйте повний комплект.");
    if (code == "RECOVERY_ERROR" || code == "IO_ERROR")
        return B("Проверьте формат, доступность файла и содержимое проекта.", "Перевірте формат, доступність файлу й вміст проєкту.");
    if (code == "INVALID_PARAMS")
        return B("Проверьте параметры задачи.", "Перевірте параметри завдання.");
    return B("Локальная служба не ответила. Повторите действие после перезапуска.",
             "Локальна служба не відповіла. Повторіть дію після перезапуску.");
}

void Window::closeEvent(QCloseEvent *event) {
    stopPlayer();
    service_.stop();
    QMainWindow::closeEvent(event);
}

Window::~Window() {
    service_.stop();
}
