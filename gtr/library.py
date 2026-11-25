"""Project tabs for media inventory and source participants."""

from pathlib import PurePosixPath

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .core import ISSUES, catalogue
from .dashboard import label, number, size_text
from .i18n import tr
from .identity import participants
from .media import KIND_NAMES

PAGE_SIZE = 60


def table(headers, stretch=0):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    widget.verticalHeader().hide()
    widget.setAlternatingRowColors(True)
    widget.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    return widget


def fill(widget, rows, description):
    widget.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, value in enumerate(description(row)):
            widget.setItem(i, j, QTableWidgetItem(str(value)))
    if rows:
        widget.selectRow(0)


class Library(QWidget):
    def __init__(self, open_asset, jump, inspect_media=None, export_media=None, gallery=None):
        super().__init__()
        self.project = None
        self.offset = 0
        self.rows = []
        self.open_asset, self.jump = open_asset, jump
        self.report = None
        self.chat, self.kind = QComboBox(), QComboBox()
        self.kind.addItem("Все типы", "")
        for key, name in KIND_NAMES.items():
            self.kind.addItem(name, key)
        self.search = QLineEdit(placeholderText="Имя файла…")
        controls = QHBoxLayout()
        for widget in (self.chat, self.kind, self.search):
            controls.addWidget(widget)
        self.grid = table(("Оригинал", "Тип", "Размер", "Состояние"))
        self.grid.cellDoubleClicked.connect(lambda *_: self.open_current())
        self.grid.itemSelectionChanged.connect(self.selection_changed)
        self.detail = label("Откройте архив для просмотра вложений.", "muted")
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.open = QPushButton("Просмотреть")
        self.open.clicked.connect(self.open_current)
        self.locate = QPushButton("К сообщению")
        self.locate.clicked.connect(self.locate_current)
        self.prev, self.next = QPushButton("← Назад"), QPushButton("Далее →")
        self.prev.clicked.connect(lambda: self.move(-PAGE_SIZE))
        self.next.clicked.connect(lambda: self.move(PAGE_SIZE))
        self.count = label("", "muted")
        footer = QHBoxLayout()
        for widget in (self.open, self.locate, self.count, self.prev, self.next):
            footer.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(controls)
        layout.addWidget(self.grid, 1)
        layout.addWidget(self.detail)
        layout.addLayout(footer)
        if inspect_media and export_media:
            tools = QHBoxLayout()
            audit = QPushButton("Проверить вложения…")
            audit.clicked.connect(lambda: inspect_media(self.chat.currentData()))
            self.save_inspection = QPushButton("Сохранить диагностику медиа…")
            self.save_inspection.setEnabled(False)
            self.save_inspection.clicked.connect(lambda: export_media(self.report))
            tools.addWidget(audit)
            tools.addWidget(self.save_inspection)
            if gallery:
                photos = QPushButton(tr("Галерея фотографий"))
                photos.clicked.connect(lambda: gallery(self.chat.currentData()))
                tools.addWidget(photos)
            tools.addStretch()
            layout.addLayout(tools)
        for signal in (self.chat.currentIndexChanged, self.kind.currentIndexChanged, self.search.textChanged):
            signal.connect(self.reset)
        self.selection_changed()
        self.prev.setEnabled(False)
        self.next.setEnabled(False)

    def bind(self, project):
        self.project = project
        self.report = None
        if hasattr(self, "save_inspection"):
            self.save_inspection.setEnabled(False)
        self.chat.blockSignals(True)
        self.chat.clear()
        self.chat.addItem("Все диалоги", None)
        with catalogue(project) as db:
            for cid, title in db.execute("SELECT id,title FROM chats ORDER BY id"):
                self.chat.addItem(title, cid)
        self.chat.blockSignals(False)
        self.reset()

    def inspected(self, report):
        self.report = report
        self.save_inspection.setEnabled(True)
        message = (
            f"Проверено вложений {number(report['count'])} из {number(report['total'])}. "
            f"Блокирующих ошибок {number(report['blocked'])}. Повторов байтов {number(report['issues'].get('duplicate', 0))}."
        )
        self.detail.setText(message)
        return message

    def reset(self, *_):
        self.offset = 0
        self.render()

    def move(self, amount):
        self.offset = max(0, self.offset + amount)
        self.render()

    def render(self):
        if not self.project:
            return
        conditions, args = ["1"], []
        if self.chat.currentData() is not None:
            conditions.append("m.chat=?")
            args.append(self.chat.currentData())
        if self.kind.currentData():
            conditions.append("a.kind=?")
            args.append(self.kind.currentData())
        if self.search.text():
            conditions.append("instr(casefold(a.original),?)>0")
            args.append(self.search.text().casefold())
        where = " AND ".join(conditions)
        with catalogue(self.project) as db:
            db.create_function("casefold", 1, str.casefold)
            joined = f"FROM assets a JOIN messages m ON m.id=a.msg WHERE {where}"
            total = db.execute(f"SELECT count(*) {joined}", args).fetchone()[0]
            self.rows = db.execute(
                f"""SELECT a.*,m.chat,m.source_id,m.date_iso,
                f.size,f.sha256,c.title FROM assets a JOIN messages m ON m.id=a.msg
                JOIN chats c ON c.id=m.chat LEFT JOIN files f ON f.path=a.path
                WHERE {where} ORDER BY a.id LIMIT ? OFFSET ?""",
                (*args, PAGE_SIZE, self.offset),
            ).fetchall()
        self.grid.clearContents()
        fill(
            self.grid,
            self.rows,
            lambda a: (
                PurePosixPath(a["original"]).name or KIND_NAMES.get(a["kind"], "Вложение"),
                KIND_NAMES.get(a["kind"], a["kind"]),
                size_text(a["size"]) if a["size"] is not None else "—",
                {"ok": "Проверено", "metadata": "Данные экспорта"}.get(
                    a["status"], ISSUES.get(a["status"], a["status"])
                ),
            ),
        )
        self.prev.setEnabled(self.offset > 0)
        self.next.setEnabled(self.offset + PAGE_SIZE < total)
        self.count.setText(f"{number(len(self.rows) + self.offset)} из {number(total)}")
        self.selection_changed()

    def current(self):
        i = self.grid.currentRow()
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def selection_changed(self):
        data = self.current()
        self.open.setEnabled(bool(data and data["status"] in {"ok", "unconfirmed_type", "metadata"}))
        self.locate.setEnabled(data is not None)
        if data:
            checksum = f" · SHA-256 {data['sha256']}" if data["sha256"] else ""
            self.detail.setText(f"{data['title']} · #{data['source_id'] or '—'}{checksum}")
        elif self.project:
            self.detail.setText("По выбранным условиям вложений нет.")

    def open_current(self):
        data = self.current()
        if data and self.open.isEnabled():
            self.open_asset(data["id"])

    def locate_current(self):
        data = self.current()
        if data:
            self.jump(data["chat"], data["msg"])


class Participants(QWidget):
    def __init__(self, perspective):
        super().__init__()
        self.project = None
        self.rows = []
        self.perspective = perspective
        self.chat = QComboBox()
        self.chat.currentIndexChanged.connect(self.refresh)
        self.grid = table(("Имя в экспорте", "Исходный ID", "Сообщения", "По группировке HTML"))
        self.grid.cellDoubleClicked.connect(lambda *_: self.open())
        self.action = QPushButton("Открыть с этой стороны")
        self.action.setEnabled(False)
        self.action.clicked.connect(self.open)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.chat)
        layout.addWidget(
            label("Для HTML доступно имя. В JSON отдельно показан ID отправителя из экспорта.", "muted")
        )
        layout.addWidget(self.grid, 1)
        layout.addWidget(self.action, alignment=Qt.AlignmentFlag.AlignLeft)

    def bind(self, project):
        self.project = project
        self.chat.blockSignals(True)
        self.chat.clear()
        with catalogue(project) as db:
            for cid, title in db.execute("SELECT id,title FROM chats ORDER BY id"):
                self.chat.addItem(title, cid)
        self.chat.blockSignals(False)
        self.refresh()

    def refresh(self, *_):
        if self.project and self.chat.currentData():
            with catalogue(self.project) as db:
                self.rows = participants(db, self.chat.currentData())
            self.grid.clearContents()
            fill(
                self.grid,
                self.rows,
                lambda a: (
                    a["sender"] or "Не определён",
                    a["source_id"] or "—",
                    number(a["messages"]),
                    number(a["joined"]),
                ),
            )
            self.action.setEnabled(bool(self.rows))

    def open(self):
        row = self.grid.currentRow()
        if 0 <= row < len(self.rows):
            self.perspective(self.chat.currentData(), self.rows[row]["sender_key"])
