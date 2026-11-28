"""Persistent, non-destructive selections over the canonical source ordinals."""

import hashlib
import json
import sqlite3
from bisect import bisect_right
from contextlib import closing, contextmanager
from datetime import date
from pathlib import Path
from uuid import uuid4

from .core import catalogue, encode, properties
from .files import digest, local, private_directory
from .model import RecoveryError, quiet
from .owner import app_home
from .package_errors import PackageValidationError
from .selection import count, decode_intervals, difference, intersection, merge

SCHEMA = """
CREATE TABLE IF NOT EXISTS presets (
    scope TEXT, name TEXT, version TEXT, cursor INTEGER,
    PRIMARY KEY (scope,name)
);
CREATE TABLE IF NOT EXISTS edits (
    scope TEXT, name TEXT, step INTEGER, excluded TEXT,
    PRIMARY KEY (scope,name,step)
);
CREATE TABLE IF NOT EXISTS marks (
    scope TEXT, name TEXT, ordinal INTEGER, note TEXT,
    PRIMARY KEY (scope,name,ordinal)
);
"""
ORDER = "coalesce(json_extract(m.meta,'$.source_ordinal'),row_number() OVER (ORDER BY m.id))"
DEFAULT = ""
MAX_PRESETS = 50
UNDO_DEPTH = 60


def _hash(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


@contextmanager
def _store(write=False):
    folder = local(app_home() / "editor")
    if not folder.is_dir() and write:
        folder.mkdir(parents=True, exist_ok=True)
        private_directory(folder)
    path = local(folder / "selections.sqlite")
    if not path.is_file() and not write:
        yield None
        return
    uri = path.as_uri() + ("?mode=rwc" if write else "?mode=ro")
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        if write:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
        else:
            db.execute("BEGIN")
        with db:
            yield db


def _scope(db, chat):
    row = db.execute("SELECT source_key FROM chats WHERE id=?", (chat,)).fetchone()
    if row is None:
        raise RecoveryError("Диалог отсутствует в проекте.")
    source = properties(db)
    return _hash([1, source["sha256"], source.get("timezone", ""), source.get("source_type"), chat, row[0]])


def _bounds(db, chat):
    last = db.execute(
        """SELECT coalesce(max(json_extract(meta,'$.source_ordinal')),count(*))
           FROM messages WHERE chat=?""", (chat,),
    ).fetchone()[0]
    return ((1, last),) if last else ()


def _read(db, scope, preset):
    if db is None:
        return "initial", 0, (), False, False
    row = db.execute(
        """SELECT p.version,p.cursor,e.excluded,
             EXISTS(SELECT 1 FROM edits WHERE scope=p.scope AND name=p.name AND step=p.cursor-1),
             EXISTS(SELECT 1 FROM edits WHERE scope=p.scope AND name=p.name AND step=p.cursor+1)
           FROM presets p JOIN edits e ON (p.scope,p.name,p.cursor)=(e.scope,e.name,e.step)
           WHERE p.scope=? AND p.name=?""", (scope, preset),
    ).fetchone()
    if row is None:
        if preset != DEFAULT:
            raise RecoveryError("Выборка не найдена.")
        return "initial", 0, (), False, False
    version, cursor, excluded, undo, redo = row
    return version, cursor, decode_intervals(json.loads(excluded)), bool(undo), bool(redo)


def _matches(ranges):
    starts = [first for first, _ in ranges]

    def contains(n):
        if n is None:
            return False
        i = bisect_right(starts, n) - 1
        return i >= 0 and n <= ranges[i][1]

    return contains


def _note(value, limit):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError
    return value


def _records(db, chat, excluded, filters, progress, *, limit=None, before=None):
    query = _note(filters.get("query", ""), 160).casefold()
    field, view = filters.get("field", "all"), filters.get("view", "all")
    start, end, kind = filters.get("start", ""), filters.get("end", ""), filters.get("kind", "")
    if field not in ("all", "text", "sender", "id", "file") or view not in ("all", "included", "excluded"):
        raise ValueError
    if set(filters) - {"query", "field", "view", "start", "end", "kind"}:
        raise ValueError
    if any(value and date.fromisoformat(value).isoformat() != value for value in (start, end)):
        raise ValueError
    if start and end and start > end:
        raise ValueError
    _note(kind, 32)
    db.create_function("excluded", 1, _matches(excluded), deterministic=True)
    db.create_function("fold", 1, str.casefold, deterministic=True)
    where = ["1"]
    values = [chat]
    for condition, value in (("substr(date_iso,1,10)>=?", start), ("substr(date_iso,1,10)<=? AND date_iso<>''", end),
                             ("ordinal<?", before)):
        if value:
            where.append(condition)
            values.append(value)
    if view != "all":
        where.append(f"excluded(ordinal)={int(view == 'excluded')}")
    if query:
        clauses = {
            "text": "instr(fold(text),?)>0", "sender": "instr(fold(sender),?)>0", "id": "source_id=?",
            "file": "EXISTS(SELECT 1 FROM assets a WHERE a.msg=r.id AND instr(fold(a.original),?)>0)",
        }
        keys = tuple(clauses) if field == "all" else (field,)
        where.append("(" + " OR ".join(clauses[key] for key in keys) + ")")
        values.extend(query for _ in keys)
    if kind == "service":
        where.append("service=1")
    elif kind == "text":
        where.append("service=0 AND NOT EXISTS(SELECT 1 FROM assets a WHERE a.msg=r.id)")
    elif kind == "forward":
        where.append("json_extract(meta,'$.forward.is_forwarded')=1")
    elif kind:
        where.append("EXISTS(SELECT 1 FROM assets a WHERE a.msg=r.id AND a.kind=?)")
        values.append(kind)
    sql = f"""WITH r AS (SELECT m.*, {ORDER} AS ordinal FROM messages m WHERE chat=?)
        SELECT id,ordinal,source_id,substr(sender,1,240) AS sender,date_iso AS date,
               substr(text,1,1000) AS text,service,target AS reply,excluded(ordinal) AS excluded,
               json_extract(meta,'$.forward.is_forwarded') AS is_forward,
               substr(json_extract(meta,'$.forward.historical_sender_name'),1,240) AS forwarded,
               json_extract(meta,'$.forward.privacy_state') AS forward_privacy,
               (SELECT count(*) FROM assets a WHERE a.msg=r.id) AS attachments,
               (SELECT min(id) FROM assets a WHERE a.msg=r.id AND status='ok') AS attachment
        FROM r WHERE {" AND ".join(where)} ORDER BY ordinal DESC"""
    if limit:
        sql += " LIMIT ?"
        values.append(limit)
    return _query(db, sql, values, progress)


def _query(db, sql, values, progress):
    stopped = None

    def checkpoint():
        nonlocal stopped
        try:
            progress("selection", 0, 0)
        except RecoveryError as error:
            stopped = error
        return int(stopped is not None)

    db.set_progress_handler(checkpoint, 12000)
    try:
        yield from db.execute(sql, values)
    except sqlite3.OperationalError:
        if stopped is not None:
            raise stopped from None
        raise
    finally:
        db.set_progress_handler(None, 0)


def page(project, chat, preset, filters, progress=quiet, before=None, limit=50):
    with catalogue(project) as source, _store() as db:
        scope = _scope(source, chat)
        version, _, excluded, undo, redo = _read(db, scope, preset)
        rows = [dict(row) for row in _records(source, chat, excluded, filters, progress, limit=limit + 1, before=before)]
        names = [row[0] for row in db.execute(
            "SELECT name FROM presets WHERE scope=? AND name<>'' ORDER BY name", (scope,),
        )] if db is not None else []
        if db is not None:
            notes = {
                row["ordinal"]: db.execute(
                    "SELECT note FROM marks WHERE scope=? AND name=? AND ordinal=?", (scope, preset, row["ordinal"]),
                ).fetchone()
                for row in rows
            }
            for row in rows:
                found = notes[row["ordinal"]]
                row["note"] = found[0] if found else ""
        return {"records": rows[:limit], "before": rows[limit - 1]["ordinal"] if len(rows) > limit else None,
                "revision": version, "undo": undo, "redo": redo, "presets": names}


def edit(project, chat, preset, revision, action, progress=quiet, *, ranges=(), filters=None, name="", note=""):
    with catalogue(project) as source, _store(write=True) as db:
        scope = _scope(source, chat)
        version, step, excluded, undo, redo = _read(db, scope, preset)
        if version != revision:
            raise stale()
        if action == "save_as":
            name = _note(name.strip(), 80)
            amount = db.execute("SELECT count(*) FROM presets WHERE scope=?", (scope,)).fetchone()[0]
            if not name or amount >= MAX_PRESETS or db.execute(
                "SELECT 1 FROM presets WHERE scope=? AND name=?", (scope, name),
            ).fetchone():
                raise RecoveryError("Укажите уникальное имя выборки.")
            db.execute("INSERT INTO presets VALUES (?,?,?,0)", (scope, name, uuid4().hex))
            db.execute("INSERT INTO edits VALUES (?,?,0,?)", (scope, name, encode(excluded)))
            db.execute("INSERT INTO marks SELECT scope,?,ordinal,note FROM marks WHERE scope=? AND name=?",
                       (name, scope, preset))
            return {"preset": name}
        bounds = _bounds(source, chat)
        selected = intersection(decode_intervals(ranges), bounds)
        if filters is not None:
            selected = merge((row["ordinal"], row["ordinal"])
                             for row in _records(source, chat, excluded, filters, progress))
        if action == "mark":
            if count(selected) != 1:
                raise ValueError
            ordinal = selected[0][0]
            if note := _note(note, 400):
                db.execute("INSERT OR REPLACE INTO marks VALUES (?,?,?,?)", (scope, preset, ordinal, note))
            else:
                db.execute("DELETE FROM marks WHERE scope=? AND name=? AND ordinal=?", (scope, preset, ordinal))
            return {"preset": preset}
        if action in ("undo", "redo"):
            delta = -1 if action == "undo" and undo else 1 if action == "redo" and redo else 0
            if delta:
                db.execute("UPDATE presets SET cursor=?,version=? WHERE scope=? AND name=?",
                           (step + delta, uuid4().hex, scope, preset))
            return {"preset": preset}
        if action == "exclude":
            updated = merge((*excluded, *selected))
        elif action == "include":
            updated = difference(excluded, selected)
        elif action == "reset":
            updated = ()
        else:
            raise ValueError
        if updated != excluded:
            if version == "initial":
                db.execute("INSERT INTO edits VALUES (?,?,0,'[]')", (scope, preset))
            db.execute("DELETE FROM edits WHERE scope=? AND name=? AND (step>? OR step<?)",
                       (scope, preset, step, step - UNDO_DEPTH + 1))
            db.execute("INSERT INTO edits VALUES (?,?,?,?)", (scope, preset, step + 1, encode(updated)))
            db.execute("INSERT OR REPLACE INTO presets VALUES (?,?,?,?)", (scope, preset, uuid4().hex, step + 1))
        return {"preset": preset}


def review(project, chat, preset, progress=quiet, *, summarize=True):
    with catalogue(project) as source, _store() as db:
        scope = _scope(source, chat)
        version, _, excluded, _, _ = _read(db, scope, preset)
        ranges = difference(_bounds(source, chat), excluded)
        result = {"schema": 1, "project_id": scope, "preset": preset, "revision": version,
                  "project_sha256": digest(Path(project) / "history.sqlite", lambda: progress("selection", 0, 0)),
                  "ranges_sha256": _hash(ranges)}
        if not summarize:
            return ranges, result, None
        source.create_function("included", 1, _matches(ranges), deterministic=True)
        stats = next(_query(source, f"""
            WITH r AS (SELECT m.id, {ORDER} AS ordinal,m.date_iso,m.target,m.reply,m.service,
                   json_extract(m.meta,'$.album_id') AS album FROM messages m WHERE chat=?),
                 chosen AS (SELECT * FROM r WHERE included(ordinal) AND NOT service),
                 selected_files AS (SELECT a.path,max(f.size) AS size,count(*) AS refs
                           FROM chosen c JOIN assets a ON a.msg=c.id LEFT JOIN files f ON f.path=a.path
                           WHERE a.status<>'metadata' GROUP BY a.path),
                 albums AS (SELECT album FROM r WHERE album IS NOT NULL GROUP BY album
                           HAVING min(included(ordinal))<>max(included(ordinal)))
            SELECT (SELECT count(*) FROM r) AS total,(SELECT count(*) FROM chosen) AS selected,
                   (SELECT count(*) FROM r WHERE NOT included(ordinal)) AS excluded,
                   (SELECT count(*) FROM r WHERE service AND included(ordinal)) AS service,
                   (SELECT coalesce(sum(refs),0) FROM selected_files) AS attachments,
                   (SELECT coalesce(sum(size),0) FROM selected_files) AS source_bytes,
                   (SELECT min(nullif(date_iso,'')) FROM chosen) AS first,
                   (SELECT max(nullif(date_iso,'')) FROM chosen) AS last,
                   (SELECT count(*) FROM chosen c WHERE c.reply<>'' AND
                        NOT EXISTS(SELECT 1 FROM chosen t WHERE t.id=c.target)) AS broken_replies,
                   (SELECT count(*) FROM albums) AS split_albums
            """, (chat,), progress))
    return ranges, result, {**dict(stats), "gaps": max(0, len(ranges) - 1), "partial": bool(excluded)}


def stale():
    return PackageValidationError("SELECTION_STALE", "selection", "selection_snapshot")


def check_snapshot(project, data, progress=quiet):
    snapshot = data.get("selection_snapshot")
    if snapshot is None:
        return
    if project is None:
        raise stale()
    try:
        ranges, current, _ = review(project, data["chat_id"], snapshot["preset"], progress, summarize=False)
    except (KeyError, OSError, sqlite3.Error):
        raise stale() from None
    included = decode_intervals(data["selection"]["ranges"])
    if current != snapshot or difference(included, ranges):
        raise stale()
