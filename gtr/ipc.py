"""Local recovery service over inherited pipes."""

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

from . import __version__, editor
from .conversion import load_bundle, plan_import, prepare
from .core import analyze, catalogue, summary
from .faults import ImportFault
from .filenames import safe_filename
from .files import is_link, local, safe_parts
from .integrity import check_local
from .journal import RecoveryRequired
from .media_engine import PICTURE_TYPES, inspect
from .media_queue import MediaDecisionRequired, MediaDiskSpace, cache_info, clean_cache
from .model import Cancelled, RecoveryError
from .owner import app_home, vault_path
from .package_errors import PackageValidationError
from .reports import plan_summary, redact_verification
from .verification import compare_files

VERSION = 2
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


def media_choices(entries):
    keys = ("key", "group", "name", "source_kind", "prepared_kind", "received_kind", "reason", "recommended", "options")
    return {
        "decisions": [{key: entry[key] for key in keys if key in entry} for entry in entries[:300]],
        "decisions_total": len(entries),
    }


def selection_args(params):
    chat, preset = params["chat"], params.get("preset", "")
    if not number(chat) or preset != "" and not string(preset, 80):
        raise ProtocolError
    return request_path(params["project"]), chat, preset


def selection_page(params, progress):
    before, limit = params.get("before"), params.get("limit", 50)
    filters = params.get("filters", {})
    if ((before is not None and not number(before)) or not number(limit, maximum=60)
            or not isinstance(filters, dict)):
        raise ProtocolError
    try:
        return editor.page(*selection_args(params), filters, progress, before, limit)
    except (TypeError, ValueError) as error:
        raise ProtocolError from error


def selection_edit(params, progress):
    revision, action = params["revision"], params["action"]
    if not string(revision, 32) or not string(action, 16):
        raise ProtocolError
    filters = params.get("filters")
    if filters is not None and not isinstance(filters, dict):
        raise ProtocolError
    try:
        return editor.edit(*selection_args(params), revision, action, progress,
                           ranges=params.get("ranges", ()), filters=filters,
                           name=params.get("name", ""), note=params.get("note", ""))
    except (AttributeError, TypeError, ValueError) as error:
        raise ProtocolError from error


def selection_preview(params, progress):
    _, snapshot, stats = editor.review(*selection_args(params), progress)
    return {"snapshot": snapshot, **stats}


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
    selection = {}
    if "preset" in params:
        ranges, snapshot, stats = editor.review(*selection_args(params), progress)
        options["record_ranges"] = ranges
        selection = {"selection": stats, "snapshot": snapshot}
        if build:
            if params.get("snapshot") != snapshot:
                raise editor.stale()
            options["selection_snapshot"] = snapshot
    if build:
        if params.get("confirmed") is not True:
            raise ProtocolError
        out = local(app_home() / "packages" / uuid4().hex)
        bundle = prepare(project, chat, out, "source", progress, **options)
        return {"path": str(bundle.root), "manifest": bundle.manifest_hash(), "summary": plan_summary(bundle.data), **selection}
    plan = plan_import(project, chat, "source", progress, **options)
    pending = plan.get("media_decisions_required", [])
    return {"summary": plan_summary(plan), **media_choices(pending), **selection}


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
        if params.get("reconcile") is True and len(captures) == 2:
            from .cli import record_evidence

            record_evidence(result)
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
        try:
            rows = db.execute(
                """SELECT id,created,phase,state,messages,media,media_done,start_request_accepted
                   FROM operations WHERE mode='owner' ORDER BY rowid DESC LIMIT 100"""
            ).fetchall()
        except sqlite3.OperationalError:
            # Older databases may use pre-Beta column names. Migrate through the normal journal.
            from .journal import Endpoint, Journal

            Journal(database, Endpoint("local-review"))
            rows = db.execute(
                """SELECT id,created,phase,state,messages,media,media_done,start_request_accepted
                   FROM operations WHERE mode='owner' ORDER BY rowid DESC LIMIT 100"""
            ).fetchall()
    return {"operations": [dict(row) for row in rows]}


def operation_report(params, progress):
    from .headless_owner import operation_report as report

    job = params["operation_id"]
    if not string(job, 32) or type(params.get("private", False)) is not bool:
        raise ProtocolError
    return report(job, params.get("private", False))


def operation_open(params, progress):
    from .headless_owner import operation_open as open_retained

    if not string(params["operation_id"], 32):
        raise ProtocolError
    return open_retained(params["operation_id"], progress)


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


RECOVERY_KEYS = {"media", "profile", "video", "notes", "language", "decisions", "first", "last", "preset"}
METHODS = {
    "project.list": (recent_projects, set(), set()),
    "project.open": (open_project, {"project"}, set()),
    "archive.analyze": (archive_analyze, {"source"}, {"timezone", "format"}),
    "chat.page": (chat_page, {"project", "chat"}, {"query", "before", "limit"}),
    "selection.page": (selection_page, {"project", "chat"}, {"preset", "filters", "before", "limit"}),
    "selection.edit": (selection_edit, {"project", "chat", "revision", "action"}, {"preset", "ranges", "filters", "name", "note"}),
    "selection.preview": (selection_preview, {"project", "chat"}, {"preset"}),
    "recovery.plan": (recovery, {"project", "chat"}, RECOVERY_KEYS),
    "recovery.prepare": (prepare_recovery, {"project", "chat", "confirmed"}, RECOVERY_KEYS | {"snapshot"}),
    "verify.local": (verify_package, {"bundle"}, {"project", "captures", "reconcile"}),
    "operation.list": (operations, set(), set()),
    "operation.report": (operation_report, {"operation_id"}, {"private"}),
    "operation.open": (operation_open, {"operation_id"}, set()),
    "media.inspect": (media_inspect, {"project", "asset"}, set()),
    "storage.info": (storage, set(), set()),
    "cache.clear": (clear_cache, {"confirmed"}, set()),
    "ui.ready": (ui_ready, set(), set()),
    "update.check": (update_check, set(), set()),
    "update.install": (update_install, {"path", "confirmed"}, set()),
    "update.launch": (update_launch, {"path", "confirmed"}, {"project"}),
}


class Service:
    def __init__(self, source, target, owner_factory=None):
        from .headless_owner import OWNER_ROUTES, OwnerFlow

        self.source, self.target = source, target
        self.output = Lock()
        self.cancelled = Event()
        self.worker = None
        self.active = None
        self.ready = False
        self.owner_factory = owner_factory or OwnerFlow
        self.owner_routes = OWNER_ROUTES
        self.owner_loop = None

    def send(self, request, **fields):
        packet = json.dumps({"v": VERSION, "id": request, **fields}, ensure_ascii=True, allow_nan=False).encode("ascii")
        if len(packet) > MAX_FRAME:
            packet = json.dumps({"v": VERSION, "id": request, "error": {"code": "RESULT_TOO_LARGE"}}).encode("ascii")
        with self.output:
            self.target.write(packet + b"\n")
            self.target.flush()

    def dispatch(self, request, method, params):
        last = 0.0
        previous_stage = ""

        def progress(stage, done, total, *, wait_seconds=None):
            nonlocal last, previous_stage
            if self.cancelled.is_set():
                raise Cancelled()
            now = monotonic()
            if stage != previous_stage or wait_seconds is not None or now - last >= 0.15:
                wait = {} if wait_seconds is None else {"wait_seconds": wait_seconds}
                self.send(request, event="progress", stage=stage, done=done, total=total, **wait)
                last, previous_stage = now, stage

        progress.upload_wait = lambda seconds: progress("upload_file_parts", 0, 0, wait_seconds=seconds)
        progress.upload_bytes = lambda event: progress("upload_bytes", event["sent"], event["size"])

        try:
            value = method(params, progress)
            self.send(request, result=value)
        except Cancelled:
            self.send(request, error={"code": "CANCELLED"})
        except ProtocolError:
            self.send(request, error={"code": "INVALID_PARAMS"})
        except PackageValidationError as error:
            self.send(request, error={"code": error.code, "package_issue": error.details(private=True)})
        except ImportFault as error:
            report = error.details(private=True)
            report["component"] = "telegram"
            if error.media_issues:
                report.update(media_choices(error.media_issues))
            self.send(request, error=report)
        except RecoveryRequired as error:
            review = error.review
            self.send(request, error={"code": "REPLAY_BLOCKED", "eligible": review.eligible_count,
                                      "confirmed": review.confirmed, "unknown": review.unknown,
                                      "conflicts": [asdict(c) for c in review.conflicts[:60]]})
        except MediaDecisionRequired as error:
            self.send(request, error={"code": "MEDIA_DECISION_REQUIRED", "count": len(error.entries),
                                      **media_choices(error.entries)})
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
                elif method not in METHODS and method not in self.owner_routes:
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
            if self.owner_loop:
                self.owner_loop.close()
        return 0

    def start(self, request, method, params):
        if method in self.owner_routes:
            required, optional = self.owner_routes[method]
            route = lambda params, progress: self.call_owner(method, params, progress)
        else:
            route, required, optional = METHODS[method]
        if not required <= params.keys() <= required | optional:
            self.send(request, error={"code": "INVALID_PARAMS"})
            return
        self.active = request
        self.cancelled.clear()
        self.worker = Thread(target=self.dispatch, args=(request, route, params), daemon=True)
        self.worker.start()

    def call_owner(self, method, params, progress):
        from .headless_owner import OwnerLoop

        if self.owner_loop is None:
            self.owner_loop = OwnerLoop(self.owner_factory)
        return self.owner_loop.run(method, params, progress, self.cancelled)


if __name__ == "__main__":
    raise SystemExit(Service(sys.stdin.buffer, sys.stdout.buffer).run())
