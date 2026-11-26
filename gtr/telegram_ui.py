"""Owner-only connection controls. The Qt event loop never waits for Telegram."""

import asyncio
import sqlite3
from collections import defaultdict
from pathlib import Path
from threading import Event
from uuid import uuid4

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QObject,
    QSize,
    Qt,
    QThread,
    QUrl,
    Signal,
)
from PySide6.QtGui import QDesktopServices, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .account import AccountProfile
from .conversion import load_bundle, prepare_eligible
from .dashboard import label, number, panel, size_text
from .faults import ImportFault
from .i18n import tr
from .journal import PHASE_NAMES, RecoveryRequired
from .media_queue import video_decision_key
from .media_queue_ui import CompatibilityDialog
from .model import Cancelled, RecoveryError
from .observation import CaptureWindow
from .owner import api_vault_path, journal_for, vault_path
from .server import OwnerSession
from .test_mode import ResetResult

STAGES = {
    "local_check": "Проверка архива",
    "inspect_media": "Проверка типов и метаданных вложений",
    "verify_text": "Сверка текста истории",
    "copy_text": "Копирование текста истории",
    "validated": "Пакет проверен локально",
    "initialized": "Telegram подготовил импорт",
    "verify": "Сверка файлов",
    "copy": "Фиксация копии пакета",
    "local_history": "Сверка исходных сообщений",
    "check_history": "Проверка текстового профиля",
    "check_peer": "Проверка диалога",
    "check_limits": "Проверка лимитов загрузки",
    "ready": "Проверка пройдена",
    "await_confirmation": "Ожидание подтверждения владельца",
    "confirmed": "Владелец подтвердил запуск",
    "init_import": "Создание операции импорта",
    "upload_transcript": "Загружается текст истории",
    "transcript_uploaded": "Текст истории принят. Импорт ещё не запущен.",
    "upload": "Загрузка медиа",
    "upload_item": "Отправляется вложение",
    "start_import": "Запуск импорта в Telegram",
    "start_pending": "Отправляется финальный запрос. Ответ ещё не получен.",
    "verifying": "Запуск принят. Ожидаются снимки обоих аккаунтов.",
    "observe": "Проверка восстановленной истории",
    "limited": "Ограничение Telegram",
    "error": "Ошибка",
}
ACTION = {
    "connect": "Подключение",
    "code": "Подтверждение кода",
    "password": "Двухэтапная проверка",
    "remember": "Сохранение сессии",
    "profile": "Чтение карточки аккаунта",
    "forward_names": "Чтение имён по открытым ID",
    "disconnect": "Отключение",
    "dialogs": "Список диалогов",
    "preflight": "Проверка пакета и цели",
    "import": "Импорт",
    "baseline": "Исходная граница",
    "observe": "Снимок истории",
    "test_connect": "Вход во второй тестовый аккаунт",
    "test_code": "Код второго тестового аккаунта",
    "test_password": "Двухэтапная проверка второго аккаунта",
    "test_cancel": "Отмена тестового сброса",
}


def _button(text, action, primary=False):
    button = QPushButton(text)
    button.clicked.connect(action)
    if primary:
        button.setObjectName("primary")
    return button


def _row(*widgets):
    bar = QHBoxLayout()
    for widget in widgets:
        bar.addWidget(widget)
    bar.addStretch()
    return bar


def _scroll():
    outer = QScrollArea()
    outer.setWidgetResizable(True)
    outer.setFrameShape(QScrollArea.Shape.NoFrame)
    content = QWidget()
    layout = QVBoxLayout(content)
    layout.setContentsMargins(0, 0, 9, 0)
    layout.setSpacing(12)
    outer.setWidget(content)
    return outer, layout


class TelegramThread(QThread):
    online = Signal()
    response = Signal(str, object)
    failure = Signal(str, object)
    update = Signal(str, int, int)
    file_progress = Signal(object)

    def __init__(self, parent=None, session_factory=OwnerSession):
        super().__init__(parent)
        self.session_factory = session_factory
        self.halt_requested = Event()
        self.abort = Event()
        self.loop = None
        self.task = None

    def run(self):
        asyncio.run(self.serve())

    async def serve(self):
        self.loop = asyncio.get_running_loop()
        self.halt = asyncio.Event()
        self.session = self.session_factory()
        if self.halt_requested.is_set():
            self.halt.set()
        self.online.emit()
        await self.halt.wait()
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        try:
            await self.session.disconnect()
        except (RecoveryError, OSError):
            pass
        self.loop = None

    def submit(self, name, operation):
        def start():
            if not self.halt_requested.is_set():
                self.abort.clear()
                self.task = self.loop.create_task(self.execute(name, operation))

        if self.loop is not None:
            self.loop.call_soon_threadsafe(start)

    async def execute(self, name, operation):
        def progress(stage, done, total):
            if self.abort.is_set():
                raise Cancelled()
            self.update.emit(stage, done, total)

        def transferred(detail):
            if self.abort.is_set():
                raise Cancelled()
            self.file_progress.emit(detail)

        progress.upload_bytes = transferred
        try:
            result = await operation(self.session, progress)
            self.response.emit(name, result)
        except asyncio.CancelledError:
            self.failure.emit(name, Cancelled())
        except RecoveryError as error:
            self.failure.emit(name, error)
        except Exception:  # noqa: BLE001 - network library errors can include private data
            self.failure.emit(
                name, RecoveryError("Операция Telegram остановлена. Проверьте соединение и журнал.")
            )

    def cancel(self):
        self.abort.set()
        if self.loop is not None:

            def stop():
                if self.task and not self.task.done():
                    self.task.cancel()

            self.loop.call_soon_threadsafe(stop)

    def shutdown(self):
        self.halt_requested.set()
        self.abort.set()
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.halt.set)


class TelegramControls(QObject):
    changed = Signal(bool)
    idle_closed = Signal()
    updated = Signal()

    def __init__(self, parent, log, go_to_account, *, thread_factory=TelegramThread):
        super().__init__(parent)
        self.window, self.log, self.go_to_account = parent, log, go_to_account
        self.thread_factory = thread_factory
        self.thread, self.pending = None, None
        self.busy, self.closing = False, False
        self.account, self.review, self.package, self.project = None, None, None, None
        self.bound_profile = None
        self.test_reset_id = None
        self.package_source = ""
        self.reset_counters()
        self.account_page = self._account_page()
        self.server_page = self._server_page()
        self.refresh()

    def _account_page(self):
        widget, layout = _scroll()
        box, form = panel()
        form.addWidget(label("Аккаунт Telegram", "heroTitle"))
        self.account_line = label("Аккаунт отключён.", "secondary")
        self.avatar = label("?", "section")
        self.avatar.setFixedSize(64, 64)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.account_details = label("", "muted")
        card = QVBoxLayout()
        card.addWidget(self.account_line)
        card.addWidget(self.account_details)
        card_row = QHBoxLayout()
        card_row.addWidget(self.avatar)
        card_row.addLayout(card, 1)
        form.addLayout(card_row)
        self.api_id = QLineEdit(placeholderText="API ID")
        self.api_hash = QLineEdit(placeholderText="API hash")
        self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.phone = QLineEdit(placeholderText="Международный формат")
        for title, field in (("API ID", self.api_id), ("API hash", self.api_hash), ("Телефон", self.phone)):
            field.setAccessibleName(title)
        creds = QGridLayout()
        for i, (title, field) in enumerate(
            (("API ID", self.api_id), ("API hash", self.api_hash), ("Телефон", self.phone))
        ):
            creds.addWidget(label(title, "muted"), 0, i)
            creds.addWidget(field, 1, i)
        form.addLayout(creds)
        form.addLayout(_row(_button("Как получить данные API…", self.open_api_help)))
        self.use_saved_api = QCheckBox("Использовать сохранённые данные API")
        self.use_saved_api.setChecked(api_vault_path().is_file())
        self.use_saved_api.toggled.connect(self._api_fields)
        self.remember_api = QCheckBox("Запомнить API ID и API hash в Windows")
        form.addWidget(self.use_saved_api)
        form.addWidget(self.remember_api)
        self.use_saved = QCheckBox("Использовать сохранённую сессию Windows")
        self.use_saved.setEnabled(vault_path().is_file())
        self.use_saved.toggled.connect(lambda value: self.phone.setEnabled(not value))
        self.remember = QCheckBox("Сохранить сессию с шифрованием Windows")
        form.addWidget(self.use_saved)
        form.addWidget(self.remember)
        self.connect_button = _button("Подключить Telegram…", self.connect_account, True)
        self.disconnect_button = _button("Отключить", self.disconnect_account)
        self.forget_button = _button("Удалить локальную сессию…", self.forget)
        self.forget_api_button = _button("Удалить данные API…", self.forget_api)
        self.profile_button = _button("Обновить карточку", self.request_profile)
        form.addLayout(_row(self.connect_button, self.disconnect_button, self.forget_button))
        form.addLayout(_row(self.profile_button, self.forget_api_button))
        layout.addWidget(box)
        box, group = panel()
        group.addWidget(label("Доступные диалоги", "section"))
        self.dialogs_button = _button("Загрузить диалоги", self.load_dialogs)
        group.addLayout(_row(self.dialogs_button))
        self.dialog_summary = label("После подключения можно загрузить список для выбора цели.", "muted")
        group.addWidget(self.dialog_summary)
        self.dialog_preview = QComboBox()
        self.dialog_preview.setAccessibleName("Доступные чаты")
        self.dialog_preview.setPlaceholderText("Загрузите список диалогов")
        group.addWidget(self.dialog_preview)
        layout.addWidget(box)
        layout.addStretch()
        return widget

    def _server_page(self):
        widget, layout = _scroll()
        box, body = panel()
        header = QHBoxLayout()
        self.server_account = label(tr("Подключите аккаунт в Telegram."), "muted")
        header.addWidget(self.server_account, 1)
        header.addWidget(_button(tr("Управление аккаунтом"), self.go_to_account))
        body.addLayout(header)
        self.target = QComboBox()
        self.target.setAccessibleName(tr("Целевой диалог"))
        self.target.setPlaceholderText(tr("Выберите целевой диалог"))
        self.target.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.target.setMinimumContentsLength(30)
        self.target.currentIndexChanged.connect(self.select_target)
        body.addWidget(self.target)
        self.source = label(tr("Подготовьте или откройте пакет на соседней вкладке."), "muted")
        self.source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self.source)
        self.plan = label("", "secondary")
        body.addWidget(self.plan)
        self.local_status = label(
            tr("Пакет — ожидается   ·   Формат Telegram — ожидается   ·   Диалог — ожидается"), "muted"
        )
        body.addWidget(self.local_status)
        self.consent = QCheckBox(tr("Все затрагиваемые участники явно согласились на импорт в этот диалог"))
        self.consent.setAccessibleName(tr("Согласие участников на импорт выбранного пакета в выбранный диалог"))
        self.consent.toggled.connect(self.consent_changed)
        body.addWidget(self.consent)
        self.preflight_button = _button(tr("Проверить формат и диалог"), self.preflight, True)
        self.start_button = _button(tr("Подтвердить и импортировать…"), self.confirm, True)
        self.cancel_button = _button(tr("Остановить"), self.cancel)
        body.addLayout(_row(self.preflight_button, self.start_button, self.cancel_button))
        body.addWidget(label(tr("Проверка формата передаёт Telegram до 100 первых строк пакета."), "muted"))
        layout.addWidget(box)
        authors, group = panel()
        group.addWidget(label(tr("Авторство для верификации"), "section"))
        self.self_author, self.peer_author = QComboBox(), QComboBox()
        group.addLayout(_row(label(tr("Мой аккаунт")), self.self_author, label(tr("Собеседник")), self.peer_author))
        group.addWidget(label(tr("Карта используется при сравнении двух снимков после импорта."), "muted"))
        box, group = panel()
        self.state = label(tr("Ожидание пакета и аккаунта."), "section")
        self.counts = label("", "secondary")
        self.details = label("", "muted")
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        group.addWidget(self.state)
        group.addWidget(self.counts)
        group.addWidget(self.details)
        self.recovery_panel = self.make_recovery_panel()
        group.addWidget(self.recovery_panel)
        self.diagnostic_button = _button(tr("Сохранить диагностический отчёт…"), self.export_diagnostic)
        self.repair_video_button = _button(tr("Выбрать обработку видео…"), self.repair_video)
        group.addLayout(_row(self.diagnostic_button, self.repair_video_button))
        self.baseline_button = _button(tr("Сохранить границу до импорта…"), self.baseline)
        self.observe_button = _button(tr("Сохранить снимок…"), self.observe)
        self.historical_button = _button(tr("Снимок по датам…"), self.historical_observe)
        self.probe_media = QCheckBox(tr("Проверить чтение первых 4 КиБ медиа"))
        group.addLayout(_row(self.baseline_button, self.observe_button, self.historical_button))
        group.addWidget(self.probe_media)
        group.addWidget(
            label(
                tr("Сохраните границу до импорта на каждом аккаунте. После запуска снимите историю отдельно с обоих аккаунтов и проверьте её в клиентах после перезапуска."),
                "muted",
            )
        )
        layout.addWidget(box)
        layout.addWidget(authors)
        layout.addStretch()
        return widget

    def info(self, text):
        QMessageBox.information(self.window, "gTR", text)

    def make_recovery_panel(self):
        box, layout = panel()
        layout.addWidget(label(tr("Предыдущие операции импорта"), "section"))
        summary = label("", "secondary")
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(summary)
        review_button = _button(tr("Проверить предыдущую операцию"), self.review_previous)
        matches_button = _button(tr("Просмотреть совпадения"), self.show_recovery_matches)
        new_button = _button(tr("Импортировать только новые записи"), self.prepare_new_records, True)
        cancel = _button(tr("Отмена"), self.dismiss_recovery)
        layout.addLayout(_row(review_button, matches_button))
        layout.addLayout(_row(new_button, cancel))
        hint = label("", "muted")
        layout.addWidget(hint)

        def update():
            review = self.recovery
            box.setVisible(review is not None)
            if review is None:
                return
            summary.setText(self.recovery_summary(review))
            ready = not self.busy and not self.closing and self.window.worker is None
            review_button.setEnabled(
                bool(any(c.operation_id for c in review.conflicts) and ready and self.account and self.target.currentData())
            )
            matches_button.setEnabled(ready)
            new_button.setEnabled(bool(ready and self._can_prepare_eligible()))
            cancel.setEnabled(ready)
            if self._can_prepare_eligible():
                hint.setText(
                    tr("Для новых записей будет создан отдельный пакет. Отправка потребует нового согласия.")
                )
            elif review.eligible_count:
                hint.setText(
                    tr("Для подготовки новых записей нужен исходный проект и пакет, подготовленный в Beta или позднее.")
                )
            else:
                hint.setText(tr("Для этого пакета сейчас нет записей, которые можно безопасно отправить."))

        self.updated.connect(update)
        update()
        return box

    @staticmethod
    def recovery_summary(review):
        lines = [
            f"{item.operation_id} · {tr(PHASE_NAMES.get(item.phase, 'Требуется проверка'))}"
            + (
                tr(' · пересечение {0}').format(number(item.overlap))
                if item.overlap is not None
                else tr(" · объём неизвестен")
            )
            for item in review.conflicts
        ]
        overlap = number(review.overlap) if review.overlap is not None else tr("не установлено")
        lines.append(
            tr('Выбрано {0} · Совпадения: {1}\nПодтверждено {2} · Можно добавить {3} · Требуют проверки {4}').format(number(review.selected), overlap, number(review.confirmed), number(review.eligible_count), number(review.unknown))
        )
        return "\n".join(lines)

    def show_recovery_matches(self):
        if self.recovery is None:
            return
        view = QDialog(self.window)
        view.setWindowTitle("Совпадения и новые записи")
        layout = QVBoxLayout(view)
        text = QPlainTextEdit(view)
        text.setReadOnly(True)
        lines = [self.recovery_summary(self.recovery), "", "Номера записей в исходной истории"]
        for title, intervals in (
            ("Подтверждены в Telegram", self.recovery.confirmed_records),
            ("Требуют проверки", self.recovery.unknown_records),
            ("Можно добавить", self.recovery.eligible),
        ):
            ranges = ", ".join(
                str(first) if first == last else f"{first}–{last}" for first, last in intervals
            )
            lines.append(f"{title} · {ranges or '—'}")
        if self.recovery.overlap is None:
            lines.append("По имеющимся данным точное пересечение не установлено.")
        if any(item.can_attach for item in self.recovery.conflicts):
            lines.append("Укажите исходный пакет предыдущей операции, чтобы уточнить выборку.")
        text.setPlainText("\n".join(lines))
        layout.addWidget(text)
        buttons = QDialogButtonBox(view)
        buttons.addButton("Закрыть", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(view.reject)
        layout.addWidget(buttons)
        view.resize(680, 390)
        view.exec()

    def dismiss_recovery(self):
        self.recovery = None
        self.consent.setChecked(False)
        self.refresh()

    def review_previous(self):
        review, bundle, target, account = self.recovery, self.package, self.target.currentData(), self.account
        if review is None or not review.conflicts or not bundle or not account or not target or self.busy:
            return
        candidates = [c for c in review.conflicts if c.operation_id]
        if not candidates:
            return
        prior = candidates[0]
        if len(candidates) > 1:
            journal = journal_for(account, target)
            choices = [
                f"{i}. {(journal.operation(item.operation_id) or {}).get('created', '')} · "
                f"{tr(PHASE_NAMES.get(item.phase, 'Требуется проверка'))}"
                for i, item in enumerate(candidates, 1)
            ]
            chosen, ok = QInputDialog.getItem(
                self.window, tr("Предыдущие операции"), tr("Выберите дату и состояние"), choices, 0, False
            )
            if not ok:
                return
            prior = candidates[choices.index(chosen)]
        if prior.can_recover:
            if (
                QMessageBox.question(
                    self.window,
                    tr("Завершение прежней подготовки"),
                    tr("Журнал подтверждает, что финальный запрос не отправлялся. Закрыть прежнюю подготовку? "
                    "Новый импорт потребуется подтвердить отдельно."),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            action = tr("Закрытие прежней подготовки")
            path = None
        elif not prior.can_attach:
            action, path = tr("Открытие сохранённой операции"), None
        else:
            action = tr("Уточнение выборки") if prior.can_attach else tr("Проверка предыдущего пакета")
            path = QFileDialog.getExistingDirectory(
                self.window, tr("Откройте папку пакета предыдущей операции"), str(bundle.root.parent)
            )
            if not path:
                return

        def job(progress):
            try:
                journal = journal_for(account, target)
                if path is None and not prior.can_recover:
                    from .incremental_import import recover_package

                    return recover_package(journal, prior.operation_id, progress), None
                if path is None:
                    journal.recover_before_start(prior.operation_id, acknowledged=True)
                else:
                    previous = load_bundle(path, progress)
                    operation = journal.operation(prior.operation_id)
                    if not operation or previous.manifest_hash() != operation["manifest_sha256"]:
                        raise RecoveryError(
                            tr("Этот пакет не совпадает с ранее проверенным пакетом выбранной операции.")
                        )
                    if not prior.can_attach:
                        return previous, None
                    journal.attach_selection(prior.operation_id, previous)
                return None, journal.review_bundle(bundle)
            except sqlite3.Error:
                raise RecoveryError(tr("Не удалось проверить локальный журнал операций.")) from None

        def ready(value):
            previous, updated = value
            if previous is not None:
                return self.window.open_recovery_verification(previous)
            self.recovery = updated if updated and updated.conflicts else None
            self.consent.setChecked(False)
            if self.recovery is None:
                self.blocked = False
                self.state.setText(tr("Локальная проверка пройдена. Проверьте текущий пакет и диалог."))
                self.local_status.setText(tr("Доступна повторная проверка пакета."))
            else:
                self.state.setText(tr("Сведения о предыдущих операциях обновлены."))
            self.count()
            self.refresh()
            return self.state.text()

        self.window.run_recovery_job(action, job, ready)

    def _can_prepare_eligible(self):
        return bool(
            self.account
            and self.target.currentData()
            and self.recovery
            and self.recovery.eligible
            and self.project
            and self.package
            and self.package.data["schema"] == 3
        )

    def prepare_new_records(self):
        review, bundle, project = self.recovery, self.package, self.project
        account, target = self.account, self.target.currentData()
        if self.busy or self.closing or self.window.worker or not self._can_prepare_eligible():
            return
        parent = QFileDialog.getExistingDirectory(
            self.window, tr("Куда сохранить пакет новых записей"), str(bundle.root.parent)
        )
        if not parent:
            return
        dest = Path(parent) / f"gTR-new-{uuid4().hex[:10]}"
        tools = self.window.video_tools()
        policy = self.window.effective_video_policy(bundle.data.get("video_policy"))

        def job(progress, **media):
            try:
                current = journal_for(account, target).review_bundle(bundle)
            except sqlite3.Error:
                raise RecoveryError(tr("Не удалось проверить локальный журнал операций.")) from None
            if current.eligible != review.eligible:
                raise RecoveryError(tr("Состояние прошлых операций изменилось. Проверьте пакет заново."))
            return prepare_eligible(project, bundle, dest, review.eligible, progress,
                                    media_tools=tools, video_policy=policy, **media)

        self.window.run_recovery_media_job(
            tr("Подготовка новых записей"), job, self.window.use_recovery_bundle, project=project,
        )

    def repair_video(self):
        bundle, project = self.package, self.project
        if self.busy or self.closing or self.window.worker or not (bundle and project and self.failed_video):
            return
        attempt = self.failed_video
        entry = next((row for row in bundle.data["records"] if row.get("source_ordinal") == attempt.get("source_ordinal")), None)
        if not entry:
            self.info(tr("Откройте исходный проект и подготовьте пакет заново."))
            return
        video = entry.get("video_recovery", {})
        original = next((asset for asset in entry.get("original_media", [])
                         if asset["kind"] in {"video", "round_video"} and asset.get("sha256")), None)
        if not original:
            self.info(tr("Откройте исходный проект и подготовьте пакет заново."))
            return
        tools = self.window.video_tools()
        key = video.get("key") or video_decision_key(
            bundle.data["source_sha256"], bundle.data["chat_id"], entry["source_ordinal"], original["sha256"],
        )
        repeat_safe = video.get("action") != "convert" or video.get("method") == "remux"
        options = ("compatible", "document") if repeat_safe and tools.ffmpeg and tools.ffprobe else ("document",)
        dialog = CompatibilityDialog(self.window, [{
            "key": key, "group": "server_document", "name": original["original"],
            "date": entry.get("original_date"), "size": original.get("size"), "source": original.get("path"),
            "sha256": original["sha256"], "original": video.get("original") or {},
            "options": options, "recommended": options[0],
            "reason": "server_document" if repeat_safe else "server_document_copy",
        }], project, tools)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        decisions = dialog.decisions() if accepted else None
        dialog.deleteLater()
        if decisions is None or self.package is not bundle:
            return
        dest = bundle.root.parent / f"gTR-media-fix-{uuid4().hex[:10]}"
        ranges = bundle.record_intervals()
        profile, test_reset_id = self.bound_profile, self.test_reset_id
        account, target = self.account, self.target.currentData()

        def job(progress, **media):
            return prepare_eligible(project, bundle, dest, ranges, progress, media_tools=tools, **media)

        def ready(value):
            message = self.window.use_recovery_bundle(value)
            if self.account == account and self.target.currentData() == target:
                self.bound_profile, self.test_reset_id = profile, test_reset_id
            self.refresh()
            return message

        self.window.run_recovery_media_job(
            tr("Подготовка нового пакета после ответа Telegram"), job, ready, project=project, decisions=decisions,
        )

    def refresh(self):
        enabled = not self.busy and not self.closing and self.window.worker is None
        connected = self.account is not None
        target = self.target.currentData()
        self.connect_button.setEnabled(enabled and not connected)
        self.disconnect_button.setEnabled(enabled and connected)
        self.forget_button.setEnabled(enabled and vault_path().is_file())
        has_api = api_vault_path().is_file()
        self.use_saved_api.setEnabled(enabled and not connected and has_api)
        self.forget_api_button.setEnabled(enabled and has_api)
        self.remember_api.setEnabled(enabled and not connected and not self.use_saved_api.isChecked())
        self.profile_button.setEnabled(enabled and connected)
        self._api_fields()
        self.dialogs_button.setEnabled(enabled and connected)
        self.dialog_preview.setEnabled(enabled and connected)
        self.target.setEnabled(enabled and connected)
        ready = bool(enabled and connected and target and self.package)
        self.consent.setEnabled(ready)
        self.preflight_button.setEnabled(ready and self.consent.isChecked())
        self.start_button.setEnabled(ready and bool(self.review) and self.consent.isChecked())
        self.cancel_button.setEnabled(self.busy)
        observable = ready and target.kind in {"pm", "group"}
        self.baseline_button.setEnabled(observable)
        self.observe_button.setEnabled(observable)
        self.historical_button.setEnabled(observable)
        self.diagnostic_button.setEnabled(bool(enabled and self.operation_id))
        self.repair_video_button.setVisible(bool(self.failed_video and self.package and self.project))
        self.repair_video_button.setEnabled(bool(ready and self.failed_video))
        name = self.account.name if connected else tr("Аккаунт отключён.")
        self.account_line.setText(name)
        if not connected:
            self.avatar.clear()
            self.avatar.setText("?")
            self.account_details.clear()
        self.server_account.setText(name)
        self.changed.emit(connected)
        self.updated.emit()

    def dispatch(self, name, operation):
        if self.busy or self.closing:
            return
        if self.window.worker:
            self.info("Дождитесь окончания локальной операции.")
            return
        self.busy = True
        self.refresh()
        self.log(f"Telegram · {tr(ACTION[name])}")
        if self.thread is None:
            self.pending = name, operation
            self.thread = self.thread_factory(self)
            self.thread.online.connect(self._online)
            self.thread.response.connect(self._response)
            self.thread.failure.connect(self._failure)
            self.thread.update.connect(self.progress)
            if hasattr(self.thread, "file_progress"):
                self.thread.file_progress.connect(self.file_transfer)
            self.thread.finished.connect(self._thread_closed)
            self.thread.start()
        else:
            self.thread.submit(name, operation)

    def _online(self):
        if self.pending and not self.closing:
            self.thread.submit(*self.pending)
        self.pending = None

    def _thread_closed(self):
        worker, self.thread = self.thread, None
        worker.deleteLater()
        self.idle_closed.emit()

    def shutdown(self):
        if self.thread is None:
            return True
        self.closing = True
        self.thread.shutdown()
        return False

    def open_api_help(self):
        QDesktopServices.openUrl(QUrl("https://core.telegram.org/api/obtaining_api_id"))

    def connect_account(self):
        if self.window.worker:
            self.info("Дождитесь окончания локальной операции.")
            return
        saved_api = self.use_saved_api.isChecked()
        api_id = 0
        if not saved_api:
            try:
                api_id = int(self.api_id.text())
            except ValueError:
                self.info("Введите числовой API ID.")
                return
        api_hash = self.api_hash.text().strip()
        phone = self.phone.text().strip()
        saved = self.use_saved.isChecked()
        self.api_id.clear()
        self.api_hash.clear()
        self.phone.clear()
        self.state.setText("Подключение к Telegram…")
        self.dispatch(
            "connect",
            lambda session, _: session.connect(api_id, api_hash, phone, saved, saved_api=saved_api),
        )

    def _api_fields(self):
        editable = self.account is None and not self.busy and not self.use_saved_api.isChecked()
        for field in (self.api_id, self.api_hash):
            field.setEnabled(editable)

    def forget_api(self):
        if QMessageBox.question(
            self.window, "Данные Telegram API", "Удалить сохранённые API ID и API hash с этого компьютера?"
        ) == QMessageBox.StandardButton.Yes:
            try:
                api_vault_path().unlink(missing_ok=True)
                self.use_saved_api.setChecked(False)
                self.remember_api.setChecked(False)
                self.refresh()
            except (OSError, RecoveryError):
                self.info("Не удалось удалить сохранённые данные API.")

    def request_profile(self):
        if self.account:
            self.dispatch("profile", lambda session, _: session.profile())

    def show_profile(self, profile):
        self.avatar.clear()
        self.avatar.setText((self.account.name[:1] or "?").upper())
        if profile.avatar:
            buffer = QBuffer()
            buffer.setData(QByteArray(profile.avatar))
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            reader = QImageReader(buffer)
            size = reader.size()
            if size.isValid() and max(size.width(), size.height()) <= 2048:
                reader.setScaledSize(size.scaled(QSize(64, 64), Qt.AspectRatioMode.KeepAspectRatio))
                pixmap = QPixmap.fromImage(reader.read())
                if not pixmap.isNull():
                    self.avatar.setPixmap(pixmap)
        handle = f"@{profile.username} · " if profile.username else ""
        premium = " · Premium" if self.account.premium else ""
        self.account_details.setText(
            f"{handle}ID {self.account.id}{premium}\n"
            f"Возраст аккаунта · {profile.age or 'данные регистрации недоступны'}\n"
            f"Последняя активность · {profile.last_seen or 'данные недоступны'}"
        )

    def disconnect_account(self):
        self.dispatch("disconnect", lambda session, _: session.disconnect())

    def forget(self):
        if (
            QMessageBox.question(
                self.window,
                "Локальная сессия",
                "Удалить сохранённую копию сессии с этого компьютера?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            try:
                vault_path().unlink(missing_ok=True)
                self.use_saved.setChecked(False)
                self.use_saved.setEnabled(False)
                self.log("Удалена локальная копия сессии.")
                self.refresh()
            except (OSError, RecoveryError):
                self.info("Сессия недоступна для удаления.")

    def load_dialogs(self):
        self.review = None
        self.dispatch("dialogs", lambda session, _: session.manager.dialogs())

    def consent_changed(self, allowed):
        if not allowed:
            self.review = None
        self.refresh()

    def reset_counters(self):
        self.uploaded = self.checked = self.errors = 0
        self.validated = 0
        self.recovery = None
        self.blocked = self.transcript_sent = False
        self.transcript_uploaded = self.start_sent = self.start_accepted = False
        self.observed = self.verified = None
        self.operation_id = None
        self.failed_video = None

    def set_package(self, bundle=None, project=None, source_path=""):
        self.bound_profile = None
        self.test_reset_id = None
        self.package, self.project, self.package_source = bundle, project, source_path
        self.review = None
        if bundle:
            data = bundle.data
            self.source.setText(source_path or str(bundle.root))
            times = [r["original_date"] for r in data["records"] if r["included"] and r.get("original_date")]
            date_range = f" · {times[0][:10]} — {times[-1][:10]}" if times else ""
            dates = tr("Исходное время") if data.get("date_policy") == "historical" else tr("Альтернативный план дат")
            self.plan.setText(f"{data.get('chat_title') or 'Диалог'} · {dates}{date_range}")
        else:
            self.source.setText(tr("Подготовьте или откройте пакет на соседней вкладке."))
            self.plan.clear()
        self.select_target()
        self.count()

    def select_target(self, *_):
        self.review = None
        self.consent.setChecked(False)
        self.reset_counters()
        self.details.clear()
        self.local_status.setText(
            tr("Пакет — ожидается   ·   Формат Telegram — ожидается   ·   Диалог — ожидается")
        )
        if not self.busy:
            if not self.package:
                self.state.setText(tr("Подготовьте или откройте пакет."))
            elif not self.account:
                self.state.setText(tr("Пакет открыт. Подключите Telegram."))
            elif not self.target.currentData():
                self.state.setText(tr("Выберите целевой диалог."))
            else:
                self.state.setText(tr("Проверьте формат и диалог."))
        self._participant_choices()
        self.count()
        self.refresh()

    def _participant_choices(self):
        target = self.target.currentData()
        identities = defaultdict(set)
        if self.package:
            for entry in self.package.data["records"]:
                if entry["included"] and entry.get("author"):
                    identities[entry["author"]].add(entry.get("source_author_id"))
        pairs = (
            (self.self_author, self.account.id if self.account else 0),
            (self.peer_author, target.id if target else 0),
        )
        for menu, peer in pairs:
            menu.clear()
            menu.addItem(tr("Выберите имя"), "")
            for name, ids in identities.items():
                menu.addItem(name, name)
                if ids == {f"user{peer}"}:
                    menu.setCurrentIndex(menu.count() - 1)

    def mapping(self):
        target = self.target.currentData()
        own, other = self.self_author.currentData(), self.peer_author.currentData()
        if self.account and target and target.kind == "pm" and own and other and own != other:
            return {own: self.account.id, other: target.id}
        return None

    def preflight(self):
        if not self._test_mode_selected():
            self.info(tr("Включите тестовый режим в выбранном профиле и подготовьте пакет заново."))
            return
        if self.bound_profile:
            from .journal import Endpoint

            target = self.target.currentData()
            if (
                not self.account
                or not target
                or self.bound_profile
                != (self.account.id, Endpoint.telegram(self.account.id, target.id, target.kind).key)
            ):
                self.info(tr("Подключите аккаунт и выберите целевой диалог сохранённого профиля."))
                return
        if not self.package or not self.target.currentData() or not self.consent.isChecked() or self.busy:
            return
        self.review = None
        self.details.clear()
        self.reset_counters()
        self.count()
        root, target, project = self.package.root, self.target.currentData(), self.project
        expected = self.package.manifest_hash()
        test_scope = {"test_reset_id": self.test_reset_id} if self.test_reset_id is not None else {}
        self.dispatch(
            "preflight",
            lambda session, p: session.validate(
                root, target, project, p, expected=expected, consented=True, **test_scope
            ),
        )

    def confirm(self):
        review = self.review
        if review is None or self.busy or not self.consent.isChecked():
            return
        if review.test_reset_id != self.test_reset_id or not self._test_mode_selected():
            self.info(tr("Режим импорта изменился. Повторите проверку пакета."))
            return
        check = review.check
        dialog = QMessageBox(self.window)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setWindowTitle(
            tr("Подтверждение тестового импорта") if self.test_reset_id is not None else tr("Подтверждение владельца")
        )
        dialog.setText(
            tr('Аккаунт {0} · ID {1}\nДиалог {2} · ID {3}\n\nСообщения {4} · Медиа {5}').format(review.account.name, review.account.id, review.target.title, review.target.id, number(check.messages), number(check.media))
        )
        dialog.setInformativeText(
            tr("Пакет добавит записи в общий чат. Проверьте цель и сообщение Telegram.\n\n") + check.confirmation
        )
        start = dialog.addButton(tr("Начать импорт"), QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton(QMessageBox.StandardButton.Cancel)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.exec()
        if dialog.clickedButton() is start:
            self.review = None
            self.dispatch("import", lambda session, p: session.execute(review, p))

    def leave_test_mode(self):
        if self.test_reset_id is not None:
            self.test_reset_id = None
            self.select_target()

    def _test_mode_selected(self):
        if self.test_reset_id is None:
            return True
        page = self.window.profiles_page
        return page.test_mode.isChecked() and page.profile_identity == self.bound_profile

    def begin_test_reset(self, account_id, target):
        if self.account is None or self.account.id != account_id or self.busy or self.window.worker:
            return
        agree = QMessageBox.question(
            self.window,
            "Сбросить реестр тестовых импортов",
            f"Личный диалог между аккаунтами {account_id} и {target.id}\n\n"
            "Подтвердите, что оба тестовых аккаунта принадлежат вам. "
            "gTR проверит пустую историю в Telegram с обеих сторон перед открытием нового тестового цикла.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if agree != QMessageBox.StandardButton.Yes:
            return
        phone, ok = QInputDialog.getText(
            self.window, "Второй тестовый аккаунт", "Номер второго аккаунта в международном формате"
        )
        if not ok:
            return
        self.review = None
        self.consent.setChecked(False)
        self.window.profiles_page.test_status.setText("Подключение второго аккаунта и проверка истории…")
        self.dispatch(
            "test_connect",
            lambda session, _: session.begin_test_reset(account_id, target, phone.strip(), consented=True),
        )

    def _test_login(self, result):
        if isinstance(result, ResetResult):
            target = result.target
            if not any(
                self.target.itemData(i) is not None and self.target.itemData(i).id == target.id
                for i in range(self.target.count())
            ):
                self.target.addItem(target.title, target)
            self.window.profiles_page.test_reset_completed(result)
            return
        password = result.challenge == "password"
        text, ok = QInputDialog.getText(
            self.window,
            "Второй тестовый аккаунт",
            "Пароль двухэтапной проверки" if password else "Одноразовый код второго аккаунта",
            QLineEdit.EchoMode.Password,
        )
        if ok:
            self.dispatch(
                "test_password" if password else "test_code",
                lambda session, _: session.answer_test_login(text, password=password),
            )
        else:
            self.dispatch("test_cancel", lambda session, _: session.cancel_test_reset())

    def baseline(self):
        target = self.target.currentData()
        if self.package and target:
            path = self._snapshot_path(tr("Сохранить границу до импорта"))
            if path:
                root, protected = self.package.root, self._protected()
                self.dispatch(
                    "baseline",
                    lambda session, _: session.baseline(target, root=root, path=path, protected=protected),
                )

    def _protected(self):
        return tuple(Path(value) for value in (self.project, self.package_source) if value)

    def _snapshot_path(self, title):
        path, _ = QFileDialog.getSaveFileName(self.window, title, "", "Снимки (*.json)")
        if path:
            out = Path(path)
            return out if out.suffix else out.with_suffix(".json")

    def observe(self):
        target = self.target.currentData()
        if not self.package or not target:
            return
        methods = (tr("Файл границы до импорта"), tr("Ввести ID вручную"))
        method, ok = QInputDialog.getItem(
            self.window, tr("Диапазон истории"), tr("Нижняя граница для этого аккаунта"), methods, 0, False
        )
        if not ok:
            return
        baseline, after = None, 0
        if method == methods[0]:
            file, _ = QFileDialog.getOpenFileName(
                self.window, tr("Граница этого аккаунта"), "", tr("Снимки (*.json)")
            )
            if not file:
                return
            baseline = Path(file)
        else:
            after, ok = QInputDialog.getInt(
                self.window, tr("Диапазон истории"), tr("Последний ID до импорта"), 0, 0, 2**31 - 2
            )
            if not ok:
                return
        through, ok = QInputDialog.getInt(
            self.window, tr("Диапазон истории"), tr("Последний ID в диапазоне (0 — до конца)"), 0, 0, 2**31 - 2
        )
        if not ok:
            return
        if path := self._snapshot_path(tr("Сохранить снимок")):
            window = CaptureWindow(
                path,
                after,
                through or None,
                self.probe_media.isChecked(),
                self._protected(),
                baseline=baseline,
            )
            root = self.package.root
            self.dispatch("observe", lambda session, p: session.observe(root, target, window, p))

    def historical_observe(self):
        target = self.target.currentData()
        if not self.package or not target:
            return
        first, ok = QInputDialog.getText(
            self.window, tr("Ограниченный поиск"), tr("Начальная историческая дата UTC (ГГГГ-ММ-ДД)")
        )
        if not ok:
            return
        last, ok = QInputDialog.getText(
            self.window, tr("Ограниченный поиск"), tr("Конечная историческая дата UTC (ГГГГ-ММ-ДД)")
        )
        if not ok:
            return
        limit, ok = QInputDialog.getInt(
            self.window,
            tr("Ограниченный поиск"),
            tr("Сколько последних записей просмотреть на наличие отметки импорта"),
            500,
            1,
            20000,
        )
        if ok and (path := self._snapshot_path(tr("Сохранить снимок по датам"))):
            window = CaptureWindow(
                path,
                0,
                None,
                self.probe_media.isChecked(),
                self._protected(),
                historical_from=first,
                historical_through=last,
                max_scan=limit,
            )
            root = self.package.root
            self.dispatch("observe", lambda session, p: session.observe(root, target, window, p))

    def export_diagnostic(self):
        if self.operation_id:
            self.window.save_operation_diagnostic(self.operation_id)

    def cancel(self):
        if self.thread:
            self.thread.cancel()

    def count(self):
        if not self.package:
            self.counts.clear()
            self.updated.emit()
            return
        data = self.package.data
        source = data.get("source_records", len(data["records"]))
        observed = "—" if self.observed is None else number(self.observed)
        verified = "—" if self.verified is None else number(self.verified)
        start = (
            tr("Принят Telegram")
            if self.start_accepted
            else tr("Отправка или ожидание ответа")
            if self.start_sent
            else tr("Не начат")
        )
        text_status = (
            tr("не отправлялся · локальная блокировка")
            if self.blocked
            else tr("принят")
            if self.transcript_uploaded
            else tr("отправка начата, приём не подтверждён")
            if self.transcript_sent
            else tr("не отправлялся")
        )
        self.counts.setText(
            tr('Найдено {0} · Выбрано {1} · Проверено записей {2}\nВ пакете {3} · Пропущено {4}\nТекст истории {5} · Медиа принято {6} из {7}\nЗапуск: {8} · Записей в снимке {9} · Сверено в двух клиентах {10} · Ошибки {11}').format(number(source), number(data.get('selected_records', data['included'])), number(self.validated), number(data['included']), number(data['skipped']), text_status, number(self.uploaded), number(len(data['media'])), start, observed, verified, self.errors)
        )
        self.updated.emit()

    def file_transfer(self, detail):
        sent, size = detail["sent"], detail["size"]
        self.state.setText(
            tr('Передача вложения {0} из {1} · {2}\n{3} из {4} · {5} % · {6}/с').format(detail['index'], detail['total'], detail['name'], size_text(sent), size_text(size), sent * 100 // max(1, size), size_text(detail['speed']))
        )
        self.updated.emit()

    def progress(self, stage, done, total):
        self.state.setText(tr(STAGES.get(stage, "Подготовка данных")))
        if stage == "check_history":
            self.local_status.setText(tr("Пакет проверен   ·   Распознавание формата Telegram"))
        elif stage == "check_peer":
            self.local_status.setText(tr("Пакет проверен   ·   Формат распознан   ·   Проверка диалога"))
        if stage in {"verify", "copy"}:
            self.checked = max(self.checked, done)
        if stage == "validated":
            self.validated = done
        elif stage == "upload_transcript":
            self.transcript_sent = True
        elif stage == "transcript_uploaded":
            self.transcript_uploaded = True
        elif stage == "upload_item":
            self.state.setText(tr('Отправляется вложение {0} из {1}').format(number(done), number(total)))
        elif stage == "upload":
            self.uploaded = done
        elif stage == "start_pending":
            self.start_sent = True
        elif stage == "verifying":
            self.start_sent = self.start_accepted = True
        self.count()

    def _response(self, name, result):
        self.busy = False
        if self.closing:
            return
        if name in {"connect", "code", "password"}:
            self._login(result)
        elif name in {"test_connect", "test_code", "test_password"}:
            self._test_login(result)
        elif name == "test_cancel":
            self.window.profiles_page.test_status.setText(tr("Проверка остановлена."))
        elif name == "disconnect":
            self.account = self.review = None
            self.target.clear()
            self.dialog_preview.clear()
            self.dialog_summary.setText(tr("Подключите аккаунт для просмотра диалогов."))
            self.state.setText(tr("Аккаунт отключён."))
        elif name == "remember":
            self.use_saved.setEnabled(vault_path().is_file())
            self.use_saved_api.setChecked(api_vault_path().is_file())
            self.request_profile()
        elif name == "profile" and self.account:
            self.show_profile(result)
        elif name == "forward_names" and self.account:
            self.window.show_forward_names(result)
        elif name == "dialogs":
            self.target.clear()
            self.dialog_preview.clear()
            self.target.addItem(tr("Выберите целевой диалог"), None)
            for target in result:
                title = target.title
                self.target.addItem(title, target)
                self.dialog_preview.addItem(title, target)
            self.dialog_summary.setText(tr('Доступных для проверки диалогов {0}.').format(number(len(result))))
        elif name == "preflight":
            target = self.target.currentData()
            if result.test_reset_id != self.test_reset_id or not self._test_mode_selected():
                self.refresh()
                return
            if self.test_reset_id is not None and target and target.input_peer is None:
                self.target.setItemData(self.target.currentIndex(), result.target)
            self.review = result
            self.operation_id = result.check.operation_id
            limit = result.check.max_upload_bytes
            self.local_status.setText(tr("Пакет проверен   ·   Формат распознан   ·   Диалог разрешён"))
            self.details.setText(
                tr('Операция {0} · ').format(result.check.operation_id)
                + (tr('предел файла {0}').format(size_text(limit)) if limit else tr("лимит файла в ответе отсутствует"))
            )
        elif name == "import":
            self.operation_id = result["operation_id"]
            self.start_sent = self.start_accepted = True
            self.details.setText(
                tr('Операция {0}. Telegram принял запуск. Для результата прочитайте историю отдельно с обоих аккаунтов.').format(result['operation_id'])
            )
        elif name == "baseline":
            self.info(tr('Граница сохранена. Последний ID сообщения {0}.').format(result['last_id']))
        elif name == "observe":
            self.observed = result["messages"]
            self.details.setText(tr('Снимок сохранён · {0} записей\n{1}').format(number(result['messages']), result['path']))
        self.count()
        self.refresh()
        self.log(tr('Telegram · {0} · результат получен').format(tr(ACTION[name])))

    def _login(self, result):
        if result.challenge:
            password = result.challenge == "password"
            text, ok = QInputDialog.getText(
                self.window,
                "Вход Telegram",
                "Пароль двухэтапной проверки" if password else "Одноразовый код Telegram",
                QLineEdit.EchoMode.Password,
            )
            if not ok:
                self.disconnect_account()
            elif password:
                self.dispatch("password", lambda session, _: session.manager.submit_password(text))
            else:
                self.dispatch("code", lambda session, _: session.manager.submit_code(text.strip()))
        else:
            self.account = result.account
            if self.account:
                self.show_profile(AccountProfile())
            self.state.setText("Аккаунт подключён. Выберите диалог после загрузки списка.")
            save_session, save_api = self.remember.isChecked(), self.remember_api.isChecked()
            if save_session or save_api:
                self.dispatch("remember", lambda session, _: session.remember(session=save_session, api=save_api))
            else:
                self.request_profile()

    def _failure(self, name, error):
        self.busy = False
        if self.closing:
            return
        if name == "profile":
            self.log(tr("Telegram · Не удалось прочитать карточку аккаунта"))
            self.refresh()
            return
        if name in {"import", "preflight"}:
            self.review = None
            self.failed_video = None
        if name == "disconnect":
            self.account = self.review = None
            self.target.clear()
            self.dialog_preview.clear()
        if isinstance(error, RecoveryRequired):
            self.blocked = True
            self.recovery = error.review
            self.state.setText(tr("Обнаружена предыдущая операция импорта."))
            self.local_status.setText(
                tr("Локальная проверка остановила попытку до обращения к Telegram.")
                if name == "preflight"
                else tr("Локальная проверка остановила запуск до отправки пакета.")
            )
            self.details.setText(self.local_status.text())
            self.log(tr("Локальная проверка выявила предыдущую операцию."))
        elif isinstance(error, ImportFault):
            self.errors += 1
            self.operation_id = getattr(error, "operation_id", None) or self.operation_id
            details = error.details()
            state = tr(error.policy.message, fallback=tr("Ошибка"))
            if error.seconds:
                state += " " + tr("Ожидание {seconds} с.").format(seconds=error.seconds)
            action = tr(details["action"], fallback=tr("Проверьте этап и состояние операции в журнале."))
            self.state.setText(f"{details['status_code']} · {state}")
            item = getattr(error, "media_attempt", None)
            if item:
                if error.code == "MEDIA_TYPE_CHANGED" and item.get("source_ordinal") is not None:
                    self.failed_video = item
                self.state.setText(tr('Ошибка на вложении {0} из {1} · {2}').format(item['index'], item['total'], error.code))
                self.details.setText(
                    tr(
                        "{0} · Запись №{1} · ID сообщения {2}\nКласс {3} · MIME определён {4}, объявлен {5} · "
                        "{6} байт\n{7} Сохраните отчёт и изучите исходный файл перед подготовкой отдельного пакета."
                    ).format(item["name"], item["source_ordinal"], item["source_id"] or "—", item["input_media"],
                             item["detected_mime"] or "—", item["declared_mime"] or "—", number(item["size"]), action)
                )
            else:
                self.details.setText(f"{tr(STAGES.get(details['stage'], ACTION[name]))} · {action}")
            self.log(f"Telegram · {tr(ACTION[name])} · {error.code}")
        else:
            self.errors += int(not isinstance(error, Cancelled))
            self.state.setText(tr(str(error), fallback=tr("Подробности доступны в диагностическом отчёте.")))
            self.log(tr('Telegram · {0} · остановлено').format(tr(ACTION[name])))
        if name in {"connect", "code", "password", "remember", "dialogs", "disconnect"}:
            self.info(str(error))
        if name.startswith("test_"):
            self.window.profiles_page.test_status.setText(str(error))
        self.count()
        self.refresh()
