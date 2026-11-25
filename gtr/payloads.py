"""Plan one loss-aware attachment per source row and materialize verified bytes."""

import hashlib
import math
import re
from pathlib import PurePosixPath
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from .core import encode
from .files import BLOCK, local, safe_parts
from .media import identify
from .model import RecoveryError

STRUCTURED = (
    "contact_information",
    "location_information",
    "address",
    "place_name",
    "live_location_period_seconds",
    "poll",
    "game_title",
    "other_media",
)
INPUT_MEDIA = {
    "photo": "InputMediaUploadedPhoto",
    "document": "InputMediaUploadedDocument",
}
PHOTO_TYPES = {"image/jpeg", "image/png"}
MIME_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+\-]*/[a-z0-9][a-z0-9!#$&^_.+\-]*", re.IGNORECASE)


def media_attributes(kind, mime, meta):
    """Use only bounded, explicitly exported dimensions/duration."""

    def number(key, limit):
        value = meta.get(key)
        return value if type(value) in (int, float) and math.isfinite(value) and 0 < value <= limit else None

    duration = number("duration_seconds", 7 * 86400)
    width, height = number("width", 16384), number("height", 16384)
    if (
        kind in {"video", "round_video"}
        and mime in {"video/mp4", "video/quicktime", "video/webm"}
        and duration
        and width
        and width >= 1
        and height
        and height >= 1
    ):
        return {
            "type": "video",
            "duration": duration,
            "width": int(width),
            "height": int(height),
            "round": kind == "round_video",
        }
    if kind in {"audio", "voice"} and mime.startswith("audio/") and duration:
        return {
            "type": "audio",
            "duration": math.ceil(duration),
            "voice": kind == "voice" and mime in {"audio/ogg", "audio/opus"},
        }
    if kind == "animation" and mime in {"video/mp4", "image/gif"}:
        return {"type": "animation"}
    if (
        kind == "sticker"
        and meta.get("sticker_emoji")
        and mime in {"image/webp", "video/webm", "application/x-tgsticker"}
    ):
        return {"type": "sticker", "emoji": str(meta["sticker_emoji"])[:80]}
    return {}


def _extension(path):
    suffix = PurePosixPath(path).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"


def _candidate(item, representation):
    return {
        **item,
        "input_media": INPUT_MEDIA[item["upload_as"]],
        "representation": representation,
        "compatibility": "unverified",
    }


def plan_media(assets, meta, ordinal, notes, *, documents_only=False, missing_references=True):
    files, missing = [], []
    for asset in assets:
        if asset["status"] == "metadata":
            continue
        if asset["status"] in {"ok", "unconfirmed_type"}:
            files.append(
                {
                    "source": asset["path"],
                    "size": asset["size"],
                    "sha256": asset["sha256"],
                    "mime": asset["mime"],
                    "kind": asset["kind"],
                    "format_confirmed": asset["status"] == "ok",
                    "detected_mime": asset["mime"] if asset["status"] == "ok" else "",
                    "declared_mime": asset["expected_mime"] or "",
                }
            )
            if asset["status"] == "unconfirmed_type":
                notes.append("file_type_unconfirmed")
        else:
            missing.append({"type": asset["kind"], "reference": asset["original"], "status": asset["status"]})
    if missing:
        notes.append("media_unavailable")
    structured = {key: meta[key] for key in STRUCTURED if meta.get(key) is not None and key in meta}
    # An existing vCard is already a faithful standalone representation of the contact.
    if any(f["kind"] == "contact" for f in files):
        structured.pop("contact_information", None)
    extra = {}
    if structured:
        extra["exported_metadata"] = structured
        notes.append("structured_document")
    if missing and missing_references:
        extra["unavailable_attachments"] = missing
        notes.append("reference_document")
    stem = f"gtr-{ordinal:07d}"
    if not files and not extra:
        return None
    if not files:
        content = encode(extra) + "\n"
        kind = (
            "location"
            if "location_information" in structured
            else "contact"
            if "contact_information" in structured
            else "document"
        )
        payload = content.encode("utf-8")
        return _candidate(
            {
                "name": stem + ".json",
                "kind": kind,
                "upload_as": "document",
                "mime": "application/json",
                "source": "",
                "generated": content,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            "generated_document",
        )
    if len(files) > 1 or extra:
        notes.append("grouped_media")
        return _candidate(
            {
                "name": stem + ".zip",
                "source": "",
                "kind": "document",
                "upload_as": "document",
                "mime": "application/zip",
                "members": files,
                "details": encode(extra) + "\n" if extra else "",
                "size": sum(f["size"] for f in files),
                "sha256": None,
                "detected_mime": "application/zip",
            },
            "archive_zip",
        )
    file = files[0]
    photo = (
        not documents_only
        and file["kind"] == "photo"
        and file["format_confirmed"]
        and file["mime"] in PHOTO_TYPES
    )
    attributes = (
        media_attributes(file["kind"], file["mime"], meta)
        if file["format_confirmed"] and not documents_only
        else {}
    )
    if file["kind"] != "document" and not photo and not attributes:
        notes.append("document_fallback")
    elif attributes:
        notes.append("media_attributes")
    return _candidate(
        {
            **file,
            "name": stem + _extension(file["source"]),
            "upload_as": "photo" if photo else "document",
            "attributes": attributes,
            **({"force_file": True} if documents_only else {}),
        },
        "photo" if photo else "specialized_document" if attributes else "document",
    )


def _integer(value, limit):
    return type(value) is int and 0 < value <= limit


def _attribute_shape(details):
    match details.get("type"):
        case "video":
            duration = details.get("duration")
            return (
                type(duration) in (int, float)
                and 0 < duration <= 7 * 86400
                and _integer(details.get("width"), 16384)
                and _integer(details.get("height"), 16384)
                and type(details.get("round")) is bool
                and all(type(details[k]) is bool for k in ("nosound", "supports_streaming") if k in details)
                and (
                    "video_codec" not in details
                    or isinstance(details["video_codec"], str) and re.fullmatch(r"[a-z0-9_]{1,32}", details["video_codec"])
                )
            )
        case "audio":
            return (
                _integer(details.get("duration"), 7 * 86400) and type(details.get("voice")) is bool
                and all(isinstance(details.get(key, ""), str) and len(details.get(key, "")) <= 128
                        for key in ("title", "performer"))
            )
        case "animation":
            return True
        case "sticker":
            return isinstance(emoji := details.get("emoji"), str) and 0 < len(emoji) <= 80
        case _:
            return not details


def validate_media_input(item, path, *, strict=False):
    """Check the local upload builder shape. Passing does not predict a server decision."""
    mime = item.get("mime")
    upload_as = item.get("upload_as")
    attrs = item.get("attributes", {})
    if (
        not isinstance(mime, str)
        or len(mime) > 127
        or not MIME_TYPE.fullmatch(mime)
        or upload_as not in INPUT_MEDIA
    ):
        raise ValueError
    if not isinstance(attrs, dict) or not _attribute_shape(attrs):
        raise ValueError
    if strict and (
        item.get("input_media") != INPUT_MEDIA[upload_as] or item.get("compatibility") != "unverified"
    ):
        raise ValueError
    if strict:
        representation = (
            "photo"
            if upload_as == "photo"
            else "specialized_document"
            if attrs
            else "archive_zip"
            if "members" in item
            else "document"
            if item.get("source")
            else "generated_document"
        )
        if item.get("representation") != representation:
            raise ValueError
    if "force_file" in item and type(item["force_file"]) is not bool:
        raise ValueError
    claimed = item.get("detected_mime") if strict else ""
    if claimed or attrs or upload_as == "photo":
        with path.open("rb") as stream:
            actual, recognized = identify(path.name, stream.read(4096))
        if not recognized or actual != mime or (claimed and claimed != actual):
            raise ValueError
    if upload_as == "photo" and (mime not in PHOTO_TYPES or attrs):
        raise ValueError
    kind = item.get("kind")
    match attrs.get("type"):
        case "video" if kind not in {"video", "round_video"} or mime not in {
            "video/mp4",
            "video/quicktime",
            "video/webm",
        }:
            raise ValueError
        case "audio" if kind not in {"audio", "voice"} or not mime.startswith("audio/"):
            raise ValueError
        case "animation" if kind != "animation" or mime not in {"video/mp4", "image/gif"}:
            raise ValueError
        case "sticker" if kind != "sticker" or mime not in {
            "image/webp",
            "video/webm",
            "application/x-tgsticker",
        }:
            raise ValueError


def copy_original(project, item, sink, progress):
    path = local(project.joinpath("source", *safe_parts(item["source"])))
    sha, size = hashlib.sha256(), 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(BLOCK):
                sha.update(chunk)
                size += len(chunk)
                sink.write(chunk)
                progress()
    except OSError:
        raise RecoveryError("Не удалось прочитать оригинал или записать вложение пакета.") from None
    if size != item["size"] or sha.hexdigest() != item["sha256"]:
        raise RecoveryError("Вложение изменилось после анализа. Пакет не сохранён.")


def publish_media(project, item, path, progress):
    if "generated" in item:
        path.write_bytes(item["generated"].encode("utf-8"))
        del item["generated"]
    elif "members" in item:
        with ZipFile(path, "x", compression=ZIP_STORED) as archive:
            for i, member in enumerate(item["members"], 1):
                member.setdefault("stored_as", f"attachment-{i:03d}" + _extension(member["source"]))
                with archive.open(ZipInfo(member["stored_as"]), "w", force_zip64=True) as sink:
                    copy_original(project, member, sink, progress)
            if item["details"]:
                archive.writestr(ZipInfo("exported-details.json"), item["details"].encode("utf-8"))
        del item["details"]
        with path.open("rb") as stream:
            item["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        item["size"] = path.stat().st_size
    else:
        with path.open("xb") as sink:
            copy_original(project, item, sink, progress)
