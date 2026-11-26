"""Audit the exact Beta 6 archives. Never prints matched private patterns."""

import argparse
import hashlib
import re
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from build_release import NAME, RELEASE, ROOT, app_files

PRIVATE_EXTENSIONS = {".sqlite", ".sqlite3", ".session", ".db", ".dpapi", ".pfx", ".p12", ".key", ".pem", ".log"}
PRIVATE_NAMES = {"result.json", "messages.html", "_chat.txt", "manifest.json", ".env", "direct_url.json"}
FORBIDDEN_PARTS = {"gfl_engine", "gfl-engine", "projects", "media-cache", "imports.sqlite", "__pycache__", ".git"}
MS_MEDIA = "runtime/Lib/site-packages/PySide6/plugins/multimedia/windowsmediaplugin.dll"
PROTECTED = re.compile(rb"(?im)^\s*(?:TELEGRAM_API_HASH|TG_API_HASH|API_HASH)\s*=\s*['\"]?[0-9a-f]{32}\b")
GITHUB_TOKEN = re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{55,})\b")


def extract_patterns(extra):
    home = str(Path.home())
    private_strings = [home, home.replace("\\", "/"), *extra]
    return tuple({data for pattern in private_strings if len(pattern) >= 8
                  for data in (pattern.encode("utf-8"), pattern.encode("utf-16-le"))})


def audit(path, source, patterns):
    prefix = NAME + "/"
    app = {file.relative_to(ROOT).as_posix(): file for file in app_files()}
    names = set()
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        for entry in archive.infolist():
            relative = entry.filename.removeprefix(prefix)
            lower = relative.casefold()
            parts = PurePosixPath(relative).parts
            if (not entry.filename.startswith(prefix) or not parts or "\\" in relative or ".." in parts
                    or lower in names or entry.is_dir() or FORBIDDEN_PARTS.intersection(p.casefold() for p in parts)
                    or parts[-1].casefold() in PRIVATE_NAMES or PurePosixPath(relative).suffix.lower() in PRIVATE_EXTENSIONS
                    or ("gfl-engine-1." in lower) or lower.endswith((".pdb", ".session-journal"))
                    or "plugins/multimedia/ffmpeg" in lower or "/av.libs/" in lower):
                raise ValueError("Disallowed or duplicate archive entry")
            names.add(lower)
            contents = archive.read(entry)
            comparable = contents.lower()
            if any(pattern.lower() in comparable for pattern in patterns) or GITHUB_TOKEN.search(contents):
                raise ValueError("Private data pattern detected")
            if relative in app and app[relative].read_bytes() != contents:
                raise ValueError("Application bytes differ from the source")
            if relative.startswith("gtr/") and PROTECTED.search(contents):
                raise ValueError("Embedded Telegram hash")
    required = {name.lower() for name in app}
    required.update({"native/window.cpp", "tests/test_beta6_ipc.py"} if source else
                    {"gtr.exe", "runtime/pythonw.exe", "runtime/lib/site-packages/pyside6/gtr-shell.exe",
                     MS_MEDIA.lower()})
    if not required <= names:
        raise ValueError("Release files are incomplete")
    return len(names)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan-strings", type=Path, help="Optional local UTF-8 list of private markers")
    args = parser.parse_args()
    extra = args.scan_strings.read_text(encoding="utf-8").splitlines() if args.scan_strings else []
    patterns = extract_patterns(extra)
    checksums = (RELEASE / f"{NAME}-SHA256SUMS.txt").read_text(encoding="ascii")
    for suffix, source in (("-Windows-x64.zip", False), ("-source.zip", True)):
        path = RELEASE / (NAME + suffix)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if f"{digest}  {path.name}" not in checksums.splitlines():
            raise ValueError("Release checksum does not match")
        print(f"{path.name}: {audit(path, source, patterns)} verified entries, SHA-256 {digest}")


if __name__ == "__main__":
    main()
