import mimetypes
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .files import safe_parts
from .model import Asset, FileInfo, RecoveryError

KIND_NAMES = {
    "photo": "Фото",
    "video": "Видео",
    "round_video": "Кружочки",
    "voice": "Голосовые",
    "sticker": "Стикеры",
    "animation": "Анимации",
    "audio": "Аудио",
    "document": "Документы",
    "contact": "Контакты",
    "location": "Геолокации",
}
EXT_MIME = {
    ".tgs": "application/x-tgsticker",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".m4a": "audio/mp4",
    ".webp": "image/webp",
    ".mkv": "video/x-matroska",
}


def identify(name: str, header: bytes) -> tuple[str, bool]:
    if header.lstrip(b"\xef\xbb\xbf\r\n ").upper().startswith(b"BEGIN:VCARD"):
        return "text/vcard", True
    ext = PurePosixPath(name).suffix.lower()
    for magic, mime in (
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"OggS", "audio/ogg"),
        (b"%PDF-", "application/pdf"),
        (b"ID3", "audio/mpeg"),
        (b"fLaC", "audio/flac"),
        (b"PK\x03\x04", "application/zip"),
    ):
        if header.startswith(magic):
            return mime, True
    if header.startswith(b"RIFF"):
        chunk = {b"WEBP": "image/webp", b"WAVE": "audio/wav", b"AVI ": "video/x-msvideo"}.get(header[8:12])
        if chunk:
            return chunk, True
    if header[4:8] == b"ftyp":
        if header[8:12] in (b"heic", b"heix", b"mif1"):
            return "image/heic", True
        if header[8:12] in (b"avif", b"avis"):
            return "image/avif", True
        if header[8:12] == b"qt  ":
            return "video/quicktime", True
        return ("audio/mp4" if ext == ".m4a" else "video/mp4"), True
    if header.startswith(b"\x1aE\xdf\xa3"):
        return ("video/webm" if b"webm" in header.lower() else "video/x-matroska"), True
    if header.startswith(b"\x1f\x8b"):
        return ("application/x-tgsticker" if ext == ".tgs" else "application/gzip"), True
    if len(header) >= 2 and header[0] == 255 and header[1] & 0xE0 == 0xE0:
        return "audio/mpeg", True
    return EXT_MIME.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream", False


def reference(asset: Asset) -> str:
    raw = unquote(asset.original) if asset.url_encoded else asset.original
    raw = raw.replace("\\", "/")
    if not raw or raw.startswith("/") or (asset.url_encoded and urlsplit(raw).scheme):
        raise RecoveryError("Недопустимая ссылка на вложение.")
    parts = list(PurePosixPath(asset.base).parts)
    for part in raw.split("/"):
        if part == ".." and parts:
            parts.pop()
        elif part != ".":
            parts.extend(safe_parts(part))
    if not parts:
        raise RecoveryError("Пустая ссылка на вложение.")
    return "/".join(parts)


def check_asset(asset: Asset, files: dict[str, FileInfo]) -> tuple[str, str]:
    if asset.metadata:
        return "", "metadata"
    if not asset.original or asset.original.startswith("(File "):
        return "", "missing"
    try:
        path = reference(asset)
    except (RecoveryError, ValueError):
        return "", "unsafe"
    info = files.get(path)
    if info is None:
        return path, "missing"
    if not info.size:
        return path, "empty"
    if asset.expected_size is not None and asset.expected_size != info.size:
        return path, "size_mismatch"
    families = {
        "photo": ("image/",),
        "video": ("video/",),
        "round_video": ("video/",),
        "voice": ("audio/",),
        "audio": ("audio/",),
        "animation": ("video/", "image/"),
        "sticker": ("image/", "video/", "application/x-tgsticker"),
    }
    if asset.kind in families and not info.mime.startswith(families[asset.kind]):
        return path, "type_mismatch"
    if not info.recognized:
        return path, "unconfirmed_type"
    ext = PurePosixPath(path).suffix.lower()
    signatures = {
        ".jpg": {"image/jpeg"},
        ".jpeg": {"image/jpeg"},
        ".png": {"image/png"},
        ".webp": {"image/webp"},
        ".gif": {"image/gif"},
        ".pdf": {"application/pdf"},
        ".mp3": {"audio/mpeg"},
        ".opus": {"audio/ogg"},
        ".tgs": {"application/x-tgsticker"},
    }
    if ext in signatures and info.mime not in signatures[ext]:
        return path, "type_mismatch"
    declared_family = (asset.expected_mime or "").partition("/")[0]
    if declared_family in {"image", "audio", "video"} and not info.mime.startswith(declared_family + "/"):
        return path, "type_mismatch"
    return path, "ok"
