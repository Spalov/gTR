"""Level A independently compares the package with the canonical project."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .core import catalogue, properties, verify
from .filenames import assign_names
from .history import HistoricalIdentity, ordered_rows
from .media_recovery import force_file, represent, source_digest
from .model import quiet
from .payloads import plan_media
from .structure import annotated_text, historical_notes
from .video import VIDEO_KINDS, video_representation


def _count(db, sql, values):
    return db.execute(sql, values).fetchone()[0]


def _payload_matches(bundle, actual, expected):
    if not actual or not expected:
        return actual is expected
    if any(actual.get(key) != expected[key] for key in ("name", "kind", "mime", "upload_as", "source")):
        return False
    if "members" not in expected:
        return (
            actual["size"] == expected["size"]
            and actual["sha256"] == expected["sha256"]
            and actual.get("attributes", {}) == expected.get("attributes", {})
            and actual.get("transform") == expected.get("transform")
            and (actual["upload_as"] == "photo" or force_file(actual) == force_file(expected))
        )
    listed = actual.get("members", [])
    if len(listed) != len(expected["members"]):
        return False
    try:
        with ZipFile(bundle.media_path(actual)) as archive:
            details = expected["details"].encode()
            names = [item["stored_as"] for item in listed] + (["exported-details.json"] if details else [])
            if archive.namelist() != names:
                return False
            for entry, source in zip(listed, expected["members"]):
                if "stored_as" in source and entry.get("stored_as") != source["stored_as"]:
                    return False
                if any(entry.get(key) != source[key] for key in ("source", "size", "sha256")):
                    return False
                info = archive.getinfo(entry["stored_as"])
                if info.file_size != source["size"]:
                    return False
                with archive.open(info) as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != source["sha256"]:
                        return False
            if details:
                return (
                    archive.getinfo("exported-details.json").file_size == len(details)
                    and archive.read("exported-details.json") == details
                )
    except (BadZipFile, EOFError, OSError, KeyError, RuntimeError, TypeError):
        return False
    return True


def check_local(bundle, project: Path | None, progress=quiet) -> dict:
    """The caller has checked the transcript and packaged bytes through load_bundle."""
    result = {
        "status": "unknown",
        "package_hashes": "verified",
        "messages": bundle.data["included"],
        "files": len(bundle.data["media"]),
        "differences": [],
    }
    if project is None or bundle.data["schema"] < 3:
        return result
    differences = result["differences"]
    chat = bundle.data["chat_id"]
    mode = bundle.data.get("media_mode", "text_media")
    packaged = {item["name"]: item for item in bundle.data["media"]}
    with catalogue(project) as db:
        props = properties(db)
        if props["sha256"] != bundle.data["source_sha256"] or not _count(
            db, "SELECT count(*) FROM chats WHERE id=?", (chat,)
        ):
            differences.append({"code": "project_mismatch"})
        else:
            listed = bundle.data["records"]
            total = _count(db, "SELECT count(*) FROM messages WHERE chat=?", (chat,))
            if total != len(listed):
                differences.append({"code": "source_count", "expected": total, "actual": len(listed)})
            asset_ordinal = 0
            used_names = {}
            title = db.execute("SELECT title FROM chats WHERE id=?", (chat,)).fetchone()[0]
            if "source_title_unique" in bundle.data and (
                bundle.data["chat_title"] != title
                or bundle.data["source_title_unique"]
                != (_count(db, "SELECT count(*) FROM chats WHERE title=?", (title,)) == 1)
            ):
                differences.append({"code": "project_mismatch"})
            if "source_conversation" in bundle.data and bundle.data["source_conversation"] != props.get(
                f"chat_peer:{chat}", ""
            ):
                differences.append({"code": "project_mismatch"})
            for i, (original, record) in enumerate(zip(ordered_rows(db, chat), listed), 1):
                if i % 100 == 1:
                    progress("local_history", i, total)
                flags = {r[0] for r in db.execute("SELECT code FROM issues WHERE msg=?", (original["id"],))}
                identity = HistoricalIdentity.read(original, flags)
                evidence = record["source_identity"]
                meta = json.loads(original["meta"])
                if identity.original_message_id != record["source_id"] or record[
                    "source_ordinal"
                ] != meta.get("source_ordinal", original["position"]):
                    differences.append({"code": "source_order", "ordinal": i})
                if (
                    identity.original_author_id != evidence["original_author_id"]
                    or identity.original_author_name != evidence["original_author_name"]
                    or identity.original_author_name != record["author"]
                    or identity.original_author_id != record["source_author_id"]
                ):
                    differences.append({"code": "source_author", "ordinal": i})
                if any(evidence.get(key) != value for key, value in asdict(identity).items()) or (
                    record["original_date"] != identity.original_timestamp
                ):
                    differences.append({"code": "source_date", "ordinal": i})
                sha = hashlib.sha256(original["text"].encode()).hexdigest()
                notes = historical_notes(
                    meta, bundle.data.get("annotation_mode", "detailed"),
                    bundle.data.get("annotation_language", "ru"),
                ) if bundle.data.get("annotate") and record["included"] else {}
                wire_sha = hashlib.sha256(annotated_text(original["text"], notes).encode()).hexdigest()
                if sha != record["original_text_sha256"] or notes != record.get("annotations", {}) or (
                    record["included"] and wire_sha != record["text_sha256"]
                ):
                    differences.append({"code": "source_text", "ordinal": i})
                resolved = original["resolved_reply"] if original["resolved_reply_chat"] == chat else None
                if (
                    record["reply_source_id"] != identity.original_reply_to
                    or record["resolved_reply"] != resolved
                ):
                    differences.append({"code": "source_reply", "ordinal": i})
                files = [
                    dict(r)
                    for r in db.execute(
                        """SELECT a.kind,a.original,a.path,a.status,f.size,f.sha256,f.mime,a.expected_mime
                    FROM assets a LEFT JOIN files f ON f.path=a.path WHERE a.msg=? ORDER BY a.id""",
                        (original["id"],),
                    )
                ]
                if [
                    {key: value for key, value in item.items() if key not in {"mime", "expected_mime"}}
                    for item in files
                ] != record["original_media"]:
                    differences.append({"code": "source_media", "ordinal": i})
                if record["included"]:
                    payload = None
                    if mode != "text_only":
                        policy = bundle.data.get("video_policy")
                        decision = record.get("video_recovery")
                        recovery = record.get("media_recovery")
                        group_skip = decision and decision.get("scope") == "group" and decision.get("action") == "skip"
                        payload = plan_media(
                            [item for item in files if item["kind"] not in VIDEO_KINDS] if group_skip else files,
                            meta, asset_ordinal + 1, [], documents_only=mode == "document",
                            missing_references=policy is None,
                        )
                        if decision and decision.get("scope") == "group" and decision.get("action") == "local":
                            payload = None
                        if policy and mode == "text_media" and payload and payload["kind"] in {"audio", "voice"}:
                            audio = record.get("audio_recovery", {})
                            if audio.get("attributes"):
                                payload.update(attributes=audio["attributes"], representation="specialized_document")
                            elif audio.get("status") == "audio_unreadable" and not recovery:
                                payload = None
                            elif audio.get("status") == "audio_unconfirmed" and bundle.data.get("binding_version"):
                                payload.update(attributes={}, representation="document", force_file=True)
                        if payload and payload["kind"] in VIDEO_KINDS and policy and mode == "text_media":
                            if (
                                not isinstance(decision, dict)
                                or decision.get("original_sha256") != payload["sha256"]
                                or decision.get("action") not in {"keep", "convert", "document", "skip", "local"}
                            ):
                                differences.append({"code": "video_provenance", "ordinal": i})
                            else:
                                payload = video_representation(payload, decision)
                        if recovery:
                            if (
                                not bundle.data.get("media_workflow") or not payload
                                or recovery.get("original_sha256") != source_digest(payload)
                                or recovery.get("action") not in {"sticker", "image", "document", "local", "skip"}
                                or recovery.get("action") == "sticker" and (
                                    payload["kind"] != "sticker" or recovery.get("mime") != payload["mime"]
                                    or recovery.get("emoji") != str(meta.get("sticker_emoji") or "")[:80]
                                )
                                or recovery.get("action") == "image" and payload["mime"] != "image/webp"
                            ):
                                differences.append({"code": "package_media", "ordinal": i})
                            else:
                                payload = represent(payload, recovery)
                        if payload and bundle.data.get("filename_version"):
                            assign_names(payload, meta, used_names)
                    elif not original["text"].strip():
                        differences.append({"code": "text_only_empty", "ordinal": i})
                    actual = packaged.get(record.get("media"))
                    if (
                        payload
                        and actual
                        and bundle.data.get("filename_version")
                        and any(
                            actual.get(key) != payload[key]
                            for key in ("storage_name", "display_name", "filename_source")
                        )
                    ):
                        differences.append({"code": "package_media", "ordinal": i})
                    if not _payload_matches(bundle, actual, payload):
                        differences.append({"code": "package_media", "ordinal": i})
                    asset_ordinal += payload is not None
            issues = dict(db.execute("SELECT code,count(*) FROM issues WHERE chat=? GROUP BY code", (chat,)))
            result["source_messages"] = _count(
                db, "SELECT count(*) FROM messages WHERE chat=? AND service=0", (chat,)
            )
            result["replies"] = _count(
                db, "SELECT count(*) FROM messages WHERE chat=? AND reply<>''", (chat,)
            )
            result["unresolved_replies"] = issues.get("unresolved_reply", 0) + issues.get("unknown_reply", 0)
            bad = _count(
                db,
                """SELECT count(*) FROM messages m LEFT JOIN messages t ON t.id=m.target
                WHERE m.chat=? AND m.target IS NOT NULL AND
                (t.id IS NULL OR t.source_id<>m.reply OR (m.reply_peer='' AND t.chat<>m.chat))""",
                (chat,),
            )
            if bad:
                differences.append({"code": "reply_graph", "count": bad})
    if not differences or differences[0]["code"] != "project_mismatch":
        results = verify(project, progress, history_only=mode == "text_only")
        result["project_files"] = results
        result["file_scope"] = "history" if mode == "text_only" else "archive"
        if any(key != "ok" for key in results):
            differences.append({"code": "project_file_hashes", "counts": results})
    result["status"] = "differences" if differences else "verified"
    return result
