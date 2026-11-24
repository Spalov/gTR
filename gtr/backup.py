"""Publish a checked project backup that can be opened directly."""

import sqlite3
from pathlib import Path

from .core import catalogue, properties, verify
from .files import BLOCK, local, new_directory, safe_parts
from .model import RecoveryError, quiet


def backup_project(project: Path, dest: Path, progress=quiet) -> Path:
    project = local(Path(project))
    with catalogue(project) as source:
        original = Path(properties(source)["original"])
        with new_directory(dest, (project, original)) as output:
            db = sqlite3.connect(output / "history.sqlite")
            try:
                source.backup(db)
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RecoveryError("Не удалось проверить копию базы.")
            finally:
                db.close()
            (output / "source").mkdir()
            files = source.execute("SELECT path,sha256,size FROM files ORDER BY path").fetchall()
            for i, item in enumerate(files, 1):
                parts = safe_parts(item["path"])
                path = local(project.joinpath("source", *parts))
                target = output.joinpath("source", *parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with path.open("rb") as src, target.open("xb") as dst:
                    while data := src.read(BLOCK):
                        progress("backup", i - 1, len(files))
                        dst.write(data)
            check = verify(output, progress)
            if check.get("ok", 0) != len(files):
                raise RecoveryError("Копия не совпадает с каталогом SHA-256. Проверьте исходный проект.")
            progress("finish", 1, 1)
    return local(Path(dest))
