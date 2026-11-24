"""Local deterministic fixtures for separate text and PNG experiments."""

import struct
import tempfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .conversion import prepare
from .core import analyze, encode
from .files import local, new_directory, private_directory
from .model import RecoveryError, quiet


def _png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    width, height = 256, 192
    header = struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0)
    pixels = b"".join(b"\0" + bytes((row, 118, 190)) * width for row in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


FIXTURE_SIZES = (2, 10, 100)


def incremental_fixture(dest):
    """Create three cumulative Desktop-style exports in a new local directory."""
    start = datetime(2025, 1, 2, 12, tzinfo=timezone(timedelta(hours=2)))
    with new_directory(Path(dest)) as root:
        for count in (10, 30, 120):
            folder = root / f"Экспорт-{count}"
            folder.mkdir()
            rows = []
            for i in range(1, count + 1):
                row = {
                    "id": i,
                    "type": "message",
                    "from": f"Участник {1 + i % 2}",
                    "from_id": f"fixture:{1 + i % 2}",
                    "date": (start + timedelta(seconds=i // 2)).isoformat(),
                    "text": f"Контрольная запись {i}",
                }
                if i >= 38:
                    name = f"Изображение_{i}.png"
                    (folder / name).write_bytes(_png())
                    if i % 2:
                        row.update(file=name, file_name=f"Документ_{i}.png", mime_type="image/png")
                    else:
                        row.update(photo=name)
                rows.append(row)
            (folder / "result.json").write_text(
                encode(
                    {"name": "Контрольный диалог", "type": "personal_chat", "id": 12345, "messages": rows}
                ),
                encoding="utf-8",
            )
    return {"экспорты":[10,30,120],"новые_записи":[10,20,90],"вложения_в_третьей_партии":83}


def prepare_fixture(dest: Path, scenario="text", progress=quiet, *, size=2):
    if scenario not in ("text", "media") or size not in FIXTURE_SIZES:
        raise RecoveryError("Выберите тип и размер синтетического пакета.")
    media = scenario == "media"
    with tempfile.TemporaryDirectory(prefix="gtr-fixture-") as tmp:
        root = local(Path(tmp))
        private_directory(root)
        source = root / "source"
        source.mkdir()
        start = datetime(2025, 1, 2, 10, tzinfo=timezone(timedelta(hours=2)))
        rows = [
            {
                "id": index,
                "type": "message",
                "from": f"Тестовый участник {1 + (index - 1) % 2}",
                "from_id": f"fixture:{1 + (index - 1) % 2}",
                "date": (start + timedelta(seconds=index if size == 2 else 1 + (index - 1) // 2)).isoformat(),
                "text": f"Синтетическое сообщение {index} для теста {'медиа' if media else 'текста'}.",
            }
            for index in range(1, size + 1)
        ]
        if media:
            image = _png()
            (source / "sample.png").write_bytes(image)
            rows[1].update(photo="sample.png", photo_file_size=len(image), mime_type="image/png")
        (source / "result.json").write_text(
            encode(
                {
                    "id": 2 if media else 1,
                    "type": "personal_chat",
                    "name": "Синтетический тест медиа" if media else "Синтетический тест текста",
                    "messages": rows,
                }
            ),
            encoding="utf-8",
        )
        project = analyze(source, root / "project", progress=progress)
        return prepare(
            project,
            1,
            dest,
            "source",
            progress,
            media_mode="text_media" if media else "text_only",
            fixture=scenario,
        )
