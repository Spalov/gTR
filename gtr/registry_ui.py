"""Offline import profiles, incremental preview and operation history."""

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHeaderView,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .account import Account, Target
from .conversion import prepare
from .core import catalogue, properties
from .dashboard import label
from .files import local, publish_text
from .incremental_import import (
    STATUS_NAMES,
    VERIFY_PREVIOUS_OPERATIONS,
    history,
    local_preview,
    recover_package,
    remember_profile,
    save_verification_preference,
)
from .integrity import check_local
from .journal import PHASE_NAMES, Endpoint, Journal
from .model import RecoveryError
from .owner import app_home
from .wizard import bar, button


def _show(window, title, text):
    dialog = QDialog(window)
    dialog.setWindowTitle(title)
    layout = QVBoxLayout(dialog)
    view = QPlainTextEdit()
    view.setReadOnly(True)
    view.setPlainText(text)
    layout.addWidget(view)
    close = QDialogButtonBox(dialog)
    close.addButton("Закрыть", QDialogButtonBox.ButtonRole.RejectRole)
    close.rejected.connect(dialog.reject)
    layout.addWidget(close)
    dialog.resize(760, 420)
    dialog.exec()


class ProfilesPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.preview = self.ready_key = None
        self.profile_identity = None
        self.selected_sources = {}
        self.profiles = []
        self.timer = QTimer(self, singleShot=True, interval=180)
        self.timer.timeout.connect(self.analyze)
        layout = QVBoxLayout(self)
        self.choice, self.chat, self.mode = QComboBox(), QComboBox(), QComboBox()
        self.chat.setModel(window.import_chat.model())
        self.mode.addItem("Текст и медиа", "text_media")
        self.mode.addItem("Только текст", "text_only")
        self.mode.addItem("Вложения как документы", "document")
        self.archive = label("Откройте архив или сохранённый проект.", "secondary")
        self.summary = label("", "section")
        self.detail = label("", "secondary")
        self.first, self.last = QSpinBox(), QSpinBox()
        self.first.setRange(1, 2**31 - 1)
        self.last.setRange(0, 2**31 - 1)
        self.last.setSpecialValueText("До конца")
        self.options = QWidget()
        self.verify_previous = QCheckBox("Проверять предыдущие операции")
        self.verify_previous.setChecked(True)
        self.verify_previous.setEnabled(False)
        self.verify_previous.setToolTip(
            "Снятый флажок позволяет продолжить с однозначно новыми записями, "
            "если результат прежнего запуска неизвестен. Совпадения исключаются автоматически."
        )
        self.test_mode = QCheckBox("Режим тестирования")
        self.test_mode.setEnabled(False)
        self.test_mode.setToolTip("Повторные импорты между двумя собственными тестовыми аккаунтами.")
        self.reset_tests = button("Сбросить реестр тестовых импортов", self.reset_test_registry)
        self.reset_tests.hide()
        self.test_status = label("", "muted")
        self.test_status.hide()
        form = QFormLayout(self.options)
        form.addRow("Диалог из архива", self.chat)
        form.addRow("Включить в пакет", self.mode)
        form.addRow("Первая запись", self.first)
        form.addRow("Последняя запись", self.last)
        form.addRow(self.verify_previous)
        form.addRow(self.test_mode)
        form.addRow(self.reset_tests)
        form.addRow(self.test_status)
        form = QFormLayout()
        form.addRow("Аккаунт и целевой диалог", self.choice)
        layout.addLayout(form)
        layout.addWidget(self.detail)
        layout.addWidget(self.archive)
        layout.addLayout(
            bar(
                button("Открыть архив…", lambda: window.choose_archive(on_ready=self.source_ready)),
                button("Открыть папку экспорта…", lambda: window.choose_folder(on_ready=self.source_ready)),
                button("Открыть проект…", lambda: window.browse_project(on_ready=self.source_ready)),
            )
        )
        layout.addWidget(self.options)
        self.options.hide()
        layout.addWidget(self.summary)
        self.continue_button = button("Продолжить импорт", self.build, primary=True)
        self.continue_button.setEnabled(False)
        self.matches = button("Проверить совпадения", self.show_matches)
        layout.addLayout(bar(self.continue_button, self.matches))
        layout.addLayout(
            bar(
                button("Просмотреть историю операций", window.show_import_history),
                button("Изменить параметры", lambda: self.options.setVisible(not self.options.isVisible())),
                button("Отмена", lambda: window.navigate_to(0)),
            )
        )
        layout.addWidget(
            button("Выбрать диалог в Telegram", lambda: window.navigate_to(11)),
            alignment=Qt.AlignmentFlag.AlignLeft,
        )
        layout.addStretch()
        self.choice.currentIndexChanged.connect(self.profile_changed)
        self.verify_previous.toggled.connect(self.change_verification_preference)
        self.test_mode.toggled.connect(self.change_test_mode)
        window.telegram.updated.connect(self.refresh_test_reset)
        for signal in (
            self.chat.currentIndexChanged,
            self.mode.currentIndexChanged,
            self.first.valueChanged,
            self.last.valueChanged,
        ):
            signal.connect(self.changed)

    def source_ready(self):
        self.ready_key = None
        p = self.choice.currentData()
        if p and self.window.project:
            self.selected_sources[p["account"], p["peer"]] = self.window.project
        self.window.show_import_profiles()

    def reload(self, preferred=None):
        try:
            self.profiles, _ = history(Journal(app_home() / "imports.sqlite", ""))
        except (RecoveryError, OSError, sqlite3.Error):
            self.summary.setText("Не удалось открыть локальный реестр.")
            return
        active = self.window.telegram
        has_saved_profiles = bool(self.profiles)
        profile = self.choice.currentData()
        previous = preferred or ((profile["account"], profile["peer"]) if profile else None)
        if active.account:
            for i in range(active.target.count()):
                target = active.target.itemData(i)
                if not target:
                    continue
                endpoint = Endpoint.telegram(active.account.id, target.id, target.kind)
                key = (active.account.id, endpoint.key)
                if target == active.target.currentData() and preferred is None:
                    previous = key
                if not any((p["account"], p["peer"]) == key for p in self.profiles):
                    self.profiles.append(
                        {
                            "account": key[0],
                            "peer": key[1],
                            "target_id": target.id,
                            "kind": target.kind,
                            "title": f"{active.account.name} ↔ {target.title}",
                            "project": "",
                            "archive": "",
                            "mode": "text_media",
                            "settings": "{}",
                            "operations": 0,
                            "confirmed": 0,
                            "pending": 0,
                            "last_success": "",
                            "last_verified": "",
                        }
                    )
        with QSignalBlocker(self.choice):
            self.choice.clear()
            prompt = previous is None and (not has_saved_profiles or len(self.profiles) > 1)
            if prompt:
                self.choice.addItem("Выберите профиль или целевой диалог", None)
            for i, p in enumerate(self.profiles, int(prompt)):
                self.choice.addItem(p["title"], p)
                if (p["account"], p["peer"]) == previous:
                    self.choice.setCurrentIndex(i)
        self.profile_changed()

    def profile_changed(self):
        p = self.choice.currentData()
        settings = json.loads(p["settings"]) if p else {}
        self.verify_previous.setEnabled(bool(p))
        with QSignalBlocker(self.verify_previous):
            self.verify_previous.setChecked(settings.get(VERIFY_PREVIOUS_OPERATIONS) is not False)
        if p:
            self.detail.setText(
                f"Аккаунт {p['account']} · Диалог {p['target_id']} · Операций {p['operations']} · "
                f"Подтверждено {p['confirmed']} · Ожидают сверки {p['pending']}\n"
                f"Последний принятый запуск {p['last_success'] or '—'} · Последняя сверка {p['last_verified'] or '—'}"
            )
            with QSignalBlocker(self.mode):
                self.mode.setCurrentIndex(max(0, self.mode.findData(p["mode"])))
            path = self.selected_sources.get((p["account"], p["peer"])) or (
                Path(p["project"]) if p["project"] else None
            )
            if path and path != self.window.project and (path / "history.sqlite").is_file():
                self.window.bind_project(path)
            if self.window.project and str(self.window.project) == p["project"] and "chat" in settings:
                index = self.chat.findData(settings["chat"])
                if index >= 0:
                    self.chat.setCurrentIndex(index)
        else:
            self.detail.setText("Для первого профиля подключите свой аккаунт и выберите диалог Telegram.")
        identity = (p["account"], p["peer"]) if p else None
        if self.profile_identity != identity:
            self.profile_identity = identity
            self.test_mode.setChecked(False)
        self.test_mode.setEnabled(bool(p and p["kind"] == "pm" and p["account"] != p["target_id"]))
        self.refresh_test_reset()
        self.changed()

    def change_verification_preference(self, enabled):
        profile = self.choice.currentData()
        if not profile:
            return
        try:
            endpoint = Endpoint.telegram(profile["account"], profile["target_id"], profile["kind"])
            journal = Journal(app_home() / "imports.sqlite", endpoint)
            save_verification_preference(journal, profile, enabled)
        except (RecoveryError, OSError, sqlite3.Error):
            with QSignalBlocker(self.verify_previous):
                self.verify_previous.setChecked(not enabled)
            self.window.info("Не удалось сохранить настройку проверки для выбранного профиля.")
            return
        self.changed()

    def change_test_mode(self, enabled):
        if not enabled:
            self.test_status.clear()
            self.window.telegram.leave_test_mode()
        self.refresh_test_reset()
        self.changed()

    def refresh_test_reset(self):
        telegram = self.window.telegram
        profile = self.choice.currentData()
        self.reset_tests.setVisible(self.test_mode.isChecked())
        self.test_status.setVisible(self.test_mode.isChecked())
        self.reset_tests.setEnabled(
            self.test_mode.isChecked()
            and profile is not None
            and telegram.account is not None
            and telegram.account.id == profile["account"]
            and not telegram.busy
            and self.window.worker is None
        )

    def reset_test_registry(self):
        profile = self.choice.currentData()
        if self.reset_tests.isEnabled() and profile:
            title = profile["title"].split(" ↔ ")[-1]
            self.window.telegram.begin_test_reset(
                profile["account"], Target(profile["target_id"], title, "pm", None)
            )

    def test_reset_completed(self, result):
        identity = result.account_id, Endpoint.telegram(result.account_id, result.target.id, "pm").key
        if self.profile_identity == identity and self.test_mode.isChecked():
            self.test_status.setText("История пуста с обеих сторон. Новый тестовый цикл открыт.")
            with QSignalBlocker(self.first), QSignalBlocker(self.last):
                self.first.setValue(1)
                self.last.setValue(0)
            self.changed()

    def key(self):
        p, project = self.choice.currentData(), self.window.project
        if not p or not project or self.chat.currentData() is None:
            return None
        return (
            p["account"],
            p["peer"],
            project,
            self.chat.currentData(),
            self.mode.currentData(),
            self.first.value(),
            self.last.value() or None,
            self.verify_previous.isChecked(),
            self.test_mode.isChecked(),
        )

    def changed(self, *_):
        self.archive.setText(str(self.window.project or "Выберите архив или проект."))
        self.preview = None
        self.ready_key = None
        self.continue_button.setEnabled(False)
        self.matches.setEnabled(False)
        self.summary.setText(
            "Поиск ранее импортированных сообщений" if self.key() else "Выберите профиль и исходную историю."
        )
        if self.isVisible() and self.key():
            self.timer.start()

    def analyze(self):
        key = self.key()
        if not self.isVisible() or not key:
            return
        if self.window.worker or self.window.telegram.busy:
            self.timer.start()
            return
        account, _, project, chat, mode, first, last, verify_previous, test_mode = key
        p = self.choice.currentData()

        def job(progress):
            path = app_home() / "imports.sqlite"
            endpoint = Endpoint.telegram(account, p["target_id"], p["kind"])
            journal = Journal(path, endpoint)
            test_reset_id = journal.latest_test_reset() if test_mode else None
            if test_mode:
                journal = Journal(path, endpoint, test_reset_id=test_reset_id)
            bundle, review, archive = local_preview(journal, project, chat, mode, first, last, progress)
            remember_profile(
                journal,
                Account(account, p["title"].split(" ↔ ", 1)[0]),
                Target(p["target_id"], p["title"].split(" ↔ ")[-1], p["kind"], None),
                project,
                archive,
                mode,
                {"chat": chat, "as_document": mode == "document"},
            )
            return bundle, review, archive, test_reset_id

        def ready(result):
            self.window.navigate_to(12)
            if self.key() != key:
                self.changed()
                return "Параметры изменились. Выполняется новый поиск."
            self.preview = result
            self.ready_key = key
            data, review, archive, test_reset_id = result
            self.archive.setText(str(archive))
            excluded_label = (
                "Требуют дополнительной проверки" if verify_previous else "Исключено для защиты от повторов"
            )
            self.summary.setText(
                ("Тестовый режим · Сброс подтверждён с обеих сторон.\n" if test_reset_id else "")
                + ("Обнаружена предыдущая история импорта.\n" if review.conflicts else "")
                + f"Записей в архиве {data.data['source_records']} · В выбранном режиме доступно {review.selected}\n"
                f"Уже восстановлено и подтверждено {review.confirmed} · Новых к подготовке {review.eligible_count}\n"
                f"{excluded_label} {review.unknown}"
            )
            self.continue_button.setEnabled(bool(review.eligible))
            self.matches.setEnabled(True)
            return self.summary.text()

        self.window.run_job("Поиск ранее импортированных сообщений", job, ready)

    def show_matches(self):
        if self.preview:
            review = self.preview[1]
            _show(
                self.window,
                "Совпадения по исходным записям",
                "\n".join(f"{n} · {STATUS_NAMES[v]}" for n, v in review.decisions.items()),
            )

    def build(self):
        if not self.preview or self.key() != self.ready_key:
            return
        key = self.ready_key
        account, peer, project, chat, mode, first, last, _, _ = key
        expected = self.preview[1].eligible
        test_reset_id = self.preview[3]
        p = self.choice.currentData()

        def job(progress):
            journal = Journal(
                app_home() / "imports.sqlite",
                Endpoint.telegram(account, p["target_id"], p["kind"]),
                test_reset_id=test_reset_id,
            )
            _, review, _ = local_preview(journal, project, chat, mode, first, last, progress)
            if not expected or review.eligible != expected:
                raise RecoveryError("История импорта изменилась. Откройте профиль для повторной сверки.")
            dest = app_home() / "packages" / ("gTR-new-" + uuid4().hex)
            package = prepare(
                project,
                chat,
                dest,
                "source",
                progress,
                media_mode=mode,
                record_ranges=expected,
                test_reset_id=test_reset_id,
            )
            if (
                check_local(package, project, progress)["status"] != "verified"
                or journal.review_bundle(package).conflicts
            ):
                raise RecoveryError("Выборка изменилась. Повторите проверку профиля.")
            return package

        def ready(bundle):
            if self.key() != key:
                return "Параметры профиля изменились. Подготовьте пакет заново."
            self.window.use_bundle(bundle)
            panel = self.window.telegram
            for i in range(panel.target.count()):
                target = panel.target.itemData(i)
                if (
                    target
                    and panel.account
                    and panel.account.id == account
                    and Endpoint.telegram(account, target.id, target.kind).key == peer
                ):
                    panel.target.setCurrentIndex(i)
                    break
            else:
                if test_reset_id is not None and panel.account and panel.account.id == account:
                    title = p["title"].split(" ↔ ")[-1]
                    panel.target.addItem(title, Target(p["target_id"], title, "pm", None))
                    panel.target.setCurrentIndex(panel.target.count() - 1)
            panel.bound_profile = account, peer
            panel.test_reset_id = test_reset_id
            self.window.import_tabs.setCurrentWidget(panel.server_page)
            panel.state.setText(
                (
                    "Тестовый импорт. Подготовлено записей"
                    if test_reset_id is not None
                    else "Подготовлено новых записей"
                )
                + f" {bundle.data['included']}. Подтвердите аккаунт и диалог перед отправкой."
            )
            return panel.state.text()

        self.window.run_job("Подготовка новых сообщений", job, ready)


class HistoryPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        self.filters = [QLineEdit() for _ in range(3)]
        for widget, title in zip(self.filters, ("Аккаунт", "Диалог", "Дата, ГГГГ-ММ-ДД")):
            widget.setPlaceholderText(title)
            widget.textChanged.connect(self.filter)
        self.status = QComboBox()
        self.status.addItem("Все состояния", "")
        for value, title in PHASE_NAMES.items():
            self.status.addItem(title, value)
        self.status.currentIndexChanged.connect(self.filter)
        layout.addLayout(bar(*self.filters, self.status))
        self.summary = label("", "secondary")
        layout.addWidget(self.summary)
        headers = (
            "Дата",
            "Аккаунт",
            "Диалог",
            "Источник",
            "Состояние",
            "Подтверждено",
            "Ждут сверки",
            "Вложений принято",
            "Последняя сверка",
        )
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)
        layout.addLayout(
            bar(
                button("Продолжить импорт", lambda: self.action("continue"), primary=True),
                button("Проверить результат", lambda: self.action("verify")),
                button("Открыть отчёт", lambda: self.action("report")),
            )
        )
        layout.addLayout(
            bar(
                button("Просмотреть записи", lambda: self.action("records")),
                button("Экспортировать диагностику", lambda: self.action("export")),
            )
        )
        self.rows = self.visible_rows = []
        self.names = {}

    def reload(self):
        try:
            profiles, self.rows = history(Journal(app_home() / "imports.sqlite", ""))
            self.names = {(p["account"], p["peer"]): p["title"] for p in profiles}
            self.filter()
        except (sqlite3.Error, OSError, RecoveryError):
            self.summary.setText("Не удалось открыть историю операций.")

    def filter(self, *_):
        account, peer, day = [w.text().strip().casefold() for w in self.filters]

        def title(row):
            return self.names.get((row["account"], row["peer"]), row["peer"])

        self.visible_rows = [
            r
            for r in self.rows
            if account in str(r["account"])
            and peer in (title(r) + " " + r["peer"]).casefold()
            and day in r["created"][:10]
            and (not self.status.currentData() or r["phase"] == self.status.currentData())
        ]
        self.table.setRowCount(len(self.visible_rows))
        for i, r in enumerate(self.visible_rows):
            values = (
                r["created"],
                r["account"] if r["account"] is not None else "Неизвестен",
                title(r),
                Path(r["archive"] or r["project"] or "").name or r["archive_chat"] or "—",
                PHASE_NAMES.get(r["phase"], "Ожидает проверки"),
                r["confirmed_count"],
                r["pending"],
                f"{r['media_done']} / {r['media']}",
                r["checked_at"] or "—",
            )
            for j, value in enumerate(values):
                self.table.setItem(i, j, QTableWidgetItem(str(value)))
        self.summary.setText(
            f"Операций {len(self.visible_rows)} · Подтверждено записей {sum(r['confirmed_count'] for r in self.visible_rows)} · "
            f"Ждут сверки {sum(r['pending'] for r in self.visible_rows)} · Ошибок {sum(r['phase'] == 'FAILED' for r in self.visible_rows)} · "
            f"Состояние неизвестно {sum(r['status'] == 'UNKNOWN_SERVER_STATE' for r in self.visible_rows)}"
        )

    def action(self, action):
        i = self.table.currentRow()
        if i < 0:
            return self.window.info("Выберите операцию в истории.")
        row = self.visible_rows[i]
        journal = Journal(app_home() / "imports.sqlite", Endpoint(row["peer"], row["account"]))
        if action == "continue":
            return self.window.show_import_profiles((row["account"], row["peer"]))
        if action == "verify":

            def ready(bundle):
                self.window.navigate_to(5)
                return self.window.open_recovery_verification(bundle)

            return self.window.run_job(
                "Открытие сохранённого пакета", lambda p: recover_package(journal, row["id"], p), ready
            )
        if action == "records":
            with journal.connect() as db:
                records = db.execute(
                    "SELECT ordinal,author,moment,status,verified_at,last_check FROM import_record_status WHERE operation=? ORDER BY ordinal",
                    (row["id"],),
                ).fetchall()
            return _show(
                self.window,
                "Исходные записи операции",
                "\n".join(
                    f"{r[0]} · {r[1]} · {r[2]} · {STATUS_NAMES[r[3]]} · {r[4] or r[5] or '—'}"
                    for r in records
                )
                or "В старом журнале состав записей отсутствует.",
            )
        if action == "report":
            return _show(
                self.window,
                "Отчёт об операции",
                ("Режим тестирования\n" if row["test_reset_id"] is not None else "")
                + f"{PHASE_NAMES.get(row['phase'], 'Ожидает проверки')}\n"
                f"Записей в пакете {row['messages']}\nПодтверждено {row['confirmed_count']}\nОжидают проверки {row['pending']}\n"
                f"Вложений принято {row['media_done']} из {row['media']}\nПоследняя сверка {row['checked_at'] or '—'}",
            )
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить обезличенный отчёт", "gTR-диагностика.json", "Отчёт (*.json)"
        )
        if path:
            try:
                dest = local(Path(path))
                protected = [journal.path.parent, *self.window.telegram._protected()]
                if self.window.project:
                    with catalogue(self.window.project) as db:
                        protected.extend((self.window.project, Path(properties(db)["original"])))
                if any(dest.is_relative_to(local(Path(p))) for p in protected if p):
                    raise RecoveryError("Выберите папку вне исходных данных.")
                publish_text(
                    dest, json.dumps(journal.diagnostic_report(row["id"]), ensure_ascii=False, indent=2)
                )
            except (OSError, RecoveryError):
                self.window.info("Сохраните отчёт в новом файле вне исходных данных.")
