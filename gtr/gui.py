import json
import os
import sqlite3
import subprocess
import sys
from collections import deque
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from threading import Event
from time import monotonic
from zoneinfo import available_timezones

from PySide6.QtCore import (
    QLibraryInfo,
    QSettings,
    QSignalBlocker,
    Qt,
    QThread,
    QTimer,
    QTranslator,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QIcon,
    QPalette,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import __version__, updates
from .backup import backup_project
from .conversion import LOSS, RECORD_KINDS, load_bundle, plan_import, prepare
from .core import ISSUES, analyze, catalogue, properties, summary, verify
from .dashboard import Dashboard, cat_art, label, number, panel, size_text
from .diagnostics import Diagnostics
from .fidelity import fidelity, public_fidelity
from .files import local, publish_text
from .gallery import PhotoGallery
from .i18n import LANGUAGES, set_language, tr
from .journal import PHASE_NAMES, Journal
from .library import Library, Participants, fill, table
from .media_engine import inspect_project, public_inventory
from .media_preview import FFmpegThumbnail
from .media_queue import (
    MediaDecisionRequired,
    MediaQueue,
    cache_info,
    clean_cache,
    validate_decisions,
)
from .media_queue_ui import CompatibilityDialog, QueueDisplay, media_size
from .mock_api import SCENARIOS, rehearse
from .model import Cancelled, RecoveryError
from .owner import app_home
from .recovery_ui import RecoveryResults
from .registry_ui import HistoryPage, ProfilesPage
from .reports import LOCAL, SERVER, plan_summary, save_report
from .telegram_ui import TelegramControls
from .verification import FIELD_NAMES, VERDICTS, VERIFICATION_NAMES
from .video import VIDEO_POLICIES, MediaTools, tool_path
from .viewer import Viewer
from .wizard import ImportWizard


class Section(IntEnum):
    OVERVIEW = 0
    ARCHIVE = 1
    CHAT = 2
    PARTICIPANTS = 3
    MEDIA = 4
    IMPORT = 5
    VERIFICATION = 6
    PROGRESS = 7
    DIAGNOSTICS = 8
    SETTINGS = 9
    ABOUT = 10
    TELEGRAM = 11
    PROFILES = 12
    HISTORY = 13
    FORWARDS = 14


SECTIONS = (
    "Обзор",
    "Архив",
    "Переписка",
    "Участники",
    "Медиа",
    "Импорт",
    "Верификация",
    "Ход операции",
    "Диагностика",
    "Настройки",
    "О программе",
    "Telegram",
    "Профили импорта",
    "История импортов",
    "Пересланные сообщения",
)
STAGES = {
    "archive_analysis": "Проверка архива",
    "duplicates": "Поиск ранее импортированных сообщений",
    "archive": "Чтение и проверка файлов",
    "parse": "Чтение истории",
    "reconstruct": "Восстановление связей",
    "write_manifest": "Сохранение описания пакета",
    "verify": "Проверка SHA-256",
    "verify_text": "Сверка текста истории",
    "copy_text": "Копирование текста истории",
    "inspect_media": "Проверка вложений",
    "convert": "Подготовка новых сообщений",
    "media": "Копирование оригинала",
    "finish": "Сохранение проекта",
    "check_history": "Проверка заголовка",
    "check_peer": "Проверка диалога",
    "initialized": "Инициализация подтверждена",
    "validated": "Пакет проверен",
    "verification": "Сверка результата",
    "init_import": "Инициализация импорта",
    "upload": "Обработка вложений",
    "start_import": "Запуск обработки",
    "accepted": "Локальная имитация завершена",
    "local_check": "Локальная проверка пакета",
    "await_confirmation": "Ожидание подтверждения",
    "verifying": "Верификация результата",
    "limited": "Ограничение Telegram",
    "error": "Ошибка",
    "complete": "Локальный сценарий завершён",
    "report": "Сохранение отчёта",
    "local_history": "Сверка исходных сообщений",
    "backup": "Копирование файлов проекта",
    "verify_update": "Сверка архива с GitHub",
    "extract_update": "Установка версии gTR",
    "probe_media": "Чтение и декодирование медиа",
    "media_cache_copy": "Копирование подготовленного видео",
}
STYLE = """
QWidget { color:#e6edeb; font-size:10pt; }
QMainWindow, #main { background:#151a1d; }
QFrame#rail { background:#101517; border-right:1px solid #2c3739; }
QLabel#brand { font-size:28pt; font-weight:700; color:#f4cf91; }
QLabel#small { color:#8ba19e; font-size:9pt; }
QLabel#title { font-size:24pt; font-weight:650; }
QLabel#heroTitle { font-size:19pt; font-weight:650; }
QLabel#welcomeTitle { font-size:25pt; font-weight:700; }
QLabel#section { font-size:12pt; font-weight:600; }
QLabel#muted { color:#a6b8b6; }
QLabel#secondary { color:#c4d3d0; }
QLabel#metric { font-size:22pt; font-weight:650; }
QLabel#mode { color:#b5d6ee; background:#213647; padding:5px 10px; border-radius:8px; }
QFrame#panel, QFrame#hero { background:#1d2528; border:1px solid #303b3e; border-radius:11px; }
QFrame#hero { background:#1d2a36; border-color:#354a5b; }
QFrame#welcome { background:#1c2937; border:1px solid #30475e; border-radius:13px; }
QPushButton { background:#2a3538; border:1px solid #425052; padding:7px 12px; border-radius:6px; }
QPushButton:hover { background:#374547; border-color:#637b74; }
QPushButton:pressed { background:#293d35; }
QPushButton:disabled { color:#748380; background:#21292b; border-color:#30383a; }
QPushButton#primary { background:#f1c77f; color:#24190b; border-color:#f1c77f; font-weight:600; }
QPushButton#primary:hover { background:#ffdfa8; }
QPushButton#primary:disabled { color:#717e89; background:#242f39; border-color:#303c48; }
QListWidget, QTextBrowser, QPlainTextEdit, QTableWidget { background:#1b2225; border:1px solid #354042; border-radius:7px; }
QListWidget#nav { background:transparent; border:0; outline:0; }
QListWidget#nav::item { padding:10px 11px; margin:1px 0; border-radius:6px; color:#afbfbc; }
QListWidget#nav::item:selected { background:#2b3e50; color:#f4cf91; font-weight:600; }
QListWidget#nav::item:hover:!selected { background:#202a2b; }
QTabWidget::pane { border:0; }
QTabBar::tab { background:#202a34; padding:9px 15px; color:#afbfca; margin-right:4px; border-radius:5px; }
QTabBar::tab:selected { background:#304354; color:#f6ca81; }
QListWidget::item { padding:6px; }
QLineEdit, QComboBox, QDateEdit { background:#1a2224; padding:6px; border:1px solid #405052; border-radius:5px; }
QLineEdit:focus, QComboBox:focus { border-color:#9fcbb6; }
QComboBox QAbstractItemView { background:#222c2e; color:#e6edeb; selection-background-color:#36506a; }
QHeaderView::section { background:#222d2f; border:0; padding:7px; color:#b4c5c0; }
QTableWidget { gridline-color:#303b3d; }
QProgressBar { border:0; background:#2b3838; min-height:9px; max-height:9px; border-radius:4px; }
QProgressBar::chunk { background:#f1c77f; border-radius:4px; }
QToolTip { color:#e4eee8; background:#24352d; border:1px solid #52705e; padding:4px; }
"""


class Worker(QThread):
    update = Signal(str, int, int)
    result = Signal(object)
    error = Signal(str)
    decision = Signal(object)

    def __init__(self, job, parent):
        super().__init__(parent)
        self.job = job
        self.stop = Event()

    def run(self):
        last, then = "", 0.0

        def report(stage, done, total):
            nonlocal last, then
            if self.stop.is_set():
                raise Cancelled()
            now = monotonic()
            if stage != last or now - then > 0.12:
                # Qt int signals are signed 32 bit, unlike byte counts.
                while max(done, total) >= 2**31:
                    done, total = done // 2, total // 2
                self.update.emit(stage, done, total)
                last, then = stage, now

        try:
            self.result.emit(self.job(report))
        except MediaDecisionRequired as exc:
            self.decision.emit(exc)
        except RecoveryError as exc:
            self.error.emit(str(exc))
        except Exception:  # noqa: BLE001 - keep details about local files out of the UI log
            self.error.emit("Не удалось завершить операцию. Проверьте доступ к файлам и свободное место.")


def button(text, callback, primary=False):
    result = QPushButton(tr(text))
    if primary:
        result.setObjectName("primary")
    result.clicked.connect(callback)
    return result


def row(*widgets):
    layout = QHBoxLayout()
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch()
    return layout


def page():
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(14)
    return widget, layout


class Window(QMainWindow):
    media_queue_changed = Signal(object)

    def __init__(self, project=None, *, telegram_factory=TelegramControls):
        super().__init__()
        self.setWindowTitle(f"gTR {__version__}")
        self.resize(1170, 780)
        self.setMinimumSize(930, 650)
        self.settings = QSettings("gAssets", "gTR")
        set_language(self.settings.value("language", "ru"))
        self.diagnostics = Diagnostics(app_home())
        self.project = self.bundle = self.worker = None
        self.plan_data = self.comparison = None
        self.planned = False
        self.after_job = None
        self.after_media_decision = None
        self.pending_media_decision = None
        self.media_auto_controls = []
        self.close_pending = False
        self.stats = {}
        self.restart_folder = None
        self.downloaded_update = None
        main = QWidget(objectName="main")
        self.setCentralWidget(main)
        shell = QHBoxLayout(main)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        rail = QFrame(objectName="rail")
        rail.setFixedWidth(192)
        left = QVBoxLayout(rail)
        left.setContentsMargins(13, 20, 13, 16)
        dogear = QHBoxLayout()
        dogear.addWidget(cat_art(40))
        dogear.addWidget(label("gTR", "brand"))
        dogear.addStretch()
        left.addLayout(dogear)
        left.addWidget(label("Грузин", "small"))
        left.addSpacing(22)
        self.nav = QListWidget(objectName="nav")
        self.nav.addItems([tr(name) for name in SECTIONS])
        left.addWidget(self.nav, 1)
        left.addWidget(label(f"gAssets   ·   {__version__}", "small"))
        shell.addWidget(rail)
        right = QVBoxLayout()
        right.setContentsMargins(26, 20, 26, 22)
        right.setSpacing(14)
        header = QHBoxLayout()
        self.heading = label(tr("Обзор"), "title")
        header.addWidget(self.heading)
        header.addStretch()
        self.connection_badge = label(tr("Локальный режим"), "mode")
        header.addWidget(self.connection_badge)
        self.update_hint = button("Обновление…", self.offer_update)
        self.update_hint.hide()
        header.addWidget(self.update_hint)
        right.addLayout(header)
        self.stack = QStackedWidget()
        right.addWidget(self.stack, 1)
        shell.addLayout(right, 1)
        self.overview = Dashboard(
            self.choose_archive,
            self.choose_folder,
            self.open_recent,
            lambda: self.navigate_to(Section.CHAT),
            lambda: self.navigate_to(Section.MEDIA),
            self.show_export_guide,
        )
        self.viewer = Viewer(self.run_job, lambda: self.video_tools().ffmpeg)
        self.forwards = Viewer(
            self.run_job, lambda: self.video_tools().ffmpeg, forwards=True,
            resolve=self.resolve_forward_names, jump=self.focus_message,
        )
        self.forward_request = None
        self.telegram = telegram_factory(self, self.log, lambda: self.navigate_to(Section.TELEGRAM))
        self.telegram.changed.connect(
            lambda connected: self.connection_badge.setText(
                tr("Telegram подключён" if connected else "Локальный режим")
            )
        )
        self.telegram.changed.connect(self.forward_connection)
        self.telegram.idle_closed.connect(self._close_after_telegram)
        self.participants = Participants(self.focus_participant)
        self.media = Library(
            self.viewer.open_asset, self.focus_message, self.inspect_project_media, self.save_media_inventory,
            self.show_photo_gallery
        )
        self.stack.addWidget(self.overview)
        self.stack.addWidget(self.archive_page())
        self.stack.addWidget(self.viewer)
        self.stack.addWidget(self.participants)
        self.stack.addWidget(self.media)
        self.stack.addWidget(self.import_page())
        self.stack.addWidget(self.verification_page())
        self.stack.addWidget(self.progress_page())
        self.stack.addWidget(self.logs_page())
        self.stack.addWidget(self.settings_page())
        self.stack.addWidget(self.about_page())
        self.stack.addWidget(self.telegram.account_page)
        self.profiles_page = ProfilesPage(self)
        self.history_page = HistoryPage(self)
        self.stack.addWidget(self.profiles_page)
        self.stack.addWidget(self.history_page)
        self.stack.addWidget(self.forwards)
        self.overview.layout().insertWidget(0, button("Продолжить импорт", self.show_import_profiles))
        self.nav.currentRowChanged.connect(self.navigate)
        self.nav.setCurrentRow(0)
        self.refresh_projects()
        if project:
            self.open_project(project)
        self.log("Приложение открыто локально.")
        if updates.portable():
            self.update_timer = QTimer(self, interval=15000)
            self.update_timer.timeout.connect(self.check_downloads)
            self.update_timer.start()
            self.check_downloads()

    def navigate(self, index):
        if 0 <= index < len(SECTIONS):
            self.heading.setText(tr(SECTIONS[index]))
            self.stack.setCurrentIndex(index)
            if index == Section.DIAGNOSTICS:
                self.refresh_jobs()
            if index == Section.PROFILES and not self.worker:
                self.profiles_page.reload(getattr(self, "requested_profile", None))
            if index == Section.HISTORY:
                self.history_page.reload()
            if index == Section.SETTINGS:
                self.update_media_cache_summary()

    def show_import_profiles(self, preferred=None):
        self.requested_profile = preferred if isinstance(preferred, tuple) else None
        explicit_reload = self.nav.currentRow() == Section.PROFILES or self.worker is not None
        self.navigate_to(Section.PROFILES)
        if explicit_reload:
            self.profiles_page.reload(self.requested_profile)
        self.requested_profile = None

    def show_import_history(self):
        self.navigate_to(Section.HISTORY)

    def navigate_to(self, section):
        self.nav.setCurrentRow(section)

    def open_recent(self):
        last = self.settings.value("recent", "")
        if last and (Path(last) / "history.sqlite").is_file():
            self.open_project(Path(last))
        else:
            self.navigate_to(Section.ARCHIVE)

    def focus_message(self, chat, message):
        self.navigate_to(Section.CHAT)
        self.viewer.jump_to(chat, message)

    def focus_participant(self, chat, key):
        self.viewer.chat.setCurrentIndex(self.viewer.chat.findData(chat))
        index = self.viewer.side.findData(key)
        if index >= 0:
            self.viewer.side.setCurrentIndex(index)
        self.navigate_to(Section.CHAT)

    def archive_page(self):
        w, layout = page()
        box, group = panel()
        group.addWidget(label("Открыть экспорт", "section"))
        group.addWidget(
            label("RAR, ZIP или распакованная папка с HTML либо JSON из Telegram Desktop.", "muted")
        )
        group.addLayout(
            row(
                button("Выбрать архив", self.choose_archive, True),
                button("Выбрать папку", self.choose_folder),
                button("Как подготовить архив…", self.show_export_guide),
            )
        )
        self.archive_format = QComboBox()
        for title, value in (("Авто · JSON при наличии", "auto"), ("HTML", "html"), ("JSON", "json")):
            self.archive_format.addItem(title, value)
        group.addLayout(row(label("Формат истории", "muted"), self.archive_format))
        layout.addWidget(box)
        layout.addWidget(label("Локальные проекты", "section"))
        self.projects = QListWidget()
        self.projects.itemDoubleClicked.connect(
            lambda item: self.open_project(Path(item.data(Qt.ItemDataRole.UserRole)))
        )
        layout.addWidget(self.projects, 1)
        layout.addLayout(
            row(
                button("Открыть выбранный", self.open_selected),
                button("Проект из другой папки…", self.browse_project),
                button("Резервная копия…", self.copy_project),
            )
        )
        return w

    def verification_page(self):
        tabs = QTabWidget()
        self.verification_tabs = tabs
        tabs.addTab(self.integrity_page(), tr("Файлы и связи"))
        w, layout = page()
        intro, group = panel()
        group.addWidget(label("Проверка с двух аккаунтов", "section"))
        group.addWidget(
            label(
                "Загрузите пакет и снимки одного диалога. Отображаемое имя, серверный отправитель и сторона сравниваются отдельно.",
                "muted",
            )
        )
        group.addLayout(
            row(
                button("Сравнить два снимка…", self.compare_observations, True),
                button("Открыть пакет…", self.select_bundle),
                button("Сохранить результат…", self.export_verification),
            )
        )
        layout.addWidget(intro)
        self.verdict = label("Откройте пакет или запустите локальный сценарий.", "secondary")
        self.levels = label(
            "A · Локально — ожидается    B · Сервер — ожидается    C · Клиенты — ожидается", "muted"
        )
        layout.addWidget(self.verdict)
        layout.addWidget(self.levels)
        self.evidence_table = table(("Что проверено", "Аккаунт A", "Аккаунт B"))
        layout.addWidget(self.evidence_table, 1)
        tabs.addTab(w, tr("После импорта"))
        self.recovery = RecoveryResults(self.export_fidelity)
        tabs.addTab(self.recovery, tr("Результаты восстановления"))
        tabs.currentChanged.connect(self.read_fidelity)
        return tabs

    def integrity_page(self):
        w, layout = page()
        box, group = panel()
        self.integrity_state = label("Для проверки откройте проект.", "section")
        self.checksum = label("", "muted")
        self.checksum.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        group.addWidget(self.integrity_state)
        group.addWidget(self.checksum)
        group.addLayout(
            row(
                button("Сверить все файлы", self.verify_project),
                button("Сохранить отчёт…", self.export_project_report),
            )
        )
        layout.addWidget(box)
        self.issues = QTableWidget(0, 2)
        self.issues.setHorizontalHeaderLabels(["Наблюдение", "Количество"])
        self.issues.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.issues.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.issues.verticalHeader().hide()
        layout.addWidget(self.issues, 1)
        self.issue_caption = label("Записи", "section")
        layout.addWidget(self.issue_caption)
        self.issue_items = QListWidget()
        self.issues.currentCellChanged.connect(self.list_issues)
        self.issue_items.itemDoubleClicked.connect(self.view_issue)
        layout.addWidget(self.issue_items, 1)
        return w

    def list_issues(self, index, *_):
        self.issue_items.clear()
        selected = self.issues.item(index, 0)
        if not selected or not self.project:
            return
        code = selected.data(Qt.ItemDataRole.UserRole)
        with catalogue(self.project) as db:
            records = db.execute(
                """SELECT DISTINCT m.id,m.chat,m.source_id,m.date_raw,m.reply FROM issues i
                JOIN messages m ON m.id=i.msg WHERE i.code=? ORDER BY m.id LIMIT 301""",
                (code,),
            ).fetchall()
        self.issue_caption.setText("Первые 300 записей" if len(records) > 300 else "Записи")
        for mid, chat, sid, date, reply in records[:300]:
            self.issue_items.addItem(f"#{sid}   ·   {date}" + (f"   →   #{reply}" if reply else ""))
            self.issue_items.item(self.issue_items.count() - 1).setData(Qt.ItemDataRole.UserRole, (chat, mid))

    def view_issue(self, item):
        chat, mid = item.data(Qt.ItemDataRole.UserRole)
        self.focus_message(chat, mid)

    def import_page(self):
        self.import_tabs = QTabWidget()
        w, layout = page()
        box, group = panel()
        group.addWidget(label(tr("Диалог и профиль"), "section"))
        group.addWidget(button(tr("Продолжить импорт"), self.show_import_profiles))
        group.addWidget(
            label(
                tr("Выберите один диалог. Пакет содержит текст, оригиналы вложений и отчёт о потерях."), "muted"
            )
        )
        self.import_chat = QComboBox()
        self.import_chat.setPlaceholderText(tr("Выберите диалог"))
        self.import_profile = QComboBox()
        self.import_profile.addItem(tr("Профиль iOS"), "ios")
        self.import_profile.addItem(tr("Профиль Android"), "android")
        self.import_sample = QComboBox()
        self.import_sample.addItem(tr("Вся переписка"), None)
        for limit in (2, 10, 20, 100, 1000, 10000):
            self.import_sample.addItem(tr('Первые {0}').format(number(limit)), limit)
        for choice in (self.import_chat, self.import_profile, self.import_sample):
            choice.setMinimumHeight(25)
        group.addLayout(row(self.import_chat, self.import_profile, self.import_sample))
        self.import_time = QComboBox()
        self.import_time.addItem(tr("Исходные даты и время"), "historical")
        group.addWidget(label(tr("Исходные даты и время"), "muted"))
        self.import_mode = QComboBox()
        self.import_mode.setAccessibleName(tr("Состав пакета истории"))
        self.import_mode.addItem(tr("Текст и медиа"), "text_media")
        self.import_mode.addItem(tr("Только текст (для первого теста)"), "text_only")
        self.import_mode.addItem(tr("Текст и медиа как документы"), "document")
        group.addLayout(row(self.import_mode))
        self.import_video = QComboBox()
        self.import_video.setAccessibleName(tr("Как подготовить видео"))
        for key, name in VIDEO_POLICIES.items():
            self.import_video.addItem(tr(name), key)
        group.addLayout(row(label(tr("Видео")), self.import_video))
        group.addWidget(self.auto_video_checkbox())
        self.import_kind = QComboBox()
        for key, name in RECORD_KINDS.items():
            self.import_kind.addItem(tr(name), key)
        group.addLayout(row(label(tr("Тип исходной записи")), self.import_kind))
        self.import_notes = self.notes_checkbox()
        group.addWidget(self.import_notes)
        group.addLayout(
            row(
                button(tr("Подготовить пакет…"), self.make_bundle, True),
                button(tr("Оценить перенос"), self.preview_plan),
                button(tr("Открыть готовый…"), self.select_bundle),
                button(tr("Предпросмотр"), self.preview_import_chat),
            )
        )
        self.bundle_summary = label(tr("Пакет ещё не выбран."), "secondary")
        group.addWidget(self.bundle_summary)
        layout.addWidget(box)
        self.plan_table = table((tr("Ограничение или преобразование"), tr("Записи")))
        layout.addWidget(self.plan_table, 1)
        self.plan_totals = label("", "muted")
        layout.addWidget(self.plan_totals)
        layout.addLayout(row(button(tr("Сохранить отчёт…"), self.export_import_report)))
        self.import_tabs.addTab(w, tr("Пакет истории"))
        self.import_tabs.addTab(self.telegram.server_page, tr("Серверный импорт"))
        operations, layout = page()
        box, group = panel()
        group.addWidget(label(tr("Локальная проверка"), "section"))
        self.scenario = QComboBox()
        for code, (title, _, _) in SCENARIOS.items():
            self.scenario.addItem(title, code)
        group.addLayout(row(self.scenario, button(tr("Запустить Mock API"), self.simulate)))
        group.addLayout(
            row(
                button(tr("Два тестовых сообщения…"), lambda: self.make_fixture("text")),
                button(tr("Тест медиа…"), lambda: self.make_fixture("media")),
            )
        )
        layout.addWidget(box)
        box, group = panel()
        group.addWidget(label(tr("Консоль владельца"), "section"))
        group.addLayout(row(button(tr("Открыть окно владельца…"), self.owner_console)))
        layout.addWidget(box)
        layout.addStretch()
        self.import_tabs.addTab(operations, tr("Локальная имитация"))
        self.wizard = ImportWizard(self)
        self.import_tabs.addTab(self.wizard, tr("Мастер импорта"))
        self.import_tabs.setCurrentWidget(self.wizard)
        return self.import_tabs

    def navigate_to_import_wizard(self):
        self.navigate_to(Section.IMPORT)
        self.import_tabs.setCurrentWidget(self.wizard)

    def run_recovery_job(self, title, job, ready):
        previous_tab = self.import_tabs.currentWidget()

        def display(value):
            self.import_tabs.setCurrentWidget(previous_tab)
            self.navigate_to(Section.IMPORT)
            return ready(value)

        self.run_job(title, job, display)

    def run_recovery_media_job(self, title, job, ready, *, project, decisions=None):
        previous_tab = self.import_tabs.currentWidget()

        def display(value):
            self.import_tabs.setCurrentWidget(previous_tab)
            self.navigate_to(Section.IMPORT)
            return ready(value)

        self.run_media_job(title, job, display, project=project, decisions=decisions)

    def use_recovery_bundle(self, bundle):
        in_wizard = self.import_tabs.currentWidget() is self.wizard
        self.use_bundle(bundle)
        message = tr("Сохранён отдельный пакет новых записей. Проверьте его и подтвердите согласие заново.")
        if in_wizard:
            self.wizard.package_ready(bundle)
        else:
            self.import_tabs.setCurrentWidget(self.telegram.server_page)
            self.telegram.state.setText(message)
        return message

    def open_recovery_verification(self, bundle):
        self.use_bundle(bundle)
        self.import_tabs.setCurrentWidget(self.telegram.server_page)
        message = tr("Открыт пакет предыдущей операции. Сохраните независимые снимки с двух аккаунтов.")
        self.telegram.state.setText(message)
        self.telegram.details.setText(tr("Сравните сохранённые снимки во вкладке «Верификация»."))
        return message

    def finish_import_wizard(self):
        self.navigate_to(Section.OVERVIEW)

    def progress_page(self):
        w, layout = page()
        box, group = panel()
        self.job_title = label(tr("Активных операций нет"), "heroTitle")
        self.stage = label(tr("Здесь появятся этап и результат операции."), "muted")
        self.media_queue_display = QueueDisplay()
        self.media_queue_changed.connect(self.media_queue_display.update_status)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setValue(0)
        self.stop_btn = button(tr("Остановить"), self.stop_job)
        self.stop_btn.setEnabled(False)
        group.addWidget(self.job_title)
        group.addWidget(self.stage)
        group.addWidget(self.media_queue_display)
        group.addSpacing(8)
        group.addWidget(self.bar)
        group.addLayout(row(self.stop_btn))
        layout.addWidget(box)
        self.outcome = label("", "secondary")
        self.outcome.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.outcome)
        layout.addStretch()
        return w

    def logs_page(self):
        tabs = QTabWidget()
        w, layout = page()
        self.log_lines = deque(self.diagnostics.recent().splitlines(), maxlen=500)
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("Поиск по действиям")
        layout.addWidget(self.log_search)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(500)
        self.log_search.textChanged.connect(self.filter_log)
        self.filter_log()
        layout.addWidget(self.logs)
        tabs.addTab(w, "Действия")
        w, layout = page()
        self.job_rows = []
        self.recent_jobs = []
        self.job_search = QLineEdit()
        self.job_search.setPlaceholderText("Поиск по номеру операции, состоянию и коду ошибки")
        self.job_search.textChanged.connect(self.filter_jobs)
        layout.addWidget(self.job_search)
        layout.addLayout(
            row(
                button("Обновить журнал операций", self.refresh_jobs),
                button("Восстановить прерванную подготовку", self.recover_job),
                button("Сохранить диагностику…", self.export_job),
            )
        )
        self.jobs = table(("Операция", "Состояние", "Записей в пакете", "Медиа принято", "Код ошибки"))
        self.jobs.itemSelectionChanged.connect(self.show_job)
        layout.addWidget(self.jobs, 1)
        self.job_log = QPlainTextEdit()
        self.job_log.setReadOnly(True)
        layout.addWidget(self.job_log, 1)
        tabs.addTab(w, "Серверные операции")
        return tabs

    def refresh_jobs(self):
        path = app_home() / "imports.sqlite"
        try:
            self.recent_jobs = []
            if path.is_file():
                journal = Journal(path, "")
                journal.classify_interrupted()
                self.recent_jobs = journal.operations()
            self.filter_jobs()
        except (OSError, RecoveryError, sqlite3.Error):
            self.job_log.setPlainText("Не удалось прочитать журнал.")

    def filter_log(self):
        query = self.log_search.text().strip().casefold()
        self.logs.setPlainText("\n".join(line for line in self.log_lines if query in line.casefold()))
        self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())

    def filter_jobs(self):
        query = self.job_search.text().strip().casefold()
        self.job_rows = [
            job
            for job in self.recent_jobs
            if query
            in " ".join(
                (job["id"], job["phase"], PHASE_NAMES.get(job["phase"], ""), job["error_code"] or "")
            ).casefold()
        ]
        self.jobs.clearContents()
        fill(
            self.jobs,
            self.job_rows,
            lambda job: (
                job["id"],
                PHASE_NAMES.get(job["phase"], "Нет данных"),
                job["messages"],
                f"{job['media_done']} / {job['media']}",
                job["error_code"] or "—",
            ),
        )
        self.show_job()

    def show_job(self):
        index = self.jobs.currentRow()
        if not 0 <= index < len(self.job_rows):
            self.job_log.clear()
            return
        job = self.job_rows[index]
        try:
            records = Journal(app_home() / "imports.sqlite", "").events(job["id"])
        except (OSError, RecoveryError, sqlite3.Error):
            self.job_log.setPlainText("Не удалось прочитать запись операции.")
            return
        accuracy = VERIFICATION_NAMES.get(job["accuracy"].get("verification"), "Не хватает данных для вывода")
        lines = [f"Результат сверки · {accuracy}"]
        lines.extend(
            f"{r['ts']} · {PHASE_NAMES.get(r['phase'], 'Нет данных')} · "
            f"{STAGES.get(r['stage'], 'Операция')}" + (f" · {r['code']}" if r["code"] else "")
            for r in records
        )
        self.job_log.setPlainText("\n".join(lines))

    def recover_job(self):
        index = self.jobs.currentRow()
        if not 0 <= index < len(self.job_rows):
            self.info("Выберите прерванную операцию в журнале.")
            return
        job = self.job_rows[index]["id"]
        if (
            QMessageBox.question(
                self,
                "Прерванная подготовка",
                "Закрыть локальную подготовку без возобновления загрузки? Перед следующей попыткой Telegram заново проверит чат и ограничения.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            Journal(app_home() / "imports.sqlite", "").recover_before_start(job, acknowledged=True)
        except RecoveryError as error:
            self.info(str(error))
        except (OSError, sqlite3.Error):
            self.info("Не удалось открыть журнал операции.")
        else:
            self.refresh_jobs()
            self.info("Прерванная подготовка закрыта. Перед новой попыткой выполните проверки диалога.")

    def export_job(self):
        index = self.jobs.currentRow()
        if 0 <= index < len(self.job_rows):
            self.save_operation_diagnostic(self.job_rows[index]["id"])
        else:
            self.info("Выберите операцию в журнале.")

    def save_operation_diagnostic(self, job):
        options = ("Обезличенный отчёт для передачи", "Локальный технический отчёт с ID сообщений и SHA-256")
        choice, ok = QInputDialog.getItem(self, "Диагностика операции", "Состав отчёта", options, 0, False)
        if not ok:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить диагностический отчёт", "gTR-diagnostic.json", "Отчёты JSON (*.json)"
        )
        if not path:
            return
        try:
            dest = local(Path(path))
            if not dest.suffix:
                dest = dest.with_suffix(".json")
            protected = (*self.telegram._protected(), self.bundle)
            if any(dest.is_relative_to(local(Path(root))) for root in protected if root):
                raise RecoveryError("Сохраните отчёт отдельно от исходных данных, проекта и пакета.")
            data = Journal(app_home() / "imports.sqlite", "").diagnostic_report(
                job, private=choice == options[1]
            )
            publish_text(dest, json.dumps(data, ensure_ascii=False, indent=2))
        except RecoveryError as exc:
            self.info(str(exc))
        except (OSError, sqlite3.Error):
            self.info("Не удалось сохранить отчёт. Выберите свободное имя файла и проверьте папку.")

    def settings_page(self):
        w, layout = page()
        language = QComboBox()
        for code, name in LANGUAGES.items():
            language.addItem(name, code)
        language.setCurrentIndex(max(0, language.findData(self.settings.value("language", "ru"))))
        language.currentIndexChanged.connect(lambda: self.save_language(language.currentData()))
        layout.addLayout(row(label(tr("Язык / Мова")), language))
        if updates.portable():
            box, group = panel()
            group.addWidget(label(tr("Обновление программы"), "section"))
            group.addLayout(row(button(tr("Выбрать скачанное обновление…"), self.choose_update)))
            layout.addWidget(box)
        box, group = panel()
        group.addWidget(label(tr("Часовой пояс"), "section"))
        group.addWidget(
            label(
                tr("Для дат без смещения и для времени в файле импорта. Исходные даты хранятся в проекте."),
                "muted",
            )
        )
        self.timezone = QComboBox()
        self.timezone.setEditable(True)
        self.timezone.addItems(sorted(available_timezones()))
        self.timezone.setCurrentText(self.settings.value("timezone", "UTC"))
        group.addWidget(self.timezone)
        self.display_zone = QComboBox()
        self.display_zone.addItems([tr("Показывать время источника"), tr("Показывать в выбранном поясе")])
        self.display_zone.setCurrentIndex(self.settings.value("display_zone", 0, type=int))
        self.forwards.zone = self.viewer.zone = (
            self.timezone.currentText() if self.display_zone.currentIndex() else ""
        )
        group.addWidget(self.display_zone)
        group.addLayout(row(button(tr("Сохранить настройки"), self.save_settings)))
        layout.addWidget(box)
        box, group = panel()
        group.addWidget(label(tr("Хранение на компьютере"), "section"))
        group.addWidget(label(str(app_home()), "muted"))
        group.addWidget(
            label(
                tr("Проекты, пакеты, отчёты и временные копии содержат личные данные в открытом виде. DPAPI защищает сохранённую сессию Telegram."),
                "muted",
            )
        )
        group.addLayout(row(button(tr("Открыть папку локальных данных"), self.open_storage)))
        layout.addWidget(box)
        box, group = panel()
        group.addWidget(label(tr("FFmpeg и FFprobe"), "section"))
        self.ffmpeg_status = label("", "muted")
        group.addWidget(self.ffmpeg_status)
        self.update_ffmpeg_status()
        group.addLayout(
            row(
                button(tr("Выбрать FFmpeg…"), self.choose_ffmpeg),
                button(tr("Выбрать FFprobe…"), self.choose_ffprobe),
                button(tr("Автоопределение"), self.disable_ffmpeg),
            )
        )
        group.addWidget(self.auto_video_checkbox())
        self.media_cache_summary = label("", "muted")
        group.addWidget(self.media_cache_summary)
        self.update_media_cache_summary()
        group.addLayout(row(button(tr("Очистить кеш подготовленных видео…"), self.clear_media_cache)))
        layout.addWidget(box)
        layout.addStretch()
        return w

    def check_downloads(self):
        self.downloaded_update = updates.find_download()
        self.update_hint.setVisible(self.downloaded_update is not None)
        if self.downloaded_update:
            self.update_hint.setText(tr("Найдена {version}…").format(version=self.downloaded_update.label))

    def choose_update(self):
        path, _ = QFileDialog.getOpenFileName(self, "Архив обновления gTR", str(updates.downloads()), "Архив ZIP (*.zip)")
        if path:
            try:
                self.offer_update(updates.candidate(path))
            except (OSError, RecoveryError) as error:
                self.info(str(error))

    def offer_update(self, update=None):
        update = update if isinstance(update, updates.Update) else self.downloaded_update
        if not update or self.worker or self.telegram.busy:
            return
        if QMessageBox.question(
            self, "Обновление gTR",
            tr("Установить {version}? gTR сверит архив с выпуском на GitHub и перезапустится. "
               "Проекты и настройки останутся доступны.").format(version=update.label),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        def ready(folder):
            self.restart_folder = folder
            QTimer.singleShot(0, self.close)
            return "Обновление установлено."

        self.run_job("Установка обновления", lambda progress: updates.install(update, progress), ready)

    def update_ffmpeg_status(self):
        tools = self.video_tools()
        self.ffmpeg_status.setText(
            f"FFmpeg — {tr('доступен' if tools.ffmpeg else 'не найден')} · "
            f"FFprobe — {tr('доступен' if tools.ffprobe else 'не найден')}"
        )

    def video_tools(self):
        return MediaTools(self.settings.value("preview_ffmpeg", ""), self.settings.value("ffprobe", ""))

    def save_language(self, language):
        self.settings.setValue("language", language)
        self.info(tr("Выбор языка вступит в силу при следующем запуске."))

    def notes_checkbox(self):
        return QCheckBox(tr("Добавлять исторические пометки о пересылках и цитатах"))

    def auto_video_checkbox(self):
        control = QCheckBox(tr("Автоматически подготавливать совместимые видео"))
        control.setToolTip(tr("Разрешает создавать совместимые копии только для несовместимых видео."))
        control.setChecked(self.settings.value("auto_media_compatibility", False, type=bool))
        self.media_auto_controls.append(control)
        control.toggled.connect(self.change_auto_video)
        return control

    def change_auto_video(self, enabled):
        self.settings.setValue("auto_media_compatibility", enabled)
        for control in self.media_auto_controls:
            with QSignalBlocker(control):
                control.setChecked(enabled)
        if hasattr(self, "wizard"):
            self.wizard.invalidate_review()

    def effective_video_policy(self, policy):
        if (policy is None or policy == "original") and self.settings.value("auto_media_compatibility", False, type=bool):
            return "compatible"
        return policy or "original"

    def update_media_cache_summary(self):
        try:
            info = cache_info()
        except (OSError, RecoveryError):
            self.media_cache_summary.setText(tr("Кеш подготовленных видео недоступен."))
            return
        self.media_cache_summary.setText(tr("Кеш подготовленных видео · {files} файлов · {size}").format(
            files=number(info["files"]), size=media_size(info["size"]),
        ))

    def clear_media_cache(self):
        if self.worker or self.telegram.busy:
            self.info(tr("Дождитесь окончания текущей операции."))
            return
        answer = QMessageBox.question(
            self, tr("Кеш подготовленных видео"),
            tr("Удалить неиспользуемые копии из кеша?"),
            defaultButton=QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def ready(info):
            self.update_media_cache_summary()
            self.navigate_to(Section.SETTINGS)
            return tr("Освобождено {size}").format(size=media_size(info["size"]))

        self.run_job(tr("Очистка кеша подготовленных видео"), lambda _: clean_cache(), ready)

    def video_options(self, policy=None, *, annotate=None):
        return {
            "media_tools": self.video_tools(),
            "video_policy": self.effective_video_policy(policy or self.import_video.currentData()),
            "annotate": self.import_notes.isChecked() if annotate is None else annotate,
        }

    def choose_ffprobe(self):
        file, _ = QFileDialog.getOpenFileName(self, tr("Выбрать FFprobe"), "", tr("Программа (ffprobe.exe)"))
        if file and (path := tool_path("ffprobe", file)):
            self.settings.setValue("ffprobe", str(path))
            self.update_ffmpeg_status()
        elif file:
            self.info(tr("Выберите установленный ffprobe.exe."))

    def choose_ffmpeg(self):
        file, _ = QFileDialog.getOpenFileName(self, tr("Выбрать программу FFmpeg"), "", tr("Программа (ffmpeg.exe)"))
        if file:
            try:
                provider = FFmpegThumbnail(Path(file))
            except RecoveryError as error:
                self.info(str(error))
                return
            self.settings.setValue("preview_ffmpeg", str(provider.executable))
            self.update_ffmpeg_status()

    def disable_ffmpeg(self):
        self.settings.remove("preview_ffmpeg")
        self.settings.remove("ffprobe")
        self.update_ffmpeg_status()

    def open_storage(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(app_home())))

    def save_settings(self):
        if self.timezone.currentText() not in available_timezones():
            self.info("Выберите часовой пояс из списка IANA.")
            return
        self.settings.setValue("timezone", self.timezone.currentText())
        self.settings.setValue("display_zone", self.display_zone.currentIndex())
        self.forwards.zone = self.viewer.zone = (
            self.timezone.currentText() if self.display_zone.currentIndex() else ""
        )
        self.viewer.set_date_range()
        self.viewer.render()
        self.forwards.set_date_range()
        self.forwards.render()
        self.info("Настройки сохранены.")

    def log(self, text):
        line = self.diagnostics.write(text)
        self.log_lines.append(line)
        if not self.log_search.text().strip():
            self.logs.appendPlainText(line)
        else:
            self.filter_log()

    def about_page(self):
        w, layout = page()
        frame, body = panel()
        body.addWidget(cat_art(220), alignment=Qt.AlignmentFlag.AlignHCenter)
        body.addWidget(label(f"gTR {__version__}", "heroTitle"), alignment=Qt.AlignmentFlag.AlignHCenter)
        body.addWidget(
            label("Грузин · gAssets", "muted"), alignment=Qt.AlignmentFlag.AlignHCenter
        )
        body.addWidget(
            label(
                "gTR использует Telegram API и позволяет просматривать архивы Telegram Desktop и восстанавливать историю.",
                "secondary",
            ),
            alignment=Qt.AlignmentFlag.AlignHCenter,
        )
        layout.addWidget(frame)
        layout.addStretch()
        return w

    def info(self, text):
        QMessageBox.information(self, "gTR", text)

    def refresh_projects(self):
        self.projects.clear()
        base = app_home() / "projects"
        if base.is_dir():
            for directory in sorted(base.iterdir(), key=lambda p: p.name.casefold()):
                if (directory / "history.sqlite").is_file() and not directory.name.startswith(".gtr-"):
                    self.projects.addItem(directory.name)
                    self.projects.item(self.projects.count() - 1).setData(
                        Qt.ItemDataRole.UserRole, str(directory)
                    )
        if self.projects.count():
            self.projects.setCurrentRow(0)

    def open_selected(self):
        item = self.projects.currentItem()
        if item:
            self.open_project(Path(item.data(Qt.ItemDataRole.UserRole)))

    def browse_project(self, *, on_ready=None):
        path = QFileDialog.getExistingDirectory(self, "Папка проекта gTR")
        if path and self.open_project(Path(path)) and on_ready:
            on_ready()

    def open_project(self, project):
        if self.telegram.busy or (self.worker and self.worker.isRunning()):
            self.info("Дождитесь окончания текущей операции.")
            return
        try:
            self.bind_project(Path(project))
            self.navigate_to(Section.OVERVIEW)
            return True
        except (OSError, RecoveryError):
            self.info("Не удалось открыть проект. Проверьте папку с базой history.sqlite.")

    def bind_project(self, project):
        stats = summary(project)
        self.viewer.bind(project)
        self.forwards.bind(project)
        self.forward_request = None
        self.recovery.bind(None)
        self.participants.bind(project)
        self.media.bind(project)
        self.overview.bind(project, stats)
        self.project, self.stats, self.bundle = project, stats, None
        self.telegram.set_package()
        self.bundle_summary.setText("Пакет ещё не выбран.")
        self.plan_data = None
        self.comparison = None
        self.plan_table.setRowCount(0)
        self.plan_totals.clear()
        self.verdict.setText("Откройте пакет или запустите локальный сценарий.")
        self.levels.setText("A · Локально — ожидается    B · Сервер — ожидается    C · Клиенты — ожидается")
        self.overview.set_quality(None)
        self.evidence_table.setRowCount(0)
        self.import_chat.clear()
        with catalogue(project) as db:
            for cid, title in db.execute("SELECT id,title FROM chats ORDER BY id"):
                self.import_chat.addItem(title, cid)
        self.integrity_state.setText(
            f"Ответы со связью   {number(stats['resolved_replies'])} / {number(stats['replies'])}"
        )
        self.checksum.setText("SHA-256   " + stats["sha256"])
        issues = list(stats["issues"].items())
        self.issues.clearContents()
        self.issues.setRowCount(len(issues))
        for i, (code, count) in enumerate(issues):
            item = QTableWidgetItem(ISSUES.get(code, code))
            item.setData(Qt.ItemDataRole.UserRole, code)
            self.issues.setItem(i, 0, item)
            self.issues.setItem(i, 1, QTableWidgetItem(str(count)))
        if issues:
            self.issues.setCurrentCell(0, 0)
            self.list_issues(0)
        else:
            self.issue_items.clear()
        self.settings.setValue("recent", str(project))
        self.wizard.project_changed()
        self.refresh_projects()
        self.log("Локальный проект открыт.")

    def forward_connection(self, connected):
        if not connected:
            self.forward_request = None
            self.forwards.clear_names()

    def resolve_forward_names(self, identities):
        if not self.telegram.account:
            self.info(tr("Подключите Telegram, чтобы прочитать имена доступных профилей."))
            return
        if not identities or self.telegram.busy or self.worker:
            return
        self.forward_request = self.telegram.account.id, self.project, identities
        self.telegram.dispatch("forward_names", lambda session, _: session.manager.forward_names(identities))

    def show_forward_names(self, names):
        request, self.forward_request = self.forward_request, None
        if request and (self.telegram.account.id, self.project) == request[:2]:
            self.forwards.apply_names(request[2], names)

    def read_fidelity(self, *_):
        if (self.worker or not self.bundle or self.verification_tabs.currentWidget() is not self.recovery
                or self.recovery.report is not None):
            return
        data = self.telegram.package.data
        comparison = self.comparison

        def job(progress):
            return fidelity(data, comparison, progress=progress)

        self.run_job("Сверка по сообщениям", job, self._fidelity_ready)

    def _fidelity_ready(self, report):
        self.recovery.bind(report)
        self.navigate_to(Section.VERIFICATION)
        return "Отчёт по сообщениям готов."

    def export_fidelity(self):
        report = self.recovery.report
        if not report:
            return
        choices = (tr("Обезличенный отчёт"), tr("Локальный отчёт с именами файлов и датами"))
        choice, ok = QInputDialog.getItem(self, tr("Результаты восстановления"), "", choices, 0, False)
        if not ok:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить отчёт…"), "gTR-fidelity.json", "JSON (*.json)"
        )
        if path:
            try:
                value = report if choice == choices[1] else public_fidelity(report)
                dest = local(Path(path))
                if any(dest.is_relative_to(local(Path(root))) for root in self.telegram._protected()):
                    raise RecoveryError("Сохраните отчёт отдельно от архива, проекта и пакета.")
                publish_text(dest, json.dumps(value, ensure_ascii=False, indent=2))
            except (OSError, RecoveryError):
                self.info("Не удалось сохранить отчёт. Выберите новый файл вне исходных данных.")

    def show_export_guide(self):
        guide = Path(__file__).parent / "assets" / "export-guide.md"
        try:
            text = guide.read_text(encoding="utf-8")
        except OSError:
            self.info("Инструкция отсутствует в комплекте gTR.")
            return
        dialog = QDialog(self, windowTitle="Как подготовить архив")
        dialog.resize(700, 570)
        layout = QVBoxLayout(dialog)
        content = QTextBrowser()
        content.setOpenExternalLinks(True)
        content.setMarkdown(text)
        layout.addWidget(content)
        layout.addWidget(button("Закрыть", dialog.accept), alignment=Qt.AlignmentFlag.AlignRight)
        dialog.exec()
        dialog.deleteLater()

    def choose_archive(self, *, on_ready=None):
        p, _ = QFileDialog.getOpenFileName(
            self, "Экспорт Telegram Desktop", "", "Архивы и история (*.rar *.zip *.html *.json)"
        )
        if p:
            self.ingest(Path(p), on_ready=on_ready)

    def choose_folder(self, *, on_ready=None):
        p = QFileDialog.getExistingDirectory(self, "Распакованный экспорт Telegram Desktop")
        if p:
            self.ingest(Path(p), on_ready=on_ready)

    def ingest(self, source, *, on_ready=None):
        dest = app_home() / "projects" / (source.stem + "-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        zone = self.settings.value("timezone", "UTC")
        fmt = self.archive_format.currentData()

        def ready(project):
            self.bind_project(project)
            self.navigate_to(Section.OVERVIEW)
            if on_ready:
                on_ready()
            return f"Проект сохранён. {self.stats['export_format'].upper()} · {number(self.stats['messages'])} сообщений."

        self.run_job("Открытие архива", lambda p: analyze(source, dest, zone, p, export_format=fmt), ready)

    def verify_project(self):
        if not self.project:
            self.info("Сначала откройте проект.")
            return
        project = self.project

        def ready(result):
            message = (
                f"Проверено по SHA-256 — {number(result.get('ok', 0))}. "
                f"Изменено — {result.get('changed', 0)}. Отсутствует — {result.get('missing', 0)}. Недоступно — {result.get('unreadable', 0)}."
            )
            self.integrity_state.setText(message)
            return message

        self.run_job("Проверка локальной копии", lambda p: verify(project, p), ready)

    def copy_project(self):
        if not self.project:
            self.info("Сначала откройте проект.")
            return
        if folder := QFileDialog.getExistingDirectory(self, "Куда сохранить резервную копию"):
            project = self.project
            dest = Path(folder) / f"{project.name}-backup-{datetime.now():%Y%m%d-%H%M%S}"
            self.run_job(
                "Резервная копия",
                lambda p: backup_project(project, dest, p),
                lambda path: f"Копия проверена и готова к открытию. {path}",
            )

    def show_photo_gallery(self, chat):
        if self.project and not self.worker:
            gallery = PhotoGallery(self, self.project, chat, self.viewer.open_asset)
            gallery.show()
            gallery.load()

    def inspect_project_media(self, chat):
        project = self.project
        if project is None:
            self.info(tr("Сначала откройте проект."))
            return

        def ready(report):
            self.navigate_to(Section.MEDIA)
            return self.media.inspected(report)

        self.run_job(
            tr("Проверка вложений проекта"), lambda progress: inspect_project(project, chat, progress), ready
        )

    def save_media_inventory(self, report, protected=()):
        if not report:
            return
        choices = (tr("Обезличенный отчёт"), tr("Локальный отчёт с путями внутри архива и SHA-256"))
        choice, ok = QInputDialog.getItem(self, tr("Диагностика медиа"), tr("Состав отчёта"), choices, 0, False)
        if not ok:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Сохранить проверку медиа"), "gTR-media.json", tr("Отчёты JSON (*.json)")
        )
        if not path:
            return
        try:
            dest = local(Path(path))
            dest = dest if dest.suffix else dest.with_suffix(".json")
            roots = [*protected, *self.telegram._protected()]
            if self.project:
                with catalogue(self.project) as db:
                    roots.extend((self.project, Path(properties(db)["original"])))
            if any(dest.is_relative_to(local(Path(root))) for root in roots):
                raise RecoveryError(tr("Сохраните отчёт отдельно от архива, проекта и пакета."))
            data = report if choice == choices[1] else public_inventory(report)
            publish_text(dest, json.dumps(data, ensure_ascii=False, indent=2))
        except (OSError, sqlite3.Error, RecoveryError):
            self.info(tr("Не удалось сохранить отчёт. Выберите новый файл вне исходных данных."))

    def make_bundle(self):
        if not self.project:
            self.info(tr("Сначала откройте проект."))
            return
        folder = QFileDialog.getExistingDirectory(self, tr("Куда поместить новую папку с пакетом"))
        if folder:
            project, cid = self.project, self.import_chat.currentData()
            dest = Path(folder) / ("gTR-import-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            profile, sample = self.import_profile.currentData(), self.import_sample.currentData()
            policy, media_mode = self.import_time.currentData(), self.import_mode.currentData()
            video = self.video_options()
            kind = self.import_kind.currentData()
            self.run_media_job(
                tr("Подготовка пакета"),
                lambda p, **media: prepare(
                    project,
                    cid,
                    dest,
                    "source",
                    p,
                    profile=profile,
                    sample=sample,
                    date_policy=policy,
                    media_mode=media_mode,
                    record_kind=kind,
                    **video,
                    **media,
                ),
                self.use_bundle,
                project=project,
            )

    def preview_plan(self):
        if not self.project:
            self.info(tr("Сначала откройте проект."))
            return
        project, cid = self.project, self.import_chat.currentData()
        profile, sample = self.import_profile.currentData(), self.import_sample.currentData()
        policy, media_mode = self.import_time.currentData(), self.import_mode.currentData()
        video = self.video_options()
        kind = self.import_kind.currentData()

        def ready(data):
            return self.show_plan(data, True)

        self.run_job(
            tr("Оценка состава пакета"),
            lambda p: plan_import(
                project,
                cid,
                "source",
                p,
                profile=profile,
                sample=sample,
                date_policy=policy,
                media_mode=media_mode,
                record_kind=kind,
                **video,
            ),
            ready,
        )

    def preview_import_chat(self):
        if cid := self.import_chat.currentData():
            self.viewer.chat.setCurrentIndex(self.viewer.chat.findData(cid))
            self.navigate_to(Section.CHAT)

    def make_fixture(self, scenario):
        from .fixtures import prepare_fixture

        if folder := QFileDialog.getExistingDirectory(self, tr("Куда сохранить отдельный тестовый пакет")):
            dest = Path(folder) / ("gTR-test-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            self.run_job(
                tr("Подготовка синтетического пакета"),
                lambda p: prepare_fixture(dest, scenario, p),
                self.use_bundle,
            )

    def show_plan(self, data, planned):
        self.plan_data, self.planned = data, planned
        stats = plan_summary(data)
        title = tr("Оценка") if planned else tr("Пакет проверен")
        message = tr('{0} · {1} сообщений · {2} вложений · {3}').format(title, number(stats['included']), number(stats['attachments']), size_text(stats['media_bytes']))
        if stats.get("media_mode") == "text_only":
            message += tr(" · Только текст")
        if stats["skipped"]:
            message += tr(' · {0} пропущено').format(number(stats['skipped']))
        if stats.get("omitted_assets"):
            message += tr(' · {0} вложений исключено режимом').format(number(stats['omitted_assets']))
        self.bundle_summary.setText(message)
        fill(
            self.plan_table,
            sorted(data["losses"].items()),
            lambda item: (tr(LOSS.get(item[0], item[0])), number(item[1])),
        )
        summary_line = (
            tr('Переслано {0} · Ответов {1} · Элементов альбомов {2} · Нет файлов {3} · Несовместимых или непроверенных видео {4} · Записей с пометками {5}.').format(number(stats['forwards']), number(stats['replies']), number(stats['album_elements']), number(stats['missing_assets']), number(stats['video_incompatible']), number(stats['annotated_records']))
        )
        self.plan_totals.setText(summary_line + "\n" + tr(LOCAL) + " " + tr(SERVER))
        self.import_tabs.setCurrentIndex(0)
        self.navigate_to(Section.IMPORT)
        return message

    def use_bundle(self, bundle):
        self.bundle = bundle.root
        project = self.project if self.stats.get("sha256") == bundle.data["source_sha256"] else None
        origin = ""
        if project:
            with catalogue(project) as db:
                origin = properties(db).get("original", "")
        self.telegram.set_package(bundle, project, origin)
        self.comparison = None
        self.recovery.bind(None)
        self.verdict.setText(tr("Пакет открыт. Можно сравнить два снимка."))
        self.levels.setText(tr("A · Проект    B1 · История на сервере    B2 · Нативное авторство    C · Клиенты"))
        self.overview.set_quality(None)
        self.evidence_table.setRowCount(0)
        return self.show_plan(bundle.data, False)

    def select_bundle(self, *, on_ready=None):
        path = QFileDialog.getExistingDirectory(
            self, tr("Папка с manifest.json и _chat.txt"), str(app_home() / "packages")
        )
        if path:

            def ready(bundle):
                message = self.use_bundle(bundle)
                if on_ready:
                    on_ready(bundle)
                return message

            self.run_job(tr("Проверка пакета"), lambda p: load_bundle(path, p), ready)

    def simulate(self):
        if not self.bundle:
            self.info("Подготовьте или откройте пакет истории.")
            return
        root, scenario = self.bundle, self.scenario.currentData()
        project = self.telegram.project

        def job(progress):
            return rehearse(load_bundle(root, progress), scenario, progress, project=project)

        def ready(result):
            if "verification" in result:
                return self.show_comparison(result["verification"])
            return f"Локальная имитация приняла {number(result['messages'])} сообщений."

        self.run_job("Сценарий Mock API", job, ready)

    def compare_observations(self):
        if not self.bundle:
            self.info("Сначала откройте пакет импорта.")
            return
        files, _ = QFileDialog.getOpenFileNames(
            self, "Один или два снимка одного диалога", "", "Снимки (*.json)"
        )
        if not files:
            return
        if len(files) not in (1, 2):
            self.info("Выберите один или два JSON-снимка. Для полной сверки нужен снимок с каждого аккаунта.")
            return
        mapping, _ = QFileDialog.getOpenFileName(
            self, "Сопоставление имён и ID (можно пропустить)", "", "JSON (*.json)"
        )
        root = self.bundle
        project = self.telegram.project
        chosen_map = self.telegram.mapping()

        def job(progress):
            from .cli import record_evidence
            from .files import read_json
            from .verification import compare

            snapshots = [read_json(file) for file in files]
            result = compare(
                load_bundle(root, progress),
                snapshots[0],
                snapshots[1] if len(snapshots) == 2 else None,
                read_json(mapping) if mapping else chosen_map,
                project=project,
                progress=progress,
            )
            return record_evidence(result)

        self.run_job(
            "Сравнение результата",
            job,
            self.show_comparison,
        )

    def show_comparison(self, result):
        self.comparison = result
        self.recovery.bind(None)
        self.wizard.refresh()
        mode = "Имитация" if result["mode"] == "mock" else "Снимки владельца"
        verdict = VERIFICATION_NAMES.get(result.get("verification_status"), VERDICTS[result["state"]])
        message = f"{mode} · {verdict} · {number(result['expected'])} ожидаемых сообщений"
        self.verdict.setText(message)
        self.overview.set_quality(result)
        levels = result["levels"]
        statuses = {"verified": "совпало", "differences": "расхождения", "unknown": "нет подтверждения"}
        text = f"A · {statuses[levels['A']['status']]}    B1 · {statuses[levels['B1']]}    C · {statuses[levels['C']]}"
        text += "\nB2 · Назначение исходных ID и отправителя не предусмотрено API."
        if result.get("evidence_status") == "NO_EVIDENCE_IN_WINDOW":
            text += "\nВ выбранном диапазоне нет подтверждающих записей."
        if result["visual_status"] == "VISUAL_RECONSTRUCTION_UNCONFIRMED":
            text += "\nОтображение истории после перезапуска пока не подтверждено."
        if result["historical_metadata_only"]:
            text += "\nИсторическая дата совпала в метаданных. Совпадение даты в клиенте не подтверждено."
        self.levels.setText(text)
        self.evidence_table.setRowCount(0)
        a, *other = result["views"]
        b = other[0] if other else None
        if result.get("operation_id") and self.telegram.operation_id == result["operation_id"]:
            self.telegram.observed = result.get("server_messages_observed")
            self.telegram.verified = result.get("client_messages_verified")
            self.telegram.count()
        rows = [
            (title, number(a.get(key, 0)), number(b.get(key, 0)) if b else "—")
            for title, key in (
                ("Количество записей", "observed"),
                ("Пропущено", "missing"),
                ("Лишние записи", "extra"),
                ("Неоднозначное сопоставление", "ambiguous"),
                ("Повторяющиеся серверные ID", "duplicate_ids"),
            )
        ]

        def metric(view, key):
            if view is None:
                return "—"
            values = view["metrics"][key]
            return "   ·   ".join(
                f"{title} {number(values.get(state, 0))}"
                for state, title in (("same", "✓"), ("different", "≠"), ("unknown", "?"))
            )

        rows.extend((label, metric(a, key), metric(b, key)) for key, label in FIELD_NAMES.items())

        def scope(view):
            info = view.get("scope") or {} if view else {}
            if not info:
                return "—"
            if info["kind"] == "legacy":
                return "Снимок предыдущей версии"
            bounds = (
                f"{info['historical_from']} — {info['historical_through']} UTC"
                if info["kind"] == "historical_date"
                else f"ID после {info['after_id']} до {info['through_id'] or 'конца'}"
            )
            return bounds + (" · достигнут предел чтения" if info["truncated"] else "")

        rows.insert(0, ("Диапазон снимка", scope(a), scope(b)))
        fill(self.evidence_table, rows, lambda values: values)
        for column in (1, 2):
            self.evidence_table.resizeColumnToContents(column)
        self.verification_tabs.setCurrentIndex(1)
        self.navigate_to(Section.VERIFICATION)
        return message

    def _save_dialog(self):
        path, selection = QFileDialog.getSaveFileName(
            self, "Сохранить отчёт", "", "HTML (*.html);;JSON (*.json)"
        )
        if not path:
            return None
        dest = Path(path)
        if not dest.suffix:
            dest = dest.with_suffix(".json" if selection.startswith("JSON") else ".html")
        return dest

    def export_project_report(self):
        if not self.project:
            self.info("Сначала откройте проект.")
            return
        dest = self._save_dialog()
        if dest:
            project = self.project
            self.run_job(
                "Отчёт по проекту",
                lambda p: save_report(dest, project=project, progress=p),
                lambda path: f"Отчёт сохранён в {path}",
            )

    def export_import_report(self):
        if not self.plan_data:
            self.info("Оцените перенос или откройте пакет.")
            return
        dest = self._save_dialog()
        if dest:
            project, package, planned = self.project, self.plan_data, self.planned
            protected = (self.bundle,) if self.bundle else ()
            self.run_job(
                "Отчёт о переносе",
                lambda p: save_report(
                    dest, project=project, package=package, planned=planned, progress=p, protected=protected
                ),
                lambda path: f"Отчёт сохранён в {path}",
            )

    def export_verification(self):
        if self.comparison is None:
            self.info("Сначала откройте снимки или запустите локальную проверку.")
            return
        choices = ("Обезличенный отчёт", "Частный отчёт с деталями снимков")
        choice, ok = QInputDialog.getItem(self, "Отчёт по результату", "Состав отчёта", choices, 0, False)
        if not ok:
            return
        dest = self._save_dialog()
        if dest:
            project, result = self.project, self.comparison
            protected = (self.bundle,) if self.bundle else ()
            self.run_job(
                "Отчёт по результату",
                lambda p: save_report(
                    dest,
                    project=project,
                    verification=result,
                    progress=p,
                    protected=protected,
                    private_details=choice == choices[1],
                ),
                lambda path: f"Отчёт сохранён в {path}",
            )

    def owner_console(self):
        if not self.bundle:
            self.info("Сначала подготовьте или откройте пакет.")
            return
        peer, ok = QInputDialog.getText(self, "Целевой диалог", "Введите @username или числовой ID чата")
        if not ok or not peer.strip():
            return
        if getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            command = [str(Path(sys.executable).with_name("python.exe")), "-m", "gtr"]
        try:
            subprocess.Popen(
                [*command, "live", str(self.bundle), "--peer", peer.strip(), "--pause"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        except (OSError, AttributeError):
            self.info("Не удалось открыть консоль владельца. Воспользуйтесь командой live из инструкции.")

    def run_media_job(self, title, job, ready, *, project, decisions=None):
        if self.worker or self.telegram.busy:
            self.info(tr("Дождитесь окончания текущей операции."))
            return
        queue = MediaQueue(self.media_queue_changed.emit)
        decisions = dict(decisions or {})
        saved = self.saved_video_decisions()

        def remember():
            saved.update(decisions)
            self.save_video_decisions(saved)

        def work(progress):
            return job(progress, media_queue=queue, video_decisions=decisions)

        def complete(value):
            result = ready(value)
            for key in decisions:
                saved.pop(key, None)
            self.save_video_decisions(saved)
            return result

        def review(error):
            dialog = CompatibilityDialog(self, error.entries, project, self.video_tools(), initial=saved)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                decisions.update(dialog.decisions())
                remember()
                self.run_job(tr("Продолжение подготовки"), work, complete, on_media_decision=review)
            else:
                self.stage.setText(tr("Подготовка отменена"))
                self.bar.setRange(0, 1)
                self.bar.setValue(0)
                self.outcome.setText(tr("Совместимые копии можно использовать при следующей подготовке."))
            dialog.deleteLater()

        if decisions:
            remember()
        self.run_job(title, work, complete, on_media_decision=review)

    def saved_video_decisions(self):
        data = self.settings.value("pending_media_decisions", "{}", type=str)
        try:
            return validate_decisions(json.loads(data))
        except (ValueError, RecoveryError):
            return {}

    def save_video_decisions(self, decisions):
        if decisions:
            self.settings.setValue("pending_media_decisions", json.dumps(decisions, separators=(",", ":")))
        else:
            self.settings.remove("pending_media_decisions")
        self.settings.sync()

    def run_job(self, title, job, ready, *, on_media_decision=None):
        if self.worker or self.telegram.busy:
            self.info(tr("Дождитесь окончания текущей операции."))
            return
        self.after_job = ready
        self.after_media_decision = on_media_decision
        self.pending_media_decision = None
        self.worker = Worker(job, self)
        self.worker.update.connect(self.progress)
        self.worker.result.connect(self.job_done)
        self.worker.error.connect(self.job_error)
        self.worker.decision.connect(self.job_waiting_for_media)
        self.worker.finished.connect(self.finished)
        self.job_title.setText(tr(title))
        self.log(tr(title))
        self.outcome.clear()
        self.media_queue_display.hide()
        self.stage.setText(tr("Начато"))
        self.stop_btn.setEnabled(True)
        self.bar.setRange(0, 0)
        self.navigate_to(Section.PROGRESS)
        self.telegram.refresh()
        self.worker.start()

    def progress(self, stage, done, total):
        self.stage.setText(
            tr(STAGES.get(stage, stage)) + (f"   {number(done)} / {number(total)}" if total else "")
        )
        self.bar.setRange(0, total)
        self.bar.setValue(done)

    def job_done(self, value):
        try:
            self.outcome.setText(tr(self.after_job(value)))
            self.stage.setText(tr("Готово"))
            self.bar.setRange(0, 1)
            self.bar.setValue(1)
            self.log(tr("Операция завершена."))
        except (OSError, RecoveryError):
            self.job_error(tr("Не удалось открыть сохранённый результат."))

    def job_error(self, safe_message):
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        if self.stage.text() != tr("Ограничение Telegram"):
            self.stage.setText(tr("Операция остановлена"))
        self.outcome.setText(tr(safe_message))
        self.log(tr(safe_message))

    def job_waiting_for_media(self, error):
        self.pending_media_decision = error
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.stage.setText(tr("Ожидание решения пользователя"))
        self.outcome.setText(tr("Выберите способ обработки видео для продолжения."))

    def stop_job(self):
        if self.worker:
            self.worker.stop.set()
        self.stop_btn.setEnabled(False)

    def finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.stop_btn.setEnabled(False)
        self.telegram.refresh()
        if self.close_pending:
            self.close()
        elif self.pending_media_decision:
            error, self.pending_media_decision = self.pending_media_decision, None
            if review := self.after_media_decision:
                review(error)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.close_pending = True
            self.stop_job()
            event.ignore()
        elif not self.telegram.shutdown():
            self.close_pending = True
            event.ignore()
        else:
            self.diagnostics.close()
            event.accept()

    def _close_after_telegram(self):
        if self.close_pending:
            self.close()


def application():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    if not hasattr(app, "gtr_translator"):
        translator = QTranslator(app)
        language = QSettings("gAssets", "gTR").value("language", "ru")
        if translator.load(f"qtbase_{language}", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
            app.installTranslator(translator)
        app.gtr_translator = translator
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "gtr.ico")))
    app.setStyle("Fusion")
    if sys.platform == "win32" and "Segoe UI" not in QFontDatabase.families():
        font_dir = Path(os.environ["SystemRoot"]) / "Fonts"
        for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf", "seguiemj.ttf"):
            QFontDatabase.addApplicationFont(str(font_dir / name))
    font = QFont()
    font.setFamilies(["Inter", "Segoe UI", "Arial"])
    app.setFont(font)
    palette = QPalette()
    for role, color in {
        "Window": "#151a1d",
        "WindowText": "#e6edeb",
        "Base": "#1b2225",
        "AlternateBase": "#222b2d",
        "Text": "#e6edeb",
        "Button": "#283235",
        "ButtonText": "#e6edeb",
        "Highlight": "#36516a",
        "HighlightedText": "#eaf4ee",
    }.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    app.setPalette(palette)
    app.setStyleSheet(STYLE)
    return app


def launch(project=None, *, bundle=None, section=None, classic=False):
    if (not classic and updates.portable() and not os.environ.get(updates.BOOT_ENV)
            and (installed := updates.installed_update())):
        try:
            updates.launch(installed, project)
            return 0
        except (OSError, RecoveryError):
            pass
    app = application()
    window = Window(project)
    window.show()
    if section == "history":
        window.show_import_history()
    elif section in {"telegram", "settings", "import"}:
        window.navigate_to({"telegram": Section.TELEGRAM, "settings": Section.SETTINGS, "import": Section.IMPORT}[section])
    if bundle:
        window.run_job(
            tr("Проверка пакета"), lambda progress: load_bundle(bundle, progress), window.use_bundle,
        )
    if os.environ.get(updates.BOOT_ENV):
        QTimer.singleShot(0, updates.signal_ready)
    status = app.exec()
    if window.restart_folder:
        try:
            updates.launch(window.restart_folder, window.project)
        except (OSError, RecoveryError):
            QMessageBox.information(None, "gTR", "Новая версия не запустилась. Открывается предыдущая версия.")
            return launch(window.project)
    return status
