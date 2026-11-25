"""Read and decode only the current page of archive photographs."""

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from .core import catalogue
from .files import safe_parts
from .i18n import tr
from .media_preview import preview
from .model import RecoveryError

PAGE_SIZE = 12


class PhotoGallery(QDialog):
    def __init__(self, parent, project, chat, open_photo):
        super().__init__(parent)
        self.project, self.chat, self.open_photo = project, chat, open_photo
        self.offset, self.active = 0, True
        self.setWindowTitle(tr("Галерея фотографий"))
        self.resize(800, 560)
        layout = QVBoxLayout(self)
        self.photos = QListWidget()
        self.photos.setViewMode(QListWidget.ViewMode.IconMode)
        self.photos.setMovement(QListWidget.Movement.Static)
        self.photos.setIconSize(QSize(144, 108))
        self.photos.setGridSize(QSize(168, 138))
        self.photos.itemDoubleClicked.connect(self.open_item)
        layout.addWidget(self.photos, 1)
        controls = QHBoxLayout()
        self.prev, self.next = QPushButton(tr("← Назад")), QPushButton(tr("Далее →"))
        self.prev.clicked.connect(lambda: self.load(-PAGE_SIZE))
        self.next.clicked.connect(lambda: self.load(PAGE_SIZE))
        self.info = QLabel()
        for widget in (self.prev, self.info, self.next):
            controls.addWidget(widget)
        layout.addLayout(controls)
        self.finished.connect(self.finish)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

    def finish(self, *_):
        self.active = False

    def open_item(self, item):
        asset = item.data(Qt.ItemDataRole.UserRole)
        if item.icon().isNull() or self.parent().worker:
            return
        open_photo = self.open_photo
        self.close()
        open_photo(asset)

    def load(self, step=0):
        if self.parent().worker:
            return
        self.prev.setEnabled(False)
        self.next.setEnabled(False)
        offset = self.offset + step
        project, chat = self.project, self.chat

        def job(progress):
            conditions = "a.kind='photo'"
            args = []
            if chat is not None:
                conditions += " AND m.chat=?"
                args.append(chat)
            with catalogue(project) as db:
                total = db.execute(
                    f"""SELECT count(*) FROM assets a JOIN messages m ON m.id=a.msg WHERE {conditions}""",
                    args,
                ).fetchone()[0]
                page = db.execute(
                    f"""SELECT a.id,a.path,a.original,a.expected_mime,a.status,f.sha256
                    FROM assets a JOIN messages m ON m.id=a.msg LEFT JOIN files f ON f.path=a.path
                    WHERE {conditions} ORDER BY a.id LIMIT ? OFFSET ?""",
                    [*args, PAGE_SIZE, offset],
                ).fetchall()
            photos = []
            for index, item in enumerate(page, 1):
                progress("inspect_media", index, len(page))
                picture = None
                if item["status"] in {"ok", "unconfirmed_type"} and item["path"]:
                    try:
                        path = project.joinpath("source", *safe_parts(item["path"]))
                        _, picture = preview(path, item["sha256"], item["expected_mime"], progress=progress)
                    except (RecoveryError, OSError):
                        pass
                name = item["original"].replace(chr(92), "/").rsplit("/", 1)[-1] or "—"
                photos.append((item["id"], name, picture))
            return total, photos

        def ready(result):
            if self.active:
                self.render(offset, *result)
            return tr("Галерея фотографий")

        self.parent().run_job(tr("Галерея фотографий"), job, ready)

    def render(self, offset, total, photos):
        self.offset = offset
        self.photos.clear()
        for asset, name, picture in photos:
            item = QListWidgetItem(name if picture and not picture.isNull() else f"{name} · —")
            item.setData(Qt.ItemDataRole.UserRole, asset)
            if picture and not picture.isNull():
                item.setIcon(QIcon(QPixmap.fromImage(picture)))
            self.photos.addItem(item)
        self.prev.setEnabled(offset > 0)
        self.next.setEnabled(offset + PAGE_SIZE < total)
        self.info.setText(f"{offset + (1 if photos else 0)}–{offset + len(photos)} / {total}")
