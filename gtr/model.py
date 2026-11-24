from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


class RecoveryError(Exception):
    """An operator-facing error with no private archive data in its message."""


class Cancelled(RecoveryError):
    def __init__(self):
        super().__init__("Операция остановлена.")


Progress = Callable[[str, int, int], None]


def quiet(stage: str, done: int, total: int) -> None:
    pass


@dataclass
class Asset:
    kind: str
    original: str
    base: str = ""
    expected_size: int | None = None
    expected_mime: str = ""
    url_encoded: bool = False
    metadata: bool = False


@dataclass
class Message:
    source_id: str
    sender: str
    sender_key: str
    date_raw: str
    date_iso: str
    text: str
    rich: str
    source: str
    reply: str = ""
    reply_peer: str = ""
    service: bool = False
    assets: list[Asset] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


@dataclass
class Chat:
    key: str
    title: str
    messages: list[Message] = field(default_factory=list)
    pages: int = 1
    peer_key: str = ""


@dataclass(frozen=True)
class FileInfo:
    size: int
    sha256: str
    mime: str
    recognized: bool


@dataclass
class Snapshot:
    root: Path
    files: dict[str, FileInfo]
    source_hash: str
    input_size: int
    source_type: str
