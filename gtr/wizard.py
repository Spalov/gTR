"""Guided import UI using the same package, transport and verification actions as the other tabs."""

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .conversion import load_bundle, prepare
from .core import verify
from .dashboard import label, number, panel
from .fixtures import FIXTURE_SIZES, prepare_fixture
from .integrity import check_local
from .media_engine import MEDIA_ISSUES, inspect_bundle
from .model import RecoveryError
from .verification import VERIFICATION_NAMES

STEPS = (
    "Выбор архива",
    "Проверка истории",
    "Выбор диалога",
    "Режим импорта",
    "Проверка вложений",
    "Подтверждение",
    "Передача в Telegram",
    "Проверка результата",
)


def button(text, action, *, primary=False):
    widget = QPushButton(text)
    widget.clicked.connect(action)
    if primary:
        widget.setObjectName("primary")
    return widget


def bar(*widgets):
    layout = QHBoxLayout()
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch()
    return layout


class ImportWizard(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window, self.telegram = window, window.telegram
        self.source, self.opened, self.checked = None, None, False
        self.reviewed, self.inventory, self.local_report = None, None, None
        self.destination_key = None
        layout = QVBoxLayout(self)
        self.title = label("", "section")
        self.step = QProgressBar()
        self.step.setRange(1, len(STEPS))
        self.step.setTextVisible(False)
        self.step.setAccessibleName("Шаг мастера импорта")
        layout.addWidget(self.title)
        layout.addWidget(self.step)
        self.pages = QStackedWidget()
        for build in (
            self._source,
            self._history,
            self._destination,
            self._mode,
            self._media,
            self._confirm,
            self._transfer,
            self._verification,
        ):
            scroll = QScrollArea()
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            card, group = panel()
            build(group)
            group.addStretch()
            scroll.setWidget(card)
            self.pages.addWidget(scroll)
        layout.addWidget(self.pages, 1)
        self.hint = label("", "muted")
        layout.addWidget(self.hint)
        self.back = button("Назад", lambda: self.go(-1))
        self.forward = button("Далее", lambda: self.go(1), primary=True)
        navigation = QHBoxLayout()
        navigation.addWidget(self.back)
        navigation.addStretch()
        navigation.addWidget(self.forward)
        layout.addLayout(navigation)
        self.telegram.updated.connect(self.refresh)
        self.refresh()

    def _source(self, area):
        self.source_label = label("", "secondary")
        area.addWidget(self.source_label)
        area.addLayout(
            bar(
                button("Открыть экспорт…", lambda: self.window.choose_archive(on_ready=self.project_ready)),
                button("Открыть папку…", lambda: self.window.choose_folder(on_ready=self.project_ready)),
                button("Открыть проект…", lambda: self.window.browse_project(on_ready=self.project_ready)),
            )
        )
        area.addLayout(
            bar(
                button(
                    "Открыть готовый пакет…", lambda: self.window.select_bundle(on_ready=self.package_ready)
                ),
                button("Синтетические данные", lambda: self.choose_source("fixture")),
            )
        )
        self.use_project = button("Выбрать открытый проект", self.project_ready)
        area.addWidget(self.use_project, alignment=Qt.AlignmentFlag.AlignLeft)

    def _history(self, area):
        self.history_label = label("", "secondary")
        area.addWidget(self.history_label)
        self.history_button = button("Проверить сохранённую историю", self.check_history, primary=True)
        area.addWidget(self.history_button, alignment=Qt.AlignmentFlag.AlignLeft)
        area.addWidget(
            label(
                "Сверяются страницы и JSON истории по SHA-256. Вложения проверяются отдельно, если они входят в пакет.",
                "muted",
            )
        )

    def _destination(self, area):
        self.account = label("", "secondary")
        area.addWidget(self.account)
        area.addLayout(
            bar(
                button("Управление аккаунтом", self.telegram.go_to_account),
                button("Загрузить диалоги", self.telegram.load_dialogs),
            )
        )
        self.target = QComboBox()
        self.target.setAccessibleName("Диалог для импорта")
        self.target.setModel(self.telegram.target.model())
        self.target.setPlaceholderText("Выберите диалог Telegram")
        self.target.currentIndexChanged.connect(self.telegram.target.setCurrentIndex)
        area.addWidget(self.target)
        area.addWidget(button("Продолжить импорт", self.window.show_import_profiles))
        self.offline = QCheckBox("Продолжить подготовку без подключения")
        self.offline.toggled.connect(self.refresh)
        area.addWidget(self.offline)

    def _mode(self, area):
        self.mode = QComboBox()
        self.mode.setAccessibleName("Режим импорта")
        for title, code in (
            ("Только текст", "text_only"),
            ("Полная история", "text_media"),
            ("Контрольный тест", "fixture"),
        ):
            self.mode.addItem(title, code)
        area.addWidget(self.mode)
        self.window_options = QWidget()
        form = QFormLayout(self.window_options)
        self.chat = QComboBox()
        self.chat.setModel(self.window.import_chat.model())
        self.chat.setAccessibleName("Диалог из локального архива")
        self.first, self.last = QSpinBox(), QSpinBox()
        self.first.setRange(1, 2**31 - 1)
        self.last.setRange(0, 2**31 - 1)
        self.last.setSpecialValueText("До конца")
        self.first.setAccessibleName("Первый исходный номер записи")
        self.last.setAccessibleName("Последний исходный номер записи")
        self.as_documents = QCheckBox("Передавать вложения как документы")
        form.addRow("Диалог из архива", self.chat)
        form.addRow("Первый номер записи", self.first)
        form.addRow("Последний номер записи", self.last)
        form.addRow(self.as_documents)
        area.addWidget(self.window_options)
        self.test_options = QWidget()
        test_form = QFormLayout(self.test_options)
        self.test_size, self.test_type = QComboBox(), QComboBox()
        self.test_size.setAccessibleName("Размер синтетического набора")
        for size in FIXTURE_SIZES:
            self.test_size.addItem(str(size), size)
        self.test_type.setAccessibleName("Состав синтетического набора")
        self.test_type.addItem("Только синтетический текст", "text")
        self.test_type.addItem("Текст и синтетическое изображение", "media")
        test_form.addRow("Синтетических записей", self.test_size)
        test_form.addRow("Состав", self.test_type)
        area.addWidget(self.test_options)
        self.mode_label = label("", "muted")
        area.addWidget(self.mode_label)
        for signal in (
            self.mode.currentIndexChanged,
            self.chat.currentIndexChanged,
            self.first.valueChanged,
            self.last.valueChanged,
            self.as_documents.toggled,
            self.test_size.currentIndexChanged,
            self.test_type.currentIndexChanged,
        ):
            signal.connect(self.invalidate_review)

    def _media(self, area):
        self.media_label = label("", "secondary")
        area.addWidget(self.media_label)
        self.build_button = button("Проверить и подготовить пакет…", self.build_package, primary=True)
        self.inventory_button = button("Сохранить диагностику медиа…", self.save_inventory)
        self.open_report = button("Открыть отчёт пакета", self.show_package_report)
        area.addWidget(self.build_button, alignment=Qt.AlignmentFlag.AlignLeft)
        area.addLayout(bar(self.inventory_button, self.open_report))

    def _confirm(self, area):
        self.confirm_label = label("", "secondary")
        area.addWidget(self.confirm_label)
        self.consent = QCheckBox("Все затрагиваемые участники согласились на этот пакет и выбранный диалог")
        self.consent.setAccessibleName("Явное согласие участников до передачи данных Telegram")
        self.consent.toggled.connect(self._consent_changed)
        area.addWidget(self.consent)
        self.preflight = button("Проверить пакет в Telegram", self.telegram.preflight, primary=True)
        area.addWidget(self.preflight, alignment=Qt.AlignmentFlag.AlignLeft)
        area.addWidget(self.telegram.make_recovery_panel())
        area.addLayout(bar(button("Сохранить исходную границу…", self.telegram.baseline)))
        area.addWidget(
            label(
                "Проверка передаёт первые строки пакета. До запуска сохраните отдельную границу на каждом тестовом аккаунте.",
                "muted",
            )
        )

    def _transfer(self, area):
        self.transfer_label = label("", "secondary")
        self.transfer_details = label("", "muted")
        area.addWidget(self.transfer_label)
        area.addWidget(self.transfer_details)
        self.launch = button("Открыть подтверждение и начать…", self.telegram.confirm, primary=True)
        self.cancel = button("Остановить", self.telegram.cancel)
        self.diagnostic = button("Диагностика операции…", self.telegram.export_diagnostic)
        area.addLayout(bar(self.launch, self.cancel, self.diagnostic))
        area.addWidget(self.telegram.make_recovery_panel())

    def _verification(self, area):
        self.result_label = label("", "secondary")
        area.addWidget(self.result_label)
        self.capture = button("Снимок истории…", self.telegram.observe)
        self.historical = button("Снимок по датам…", self.telegram.historical_observe)
        self.compare = button("Сравнить сохранённые снимки…", self.window.compare_observations, primary=True)
        area.addLayout(bar(self.capture, self.historical))
        area.addWidget(self.compare, alignment=Qt.AlignmentFlag.AlignLeft)
        area.addWidget(
            label(
                "Снимите историю отдельно с каждого аккаунта. После перезапуска Telegram проверьте даты, метку импорта, авторов, порядок и медиа в обоих клиентах.",
                "muted",
            )
        )

    def focus(self):
        self.window.navigate_to_import_wizard()
        self.refresh()

    def project_ready(self):
        if self.window.project:
            self.choose_source("project")

    def project_changed(self):
        if self.source == "project":
            self.checked = False
            self.history_label.setText("Открыт новый проект. Сверьте исходную историю.")
            self.invalidate_review()
            self.pages.setCurrentIndex(0)
        self.refresh()

    def package_ready(self, bundle):
        self.opened = bundle
        self.choose_source("package")

    def choose_source(self, kind):
        self.source = kind
        self.checked = kind == "fixture"
        self.history_label.setText(
            "Контрольный набор создаётся локально."
            if self.checked
            else "Сверьте исходную историю перед подготовкой."
        )
        self.invalidate_review()
        with QSignalBlocker(self.mode):
            mode = "fixture" if kind == "fixture" else "text_only"
            if kind == "package":
                mode = (
                    "fixture"
                    if self.opened.data.get("fixture")
                    else self.opened.data.get("media_mode", "text_media")
                )
                if mode == "document":
                    mode = "text_media"
            self.mode.setCurrentIndex(self.mode.findData(mode))
        self.pages.setCurrentIndex(0)
        self.focus()

    def _busy(self):
        return self.window.worker is not None or self.telegram.busy

    def invalidate_review(self, *_):
        self.reviewed, self.inventory, self.local_report = None, None, None
        with QSignalBlocker(self.consent):
            self.consent.setChecked(False)
        if hasattr(self.telegram, "consent"):
            self.telegram.consent.setChecked(False)
        self.refresh()

    def _consent_changed(self, value):
        self.telegram.consent.setChecked(value and self._package_active())
        self.refresh()

    def _package_active(self):
        return self.reviewed is not None and self.telegram.package is self.reviewed

    def check_history(self):
        if self.source == "project":
            project = self.window.project
            if project is None:
                return

            def job(progress):
                return verify(project, progress, history_only=True)
        elif self.source == "package" and self.opened:
            root = self.opened.root
            expected = self.opened.manifest_hash()

            def job(progress):
                if load_bundle(root, progress).manifest_hash() != expected:
                    raise RecoveryError("Пакет изменился. Откройте его заново.")
                return {"ok": 1}
        else:
            return

        def ready(result):
            self.checked = bool(result.get("ok")) and not any(key != "ok" for key in result)
            text = "Контрольные суммы совпали." if self.checked else "Есть расхождения в исходной истории."
            self.history_label.setText(text)
            self.focus()
            return text

        self.window.run_job("Сверка исходной истории", job, ready)

    def build_package(self):
        mode, project = self.mode.currentData(), self.window.project
        stored = self.opened if self.source == "package" else None
        if stored is None:
            if (
                mode == "fixture"
                and self.source == "project"
                and QMessageBox.question(
                    self,
                    "Контрольный тест",
                    "Создать отдельный синтетический пакет без записей из открытого архива?",
                    defaultButton=QMessageBox.StandardButton.No,
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            chosen = QFileDialog.getExistingDirectory(self, "Папка для нового пакета")
            if not chosen:
                return
            dest = Path(chosen) / ("gTR-beta-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        else:
            dest = stored.root
        chat, first, last = self.chat.currentData(), self.first.value(), self.last.value() or None
        scenario, size = self.test_type.currentData(), self.test_size.currentData()
        content = "document" if mode == "text_media" and self.as_documents.isChecked() else mode
        associated = project if self.source == "project" and mode != "fixture" else None

        def job(progress):
            if stored:
                bundle = load_bundle(dest, progress)
                if bundle.manifest_hash() != stored.manifest_hash():
                    raise RecoveryError("Пакет изменился. Откройте его заново.")
            elif mode == "fixture":
                bundle = prepare_fixture(dest, scenario, progress, size=size)
            else:
                bundle = prepare(
                    project,
                    chat,
                    dest,
                    "source",
                    progress,
                    media_mode=content,
                    first_record=first,
                    last_record=last,
                )
            inventory = inspect_bundle(bundle, progress, hash_verified=True)
            evidence = check_local(bundle, associated, progress)
            return bundle, inventory, evidence

        def ready(result):
            self.reviewed, self.inventory, self.local_report = result
            message = self.window.use_bundle(self.reviewed)
            self.focus()
            return message

        self.window.run_job("Подготовка и проверка пакета", job, ready)

    def save_inventory(self):
        if self.inventory and self.reviewed:
            self.window.save_media_inventory(self.inventory, (self.reviewed.root,))

    def show_package_report(self):
        if self.reviewed and (path := self.reviewed.root / "report.html").is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _ready_package(self):
        return bool(
            self._package_active()
            and self.reviewed.data["included"]
            and self.inventory is not None
            and not self.inventory["blocked"]
            and self.local_report["status"] != "differences"
        )

    def _can_advance(self, index):
        target = self.telegram.target.currentData()
        valid_range = self.last.value() == 0 or self.last.value() >= self.first.value()
        return (
            self.source is not None,
            self.checked,
            bool(target and self.telegram.account) or self.offline.isChecked(),
            bool(
                self.mode.currentData() == "fixture"
                or self.source == "package"
                or (self.chat.currentData() is not None and valid_range)
            ),
            self._ready_package(),
            self._ready_package() and self.telegram.review is not None and self.consent.isChecked(),
            bool(self.telegram.start_sent or self.telegram.errors),
            True,
        )[index]

    def go(self, direction):
        index = self.pages.currentIndex()
        if self._busy() or (direction > 0 and not self._can_advance(index)):
            return
        target, account = self.telegram.target.currentData(), self.telegram.account
        if (
            index == 2
            and direction > 0
            and self.source == "project"
            and target
            and account
            and not self.offline.isChecked()
        ):
            from .journal import Endpoint

            key = (account.id, Endpoint.telegram(account.id, target.id, target.kind).key)
            self.window.profiles_page.selected_sources[key] = self.window.project
            self.window.show_import_profiles(key)
            return
        if index == len(STEPS) - 1 and direction > 0:
            self.window.finish_import_wizard()
            return
        self.pages.setCurrentIndex(max(0, min(len(STEPS) - 1, index + direction)))
        self.refresh()

    def refresh(self, *_):
        if not hasattr(self, "forward"):
            return
        telegram, busy = self.telegram, self._busy()
        target = telegram.target.currentData()
        account = telegram.account
        key = (account.id, target.id) if account and target else None
        if key != self.destination_key or not telegram.consent.isChecked():
            with QSignalBlocker(self.consent):
                self.consent.setChecked(False)
        self.destination_key = key
        with QSignalBlocker(self.target):
            self.target.setCurrentIndex(telegram.target.currentIndex())
        source = self.source
        project = self.window.project
        self.source_label.setText(
            f"Проект · {project.name}"
            if source == "project" and project
            else f"Готовый пакет · {self.opened.root.name}"
            if source == "package" and self.opened
            else "Контрольный набор с вымышленными участниками"
            if source == "fixture"
            else "Выберите источник для нового импорта."
        )
        self.use_project.setEnabled(project is not None and not busy)
        self.history_button.setEnabled(source in {"project", "package"} and not busy)
        self.account.setText(telegram.account_line.text())
        self.mode.setEnabled(source == "project" and not busy)
        self.window_options.setVisible(source == "project" and self.mode.currentData() != "fixture")
        self.as_documents.setVisible(self.mode.currentData() == "text_media")
        self.test_options.setVisible(source != "package" and self.mode.currentData() == "fixture")
        self.mode_label.setText(
            "Синтетические записи сохраняются в новом пакете."
            if self.mode.currentData() == "fixture"
            else "Порядок, даты и смещения берутся из источника. Верхняя граница включается."
        )
        self.build_button.setText(
            "Проверить открытый пакет" if source == "package" else "Проверить и подготовить пакет…"
        )
        self.build_button.setEnabled(not busy and self._can_advance(3))
        if self.inventory and self.reviewed:
            data = self.reviewed.data
            parts = [
                f"Найдено {number(data.get('source_records', len(data['records'])))} · Выбрано {number(data.get('selected_records', data['included']))} · В пакете {number(data['included'])}",
                f"Вложений {number(self.inventory['count'])} · Блокирующих ошибок {number(self.inventory['blocked'])} · Пропущено записей {number(data['skipped'])}",
            ]
            parts.extend(
                f"{MEDIA_ISSUES.get(code, code)} — {count}"
                for code, count in self.inventory["issues"].items()
            )
            if self.local_report["status"] == "differences":
                parts.append("Есть расхождения с исходным проектом.")
            self.media_label.setText("\n".join(parts))
        else:
            self.media_label.setText("Проверьте формат, размеры и контрольные суммы перед сетевой проверкой.")
        self.inventory_button.setEnabled(self.inventory is not None and not busy)
        self.open_report.setEnabled(self.reviewed is not None and not busy)
        self.confirm_label.setText(
            f"{account.name} · {target.title}\n{telegram.local_status.text()}"
            if key
            else "Подключите аккаунт и выберите целевой диалог на шаге 3."
        )
        self.consent.setEnabled(bool(key and self._ready_package() and not busy))
        self.preflight.setEnabled(self._ready_package() and telegram.preflight_button.isEnabled())
        self.transfer_label.setText(telegram.counts.text())
        self.transfer_details.setText(f"{telegram.state.text()}\n{telegram.details.text()}".strip())
        self.launch.setEnabled(self._ready_package() and telegram.start_button.isEnabled())
        self.cancel.setEnabled(telegram.busy)
        self.diagnostic.setEnabled(telegram.diagnostic_button.isEnabled())
        self.capture.setEnabled(self._package_active() and telegram.observe_button.isEnabled())
        self.historical.setEnabled(self._package_active() and telegram.historical_button.isEnabled())
        self.compare.setEnabled(self._package_active() and not busy)
        result = self.window.comparison
        self.result_label.setText(
            VERIFICATION_NAMES.get(result.get("verification_status"), VERIFICATION_NAMES["INCONCLUSIVE"])
            if result
            else "Ожидаются независимые снимки."
        )
        index = self.pages.currentIndex()
        self.title.setText(f"{index + 1} из {len(STEPS)} · {STEPS[index]}")
        self.step.setValue(index + 1)
        self.pages.setEnabled(not busy or index == 6)
        self.back.setEnabled(index > 0 and not busy)
        self.forward.setText("Закрыть мастер" if index == len(STEPS) - 1 else "Далее")
        allowed = self._can_advance(index)
        self.forward.setEnabled(allowed and not busy)
        self.hint.setText(
            "Выполняется проверка или передача."
            if busy
            else "Проверьте предыдущую операцию или подготовьте доступные новые записи."
            if telegram.recovery is not None and index in (5, 6)
            else ""
            if allowed
            else (
                "Выберите источник.",
                "Завершите сверку.",
                "Выберите диалог или локальную подготовку.",
                "Выберите состав пакета.",
                "Подготовьте пакет и устраните блокирующие ошибки.",
                "Подтвердите согласие и завершите проверку Telegram.",
                "Дождитесь ответа о запуске.",
                "",
            )[index]
        )
