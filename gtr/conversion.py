"""Build an inspectable WhatsApp-style UTF-8 transcript and a local loss ledger."""

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
from .core import ISSUES, catalogue, encode, properties
from .filenames import assign_names, safe_filename
from .files import (
    BLOCK,
    digest,
    local,
    new_directory,
    private_directory,
    read_json,
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
from .model import RecoveryError, quiet
from .payloads import STRUCTURED, plan_media, publish_media, validate_media_input
from .profiles import MARKER, MEDIA_NAME, PROFILES, read_records, wire_line
from .selection import decode_intervals, difference, included_intervals
from .structure import annotated_text, historical_notes, local_structure
from .video import VIDEO_KINDS, VIDEO_POLICIES, MediaTools, prepare_video

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
    annotate=False,
    record_kind="all",
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
    starts = tuple(first for first, _ in record_ranges or ())
    date_policy = "historical"
    profile = PROFILES[profile]
    if video_policy and media_mode == "text_media" and media_tools is None:
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
            video = audio = None
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
                grouped_video = bool(item and any(member["kind"] in VIDEO_KINDS for member in item.get("members", [])))
                if video_policy and media_mode == "text_media" and grouped_video:
                    if video_policy == "document":
                        notes.append("group_video_document")
                    elif video_policy == "skip":
                        notes = [key for key in notes if key != "grouped_media"]
                        item = plan_media(
                            [a for a in assets if a["kind"] not in VIDEO_KINDS], meta,
                            len(media) + 1, notes, missing_references=False,
                        )
                        notes.append("video_skipped")
                        video = {"action": "skip", "scope": "group"}
                    else:
                        video = {"action": "blocked", "scope": "group", "original": {"status": "multiple_attachments"}}
                        reason = "video_decision"
                        item = None
                if (
                    media_tools and video_policy and media_mode == "text_media"
                    and item and item["source"] and item["kind"] in VIDEO_KINDS
                ):
                    source = Path(project).joinpath("source", *safe_parts(item["source"]))
                    notes = [note for note in notes if note not in {"document_fallback", "media_attributes"}]
                    item, video = prepare_video(item, source, video_policy, media_tools, progress, video_work)
                    if video["action"] == "blocked":
                        reason = "video_decision"
                    elif key := {"document": "video_document", "skip": "video_skipped",
                                  "convert": "video_transcode"}.get(video["action"]):
                        notes.append(key)
                if (
                    media_tools and video_policy and media_mode == "text_media"
                    and item and item["source"] and item["kind"] in {"audio", "voice"}
                ):
                    source = Path(project).joinpath("source", *safe_parts(item["source"]))
                    audio = media_tools.audio(source, item, progress)
                    attributes = audio["attributes"]
                    if attributes:
                        notes = [key for key in notes if key not in {"document_fallback", "media_attributes"}]
                        item.update(attributes=attributes, representation="specialized_document")
                    elif audio["status"] == "audio_unreadable":
                        item = None
                        notes.append("audio_unreadable")
                    else:
                        notes.append("audio_unconfirmed")
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
            if media_mode == "text_only":
                record["omitted_media_count"] = len(assets)
            if reason:
                if item and item.get("transform") and video_work is not None:
                    (video_work / item["name"]).unlink()
                notes.append(reason)
            else:
                if item:
                    assign_names(item, meta, used_names)
                if not row["source_id"]:
                    notes.append("missing_source_id")
                if change:
                    notes.append(change)
                annotations = historical_notes(meta) if annotate else {}
                if annotations:
                    record["annotations"] = annotations
                wire_body = annotated_text(body, annotations)
                line = wire_line(profile, date, row["sender"], wire_body, item["name"] if item else None).encode(
                    "utf-8"
                )
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
                notes.extend(_text_losses(row, flags, meta))
            records.append(record)
            losses.update(notes)
    return {
        "schema": 3,
        "filename_version": 1,
        "source_conversation": source_props.get(f"chat_peer:{chat_id}", ""),
        "source_title_unique": title_unique,
        "profile": profile,
        "timezone": timezone_name,
        "source_sha256": source_hash,
        "source_kind": source_props["source_type"],
        "date_policy": date_policy,
        "media_mode": media_mode,
        **({"video_policy": video_policy} if video_policy else {}),
        **({"annotate": True} if annotate else {}),
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
    annotate=False,
    record_kind="all",
):
    from .reports import package_html

    project, dest = local(Path(project)), local(Path(dest))
    if fixture not in (None, "text", "media"):
        raise RecoveryError("Выберите вид синтетического пакета.")
    if test_reset_id is not None and (type(test_reset_id) is not int or test_reset_id < 0):
        raise RecoveryError("Недопустимый номер тестового цикла.")
    with catalogue(project) as db:
        original = Path(properties(db)["original"])
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
                annotate=annotate,
                record_kind=record_kind,
            )
        if data["losses"].get("video_decision"):
            raise RecoveryError(
                f"Для {data['losses']['video_decision']} видео выберите совместимую копию, документ или пропуск. "
                "Сначала откройте локальный план для просмотра причин."
            )
        if record_ranges is not None:
            actual = included_intervals(data["records"])
            reviewed = decode_intervals(record_ranges)
            if actual != reviewed:
                skipped = included_intervals([
                    {**r, "included": True} for r in data["records"]
                    if not r["included"] and "empty" in r["losses"]
                    and r.get("video_recovery", {}).get("action") == "skip"
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
        _require_space(work, estimated)
        for i, item in enumerate(data["media"], 1):
            path = work / "media" / item["storage_name"]
            if item.get("transform"):
                (work / ".video" / item["storage_name"]).replace(path)
            else:
                publish_media(
                    project,
                    item,
                    path,
                    lambda i=i: progress("media", i - 1, len(data["media"])),
                )
            try:
                validate_media_input(item, path, strict=True)
            except ValueError:
                raise RecoveryError(f"Локальная проверка вложения {item['name']} не пройдена.") from None
            progress("media", i, len(data["media"]))
        if (work / ".video").is_dir():
            (work / ".video").rmdir()
        if sum(item["size"] for item in data["media"]) > MAX_TOTAL_BYTES:
            raise RecoveryError("Объём вложений превышает локальный предел пакета.")
        with (work / "manifest.json").open("w", encoding="utf-8", newline="\n") as output:
            encode_manifest(data, output, progress)
        (work / "report.html").write_text(package_html(data), encoding="utf-8")
        progress("finish", 1, 1)
    return Bundle(dest, data)


def prepare_eligible(project, bundle, dest, eligible, progress=quiet):
    """Regenerate only the reviewed eligible records from the associated source project."""
    from .integrity import check_local

    selected = bundle.record_intervals()
    eligible = decode_intervals(eligible)
    if not eligible or selected is None or difference(eligible, selected) or bundle.data["schema"] != 3:
        raise RecoveryError("Выборка новых записей недоступна для этого пакета.")
    if project is None or check_local(bundle, project, progress)["status"] != "verified":
        raise RecoveryError("Откройте и проверьте исходный проект этого пакета.")
    profile = next(name for name, value in PROFILES.items() if value == bundle.data["profile"])
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
        video_policy=bundle.data.get("video_policy"),
        annotate=bundle.data.get("annotate", False),
        record_kind=bundle.data.get("selection", {}).get("record_kind", "all"),
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


def load_bundle(root, progress=quiet):
    root = local(Path(root))
    try:
        data = read_json(root / "manifest.json")
        bundle = Bundle(root, data)
        if (
            data["schema"] not in {1, 2, 3}
            or data.get("filename_version", 1) != 1
            or data["profile"] not in PROFILES.values()
            or local(bundle.transcript).stat().st_size > MAX_TOTAL_BYTES
            or digest(bundle.transcript, lambda: progress("verify_text", 0, 0)) != data["text_sha256"]
        ):
            raise ValueError
        if data["timezone"] != "source" or data["schema"] < 3:
            ZoneInfo(data["timezone"])
        if data["schema"] == 3 and data["date_policy"] not in DATE_POLICIES:
            raise ValueError
        if "test_reset_id" in data and (type(data["test_reset_id"]) is not int or data["test_reset_id"] < 0):
            raise ValueError
        modern = "media_mode" in data
        if "filename_version" in data:
            if type(data.get("source_title_unique", True)) is not bool or not re.fullmatch(
                r"(?:(?:user|chat|channel)-?[1-9]\d{0,18})?", data.get("source_conversation", "")
            ):
                raise ValueError
            previous = 0
            for record in data["records"]:
                pos = record["source_ordinal"]
                if (
                    type(pos) is not int
                    or not previous < pos <= 2**31 - 1
                    or not isinstance(record["source_id"], str)
                    or not isinstance(record["source_identity"], dict)
                    or not isinstance(record["original_media"], list)
                    or any(not isinstance(a, dict) for a in record["original_media"])
                    or not re.fullmatch(r"[a-f0-9]{64}", record["original_text_sha256"])
                ):
                    raise ValueError
                previous=pos
        mode = data.get("media_mode", "text_media")
        if data.get("video_policy") not in (None, *VIDEO_POLICIES):
            raise ValueError
        if (
            type(data["chat_id"]) is not int
            or not re.fullmatch(r"[a-f0-9]{64}", data["source_sha256"])
            or not isinstance(data["records"], list)
            or not isinstance(data["losses"], dict)
            or any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in data["losses"].items())
            or not isinstance(data["media"], list)
            or type(data["included"]) is not int
            or type(data["skipped"]) is not int
            or (
                modern
                and (
                    mode not in MEDIA_MODES
                    or data["source_records"] != len(data["records"])
                    or data["media_count"] != len(data["media"])
                    or data.get("fixture") not in (None, "text", "media")
                    or (
                        mode == "text_only"
                        and (
                            data["media"]
                            or any(
                                r["omitted_media_count"] != len(r["original_media"]) for r in data["records"]
                            )
                            or data["omitted_assets"]
                            != sum(r["omitted_media_count"] for r in data["records"])
                        )
                    )
                )
            )
        ):
            raise ValueError
        if "selection" in data:
            selection = data["selection"]
            first, last, sample = (selection[k] for k in ("first_record", "last_record", "sample"))
            selected = data["selected_records"]
            if (
                selection.get("record_kind", "all") not in RECORD_KINDS
                or type(first) is not int
                or not 1 <= first <= 2**31 - 1
                or (last is not None and (type(last) is not int or not first <= last <= 2**31 - 1))
                or (sample is not None and (type(sample) is not int or sample < max(1, data["included"])))
                or type(selected) is not int
                or not data["included"] <= selected <= len(data["records"])
                or any(
                    not first <= r["source_ordinal"] <= (last or 2**31 - 1)
                    for r in data["records"]
                    if r["included"]
                )
            ):
                raise ValueError
            if "ranges" in selection and decode_intervals(selection["ranges"]) != bundle.record_intervals():
                raise ValueError
        names, stored, planned = set(), set(), []
        total_bytes = 0
        for i, item in enumerate(data["media"], 1):
            progress("verify", i, len(data["media"]))
            name = item["name"]
            if (
                not isinstance(name, str)
                or not name
                or safe_filename(name) != name
                or name.casefold() in names
                or item.get("storage_name", name) in stored
                or (
                    not data.get("filename_version")
                    and any(k in item for k in ("storage_name", "display_name", "filename_source"))
                )
                or (
                    data.get("filename_version") == 1
                    and (
                        not item.get("display_name")
                        or safe_filename(item["display_name"]) != item["display_name"]
                        or item.get("filename_source")
                        not in {"export_metadata", "export_basename", "generated"}
                        or "storage_name" not in item
                    )
                )
                or not isinstance(item["kind"], str)
                or type(item["size"]) is not int
                or item["size"] <= 0
                or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
                or (
                    mode == "document"
                    and (
                        item["upload_as"] != "document"
                        or item.get("attributes")
                        or (item["source"] and not item.get("force_file"))
                    )
                )
            ):
                raise ValueError
            path = bundle.media_path(item)
            total_bytes += item["size"]
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError
            transform = item.get("transform")
            if transform and (
                not isinstance(transform, dict) or data.get("video_policy") != "compatible"
                or transform.get("type") != "mp4_h264_aac" or item["mime"] != "video/mp4"
                or not re.fullmatch(r"[a-f0-9]{64}", transform.get("original_sha256", ""))
                or item.get("attributes", {}).get("video_codec") != "h264"
            ):
                raise ValueError
            validate_media_input(item, path, strict=modern)
            if (
                path.stat().st_size != item["size"]
                or digest(path, lambda i=i: progress("verify", i, len(data["media"]))) != item["sha256"]
            ):
                raise ValueError
            names.add(name.casefold())
            stored.add(item.get("storage_name", name))
            planned.append(name)
        markers, headers = [], 0
        expected = iter(r for r in data["records"] if r["included"])
        previous_position, previous_date = 0, ""
        with local(bundle.transcript).open(encoding="utf-8", newline="\n") as text:
            for wire_date, author, body, name in read_records(text, data["profile"]):
                headers += 1
                if name:
                    markers.append(name)
                if mode == "text_only" and not body.strip():
                    raise ValueError
                row = next(expected, None)
                if row is None or (
                    data["schema"] >= 2
                    and (
                        row["author"] != author
                        or row.get("media", "") != name
                        or row["text_sha256"] != hashlib.sha256(body.encode("utf-8")).hexdigest()
                    )
                ):
                    raise ValueError
                if data["schema"] >= 2:
                    wire_fmt = "%d/%m/%Y, %H:%M:%S" if data["profile"] == PROFILE else "%d/%m/%y, %H:%M:%S"
                    date = datetime.fromisoformat(row["import_date"])
                    if (
                        date.strftime(wire_fmt) != wire_date
                        or (
                            (data["schema"] == 2 or data["date_policy"] == "ordered")
                            and row["import_date"] <= previous_date
                        )
                        or type(row["source_ordinal"]) is not int
                        or row["source_ordinal"] <= previous_position
                        or row["ordinal"] != headers
                    ):
                        raise ValueError
                    previous_date, previous_position = row["import_date"], row["source_ordinal"]
        if (
            headers != data["included"]
            or len(markers) != len(names)
            or {marker.casefold() for marker in markers} != names
            or (modern and markers != planned)
            or sum(bool(r["included"]) for r in data["records"]) != headers
            or len(data["records"]) - headers != data["skipped"]
            or not isinstance(data["losses"], dict)
            or not isinstance(data["timezone"], str)
        ):
            raise ValueError
        return bundle
    except (ValueError, KeyError, TypeError, OSError, UnicodeError, ZoneInfoNotFoundError, AttributeError):
        raise RecoveryError("Пакет неполон, изменён или имеет неизвестный формат.") from None


def _require_space(path, size):
    if shutil.disk_usage(path).free < size:
        raise RecoveryError("Недостаточно места на диске для копирования пакета.")


def _copy_bounded(source, destination, size, checkpoint):
    with local(source).open("rb") as reader, destination.open("xb") as writer:
        remaining = size
        while remaining:
            checkpoint()
            chunk = reader.read(min(BLOCK, remaining))
            if not chunk:
                raise RecoveryError("Пакет изменился во время копирования.")
            writer.write(chunk)
            remaining -= len(chunk)
        if reader.read(1):
            raise RecoveryError("Пакет изменился во время копирования.")


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
    transcript_size = local(bundle.transcript).stat().st_size
    required = len(manifest) + transcript_size + sum(item["size"] for item in bundle.data["media"])
    _require_space(root, required)
    (root / "media").mkdir()
    (root / "manifest.json").write_bytes(manifest)
    _copy_bounded(bundle.transcript, root / "_chat.txt", transcript_size, lambda: progress("copy_text", 0, 0))
    for i, item in enumerate(bundle.data["media"], 1):
        path = bundle.media_path(item)
        progress("copy", i, len(bundle.data["media"]))
        _copy_bounded(
            path,
            root / "media" / path.name,
            item["size"],
            lambda i=i: progress("copy", i, len(bundle.data["media"])),
        )
