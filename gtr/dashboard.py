from datetime import date
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .core import catalogue
from .media import KIND_NAMES

ARTWORK = Path(__file__).parent / "assets" / "cat.png"


def cat_art(size):
    picture = QLabel()
    picture.setAccessibleName("Логотип gTR с котом")
    picture.setPixmap(
        QPixmap(str(ARTWORK)).scaled(
            size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
    )
    picture.setFixedSize(size, size)
    return picture


def number(n):
    return f"{n:,}".replace(",", " ")


def count_label(n, one, few, many):
    word = many if 11 <= n % 100 <= 14 else {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)
    return f"{number(n)} {word}"


def size_text(n):
    return f"{n / 1024**2:.1f} МиБ".replace(".", ",")


def label(text, role=""):
    obj = QLabel(text)
    obj.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        obj.setObjectName(role)
    obj.setWordWrap(True)
    return obj


def panel():
    box = QFrame()
    box.setObjectName("panel")
    layout = QVBoxLayout(box)
    layout.setContentsMargins(18, 15, 18, 15)
    layout.setSpacing(7)
    return box, layout


class Timeline(QWidget):
    def __init__(self):
        super().__init__()
        self.series = []
        self.setMinimumHeight(80)

    def set_series(self, project):
        with catalogue(project) as db:
            counts = dict(
                db.execute("""SELECT substr(date_iso,1,10),count(*) FROM messages
                WHERE service=0 AND epoch IS NOT NULL GROUP BY substr(date_iso,1,10)""")
            )
        self.series = []
        if counts:
            first, last = min(counts), max(counts)
            day = date.fromisoformat(first)
            length = (date.fromisoformat(last) - day).days + 1
            buckets = min(110, length)
            self.series = [0] * buckets
            for key, count in counts.items():
                index = (date.fromisoformat(key) - day).days * buckets // length
                self.series[index] += count
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height() - 4
        painter.setPen(QPen(QColor("#354044"), 1, Qt.PenStyle.DotLine))
        for part in (0.1, 0.45, 0.8):
            painter.drawLine(0, int(h * part), w, int(h * part))
        if not self.series:
            return
        step = w / len(self.series)
        gradient = QLinearGradient(0, 0, 0, h)
        gradient.setColorAt(0, QColor("#93cbf0"))
        gradient.setColorAt(1, QColor("#426c96"))
        painter.setBrush(gradient)
        painter.setPen(Qt.PenStyle.NoPen)
        peak = max(self.series)
        for i, value in enumerate(self.series):
            height = h * value / peak
            if value:
                painter.drawRoundedRect(QRectF(i * step, h - height, max(1, step * 0.74), height), 2, 2)


class Dashboard(QWidget):
    def __init__(self, open_archive, open_folder, open_project, open_viewer, open_media, show_export_guide):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(15)
        self.welcome = QFrame(objectName="welcome")
        hero = QHBoxLayout(self.welcome)
        hero.setContentsMargins(26, 26, 26, 26)
        hero.setSpacing(24)
        intro = QVBoxLayout()
        intro.addStretch()
        intro.addWidget(label("Восстановите историю\nиз архива Telegram", "welcomeTitle"))
        intro.addWidget(
            label(
                "Неофициальное приложение на базе Telegram API. Откройте экспорт Telegram Desktop.",
                "muted",
            )
        )
        intro.addSpacing(12)
        start = QPushButton("Выбрать файл…")
        start.setObjectName("primary")
        start.clicked.connect(open_archive)
        folder = QPushButton("Выбрать папку…")
        folder.clicked.connect(open_folder)
        intro.addWidget(start, alignment=Qt.AlignmentFlag.AlignLeft)
        intro.addWidget(folder, alignment=Qt.AlignmentFlag.AlignLeft)
        export_help = QPushButton("Как подготовить архив…")
        export_help.clicked.connect(show_export_guide)
        intro.addWidget(export_help, alignment=Qt.AlignmentFlag.AlignLeft)
        intro.addStretch()
        hero.addLayout(intro, 1)
        hero.addWidget(cat_art(246), alignment=Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.welcome)
        self.continue_bar, actions = panel()
        actions.addWidget(label("Сохранённая работа", "section"))
        resume = QPushButton("Открыть проект…")
        resume.clicked.connect(open_project)
        actions.addWidget(resume, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.continue_bar)
        self.results = QWidget()
        result_layout = QVBoxLayout(self.results)
        result_layout.setContentsMargins(0, 0, 0, 0)
        result_layout.setSpacing(14)
        layout.addWidget(self.results, 1)
        self.results.hide()
        self.banner, top = panel()
        self.banner.setObjectName("hero")
        self.title = label("Откройте экспорт Telegram", "heroTitle")
        self.detail = label(
            "Выберите архив или папку, чтобы восстановить переписку на этом компьютере.", "muted"
        )
        top.addWidget(self.title)
        top.addWidget(self.detail)
        actions = QHBoxLayout()
        open_button = QPushButton("Выбрать архив")
        open_button.setObjectName("primary")
        open_button.clicked.connect(open_archive)
        self.viewer_button = QPushButton("Смотреть переписку  →")
        self.viewer_button.clicked.connect(open_viewer)
        self.viewer_button.setEnabled(False)
        media_button = QPushButton("Открыть медиа")
        media_button.clicked.connect(open_media)
        actions.addWidget(open_button)
        actions.addWidget(self.viewer_button)
        actions.addWidget(media_button)
        actions.addStretch()
        top.addLayout(actions)
        result_layout.addWidget(self.banner)
        metrics = QHBoxLayout()
        self.values = []
        for title in ("Сообщения", "Участники", "Вложения", "Ошибки"):
            card, inner = panel()
            inner.addWidget(label(title, "muted"))
            value = label("—", "metric")
            inner.addWidget(value)
            self.values.append(value)
            metrics.addWidget(card)
        result_layout.addLayout(metrics)
        chart, graph = panel()
        graph.addWidget(label("Активность переписки", "section"))
        self.timeline = Timeline()
        graph.addWidget(self.timeline, 1)
        dates = QHBoxLayout()
        self.first, self.last = label("Период по датам источника", "muted"), label("", "muted")
        dates.addWidget(self.first)
        dates.addStretch()
        dates.addWidget(self.last)
        graph.addLayout(dates)
        result_layout.addWidget(chart, 1)
        media, body = panel()
        body.addWidget(label("Состав вложений", "section"))
        self.media_grid = QGridLayout()
        self.breakdown = []
        for i, name in enumerate(KIND_NAMES.values()):
            item = label(name + "   —", "secondary")
            self.media_grid.addWidget(item, i // 4, i % 4)
            self.breakdown.append(item)
        body.addLayout(self.media_grid)
        result_layout.addWidget(media)
        quality, body = panel()
        self.quality_heading = label("Качество восстановления", "section")
        body.addWidget(self.quality_heading)
        grid = QGridLayout()
        self.quality = {}
        for i, title in enumerate(("Текст", "Даты", "Авторы", "Медиа", "Ответы", "Видимость")):
            item = label(f"{title} · ожидается", "muted")
            grid.addWidget(item, i // 3, i % 3)
            self.quality[title] = item
        body.addLayout(grid)
        result_layout.addWidget(quality)

    def set_quality(self, result):
        mode = " · имитация" if result and result["mode"] == "mock" else ""
        self.quality_heading.setText("Качество восстановления" + mode)
        labels = {
            "verified": "проверено",
            "differences": "расхождения",
            "unknown": "нет подтверждения",
            "UNSUPPORTED_BY_API": "вне возможностей API",
        }
        for key, widget in self.quality.items():
            status = result["quality"].get(key, "unknown") if result else None
            widget.setText(f"{key} · {labels.get(status, 'ожидается')}")

    def bind(self, project, stats):
        self.welcome.hide()
        self.continue_bar.hide()
        self.results.show()
        self.title.setText(project.name)
        chats = count_label(stats["chats"], "диалог", "диалога", "диалогов")
        files = count_label(stats["files"], "файл", "файла", "файлов")
        self.detail.setText(
            f"{stats['export_format'].upper()}   ·   {chats}   ·   {files}   ·   {size_text(stats['extracted_bytes'])} после распаковки"
        )
        self.viewer_button.setEnabled(True)
        for item, value in zip(
            self.values,
            (stats["messages"], stats["participants"], sum(stats["attachments"].values()), stats["errors"]),
        ):
            item.setText(number(value))
        for k, item in zip(KIND_NAMES, self.breakdown):
            item.setText(f"{KIND_NAMES[k]}   {number(stats['attachments'].get(k, 0))}")
        self.first.setText((stats["first"] or "—")[:10])
        self.last.setText((stats["last"] or "—")[:10])
        self.timeline.set_series(project)
