"""Durable operation outcomes and one writer per destination."""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from .files import local
from .model import RecoveryError
from .selection import (
    Intervals,
    count,
    decode_intervals,
    difference,
    intersection,
    merge,
)


class State(StrEnum):
    IDLE = "IDLE"
    VALIDATING = "VALIDATING"
    READY = "READY"
    AWAITING_USER = "AWAITING_USER"
    CONFIRMED = "CONFIRMED"
    INITIALIZING = "INITIALIZING"
    UPLOADING = "UPLOADING"
    STARTING = "STARTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNSUPPORTED = "UNSUPPORTED"
    STAGED_NOT_STARTED = "STAGED_NOT_STARTED"
    START_UNCERTAIN = "START_UNCERTAIN"
    FAILED_BEFORE_START = "FAILED_BEFORE_START"
    START_REJECTED = "START_REJECTED"
    VERIFICATION_MISMATCH = "VERIFICATION_MISMATCH"


class Phase(StrEnum):
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    PREFLIGHT = "PREFLIGHT"
    AUTHORIZED = "AUTHORIZED"
    INITIALIZED = "INITIALIZED"
    UPLOADING = "UPLOADING"
    STARTING = "STARTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PARTIAL = "PARTIAL"
    UNKNOWN_SERVER_STATE = "UNKNOWN_SERVER_STATE"


PHASE_TRANSITIONS = {
    Phase.CREATED: {Phase.VALIDATED, Phase.FAILED, Phase.CANCELLED},
    Phase.VALIDATED: {Phase.PREFLIGHT, Phase.FAILED, Phase.CANCELLED},
    Phase.PREFLIGHT: {Phase.AUTHORIZED, Phase.FAILED, Phase.CANCELLED},
    Phase.AUTHORIZED: {Phase.INITIALIZED, Phase.PARTIAL, Phase.FAILED, Phase.CANCELLED},
    Phase.INITIALIZED: {Phase.UPLOADING, Phase.PARTIAL},
    Phase.UPLOADING: {Phase.STARTING, Phase.PARTIAL},
    Phase.STARTING: {Phase.VERIFYING, Phase.FAILED, Phase.UNKNOWN_SERVER_STATE},
    Phase.UNKNOWN_SERVER_STATE: {Phase.VERIFYING},
    Phase.VERIFYING: {Phase.COMPLETED, Phase.PARTIAL},
    Phase.PARTIAL: {Phase.VERIFYING},
}
PHASE_NAMES = {
    Phase.CREATED: "Создано",
    Phase.VALIDATED: "Пакет проверен",
    Phase.PREFLIGHT: "Проверка Telegram",
    Phase.AUTHORIZED: "Разрешено владельцем",
    Phase.INITIALIZED: "Telegram подготовил импорт",
    Phase.UPLOADING: "Передача вложений",
    Phase.STARTING: "Ответ на запуск ожидается",
    Phase.VERIFYING: "Ожидается сверка",
    Phase.COMPLETED: "Подтверждено сверкой",
    Phase.FAILED: "Ошибка",
    Phase.CANCELLED: "Отменено",
    Phase.PARTIAL: "Частичный результат",
    Phase.UNKNOWN_SERVER_STATE: "Состояние Telegram неизвестно",
}


def phase_of(state, stage):
    if state == State.VALIDATING:
        if stage == "validated":
            return Phase.VALIDATED
        return Phase.PREFLIGHT if stage.startswith("check_") else Phase.CREATED
    if state == State.INITIALIZING:
        return Phase.INITIALIZED if stage == "initialized" else Phase.AUTHORIZED
    return {
        State.IDLE: Phase.CREATED,
        State.READY: Phase.PREFLIGHT,
        State.AWAITING_USER: Phase.PREFLIGHT,
        State.CONFIRMED: Phase.AUTHORIZED,
        State.UPLOADING: Phase.UPLOADING,
        State.STARTING: Phase.STARTING,
        State.VERIFYING: Phase.VERIFYING,
        State.COMPLETED: Phase.COMPLETED,
        State.PARTIAL: Phase.PARTIAL,
        State.STAGED_NOT_STARTED: Phase.PARTIAL,
        State.VERIFICATION_MISMATCH: Phase.PARTIAL,
        State.CANCELLED: Phase.CANCELLED,
        State.START_UNCERTAIN: Phase.UNKNOWN_SERVER_STATE,
    }.get(state, Phase.FAILED)


TRANSITIONS = {
    State.IDLE: {State.VALIDATING, State.CANCELLED},
    State.VALIDATING: {State.READY, State.FAILED, State.UNSUPPORTED, State.CANCELLED},
    State.READY: {State.AWAITING_USER, State.CONFIRMED, State.CANCELLED, State.FAILED},
    State.AWAITING_USER: {State.CONFIRMED, State.CANCELLED, State.FAILED},
    State.CONFIRMED: {State.INITIALIZING, State.FAILED, State.CANCELLED, State.FAILED_BEFORE_START},
    State.INITIALIZING: {
        State.UPLOADING,
        State.STAGED_NOT_STARTED,
        State.FAILED_BEFORE_START,
        State.CANCELLED,
    },
    State.UPLOADING: {State.STARTING, State.STAGED_NOT_STARTED},
    State.STARTING: {State.VERIFYING, State.START_UNCERTAIN, State.START_REJECTED},
    State.VERIFYING: {State.COMPLETED, State.VERIFICATION_MISMATCH},
    State.VERIFICATION_MISMATCH: {State.VERIFYING, State.COMPLETED},
    State.PARTIAL: {State.VERIFYING, State.COMPLETED},
}
INITIALIZED = """
CREATE TABLE IF NOT EXISTS operations(
 id TEXT PRIMARY KEY, created TEXT NOT NULL, peer TEXT NOT NULL, account INTEGER, mode TEXT NOT NULL,
 fingerprint TEXT NOT NULL, scope TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, state TEXT NOT NULL,
 stage TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT '', server_id INTEGER, retry_after TEXT NOT NULL DEFAULT '',
 messages INTEGER NOT NULL, media INTEGER NOT NULL, skipped INTEGER NOT NULL, duplicates INTEGER NOT NULL,
 media_done INTEGER NOT NULL DEFAULT 0, error_code TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS operation_scope ON operations(peer, scope, outcome);
CREATE TABLE IF NOT EXISTS operation_events(
 seq INTEGER PRIMARY KEY, operation TEXT NOT NULL, ts TEXT NOT NULL, state TEXT NOT NULL,
 stage TEXT NOT NULL, done INTEGER NOT NULL, total INTEGER NOT NULL, code TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS destination_locks(peer TEXT PRIMARY KEY, operation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS test_import_resets(
 id INTEGER PRIMARY KEY, peer TEXT NOT NULL, account INTEGER NOT NULL,
 other_account INTEGER NOT NULL, verified_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS test_resets_by_peer ON test_import_resets(peer,id);
CREATE TABLE IF NOT EXISTS test_reset_exclusions(
 reset_id INTEGER NOT NULL, operation TEXT NOT NULL, PRIMARY KEY(reset_id,operation));
CREATE TABLE IF NOT EXISTS operation_workers(operation TEXT PRIMARY KEY, pid INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS operation_record_evidence(
 operation TEXT PRIMARY KEY, selection TEXT, confirmed TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS media_attempts (
 operation TEXT NOT NULL, item_index INTEGER NOT NULL, details TEXT NOT NULL,
 stage TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
 status TEXT NOT NULL, error_code TEXT, response_constructor TEXT,
 PRIMARY KEY(operation,item_index));
"""
OPERATION_COLUMNS = {
    "test_reset_id": "INTEGER",
    "app_version": "TEXT NOT NULL DEFAULT ''",
    "content_fingerprint": "TEXT NOT NULL DEFAULT ''",
    "transcript_upload_done": "INTEGER NOT NULL DEFAULT 0",
    "start_request_sent": "INTEGER DEFAULT 0",
    "start_request_accepted": "INTEGER NOT NULL DEFAULT 0",
    "server_messages_observed": "INTEGER",
    "client_messages_verified": "INTEGER",
    "phase": "TEXT NOT NULL DEFAULT ''",
    "selection": "TEXT",
    "selected_messages": "INTEGER",
    "validated_messages": "INTEGER NOT NULL DEFAULT 0",
    "accuracy": "TEXT",
}
EVENT_COLUMNS = {"phase": "TEXT NOT NULL DEFAULT ''"}
WORKER_COLUMNS = {"process_started_at": "TEXT NOT NULL DEFAULT ''", "disconnected_at": "TEXT"}
_KEEP = object()
PRE_START_STAGES = {
    "confirmed",
    "upload_transcript",
    "transcript_uploaded",
    "init_import",
    "initialized",
    "upload",
}
PRE_START_STATES = {
    State.CONFIRMED,
    State.INITIALIZING,
    State.UPLOADING,
    State.PARTIAL,
    State.STAGED_NOT_STARTED,
}


def redact_attempt(attempt: dict) -> dict:
    return {
        key: value for key, value in attempt.items() if key not in {"source_id", "sha256", "archive_paths", "name"}
    }


def safe_constructor(value):
    return value if isinstance(value, str) and re.fullmatch(r"MessageMedia[A-Za-z]{1,40}", value) else None


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def worker_exited(pid: int) -> bool:
    if os.name == "nt":
        import pywintypes
        import win32api
        import win32event

        try:
            worker = win32api.OpenProcess(0x00100000, False, pid)
            try:
                return win32event.WaitForSingleObject(worker, 0) == win32event.WAIT_OBJECT_0
            finally:
                worker.Close()
        except pywintypes.error as error:
            return error.winerror == 87  # ERROR_INVALID_PARAMETER, a missing process ID
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def worker_signature(pid: int) -> str:
    if os.name != "nt":
        return ""
    import pywintypes
    import win32api
    import win32process

    try:
        worker = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        try:
            return win32process.GetProcessTimes(worker)["CreationTime"].isoformat()
        finally:
            worker.Close()
    except pywintypes.error:
        return ""


def worker_stopped(row) -> bool:
    if row["disconnected_at"] or worker_exited(row["pid"]):
        return True
    current = worker_signature(row["pid"])
    return bool(current and row["process_started_at"] and current != row["process_started_at"])


@dataclass(frozen=True)
class Endpoint:
    key: str
    account_id: int | None = None
    aliases: tuple[str, ...] = ()

    @classmethod
    def telegram(cls, account_id: int, peer_id: int, kind: str) -> "Endpoint":
        if kind == "pm":
            low, high = sorted((account_id, peer_id))
            return cls(f"pm:{low}/{high}", account_id, (f"{account_id}/{peer_id}", f"{peer_id}/{account_id}"))
        return cls(f"group:{peer_id}", account_id, (f"{account_id}/{peer_id}",))


@dataclass(frozen=True)
class RecoveryConflict:
    operation_id: str
    phase: str
    overlap: int | None
    can_attach: bool = False
    can_recover: bool = False


@dataclass(frozen=True)
class RecoveryReview:
    conflicts: tuple[RecoveryConflict, ...]
    selected: int
    overlap: int | None
    confirmed: int
    eligible_count: int
    unknown: int
    eligible: Intervals
    confirmed_records: Intervals
    unknown_records: Intervals
    decisions: dict = field(default_factory=dict)


class RecoveryRequired(RecoveryError):
    def __init__(self, review: RecoveryReview, *, locked=False):
        self.review = review
        super().__init__(
            "В журнале Alpha есть запись о предыдущей попытке импорта без установленного состава."
            if any(not c.operation_id for c in review.conflicts)
            else "Для диалога есть активная или неопределённая операция. Проверьте её состояние."
            if locked
            else "Выбранные записи уже связаны с предыдущим импортом. Посмотрите совпадения и варианты восстановления."
        )


def _record_ranges(raw, expected=None):
    try:
        ranges = decode_intervals(json.loads(raw)) if raw is not None else None
        if ranges is not None and (expected is None or count(ranges) == expected):
            return ranges
    except (ValueError, TypeError):
        pass
    return None


def _terminal_unknown(operation):
    return operation["state"] == State.START_UNCERTAIN and operation["outcome"] == "uncertain"


class Journal:
    def __init__(self, path: Path, destination: str | Endpoint, aliases=(), *, test_reset_id=None):
        self.path = local(Path(path))
        self.endpoint = (
            destination
            if isinstance(destination, Endpoint)
            else Endpoint(destination, aliases=tuple(aliases))
        )
        self.test_reset_id = test_reset_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect(lock=True) as db:
            for statement in INITIALIZED.split(";"):
                if statement.strip():
                    db.execute(statement)
            new_counters = self._extend(db, "operations", OPERATION_COLUMNS)
            self._extend(db, "operation_workers", WORKER_COLUMNS)
            new_events = self._extend(db, "operation_events", EVENT_COLUMNS)
            if "start_request_sent" in new_counters:
                self._backfill(db)
            if "phase" in new_counters:
                self._backfill_phases(db, "operations", "id")
            if "phase" in new_events:
                self._backfill_phases(db, "operation_events", "seq")
            from .incremental_import import migrate

            migrate(db)

    def latest_test_reset(self, db=None) -> int:
        if db is None:
            with self.connect() as transaction:
                return self.latest_test_reset(transaction)
        return db.execute(
            "SELECT coalesce(max(id),0) FROM test_import_resets WHERE peer=?", (self.endpoint.key,)
        ).fetchone()[0]

    def _check_test_scope(self, db):
        if self.test_reset_id is None:
            return
        pair = re.fullmatch(r"pm:([1-9]\d*)/([1-9]\d*)", self.endpoint.key)
        if (
            not pair
            or int(pair[1]) >= int(pair[2])
            or self.endpoint.account_id not in map(int, pair.groups())
        ):
            raise RecoveryError("Тестовый режим доступен для личного диалога двух аккаунтов.")
        if type(self.test_reset_id) is not int or self.test_reset_id != self.latest_test_reset(db):
            raise RecoveryError("Тестовый цикл изменился. Подготовьте пакет заново в профиле импорта.")

    @contextmanager
    def connect(self, *, lock=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute("BEGIN IMMEDIATE" if lock else "BEGIN")
                yield db
        finally:
            db.close()

    @staticmethod
    def _extend(db, table, column_types):
        columns = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
        added = set(column_types) - columns
        for column in added:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_types[column]}")
        return added

    @staticmethod
    def _backfill(db):
        db.execute("UPDATE operations SET transcript_upload_done=1 WHERE server_id IS NOT NULL")
        db.execute("""UPDATE operations SET media_done=max(media_done,COALESCE((SELECT max(done)
            FROM operation_events WHERE operation=operations.id AND state='UPLOADING' AND stage='upload'),0))""")
        db.execute("""UPDATE operations SET start_request_sent=1,start_request_accepted=1 WHERE id IN (
            SELECT operation FROM operation_events WHERE state='VERIFYING' AND stage='verifying')""")
        db.execute("""UPDATE operations SET start_request_sent=NULL WHERE start_request_accepted=0
            AND EXISTS (SELECT 1 FROM operation_events WHERE operation=operations.id AND state='STARTING')""")

    @staticmethod
    def _backfill_phases(db, table, key):
        # Backfill only the milestone implied by each old row. Do not create historical events.
        values = [
            (phase_of(row["state"], row["stage"]), row[key])
            for row in db.execute(f"SELECT {key},state,stage FROM {table}")
        ]
        db.executemany(f"UPDATE {table} SET phase=? WHERE {key}=?", values)

    def _review(
        self,
        db,
        fingerprint,
        scope,
        content_fingerprint,
        selection,
        selected,
        *,
        indexed=False,
        verify_previous=True,
    ):
        self._check_test_scope(db)
        key = self.endpoint.key
        active = db.execute(
            """SELECT l.operation,o.state,o.outcome,
                  EXISTS(SELECT 1 FROM test_reset_exclusions x WHERE x.reset_id=? AND x.operation=l.operation) AS reset
               FROM destination_locks l
               LEFT JOIN operations o ON o.id=l.operation AND o.peer=l.peer WHERE l.peer=?""",
            (self.test_reset_id, key),
        ).fetchone()
        if active and active["reset"] and _terminal_unknown(active):
            active = None
        locked_id = active["operation"] if active else None
        defer_verification = indexed and selection is not None and not verify_previous
        lock_all = bool(active and not (defer_verification and _terminal_unknown(active)))
        processed = db.execute(
            """SELECT o.*,e.selection AS linked_selection,e.confirmed,w.pid,w.process_started_at,w.disconnected_at
               FROM operations o LEFT JOIN operation_record_evidence e ON e.operation=o.id
               LEFT JOIN operation_workers w ON w.operation=o.id
               WHERE o.peer=? AND NOT EXISTS (SELECT 1 FROM test_reset_exclusions x
                 WHERE x.reset_id=? AND x.operation=o.id)
               AND (o.id=? OR o.outcome='uncertain' OR (o.outcome='accepted'
                 AND (o.fingerprint IN (?,?) OR o.content_fingerprint IN (?,?) OR o.scope=?)))
               ORDER BY o.id=? DESC,o.rowid DESC""",
            (
                key,
                self.test_reset_id,
                locked_id,
                fingerprint,
                content_fingerprint,
                fingerprint,
                content_fingerprint,
                scope,
                locked_id,
            ),
        )
        current = selection or ()
        conflicts, overlapping, confirmed = [], [], []
        unknown_coverage = selection is None
        for prior in processed:
            holds_dialog = (prior["id"] == locked_id or prior["outcome"] == "uncertain") and not (
                defer_verification and _terminal_unknown(prior)
            )
            if (
                indexed
                and prior["scope"] != scope
                and not holds_dialog
                and prior["messages"] > 0
                and db.execute(
                    "SELECT count(*) FROM registry_records WHERE operation=?", (prior["id"],)
                ).fetchone()[0]
                == prior["messages"]
            ):
                # Across exports a complete record index supplies the overlap in classify().
                continue
            old = _record_ranges(prior["linked_selection"] or prior["selection"], prior["messages"])
            shared = (
                intersection(current, old)
                if selection is not None and old is not None and prior["scope"] == scope
                else None
            )
            if shared == () and not holds_dialog and prior["fingerprint"] != fingerprint:
                continue
            # An exact package identity remains blocked even if a damaged journal disagrees.
            if shared == () and prior["fingerprint"] == fingerprint and current:
                shared = None
            if holds_dialog:
                lock_all = True
            if shared is None:
                unknown_coverage = True
            else:
                overlapping.extend(shared)
                if prior["state"] == State.COMPLETED and prior["outcome"] == "accepted":
                    confirmed.extend(shared)
                else:
                    confirmed.extend(intersection(shared, _record_ranges(prior["confirmed"]) or ()))
            can_recover = bool(
                holds_dialog
                and prior["id"] == locked_id
                and prior["mode"] == "owner"
                and prior["pid"]
                and prior["stage"] in PRE_START_STAGES
                and prior["state"] in PRE_START_STATES
                and not self._has_start_boundary(db, prior["id"])
                and worker_stopped(prior)
            )
            conflicts.append(
                RecoveryConflict(
                    prior["id"],
                    prior["phase"],
                    count(shared) if shared is not None else None,
                    old is None and prior["scope"] == scope and bool(prior["manifest_sha256"]),
                    can_recover,
                )
            )
        if active and lock_all and not any(c.operation_id == locked_id for c in conflicts):
            conflicts.insert(0, RecoveryConflict(locked_id, Phase.UNKNOWN_SERVER_STATE, None))
            unknown_coverage = True
        shared = merge(overlapping)
        proven = merge(confirmed)
        held = current if conflicts and (lock_all or unknown_coverage) else shared
        eligible = difference(current, held)
        unknown = difference(held, proven)
        return RecoveryReview(
            tuple(conflicts),
            selected,
            None if conflicts and unknown_coverage else count(shared),
            count(proven),
            count(eligible) if selection is not None else (0 if conflicts else selected),
            count(unknown) if selection is not None else (selected if conflicts else 0),
            eligible,
            proven,
            unknown,
        ), lock_all

    def review_bundle(self, bundle):
        from .incremental_import import classify
        with self.connect() as db:
            return classify(self, db, bundle)[0]

    def attach_selection(self, job, bundle):
        """Attach exact known package provenance without changing historical journal rows."""
        from .conversion import load_bundle

        checked = load_bundle(bundle.root)
        selected = checked.record_intervals()
        with self.connect(lock=True) as db:
            row = db.execute(
                "SELECT * FROM operations WHERE id=? AND peer=?", (job, self.endpoint.key)
            ).fetchone()
            if (
                row is None
                or selected is None
                or checked.scope() != row["scope"]
                or checked.fingerprint() != row["fingerprint"]
                or checked.manifest_hash() != row["manifest_sha256"]
                or count(selected) != row["messages"]
            ):
                raise RecoveryError("Этот пакет не совпадает с сохранённым пакетом операции.")
            existing = _record_ranges(row["selection"], row["messages"])
            if existing is not None and existing != selected:
                raise RecoveryError("Выборка пакета расходится с журналом операции.")
            db.execute(
                """INSERT INTO operation_record_evidence(operation,selection) VALUES (?,?)
                       ON CONFLICT(operation) DO UPDATE SET selection=excluded.selection""",
                (job, json.dumps(selected, separators=(",", ":"))),
            )
            from .incremental_import import register

            register(db, job, checked)
        from .incremental_import import retain

        retain(self, checked)

    def _assert_available(
        self, db, fingerprint, scope, content_fingerprint=None, selection=None, selected=None, *, bundle=None, exclude=""
    ):
        if bundle is None:
            review, locked = self._review(
                db,
                fingerprint,
                scope,
                content_fingerprint,
                selection,
                selected if selected is not None else count(selection or ()),
            )
        else:
            from .incremental_import import classify

            review, locked = classify(self, db, bundle, exclude=exclude)
        if locked or review.conflicts:
            raise RecoveryRequired(review, locked=locked)
        key = self.endpoint.key
        waiting = db.execute(
            """SELECT max(retry_after) FROM operations
            WHERE peer=? OR (account=? AND error_code='FLOOD_WAIT')""",
            (key, self.endpoint.account_id),
        ).fetchone()[0]
        if waiting and waiting > timestamp():
            raise RecoveryError(f"Действует полученный от Telegram интервал ожидания до {waiting} (UTC).")
        # Respect an Alpha 0.1/0.2 guard if the user has one.
        if (
            not self.test_reset_id
            and db.execute("SELECT 1 FROM sqlite_master WHERE name='starts' AND type='table'").fetchone()
        ):
            has_plans = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='plans' AND type='table'"
            ).fetchone()
            for old in (key, *self.endpoint.aliases):
                starts = db.execute(
                    """SELECT 1 FROM starts s WHERE peer=? AND fingerprint IN (?,?) AND
                    (?=0 OR NOT EXISTS (SELECT 1 FROM alpha_links a WHERE a.peer=s.peer AND a.fingerprint=s.fingerprint))""",
                    (old, fingerprint, content_fingerprint, bundle is not None),
                ).fetchone()
                known = (
                    has_plans
                    and db.execute(
                        """SELECT 1 FROM plans p WHERE peer=? AND scope=? AND NOT EXISTS
                         (SELECT 1 FROM alpha_links a WHERE a.peer=p.peer AND a.fingerprint=p.fingerprint)""",
                        (old, scope),
                    ).fetchone()
                )
                if starts or known:
                    raise RecoveryError("Для этого источника есть запись о предыдущей попытке импорта.")

    def check(self, fingerprint: str, scope: str, content_fingerprint=None, selection=None, *, selected=None, bundle=None):
        with self.connect() as db:
            if bundle is None:
                from .conversion import Bundle

                row = db.execute(
                    "SELECT manifest FROM import_packages WHERE sha=?", (fingerprint,)
                ).fetchone()
                if row:
                    recorded = Bundle(self.path.parent, json.loads(row[0]))
                    if recorded.scope() == scope:
                        bundle = recorded
            self._assert_available(db, fingerprint, scope, content_fingerprint, selection, selected, bundle=bundle)

    def create(self, bundle, mode, scope) -> str:
        from . import __version__
        from .incremental_import import register
        job = uuid4().hex
        selection = bundle.record_intervals()
        with self.connect(lock=True) as db:
            self._check_test_scope(db)
            if self.test_reset_id is not None and bundle.data.get("test_reset_id") != self.test_reset_id:
                raise RecoveryError("Пакет подготовлен для другого режима или тестового цикла.")
            db.execute(
                """INSERT INTO operations(id,created,peer,account,mode,fingerprint,scope,manifest_sha256,
                state,stage,messages,media,skipped,duplicates,content_fingerprint,phase,selection,selected_messages,
                test_reset_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job,
                    timestamp(),
                    self.endpoint.key,
                    self.endpoint.account_id,
                    mode,
                    bundle.fingerprint(),
                    scope,
                    bundle.manifest_hash(),
                    State.IDLE,
                    "idle",
                    bundle.data["included"],
                    len(bundle.data["media"]),
                    bundle.data["skipped"],
                    bundle.data.get("source_diagnostics", {}).get("duplicates", 0),
                    bundle.content_fingerprint(),
                    Phase.CREATED,
                    json.dumps(selection, separators=(",", ":")) if selection is not None else None,
                    bundle.data.get("selected_records"),
                    self.test_reset_id,
                ),
            )
            self._event(db, job, State.IDLE, "idle", 0, 0, "")
            db.execute("UPDATE operations SET app_version=? WHERE id=?", (__version__, job))
            register(db, job, bundle)
        return job

    def _event(self, db, job, state, stage, done, total, code):
        db.execute(
            "INSERT INTO operation_events(operation,ts,state,stage,done,total,code,phase) VALUES (?,?,?,?,?,?,?,?)",
            (job, timestamp(), state, stage, done, total, code, phase_of(state, stage)),
        )

    def transition(
        self,
        job: str,
        state: State,
        stage: str,
        *,
        done=0,
        total=0,
        fault=None,
        server_id=None,
        reserve=False,
        release=False,
        outcome=None,
        transcript_uploaded=False,
        start_sent=_KEEP,
        start_accepted=False,
    ):
        with self.connect(lock=True) as db:
            row = db.execute(
                "SELECT * FROM operations WHERE id=? AND peer=?", (job, self.endpoint.key)
            ).fetchone()
            if not row or (
                state != row["state"] and state not in TRANSITIONS.get(State(row["state"]), set())
            ):
                raise RecoveryError("Недопустимый переход состояния импорта.")
            phase = phase_of(state, stage)
            if phase != row["phase"] and phase not in PHASE_TRANSITIONS.get(row["phase"], set()):
                raise RecoveryError("Этап операции пропущен или выполнен в неверном порядке.")
            if state == State.STARTING and row["media_done"] != row["media"]:
                raise RecoveryError("Telegram ещё не подтвердил загрузку всех вложений.")
            if reserve:
                from .conversion import Bundle

                if row["test_reset_id"] != self.test_reset_id or (
                    self.test_reset_id is not None and row["account"] != self.endpoint.account_id
                ):
                    raise RecoveryError("Аккаунт или режим импорта изменился. Повторите проверку пакета.")
                saved = db.execute("SELECT manifest FROM import_packages WHERE sha=?", (row["manifest_sha256"],)).fetchone()
                self._assert_available(
                    db,
                    row["fingerprint"],
                    row["scope"],
                    row["content_fingerprint"] or None,
                    json.loads(row["selection"]) if row["selection"] is not None else None,
                    row["messages"],
                    bundle=Bundle(self.path.parent, json.loads(saved[0])) if saved else None,
                    exclude=job,
                )
                # A terminal unknown start keeps its record protection in operations.
                # Availability only permits replacing its reservation for a disjoint package.
                db.execute(
                    """INSERT INTO destination_locks VALUES (?,?)
                       ON CONFLICT(peer) DO UPDATE SET operation=excluded.operation""",
                    (self.endpoint.key, job),
                )
                pid = os.getpid()
                db.execute(
                    "INSERT INTO operation_workers(operation,pid,process_started_at) VALUES (?,?,?)",
                    (job, pid, worker_signature(pid)),
                )
            if release:
                db.execute("DELETE FROM destination_locks WHERE operation=?", (job,))
            end = (
                (datetime.now(UTC) + timedelta(seconds=fault.seconds)).isoformat(timespec="seconds")
                if fault and fault.seconds
                else row["retry_after"]
            )
            code = fault.code if fault else row["error_code"]
            db.execute(
                """UPDATE operations SET state=?,stage=?,outcome=?,server_id=?,retry_after=?,media_done=?,
                    error_code=?,transcript_upload_done=?,start_request_sent=?,start_request_accepted=?,
                    phase=?,validated_messages=? WHERE id=?""",
                (
                    state,
                    stage,
                    row["outcome"] if outcome is None else outcome,
                    row["server_id"] if server_id is None else server_id,
                    end,
                    done if stage == "upload" and state == State.UPLOADING else row["media_done"],
                    code,
                    row["transcript_upload_done"] or transcript_uploaded,
                    row["start_request_sent"] if start_sent is _KEEP else start_sent,
                    row["start_request_accepted"] or start_accepted,
                    phase,
                    row["messages"] if phase == Phase.VALIDATED else row["validated_messages"],
                    job,
                ),
            )
            self._event(db, job, state, stage, done, total, code if fault else "")

    @staticmethod
    def _operation(row):
        values = dict(row)
        values.update(
            source_records=values["messages"] + values["skipped"],
            package_records=values["messages"],
            media_uploaded_successfully=values["media_done"],
            media_total=values["media"],
            selected_records=values["messages"]
            if values["selected_messages"] is None
            else values["selected_messages"],
            validated_records=values["validated_messages"],
        )
        values["accuracy"] = (
            json.loads(values["accuracy"])
            if values["accuracy"]
            else {
                "verification": "INCONCLUSIVE",
                "expected": values["messages"],
                "local": None,
                "server": None,
                "client": None,
                "native_authorship": "UNSUPPORTED_BY_API",
            }
        )
        for flag in ("transcript_upload_done", "start_request_sent", "start_request_accepted"):
            if values[flag] is not None:
                values[flag] = bool(values[flag])
        return values

    def operations(self, limit=100) -> list[dict]:
        with self.connect() as db:
            return [
                self._operation(row)
                for row in db.execute("SELECT * FROM operations ORDER BY rowid DESC LIMIT ?", (limit,))
            ]

    def operation(self, job: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (job,)).fetchone()
            return self._operation(row) if row else None

    def begin_media(self, job: str, attempt: dict):
        with self.connect(lock=True) as db:
            db.execute(
                """INSERT INTO media_attempts(operation,item_index,details,stage,started_at,status)
                   VALUES (?,?,?,?,?,'UPLOADING_ITEM')""",
                (
                    job,
                    attempt["index"],
                    json.dumps(attempt, ensure_ascii=False),
                    attempt["stage"],
                    attempt["started_at"],
                ),
            )

    def media_stage(self, job: str, index: int, stage: str):
        with self.connect(lock=True) as db:
            updated = db.execute(
                "UPDATE media_attempts SET stage=? WHERE operation=? AND item_index=? AND status='UPLOADING_ITEM'",
                (stage, job, index),
            )
            if updated.rowcount != 1:
                raise RecoveryError("Запись о попытке загрузки отсутствует в журнале.")

    def finish_media(self, job: str, attempt: dict):
        with self.connect(lock=True) as db:
            result = db.execute(
                """UPDATE media_attempts SET stage=?,ended_at=?,status=?,error_code=?,response_constructor=?
                WHERE operation=? AND item_index=? AND status='UPLOADING_ITEM'""",
                (
                    attempt["stage"],
                    attempt["ended_at"],
                    attempt["status"],
                    attempt["error_code"],
                    attempt["response_constructor"],
                    job,
                    attempt["index"],
                ),
            )
            if result.rowcount != 1:
                raise RecoveryError("Запись о попытке загрузки отсутствует в журнале.")
            if attempt["status"] == "MEDIA_ACCEPTED":
                db.execute("UPDATE operations SET media_done=? WHERE id=?", (attempt["index"], job))

    def media_attempts(self, job: str) -> list[dict]:
        with self.connect() as db:
            return self._media_attempts(db, job)

    @staticmethod
    def _media_attempts(db, job):
        return [
            json.loads(row["details"])
            | {
                key: row[key]
                for key in ("stage", "started_at", "ended_at", "status", "error_code", "response_constructor")
            }
            for row in db.execute(
                "SELECT * FROM media_attempts WHERE operation=? ORDER BY item_index", (job,)
            )
        ]

    def diagnostic_report(self, job: str, *, private=False) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (job,)).fetchone()
            if row is None:
                raise RecoveryError("Операция отсутствует в журнале.")
            operation = self._operation(row)
            events = self._events(db, job)
            attempts = self._media_attempts(db, job)
        keys = {
            "id",
            "created",
            "mode",
            "state",
            "stage",
            "outcome",
            "retry_after",
            "error_code",
            *(
                OPERATION_COLUMNS.keys()
                - {"content_fingerprint", "selection", "selected_messages", "validated_messages"}
            ),
            "source_records",
            "package_records",
            "selected_records",
            "validated_records",
            "skipped",
            "duplicates",
            "media_total",
            "media_uploaded_successfully",
        }
        if private:
            keys |= {
                "peer",
                "account",
                "server_id",
                "fingerprint",
                "manifest_sha256",
                "content_fingerprint",
                "selection",
            }
        return {
            "schema": 1,
            "redacted": not private,
            "operation": {k: v for k, v in operation.items() if k in keys},
            "events": events,
            "media_attempts": attempts if private else list(map(redact_attempt, attempts)),
        }

    def events(self, job: str) -> list[dict]:
        with self.connect() as db:
            return self._events(db, job)

    @staticmethod
    def _events(db, job):
        return [
            dict(row)
            for row in db.execute("SELECT * FROM operation_events WHERE operation=? ORDER BY seq", (job,))
        ]

    def owner_disconnected(self, job: str):
        """Record shutdown acknowledged by the same process after terminal staging."""
        pid = os.getpid()
        with self.connect(lock=True) as db:
            db.execute(
                """UPDATE operation_workers SET disconnected_at=? WHERE operation=? AND pid=? AND process_started_at=?
                   AND EXISTS (SELECT 1 FROM operations WHERE id=? AND mode='owner' AND state='STAGED_NOT_STARTED')""",
                (timestamp(), job, pid, worker_signature(pid), job),
            )

    def classify_interrupted(self):
        """Expose abandoned work without removing a destination lock or using a server call."""
        with self.connect(lock=True) as db:
            rows = list(
                db.execute("""SELECT o.id,o.state,o.stage,w.pid,w.process_started_at,w.disconnected_at
                FROM destination_locks l JOIN operations o ON o.id=l.operation
                JOIN operation_workers w ON w.operation=o.id WHERE o.mode='owner'
                AND o.state IN ('CONFIRMED','INITIALIZING','UPLOADING','STARTING')""")
            )
            for row in rows:
                if not worker_stopped(row):
                    continue
                final = row["state"] == State.STARTING
                state = State.START_UNCERTAIN if final else State.STAGED_NOT_STARTED
                db.execute(
                    "UPDATE operations SET state=?,phase=?,outcome=? WHERE id=?",
                    (state, phase_of(state, row["stage"]), "uncertain" if final else "staged", row["id"]),
                )
                db.execute(
                    """UPDATE media_attempts SET status='MEDIA_OUTCOME_UNKNOWN',ended_at=?
                    WHERE operation=? AND status='UPLOADING_ITEM'""",
                    (timestamp(), row["id"]),
                )
                self._event(db, row["id"], state, row["stage"], 0, 0, "PROCESS_INTERRUPTED")

    def recover_before_start(self, job: str, *, acknowledged=False) -> str:
        """Clear a stale local lock only when the durable final boundary was never reached."""
        if acknowledged is not True:
            raise RecoveryError(
                "Подтвердите закрытие подготовки. Повторная отправка не начнётся автоматически."
            )
        with self.connect(lock=True) as db:
            row = db.execute(
                """SELECT o.stage,o.state,w.pid,w.process_started_at,w.disconnected_at FROM operations o
                JOIN destination_locks l ON l.operation=o.id JOIN operation_workers w ON w.operation=o.id
                WHERE o.id=? AND o.mode='owner'""",
                (job,),
            ).fetchone()
            if not row or row["stage"] not in PRE_START_STAGES or row["state"] not in PRE_START_STATES:
                raise RecoveryError("Эта операция не подходит для локального восстановления до запуска.")
            if self._has_start_boundary(db, job):
                raise RecoveryError("Финальный запрос мог быть отправлен. Нужна сверка истории.")
            if not worker_stopped(row):
                raise RecoveryError("Процесс операции ещё работает или его завершение не подтверждено.")
            db.execute(
                "UPDATE operations SET state=?,stage=?,outcome=?,phase=? WHERE id=?",
                (State.STAGED_NOT_STARTED, "recovered_before_start", "staged_closed", Phase.PARTIAL, job),
            )
            db.execute("DELETE FROM destination_locks WHERE operation=?", (job,))
            db.execute(
                """UPDATE media_attempts SET status='MEDIA_OUTCOME_UNKNOWN',ended_at=?
                       WHERE operation=? AND status='UPLOADING_ITEM'""",
                (timestamp(), job),
            )
            self._event(db, job, State.STAGED_NOT_STARTED, "recovered_before_start", 0, 0, "")
        return job

    @staticmethod
    def _has_start_boundary(db, job):
        return (
            db.execute(
                "SELECT 1 FROM operation_events WHERE operation=? AND state=?", (job, State.STARTING)
            ).fetchone()
            is not None
        )

    def reconcile(self, report: dict) -> str | None:
        """Only a real, matched capture can resolve a dispatched operation."""
        if report.get("mode") != "telegram_observation":
            return None
        views = report.get("views", [])
        if not views or any(
            Endpoint.telegram(v["account_id"], v["peer_id"], v.get("peer_kind", "pm")).key
            != self.endpoint.key
            for v in views
        ):
            return None
        with self.connect(lock=True) as db:
            row = db.execute(
                """SELECT o.id,o.content_fingerprint,o.state,o.stage,o.outcome,o.messages,
                coalesce(e.selection,o.selection) AS selection,e.confirmed,w.pid,w.process_started_at,w.disconnected_at
                FROM operations o
                LEFT JOIN operation_record_evidence e ON e.operation=o.id
                LEFT JOIN operation_workers w ON w.operation=o.id WHERE o.peer=? AND o.mode='owner' AND o.fingerprint=?
                AND o.manifest_sha256=? AND o.outcome IN ('accepted','uncertain')
                AND o.state IN ('STARTING','START_UNCERTAIN','PARTIAL','VERIFYING','VERIFICATION_MISMATCH','COMPLETED')
                ORDER BY o.rowid DESC LIMIT 1""",
                (self.endpoint.key, report["package"], report["manifest_sha256"]),
            ).fetchone()
            if not row:
                return None
            if row["state"] == State.COMPLETED:
                return row["id"]
            proven = report.get("accepted_evidence") is True
            if row["state"] == State.STARTING and (not row["pid"] or not worker_stopped(row)):
                return None
            views = report.get("views", [])
            db.execute("INSERT OR REPLACE INTO registry_checks VALUES (?,?)", (row["id"], timestamp()))
            self._record_presence(db, row, report.get("confirmed_source_ordinals", ()), views)
            if row["outcome"] == "uncertain" and (not proven or row["stage"] != "start_import"):
                return None
            if row["outcome"] == "uncertain" and row["content_fingerprint"]:
                marker = db.execute(
                    """SELECT ts FROM operation_events WHERE operation=? AND state='STARTING'
                    ORDER BY seq LIMIT 1""",
                    (row["id"],),
                ).fetchone()
                if marker is None or len(views) != 2:
                    return None
                try:
                    deadline = datetime.fromisoformat(marker["ts"])
                    if not all(
                        datetime.fromisoformat(view["scope"]["baseline_captured_at"]) <= deadline
                        for view in views
                    ):
                        return None
                except (KeyError, ValueError, TypeError):
                    return None
            observed = report.get("server_messages_observed") if len(views) == 2 else None
            verified = report.get("client_messages_verified") if len(views) == 2 else None
            state = {"verified": State.COMPLETED, "differences": State.VERIFICATION_MISMATCH}.get(
                report["state"], State.VERIFYING
            )
            if row["state"] != State.VERIFYING and state != State.VERIFYING:
                self._event(db, row["id"], State.VERIFYING, "verification", 0, report.get("expected", 0), "")
            verification = report.get("verification_status", "INCONCLUSIVE")
            levels = report.get("levels", {})
            accuracy = {
                "verification": verification,
                "expected": report.get("expected"),
                "server_messages_observed": observed,
                "client_messages_verified": verified,
                "historical_dates": [
                    {
                        key: view.get("metrics", {}).get("date", {}).get(key, 0)
                        for key in ("same", "different", "unknown")
                    }
                    for view in views
                ],
                "historical_author_labels": [
                    {
                        key: view.get("metrics", {}).get("label", {}).get(key, 0)
                        for key in ("same", "different", "unknown")
                    }
                    for view in views
                ],
                "local": levels.get("A", {}).get("status"),
                "server": levels.get("B1"),
                "client": levels.get("C"),
                "native_authorship": "UNSUPPORTED_BY_API",
            }
            db.execute(
                """UPDATE operations SET state=?,phase=?,stage='verification',outcome='accepted',
                        server_messages_observed=?,client_messages_verified=?,accuracy=? WHERE id=?""",
                (state, phase_of(state, "verification"), observed, verified, json.dumps(accuracy), row["id"]),
            )
            db.execute("DELETE FROM destination_locks WHERE operation=?", (row["id"],))
            self._event(db, row["id"], state, "verification", verified or 0, report.get("expected", 0), "")
        return row["id"]

    def _record_presence(self, db, row, ordinals, views):
        """Accumulate positive matches from two account windows taken before dispatch."""
        chosen = _record_ranges(row["selection"], row["messages"])
        if not chosen or not ordinals or len(views) != 2:
            return
        marker = db.execute(
            "SELECT ts FROM operation_events WHERE operation=? AND state='STARTING' ORDER BY seq LIMIT 1",
            (row["id"],),
        ).fetchone()
        if not marker:
            return
        try:
            deadline = datetime.fromisoformat(marker["ts"])
            if not all(
                not view["scope"]["truncated"]
                and (
                    view["scope"]["kind"] == "id_window"
                    and datetime.fromisoformat(view["scope"]["baseline_captured_at"]) <= deadline
                    or row["outcome"] == "accepted"
                    and view["scope"]["kind"] == "historical_date"
                    and datetime.fromisoformat(view["captured_at"]) >= deadline
                )
                for view in views
            ):
                return
            confirmed = decode_intervals(tuple((number, number) for number in ordinals))
        except (KeyError, ValueError, TypeError):
            return
        if difference(confirmed, chosen):
            return
        combined = merge((*(_record_ranges(row["confirmed"]) or ()), *confirmed))
        now = timestamp()
        db.executemany(
            "INSERT OR REPLACE INTO record_verifications VALUES (?,?,?)",
            ((row["id"], pos, now) for a, b in confirmed for pos in range(a, b + 1)),
        )
        db.execute(
            """INSERT INTO operation_record_evidence(operation,confirmed) VALUES (?,?)
                    ON CONFLICT(operation) DO UPDATE SET confirmed=excluded.confirmed""",
            (row["id"], json.dumps(combined, separators=(",", ":"))),
        )
