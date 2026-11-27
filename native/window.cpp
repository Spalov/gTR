#include "window.hpp"
#include "release.hpp"
#include "translation.hpp"

#include <QApplication>
#include <QAudioOutput>
#include <QCheckBox>
#include <QCloseEvent>
#include <QComboBox>
#include <QDesktopServices>
#include <QDir>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QFrame>
#include <QHeaderView>
#include <QImageReader>
#include <QLabel>
#include <QLineEdit>
#include <QListView>
#include <QListWidget>
#include <QMediaPlayer>
#include <QMenu>
#include <QMessageBox>
#include <QProgressBar>
#include <QPushButton>
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
    return B("Сообщений в пакете %1 из %2\nВложений %3 · %4\nПропущено записей %5 · отсутствует файлов %6\n"
             "Пересылок %7 · исторических пометок %8",
             "Повідомлень у пакеті %1 із %2\nВкладень %3 · %4\nПропущено записів %5 · відсутньо файлів %6\n"
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
    head->addWidget(button(B("Аккаунт", "Обліковий запис"), this, [this] { classic("telegram"); }));
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
            progress_->setToolTip(QStringLiteral("%1 / %2").arg(count(done), count(total)));
        }
        if (stage == "convert_video")
            status(B("Подготовка и проверка видео", "Підготовка й перевірка відео"));
        else if (stage == "media")
            status(B("Вложения %1 из %2", "Вкладення %1 із %2").arg(count(done), count(total)));
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
    layout->addLayout(filters);
    connect(chat_, &QComboBox::currentIndexChanged, this, [this] { selectChat(); });
    connect(query_, &QLineEdit::returnPressed, this, [this] { messages(); });
    connect(query_, &QLineEdit::textEdited, this, [this] {
        cursor_ = 0;
        older_->setEnabled(false);
    });
    auto *split = new QSplitter(Qt::Horizontal);
    auto *list = new QListView;
    list->setObjectName("archive-records");
    list->setWordWrap(true);
    list->setTextElideMode(Qt::ElideRight);
    list->setAccessibleName(B("Записи диалога", "Записи діалогу"));
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
    detailsScroll->setMinimumWidth(210);
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
                              [this] { if (!bundle_.isEmpty()) classic("import"); }, true), 0, Qt::AlignLeft);
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
    actions->addWidget(button(B("Журнал операций…", "Журнал операцій…"), this, [this] { classic("history"); }));
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
    auto *applyLanguage = button(B("Сохранить язык и перезапустить", "Зберегти мову й перезапустити"), this, [this, language] {
        if (service_.busy())
            return;
        QSettings settings;
        settings.setValue("language", language->currentData());
        settings.sync();
        auto args = QCoreApplication::arguments();
        args.removeFirst();
        if (QProcess::startDetached(QCoreApplication::applicationFilePath(), args, root_))
            close();
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
    about->addWidget(button(B("Проверить загрузки…", "Перевірити завантаження…"), this,
                             [this] { updates(true); }), 0, Qt::AlignLeft);
    about->addWidget(button(B("Дополнительные настройки…", "Додаткові налаштування…"), this,
                             [this] { classic("settings"); }), 0, Qt::AlignLeft);
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
    QJsonObject request{{"project", project_}, {"chat", chat_->currentData().toInt()}, {"query", query_->text().trimmed()}};
    if (next)
        request["before"] = cursor_;
    task(B("Чтение архива", "Читання архіву"), "chat.page", request, [this](const QJsonObject &value) {
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
            item->setEditable(false);
            item->setData(row, RecordData);
            records_->appendRow(item);
        }
        cursor_ = value["before"].toInteger();
        older_->setEnabled(cursor_ > 0);
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
            {"language", QSettings().value("language", "ru").toString()}, {"decisions", decisions_}};
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
    task(B("Подготовка пакета", "Підготовка пакета"), "recovery.prepare", params, [this](const QJsonObject &value) {
        bundle_ = value["path"].toString();
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
            for (int col = 0; col < columns.size(); ++col)
                history_->setItem(static_cast<int>(index), col, new QTableWidgetItem(columns[col]));
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
    QString root = bundle_;
    if (root.isEmpty())
        root = QFileDialog::getExistingDirectory(this, B("Выберите папку пакета", "Виберіть теку пакета"));
    if (root.isEmpty())
        return;
    QJsonObject request{{"bundle", root}};
    if (!project_.isEmpty())
        request["project"] = project_;
    if (snapshots) {
        const auto captures = QFileDialog::getOpenFileNames(this, B("Выберите один или два снимка",
                                                                   "Виберіть один або два знімки"), {}, "JSON (*.json)");
        if (captures.size() < 1 || captures.size() > 2)
            return;
        request["captures"] = QJsonArray::fromStringList(captures);
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

void Window::classic(const QString &section) {
    auto python = QFileInfo(python_).dir().filePath("pythonw.exe");
    if (!QFileInfo::exists(python))
        python = python_;
    QStringList args{"-m", "gtr", "classic", "--section", section};
    if (!projectData_.isEmpty())
        args << "--project" << project_;
    if (section == "import" && !bundle_.isEmpty())
        args << "--bundle" << bundle_;
    if (!QProcess::startDetached(python, args, root_))
        status(B("Не удалось открыть прежний интерфейс.", "Не вдалося відкрити попередній інтерфейс."));
}

void Window::updates(bool prompt) {
    if (!service_.available() || service_.busy())
        return;
    service_.send("update.check", {}, [this, prompt](const QJsonObject &value, const QString &error) {
        if (!error.isEmpty()) {
            if (prompt)
                status(fault(error));
            return;
        }
        const auto downloaded = value["download"].toObject();
        const auto installed = value["installed"].toObject();
        update_->setVisible(!downloaded.isEmpty() || !installed.isEmpty());
        if (!prompt)
            return;
        if (downloaded.isEmpty() && installed.isEmpty()) {
            status(B("В папке загрузок нет более новой версии gTR.", "У теці завантажень немає новішої версії gTR."));
            return;
        }
        auto launch = [this](const QString &path) {
            QJsonObject input{{"path", path}, {"confirmed", true}};
            if (!projectData_.isEmpty())
                input["project"] = project_;
            task(B("Запуск новой версии", "Запуск нової версії"), "update.launch", input,
                 [this](const QJsonObject &) { QTimer::singleShot(0, this, [this] { close(); }); });
        };
        if (!installed.isEmpty()) {
            if (QMessageBox::question(this, "gTR", B("Открыть установленное обновление %1?",
                                                    "Відкрити встановлене оновлення %1?").arg(installed["label"].toString()),
                                      QMessageBox::Yes | QMessageBox::No, QMessageBox::No) == QMessageBox::Yes)
                launch(installed["path"].toString());
        } else if (QMessageBox::question(this, "gTR",
            B("Установить %1 из загрузок? gTR сверит файл с выпуском GitHub.",
              "Установити %1 із завантажень? gTR звірить файл із випуском GitHub.").arg(downloaded["label"].toString()),
            QMessageBox::Yes | QMessageBox::No, QMessageBox::No) == QMessageBox::Yes) {
            task(B("Проверка и установка обновления", "Перевірка й установлення оновлення"), "update.install",
                 {{"path", downloaded["path"].toString()}, {"confirmed", true}},
                 [launch](const QJsonObject &result) { launch(result["path"].toString()); });
        }
    });
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
    service_.send(method, params, [this, done = std::move(done)](const QJsonObject &value, const QString &error) {
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
            status(fault(error));
        }
    });
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
