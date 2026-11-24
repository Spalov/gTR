"""Bounded local reads, safe names and atomic publication."""

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

from .model import RecoveryError

BLOCK = 1024 * 1024
BAD_NAMES = re.compile(r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³])(?:\.|$)", re.IGNORECASE)


def safe_parts(name: str) -> list[str]:
    parts = name.replace("\\", "/").split("/")
    if any(
        not s
        or s in (".", "..")
        or s[-1] in " ."
        or BAD_NAMES.match(s)
        or any(ord(c) < 32 or c in '<>:"|?*' for c in s)
        for s in parts
    ):
        raise RecoveryError("Путь в архиве недопустим.")
    return parts


def name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def local(path: Path) -> Path:
    # Check the lexical path before resolve() could follow a junction or UNC link.
    path = Path(os.path.abspath(path))
    if str(path).startswith(("\\\\", "//")) or PureWindowsPath(path).drive.startswith("\\\\"):
        raise RecoveryError("Выберите локальный диск.")
    if os.name == "nt":
        import ctypes

        if ctypes.windll.kernel32.GetDriveTypeW(path.anchor) == 4:  # DRIVE_REMOTE
            raise RecoveryError("Выберите локальный диск.")
    for parent in (*reversed(path.parents), path):
        if os.path.lexists(parent) and is_link(parent):
            raise RecoveryError("Ссылки и точки повторного анализа не поддерживаются.")
    return path


def digest(path: Path, checkpoint=None) -> str:
    with local(path).open("rb") as stream:
        if checkpoint is None:
            return hashlib.file_digest(stream, "sha256").hexdigest()
        sha = hashlib.sha256()
        while data := stream.read(BLOCK):
            checkpoint()
            sha.update(data)
        return sha.hexdigest()


def private_directory(path: Path) -> None:
    """Set an inheritable owner-only ACL on an empty, newly created directory."""
    if os.name != "nt":
        path.chmod(0o700)
        return
    import ntsecuritycon
    import pywintypes
    import win32api
    import win32con
    import win32security as security

    try:
        token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid, _ = security.GetTokenInformation(token, security.TokenUser)
        finally:
            token.Close()
        acl = security.ACL()
        acl.AddAccessAllowedAceEx(
            security.ACL_REVISION,
            security.OBJECT_INHERIT_ACE | security.CONTAINER_INHERIT_ACE,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
        security.SetNamedSecurityInfo(
            str(path),
            security.SE_FILE_OBJECT,
            security.DACL_SECURITY_INFORMATION | security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            acl,
            None,
        )
    except pywintypes.error:
        raise RecoveryError("Не удалось ограничить доступ к новой папке gTR.") from None


def read_json(path):
    try:
        path = local(Path(path))
        if not path.is_file() or path.stat().st_size > 128 * 1024**2:
            raise ValueError
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError, UnicodeError, RecursionError):
        raise RecoveryError("Не удалось прочитать выбранный JSON.") from None


def publish_text(dest: Path, text: str, *, replace=False) -> None:
    with tempfile.NamedTemporaryFile(
        dir=dest.parent, prefix=".gtr-write-", mode="w", encoding="utf-8", delete=False
    ) as out:
        temp = Path(out.name)
        try:
            out.write(text)
            out.close()
            if replace:
                os.replace(temp, dest)
            elif os.name == "nt":
                os.rename(temp, dest)
            else:
                os.link(temp, dest)
        finally:
            out.close()
            temp.unlink(missing_ok=True)


@contextmanager
def new_directory(dest: Path, protected: tuple[Path, ...] = ()):
    dest = local(dest)
    if dest.exists() or any(dest.is_relative_to(local(p)) for p in protected):
        raise RecoveryError("Выберите новую папку за пределами исходных данных.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".gtr-", dir=dest.parent) as tmp:
        private_directory(Path(tmp))
        workspace = Path(tmp) / "work"
        workspace.mkdir()
        private_directory(workspace)
        yield workspace
        if dest.exists():
            raise RecoveryError("Выбранная папка уже занята.")
        workspace.rename(dest)
