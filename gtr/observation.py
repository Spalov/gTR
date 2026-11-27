"""Read back a chosen interval. Alpha captures keep metadata and text hashes local."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from .bindings import digest_text, literal_marker, wire_name
from .faults import ImportFault
from .files import local, publish_text, read_json
from .media_recovery import observed_media_kind
from .model import RecoveryError, quiet

MAX_SCAN = 20_000
MAX_MEDIA_HASH = 128 * 1024**2
DOWNLOAD_CHUNK = 128 * 1024


def _new_json(path, protected):
    path = local(Path(path))
    if (
        path.exists()
        or path.suffix.lower() != ".json"
        or not path.parent.is_dir()
        or any(path.is_relative_to(local(Path(root))) for root in protected)
    ):
        raise RecoveryError("Выберите новый JSON вне пакета, проекта и источника в существующей папке.")
    return path


def _valid_id_window(after, through):
    return (
        type(after) is int
        and 0 <= after < 2**31 - 1
        and (through is None or type(through) is int and after < through < 2**31 - 1)
    )


@dataclass(frozen=True)
class CaptureWindow:
    path: Path
    after_id: int
    through_id: int | None
    probe_media: bool = False
    protected: tuple[Path, ...] = ()
    historical_from: str | None = None
    historical_through: str | None = None
    max_scan: int | None = None
    baseline: Path | None = None
    media_hash_limit: int = 0

    def date_bounds(self):
        if self.historical_from is None and self.historical_through is None:
            return None
        try:
            first, last = (
                date.fromisoformat(self.historical_from),
                date.fromisoformat(self.historical_through),
            )
            if first <= last:
                return first, last
        except (ValueError, TypeError):
            pass
        raise RecoveryError("Введите обе границы исторической даты в формате ГГГГ-ММ-ДД по UTC.")

    def validate(self, bundle):
        path = _new_json(self.path, (bundle.root, *self.protected))
        if not _valid_id_window(self.after_id, self.through_id):
            raise RecoveryError("Проверьте границы серверных ID.")
        if self.max_scan is not None and (
            type(self.max_scan) is not int or not 1 <= self.max_scan <= MAX_SCAN
        ):
            raise RecoveryError(f"Укажите предел чтения от 1 до {MAX_SCAN} записей.")
        if type(self.media_hash_limit) is not int or not 0 <= self.media_hash_limit <= MAX_MEDIA_HASH:
            raise RecoveryError("Предел полного чтения медиа составляет 128 МиБ на снимок.")
        if self.date_bounds() and self.baseline:
            raise RecoveryError("Для поиска по исторической дате выберите диапазон без исходной границы.")
        return path


@dataclass(frozen=True)
class ObservedMessage:
    id: int
    from_id: int | None
    effective_sender_id: int | None
    out: bool | None
    imported: bool
    imported_name: str | None
    imported_from_id: int | None
    text_sha256: str
    has_media: bool
    media_constructor: str | None
    media_accessible: bool | None
    media_error: str | None
    reply_to_id: int | None
    original_date: str | None
    server_date: str | None
    visual_out: bool | None = None
    visual_name: str | None = None
    visual_date: str | None = None
    visual_precision: str | None = None
    visual_rank: int | None = None
    visual_visible: bool | None = None
    visual_jump: bool | None = None
    visual_media: bool | None = None
    media_sha256: str | None = None
    media_size: int | None = None
    media_probe_scope: str = "none"
    document_name: str | None = None
    document_mime: str | None = None
    media_kind: str | None = None
    attachment_marker_sha256: str | None = None
    caption_without_marker_sha256: str | None = None
    forward_header_kind: str = "absent"


async def last_id(manager, target, *, bundle=None, path=None, protected=()):
    if path is not None:
        if bundle is None or target.kind not in {"pm", "group"}:
            raise RecoveryError("Выберите пакет и диалог для сохранения исходной границы.")
        path = _new_json(path, (bundle.root, *protected))
    rows = await manager.rpc(manager.client.get_messages(target.input_peer, limit=1))
    snapshot = {
        "state": "baseline",
        "account_id": manager.account.id,
        "peer_id": target.id,
        "last_id": rows[0].id if rows else 0,
    }
    if path is not None:
        snapshot.update(
            schema=1,
            before_import=True,
            captured_at=datetime.now(UTC).isoformat(),
            package=bundle.fingerprint(),
            manifest_sha256=bundle.manifest_hash(),
        )
        publish_text(path, json.dumps(snapshot, indent=2, ensure_ascii=False))
        snapshot["path"] = str(path)
    return snapshot


def _baseline(manager, target, bundle, window):
    if window.baseline is None:
        return window.after_id, None
    saved = read_json(window.baseline)
    try:
        timestamp = datetime.fromisoformat(saved["captured_at"])
        if (
            saved["state"] == "baseline"
            and saved["schema"] == 1
            and saved["before_import"] is True
            and saved["account_id"] == manager.account.id
            and saved["peer_id"] == target.id
            and saved["package"] == bundle.fingerprint()
            and saved["manifest_sha256"] == bundle.manifest_hash()
            and timestamp.tzinfo is not None
            and timestamp <= datetime.now(UTC)
            and _valid_id_window(saved["last_id"], window.through_id)
        ):
            return saved["last_id"], timestamp.astimezone(UTC).isoformat()
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    raise RecoveryError("Исходная граница не соответствует выбранному аккаунту, чату или пакету.")


async def _probe(manager, media):
    async def available():
        found = False
        async for block in manager.client.iter_download(media, request_size=4096, limit=1):
            found = bool(block)
        return found

    try:
        return await manager.rpc(available()), None
    except ImportFault as fault:
        if fault.seconds or fault.code in {"TIMEOUT", "CONNECTION_LOST", "SERVER_FAILURE"}:
            raise
        return False, fault.code
    except (ValueError, TypeError):
        return None, "MEDIA_PROBE_UNSUPPORTED"


async def _full_hash(manager, media, size):
    async def read():
        hashed, count = hashlib.sha256(), 0
        async for block in manager.client.iter_download(
            media,
            request_size=DOWNLOAD_CHUNK,
            file_size=size,
            limit=(size + DOWNLOAD_CHUNK - 1) // DOWNLOAD_CHUNK,
        ):
            count += len(block)
            hashed.update(block)
        return (True, None, hashed.hexdigest()) if count == size else (None, "MEDIA_HASH_SIZE_MISMATCH", None)

    try:
        return await manager.rpc(read())
    except ImportFault as fault:
        if fault.seconds or fault.code in {"TIMEOUT", "CONNECTION_LOST", "SERVER_FAILURE"}:
            raise
        return False, fault.code, None
    except (ValueError, TypeError):
        return None, "MEDIA_PROBE_UNSUPPORTED", None


async def capture(manager, target, bundle, window: CaptureWindow, progress=quiet):
    from telethon import types, utils

    if target.kind not in {"pm", "group"}:
        raise RecoveryError("Выберите поддерживаемый диалог.")
    path = window.validate(bundle)
    dates = window.date_bounds()
    after, baseline_at = _baseline(manager, target, bundle, window)
    through = window.through_id
    cap = window.max_scan or min(bundle.data["included"] + 200, MAX_SCAN)
    signatures = {row["text_sha256"] for row in bundle.data["records"] if row["included"]} if dates else set()
    aliases = {digest_text(wire_name(item)) for item in bundle.data["media"]} if dates else set()

    async def collect():
        observations = []
        scanned, truncated, remaining = 0, False, window.media_hash_limit
        async for msg in manager.client.iter_messages(
            target.input_peer,
            min_id=after,
            max_id=through + 1 if through else 0,
            limit=cap + 1,
        ):
            if scanned == cap:
                truncated = True
                break
            scanned += 1
            progress("observe", scanned, cap)
            header = getattr(msg, "fwd_from", None)
            source = getattr(msg, "from_id", None)
            media = getattr(msg, "media", None)
            original_date = getattr(header, "date", None)
            imported = bool(getattr(header, "imported", False))
            message = getattr(msg, "message", None) or ""
            text_hash = digest_text(message)
            marker, caption = literal_marker(message) if imported else (None, None)
            if dates and (
                not imported
                or not original_date
                or not dates[0] <= original_date.astimezone(UTC).date() <= dates[1]
                or (text_hash not in signatures and marker not in aliases)
            ):
                continue
            has_media = bool(
                media and not isinstance(media, (types.MessageMediaEmpty, types.MessageMediaWebPage))
            )
            accessible, error, sha, size, scope = None, None, None, None, "none"
            if has_media and window.media_hash_limit:
                size = getattr(getattr(msg, "file", None), "size", None)
                if type(size) is not int or size <= 0:
                    size, error = None, "MEDIA_HASH_SIZE_UNKNOWN"
                elif size > remaining:
                    error = "MEDIA_HASH_BUDGET"
                else:
                    remaining -= size
                    scope = "full"
                    accessible, error, sha = await _full_hash(manager, media, size)
            if has_media and window.probe_media and scope != "full":
                scope = "header"
                accessible, probe_error = await _probe(manager, media)
                error = probe_error or error
            from .filenames import safe_filename

            document = getattr(media, "document", None)
            filename = next(
                (
                    safe_filename(a.file_name)
                    for a in getattr(document, "attributes", ())
                    if type(a).__name__ == "DocumentAttributeFilename"
                ),
                None,
            )
            item = ObservedMessage(
                msg.id,
                utils.get_peer_id(source) if source else None,
                getattr(msg, "sender_id", None),
                getattr(msg, "out", None),
                imported,
                getattr(header, "from_name", None),
                utils.get_peer_id(header.from_id) if getattr(header, "from_id", None) else None,
                text_hash,
                has_media,
                type(media).__name__ if media else None,
                accessible,
                error,
                getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None),
                original_date.isoformat() if original_date else None,
                msg.date.isoformat() if msg.date else None,
                media_sha256=sha,
                media_size=size,
                media_probe_scope=scope,
                document_name=filename,
                document_mime=getattr(document, "mime_type", None),
                media_kind=observed_media_kind(media),
                attachment_marker_sha256=marker,
                caption_without_marker_sha256=caption,
                forward_header_kind="imported" if imported else "native_forward" if header else "absent",
            )
            observations.append(asdict(item))
        observations.reverse()
        return observations, scanned, truncated

    rows, scanned, truncated = await manager.rpc(
        collect(),
        seconds=3600 if window.probe_media or window.media_hash_limit else 300,
    )
    scope = {
        "kind": "historical_date" if dates else "id_window",
        "after_id": after,
        "through_id": through,
        "historical_from": dates[0].isoformat() if dates else None,
        "historical_through": dates[1].isoformat() if dates else None,
        "scan_limit": cap,
        "scanned": scanned,
        "returned": len(rows),
        "truncated": truncated,
        "baseline_captured_at": baseline_at,
        "media_hash_limit": window.media_hash_limit,
    }
    data = {
        "schema": 3,
        "mode": "telegram_observation",
        "account_id": manager.account.id,
        "peer_id": target.id,
        "peer_kind": target.kind,
        "package": bundle.fingerprint(),
        "manifest_sha256": bundle.manifest_hash(),
        "after_id": after,
        "through_id": through,
        "captured_at": datetime.now(UTC).isoformat(),
        "scope": scope,
        "after_restart": None,
        "client": "",
        "messages": rows,
    }
    publish_text(path, json.dumps(data, indent=2, ensure_ascii=False))
    return {"state": "observation_saved", "messages": len(rows), "path": str(path), "scope": scope}
