"""Build an inspectable WhatsApp-style UTF-8 transcript and a local loss ledger."""

import errno
import hashlib
import json
import re
import shutil
import tempfile
from bisect import bisect_right
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .archive import MAX_TOTAL_BYTES
from .bindings import VERSION as BINDING_VERSION
from .bindings import AttachmentBindingError, bind, check_binding, wire_name
from .core import ISSUES, catalogue, encode, properties
from .filenames import assign_names, safe_filename
from .files import (
    BLOCK,
    digest,
    local,
    new_directory,
    private_directory,
    safe_parts,
)
from .history import (
    DATE_POLICIES,
    HistoricalIdentity,
    WireClock,
    first_reliable_time,
    ordered_rows,
)
from .media import KIND_NAMES
from .media_queue import (
    MediaDecisionRequired,
    MediaQueue,
    validate_decisions,
    video_decision_key,
)
from .media_recovery import (
    SKIP_ACTIONS,
    queue_attachment,
    recovery_entry,
    source_digest,
)
from .media_recovery import VERSION as MEDIA_WORKFLOW
from .model import Cancelled, RecoveryError, quiet
from .package_errors import PackageValidationError
from .payloads import (
    STRUCTURED,
    MediaInputError,
    plan_media,
    publish_media,
    validate_media_input,
)
from .profiles import MARKER, MEDIA_NAME, PROFILES, read_records, wire_line
from .selection import decode_intervals, difference, included_intervals
from .structure import (
    NOTE_LANGUAGES,
    NOTE_MODES,
    annotated_text,
    historical_notes,
    local_structure,
)
from .video import VIDEO_KINDS, VIDEO_POLICIES, MediaTools, queue_video

PROFILE = PROFILES["ios"]
MEDIA_MODES = {
    "text_media": "Текст и медиа",
    "text_only": "Только текст (для первого теста)",
    "document": "Текст и медиа в виде документов",
}
POSSIBLE_HEADER = re.compile(r"^[\s\u200e\u200f\ufeff]*\[?\d{1,4}[./-]\d{1,2}[./-]\d{1,4}[,\s]+\d{1,2}:\d{2}")
LOSS = {
    "service": "Служебное событие сохранено локально",
    "identity": "Нельзя однозначно записать автора или исходный ID",
    "date": ISSUES["invalid_date"],
    "date_conflict": ISSUES["date_conflict"],
    "ambiguous_local_time": ISSUES["ambiguous_local_time"],
    "nonexistent_local_time": ISSUES["nonexistent_local_time"],
    "conflict": "Конфликт исходного ID",
    "ambiguous_text": "Текст может быть прочитан как граница сообщения или ссылка на файл",
    "empty": "В сообщении нет переносимого текста или оригинала",
    "multiple_media": "Несколько вложений в одной записи",
    "media_unavailable": "Непригодное вложение пропущено",
    "document_fallback": "Файл передаётся как документ",
    "rich_text": "Оформление остаётся в локальном проекте",
    "reply": "Связь ответа остаётся в локальном проекте",
    "reactions": "Реакции остаются в источнике",
    "forward": "Данные пересылки остаются в источнике",
    "structured": "Структурированные поля остаются в источнике",
    "assumed_timezone": "Применён выбранный пояс источника",
    "file_type_unconfirmed": "Содержимое передаётся документом с непроверенным типом",
    "ordered_time": "Метка импорта сдвинута для сохранения порядка",
    "synthetic_time": "Метка импорта назначена по порядку, исходное время неизвестно",
    "missing_source_id": "Исходный ID отсутствует",
    "grouped_media": "Вложения и сведения объединены в один ZIP",
    "structured_document": "Исходные структурированные данные вложены в виде документа",
    "reference_document": "Документ описывает недоступный оригинал",
    "media_attributes": "Пакет содержит применимые атрибуты медиа",
    "video_decision": "Для видео требуется выбрать совместимую копию, документ или пропуск",
    "audio_unconfirmed": "Не удалось подтвердить нативный тип или длительность аудио",
    "audio_unreadable": "Локальная проверка аудио не пройдена",
    "group_video_document": "Многовложенная запись с видео подготовлена в виде одного архива",
    "video_document": "Пользователь выбрал передачу видео документом",
    "video_skipped": "Несовместимое видео пропущено по выбору пользователя",
    "video_transcode": "Для видео выбрана совместимая копия H.264/AAC",
    "attachment_decision": "Для вложения требуется выбрать обработку",
    "media_local_only": "Оригинал оставлен в локальном проекте",
    "media_skipped": "Вложение исключено из пакета по выбору пользователя",
    "media_png": "Подготовлена копия изображения PNG",
    "type_selection": "Тип записи вне выборки",
    "sample_limit": "Запись за пределами экспериментальной выборки",
    "outside_selection": "Запись за пределами выбранного диапазона",
    "text_only_media": "Вложения и структурированные данные остаются в проекте",
    "text_only_empty": "Запись с медиа без текста остаётся в проекте",
}


@dataclass
class Bundle:
    root: Path
    data: dict

    @property
    def transcript(self):
        return self.root / "_chat.txt"

    def media_path(self, item):
        name = item.get("storage_name", item["name"])
        if not isinstance(name, str) or not MEDIA_NAME.fullmatch(name):
            raise RecoveryError("Имя вложения в пакете недопустимо.")
        return local(self.root / "media" / name)

    def head(self):
        with self.transcript.open(encoding="utf-8", newline="") as stream:
            from itertools import islice

            return "".join(islice(stream, 100))

    def fingerprint(self):
        if "media_mode" in self.data:
            # Bind the upload representation and package scope as well as the byte hashes.
            return self.manifest_hash()
        return self.content_fingerprint()

    def content_fingerprint(self) -> str:
        """Byte identity used by legacy replay journals before manifest binding."""
        hashes = [self.data["text_sha256"], *[f["sha256"] for f in self.data["media"]]]
        # A planned generated ZIP acquires its byte digest only during publication.
        return hashlib.sha256("".join(hashes).encode("ascii")).hexdigest() if all(hashes) else ""

    def manifest_hash(self) -> str:
        return hashlib.sha256(encode(self.data).encode()).hexdigest()

    def scope(self) -> str:
        return hashlib.sha256(f"{self.data['source_sha256']}:{self.data['chat_id']}".encode()).hexdigest()

    def record_intervals(self):
        return included_intervals(self.data["records"]) if self.data["schema"] >= 2 else None


def skip_reason(row, ambiguous_names):
    if row["service"]:
        return "service"
    if row["conflict"]:
        return "conflict"
    sender = row["sender"]
    if (
        not sender.strip()
        or sender in ambiguous_names
        or ":" in sender
        or len(sender.splitlines()) != 1
        or any(ord(ch) < 32 for ch in sender)
    ):
        return "identity"
    body = row["text"]
    if (
        any(POSSIBLE_HEADER.match(line) for line in body.splitlines()[1:])
        or MARKER.search(body)
        or "(file attached)" in body.lower()
        or "\x00" in body
    ):
        return "ambiguous_text"
    return ""


def _text_losses(row, flags, meta):
    observations = {
        "reply": row["reply"] or "unknown_reply" in flags,
        "rich_text": re.search(r"<(?!br\b)[a-z]+", row["rich"]),
        "reactions": meta.get("reactions"),
        "forward": "forwarded_from" in meta or "forwarded_from_id" in meta,
        "structured": any(
            k in meta
            for k in ("other_media", "poll", "location_information", "contact_information", "game_title")
        ),
        "assumed_timezone": "assumed_timezone" in flags,
    }
    return [code for code, present in observations.items() if present]


RECORD_KINDS = {
    "all": "Все записи", "text": "Текст без вложений", "forward": "Пересланные сообщения",
    **{key: name for key, name in KIND_NAMES.items() if key != "text"},
}


def _video_entry(item, meta, row, source_hash, chat_id, source_ordinal, *, grouped=False):
    members = [member for member in item.get("members", ()) if member["kind"] in VIDEO_KINDS]
    source = members[0] if grouped else item
    digest_key = hashlib.sha256(encode([member["sha256"] for member in members]).encode()).hexdigest() if grouped else source["sha256"]
    labels = meta.get("file_names", {})
    if not isinstance(labels, dict):
        labels = {}
    name = safe_filename(labels.get(source["source"]) or meta.get("file_name") or source["source"])
    return {
        "key": video_decision_key(source_hash, chat_id, source_ordinal, digest_key, grouped),
        "name": name, "date": row["date_iso"] or row["date_raw"], "source": source["source"],
        "size": item["size"], "sha256": source["sha256"],
    }


def _group_video(entry, action, queue):
    queue.start(entry["name"])
    record = {"key": entry["key"], "scope": "group", "action": action,
              "original": {"status": "multiple_attachments"}}
    if action in {"document", "skip", "local"}:
        queue.finish()
    else:
        record["action"] = "blocked"
        entry.update(group="multiple_attachments", original=record["original"], source_kind="video",
                     prepared_kind="document", options=("document", "local", "skip"),
                     reason="multiple_attachments", recommended="document")
        queue.finish(entry)
    return record


def media_entry(item, meta, row, source_hash, chat_id, source_ordinal):
    if item["source"]:
        return _video_entry(item, meta, row, source_hash, chat_id, source_ordinal)
    original = source_digest(item)
    return {
        "key": video_decision_key(source_hash, chat_id, source_ordinal, original),
        "name": item["name"], "date": row["date_iso"] or row["date_raw"], "source": "",
        "size": item["size"], "sha256": original,
    }


def plan_import(
    project,
    chat_id,
    timezone_name="UTC",
    progress=quiet,
    *,
    profile="ios",
    sample=None,
    date_policy="historical",
    media_mode="text_media",
    first_record=1,
    last_record=None,
    record_ranges=None,
    write_line=None,
    video_policy=None,
    media_tools=None,
    video_work=None,
    video_decisions=None,
    media_queue=None,
    annotate=False,
    annotation_mode="detailed",
    annotation_language="ru",
    record_kind="all",
    binding_version=BINDING_VERSION,
    media_workflow=None,
    reuse_records=None,
    reused_paths=None,
):
    """Build the same ledger for a read-only preview and for a published package."""
    try:
        zone = None if timezone_name == "source" else ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        raise RecoveryError("Неизвестный часовой пояс IANA.") from None
    if (
        profile not in PROFILES
        or date_policy not in DATE_POLICIES
        or media_mode not in MEDIA_MODES
        or video_policy not in (None, *VIDEO_POLICIES)
        or record_kind not in RECORD_KINDS
        or binding_version not in (0, BINDING_VERSION)
        or annotation_mode not in NOTE_MODES
        or annotation_language not in NOTE_LANGUAGES
        or (sample is not None and sample < 1)
        or type(first_record) is not int
        or not 1 <= first_record <= 2**31 - 1
        or (
            last_record is not None
            and (type(last_record) is not int or not first_record <= last_record <= 2**31 - 1)
        )
    ):
        raise RecoveryError("Проверьте профиль, режим дат и границы выборки.")
    if record_ranges is not None:
        record_ranges = decode_intervals(record_ranges)
    video_decisions = validate_decisions(video_decisions)
    if media_workflow is None:
        media_workflow = bool(video_policy or video_decisions)
    if video_decisions and not video_policy and reuse_records is None:
        video_policy = "original"
    media_queue = media_queue or MediaQueue()
    starts = tuple(first for first, _ in record_ranges or ())
    date_policy = "historical"
    profile = PROFILES[profile]
    if (video_policy or media_workflow) and media_mode == "text_media" and media_tools is None:
        media_tools = MediaTools()
    with catalogue(project) as db:
        chat = db.execute("SELECT title FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not chat:
            raise RecoveryError("Диалог не найден в проекте.")
        title_unique = db.execute("SELECT count(*) FROM chats WHERE title=?", (chat["title"],)).fetchone()[0]==1
        source_props = properties(db)
        source_hash = source_props["sha256"]
        ambiguous = {
            r[0]
            for r in db.execute(
                """SELECT sender FROM messages WHERE chat=? AND service=0
                     GROUP BY sender HAVING count(DISTINCT sender_key)>1""",
                (chat_id,),
            )
        }
        total = db.execute("SELECT count(*) FROM messages WHERE chat=?", (chat_id,)).fetchone()[0]
        planned_media = 0
        if media_mode == "text_media" and (media_workflow or video_policy):
            planned_media = db.execute(
                """SELECT count(*) FROM messages m WHERE m.chat=? AND NOT m.service
                AND coalesce(json_extract(m.meta,'$.source_ordinal'),?) BETWEEN ? AND ?
                AND EXISTS (SELECT 1 FROM assets a WHERE a.msg=m.id AND (? OR a.kind IN ('video','round_video'))
                    AND a.status IN ('ok','unconfirmed_type'))""",
                (chat_id, first_record, first_record, last_record or 2**31 - 1, bool(media_workflow)),
            ).fetchone()[0]
        media_queue.begin(planned_media)
        rows = ordered_rows(db, chat_id)
        clock = WireClock(first_reliable_time(db, chat_id), zone, date_policy)
        duplicates = db.execute("SELECT count(*) FROM duplicates WHERE chat=?", (chat_id,)).fetchone()[0]
        issues = dict(db.execute("SELECT code,count(*) FROM issues WHERE chat=? GROUP BY code", (chat_id,)))
        media, records, losses = [], [], Counter()
        used_names = {}
        album_ids = set()
        text_hash, included, omitted_assets, selected = hashlib.sha256(), 0, 0, 0
        for i, row in enumerate(rows, 1):
            if i % 100 == 1:
                progress("convert", i, total)
            flags = {r[0] for r in db.execute("SELECT code FROM issues WHERE msg=?", (row["id"],))}
            meta = json.loads(row["meta"])
            source_ordinal = meta.get("source_ordinal", row["position"])
            in_window = first_record <= source_ordinal and (
                last_record is None or source_ordinal <= last_record
            )
            if record_ranges is not None and in_window:
                position = bisect_right(starts, source_ordinal) - 1
                in_window = position >= 0 and source_ordinal <= record_ranges[position][1]
            in_sample = sample is None or included < sample
            reason = skip_reason(row, ambiguous)
            notes, body, item = [], row["text"], None
            video = audio = recovery = reused_path = None
            identity = HistoricalIdentity.read(row, flags)
            assets = db.execute(
                """SELECT a.*,f.size,f.sha256,f.mime FROM assets a
                    LEFT JOIN files f ON f.path=a.path WHERE msg=? ORDER BY a.id""",
                (row["id"],),
            ).fetchall()
            structure = local_structure(meta, row["rich"])
            type_match = (
                record_kind == "all"
                or record_kind == "text" and bool(body) and not assets
                or record_kind == "forward" and structure["forward"]
                or any(asset["kind"] == record_kind for asset in assets)
            )
            selected += int(in_window and in_sample and type_match and not row["service"])
            if not reason:
                reason = (
                    "outside_selection" if not in_window else "type_selection" if not type_match
                    else "sample_limit" if not in_sample else ""
                )
            if media_mode == "text_only":
                omitted_assets += len(assets)
                has_media = bool(assets) or any(meta.get(key) is not None for key in STRUCTURED)
                if has_media:
                    notes.append("text_only_media")
                if not reason and not body.strip():
                    reason = "text_only_empty" if has_media else "empty"
            elif not reason:
                item = plan_media(
                    assets, meta, len(media) + 1, notes, documents_only=media_mode == "document",
                    missing_references=video_policy is None,
                )
                previous = (reuse_records or {}).get(source_ordinal)
                if previous:
                    old, old_item, reused_path = previous
                    item = dict(old_item) if old_item else None
                    if item:
                        suffix = Path(item["storage_name"]).suffix
                        item["name"] = f"gtr-{len(media) + 1:07d}{suffix}"
                        if not item.get("source"):
                            item.pop("display_name", None)
                        if "members" in item:
                            item["members"] = [dict(member) for member in item["members"]]
                    notes = list(old["losses"])
                    video = old.get("video_recovery")
                    audio = old.get("audio_recovery")
                    recovery = old.get("media_recovery")
                grouped_video = bool(item and any(member["kind"] in VIDEO_KINDS for member in item.get("members", [])))
                if not previous and video_policy and media_mode == "text_media" and grouped_video:
                    entry = _video_entry(item, meta, row, source_hash, chat_id, source_ordinal, grouped=True)
                    video = _group_video(entry, video_decisions.get(entry["key"], video_policy), media_queue)
                    if video["action"] == "document":
                        notes.append("group_video_document")
                    elif video["action"] == "skip":
                        notes = [key for key in notes if key != "grouped_media"]
                        item = plan_media(
                            [a for a in assets if a["kind"] not in VIDEO_KINDS], meta,
                            len(media) + 1, notes, missing_references=False,
                        )
                        notes.append("video_skipped")
                    elif video["action"] == "local":
                        item = None
                        notes.append("media_local_only")
                    else:
                        reason = "video_decision"
                        item = None
                if (
                    not previous and media_tools and video_policy and media_mode == "text_media"
                    and item and item["source"] and item["kind"] in VIDEO_KINDS
                ):
                    source = Path(project).joinpath("source", *safe_parts(item["source"]))
                    notes = [note for note in notes if note not in {"document_fallback", "media_attributes"}]
                    entry = _video_entry(item, meta, row, source_hash, chat_id, source_ordinal)
                    item, video = queue_video(
                        item, source, video_policy, media_tools, progress, video_work, media_queue, entry,
                        video_decisions.get(entry["key"]),
                    )
                    if video["action"] == "blocked":
                        reason = "video_decision"
                    elif key := {"document": "video_document", "skip": "video_skipped",
                                  "local": "media_local_only", "convert": "video_transcode"}.get(video["action"]):
                        notes.append(key)
                if (
                    not previous and media_tools and video_policy and media_mode == "text_media"
                    and item and item["source"] and item["kind"] in {"audio", "voice"}
                ):
                    source = Path(project).joinpath("source", *safe_parts(item["source"]))
                    audio = media_tools.audio(source, item, progress)
                    attributes = audio["attributes"]
                    if attributes:
                        notes = [key for key in notes if key not in {"document_fallback", "media_attributes"}]
                        item.update(attributes=attributes, representation="specialized_document")
                    elif audio["status"] == "audio_unreadable" and not media_workflow:
                        item = None
                        notes.append("audio_unreadable")
                    elif not media_workflow:
                        notes.append("audio_unconfirmed")
                        if binding_version:
                            item.update(attributes={}, representation="document", force_file=True)
                if not previous and item and media_workflow and not video:
                    entry = media_entry(item, meta, row, source_hash, chat_id, source_ordinal)
                    choice = video_decisions.get(entry["key"])
                    if item["source"] and (media_mode == "text_media" or choice):
                        source = Path(project).joinpath("source", *safe_parts(item["source"]))
                        item, recovery = queue_attachment(
                            item, source, meta, progress, video_work, media_queue, entry, choice, media_tools, audio=audio
                        )
                    elif choice in SKIP_ACTIONS:
                        recovery = {"key": entry["key"], "action": choice, "source_kind": item["kind"],
                                    "original_sha256": entry["sha256"]}
                        item = None
                    if recovery:
                        action = recovery["action"]
                        notes = [key for key in notes if key not in {"document_fallback", "media_attributes"}]
                        if action == "blocked":
                            reason = "attachment_decision"
                        elif note := {
                            "local": "media_local_only", "skip": "media_skipped", "image": "media_png",
                            "document": "document_fallback", "sticker": "media_attributes",
                        }.get(action):
                            notes.append(note)
                if (item and media_workflow and not previous
                        and item["upload_as"] == "document" and not item.get("attributes")):
                    item["force_file"] = True
                if not reason and not body and not item:
                    reason = "empty"
            date, change = (
                clock.next(row["date_iso"], not identity.date_reliable) if not reason else (None, "")
            )
            if not reason and date is None:
                reason = next(
                    (
                        code
                        for code in ("date_conflict", "ambiguous_local_time", "nonexistent_local_time")
                        if code in flags
                    ),
                    "date",
                )
            record = {
                "source_id": row["source_id"],
                "source_ordinal": source_ordinal,
                "author": row["sender"],
                "source_author_id": identity.original_author_id,
                "original_date": row["date_iso"] or row["date_raw"],
                "reply_source_id": row["reply"],
                "source_identity": asdict(identity),
                "resolved_reply": row["resolved_reply"] if row["resolved_reply_chat"] == chat_id else None,
                "original_text_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "original_media": [
                    {k: a[k] for k in ("kind", "original", "path", "status", "size", "sha256")}
                    for a in assets
                ],
                "included": not reason,
                "losses": notes,
            }
            structure.update({
                "line_breaks": "\n" in body, "unicode": not body.isascii(),
                "emoji": any(0x1F000 <= ord(letter) <= 0x1FAFF for letter in body),
            })
            if present := {key: True for key, value in structure.items() if value}:
                record["structure"] = present
            if (album_id := meta.get("album_id")) and in_window:
                album_ids.add(str(album_id))
            if video:
                record["video_recovery"] = video
            if audio:
                record["audio_recovery"] = audio
            if recovery:
                record["media_recovery"] = recovery
            if media_mode == "text_only":
                record["omitted_media_count"] = len(assets)
            if reason:
                if item and item.get("transform") and video_work is not None:
                    (video_work / item["name"]).unlink()
                notes.append(reason)
            else:
                if item:
                    assign_names(item, meta, used_names)
                    if binding_version:
                        bind(source_hash, chat_id, record, item, len(media) + 1, date, profile)
                    if reused_path:
                        reused_paths[item["name"]] = reused_path
                if not row["source_id"]:
                    notes.append("missing_source_id")
                if change:
                    notes.append(change)
                annotations = historical_notes(meta, annotation_mode, annotation_language) if annotate else {}
                if annotations:
                    record["annotations"] = annotations
                wire_body = annotated_text(body, annotations)
                line = wire_line(profile, date, row["sender"], wire_body, wire_name(item) if item else None,
                                 bound=bool(binding_version)).encode("utf-8")
                text_hash.update(line)
                if write_line is not None:
                    write_line(line)
                included += 1
                record["ordinal"] = included
                record["import_date"] = date.isoformat(timespec="seconds")
                record["import_utc"] = clock.utc(date, row["date_iso"])
                record["text_sha256"] = hashlib.sha256(wire_body.encode("utf-8")).hexdigest()
                if item:
                    record["media"] = item["name"]
                    media.append(item)
                if not (reuse_records or {}).get(source_ordinal):
                    notes.extend(_text_losses(row, flags, meta))
                if media_workflow:
                    notes[:] = list(dict.fromkeys(notes))
            records.append(record)
            losses.update(notes)
    media_queue.settle()
    return {
        "schema": 3,
        "filename_version": 1,
        **({"binding_version": binding_version} if binding_version else {}),
        **({"media_workflow": MEDIA_WORKFLOW} if media_workflow else {}),
        "source_conversation": source_props.get(f"chat_peer:{chat_id}", ""),
        "source_title_unique": title_unique,
        "profile": profile,
        "timezone": timezone_name,
        "source_sha256": source_hash,
        "source_kind": source_props["source_type"],
        "date_policy": date_policy,
        "media_mode": media_mode,
        **({"video_policy": video_policy} if video_policy else {}),
        **({"video_decisions": video_decisions} if video_decisions else {}),
        **({"media_decisions_required": media_queue.entries} if media_queue.entries else {}),
        **({"annotate": True} if annotate else {}),
        **({"annotation_mode": annotation_mode, "annotation_language": annotation_language}
           if annotate and (annotation_mode, annotation_language) != ("detailed", "ru") else {}),
        "album_count": len(album_ids),
        "media_count": len(media),
        "source_records": total,
        "selected_records": selected,
        "selection": {
            "first_record": first_record,
            "last_record": last_record,
            "sample": sample,
            **({"record_kind": record_kind} if record_kind != "all" else {}),
            **({"ranges": record_ranges} if record_ranges is not None else {}),
        },
        "omitted_assets": omitted_assets,
        "timeline": clock.audit,
        "source_diagnostics": {"duplicates": duplicates, "issues": issues},
        "chat_id": chat_id,
        "chat_title": chat["title"],
        "source_span": [
            next((r["source_ordinal"] for r in records if r["included"]), 0),
            next((r["source_ordinal"] for r in reversed(records) if r["included"]), 0),
        ],
        "global_limits": [
            "server_ids",
            "read_states",
            "server_authorship",
            "timezone_interpretation",
            "original_chat_positions",
        ],
        "included": included,
        "skipped": total - included,
        "losses": dict(losses),
        "media": media,
        "records": records,
        "text_sha256": text_hash.hexdigest(),
    }


def prepare(
    project,
    chat_id,
    dest,
    timezone_name="UTC",
    progress=quiet,
    *,
    profile="ios",
    sample=None,
    date_policy="historical",
    media_mode="text_media",
    fixture=None,
    first_record=1,
    last_record=None,
    record_ranges=None,
    test_reset_id=None,
    video_policy=None,
    media_tools=None,
    video_decisions=None,
    media_queue=None,
    annotate=False,
    annotation_mode="detailed",
    annotation_language="ru",
    record_kind="all",
    binding_version=BINDING_VERSION,
    media_workflow=None,
    reuse_records=None,
    selection_snapshot=None,
):
    from .reports import package_html

    project, dest = local(Path(project)), local(Path(dest))
    if fixture not in (None, "text", "media"):
        raise RecoveryError("Выберите вид синтетического пакета.")
    if test_reset_id is not None and (type(test_reset_id) is not int or test_reset_id < 0):
        raise RecoveryError("Недопустимый номер тестового цикла.")
    with catalogue(project) as db:
        original = Path(properties(db)["original"])
    media_queue = media_queue or MediaQueue()
    reused_paths = {}
    with new_directory(dest, (project, original)) as work:
        with (work / "_chat.txt").open("xb") as transcript:
            data = plan_import(
                project,
                chat_id,
                timezone_name,
                progress,
                profile=profile,
                sample=sample,
                date_policy=date_policy,
                media_mode=media_mode,
                first_record=first_record,
                last_record=last_record,
                record_ranges=record_ranges,
                write_line=transcript.write,
                video_policy=video_policy,
                media_tools=media_tools,
                video_work=work / ".video",
                video_decisions=video_decisions,
                media_queue=media_queue,
                annotate=annotate,
                annotation_mode=annotation_mode,
                annotation_language=annotation_language,
                record_kind=record_kind,
                binding_version=binding_version,
                media_workflow=media_workflow,
                reuse_records=reuse_records,
                reused_paths=reused_paths,
            )
        if data.get("media_decisions_required"):
            raise MediaDecisionRequired(data["media_decisions_required"])
        if selection_snapshot is not None:
            from .editor import check_snapshot

            actual = included_intervals(data["records"])
            if record_ranges is None or actual is None or difference(actual, decode_intervals(record_ranges)):
                raise PackageValidationError("SELECTION_STALE", "write_package", "selection_snapshot")
            data["selection"]["ranges"] = actual
            data["selection_snapshot"] = selection_snapshot
            check_snapshot(project, data, progress)
        elif record_ranges is not None:
            actual = included_intervals(data["records"])
            reviewed = decode_intervals(record_ranges)
            if actual != reviewed:
                skipped = included_intervals([
                    {**r, "included": True} for r in data["records"]
                    if not r["included"] and "empty" in r["losses"]
                    and (r.get("media_recovery") or r.get("video_recovery") or {}).get("action") in SKIP_ACTIONS
                ])
                if not actual or difference(actual, reviewed) or difference(difference(reviewed, actual), skipped):
                    raise RecoveryError("Состав доступных записей изменился. Повторите сверку исходного проекта.")
                data["selection"]["ranges"] = actual
        if fixture is not None:
            data["fixture"] = fixture
        if test_reset_id is not None:
            data["test_reset_id"] = test_reset_id
        (work / "media").mkdir()
        estimated = sum(item["size"] for item in data["media"])
        if estimated > MAX_TOTAL_BYTES:
            raise RecoveryError("Объём вложений превышает локальный предел пакета.")
        _require_space(work, sum(item["size"] for item in data["media"]
                                 if not item.get("transform") or item["name"] in reused_paths))
        media_owners = {row["media"]: row for row in data["records"] if row.get("media")}
        for i, item in enumerate(data["media"], 1):
            path = work / "media" / item["storage_name"]
            if source := reused_paths.get(item["name"]):
                checkpoint = lambda i=i: progress("media", i - 1, len(data["media"]))
                _copy_bounded(source, path, item["size"], checkpoint, stage="write_package",
                              file=item["name"], record=media_owners.get(item["name"]))
            elif item.get("transform"):
                (work / ".video" / item["storage_name"]).replace(path)
            else:
                publish_media(
                    project,
                    item,
                    path,
                    lambda i=i: progress("media", i - 1, len(data["media"])),
                )
            progress("media", i, len(data["media"]))
        if (work / ".video").is_dir():
            (work / ".video").rmdir()
        if sum(item["size"] for item in data["media"]) > MAX_TOTAL_BYTES:
            raise RecoveryError("Объём вложений превышает локальный предел пакета.")
        with (work / "manifest.json").open("w", encoding="utf-8", newline="\n") as output:
            encode_manifest(data, output, progress)
        checked = load_bundle(work, progress)
        (work / "report.html").write_text(package_html(data), encoding="utf-8")
        progress("finish", 1, 1)
    media_queue.settle(ready=True)
    return Bundle(dest, checked.data)


def prepare_eligible(
    project, bundle, dest, eligible, progress=quiet, *, video_decisions=None,
    media_queue=None, media_tools=None, video_policy=None, repair=False,
):
    """Regenerate only the reviewed eligible records from the associated source project."""
    from .integrity import check_local

    selected = bundle.record_intervals()
    eligible = decode_intervals(eligible)
    if not eligible or selected is None or difference(eligible, selected) or bundle.data["schema"] != 3:
        raise RecoveryError("Выборка новых записей недоступна для этого пакета.")
    if repair:
        checked = load_bundle(bundle.root, progress)
        if checked.manifest_hash() != bundle.manifest_hash():
            raise RecoveryError("Пакет изменился после выбора. Откройте его снова.")
        bundle = checked
    if project is None or check_local(bundle, project, progress)["status"] != "verified":
        raise RecoveryError("Откройте и проверьте исходный проект этого пакета.")
    profile = next(name for name, value in PROFILES.items() if value == bundle.data["profile"])
    overrides = dict(validate_decisions(video_decisions))
    originals = None
    if repair:
        by_name = {item["name"]: item for item in bundle.data["media"]}
        originals = {}
        changed = set()
        for row in bundle.data["records"]:
            if not row["included"]:
                continue
            item = by_name.get(row.get("media"))
            if item and (key := recovery_entry(bundle, row, item)["key"]) in overrides:
                changed.add(key)
            else:
                originals[row["source_ordinal"]] = (row, item, bundle.media_path(item) if item else None)
        if not changed or changed != overrides.keys():
            raise RecoveryError("Решение не соответствует вложениям сохранённого пакета.")
    for row in bundle.data["records"]:
        previous = row.get("video_recovery", {})
        key = previous.get("key")
        if previous.get("method") == "remux" and overrides.get(key) == "compatible":
            overrides[key] = "reencode"
    return prepare(
        project,
        bundle.data["chat_id"],
        dest,
        bundle.data["timezone"],
        progress,
        profile=profile,
        date_policy=bundle.data["date_policy"],
        media_mode=bundle.data.get("media_mode", "text_media"),
        fixture=bundle.data.get("fixture"),
        record_ranges=eligible,
        test_reset_id=bundle.data.get("test_reset_id"),
        video_policy=video_policy or bundle.data.get("video_policy"),
        video_decisions={**bundle.data.get("video_decisions", {}), **overrides},
        media_queue=media_queue,
        media_tools=media_tools,
        annotate=bundle.data.get("annotate", False),
        annotation_mode=bundle.data.get("annotation_mode", "detailed"),
        annotation_language=bundle.data.get("annotation_language", "ru"),
        record_kind=bundle.data.get("selection", {}).get("record_kind", "all"),
        media_workflow=True if repair or bundle.data.get("media_workflow") else None,
        reuse_records=originals,
        selection_snapshot=bundle.data.get("selection_snapshot"),
    )


def encode_manifest(data, output, progress):
    """Write large manifests by record, producing the same canonical key order."""
    output.write("{")
    for index, key in enumerate(sorted(data)):
        if index:
            output.write(",")
        output.write(encode(key) + ":")
        if key not in {"records", "media"}:
            output.write(encode(data[key]))
            continue
        output.write("[")
        total = len(data[key])
        for position, row in enumerate(data[key]):
            if position % 100 == 0:
                progress("write_manifest", position, total)
            if position:
                output.write(",")
            output.write(encode(row))
        output.write("]")
    output.write("}\n")


def _package_field(valid, field, expected, actual, *, code="PACKAGE_SCHEMA_INVALID",
                   stage="load_manifest", file="manifest.json", record=None):
    if not valid:
        raise PackageValidationError(code, stage, field, file=file, record=record, expected=expected, actual=actual)


def _manifest(data, bundle):
    _package_field(isinstance(data, dict), "manifest", "объект JSON", data)
    for field, default, supported in (
        ("schema", None, (1, 2, 3)), ("filename_version", 1, (1,)),
        ("binding_version", 0, (0, BINDING_VERSION)), ("media_workflow", 0, (0, MEDIA_WORKFLOW)),
    ):
        value = data.get(field, default)
        _package_field(type(value) is int and value in supported, field, ", ".join(map(str, supported)), value,
                       code="UNSUPPORTED_PACKAGE_VERSION")
    if data.get("binding_version") and not data.get("filename_version"):
        raise AttachmentBindingError("filename_version", file="manifest.json", expected=1)
    schema = data["schema"]
    _package_field(data.get("profile") in PROFILES.values(), "profile", ", ".join(PROFILES.values()), data.get("profile"))
    tz = data.get("timezone")
    try:
        if tz != "source" or schema < 3:
            ZoneInfo(tz)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        raise PackageValidationError("PACKAGE_SCHEMA_INVALID", "load_manifest", "timezone",
                                     file="manifest.json", expected="часовой пояс IANA или source", actual=tz) from None
    if schema == 3:
        _package_field(data.get("date_policy") in DATE_POLICIES, "date_policy", "известный режим даты", data.get("date_policy"))
    if "test_reset_id" in data:
        value = data["test_reset_id"]
        _package_field(type(value) is int and value >= 0, "test_reset_id", "целое число от 0", value)
    for field in ("records", "media"):
        value = data.get(field)
        _package_field(isinstance(value, list) and all(isinstance(entry, dict) for entry in value),
                       field, "массив объектов JSON", value)
    for field in ("included", "skipped", "chat_id"):
        value = data.get(field)
        _package_field(type(value) is int and (field == "chat_id" or value >= 0), field, "целое число", value)
    for field in ("source_sha256", "text_sha256"):
        value = data.get(field)
        _package_field(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value), field, "SHA-256", value)
    losses = data.get("losses")
    _package_field(isinstance(losses, dict) and all(type(v) is int and v >= 0 for v in losses.values()),
                   "losses", "неотрицательные счётчики", losses)
    records = data["records"]
    previous = 0
    for row in records:
        _package_field(isinstance(row.get("media", ""), str), "records.media", "имя вложения", row.get("media"), record=row)
        _package_field("included" in row, "records.included", "признак включения", None, record=row)
        for field in ("video_recovery", "media_recovery"):
            value = row.get(field)
            if value:
                _package_field(isinstance(value, dict), field, "решение JSON", value, record=row)
                key = value.get("key")
                _package_field(key is None or isinstance(key, str), field + ".key", "ключ решения", key, record=row)
        if "filename_version" in data:
            position = row.get("source_ordinal")
            _package_field(type(position) is int and previous < position <= 2**31 - 1,
                           "source_ordinal", f"номер после {previous}", position, record=row)
            previous = position
            for field, kind in (("source_id", str), ("source_identity", dict), ("original_media", list)):
                _package_field(isinstance(row.get(field), kind), field, kind.__name__, row.get(field), record=row)
            value = row.get("original_text_sha256")
            _package_field(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value),
                           "original_text_sha256", "SHA-256", value, record=row)
            _package_field(all(isinstance(a, dict) for a in row["original_media"]),
                           "original_media", "массив объектов JSON", row["original_media"], record=row)
    if "filename_version" in data:
        conversation = data.get("source_conversation", "")
        _package_field(isinstance(conversation, str)
                       and re.fullmatch(r"(?:(?:user|chat|channel)-?[1-9]\d{0,18})?", conversation),
                       "source_conversation", "идентификатор диалога или пустая строка", conversation)
        _package_field(type(data.get("source_title_unique", True)) is bool,
                       "source_title_unique", "true или false", data.get("source_title_unique"))
    mode = data.get("media_mode", "text_media")
    if "media_mode" in data:
        _package_field(isinstance(mode, str) and mode in MEDIA_MODES, "media_mode", "известный режим", mode)
        for field, amount in (("media_count", len(data["media"])), ("source_records", len(records))):
            _package_field(data.get(field) == amount, field, amount, data.get(field))
        _package_field(data.get("fixture") in (None, "text", "media"), "fixture", "text, media или пустое поле", data.get("fixture"))
        if mode == "text_only":
            _package_field(not data["media"], "media_count", 0, len(data["media"]))
            for row in records:
                expected = len(row["original_media"])
                _package_field(row.get("omitted_media_count") == expected, "omitted_media_count",
                               expected, row.get("omitted_media_count"), record=row)
            expected = sum(row["omitted_media_count"] for row in records)
            _package_field(data.get("omitted_assets") == expected, "omitted_assets", expected, data.get("omitted_assets"))
    policy = data.get("video_policy")
    _package_field(policy in (None, *VIDEO_POLICIES), "video_policy", "известный профиль", policy)
    if "selection" in data:
        selection = data["selection"]
        _package_field(isinstance(selection, dict), "selection", "объект JSON", selection)
        first, last, sample = (selection[k] for k in ("first_record", "last_record", "sample"))
        _package_field(selection.get("record_kind", "all") in RECORD_KINDS,
                       "selection.record_kind", "известный тип записи", selection.get("record_kind"))
        _package_field(type(first) is int and 1 <= first <= 2**31 - 1, "selection.first_record", "положительный номер", first)
        _package_field(last is None or type(last) is int and first <= last <= 2**31 - 1,
                       "selection.last_record", f"номер от {first} или пустое поле", last)
        _package_field(sample is None or type(sample) is int and sample >= max(1, data["included"]),
                       "selection.sample", f"не меньше {data['included']} или пустое поле", sample)
        selected = data.get("selected_records")
        _package_field(type(selected) is int and data["included"] <= selected <= len(records),
                       "selected_records", f"от {data['included']} до {len(records)}", selected)
        for row in records:
            if row["included"]:
                pos = row.get("source_ordinal")
                _package_field(type(pos) is int and first <= pos <= (last or 2**31 - 1),
                               "source_ordinal", f"в диапазоне {first}–{last or 2**31 - 1}", pos, record=row)
        if "ranges" in selection:
            try:
                matched = decode_intervals(selection["ranges"]) == bundle.record_intervals()
            except (TypeError, ValueError):
                matched = False
            _package_field(matched, "selection.ranges", "диапазоны включённых записей", selection["ranges"])
    if "selection_snapshot" in data:
        value = data["selection_snapshot"]
        keys = {"schema", "project_id", "preset", "revision", "project_sha256", "ranges_sha256"}
        valid = (schema == 3 and isinstance(value, dict) and value.keys() == keys
                 and "ranges" in data.get("selection", {}) and type(value["schema"]) is int and value["schema"] == 1
                 and all(isinstance(value[k], str) and re.fullmatch(r"[a-f0-9]{64}", value[k])
                         for k in ("project_id", "project_sha256", "ranges_sha256"))
                 and isinstance(value["preset"], str) and len(value["preset"]) <= 80
                 and not any(ord(c) < 32 for c in value["preset"])
                 and isinstance(value["revision"], str)
                 and re.fullmatch(r"initial|[a-f0-9]{32}", value["revision"]))
        _package_field(valid, "selection_snapshot", "снимок выборки схемы 1", value)


def _file_error(error, stage, file, *, record=None):
    missing = isinstance(error, FileNotFoundError)
    return PackageValidationError("FILE_NOT_FOUND" if missing else "PACKAGE_IO_ERROR", stage,
                                  "file", file=file, record=record, expected="доступный локальный файл",
                                  actual="отсутствует" if missing else getattr(error, "winerror", None) or error.errno)


def _media_ledger(bundle, progress):
    data = bundle.data
    try:
        choices = validate_decisions(data.get("video_decisions"))
    except RecoveryError:
        raise PackageValidationError("PACKAGE_SCHEMA_INVALID", "load_manifest", "video_decisions",
                                     file="manifest.json", expected="известные решения по вложениям",
                                     actual=data.get("video_decisions")) from None
    records = {row["media"]: row for row in data["records"] if row.get("media")}
    names, stored, planned, media_by_name = set(), set(), [], {}
    total_bytes = 0
    for i, item in enumerate(data["media"], 1):
        progress("verify", i, len(data["media"]))
        name = item.get("name")
        storage = item.get("storage_name", name)
        record = records.get(name, {}) if isinstance(name, str) else {}
        if not name or not isinstance(name, str) or safe_filename(name) != name or name.casefold() in names:
            raise AttachmentBindingError("name", file=name, record=record, expected="уникальное безопасное имя", actual=name)
        if not isinstance(storage, str) or not MEDIA_NAME.fullmatch(storage) or storage.casefold() in stored:
            raise AttachmentBindingError("storage_name", file=name, record=record, expected="уникальное локальное имя", actual=storage)
        marker = wire_name(item)
        if not isinstance(marker, str):
            raise AttachmentBindingError("wire_name", file=name, record=record, expected="имя для привязки", actual=marker)
        named = "filename_version" in data
        if named:
            display = item.get("display_name")
            if ("storage_name" not in item or not display or safe_filename(display) != display
                    or item.get("filename_source") not in {"export_metadata", "export_basename", "generated"}):
                raise AttachmentBindingError("display_name", file=name, record=record, expected="имя из экспорта или сгенерированное",
                                             actual=display)
        elif any(k in item for k in ("storage_name", "display_name", "filename_source")):
            raise AttachmentBindingError("filename_version", file=name, record=record, expected=1)
        stage = "verify_media"
        for field, valid, expected in (
            ("kind", isinstance(item.get("kind"), str), "тип вложения"),
            ("size", type(item.get("size")) is int and item["size"] > 0, "положительный размер в байтах"),
            ("sha256", isinstance(item.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", item["sha256"]), "SHA-256"),
        ):
            _package_field(valid, field, expected, item.get(field), stage=stage, file=name, record=record)
        total_bytes += item["size"]
        _package_field(total_bytes <= MAX_TOTAL_BYTES, "media_total_bytes", MAX_TOTAL_BYTES, total_bytes,
                       stage=stage, file=name, record=record)
        try:
            path = bundle.media_path(item)
            size = path.stat().st_size
            _package_field(size == item["size"], "size", item["size"], size, code="MEDIA_HASH_MISMATCH",
                           stage=stage, file=name, record=record)
            actual = digest(path, lambda i=i: progress("verify", i, len(data["media"])))
            _package_field(actual == item["sha256"], "sha256", item["sha256"], actual, code="MEDIA_HASH_MISMATCH",
                           stage=stage, file=name, record=record)
            validate_media_input(item, path, strict="media_mode" in data)
        except OSError as error:
            raise _file_error(error, stage, name, record=record) from None
        except MediaInputError as error:
            raise PackageValidationError("MEDIA_ATTRIBUTES_INVALID", stage, error.field, file=name, record=record,
                                         expected=error.expected, actual=error.actual) from None
        if data.get("media_mode") == "document":
            for field, value, expected in (
                ("upload_as", item["upload_as"], "document"), ("attributes", bool(item.get("attributes")), False),
                ("force_file", bool(item.get("force_file")) or not item.get("source"), True),
            ):
                _package_field(value == expected, field, expected, value, code="MEDIA_ATTRIBUTES_INVALID",
                               stage=stage, file=name, record=record)
        _validate_transform(data, item, record, choices)
        names.add(name.casefold())
        stored.add(storage.casefold())
        planned.append(marker)
        media_by_name[name] = (i, item)
    return names, planned, media_by_name


def _validate_transform(data, item, row, choices):
    transform = item.get("transform")
    if not transform:
        return
    source_hash = transform.get("original_sha256") if isinstance(transform, dict) else None
    context = {"code": "MEDIA_ATTRIBUTES_INVALID", "stage": "verify_media", "file": item["name"], "record": row}
    _package_field(isinstance(source_hash, str) and re.fullmatch(r"[a-f0-9]{64}", source_hash),
                   "transform.original_sha256", "SHA-256 оригинала", source_hash, **context)
    if transform.get("type") == "png_image":
        decision = row.get("media_recovery")
        valid = (isinstance(decision, dict) and decision.get("action") == "image"
                 and choices.get(decision.get("key")) == "image"
                 and data.get("media_workflow") and item["mime"] == "image/png"
                 and type(decision.get("alpha")) is bool and item.get("force_file") is decision["alpha"]
                 and source_hash == decision.get("original_sha256")
                 and all(item[key] == decision.get(key) for key in ("size", "sha256")))
        expected = "PNG, соответствующий решению по исходному файлу"
    else:
        decision = row.get("video_recovery", {})
        converted = (isinstance(decision, dict) and decision.get("action") == "convert"
                     and choices.get(decision.get("key")) in {"compatible", "reencode", "remux", "fidelity", "compact"})
        valid = (transform.get("type") == "mp4_h264_aac" and item["mime"] == "video/mp4"
                 and (data.get("video_policy") in {"compatible", "remux", "fidelity", "compact"} or converted)
                 and item.get("attributes", {}).get("video_codec") == "h264")
        expected = "MP4 H.264, соответствующий решению по исходному видео"
    _package_field(valid, "transform", expected, transform.get("type"), **context)


def _transcript(bundle, names, planned, media_by_name):
    data = bundle.data
    include = iter(row for row in data["records"] if row["included"])
    headers, markers, previous_position, previous_date = 0, [], 0, ""
    binding = data.get("binding_version", 0)
    with local(bundle.transcript).open(encoding="utf-8", newline="\n") as stream:
        for stamp, author, body, marker in read_records(stream, data["profile"]):
            headers += 1
            if marker:
                markers.append(marker)
            row = next(include, None)
            ctx = {"stage": "verify_text", "file": "_chat.txt", "record": row}
            _package_field(row is not None, "included", data["included"], headers, **ctx)
            reference = row.get("media", "")
            asset = media_by_name.get(reference)
            expected = wire_name(asset[1]) if asset else reference
            if ((binding or data["schema"] >= 2) and marker != expected
                    or binding and (reference and not asset or not asset and "attachment_id" in row)):
                raise AttachmentBindingError("attached", file=reference or "_chat.txt", record=row,
                                             expected=expected, actual=marker)
            if data.get("media_mode") == "text_only":
                _package_field(bool(body.strip()), "text_only", "непустая запись", "пустая запись", **ctx)
            if data["schema"] >= 2:
                code = "MANIFEST_MISMATCH"
                _package_field(row["author"] == author, "author", "автор из манифеста", "имя расходится", code=code, **ctx)
                body_hash = hashlib.sha256(body.encode()).hexdigest()
                _package_field(row["text_sha256"] == body_hash, "text_sha256", row["text_sha256"], body_hash, code=code, **ctx)
                try:
                    date = datetime.fromisoformat(row.get("import_date"))
                except (TypeError, ValueError):
                    raise PackageValidationError("PACKAGE_SCHEMA_INVALID", "verify_text", "import_date",
                                                 file="_chat.txt", record=row, expected="дата и время ISO 8601",
                                                 actual=row.get("import_date")) from None
                fmt = "%d/%m/%Y, %H:%M:%S" if data["profile"] == PROFILE else "%d/%m/%y, %H:%M:%S"
                if binding and asset:
                    check_binding(data, row, asset[1], asset[0], date)
                _package_field(date.strftime(fmt) == stamp, "import_date", date.strftime(fmt), stamp, code=code, **ctx)
                position = row["source_ordinal"]
                ordered = data["schema"] == 2 or data["date_policy"] == "ordered"
                _package_field(not ordered or row["import_date"] > previous_date,
                               "import_date", "дата после " + previous_date, row["import_date"], **ctx)
                _package_field(type(position) is int and position > previous_position,
                               "source_ordinal", f"после {previous_position}", position, **ctx)
                _package_field(row["ordinal"] == headers, "ordinal", headers, row["ordinal"], **ctx)
                previous_date, previous_position = row["import_date"], position
    matched = markers == planned if "media_mode" in data else {marker.casefold() for marker in markers} == names
    if len(markers) != len(names) or not matched or binding and len(set(planned)) != len(planned):
        raise AttachmentBindingError("attached", file="_chat.txt", expected=f"{len(names)} точных привязок в порядке записей",
                                     actual=f"{len(markers)} маркеров")
    included = sum(bool(row["included"]) for row in data["records"])
    for field, expected, actual in (
        ("included", data["included"], headers), ("records.included", headers, included),
        ("skipped", data["skipped"], len(data["records"]) - headers),
    ):
        _package_field(expected == actual, field, expected, actual, stage="verify_text", file="_chat.txt")


def _read_package_manifest(root, stage="load_manifest"):
    file = "manifest.json"
    try:
        path = local(root / file)
        size = path.stat().st_size
        _package_field(size <= 128 * 1024**2, "manifest_size", 128 * 1024**2, size, stage=stage)
        with path.open(encoding="utf-8-sig") as stream:
            return json.load(stream)
    except (PackageValidationError, Cancelled):
        raise
    except OSError as error:
        raise _file_error(error, stage, file) from None
    except RecoveryError as error:
        raise PackageValidationError("PACKAGE_IO_ERROR", stage, "file", file=file,
                                     expected="доступный локальный файл", reason=str(error)) from None
    except (UnicodeError, ValueError) as error:
        raise PackageValidationError("PACKAGE_SCHEMA_INVALID", stage, "manifest", file=file,
                                     expected="манифест JSON в UTF-8", actual=type(error).__name__) from None


def check_manifest_identity(bundle, stage):
    data = _read_package_manifest(bundle.root, stage)
    actual = hashlib.sha256(encode(data).encode()).hexdigest()
    expected = bundle.manifest_hash()
    _package_field(expected == actual, "manifest_sha256", expected, actual, code="MANIFEST_MISMATCH", stage=stage)


def load_bundle(root, progress=quiet):
    stage, file = "load_manifest", "manifest.json"
    try:
        root = local(Path(root))
        data = _read_package_manifest(root, stage)
        bundle = Bundle(root, data)
        _manifest(data, bundle)
        stage, file = "verify_text", "_chat.txt"
        text = local(bundle.transcript)
        size = text.stat().st_size
        _package_field(size <= MAX_TOTAL_BYTES, "text_size", MAX_TOTAL_BYTES, size, stage=stage, file=file)
        actual = digest(text, lambda: progress("verify_text", 0, 0))
        _package_field(actual == data["text_sha256"], "text_sha256", data["text_sha256"], actual,
                       code="MANIFEST_MISMATCH", stage=stage, file=file)
        stage, file = "verify_media", "manifest.json"
        names, planned, media_by_name = _media_ledger(bundle, progress)
        stage, file = "verify_text", "_chat.txt"
        _transcript(bundle, names, planned, media_by_name)
        return bundle
    except (PackageValidationError, Cancelled):
        raise
    except OSError as error:
        raise _file_error(error, stage, file) from None
    except RecoveryError as error:
        raise PackageValidationError("PACKAGE_IO_ERROR", stage, "file", file=file,
                                     expected="доступный локальный файл", reason=str(error)) from None
    except (ValueError, KeyError, TypeError, UnicodeError, AttributeError) as error:
        field = error.args[0] if isinstance(error, KeyError) else "transcript" if stage == "verify_text" else "manifest"
        raise PackageValidationError("PACKAGE_SCHEMA_INVALID", stage, field, file=file,
                                     expected="данные пакета по схеме gTR", actual=type(error).__name__) from None


def _require_space(path, size, stage="write_package"):
    free = shutil.disk_usage(path).free
    if free < size:
        raise PackageValidationError("PACKAGE_STORAGE_INSUFFICIENT", stage, "free_bytes",
                                     file=path.anchor.rstrip("/\\") or path, expected=size, actual=free)


def _copy_bounded(source, destination, size, checkpoint, *, stage="copy_snapshot", record=None, file=None):
    file = file or source.name
    code = "MANIFEST_MISMATCH" if source.name == "_chat.txt" else "MEDIA_HASH_MISMATCH"
    remaining = size
    try:
        with local(source).open("rb") as reader, destination.open("xb") as writer:
            while remaining:
                checkpoint()
                chunk = reader.read(min(BLOCK, remaining))
                if not chunk:
                    raise PackageValidationError(code, stage, "size", file=file, record=record,
                                                 expected=size, actual=size - remaining)
                writer.write(chunk)
                remaining -= len(chunk)
            if reader.read(1):
                raise PackageValidationError(code, stage, "size", file=file, record=record, expected=size,
                                             actual=f"не менее {size + 1}")
    except OSError as error:
        if error.errno == errno.ENOSPC or getattr(error, "winerror", None) in (39, 112):
            raise PackageValidationError("PACKAGE_STORAGE_INSUFFICIENT", stage, "free_bytes", file=file, record=record,
                                         expected=max(1, remaining), actual=shutil.disk_usage(destination.parent).free) from None
        raise _file_error(error, stage, file, record=record) from None


@contextmanager
def owner_copy(bundle, progress=quiet):
    """Keep the exact reviewed bytes across login prompts, preflight and finalization."""
    with tempfile.TemporaryDirectory(prefix="gtr-owner-") as tmp:
        root = local(Path(tmp))
        private_directory(root)
        copy_package(bundle, root, progress)
        yield load_bundle(root, progress)


def copy_package(bundle, root, progress):
    manifest = encode(bundle.data).encode("utf-8")
    try:
        transcript_size = local(bundle.transcript).stat().st_size
    except OSError as error:
        raise _file_error(error, "copy_snapshot", "_chat.txt") from None
    required = len(manifest) + transcript_size + sum(item["size"] for item in bundle.data["media"])
    _require_space(root, required, "copy_snapshot")
    (root / "media").mkdir()
    (root / "manifest.json").write_bytes(manifest)
    _copy_bounded(bundle.transcript, root / "_chat.txt", transcript_size, lambda: progress("copy_text", 0, 0))
    by_media = {r["media"]: r for r in bundle.data["records"] if r.get("media")}
    for i, item in enumerate(bundle.data["media"], 1):
        path = bundle.media_path(item)
        progress("copy", i, len(bundle.data["media"]))
        _copy_bounded(
            path,
            root / "media" / path.name,
            item["size"],
            lambda i=i: progress("copy", i, len(bundle.data["media"])),
            file=item["name"],
            record=by_media.get(item["name"]),
        )
