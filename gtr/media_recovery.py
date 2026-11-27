"""Source roles, Telegram media kinds and explicit attachment recovery decisions."""

from hashlib import sha256
from pathlib import Path

from .bindings import requested_kind
from .files import digest
from .media_engine import FILE_BLOCKERS, inspect
from .media_queue import video_decision_key
from .model import Cancelled, RecoveryError
from .payloads import INPUT_MEDIA

VERSION = 1
STICKERS = {
    "image/webp": ("webp_sticker", ".webp"),
    "application/x-tgsticker": ("tgs_sticker", ".tgs"),
    "video/webm": ("webm_sticker", ".webm"),
}
SKIP_ACTIONS = {"local", "skip"}
SERVER_MEDIA_ERRORS = {"MEDIA_INVALID", "MEDIA_TYPE_CHANGED"}
DOCUMENT_ATTRIBUTES = (
    "DocumentAttributeFilename", "DocumentAttributeSticker", "DocumentAttributeVideo", "DocumentAttributeAudio",
    "DocumentAttributeAnimated", "DocumentAttributeCustomEmoji", "DocumentAttributeImageSize",
)


def source_kind(item, mime):
    if item["kind"] == "sticker" and mime in STICKERS:
        return STICKERS[mime][0]
    if item["kind"] == "photo" and mime == "image/webp":
        return "webp_image"
    return item["kind"]


def force_file(item):
    return item.get("force_file", item["kind"] in {"document", "contact", "location"})


def observed_media_kind(media):
    """Interpret the returned Telegram attributes independently of the upload constructor."""
    name = type(media).__name__
    if name == "MessageMediaPhoto":
        return "photo"
    if name != "MessageMediaDocument":
        return None
    attributes = {
        type(attribute).__name__: attribute
        for attribute in getattr(getattr(media, "document", None), "attributes", ())
    }
    if "DocumentAttributeSticker" in attributes:
        return "sticker"
    if "DocumentAttributeCustomEmoji" in attributes:
        return "custom_emoji"
    if "DocumentAttributeAnimated" in attributes:
        return "animation"
    if video := attributes.get("DocumentAttributeVideo"):
        return "round_video" if getattr(video, "round_message", False) else "video"
    if audio := attributes.get("DocumentAttributeAudio"):
        return "voice" if getattr(audio, "voice", False) else "audio"
    for flag, kind in (("round", "round_video"), ("voice", "voice"), ("video", "video")):
        if getattr(media, flag, False):
            return kind
    return "document"


def source_digest(item):
    if "members" in item:
        return sha256(",".join(member["sha256"] for member in item["members"]).encode()).hexdigest()
    return item["sha256"]


def represent(item, decision):
    action = decision["action"]
    if action in SKIP_ACTIONS or action == "blocked":
        return None
    item = dict(item)
    if action == "document":
        item.update(upload_as="document", input_media=INPUT_MEDIA["document"],
                    representation="archive_zip" if "members" in item else "document",
                    attributes={}, force_file=True)
    elif action == "sticker":
        mime = decision["mime"]
        item.update(mime=mime, detected_mime=mime, format_confirmed=True, upload_as="document",
                    input_media=INPUT_MEDIA["document"], representation="specialized_document",
                    attributes={"type": "sticker", "emoji": decision["emoji"]}, force_file=False)
        item["name"] = Path(item["name"]).with_suffix(STICKERS[mime][1]).name
    elif action == "image":
        alpha = decision["alpha"]
        upload_as = "document" if alpha else "photo"
        item.update(mime="image/png", detected_mime="image/png", format_confirmed=True,
                    upload_as=upload_as, input_media=INPUT_MEDIA[upload_as], representation=upload_as,
                    attributes={}, force_file=alpha,
                    transform={"type": "png_image", "original_sha256": decision["original_sha256"]})
        item.update({key: decision[key] for key in ("size", "sha256") if key in decision})
        item["name"] = Path(item["name"]).with_suffix(".png").name
    return item


def _webp_decodes(path, facts):
    if not (facts.width and facts.height and facts.width * facts.height <= 16_000_000 and not facts.animated):
        return False
    from PySide6.QtGui import QImageReader

    from .media_preview import read_image

    return not read_image(QImageReader(str(path), b"webp")).isNull()


def _native_sticker(path, facts, tools, progress, webp_decodes):
    if facts.mime == "video/webm":
        if tools is None:
            return False
        evidence = tools.analyze(path, "video", expected_size=facts.size, expected_hash=facts.sha256, progress=progress)
        return (
            evidence.codec == "vp9" and evidence.audio == () and evidence.streams == 1
            and evidence.decode == "sample_ok" and evidence.duration is not None and evidence.duration <= 3
            and evidence.width and evidence.height and max(evidence.width, evidence.height) == 512
            and evidence.fps is not None and evidence.fps <= 30
        )
    if facts.mime == "image/webp":
        return max(facts.width or 0, facts.height or 0) == 512 and webp_decodes
    if facts.mime == "application/x-tgsticker":
        return facts.width == facts.height == 512 and facts.duration is not None and 0 < facts.duration <= 3
    return False


def _image_copy(path, target, expected_hash, progress):
    from .media_preview import save_compatible_png

    checkpoint = lambda: progress("inspect_media", 0, 0)
    target.parent.mkdir(exist_ok=True)
    alpha = save_compatible_png(path, target, progress)
    if digest(path, checkpoint) != expected_hash:
        raise RecoveryError("Оригинал изображения изменился после анализа.")
    return {"alpha": alpha, "size": target.stat().st_size, "sha256": digest(target, checkpoint)}


def _options(item, *, intact, can_image, can_original=False, failed=False):
    options = []
    if can_original:
        options.append("original")
    if intact:
        if can_image and not item.get("transform"):
            options.append("image")
        if not failed or requested_kind(item) != "document" or not force_file(item):
            options.append("document")
    return (*options, "local", "skip")


def describe(entry, item, kind, reason, options, *, received=None, evidence=None, group_reason=None):
    prepared = requested_kind(item)
    return {
        **entry, "source_kind": kind, "prepared_kind": prepared, "received_kind": received,
        "reason": reason, "options": options, "original": evidence or {},
        "group": ":".join(str(value or "-") for value in (
            group_reason or reason, kind, prepared, received if not group_reason else None, item.get("mime"), *options,
        )),
        "recommended": next(action for action in ("original", "image", "compatible", "document", "local")
                            if action in options),
    }


def queue_attachment(item, path, meta, progress, work, queue, entry, choice, tools, *, audio=None):
    """Inspect source bytes once for this preparation and keep lossy choices explicit."""
    queue.start(entry["name"])
    facts = inspect(path, expected_size=item["size"], expected_hash=item["sha256"],
                    declared_mime=item.get("declared_mime"), checkpoint=lambda: progress("inspect_media", 0, 0))
    kind = source_kind(item, facts.mime)
    issue = next((code for code in facts.issues if code in FILE_BLOCKERS), None)
    damaged = "corrupt_container" in facts.issues
    webp_image = kind in {"webp_image", "webp_sticker"}
    can_image = not issue and not damaged and webp_image and _webp_decodes(path, facts)
    native = False
    if not issue and item["kind"] == "sticker" and not item.get("force_file"):
        native = not damaged and _native_sticker(path, facts, tools, progress, can_image)
        if not native:
            issue = "sticker_unconfirmed"
    if not issue and kind == "webp_image":
        issue = "webp_photo"
    if not issue and audio and audio["status"] != "audio_confirmed":
        issue = audio["status"]
    if not issue and item["kind"] in {"photo", "animation", "voice", "audio", "video", "round_video"}:
        if damaged or "mime_mismatch" in facts.issues:
            issue = "attachment_format"
        elif not facts.mime or requested_kind(item) == "document":
            issue = "attachment_unconfirmed"
    options = _options(item, intact=facts.intact, can_image=can_image, can_original=native)
    decision = {"key": entry["key"], "original_sha256": item["sha256"], "source_kind": kind}
    if choice in SKIP_ACTIONS or choice == "document" and "document" in options:
        decision["action"] = choice
    elif native and choice in {None, "original"}:
        decision.update(action="sticker", mime=facts.mime, emoji=str(meta.get("sticker_emoji") or "")[:80])
    elif choice == "image" and can_image:
        decision.update(action="image", alpha=bool(facts.alpha))
        if work is not None:
            target = work / Path(item["name"]).with_suffix(".png").name
            try:
                decision.update(_image_copy(path, target, item["sha256"], progress))
            except Cancelled:
                raise
            except (OSError, RecoveryError):
                target.unlink(missing_ok=True)
                issue = "image_copy_failed"
                options = tuple(option for option in options if option != "image")
                decision["action"] = "blocked"
    elif issue or choice and choice != "original":
        decision["action"] = "blocked"
    else:
        queue.finish()
        return item, None
    if decision["action"] == "blocked":
        queue.finish(describe(entry, item, kind, issue or "attachment_unconfirmed", options,
                              evidence={"container": facts.mime, "width": facts.width, "height": facts.height}))
    else:
        queue.finish()
    return represent(item, decision), decision


def recovery_entry(bundle, record, item):
    previous = record.get("media_recovery") or record.get("video_recovery") or {}
    original_digest = previous.get("original_sha256") or source_digest(item)
    key = previous.get("key") or video_decision_key(
        bundle.data["source_sha256"], bundle.data["chat_id"], record["source_ordinal"], original_digest
    )
    return {
        "key": key, "name": item["name"], "date": record["original_date"],
        "source": item.get("source", ""), "size": item["size"], "sha256": original_digest,
    }


def bundle_issues(bundle, inventory):
    """Find all locally actionable issues, including packages prepared by older versions."""
    records = {r["media"]: r for r in bundle.data["records"] if r.get("media")}
    issues = []
    for item, facts in zip(bundle.data["media"], inventory["items"]):
        record = records.get(item["name"])
        if not record or bundle.data["schema"] != 3:
            continue
        kind = source_kind(item, facts["mime"])
        reason = "attachment_format" if facts["blocked"] else None
        if (
            bundle.data.get("binding_version") and bundle.data.get("media_mode") == "text_media"
            and not force_file(item) and requested_kind(item) == "document"
        ):
            if item["kind"] == "sticker":
                reason = "sticker_legacy_document"
            elif kind == "webp_image":
                reason = "webp_photo"
        if not reason:
            continue
        intact = FILE_BLOCKERS.isdisjoint(facts["issues"])
        convertible = kind in {"webp_image", "webp_sticker"} and not facts["animated"] and facts["width"]
        options = _options(item, intact=intact, can_image=convertible,
                           can_original=reason == "sticker_legacy_document" and intact)
        issues.append(describe(recovery_entry(bundle, record, item), item, kind, reason, options,
                               evidence={"container": facts["mime"], "width": facts["width"], "height": facts["height"]}))
    return issues


def server_issue(bundle, attempt, tools=None):
    if not attempt or type(index := attempt.get("index")) is not int or not 1 <= index <= len(bundle.data["media"]):
        return None
    item = bundle.data["media"][index - 1]
    record = next((row for row in bundle.data["records"] if row.get("media") == item["name"]), None)
    if not record or bundle.data["schema"] != 3 or attempt.get("sha256") != item["sha256"]:
        return None
    kind = (record.get("media_recovery") or {}).get("source_kind") or source_kind(item, item["mime"])
    recognized_sticker = (attempt.get("received_kind") == "sticker" and item["kind"] == "sticker"
                          and requested_kind(item) == "document" and not force_file(item))
    options = _options(
        item, intact=True, can_image=bundle.data.get("media_mode") == "text_media"
        and kind in {"webp_image", "webp_sticker"}, can_original=recognized_sticker, failed=True,
    )
    video = record.get("video_recovery") or {}
    if (
        tools and tools.ffmpeg and tools.ffprobe and kind in {"video", "round_video"}
        and video.get("original", {}).get("status") in {"compatible", "transcode_required"}
        and not video["original"].get("alpha")
        and (video.get("action") != "convert" or video.get("method") == "remux")
    ):
        options = ("compatible", *options)
    return describe(
        recovery_entry(bundle, record, item), item, kind, attempt["error_code"], options,
        received=attempt.get("received_kind"), evidence={"container": item["mime"]},
        group_reason="sticker_legacy_document" if recognized_sticker else None,
    )
