"""Check the current local release artifacts without printing private match values."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from audit_beta6 import (
    GITHUB_TOKEN,
    PRIVATE_EXTENSIONS,
    PRIVATE_NAMES,
    PROTECTED,
    audit,
    extract_patterns,
)
from build_release import NAME, RELEASE
from gassets_release import GSU_NAME, SETUP_NAME, sha_file

KEY_BLOCK = re.compile(rb"-{5}BEGIN (?:[A-Z]+ )?PRIVATE KEY-{5}")
CENTER_FILES = {
    "gAssets.exe", "gAssets.exe.config", "gassets.ico", "gsu.ico", "TERMS.txt",
    "gassets-catalog.json", "gassets-catalog.sig",
}


def secret_scan(data, patterns):
    lower = data.lower()
    if (any(pattern.lower() in lower for pattern in patterns) or GITHUB_TOKEN.search(data)
            or PROTECTED.search(data) or KEY_BLOCK.search(data)):
        raise ValueError("Private data pattern detected")


def scan_git(repo, patterns):
    objects = subprocess.check_output(["git", "-C", str(repo), "rev-list", "--objects", "--all"]).splitlines()
    count = 0
    for line in objects:
        revision, separator, filename = line.partition(b" ")
        if not separator:
            continue
        path = PurePosixPath(filename.decode("utf-8", errors="replace"))
        if path.name.lower() in PRIVATE_NAMES or path.suffix.lower() in PRIVATE_EXTENSIONS:
            raise ValueError("Private file pattern detected in Git history")
        git = ["git", "-C", str(repo), "cat-file"]
        if subprocess.check_output([*git, "-t", revision.decode()]).strip() != b"blob":
            continue
        data = subprocess.check_output([*git, "blob", revision.decode()])
        secret_scan(data, patterns)
        count += 1
    return count


def scan_gsu(kit, portable, patterns):
    with ZipFile(portable) as source, ZipFile(kit / GSU_NAME) as gsu:
        assert gsu.testzip() is None
        manifest = gsu.read("manifest.json")
        secret_scan(manifest, patterns)
        fields = json.loads(manifest)
        entries = fields["files"]
        named = {item["path"]: item for item in entries}
        assert len(named) == len(entries)
        assert len(gsu.namelist()) == len(named) + 2
        assert set(gsu.namelist()) == {"manifest.json", "manifest.sig", *("payload/" + n for n in named)}
        assert set(source.namelist()) == {NAME + "/" + n for n in named}
        assert len(gsu.read("manifest.sig")) == 64
        for path, entry in named.items():
            assert gsu.getinfo("payload/" + path).file_size == source.getinfo(NAME + "/" + path).file_size == entry["size"]
            for archive, name in ((gsu, "payload/" + path), (source, NAME + "/" + path)):
                with archive.open(name) as stream:
                    assert hashlib.file_digest(stream, "sha256").hexdigest() == entry["sha256"]
    return len(named)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, default=RELEASE / f"{NAME.removeprefix('gTR-')}-Local-Test")
    parser.add_argument("--scan-strings", type=Path)
    parser.add_argument("--git", type=Path)
    args = parser.parse_args()
    extra = args.scan_strings.read_text(encoding="utf-8").splitlines() if args.scan_strings else []
    marks = extract_patterns(extra)
    kit = args.kit.resolve()
    expected = (kit / f"{NAME}-SHA256SUMS.txt").read_text(encoding="ascii").splitlines()
    for line in expected:
        digest, name = line.split("  ", 1)
        assert "/" not in name and "\\" not in name
        assert sha_file(kit / name) == digest

    portable = kit / f"{NAME}-Windows-x64.zip"
    print(f"Windows archive: {audit(portable, False, marks)} entries")
    print(f"Source archive: {audit(kit / f'{NAME}-source.zip', True, marks)} entries")
    print(f"GSU payload: {scan_gsu(kit, portable, marks)} files match ZIP and manifest")
    with ZipFile(kit / "gAssets-UpdateCenter-Windows-x64.zip") as center:
        assert center.testzip() is None and set(center.namelist()) == CENTER_FILES
        for name in CENTER_FILES:
            secret_scan(center.read(name), marks)
    setup = kit / SETUP_NAME
    if not setup.is_file() and (kit / "LOCAL-TEST.txt").is_file():
        setup = kit / "gAssets-Setup-Windows-x64.exe"
    secret_scan(setup.read_bytes(), marks)
    meta = json.loads((kit / "gassets-release.json").read_bytes())
    assert meta["package"]["sha256"] == sha_file(kit / GSU_NAME)
    assert meta["package"]["size_bytes"] == (kit / GSU_NAME).stat().st_size
    for file in ("gassets-release", "gassets-catalog"):
        secret_scan((kit / (file + ".json")).read_bytes(), marks)
        assert len((kit / (file + ".sig")).read_bytes()) == 64
    if args.git:
        print(f"Local Git history: {scan_git(args.git, marks)} blobs")
    print("Checked SHA-256, file boundaries, matching payload and private-data markers.")


if __name__ == "__main__":
    main()
