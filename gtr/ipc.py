"""Version 1 of the local recovery service. Only the parent owns the pipes."""

import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

from . import __version__
from .bindings import AttachmentBindingError
from .conversion import load_bundle, plan_import, prepare
from .core import analyze, catalogue, summary
from .filenames import safe_filename
from .files import is_link, local, safe_parts
from .integrity import check_local
from .media_engine import PICTURE_TYPES, inspect
from .media_queue import MediaDecisionRequired, MediaDiskSpace, cache_info, clean_cache
from .model import Cancelled, RecoveryError
from .owner import app_home, vault_path
from .reports import plan_summary, redact_verification
from .verification import compare_files

VERSION = 1
MAX_FRAME = 1024 * 1024
MAX_ID = 2**53 - 1
PLAYABLE = {"video/mp4", "video/quicktime", "video/webm", "video/x-matroska", "video/x-msvideo",
            "audio/ogg", "audio/mpeg", "audio/aac", "audio/mp4", "audio/wav", "audio/flac"}


class ProtocolError(Exception):
    pass


def unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ProtocolError
    return result


def reject_constant(value):
    raise ProtocolError


def read_request(raw):
    message = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)
    if type(message) is not dict or set(message) != {"v", "id", "method", "params"}:
        raise ProtocolError
    version, request, method, params = (message[key] for key in ("v", "id", "method", "params"))
    if (type(version) is not int or type(request) is not int or not 0 < request <= MAX_ID
            or type(method) is not str or type(params) is not dict):
        raise ProtocolError
    return version, request, method, params


def string(value, maximum=4096):
    return type(value) is str and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value)


def number(value, minimum=1, maximum=2**31 - 1):
    return type(value) is int and minimum <= value <= maximum


def request_path(value):
    if not string(value) or not Path(value).is_absolute():
        raise ProtocolError
    return local(Path(value))


def no_params(params):
    if params:
        raise ProtocolError


def project_info(project, progress):
    details = summary(project)
    with catalogue(project) as db:
        chats = db.execute("SELECT id,substr(title,1,300) AS title FROM chats ORDER BY id LIMIT 201").fetchall()
    progress("project", 1, 1)
    return {
        "path": str(project), "name": project.name,
        "summary": {key: details[key] for key in (
            "messages", "service_events", "unique_media_files", "input_bytes", "first", "last", "errors",
        )},
        "chats": [dict(row) for row in chats[:200]], "more_chats": len(chats) > 200,
    }


def open_project(params, progress):
    return project_info(request_path(params["project"]), progress)


def archive_analyze(params, progress):
    source = request_path(params["source"])
    zone = params.get("timezone", "UTC")
    fmt = params.get("format", "auto")
    if not string(zone, 120) or fmt not in ("auto", "json", "html"):
        raise ProtocolError
    folder = local(app_home() / "projects")
    name = safe_filename(source.stem[:80]) or "export"
    project = analyze(source, folder / f"{name}-{uuid4().hex[:10]}", zone, progress, export_format=fmt)
    return project_info(project, progress)


def recent_projects(params, progress):
    folder = local(app_home() / "projects")
    result = []
    if folder.is_dir():
        for item in folder.iterdir():
            progress("project", 0, 0)
            if not is_link(item) and item.is_dir():
                db = local(item / "history.sqlite")
                if db.is_file():
                    result.append((db.stat().st_mtime_ns, item))
    return {"projects": [{"name": item.name, "path": str(item)}
                         for _, item in sorted(result, reverse=True)[:100]],
            "saved_session": vault_path().is_file()}


def chat_page(params, progress):
    project = request_path(params["project"])
    chat, before, limit = params["chat"], params.get("before", MAX_ID), params.get("limit", 80)
    query = params.get("query", "")
    if (not number(chat) or not number(before, maximum=MAX_ID) or not number(limit, maximum=80)
            or (query != "" and not string(query, 160))):
        raise ProtocolError
    with catalogue(project) as db:
        # VM checkpoints also make an unsuccessful search through a large archive cancellable.
        cancelled = False

        def checkpoint():
            nonlocal cancelled
            try:
                progress("search", 0, 0)
            except Cancelled:
                cancelled = True
            return int(cancelled)

        db.set_progress_handler(checkpoint, 20000)
        db.create_function("fold", 1, str.casefold, deterministic=True)
        try:
            rows = db.execute(
                """SELECT m.id,substr(m.sender,1,240) AS sender,m.date_iso AS date,substr(m.text,1,1000) AS text,
                    m.service,m.target AS reply,
                    json_extract(m.meta,'$.forward.is_forwarded') AS is_forward,
                    substr(json_extract(m.meta,'$.forward.historical_sender_name'),1,240) AS forwarded,
                    json_extract(m.meta,'$.forward.privacy_state') AS forward_privacy,
                    (SELECT min(a.id) FROM assets a WHERE a.msg=m.id AND a.status='ok') AS attachment,
                    (SELECT count(*) FROM assets a WHERE a.msg=m.id) AS attachments
                    FROM messages m WHERE m.chat=? AND m.id<?
                      AND (?='' OR instr(fold(m.text),?)>0 OR instr(fold(m.sender),?)>0)
                    ORDER BY m.id DESC LIMIT ?""",
                (chat, before, query, query.casefold(), query.casefold(), limit + 1),
            ).fetchall()
        finally:
            db.set_progress_handler(None, 0)
            if cancelled:
                raise Cancelled()
    return {"records": [dict(row) for row in rows[:limit]],
            "before": rows[limit - 1]["id"] if len(rows) > limit else None}


def recovery(params, progress, *, build=False):
    project = request_path(params["project"])
    chat = params["chat"]
    mode, profile = params.get("media", "text_media"), params.get("profile", "ios")
    policy = params.get("video", "original")
    notes, language = params.get("notes", "local"), params.get("language", "ru")
    first, last = params.get("first", 1), params.get("last")
    decisions = params.get("decisions", {})
    if (not number(chat) or mode not in ("text_media", "document", "text_only")
            or profile not in ("ios", "android")
            or policy not in ("original", "compatible", "remux", "fidelity", "compact", "document", "skip")
            or notes not in ("local", "compact", "detailed") or language not in ("ru", "uk")
            or not number(first) or (last is not None and not number(last, first)) or type(decisions) is not dict):
        raise ProtocolError
    options = {
        "profile": profile, "media_mode": mode, "video_policy": policy, "first_record": first, "last_record": last,
        "video_decisions": decisions, "annotate": notes != "local",
        "annotation_mode": notes, "annotation_language": language,
    }
    if build:
        if params.get("confirmed") is not True:
            raise ProtocolError
        out = local(app_home() / "packages" / uuid4().hex)
        bundle = prepare(project, chat, out, "source", progress, **options)
        return {"path": str(bundle.root), "summary": plan_summary(bundle.data)}
    plan = plan_import(project, chat, "source", progress, **options)
    pending = plan.get("media_decisions_required", [])
    return {"summary": plan_summary(plan), "decisions": pending[:30], "decisions_total": len(pending)}


def prepare_recovery(params, progress):
    return recovery(params, progress, build=True)


def verify_package(params, progress):
    root = request_path(params["bundle"])
    project = request_path(params["project"]) if "project" in params else None
    captures = params.get("captures", [])
    if type(captures) is not list or len(captures) not in (0, 1, 2):
        raise ProtocolError
    bundle = load_bundle(root, progress)
    if captures:
        result = compare_files(bundle, [request_path(path) for path in captures], project=project, progress=progress)
        return redact_verification(result)
    result = check_local(bundle, project, progress)
    return {**result, "differences": result["differences"][:30]}


def operations(params, progress):
    database = local(app_home() / "imports.sqlite")
    if not database.is_file():
        return {"operations": []}
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA trusted_schema=OFF")
        rows = db.execute(
            """SELECT id,created,phase,state,messages,media,media_done,start_request_accepted
               FROM operations WHERE mode='owner' ORDER BY rowid DESC LIMIT 100"""
        ).fetchall()
    return {"operations": [dict(row) for row in rows]}


def media_inspect(params, progress):
    project = request_path(params["project"])
    asset = params["asset"]
    if not number(asset):
        raise ProtocolError
    with catalogue(project) as db:
        record = db.execute(
            """SELECT a.path,a.kind,f.mime,f.size,f.sha256 FROM assets a JOIN files f ON a.path=f.path
               WHERE a.id=? AND a.status='ok'""",
            (asset,),
        ).fetchone()
    if record is None:
        raise RecoveryError("Вложение не найдено.")
    path = local(project.joinpath("source", *safe_parts(record["path"])))
    facts = inspect(path, expected_hash=record["sha256"], expected_size=record["size"], declared_mime=record["mime"],
                    checkpoint=lambda: progress("inspect_media", 0, 1))
    result = asdict(facts)
    result.pop("sha256", None)
    result.update(role=record["kind"], playback=False, image=False)
    if facts.intact and "corrupt_container" not in facts.issues:
        result.update(path=str(path), playback=facts.mime in PLAYABLE, image=facts.mime in PICTURE_TYPES)
        if result["playback"]:
            from .video import detailed_probe

            result["probe"] = detailed_probe(path, facts.mime, progress)
    return result


def storage(params, progress):
    root = local(app_home())
    totals = {"originals": 0, "projects": 0, "packages": 0, "temporary": 0}

    def walk(folder, kind):
        if not folder.is_dir() or is_link(folder):
            return
        for parent, children, files in os.walk(folder, followlinks=False):
            base = Path(parent)
            children[:] = [name for name in children if not is_link(base / name)]
            for filename in files:
                progress("storage", 0, 0)
                path = base / filename
                if is_link(path):
                    continue
                parts = path.relative_to(folder).parts
                if parts[0].startswith("."):
                    key = "temporary"
                elif kind == "projects" and len(parts) > 2 and parts[1] == "source":
                    key = "originals"
                else:
                    key = kind
                totals[key] += path.stat().st_size

    walk(root / "projects", "projects")
    walk(root / "packages", "packages")
    volumes = shutil.disk_usage(root if root.is_dir() else root.parent if root.parent.is_dir() else root.anchor)
    return {**totals, "cache": cache_info()["size"], "free": volumes.free}


def clear_cache(params, progress):
    if params.get("confirmed") is not True:
        raise ProtocolError
    return clean_cache(progress)


def ui_ready(params, progress):
    from .updates import signal_ready

    signal_ready()
    return {}


def update_check(params, progress):
    from . import updates

    downloaded = updates.find_download()
    installed = updates.installed_update()
    return {
        "download": {"path": str(downloaded.path), "label": downloaded.label} if downloaded else None,
        "installed": {"path": str(installed), "label": installed.name} if installed else None,
    }


def update_install(params, progress):
    from . import updates

    if params.get("confirmed") is not True:
        raise ProtocolError
    update = updates.candidate(request_path(params["path"]))
    if update is None:
        raise RecoveryError("Версия обновления недоступна.")
    return {"path": str(updates.install(update, progress))}


def update_launch(params, progress):
    from . import updates

    path = request_path(params["path"])
    if params.get("confirmed") is not True or updates.installed_update() != path:
        raise ProtocolError
    project = request_path(params["project"]) if "project" in params else None
    progress("launch_update", 0, 0)
    updates.launch(path, project)
    return {}


RECOVERY_KEYS = {"media", "profile", "video", "notes", "language", "decisions", "first", "last"}
METHODS = {
    "project.list": (recent_projects, set(), set()),
    "project.open": (open_project, {"project"}, set()),
    "archive.analyze": (archive_analyze, {"source"}, {"timezone", "format"}),
    "chat.page": (chat_page, {"project", "chat"}, {"query", "before", "limit"}),
    "recovery.plan": (recovery, {"project", "chat"}, RECOVERY_KEYS),
    "recovery.prepare": (prepare_recovery, {"project", "chat", "confirmed"}, RECOVERY_KEYS),
    "verify.local": (verify_package, {"bundle"}, {"project", "captures"}),
    "operation.list": (operations, set(), set()),
    "media.inspect": (media_inspect, {"project", "asset"}, set()),
    "storage.info": (storage, set(), set()),
    "cache.clear": (clear_cache, {"confirmed"}, set()),
    "ui.ready": (ui_ready, set(), set()),
    "update.check": (update_check, set(), set()),
    "update.install": (update_install, {"path", "confirmed"}, set()),
    "update.launch": (update_launch, {"path", "confirmed"}, {"project"}),
}


class Service:
    def __init__(self, source, target):
        self.source, self.target = source, target
        self.output = Lock()
        self.cancelled = Event()
        self.worker = None
        self.active = None
        self.ready = False

    def send(self, request, **fields):
        packet = json.dumps({"v": VERSION, "id": request, **fields}, ensure_ascii=True, allow_nan=False).encode("ascii")
        if len(packet) > MAX_FRAME:
            packet = json.dumps({"v": VERSION, "id": request, "error": {"code": "RESULT_TOO_LARGE"}}).encode("ascii")
        with self.output:
            self.target.write(packet + b"\n")
            self.target.flush()

    def dispatch(self, request, method, params):
        last = 0.0

        def progress(stage, done, total):
            nonlocal last
            if self.cancelled.is_set():
                raise Cancelled()
            now = monotonic()
            if now - last >= 0.15:
                self.send(request, event="progress", stage=stage, done=done, total=total)
                last = now

        try:
            value = method(params, progress)
            self.send(request, result=value)
        except Cancelled:
            self.send(request, error={"code": "CANCELLED"})
        except ProtocolError:
            self.send(request, error={"code": "INVALID_PARAMS"})
        except AttachmentBindingError:
            self.send(request, error={"code": "UNRESOLVED_ATTACHMENT"})
        except MediaDecisionRequired as error:
            self.send(request, error={"code": "MEDIA_DECISION_REQUIRED", "count": len(error.entries)})
        except MediaDiskSpace:
            self.send(request, error={"code": "DISK_SPACE"})
        except MemoryError:
            self.send(request, error={"code": "MEMORY_LIMIT"})
        except OSError as error:
            self.send(request, error={"code": "DISK_SPACE" if error.errno == 28 or getattr(error, "winerror", 0) == 112 else "IO_ERROR"})
        except (RecoveryError, sqlite3.Error):
            self.send(request, error={"code": "RECOVERY_ERROR"})
        except Exception as error:  # noqa: BLE001 - isolate failures without disclosing local paths
            print("gTR service:", type(error).__name__, file=sys.stderr)
            self.send(request, error={"code": "INTERNAL_ERROR"})

    def run(self):
        last_request = 0
        try:
            while raw := self.source.readline(MAX_FRAME + 2):
                request = None
                if len(raw) > MAX_FRAME + 1 or not raw.endswith(b"\n"):
                    self.send(request, error={"code": "FRAME_LIMIT"})
                    return 2
                try:
                    version, request, method, params = read_request(raw)
                except (ValueError, TypeError, UnicodeError, RecursionError, ProtocolError):
                    self.send(request, error={"code": "INVALID_REQUEST"})
                    continue
                if request <= last_request:
                    self.send(request, error={"code": "INVALID_REQUEST"})
                    continue
                last_request = request
                if version != VERSION:
                    self.send(request, error={"code": "VERSION_MISMATCH"})
                elif method == "hello":
                    no = bool(params)
                    self.ready = not no
                    if no:
                        self.send(request, error={"code": "INVALID_PARAMS"})
                    else:
                        self.send(request, result={"protocol": VERSION, "release": __version__})
                elif not self.ready:
                    self.send(request, error={"code": "HANDSHAKE_REQUIRED"})
                elif method == "cancel":
                    if set(params) != {"target"} or not number(params["target"], maximum=MAX_ID):
                        self.send(request, error={"code": "INVALID_PARAMS"})
                        continue
                    active = bool(self.worker and self.worker.is_alive() and params["target"] == self.active)
                    if active:
                        self.cancelled.set()
                    self.send(request, result={"cancelling": active})
                elif method not in METHODS:
                    self.send(request, error={"code": "UNKNOWN_METHOD"})
                elif self.worker and self.worker.is_alive():
                    # The completed response may already be queued in the pipe while its thread exits.
                    self.worker.join(0.01)
                    if self.worker.is_alive():
                        self.send(request, error={"code": "BUSY"})
                        continue
                    self.start(request, method, params)
                else:
                    self.start(request, method, params)
        finally:
            self.cancelled.set()
            if self.worker:
                self.worker.join(3)
        return 0

    def start(self, request, method, params):
        route, required, optional = METHODS[method]
        if not required <= params.keys() <= required | optional:
            self.send(request, error={"code": "INVALID_PARAMS"})
            return
        self.active = request
        self.cancelled.clear()
        self.worker = Thread(target=self.dispatch, args=(request, route, params), daemon=True)
        self.worker.start()


if __name__ == "__main__":
    raise SystemExit(Service(sys.stdin.buffer, sys.stdout.buffer).run())
