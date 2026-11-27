"""Local review and preview for attachments requiring an explicit decision."""

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .files import local, safe_parts
from .i18n import tr
from .media import KIND_NAMES
from .media_preview import FFmpegThumbnail, preview
from .model import RecoveryError

ACTION = {
    "compatible": "Создать совместимую копию",
    "original": "Использовать оригинал",
    "document": "Отправить как документ",
    "image": "Создать совместимое изображение PNG",
    "local": "Оставить только локально",
    "remux": "Сменить контейнер без перекодирования",
    "fidelity": "Создать копию с максимальной точностью",
    "compact": "Создать уменьшенную копию",
    "skip": "Пропустить вложение",
}
REASON = {
    "unreadable": "Файл недоступен или повреждён",
    "probe_failed": "Не удалось прочитать метаданные",
    "ffmpeg_unavailable": "Не найден FFmpeg",
    "ffprobe_unavailable": "Не найден FFprobe",
    "transcode_required": "Формат видео не подходит для передачи",
    "unknown_container": "Формат файла не удалось определить",
    "corrupt_container": "Повреждён контейнер видео",
    "missing_metadata": "Не подтверждены длительность или размеры видео",
    "no_video": "Не найдена видеодорожка",
    "missing": "Исходный файл отсутствует",
    "empty": "Исходный файл пуст",
    "changed": "Исходный файл изменился после анализа",
    "hash_mismatch": "Контрольная сумма оригинала не совпала",
    "size_mismatch": "Размер оригинала не совпал",
    "decode_failed": "Не удалось декодировать видео",
    "conversion_failed": "Не удалось создать совместимую копию",
    "disk_space": "Недостаточно места для совместимой копии",
    "multiple_attachments": "Видео входит в группу вложений",
    "sticker_legacy_document": "Предыдущая версия подготовила стикер как документ",
    "sticker_unconfirmed": "Содержимое файла не подтверждает допустимый формат стикера",
    "webp_photo": "Для отправки WebP как изображения нужна копия в поддерживаемом формате",
    "attachment_format": "Содержимое файла не соответствует подготовленному типу",
    "attachment_unconfirmed": "Не удалось подтвердить тип или метаданные вложения",
    "audio_unconfirmed": "Не удалось подтвердить тип или длительность аудио",
    "audio_unreadable": "Аудиофайл недоступен или повреждён",
    "image_copy_failed": "Не удалось создать PNG с сохранением изображения",
    "MEDIA_INVALID": "Telegram отклонил подготовленный тип вложения",
    "MEDIA_TYPE_CHANGED": "Telegram вернул тип, отличный от подготовленного",
}
MEDIA_TYPES = {
    **KIND_NAMES, "webp_sticker": "Статический стикер WebP", "tgs_sticker": "Анимированный стикер TGS",
    "webm_sticker": "Видеостикер WebM", "webp_image": "Изображение WebP",
    "custom_emoji": "Пользовательский эмодзи", "unsupported": "Неподдерживаемое вложение",
}
QUEUE_STAGES = {
    "analysis": "Анализ медиа",
    "waiting": "Ожидание решения пользователя",
    "convert": "Подготовка совместимых копий",
    "verify": "Проверка подготовленных файлов",
    "continue": "Продолжение подготовки",
    "ready": "Пакет готов",
}
TOOL_HELP = (
    "Для проверки и преобразования видео установите FFmpeg с FFprobe. Добавьте папку bin в PATH "
    "или укажите ffmpeg.exe и ffprobe.exe в настройках gTR."
)


def media_size(n):
    for unit in ("байт", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            value = f"{n:.0f}" if unit == "байт" else f"{n:.1f}".replace(".", ",")
            return f"{value} {unit}"
        n /= 1024


class QueueDisplay(QLabel):
    def __init__(self):
        super().__init__()
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.hide()

    def update_status(self, status):
        phase = tr(QUEUE_STAGES.get(status["phase"], QUEUE_STAGES["analysis"]))
        counts = tr("Готово {done} из {total} · копий {converted} · из кеша {reused} · ошибок {errors}").format(
            **status
        )
        self.setText(f"{phase}\n{counts}")
        progress = []
        if status.get("fraction") is not None:
            progress.append(f"{status['fraction']:.0%}")
        if status.get("speed"):
            speed = status["speed"]
            progress.append(f"{tr('Скорость')} {speed:g}×" if isinstance(speed, (int, float)) else str(speed))
        if status.get("eta") is not None:
            progress.append(tr("Осталось примерно {seconds} с").format(seconds=round(status["eta"])))
        if current := status.get("current"):
            self.setText(self.text() + "\n" + str(current))
        if progress:
            self.setText(self.text() + " · " + " · ".join(progress))
        self.show()


class PreviewWorker(QThread):
    picture = Signal(object)

    def __init__(self, project, entry, ffmpeg, parent):
        super().__init__(parent)
        self.project, self.entry, self.ffmpeg = project, entry, ffmpeg

    def run(self):
        try:
            path = local(self.project / "source" / "/".join(safe_parts(self.entry["source"])))
            provider = FFmpegThumbnail(self.ffmpeg) if self.ffmpeg else None
            _, image = preview(path, self.entry.get("sha256"), provider=provider)
            self.picture.emit(image)
        except (OSError, RecoveryError, ValueError):
            self.picture.emit(None)


class CompatibilityDialog(QDialog):
    def __init__(self, parent, entries, project, tools, *, initial=None):
        super().__init__(parent)
        self.entries, self.project, self.tools = entries, project, tools
        self.preview_job = None
        self.choices = []
        self.setWindowTitle(tr("Совместимость медиафайлов"))
        self.resize(1030, 590)
        layout = QVBoxLayout(self)
        intro = QLabel(tr("Выберите обработку проблемных вложений. Затем подготовка продолжится автоматически."))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(len(entries), 7)
        self.table.setHorizontalHeaderLabels([tr(x) for x in (
            "Оригинальное имя", "Исходный тип", "Подготовленный тип", "Ответ Telegram",
            "Причина", "Рекомендация", "Решение",
        )])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        for row, entry in enumerate(entries):
            self.add_entry(row, entry)
        restored = False
        for entry, choice in zip(entries, self.choices, strict=True):
            if (initial and (action := initial.get(entry["key"])) and (index := choice.findData(action)) >= 0):
                choice.blockSignals(True)
                choice.setCurrentIndex(index)
                choice.blockSignals(False)
                restored = True
        if restored:
            reminder = QLabel(tr("Сохранённые решения подставлены. Проверьте их и подтвердите продолжение."))
            reminder.setWordWrap(True)
            layout.addWidget(reminder)
        if (not tools.ffprobe or not tools.ffmpeg) and any(
            entry.get("source_kind") in {"video", "round_video", "webm_sticker"} for entry in entries
        ):
            help_line = QLabel(tr(TOOL_HELP))
            help_line.setWordWrap(True)
            layout.addWidget(help_line)
        self.details = QLabel()
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        layout.addWidget(self.details)
        self.thumbnail = QLabel()
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.thumbnail)
        row_tools = QHBoxLayout()
        self.preview_button = QPushButton(tr("Предпросмотр"), clicked=self.request_preview)
        self.group_button = QPushButton(tr("Применить к аналогичным файлам"), clicked=self.apply_group)
        row_tools.addWidget(self.preview_button)
        row_tools.addWidget(self.group_button)
        row_tools.addStretch()
        layout.addLayout(row_tools)
        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel_button = QPushButton(tr("Отменить подготовку"), clicked=self.reject)
        self.apply_button = QPushButton(tr("Применить решения и продолжить"), clicked=self.accept)
        self.batch_button = QPushButton(tr("Подготовить все совместимые копии и продолжить"), clicked=self.batch)
        self.batch_button.setObjectName("primary")
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.apply_button)
        actions.addWidget(self.batch_button)
        layout.addLayout(actions)
        self.table.currentCellChanged.connect(self.refresh)
        self.table.selectRow(0)
        self.refresh()

    def add_entry(self, row, entry):
        cause = tr(REASON.get(entry.get("reason"), REASON["attachment_unconfirmed"]))
        values = (
            entry["name"], *(tr(MEDIA_TYPES.get(entry.get(key), "—")) for key in (
                "source_kind", "prepared_kind", "received_kind",
            )), cause, tr(ACTION.get(entry.get("recommended"), "—")),
        )
        for column, value in enumerate(values):
            cell = QTableWidgetItem(str(value))
            cell.setToolTip(str(value))
            self.table.setItem(row, column, cell)
        combo = QComboBox()
        combo.addItem(tr("Выберите действие"), None)
        for action in entry["options"]:
            combo.addItem(tr(ACTION[action]), action)
        combo.currentIndexChanged.connect(self.refresh)
        self.choices.append(combo)
        self.table.setCellWidget(row, 6, combo)

    def decisions(self):
        return {entry["key"]: choice.currentData() for entry, choice in zip(self.entries, self.choices, strict=True)}

    def refresh(self, *_):
        row = self.table.currentRow()
        selected = row >= 0
        if selected:
            entry = self.entries[row]
            original = entry.get("original") or {}
            width, height = original.get("width"), original.get("height")
            duration = original.get("duration")
            fields = (
                entry.get("date"), media_size(entry.get("size") or 0), original.get("container"), original.get("codec"),
                tr("{seconds} с").format(seconds=f"{duration:g}") if duration else None,
                f"{width} × {height}" if width and height else None,
            )
            self.details.setText(" · ".join(value for value in fields if value))
        self.apply_button.setEnabled(all(self.decisions().values()))
        self.batch_button.setEnabled(
            any(choice.findData("compatible") >= 0 for choice in self.choices)
            and all(choice.currentData() or choice.findData("compatible") >= 0 for choice in self.choices)
        )
        self.group_button.setEnabled(selected and bool(self.choices[row].currentData()))
        self.preview_button.setEnabled(bool(
            selected and self.project and self.entries[row].get("source") and not self.preview_job
        ))

    def apply_group(self):
        row = self.table.currentRow()
        if row < 0 or not (action := self.choices[row].currentData()):
            return
        group = self.entries[row]["group"]
        for entry, choice in zip(self.entries, self.choices, strict=True):
            if entry["group"] == group and (index := choice.findData(action)) >= 0:
                choice.setCurrentIndex(index)

    def batch(self):
        for choice in self.choices:
            if not choice.currentData() and (index := choice.findData("compatible")) >= 0:
                choice.setCurrentIndex(index)
        if self.apply_button.isEnabled():
            self.accept()

    def accept(self):
        if self.apply_button.isEnabled():
            super().accept()

    def request_preview(self):
        self.thumbnail.setText(tr("Загрузка предпросмотра…"))
        entry = self.entries[self.table.currentRow()]
        self.preview_job = PreviewWorker(self.project, entry, self.tools.ffmpeg, self)
        self.preview_job.picture.connect(self.show_preview)
        self.preview_job.finished.connect(self.preview_finished)
        self.preview_job.start()
        self.refresh()

    def show_preview(self, image):
        if image is None or image.isNull():
            self.thumbnail.setText(tr("Предпросмотр недоступен"))
        else:
            self.thumbnail.setPixmap(QPixmap.fromImage(image).scaledToHeight(145, Qt.TransformationMode.SmoothTransformation))

    def preview_finished(self):
        worker, self.preview_job = self.preview_job, None
        worker.deleteLater()
        self.refresh()

    def done(self, result):
        if self.preview_job:
            worker, self.preview_job = self.preview_job, None
            worker.finished.disconnect(self.preview_finished)
            worker.finished.connect(worker.deleteLater)
            worker.picture.disconnect(self.show_preview)
            worker.setParent(self.parent())
        super().done(result)
