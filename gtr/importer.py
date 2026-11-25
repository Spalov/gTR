"""History Import preflight and dispatch, with a durable boundary before each mutation."""

import asyncio
import re
import sqlite3
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .conversion import Bundle, load_bundle, owner_copy
from .faults import AMBIGUOUS_RPC, FAULTS, RESTRICTIONS, UNSUPPORTED, ImportFault
from .filenames import safe_filename
from .journal import Journal, RecoveryRequired, State, safe_constructor, timestamp
from .media import KIND_NAMES
from .media_engine import archive_paths, inspect_bundle
from .model import Cancelled, RecoveryError, quiet

__all__ = ["FAULTS", "HistoryAPI", "ImportController", "ImportFault", "Journal", "Preflight", "run_import"]


class HistoryAPI(Protocol):
    async def check_head(self, head: str) -> str: ...
    async def check_peer(self, peer, parsed_kind: str) -> str: ...
    async def initialize(self, peer, transcript: Path, media_count: int) -> int: ...
    async def upload_media(self, peer, import_id: int, item: dict, path: Path) -> str | None: ...
    async def start(self, peer, import_id: int) -> bool: ...


@dataclass(frozen=True)
class Preflight:
    operation_id: str | None
    fingerprint: str
    parsed_kind: str
    confirmation: str
    messages: int
    media: int
    max_upload_bytes: int | None
    manifest_sha256: str = ""


def _accepted_mime(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9.+-]{1,60}/[a-zA-Z0-9.+-]{1,100}", value)
        else None
    )


def _media_attempt(index, total, item, record, facts=None):
    name = item["name"]
    source_id = str(record.get("source_id", ""))
    attrs = item.get("attributes", {})
    category = attrs.get("type")
    attributes = {"type": category} if category in ("video", "audio", "animation", "sticker") else {}
    for key in ("duration", "width", "height", "round", "voice"):
        value = attrs.get(key)
        if type(value) is bool or (type(value) in (int, float) and 0 <= value < 10**9):
            attributes[key] = value
    if category == "sticker":
        attributes["has_alt"] = bool(attrs.get("emoji"))
    upload_as = "photo" if item["upload_as"] == "photo" else "document"
    return {
        "index": index,
        "total": total,
        "name": safe_filename(name),
        "source_id": source_id if re.fullmatch(r"-?\d{1,20}", source_id) else None,
        "source_ordinal": record.get("source_ordinal") if type(record.get("source_ordinal")) is int else None,
        "archive_paths": archive_paths(item, record),
        "detected_mime": _accepted_mime(
            facts.get("mime") if facts is not None else item.get("detected_mime")
        ),
        "declared_mime": _accepted_mime(item.get("declared_mime")),
        "wire_mime": _accepted_mime(item.get("mime")),
        "kind": item["kind"] if item["kind"] in KIND_NAMES else "unknown",
        "upload_as": upload_as,
        "size": item["size"],
        "sha256": item["sha256"],
        "attributes": attributes,
        "input_media": "InputMediaUploadedPhoto" if upload_as == "photo" else "InputMediaUploadedDocument",
        "stage": "upload_item",
        "status": "UPLOADING_ITEM",
        "started_at": timestamp(),
        "ended_at": None,
        "error_code": None,
        "response_constructor": None,
        "requested_kind": item["kind"] if item.get("attributes") or item["upload_as"] == "photo" else "document",
        "received_kind": None,
        "local_issues": facts["issues"] if facts is not None else (),
    }


class ImportController:
    """One validated package, one target, one explicit owner approval."""

    def __init__(
        self,
        api: HistoryAPI,
        peer,
        *,
        journal: Journal | None = None,
        mode: str = "mock",
        progress=quiet,
        limits=None,
    ):
        self.api, self.peer, self.journal = api, peer, journal
        self.mode, self.progress, self.limits = mode, progress, limits
        self.state, self.stage = State.IDLE, "idle"
        self.job: str | None = None
        self.preflight: Preflight | None = None
        self.bundle: Bundle | None = None
        self.files = ExitStack()
        self.media_attempt: dict | None = None
        self.media_facts = []

    def _record(self, state: State, stage: str, **args):
        if self.journal and self.job:
            self.journal.transition(self.job, state, stage, **args)
        self.state, self.stage = state, stage

    def _advance(self, state: State, stage: str, done=0, total=0, **args):
        self._record(state, stage, done=done, total=total, **args)
        self.progress(stage, done, total)

    def close(self):
        self.files.close()
        self.bundle = None
        self.preflight = None

    def cancel(self):
        if self.state in {State.IDLE, State.READY, State.AWAITING_USER}:
            self._record(State.CANCELLED, "cancelled")
        self.close()

    def _problem(self, error, *, init_sent=False, staged=False, reserved=False):
        cancelled = isinstance(error, (Cancelled, asyncio.CancelledError, KeyboardInterrupt))
        fault = (
            error.at(self.stage)
            if isinstance(error, ImportFault)
            else ImportFault(
                "REPLAY_BLOCKED"
                if isinstance(error, RecoveryRequired)
                else "LOCAL_JOURNAL_UNAVAILABLE"
                if isinstance(error, sqlite3.Error)
                else "LOCAL_INVALID",
                stage=self.stage,
            )
        )
        missing_reply = not isinstance(error, ImportFault) or fault.code in AMBIGUOUS_RPC | {
            "START_UNCERTAIN"
        }
        final = self.state == State.STARTING
        state_args = {}
        if final:
            state = State.START_UNCERTAIN if missing_reply else State.START_REJECTED
            if missing_reply:
                fault = ImportFault("START_UNCERTAIN", stage="start_import")
            state_args = {
                "start_sent": None if missing_reply else True,
                "outcome": "uncertain" if missing_reply else "rejected",
            }
        elif staged or (init_sent and missing_reply):
            state = State.STAGED_NOT_STARTED
            if not staged and missing_reply:
                fault = ImportFault("STAGING_UNCERTAIN", stage=self.stage)
            state_args = {"outcome": "staged"}
        elif cancelled:
            state = State.CANCELLED
        elif reserved:
            state = State.FAILED_BEFORE_START
            state_args = {"outcome": "rejected"}
        else:
            state = State.UNSUPPORTED if fault.code in UNSUPPORTED else State.FAILED
        try:
            self._record(
                state,
                self.stage,
                fault=None if cancelled and not (final or init_sent and not staged) else fault,
                release=reserved and state not in {State.STAGED_NOT_STARTED, State.START_UNCERTAIN},
                **state_args,
            )
        except (OSError, sqlite3.Error):
            if not final:
                fault = ImportFault("LOCAL_JOURNAL_UNAVAILABLE", stage=self.stage)
        fault.operation_id = self.job
        fault.media_attempt = self.media_attempt
        try:
            self.progress("limited" if fault.code in RESTRICTIONS else "error", 0, 0)
        except Cancelled:
            pass  # Preserve the operation outcome if the progress consumer stops.
        if cancelled and not final and not (init_sent and not staged):
            return error
        return fault

    async def validate(self, bundle: Bundle) -> Preflight:
        if self.state != State.IDLE:
            raise RecoveryError("Проверка уже выполнялась. Откройте новую операцию.")
        fingerprint = bundle.fingerprint()
        scope = bundle.scope()
        try:
            if self.journal:
                self.job = self.journal.create(bundle, self.mode, scope)
            self._advance(State.VALIDATING, "local_check")
            if self.journal:
                self.journal.check(
                    fingerprint,
                    scope,
                    bundle.content_fingerprint(),
                    bundle.record_intervals(),
                    selected=bundle.data["included"],
                    bundle=bundle,
                )
            if not bundle.data["included"]:
                raise RecoveryError("В пакете нет сообщений для переноса.")
            if self.mode == "owner" and any(
                bundle.data["losses"].get(key) for key in ("ordered_time", "synthetic_time")
            ):
                raise RecoveryError(
                    "Для отправки подготовьте пакет с исходными датами без сдвигов и подстановок."
                )
            if self.mode == "owner":
                checked = self.files.enter_context(owner_copy(bundle, self.progress))
            else:
                checked = load_bundle(bundle.root, self.progress)
            if checked.manifest_hash() != bundle.manifest_hash():
                raise RecoveryError("Пакет изменился после выбора. Откройте его снова.")
            self.bundle = checked
            if self.journal and self.mode == "owner":
                from .incremental_import import retain

                retain(self.journal, checked, getattr(self, "source_project", None), self.progress)
            if self.mode == "owner" and checked.data["media"]:
                inventory = inspect_bundle(checked, self.progress, hash_verified=True)
                self.media_facts = inventory["items"]
                if bad := next((item for item in self.media_facts if item["blocked"]), None):
                    record = next(
                        (entry for entry in checked.data["records"] if entry.get("media") == bad["name"]), {}
                    )
                    attempt = _media_attempt(
                        bad["index"], inventory["count"], checked.data["media"][bad["index"] - 1], record, bad
                    )
                    attempt.update(
                        stage="inspect_media",
                        status="MEDIA_LOCAL_REJECTED",
                        error_code="MEDIA_LOCAL_INVALID",
                        ended_at=timestamp(),
                    )
                    self.media_attempt = attempt
                    if self.journal and self.job:
                        self.journal.begin_media(self.job, attempt)
                        self.journal.finish_media(self.job, attempt)
                    raise ImportFault("MEDIA_LOCAL_INVALID", stage="inspect_media")
            self._advance(State.VALIDATING, "validated", checked.data["included"], checked.data["included"])
            self._advance(State.VALIDATING, "check_history")
            kind = await self.api.check_head(checked.head())
            self._advance(State.VALIDATING, "check_peer")
            confirmation = await self.api.check_peer(self.peer, kind)
            if not isinstance(confirmation, str) or not confirmation.strip():
                raise ImportFault("RPC_REJECTED")
            limit = None
            if self.limits:
                self._advance(State.VALIDATING, "check_limits")
                limit = await self.limits()
                if limit and (
                    checked.transcript.stat().st_size > limit
                    or any(f["size"] > limit for f in checked.data["media"])
                ):
                    raise ImportFault("MEDIA_SIZE_LIMIT")
            receipt = Preflight(
                self.job,
                fingerprint,
                kind,
                confirmation,
                checked.data["included"],
                len(checked.data["media"]),
                limit,
                checked.manifest_hash(),
            )
            self._advance(State.READY, "ready")
            self._advance(State.AWAITING_USER, "await_confirmation")
            self.preflight = receipt
            return receipt
        except (Exception, asyncio.CancelledError, KeyboardInterrupt) as error:
            try:
                # Preserve safe local messages such as a replay guard or damaged package.
                fault = self._problem(error)
            finally:
                self.close()
            if isinstance(error, RecoveryError) and not isinstance(error, ImportFault):
                raise
            raise fault from None

    async def _upload_item(self, bundle, server_id, index, item, total, record):
        attempt = _media_attempt(
            index, total, item, record, self.media_facts[index - 1] if self.media_facts else None
        )
        self.media_attempt = attempt
        if self.journal and self.job:
            self.journal.begin_media(self.job, attempt)

        def stage(name):
            attempt["stage"] = name
            if self.journal and self.job:
                self.journal.media_stage(self.job, index, name)

        stage.response = lambda actual: attempt.update(received_kind=actual)
        report = getattr(self.progress, "upload_bytes", None)
        if report:
            stage.upload_bytes = lambda name, sent, size, speed: report(
                {"name": name, "index": index, "total": total, "sent": sent, "size": size, "speed": speed}
            )
        try:
            self.progress("upload_item", index, total)
            path = bundle.media_path(item)
            traced = getattr(self.api, "upload_media_traced", None)
            if traced:
                constructor = await traced(self.peer, server_id, item, path, stage)
            else:
                stage("upload_imported_media")
                constructor = await self.api.upload_media(self.peer, server_id, item, path)
        except (Exception, asyncio.CancelledError, KeyboardInterrupt) as error:
            fault = error if isinstance(error, ImportFault) else None
            cancelled = isinstance(error, (Cancelled, asyncio.CancelledError, KeyboardInterrupt))
            unknown = attempt["stage"] == "upload_imported_media" and (
                fault is None or fault.code in AMBIGUOUS_RPC
            )
            status = (
                "MEDIA_OUTCOME_UNKNOWN" if unknown else "MEDIA_CANCELLED" if cancelled else "MEDIA_REJECTED"
            )
            code = fault.code if fault else "CANCELLED" if cancelled else "LOCAL_INVALID"
            attempt.update(
                status=status,
                ended_at=timestamp(),
                error_code=code,
                response_constructor=safe_constructor(fault.response_constructor) if fault else None,
            )
            if self.journal and self.job:
                self.journal.finish_media(self.job, attempt)
            raise
        attempt.update(
            status="MEDIA_ACCEPTED", ended_at=timestamp(), response_constructor=safe_constructor(constructor)
        )
        if self.journal and self.job:
            self.journal.finish_media(self.job, attempt)
        self.media_attempt = None

    async def execute(self, checked: Preflight, *, approved: bool):
        if (
            self.state not in {State.READY, State.AWAITING_USER}
            or checked is not self.preflight
            or self.bundle is None
        ):
            raise RecoveryError("Сначала проверьте текущие пакет и диалог.")
        if not approved:
            self.cancel()
            raise Cancelled()
        bundle = self.bundle
        init_sent = False
        staged = False
        reserved = False
        try:
            bundle = load_bundle(bundle.root, self.progress)
            if (
                bundle.fingerprint() != checked.fingerprint
                or bundle.manifest_hash() != checked.manifest_sha256
            ):
                raise RecoveryError("Пакет изменился после проверки. Откройте его снова.")
            media = bundle.data["media"]
            source_records = {
                record.get("media"): record for record in bundle.data["records"] if record.get("media")
            }
            self._record(State.CONFIRMED, "confirmed", reserve=True)
            reserved = True
            self.progress("confirmed", 0, 0)
            upload_transcript = getattr(self.api, "upload_transcript", None)
            if upload_transcript:
                self._advance(State.INITIALIZING, "upload_transcript")
                file = await upload_transcript(bundle.transcript)
                self._advance(State.INITIALIZING, "transcript_uploaded", 1, 1, transcript_uploaded=True)
                self._advance(State.INITIALIZING, "init_import", 0, len(media))
                init_sent = True
                server_id = await self.api.initialize_uploaded(self.peer, file, len(media))
            else:
                self._advance(State.INITIALIZING, "init_import", 0, len(media))
                init_sent = True
                server_id = await self.api.initialize(self.peer, bundle.transcript, len(media))
            staged = True
            if not upload_transcript:
                self._advance(State.INITIALIZING, "transcript_uploaded", 1, 1, transcript_uploaded=True)
            self._record(State.INITIALIZING, "initialized", server_id=server_id, transcript_uploaded=True)
            self._advance(
                State.UPLOADING, "upload", 0, len(media), server_id=server_id, transcript_uploaded=True
            )
            for i, item in enumerate(media, 1):
                await self._upload_item(
                    bundle, server_id, i, item, len(media), source_records.get(item["name"], {})
                )
                self._advance(State.UPLOADING, "upload", i, len(media))
            # First notify a cancellation checkpoint. Only then persist and dispatch the final RPC.
            self.progress("start_import", len(media), len(media))
            self.progress("start_pending", len(media), len(media))
            self._record(
                State.STARTING,
                "start_import",
                done=len(media),
                total=len(media),
                outcome="uncertain",
                start_sent=None,
            )
            response = await self.api.start(self.peer, server_id)
            if response is False:
                raise ImportFault("RPC_REJECTED")
            if response is not True:
                raise ImportFault("START_UNCERTAIN")
            self._record(
                State.VERIFYING,
                "verifying",
                release=True,
                outcome="accepted",
                start_sent=True,
                start_accepted=True,
            )
        except (Exception, asyncio.CancelledError, KeyboardInterrupt) as error:
            try:
                fault = self._problem(error, init_sent=init_sent, staged=staged, reserved=reserved)
            finally:
                self.close()
            if (
                isinstance(error, RecoveryError)
                and not isinstance(error, ImportFault)
                and self.state != State.START_UNCERTAIN
            ):
                raise
            raise fault from None
        self.close()
        try:
            self.progress("verifying", 0, 0)
        except Cancelled:
            pass  # Cancellation cannot undo the accepted start.
        return {"messages": checked.messages, "media": checked.media, "state": "accepted"}


async def run_import(
    bundle: Bundle,
    api: HistoryAPI,
    peer,
    owner_enabled: bool,
    confirm: Callable[[str], bool],
    progress=quiet,
    journal: Journal | None = None,
):
    if not owner_enabled:
        raise RecoveryError("Владелец должен разрешить проверку импорта.")
    controller = ImportController(api, peer, journal=journal, progress=progress)
    try:
        checked = await controller.validate(bundle)
        return await controller.execute(checked, approved=confirm(checked.confirmation))
    finally:
        controller.cancel()
