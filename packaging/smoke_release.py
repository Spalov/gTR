"""Exercise both launchers and the exact runtime contained in the Windows ZIP."""

import argparse
import os
import subprocess
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from zipfile import ZipFile

import pywintypes
import win32api
import win32con
import win32gui
import win32process
from build_release import EMBED, FILE_VERSION, NAME, RELEASE, ROOT, VERSION


def members(archive):
    assert archive.testzip() is None
    names = set()
    for entry in archive.namelist():
        member = PurePosixPath(entry)
        assert member.parts[0] == NAME and ".." not in member.parts and "\\" not in entry
        assert entry not in names and not {"owner.dpapi", "direct_url.json", "diagnostics.log"}.intersection(
            member.parts
        )
        assert not entry.endswith((".sqlite", ".session", ".dpapi", ".rar", ".pfx")) or entry.endswith(
            "/tests/fixtures/synthetic.rar"
        )
        names.add(entry)
    return names


def app_windows(runtime):
    result = set()

    def scan(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetClassName(hwnd).startswith("Qt"):
            return
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid
            )
            try:
                executable = Path(win32process.GetModuleFileNameEx(handle, 0))
            finally:
                handle.Close()
        except pywintypes.error:
            return
        if executable == runtime:
            result.add(hwnd)

    win32gui.EnumWindows(scan, None)
    return result


def open_gui(command, folder, env):
    runtime = folder / "runtime" / "pythonw.exe"
    previous = app_windows(runtime)
    with subprocess.Popen(
        command, cwd=folder, env=env, creationflags=subprocess.CREATE_NO_WINDOW
    ) as launcher:
        deadline = monotonic() + 18
        while monotonic() < deadline:
            if windows := app_windows(runtime) - previous:
                for hwnd in windows:
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                for _ in range(40):
                    if not any(win32gui.IsWindow(hwnd) for hwnd in windows):
                        return launcher.wait(timeout=4)
                    sleep(0.1)
                raise RuntimeError("The tested Qt window did not close")
            sleep(0.2)
        if launcher.poll() is None:
            launcher.terminate()
        raise RuntimeError("The tested launcher did not open a Qt window")


def check(folder, work, suite):
    exe, cmd, python = folder / "gTR.exe", folder / "gTR.cmd", folder / "runtime/python.exe"
    major, minor, patch, build = FILE_VERSION
    info = win32api.GetFileVersionInfo(str(exe), "\\")
    assert (info["FileVersionMS"], info["FileVersionLS"]) == ((major << 16) | minor, (patch << 16) | build)
    env = {**os.environ, "LOCALAPPDATA": str(work / "profile"), "QT_QPA_PLATFORM": "windows"}
    shell = [os.environ["ComSpec"], "/d", "/c", str(cmd)]
    expected = f"gTR {VERSION}"
    assert subprocess.check_output([*shell, "--version"], cwd=folder, env=env).decode().strip() == expected
    try:
        assert open_gui([str(exe)], folder, env) == 0
        print("gTR.exe opened and closed its Qt window", flush=True)
    except OSError as error:
        if error.winerror != 4551:
            raise
        print("gTR.exe blocked by Windows Application Control (4551)", flush=True)
    assert open_gui(shell, folder, env) == 0
    print("gTR.cmd opened and closed its Qt window", flush=True)
    if suite:
        import pytest

        testing = Path(pytest.__file__).resolve().parents[1]
        runtime_version = tuple(map(int, EMBED.split("-")[1].split(".")))
        script = f"""
from pathlib import Path
import sys
import PySide6, telethon, win32crypt, gtr
assert all(Path(m.__file__).is_relative_to(Path.cwd()) for m in (PySide6, telethon, win32crypt, gtr))
assert gtr.__version__ == {expected.removeprefix("gTR ")!r} and sys.version_info[:3] == {runtime_version!r}
sys.path.append({str(testing)!r})
import pytest
raise SystemExit(pytest.main([{str(ROOT / "tests")!r}, '--tb=short']))
"""
        subprocess.run([str(python), "-X", "utf8", "-c", script], check=True, cwd=folder, env=env)


def check_update(folder, work, archive):
    env = {**os.environ, "LOCALAPPDATA": str(work / "update-profile")}
    script = """
import subprocess
import sys
from functools import partial
from hashlib import file_digest
from pathlib import Path
from unittest.mock import patch

from gtr import BETA_NUMBER, __version__, updates

file = Path(sys.argv[1])
with file.open("rb") as stream:
    digest = file_digest(stream, "sha256").hexdigest()
from_previous_beta = partial(updates.candidate, current=BETA_NUMBER - 1)
project = updates.app_home() / "projects" / "synthetic.txt"
project.parent.mkdir(parents=True)
project.write_text("existing local data")
with patch.object(updates, "candidate", from_previous_beta), patch.object(
    updates, "official_asset", return_value=(file.stat().st_size, digest)
):
    installed = updates.install(from_previous_beta(file))
assert updates.installed_update(current=BETA_NUMBER - 1) == installed
assert project.read_text() == "existing local data"
version = subprocess.check_output(
    [str(installed / "runtime/python.exe"), "-m", "gtr", "--version"], cwd=installed
)
assert version.decode().strip() == "gTR " + __version__
"""
    subprocess.run(
        [str(folder / "runtime/python.exe"), "-X", "utf8", "-c", script, str(archive)],
        check=True, cwd=folder, env=env,
    )
    print("Verified side-by-side installation from the final Windows ZIP", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--pytest", action="store_true")
    args = parser.parse_args()
    archive = RELEASE / f"{NAME}-Windows-x64.zip"
    with (
        ZipFile(archive) as portable,
        ZipFile(RELEASE / f"{NAME}-source.zip") as source,
    ):
        shipped, sources = members(portable), members(source)
        assert all(portable.read(name) == source.read(name) for name in shipped & sources)
        assert all(
            source.read(name) == (ROOT / name.removeprefix(NAME + "/")).read_bytes() for name in sources
        )
        with TemporaryDirectory(dir=RELEASE, prefix="gtr-smoke-") as tmp:
            work = Path(tmp)
            assert work.resolve().is_relative_to(RELEASE.resolve())
            destination = RELEASE if args.publish else work
            assert not (destination / NAME).exists(), "Choose a clean extraction directory"
            portable.extractall(destination)
            check_update(destination / NAME, work, archive)
            check(destination / NAME, work, args.pytest)
    print(f"Checked {len(shipped)} portable entries and {len(sources)} source entries.")


if __name__ == "__main__":
    main()
