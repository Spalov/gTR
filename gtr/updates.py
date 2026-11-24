"""Install an explicitly selected official Windows package into an isolated directory."""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from . import BETA_NUMBER
from .files import BLOCK, local, name_key, new_directory, private_directory, safe_parts
from .model import RecoveryError, quiet
from .owner import app_home

OFFICIAL_REPOSITORY = "Spalov/gTR"
ARCHIVE = re.compile(r"gTR-Beta-([1-9][0-9]{0,4})-Windows-x64\.zip")
FOLDER = re.compile(r"gTR-Beta-([1-9][0-9]{0,4})")
SHA256 = re.compile(r"[a-f0-9]{64}")
RECEIPT = ".gtr-installed.json"
MAX_ARCHIVE = 512 * 1024**2
MAX_EXTRACTED = 2 * 1024**3
REQUIRED = ("gTR.cmd", "gtr/__init__.py", "runtime/pythonw.exe", "runtime/python314._pth")


@dataclass(frozen=True)
class Update:
    path: Path
    number: int


def portable() -> bool:
    executable = Path(sys.executable)
    return (
        sys.platform == "win32"
        and executable.parent.name == "runtime"
        and (executable.parent.parent / "gTR.cmd").is_file()
    )


def downloads():
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                path, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
                return Path(os.path.expandvars(path))
        except OSError:
            pass
    return Path.home() / "Downloads"


def candidate(path, current=BETA_NUMBER):
    path = local(Path(path))
    match = ARCHIVE.fullmatch(path.name)
    if match and int(match[1]) > current and path.is_file() and 0 < path.stat().st_size <= MAX_ARCHIVE:
        return Update(path, int(match[1]))
    raise RecoveryError("Выберите скачанный ZIP выпуска gTR Beta с более высоким номером.")


def find_download(folder=None, current=BETA_NUMBER):
    try:
        root = local(Path(folder or downloads()))
        latest = None
        for path in root.glob("gTR-Beta-*-Windows-x64.zip"):
            try:
                found = candidate(path, current)
            except (OSError, RecoveryError):
                continue
            if latest is None or found.number > latest.number:
                latest = found
        return latest
    except (OSError, RecoveryError):
        return None


def official_asset(update):
    url = f"https://api.github.com/repos/{OFFICIAL_REPOSITORY}/releases/tags/beta-{update.number}"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "gTR Windows Update",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            domain = urlsplit(response.geturl())
            if domain.scheme != "https" or domain.netloc != "api.github.com":
                raise ValueError
            body = response.read(1024**2 + 1)
            if len(body) > 1024**2:
                raise ValueError
        release = json.loads(body)
        if release["tag_name"] != f"beta-{update.number}" or release["draft"]:
            raise ValueError
        asset = next(
            x for x in release["assets"] if x["name"] == update.path.name and x["state"] == "uploaded"
        )
        digest = asset["digest"]
        size = asset["size"]
        if (
            type(size) is not int
            or not 0 < size <= MAX_ARCHIVE
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or not SHA256.fullmatch(digest[7:])
        ):
            raise ValueError
        return size, digest[7:]
    except (URLError, OSError, StopIteration, ValueError, TypeError, KeyError, RecursionError):
        raise RecoveryError(
            "GitHub не подтвердил архив этого выпуска. Скачайте официальный комплект и повторите проверку."
        ) from None


def _verify_copy(source, copy, size, expected, progress):
    sha = hashlib.sha256()
    done = 0
    with local(source).open("rb") as src, copy.open("xb") as dst:
        for chunk in iter(lambda: src.read(BLOCK), b""):
            done += len(chunk)
            if done > size:
                break
            sha.update(chunk)
            dst.write(chunk)
            progress("verify_update", done // 1024, size // 1024)
    if done != size or sha.hexdigest() != expected:
        raise RecoveryError("Контрольная сумма архива отличается от официальной сборки GitHub.")


def _extract(zip_path, work, prefix, progress):
    with ZipFile(zip_path) as package:
        entries = package.infolist()
        if len(entries) > 20000 or sum(entry.file_size for entry in entries) > MAX_EXTRACTED:
            raise RecoveryError("Объём комплекта превышает допустимый размер.")
        seen = set()
        for index, entry in enumerate(entries, 1):
            parts = safe_parts(entry.filename.rstrip("/"))
            key = name_key("/".join(parts))
            if (
                len(parts) < 2
                or parts[0] != prefix
                or key in seen
                or name_key(parts[-1]) == RECEIPT
                or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)
            ):
                raise RecoveryError("Архив обновления содержит недопустимый путь.")
            seen.add(key)
            out = work.joinpath(*parts[1:])
            if entry.is_dir():
                out.mkdir(parents=True, exist_ok=True)
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                with package.open(entry) as source, out.open("xb") as target:
                    for chunk in iter(lambda: source.read(BLOCK), b""):
                        target.write(chunk)
                        progress("extract_update", index, len(entries))
        if any(not (work / name).is_file() for name in REQUIRED):
            raise RecoveryError("В архиве отсутствуют файлы запуска gTR.")


def _versions():
    return local(app_home() / "versions")


def install(update, progress=quiet):
    update = candidate(update.path)
    progress("verify_update", 0, 0)
    size, digest = official_asset(update)
    root = _versions()
    try:
        root.mkdir(parents=True)
    except FileExistsError:
        pass
    else:
        private_directory(root)
    dest = root / f"gTR-Beta-{update.number}"
    try:
        with TemporaryDirectory(dir=root, prefix=".gtr-download-") as temporary:
            folder = Path(temporary)
            private_directory(folder)
            copy = folder / "verified.zip"
            _verify_copy(update.path, copy, size, digest, progress)
            with new_directory(dest) as work:
                _extract(copy, work, dest.name, progress)
                with (work / RECEIPT).open("x", encoding="ascii") as output:
                    json.dump({"beta": update.number, "sha256": digest}, output)
    except (OSError, BadZipFile, RuntimeError, ValueError):
        raise RecoveryError(
            "Не удалось установить обновление. Проверьте свободное место и целостность архива."
        ) from None
    return dest


def installed_update(current=BETA_NUMBER):
    latest = None
    try:
        for folder in _versions().glob("gTR-Beta-*"):
            match = FOLDER.fullmatch(folder.name)
            if not match or int(match[1]) <= current:
                continue
            try:
                folder = local(folder)
                with local(folder / RECEIPT).open("rb") as marker:
                    data = json.loads(marker.read(256))
                if data["beta"] != int(match[1]) or not SHA256.fullmatch(data["sha256"]):
                    continue
                if any(not local(folder / name).is_file() for name in REQUIRED):
                    continue
            except (OSError, RecoveryError, ValueError, TypeError, KeyError):
                continue
            current, latest = int(match[1]), folder
    except (OSError, RecoveryError):
        return None
    return latest


def launch(folder, project=None):
    runtime = local(folder / "runtime/pythonw.exe")
    env = {k: v for k, v in os.environ.items() if k.upper() not in {"PYTHONPATH", "PYTHONHOME"}}
    command = [str(runtime), "-m", "gtr", "gui"]
    if project:
        command.extend(["--project", str(project)])
    subprocess.Popen(command, cwd=folder, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
