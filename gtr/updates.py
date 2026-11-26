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
from time import monotonic, sleep
from urllib.error import URLError
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from . import BETA_VERSION, beta_folder, beta_label
from .files import (
    BLOCK,
    local,
    name_key,
    new_directory,
    private_directory,
    publish_text,
    safe_parts,
)
from .model import RecoveryError, quiet
from .owner import app_home

OFFICIAL_REPOSITORY = "Spalov/gTR"
RELEASE_PATTERN = r"gTR-Beta-([1-9][0-9]{0,4})(-Fix)?"
ARCHIVE = re.compile(RELEASE_PATTERN + r"-Windows-x64\.zip")
FOLDER = re.compile(RELEASE_PATTERN)
SHA256 = re.compile(r"[a-f0-9]{64}")
RECEIPT = ".gtr-installed.json"
FAILED_LAUNCH = ".gtr-launch-failed"
BOOT_ENV = "GTR_UPDATE_BOOT_FILE"
MAX_ARCHIVE = 512 * 1024**2
MAX_EXTRACTED = 2 * 1024**3
REQUIRED = ("gTR.cmd", "gtr/__init__.py", "runtime/pythonw.exe", "runtime/python314._pth")


@dataclass(frozen=True)
class Update:
    path: Path
    number: int
    fix: bool = False

    @property
    def version(self):
        return self.number, self.fix

    @property
    def label(self):
        return beta_label(*self.version)

    @property
    def folder_name(self):
        return beta_folder(*self.version)

    @property
    def tag(self):
        return self.folder_name.removeprefix("gTR-").lower()


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


def _current_version(current):
    # An integer refers to the original, unfixed Beta of that number.
    return (current, False) if isinstance(current, int) else current


def candidate(path, current=BETA_VERSION):
    path = local(Path(path))
    match = ARCHIVE.fullmatch(path.name)
    if match and path.is_file() and 0 < path.stat().st_size <= MAX_ARCHIVE:
        update = Update(path, int(match[1]), bool(match[2]))
        if update.version > _current_version(current):
            return update
    raise RecoveryError("Выберите скачанный ZIP более нового выпуска gTR Beta.")


def find_download(folder=None, current=BETA_VERSION):
    try:
        root = local(Path(folder or downloads()))
        latest = None
        for path in root.glob("gTR-Beta-*-Windows-x64.zip"):
            try:
                found = candidate(path, current)
            except (OSError, RecoveryError):
                continue
            if latest is None or found.version > latest.version:
                latest = found
        return latest
    except (OSError, RecoveryError):
        return None


def official_asset(update):
    url = f"https://api.github.com/repos/{OFFICIAL_REPOSITORY}/releases/tags/{update.tag}"
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
            if response.geturl() != url:
                raise ValueError
            body = response.read(1024**2 + 1)
            if len(body) > 1024**2:
                raise ValueError
        release = json.loads(body)
        if release["tag_name"] != update.tag or release["draft"]:
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


def install(update, progress=quiet, *, current=BETA_VERSION):
    update = candidate(update.path, current)
    progress("verify_update", 0, 0)
    size, digest = official_asset(update)
    root = _versions()
    try:
        root.mkdir(parents=True)
    except FileExistsError:
        pass
    else:
        private_directory(root)
    dest = root / update.folder_name
    try:
        with TemporaryDirectory(dir=root, prefix=".gtr-download-") as temporary:
            folder = Path(temporary)
            private_directory(folder)
            copy = folder / "verified.zip"
            _verify_copy(update.path, copy, size, digest, progress)
            with new_directory(dest) as work:
                _extract(copy, work, dest.name, progress)
                with (work / RECEIPT).open("x", encoding="ascii") as output:
                    json.dump({"beta": update.number, "fix": update.fix, "sha256": digest}, output)
    except (OSError, BadZipFile, RuntimeError, ValueError):
        raise RecoveryError(
            "Не удалось установить обновление. Проверьте свободное место и целостность архива."
        ) from None
    return dest


def installed_update(current=BETA_VERSION):
    latest = None
    current = _current_version(current)
    try:
        for folder in _versions().glob("gTR-Beta-*"):
            match = FOLDER.fullmatch(folder.name)
            if not match:
                continue
            version = int(match[1]), bool(match[2])
            if version <= current:
                continue
            try:
                folder = local(folder)
                if (folder / FAILED_LAUNCH).exists():
                    continue
                with local(folder / RECEIPT).open("rb") as marker:
                    data = json.loads(marker.read(256))
                if type(data["beta"]) is not int or data["beta"] != version[0]:
                    continue
                if data.get("fix", False) is not version[1]:
                    continue
                if not SHA256.fullmatch(data["sha256"]):
                    continue
                if any(not local(folder / name).is_file() for name in REQUIRED):
                    continue
            except (OSError, RecoveryError, ValueError, TypeError, KeyError):
                continue
            current, latest = version, folder
    except (OSError, RecoveryError):
        return None
    return latest


def launch(folder, project=None, *, timeout=30):
    folder = local(Path(folder))
    runtime = local(folder / "runtime/pythonw.exe")
    env = {k: v for k, v in os.environ.items() if k.upper() not in {"PYTHONPATH", "PYTHONHOME", BOOT_ENV}}
    command = [str(runtime), "-m", "gtr", "gui"]
    if project:
        command.extend(["--project", str(project)])
    try:
        with TemporaryDirectory(dir=_versions(), prefix=".gtr-boot-") as temporary:
            boot = Path(temporary)
            private_directory(boot)
            marker = boot / "ready"
            env[BOOT_ENV] = str(marker)
            process = subprocess.Popen(command, cwd=folder, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            expires = monotonic() + timeout
            while process.poll() is None and monotonic() < expires:
                if marker.is_file() and marker.read_bytes() == b"ready":
                    return
                sleep(0.1)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    publish_text(folder / FAILED_LAUNCH, "startup_failed\n", replace=True)
    raise RecoveryError("Новая версия не запустилась. Можно продолжить работу в предыдущей версии.")


def signal_ready():
    value = os.environ.pop(BOOT_ENV, "")
    if not value:
        return
    target = local(Path(value))
    if target.name == "ready" and target.parent.name.startswith(".gtr-boot-") and target.parent.parent == _versions():
        publish_text(target, "ready")
