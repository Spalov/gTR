"""Paged, local presentation of the three layers of recovery evidence."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .conversion import LOSS
from .fidelity import STATUS
from .i18n import tr
from .media import KIND_NAMES

FIELDS = {
    "text": "Текст", "line_breaks": "Переносы строк", "unicode": "Unicode", "emoji": "Эмодзи",
    "date": "Дата", "time": "Время", "timezone": "Часовой пояс", "order": "Порядок",
    "author": "Историческая подпись", "attachments": "Вложения", "media_type": "Тип медиа",
    "filename": "Имя файла", "caption": "Подпись к медиа", "formatting": "Форматирование",
    "forward": "Пересылка", "reply": "Ответ", "quote": "Цитата", "album": "Альбом",
    "editing": "Редактирование", "reactions": "Реакции", "links": "Ссылки",
}
PAGE = 80


def kind_name(kind):
    return tr(KIND_NAMES.get(kind, kind or "—"))


class RecoveryResults(QWidget):
    def __init__(self, export):
        super().__init__()
        self.report, self.offset = None, 0
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([
            tr(value) for value in ("Исходная запись", "Источник", "Пакет", "Наблюдение", "Состояние")
        ])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.currentCellChanged.connect(self.details)
        layout.addWidget(self.table, 2)
        self.detail = QPlainTextEdit(readOnly=True)
        layout.addWidget(self.detail, 1)
        controls = QHBoxLayout()
        self.prev = QPushButton(tr("← Назад"))
        self.next = QPushButton(tr("Далее →"))
        self.page = QLabel()
        self.export = QPushButton(tr("Сохранить отчёт…"))
        self.export.clicked.connect(export)
        self.prev.clicked.connect(lambda: self.turn(-PAGE))
        self.next.clicked.connect(lambda: self.turn(PAGE))
        for widget in (self.prev, self.page, self.next, self.export):
            controls.addWidget(widget)
        layout.addLayout(controls)
        self.bind(None)

    def bind(self, report):
        self.report, self.offset = report, 0
        if report:
            quality = report["quality"]
            score = "—" if quality["index"] is None else f'{quality["index"]:g}%'
            independent = "" if quality["independent_fraction"] else f" · {tr('Нет независимых снимков')}"
            self.summary.setText(
                f"{tr('Подтверждённый индекс')} {score}{independent} · "
                f"{tr('Записей с расхождениями')} {quality['messages_with_errors']}"
            )
        else:
            self.summary.setText(tr("Откройте пакет импорта для сверки по сообщениям."))
        self.export.setEnabled(report is not None)
        self.turn(0)

    def turn(self, step):
        self.offset += step
        rows = self.report["records"] if self.report else []
        page = rows[self.offset:self.offset + PAGE]
        self.table.blockSignals(True)
        self.table.clearContents()
        self.table.setRowCount(len(page))
        for index, row in enumerate(page):
            values = (
                str(row["source_ordinal"]),
                ", ".join(map(kind_name, row["source_kinds"])) or "—",
                kind_name(row["prepared_kind"]),
                " / ".join(kind_name(view["kind"]) for view in row["observed"]) or "—",
                tr(STATUS[row["status"]]),
            )
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(value))
        self.table.blockSignals(False)
        if page:
            self.table.setCurrentCell(0, 0)
        self.details(0 if page else -1)
        self.page.setText(f"{self.offset + (1 if page else 0)}–{self.offset + len(page)} / {len(rows)}")
        self.prev.setEnabled(self.offset > 0)
        self.next.setEnabled(self.offset + PAGE < len(rows))

    def details(self, row, *_):
        if row < 0 or not self.report:
            self.detail.clear()
            return
        entry = self.report["records"][self.offset + row]
        lines = [f"{tr('Источник')} · #{entry['source_id']} · {entry['historical_date'] or '—'}"]
        lines.extend(str(name) for name in entry["original_filenames"] if name)
        lines.append(f"{tr('Пакет')} · {entry['prepared_date'] or '—'} · {entry['prepared_file'] or '—'}")
        for index, seen in enumerate(entry["observed"], 1):
            lines.append(
                f"{tr('Наблюдение')} {index} · #{seen['server_id']} · {seen['date'] or '—'} → {seen['server_date'] or '—'}"
                f" · {seen['file'] or '—'} · {seen['constructor'] or '—'}"
            )
        lines.extend(f"{tr(title)} · {tr(STATUS[entry['fields'][key]])}" for key, title in FIELDS.items())
        lines.extend(tr(LOSS.get(code, code)) for code in entry["reasons"])
        self.detail.setPlainText("\n".join(lines))
