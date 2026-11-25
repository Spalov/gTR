"""Project lifecycle, reconstruction, SQLite catalogue and aggregate reports."""

import hashlib
import json
import re
import sqlite3
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .archive import snapshot
from .files import digest, local, new_directory, safe_parts
from .media import check_asset
from .model import Cancelled, RecoveryError, quiet
from .parser import discover, parse
from .replies import resolve as resolve_replies

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE properties (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER, sha256 TEXT, mime TEXT, recognized INTEGER);
CREATE TABLE chats (id INTEGER PRIMARY KEY, source_key TEXT, title TEXT, pages INTEGER);
CREATE TABLE messages (id INTEGER PRIMARY KEY, chat INTEGER REFERENCES chats, source_id TEXT,
  sender TEXT, sender_key TEXT, date_raw TEXT, date_iso TEXT, epoch REAL, text TEXT, rich TEXT,
  source TEXT, reply TEXT, reply_peer TEXT, target INTEGER REFERENCES messages, service INTEGER,
  conflict INTEGER DEFAULT 0, meta TEXT);
CREATE UNIQUE INDEX source_identity ON messages(chat,source_id) WHERE source_id <> '';
CREATE INDEX chronology ON messages(chat,epoch,id);
CREATE INDEX source_order ON messages(chat,id);
CREATE TABLE assets (id INTEGER PRIMARY KEY, msg INTEGER REFERENCES messages, kind TEXT, original TEXT,
  path TEXT, status TEXT, expected_size INTEGER, expected_mime TEXT);
CREATE INDEX assets_by_message ON assets(msg);
CREATE TABLE issues (chat INTEGER REFERENCES chats, msg INTEGER REFERENCES messages, level TEXT, code TEXT);
CREATE INDEX issues_by_message ON issues(msg,code);
CREATE TABLE duplicates (chat INTEGER REFERENCES chats, msg INTEGER REFERENCES messages, identical INTEGER,
  extra_record TEXT);
PRAGMA user_version=1;
"""

ISSUES = {
    "missing": "Оригинал вложения отсутствует",
    "empty": "Пустой файл вложения",
    "unsafe": "Недопустимая ссылка на файл",
    "type_mismatch": "Тип файла расходится с описанием",
    "size_mismatch": "Размер файла расходится с описанием",
    "unconfirmed_type": "Тип определён по расширению",
    "unknown_sender": "Источник не указывает автора",
    "invalid_date": "Нет распознанной полной даты и времени",
    "ambiguous_local_time": "Местное время повторяется при переводе часов",
    "nonexistent_local_time": "Местное время попадает в пропущенный интервал при переводе часов",
    "date_conflict": "Исходные даты противоречат друг другу",
    "missing_id": "Отсутствует исходный ID",
    "assumed_timezone": "Применён выбранный часовой пояс",
    "unknown_reply": "Не распознана ссылка ответа",
    "unresolved_reply": "Адресат ответа отсутствует в этом диалоге",
    "duplicate": "Повтор исходного ID",
    "conflict": "У одного исходного ID разные версии",
    "entity_mismatch": "Разметка расходится с текстом",
    "other_media": "Структурированное содержимое доступно в источнике",
    "page_gap": "В нумерации HTML-страниц есть пропуск",
    "service_no_time": "У служебного события нет точного времени",
    "invalid_epoch": "Не распознана метка Unix",
    "invalid_wall_date": "Не распознана текстовая дата",
}
ERRORS = {
    "missing",
    "empty",
    "unsafe",
    "type_mismatch",
    "size_mismatch",
    "unknown_sender",
    "invalid_date",
    "date_conflict",
    "ambiguous_local_time",
    "nonexistent_local_time",
    "conflict",
    "page_gap",
}


def encode(value):
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return re.sub(r"[\ud800-\udfff]", lambda match: f"\\u{ord(match[0]):04x}", data)


def reconstruct(db, chats, snap, progress):
    props = {"sha256": snap.source_hash, "input_bytes": snap.input_size, "source_type": snap.source_type}
    db.executemany("INSERT INTO properties VALUES (?,?)", [(k, str(v)) for k, v in props.items()])
    db.executemany(
        "INSERT INTO files VALUES (?,?,?,?,?)",
        ((p, f.size, f.sha256, f.mime, f.recognized) for p, f in snap.files.items()),
    )
    total = sum(len(c.messages) for c in chats)
    peer_chats = {}
    count = 0
    for chat in chats:
        cid = db.execute(
            "INSERT INTO chats(source_key,title,pages) VALUES (?,?,?)", (chat.key, chat.title, chat.pages)
        ).lastrowid
        if chat.peer_key:
            db.execute("INSERT INTO properties VALUES (?,?)", (f"chat_peer:{cid}", chat.peer_key))
        if chat.peer_key and chat.messages:
            peer_chats.setdefault((chat.source, chat.peer_key), []).append(cid)
        fingerprints = {}
        for ordinal, m in enumerate(chat.messages, 1):
            if count % 100 == 0:
                progress("reconstruct", count, total)
            count += 1
            signature = None
            if m.source_id:
                content = {
                    "author": (m.sender, m.sender_key), "date": (m.date_raw, m.date_iso), "text": m.text,
                    "rich": m.rich, "reply": (m.reply, m.reply_peer), "service": m.service,
                    "assets": [asdict(asset) for asset in m.assets],
                    "meta": {
                        k: v for k, v in m.meta.items()
                        if k not in {"joined", "identity_basis", "reply_href", "date_evidence", "historical_time"}
                    },
                }
                signature = hashlib.sha256(encode(content).encode()).digest()
            if m.source_id and m.source_id in fingerprints:
                orig, prior = fingerprints[m.source_id]
                same = prior == signature
                record = {**asdict(m), "source_ordinal": ordinal}
                db.execute("INSERT INTO duplicates VALUES (?,?,?,?)", (cid, orig, same, encode(record)))
                db.execute(
                    "INSERT INTO issues VALUES (?,?,?,?)",
                    (cid, orig, "info" if same else "error", "duplicate" if same else "conflict"),
                )
                if not same:
                    db.execute("UPDATE messages SET conflict=1 WHERE id=?", (orig,))
                continue
            epoch = datetime.fromisoformat(m.date_iso).timestamp() if m.date_iso else None
            row = db.execute(
                """INSERT INTO messages(chat,source_id,sender,sender_key,date_raw,date_iso,
                epoch,text,rich,source,reply,reply_peer,service,meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cid,
                    m.source_id,
                    m.sender,
                    m.sender_key,
                    m.date_raw,
                    m.date_iso,
                    epoch,
                    m.text,
                    m.rich,
                    m.source,
                    m.reply,
                    m.reply_peer,
                    m.service,
                    encode({**m.meta, "source_ordinal": ordinal}),
                ),
            ).lastrowid
            if m.source_id:
                fingerprints[m.source_id] = (row, signature)
            flags = list(m.flags)
            if not m.service and not m.sender:
                flags.append("unknown_sender")
            if not m.source_id:
                flags.append("missing_id")
            if m.meta.get("other_media"):
                flags.append("other_media")
            for asset in m.assets:
                path, status = check_asset(asset, snap.files)
                db.execute(
                    """INSERT INTO assets(msg,kind,original,path,status,expected_size,expected_mime)
                              VALUES (?,?,?,?,?,?,?)""",
                    (row, asset.kind, asset.original, path, status, asset.expected_size, asset.expected_mime),
                )
                if status not in {"ok", "metadata"}:
                    flags.append(status)
            db.executemany(
                "INSERT INTO issues VALUES (?,?,?,?)",
                [(cid, row, "error" if f in ERRORS else "warning", f) for f in flags],
            )
        if chat.key.startswith("html:"):
            from pathlib import PurePosixPath

            from .parser import PAGE

            root = chat.key[5:]
            numbers = [
                int(match[1] or 1)
                for p in snap.files
                if str(PurePosixPath(p).parent) == root and (match := PAGE.fullmatch(PurePosixPath(p).name))
            ]
            if sorted(numbers) != list(range(1, len(numbers) + 1)):
                db.execute("INSERT INTO issues VALUES (?,NULL,'error','page_gap')", (cid,))
    resolve_replies(db, peer_chats)
    progress("reconstruct", total, total)


def analyze(source: Path, dest: Path, timezone_name="UTC", progress=quiet, *, export_format="auto"):
    source = local(source)
    with new_directory(dest, (source,)) as work:
        if source.is_file() and source.suffix.lower() in {".html", ".json"}:
            from .selected_export import selected_snapshot

            snap, sources = selected_snapshot(source, work / "source", timezone_name, progress)
        else:
            snap = snapshot(source, work / "source", progress)
            sources = discover(snap)
        chats = parse(snap, timezone_name, progress, export_format=export_format, sources=sources, stream=True)
        formats = [fmt for fmt, group in zip(("html", "json"), sources) if group]
        db = sqlite3.connect(work / "history.sqlite")
        try:
            db.executescript(SCHEMA)
            with db:
                reconstruct(db, chats, snap, progress)
                db.executemany(
                    "INSERT INTO properties VALUES (?,?)",
                    [
                        ("original", str(source)),
                        ("timezone", timezone_name),
                        ("available_formats", ",".join(formats)),
                    ],
                )
        finally:
            db.close()
        progress("finish", 1, 1)
    return local(dest)


@contextmanager
def catalogue(project):
    path = local(Path(project) / "history.sqlite")
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        raise RecoveryError("Папка не содержит проекта gTR.") from None
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise RecoveryError("Версия базы проекта не поддерживается.")
        yield db
    except sqlite3.DatabaseError:
        raise RecoveryError("База проекта повреждена или несовместима.") from None
    finally:
        db.close()


def properties(db):
    return dict(db.execute("SELECT name,value FROM properties"))


def summary(project):
    with catalogue(project) as db:
        props = properties(db)

        def scalar(sql):
            return db.execute(sql).fetchone()[0]

        formats = [
            r[0]
            for r in db.execute(
                "SELECT DISTINCT substr(source_key,1,instr(source_key,':')-1) FROM chats ORDER BY 1"
            )
        ]

        def boundary(order):
            row = db.execute(
                f"SELECT date_iso FROM messages WHERE epoch IS NOT NULL AND service=0 ORDER BY epoch {order},id {order} LIMIT 1"
            ).fetchone()
            return row[0] if row else None

        return {
            "source_type": props["source_type"],
            "export_format": "+".join(formats),
            "available_formats": props["available_formats"].split(",")
            if props.get("available_formats")
            else formats,
            "sha256": props["sha256"],
            "input_bytes": int(props["input_bytes"]),
            "extracted_bytes": scalar("SELECT coalesce(sum(size),0) FROM files"),
            "files": scalar("SELECT count(*) FROM files"),
            "pages": scalar("SELECT sum(pages) FROM chats"),
            "chats": scalar("SELECT count(*) FROM chats"),
            "messages": scalar("SELECT count(*) FROM messages WHERE service=0"),
            "service_events": scalar("SELECT count(*) FROM messages WHERE service=1"),
            "participants": scalar(
                "SELECT count(DISTINCT sender_key) FROM messages WHERE sender_key<>'' AND service=0"
            ),
            "replies": scalar("""SELECT count(*) FROM messages m WHERE reply<>'' OR EXISTS
                (SELECT 1 FROM issues WHERE msg=m.id AND code='unknown_reply')"""),
            "resolved_replies": scalar("SELECT count(*) FROM messages WHERE target IS NOT NULL"),
            "duplicates": scalar("SELECT count(*) FROM duplicates"),
            "conflicting_ids": scalar("SELECT sum(conflict) FROM messages") or 0,
            "attachments": dict(db.execute("SELECT kind,count(*) FROM assets GROUP BY kind")),
            "attachment_status": dict(db.execute("SELECT status,count(*) FROM assets GROUP BY status")),
            "media_bytes": scalar(
                "SELECT coalesce(sum(size),0) FROM files WHERE path IN (SELECT path FROM assets)"
            ),
            "unique_media_files": scalar(
                "SELECT count(*) FROM files WHERE path IN (SELECT path FROM assets)"
            ),
            "first": boundary("ASC"),
            "last": boundary("DESC"),
            "issues": dict(db.execute("SELECT code,count(*) FROM issues GROUP BY code")),
            "errors": scalar("SELECT count(*) FROM issues WHERE level='error'"),
        }


def verify(project, progress=quiet, *, problems=None, history_only=False):
    project = local(Path(project))
    counts = Counter()
    with catalogue(project) as db:
        query = (
            """SELECT s.path,f.size,f.sha256 FROM (SELECT DISTINCT source AS path FROM messages) s
                LEFT JOIN files f ON f.path=s.path ORDER BY s.path"""
            if history_only
            else "SELECT path,size,sha256 FROM files ORDER BY path"
        )
        files = db.execute(query).fetchall()
        for i, entry in enumerate(files, 1):
            progress("verify", i, len(files))
            try:
                path = local(project.joinpath("source", *safe_parts(entry["path"])))
                if not path.is_file():
                    state = "missing"
                elif (
                    path.stat().st_size != entry["size"]
                    or digest(path, lambda i=i: progress("verify", i, len(files))) != entry["sha256"]
                ):
                    state = "changed"
                else:
                    state = "ok"
            except Cancelled:
                raise
            except (OSError, RecoveryError):
                state = "unreadable"
            counts[state] += 1
            if state != "ok" and problems is not None:
                problems.append({"path": entry["path"], "status": state})
    return dict(counts)
