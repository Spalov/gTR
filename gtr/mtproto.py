"""The user-session MTProto adapter. This module never connects a client."""

import asyncio
import re
from pathlib import Path
from time import monotonic

from .faults import FAULTS, ImportFault
from .filenames import display_name

PART_KIB = 512
READ_RETRY_CODES = {"TIMEOUT", "CONNECTION_LOST", "SERVER_FAILURE"}


async def repeat_read(operation, *, sleep=asyncio.sleep):
    for delay in (0.25, 0.5, None):
        try:
            return await operation()
        except ImportFault as fault:
            if delay is None or fault.seconds or fault.code not in READ_RETRY_CODES:
                raise
            await sleep(delay)


def rpc_error(error: Exception) -> ImportFault:
    if isinstance(error, TimeoutError):
        return ImportFault("TIMEOUT")
    if isinstance(error, OSError):
        return ImportFault("CONNECTION_LOST")
    code = getattr(error, "code", 0) or 0
    if code >= 500 or code < 0:
        return ImportFault("SERVER_FAILURE")
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", type(error).__name__.removesuffix("Error")).upper()
    raw = str(getattr(error, "message", ""))
    if name in {"FLOOD_WAIT", "FLOOD_PREMIUM_WAIT"} or raw.startswith(("FLOOD_WAIT", "FLOOD_PREMIUM_WAIT")):
        seconds = getattr(error, "seconds", 0) or int((re.findall(r"\d+", raw) or [0])[0])
        return ImportFault("FLOOD_WAIT", seconds)
    if name == "PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT_MIN" or raw.startswith("PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT"):
        minutes = getattr(error, "minutes", 0) or int((re.findall(r"\d+", raw) or [0])[0])
        return ImportFault("PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT", 60 * minutes)
    if name in {"PHONE_CODE_EXPIRED", "PHONE_CODE_EMPTY"}:
        return ImportFault("PHONE_CODE_INVALID")
    if name in {"FILE_PARTS_INVALID", "FILE_TOO_BIG"}:
        return ImportFault("MEDIA_SIZE_LIMIT")
    return ImportFault(next((key for key in (name, raw) if key in FAULTS), "RPC_REJECTED"))


async def request(awaitable, *, seconds=45, timed=True):
    from telethon.errors import RPCError

    try:
        return await asyncio.wait_for(awaitable, seconds) if timed else await awaitable
    except (RPCError, OSError, TimeoutError) as error:
        raise rpc_error(error) from None


class TelegramAPI:
    def __init__(self, client, peer_kind: str, *, premium=False, timed=False):
        from telethon import functions, types

        self.client, self.peer_kind = client, peer_kind
        self.messages, self.types = functions.messages, types
        self.help = functions.help
        self.premium, self.timed = premium, timed

    async def rpc(self, call):
        return await request(self.client(call), timed=self.timed)

    async def read_rpc(self, call):
        if not self.timed:
            return await self.rpc(call)
        return await repeat_read(lambda: self.rpc(call))

    async def check_head(self, head):
        response = await self.read_rpc(self.messages.CheckHistoryImportRequest(import_head=head))
        if bool(response.pm) == bool(response.group):
            return "unknown"
        return "pm" if response.pm else "group" if response.group else "unknown"

    async def check_peer(self, peer, parsed_kind):
        if parsed_kind not in ("unknown", self.peer_kind):
            raise ImportFault("PEER_TYPE")
        response = await self.read_rpc(self.messages.CheckHistoryImportPeerRequest(peer=peer))
        return response.confirm_text

    async def upload_limit(self) -> int | None:
        response = await self.read_rpc(self.help.GetAppConfigRequest(hash=0))
        key = "upload_max_fileparts_premium" if self.premium else "upload_max_fileparts_default"
        for entry in getattr(getattr(response, "config", None), "value", ()):
            if entry.key == key:
                count = getattr(entry.value, "value", None)
                if type(count) in (int, float) and 1 <= count <= 1_000_000:
                    return int(count) * PART_KIB * 1024
        return None

    async def upload_file(self, path: Path, name: str, progress=None):
        options = {"progress_callback": progress} if progress else {}
        if not self.timed:
            return await request(self.client.upload_file(str(path), file_name=name, **options), timed=False)
        # File-part requests have bounded time; a large file still has a finite total deadline.
        seconds = min(3600, max(120, 120 + path.stat().st_size // (128 * 1024)))
        return await request(
            self.client.upload_file(str(path), file_name=name, part_size_kb=PART_KIB, **options), seconds=seconds
        )

    async def upload_transcript(self, transcript):
        return await self.upload_file(transcript, "_chat.txt")

    async def initialize_uploaded(self, peer, file, media_count):
        result = await self.rpc(
            self.messages.InitHistoryImportRequest(peer=peer, file=file, media_count=media_count)
        )
        return result.id

    async def initialize(self, peer, transcript, media_count):
        return await self.initialize_uploaded(peer, await self.upload_transcript(transcript), media_count)

    def input_media(self, file, item):
        if item["upload_as"] == "photo":
            return self.types.InputMediaUploadedPhoto(file=file)
        attributes = [self.types.DocumentAttributeFilename(file_name=display_name(item))]
        details = item.get("attributes", {})
        match details.get("type"):
            case "video":
                attributes.append(
                    self.types.DocumentAttributeVideo(
                        duration=details["duration"],
                        w=details["width"],
                        h=details["height"],
                        round_message=details["round"],
                    )
                )
            case "audio":
                attributes.append(
                    self.types.DocumentAttributeAudio(duration=details["duration"], voice=details["voice"])
                )
            case "animation":
                attributes.append(self.types.DocumentAttributeAnimated())
            case "sticker":
                attributes.append(
                    self.types.DocumentAttributeSticker(
                        alt=details["emoji"], stickerset=self.types.InputStickerSetEmpty()
                    )
                )
        return self.types.InputMediaUploadedDocument(
            file=file,
            mime_type=item["mime"],
            attributes=attributes,
            force_file=item.get("force_file", item["kind"] in {"document", "contact", "location"}),
        )

    async def upload_media(self, peer, import_id, item, path):
        return await self.upload_media_traced(peer, import_id, item, path, lambda stage: None)

    async def upload_media_traced(self, peer, import_id, item, path, stage):
        stage("upload_file_parts")
        name = display_name(item)
        started = last = monotonic()
        report = getattr(stage, "upload_bytes", None)

        def progress(sent, size):
            nonlocal last
            now = monotonic()
            if sent == size or now - last >= 0.15:
                report(name, sent, size, int(sent / max(now - started, 0.001)))
                last = now

        file = await self.upload_file(path, name, progress if report else None)
        stage("build_input_media")
        media = self.input_media(file, item)
        stage("upload_imported_media")
        result = await self.rpc(
            self.messages.UploadImportedMediaRequest(
                peer=peer,
                import_id=import_id,
                file_name=item["name"],
                media=media,
            )
        )
        constructor = type(result).__name__
        if not (
            isinstance(result, self.types.MessageMediaPhoto)
            and isinstance(result.photo, self.types.Photo)
            or isinstance(result, self.types.MessageMediaDocument)
            and isinstance(result.document, self.types.Document)
        ):
            raise ImportFault("MEDIA_INVALID", response_constructor=constructor)
        return constructor

    async def start(self, peer, import_id):
        return await self.rpc(self.messages.StartHistoryImportRequest(peer=peer, import_id=import_id))
