"""Paged, offline chat viewer. Archive HTML never reaches a browser engine."""

import json
import os
import re
from datetime import datetime, time, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, Qt, QUrl
from PySide6.QtGui import QFontMetrics, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .core import ISSUES, catalogue, properties
from .files import local, publish_text, safe_parts
from .history import SOURCE_ORDER
from .i18n import tr
from .media import KIND_NAMES
from .media_engine import MEDIA_ISSUES
from .media_preview import FFmpegThumbnail, preview
from .model import RecoveryError
from .structure import forward_from_meta

PAGE_SIZE = 40
ORDER = SOURCE_ORDER


class LocalBrowser(QTextBrowser):
    def loadResource(self, resource_type, name):
        # The transcript contains text and internal action links only.
        return None


class Viewer(QWidget):
    def __init__(self, run_job=None, ffmpeg_path=lambda: "", *, forwards=False, resolve=None, jump=None):
        super().__init__()
        self.run_job, self.ffmpeg_path = run_job, ffmpeg_path
        self.project = None
        self.forwards_only = forwards
        self.current_names = {}
        self.page_ids = ()
        self.resolve = resolve
        self.jump = jump
        self.offset = 0
        self.zone = ""
        self.chat, self.side = QComboBox(), QComboBox()
        self.chat.setToolTip("Диалог из экспорта")
        self.side.setToolTip("Чьи сообщения показывать справа")
        self.search = QLineEdit(placeholderText=tr("Поиск по тексту…"))
        self.search.setClearButtonEnabled(True)
        self.browser = LocalBrowser()
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.anchorClicked.connect(self.follow)
        self.status = QLabel()
        self.prev, self.next = QPushButton(tr("← Назад")), QPushButton(tr("Далее →"))
        self.prev.clicked.connect(lambda: self.move(-PAGE_SIZE))
        self.next.clicked.connect(lambda: self.move(PAGE_SIZE))
        controls = QHBoxLayout()
        for w in (self.chat, self.search, QLabel("Справа"), self.side):
            controls.addWidget(w)
        self.kind = QComboBox()
        self.kind.setAccessibleName("Фильтр по вложениям")
        self.kind.addItem("Все сообщения", "")
        self.kind.addItem("Все с вложениями", "any")
        self.kind.addItem("Только текст", "text")
        for key, title in KIND_NAMES.items():
            self.kind.addItem(title, key)
        self.day = QDateEdit(QDate.currentDate())
        self.day.setCalendarPopup(True)
        self.day.setDisplayFormat("dd.MM.yyyy")
        self.go = QPushButton("К дате")
        self.day.setEnabled(False)
        self.go.setEnabled(False)
        self.go.clicked.connect(self.jump_day)
        filters = QHBoxLayout()
        filters.addWidget(self.kind)
        self.author = QComboBox()
        self.author.setAccessibleName(tr("Все авторы"))
        filters.addWidget(self.author)
        self.forward_filter = QCheckBox(tr("Только пересланные"))
        self.forward_filter.setChecked(forwards)
        if not forwards:
            filters.addWidget(self.forward_filter)
        self.message_id = QLineEdit(placeholderText="ID сообщения")
        self.message_id.setAccessibleName("Исходный ID сообщения")
        self.message_id.setMaximumWidth(135)
        self.message_id.returnPressed.connect(self.jump_id)
        find = QPushButton("К ID")
        find.clicked.connect(self.jump_id)
        jump = QHBoxLayout()
        jump.addWidget(self.message_id)
        jump.addWidget(find)
        jump.addStretch()
        jump.addWidget(self.day)
        jump.addWidget(self.go)
        pager = QHBoxLayout()
        pager.addWidget(self.status)
        pager.addStretch()
        pager.addWidget(self.prev)
        pager.addWidget(self.next)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(controls)
        layout.addLayout(filters)
        layout.addLayout(jump)
        if resolve:
            self.lookup = QPushButton(tr("Обновить имена на этой странице"))
            self.lookup.clicked.connect(lambda: resolve(self.page_ids))
            clear = QPushButton(tr("Показать только локальный архив"))
            clear.clicked.connect(self.clear_names)
            options = QHBoxLayout()
            options.addWidget(self.lookup)
            options.addWidget(clear)
            options.addStretch()
            layout.addLayout(options)
        else:
            export = QPushButton(tr("Сохранить выбранный фрагмент…"))
            export.clicked.connect(self.export_fragment)
            jump.addWidget(export)
        layout.addWidget(self.browser, 1)
        layout.addLayout(pager)
        self.chat.currentIndexChanged.connect(self.load_sides)
        self.side.currentIndexChanged.connect(self.render)
        self.search.textChanged.connect(self.reset_page)
        self.kind.currentIndexChanged.connect(self.reset_page)
        self.author.currentIndexChanged.connect(self.reset_page)
        self.forward_filter.toggled.connect(self.reset_page)
        self.render()

    def bind(self, project):
        self.project = project
        self.current_names.clear()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.kind.blockSignals(True)
        self.kind.setCurrentIndex(0)
        self.kind.blockSignals(False)
        self.chat.blockSignals(True)
        self.chat.clear()
        with catalogue(project) as db:
            for row in db.execute("SELECT id,title FROM chats ORDER BY id"):
                self.chat.addItem(row["title"], row["id"])
        self.chat.blockSignals(False)
        self.load_sides()

    def load_sides(self):
        if not self.project or not self.chat.currentData():
            return
        self.side.blockSignals(True)
        self.side.clear()
        self.author.blockSignals(True)
        self.author.clear()
        self.author.addItem(tr("Все авторы"), "")
        with catalogue(self.project) as db:
            for row in db.execute(
                """SELECT sender_key,min(sender) AS label FROM messages WHERE chat=?
                AND service=0 AND sender_key<>'' GROUP BY sender_key ORDER BY min(id)""",
                (self.chat.currentData(),),
            ):
                title = row["label"] or row["sender_key"]
                self.side.addItem(title, row["sender_key"])
                self.author.addItem(title, row["sender_key"])
        self.side.blockSignals(False)
        self.author.blockSignals(False)
        self.set_date_range()
        self.reset_page()

    def set_date_range(self):
        if not self.project or not self.chat.currentData():
            return
        with catalogue(self.project) as db:
            dates = db.execute(
                """SELECT min(substr(date_iso,1,10)),max(substr(date_iso,1,10)),min(epoch),max(epoch)
                FROM messages WHERE chat=? AND epoch IS NOT NULL""",
                (self.chat.currentData(),),
            ).fetchone()
        valid = dates[0] is not None
        self.day.setEnabled(valid)
        self.go.setEnabled(valid)
        if valid:
            if self.zone:
                zone = ZoneInfo(self.zone)
                lo, hi = [QDate(datetime.fromtimestamp(v, zone).date()) for v in dates[2:]]
            else:
                lo, hi = [QDate.fromString(v, "yyyy-MM-dd") for v in dates[:2]]
            self.day.setDateRange(lo, hi)
            self.day.setDate(lo)

    def selection(self):
        clauses, args = ["chat=?"], [self.chat.currentData()]
        if self.search.text():
            clauses.append("instr(casefold(text),?)>0")
            args.append(self.search.text().casefold())
        if author := self.author.currentData():
            clauses.append("sender_key=?")
            args.append(author)
        if self.forward_filter.isChecked():
            clauses.append("""(
                json_extract(meta,'$.forward.is_forwarded')=1
                OR json_type(meta,'$.forwarded_from') IS NOT NULL
                OR json_type(meta,'$.forwarded_from_id') IS NOT NULL
                OR json_type(meta,'$.forward_info') IS NOT NULL
            )""")
        kind = self.kind.currentData()
        if kind == "text":
            clauses.append(
                "service=0 AND text<>'' AND NOT EXISTS (SELECT 1 FROM assets WHERE msg=messages.id)"
            )
        elif kind == "any":
            clauses.append("EXISTS (SELECT 1 FROM assets WHERE msg=messages.id)")
        elif kind:
            clauses.append("EXISTS (SELECT 1 FROM assets WHERE msg=messages.id AND kind=?)")
            args.append(kind)
        return " AND ".join(clauses), args

    def jump_day(self):
        if not self.project or not self.chat.currentData():
            return
        day = self.day.date().toPython()
        condition, args = self.selection()
        if self.zone:
            zone = ZoneInfo(self.zone)
            day_sql = "epoch>=? AND epoch<?"
            limits = [
                datetime.combine(day + timedelta(days=n), time.min, tzinfo=zone).timestamp() for n in (0, 1)
            ]
        else:
            day_sql, limits = "substr(date_iso,1,10)=?", [day.isoformat()]
        with catalogue(self.project) as db:
            db.create_function("casefold", 1, str.casefold)
            target = db.execute(
                f"""SELECT id,pos FROM (SELECT id,epoch,date_iso,row_number() OVER
                (ORDER BY {ORDER})-1 AS pos FROM messages WHERE {condition}) WHERE {day_sql} ORDER BY pos LIMIT 1""",
                [*args, *limits],
            ).fetchone()
        if target is None:
            self.status.setText("За выбранный день с этими фильтрами сообщений нет.")
        else:
            self.offset = target["pos"] // PAGE_SIZE * PAGE_SIZE
            self.render()
            self.browser.scrollToAnchor("m" + str(target["id"]))

    def jump_id(self):
        if not self.project or not self.chat.currentData():
            return
        value = self.message_id.text().strip().removeprefix("#")
        if not re.fullmatch(r"-?\d+", value):
            self.status.setText("Введите исходный числовой ID.")
            return
        with catalogue(self.project) as db:
            result = db.execute(
                "SELECT id FROM messages WHERE chat=? AND source_id=?", (self.chat.currentData(), value)
            ).fetchone()
        if result:
            self.jump_to(self.chat.currentData(), result["id"])
        else:
            self.status.setText("В выбранном диалоге такого ID нет.")

    def showEvent(self, event):
        super().showEvent(event)
        if self.project:
            scroll = self.browser.verticalScrollBar()
            position = scroll.value()
            self.render()
            scroll.setValue(position)

    def reset_page(self):
        self.offset = 0
        self.render()

    def move(self, delta):
        self.offset = max(0, self.offset + delta)
        self.render()

    def render(self, *_):
        self.prev.setEnabled(False)
        self.next.setEnabled(False)
        self.page_ids = ()
        if self.resolve:
            self.lookup.setEnabled(False)
        if not self.project or not self.chat.currentData():
            self.browser.setPlainText("Откройте архив, чтобы просмотреть переписку.")
            self.status.clear()
            return
        condition, args = self.selection()
        with catalogue(self.project) as db:
            db.create_function("casefold", 1, str.casefold)
            total = db.execute(f"SELECT count(*) FROM messages WHERE {condition}", args).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM messages WHERE {condition} ORDER BY {ORDER} LIMIT ? OFFSET ?",
                (*args, PAGE_SIZE, self.offset),
            ).fetchall()
            parts, last_day = [], ""
            self.page_ids = tuple(sorted({
                identity for row in rows
                if (identity := forward_from_meta(json.loads(row["meta"])).user_id) is not None
            }))
            if self.resolve:
                self.lookup.setEnabled(bool(self.page_ids))
            for row in rows:
                moment = datetime.fromisoformat(row["date_iso"]) if row["date_iso"] else None
                if moment and self.zone:
                    moment = moment.astimezone(ZoneInfo(self.zone))
                day = moment.strftime("%d.%m.%Y") if moment else row["date_raw"] or "Дата неизвестна"
                if day != last_day:
                    parts.append(
                        f'<p align="center" style="margin-top:6px;margin-bottom:6px;color:#a0b6bd;font-size:9pt">{escape(day)}</p>'
                    )
                    last_day = day
                parts.append(self.bubble(db, row, moment))
        doc = "".join(parts) or '<p align="center">Совпадений нет.</p>'
        self.browser.setHtml(doc)
        self.prev.setEnabled(self.offset > 0)
        self.next.setEnabled(self.offset + PAGE_SIZE < total)
        self.status.setText(
            f"{self.offset + 1 if rows else 0}–{self.offset + len(rows)} из {total:,}".replace(",", " ")
        )

    def bubble(self, db, row, moment):
        mid = row["id"]
        if row["service"]:
            meta = json.loads(row["meta"])
            text = escape(row["text"] or meta.get("action", "Служебное событие"))
            return f'<p align="center" style="color:#acb8ba"><a name="m{mid}"></a>{text}</p>'
        right = bool(row["sender_key"]) and row["sender_key"] == self.side.currentData()
        align, fill = ("right", "#253f54") if right else ("left", "#252f3a")
        inner = [f'<b style="color:#f4cf91">{escape(row["sender"] or "Автор неизвестен")}</b>']
        widths = [row["sender"], *row["text"].splitlines()[:40]]
        meta = json.loads(row["meta"])
        forward = forward_from_meta(meta)
        if forward.is_forwarded:
            if forward.user_id in self.current_names:
                forward = forward.resolve(self.current_names)
            historical = forward.historical_sender_name
            display = forward.resolved_display_name or historical or tr("Неизвестный отправитель")
            detail = {
                "contacts": "Имя из ваших контактов", "public": "Имя в Telegram",
            }.get(forward.resolution_source)
            lines = [f"{tr('Переслано от')} · {escape(display)}"]
            if detail:
                lines.append(tr(detail))
            if historical and historical != forward.resolved_display_name:
                lines.append(f"{tr('Историческое имя')} · {escape(historical)}")
            if forward.privacy_state in {"hidden", "unavailable"}:
                lines.append(tr("Профиль скрыт" if forward.privacy_state == "hidden" else "Профиль недоступен"))
            if forward.original_peer_type != "unknown":
                lines.append(tr({"user": "Пользователь", "chat": "Группа", "channel": "Канал"}[forward.original_peer_type]))
            if forward.original_message_date:
                lines.append(escape(forward.original_message_date))
            inner.append("<small>" + " · ".join(lines) + "</small>")
        if meta.get("album_id"):
            inner.append(f"<small>{tr('Альбом')} · {escape(str(meta['album_id']))}</small>")
        if quote := meta.get("quote"):
            inner.append(f"<small>{tr('Цитата')} · {escape(str(quote)[:800])}</small>")
        if meta.get("edited_available") or meta.get("edited"):
            edit = meta.get("edited") or meta.get("edit_date") or meta.get("edited_unixtime") or ""
            inner.append(f"<small>{tr('Редактирование')} · {escape(str(edit))}</small>")
        if reactions := meta.get("reactions"):
            labels = [
                f"{r.get('emoji', '')} × {r.get('count', '—')}" for r in reactions[:24] if isinstance(r, dict)
            ] if isinstance(reactions, list) else []
            inner.append(f"<small>{tr('Реакции')} · {escape(' · '.join(labels) or '—')}</small>")
        if row["reply"]:
            target = row["target"]
            if target:
                original = db.execute("SELECT sender,text FROM messages WHERE id=?", (target,)).fetchone()
                excerpt = escape((original["text"] or "Вложение").replace("\n", " ")[:72])
                widths.append(original["sender"] + " · " + excerpt)
                inner.append(
                    f'<a href="gtrreply:{target}" style="color:#9cc7e6">↳ {escape(original["sender"])} · {excerpt}</a>'
                )
            else:
                inner.append(f"<small>↳ Вне архива · #{escape(row['reply'])}</small>")
        elif meta.get("unresolved_reply"):
            inner.append("<small>↳ Исходная запись вне архива</small>")
        for a in db.execute("SELECT * FROM assets WHERE msg=? ORDER BY id", (mid,)):
            label = f"{KIND_NAMES.get(a['kind'], 'Файл')} · {escape(Path(a['original']).name or 'файл отсутствует')}"
            widths.append(label)
            if a["status"] == "metadata":
                inner.append(
                    f'<a style="color:#c2dbf0" href="gtrmedia:{a["id"]}">{KIND_NAMES[a["kind"]]} · данные экспорта</a>'
                )
            elif a["status"] in {"ok", "unconfirmed_type"}:
                inner.append(f'<a style="color:#c2dbf0" href="gtrmedia:{a["id"]}">{label}</a>')
            else:
                inner.append(f"<small>{label} · {escape(ISSUES.get(a['status'], a['status']))}</small>")
        if row["rich"]:
            inner.append('<span style="white-space:pre-wrap">' + row["rich"] + "</span>")
        if meta.get("other_media"):
            inner.append(escape("\n".join(meta["other_media"])).replace("\n", "<br>"))
        if not row["rich"] and any(
            key in meta for key in ("poll", "location_information", "contact_information")
        ):
            inner.append("<i>Структурированное сообщение · подробности в исходном экспорте</i>")
        time = moment.strftime("%H:%M:%S %z") if moment else "Время неизвестно"
        inner.append(
            f'<small style="color:#a2b7b6">{time} &nbsp; · &nbsp; #{escape(row["source_id"])}</small>'
        )
        metrics = QFontMetrics(self.browser.font())
        width = max(180, max((metrics.horizontalAdvance(s[:160]) for s in widths), default=0) + 32)
        width = min(width, max(240, int(self.browser.viewport().width() * 0.78)))
        return (
            f'<table align="{align}" width="{width}" cellpadding="7" cellspacing="0">'
            f'<tr><td bgcolor="{fill}"><a name="m{mid}"></a>'
            + "<br>".join(inner)
            + '</td></tr></table><p style="margin:0;font-size:3pt">&nbsp;</p>'
        )

    def follow(self, url: QUrl):
        data = url.toString().partition(":")[2]
        if not re.fullmatch(r"\d+", data):
            return
        if url.scheme() == "gtrreply":
            with catalogue(self.project) as db:
                target = db.execute("SELECT chat FROM messages WHERE id=?", (int(data),)).fetchone()
            if target:
                self.jump_to(target["chat"], int(data))
            return
        if url.scheme() == "gtrmedia":
            self.open_asset(int(data))

    def open_asset(self, asset_id):
        with catalogue(self.project) as db:
            entry = db.execute(
                """SELECT a.path,a.status,a.kind,a.expected_mime,f.mime,f.sha256,m.meta FROM assets a
                JOIN messages m ON m.id=a.msg LEFT JOIN files f ON f.path=a.path WHERE a.id=?""",
                (asset_id,),
            ).fetchone()
        if not entry:
            return
        if entry["status"] == "metadata":
            key = "contact_information" if entry["kind"] == "contact" else "location_information"
            dialog = QDialog(self)
            dialog.setWindowTitle(KIND_NAMES[entry["kind"]])
            view = QPlainTextEdit(
                json.dumps(json.loads(entry["meta"]).get(key), ensure_ascii=False, indent=2)
            )
            view.setReadOnly(True)
            QVBoxLayout(dialog).addWidget(view)
            dialog.resize(480, 260)
            dialog.exec()
            return
        try:
            if entry["status"] not in {"ok", "unconfirmed_type"}:
                raise RecoveryError("Вложение отсутствует.")
            self.show_media(entry)
        except (RecoveryError, OSError):
            QMessageBox.warning(self, "Вложение", "Файл недоступен или изменился после анализа.")

    def jump_to(self, chat, mid):
        if self.jump:
            self.jump(chat, mid)
            return
        with catalogue(self.project) as db:
            pos = db.execute(
                f"""SELECT pos FROM (SELECT id,row_number() OVER (ORDER BY {ORDER})-1 AS pos
                FROM messages WHERE chat=?) WHERE id=?""",
                (chat, mid),
            ).fetchone()
        if pos:
            self.chat.setCurrentIndex(self.chat.findData(chat))
            self.search.clear()
            self.kind.setCurrentIndex(0)
            self.author.setCurrentIndex(0)
            self.forward_filter.setChecked(False)
            self.offset = pos[0] // PAGE_SIZE * PAGE_SIZE
            self.render()
            self.browser.scrollToAnchor("m" + str(mid))

    def clear_names(self):
        if self.current_names:
            self.current_names.clear()
            self.render()

    def apply_names(self, ids, names):
        self.current_names.update(dict.fromkeys(ids))
        self.current_names.update(names)
        self.render()

    def export_fragment(self):
        selection = self.browser.textCursor().selectedText()
        if not self.project or not selection.strip():
            QMessageBox.information(self, "gTR", tr("Выделите фрагмент переписки в просмотрщике."))
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Сохранить выбранный фрагмент…"), "gTR-fragment.txt", "*.txt")
        if path:
            try:
                dest = local(Path(path))
                with catalogue(self.project) as db:
                    original = local(Path(properties(db)["original"]))
                if any(dest.is_relative_to(root) for root in (self.project, original)):
                    raise RecoveryError("Выберите отдельную папку.")
                publish_text(dest, selection.replace(chr(8233), chr(10)))
            except (OSError, RecoveryError):
                QMessageBox.warning(self, "gTR", tr("Выберите новый файл вне исходных данных."))

    def show_media(self, entry):
        path = local(self.project.joinpath("source", *safe_parts(entry["path"])))
        program = self.ffmpeg_path()
        provider = (
            FFmpegThumbnail(Path(program)) if program and (entry["mime"] or "").startswith("video/") else None
        )

        def inspect_file(progress):
            return preview(
                path, entry["sha256"], entry["expected_mime"], provider=provider, progress=progress
            )

        def ready(result):
            self._show_preview(path, *result)
            return "Предпросмотр подготовлен."

        if self.run_job:
            self.run_job("Подготовка предпросмотра", inspect_file, ready)
        else:
            self._show_preview(
                path, *preview(path, entry["sha256"], entry["expected_mime"], provider=provider)
            )

    def _show_preview(self, path, facts, picture):
        dialog = QDialog(self)
        dialog.setWindowTitle(path.name)
        layout = QVBoxLayout(dialog)
        if not picture.isNull():
            image = QLabel()
            image.setAccessibleName("Предпросмотр вложения")
            image.setPixmap(QPixmap.fromImage(picture))
            layout.addWidget(image)
        title = QLabel(path.name)
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)
        details = [facts.mime or "Тип по содержимому не определён", f"{facts.size:,} байт".replace(",", " ")]
        if facts.width and facts.height:
            details.append(f"{facts.width} × {facts.height} пикселей")
        if facts.duration is not None:
            details.append(f"{facts.duration:g} с")
        if facts.sample_rate:
            details.append(f"{facts.sample_rate} Гц · каналов {facts.channels}")
        details.extend(MEDIA_ISSUES[code] for code in facts.issues)
        info = QLabel(" · ".join(details))
        info.setTextFormat(Qt.TextFormat.PlainText)
        info.setWordWrap(True)
        layout.addWidget(info)

        def reveal():
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

        button = QPushButton("Открыть папку с оригиналом")
        button.clicked.connect(reveal)
        layout.addWidget(button)
        if (
            os.name == "nt"
            and (facts.mime or "").startswith(("audio/", "video/"))
            and path.suffix.lower()
            in {".mp4", ".mov", ".webm", ".ogg", ".opus", ".mp3", ".m4a", ".wav", ".flac"}
        ):
            play = QPushButton("Открыть в проигрывателе")
            play.clicked.connect(lambda: os.startfile(path))
            layout.addWidget(play)
        dialog.exec()
