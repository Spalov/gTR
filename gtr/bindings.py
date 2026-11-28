"""Versioned, filename-exact binding for one History Import marker per record."""

from hashlib import sha256
from pathlib import PurePosixPath

from .package_errors import PackageValidationError
from .profiles import ANDROID_MARKER, MARKER, PROFILES

VERSION = 1
UNRESOLVED = "UNRESOLVED_ATTACHMENT"
IOS = {"photo": "PHOTO", "voice": "AUDIO", "audio": "AUDIO", "video": "VIDEO",
       "round_video": "VIDEO", "sticker": "STICKER", "animation": "GIF"}
ANDROID = {"photo": "IMG", "voice": "PTT", "audio": "AUD", "video": "VID",
           "round_video": "VID", "sticker": "STK", "animation": "GIF"}


class AttachmentBindingError(PackageValidationError):
    def __init__(self, field="wire_name", **details):
        super().__init__("ATTACHMENT_BINDING_INVALID", "verify_binding", field, **details)


def requested_kind(item):
    if item["upload_as"] == "photo":
        return "photo"
    attributes = item.get("attributes", {})
    kind = attributes.get("type")
    if kind == "video":
        return "round_video" if attributes.get("round") else "video"
    if kind == "audio":
        return "voice" if attributes.get("voice") else "audio"
    return kind if kind in {"sticker", "animation"} else "document"


def wire_name(item):
    return item.get("wire_name", item["name"])


def alias(item, ordinal, date, profile):
    storage = item.get("storage_name", item["name"])
    extension = PurePosixPath(storage).suffix.lower()
    role = requested_kind(item)
    if role == "voice":
        extension = ".opus"
    if profile == PROFILES["android"]:
        return f"{ANDROID.get(role, 'DOC')}-{date:%Y%m%d}-WA{ordinal:04d}{extension}"
    return f"{ordinal:08d}-{IOS.get(role, 'DOCUMENT')}-{date:%Y-%m-%d-%H-%M-%S}{extension}"


def identity(source_hash, chat, record):
    raw = f"{source_hash}:{chat}:{record['source_ordinal']}"
    return sha256(raw.encode("utf-8")).hexdigest()


def bind(source_hash, chat, record, item, ordinal, date, profile):
    stable_id = identity(source_hash, chat, record)
    item.update(attachment_id=stable_id, wire_name=alias(item, ordinal, date, profile))
    record["attachment_id"] = stable_id


def check_binding(data, record, item, ordinal, date):
    expected = identity(data["source_sha256"], data["chat_id"], record)
    for field, value, actual in (
        ("media.attachment_id", expected, item.get("attachment_id")),
        ("records.attachment_id", expected, record.get("attachment_id")),
        ("wire_name", alias(item, ordinal, date, data["profile"]), item.get("wire_name")),
    ):
        if value != actual:
            raise AttachmentBindingError(field, file=item["name"], record=record, expected=value, actual=actual)


def digest_text(text):
    return sha256(text.encode("utf-8")).hexdigest()


def literal_marker(text):
    """Capture a marker signature and caption digest without retaining the marker or text."""
    line, sep, rest = text.partition("\n")
    line = line.removesuffix("\r").strip().lstrip("\u200e\u200f\ufeff")
    match = MARKER.fullmatch(line) or ANDROID_MARKER.fullmatch(line)
    if match and len(match[1]) <= 512:
        return digest_text(match[1].strip()), digest_text(rest if sep else "")
    return None, None
