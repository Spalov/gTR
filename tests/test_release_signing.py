import subprocess
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest


@pytest.fixture
def builder(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "packaging"))
    return import_module("build_release")


def test_native_executables_are_signed_before_ctest(builder, monkeypatch, tmp_path):
    sdk = tmp_path / "sdk"
    version = sdk / "lib/cmake/Qt6/Qt6ConfigVersionImpl.cmake"
    version.parent.mkdir(parents=True)
    version.write_text(f'set(PACKAGE_VERSION "{builder.QT_VERSION}")', encoding="ascii")
    version.with_name("Qt6Config.cmake").touch()
    for source, _ in builder.QT_RUNTIME:
        library = sdk / source
        library.parent.mkdir(parents=True, exist_ok=True)
        library.touch()
    commands = []
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "subprocess", SimpleNamespace(run=lambda args, **_: commands.append(args)))
    compiled = builder.build_native(sdk, signing=SimpleNamespace(sign=lambda exe: commands.append(["sign", exe])))
    assert commands[0][0] == commands[1][0] == "cmake"
    assert "-DBUILD_TESTING=ON" in commands[0]
    assert commands[2:4] == [["sign", compiled / "protocol-test.exe"], ["sign", compiled / "native-integration-test.exe"]]
    assert commands[4][0] == "ctest" and "--no-tests=error" in commands[4]


@pytest.mark.parametrize("status", [0, 43])
def test_release_runs_exact_packaged_launcher_in_isolation(builder, monkeypatch, tmp_path, status):
    portable = tmp_path / "candidate.zip"
    with ZipFile(portable, "w") as package:
        for member in builder.PORTABLE_PROGRAMS:
            package.writestr(f"{builder.NAME}/{member}", member)

    def launch(args, *, cwd, env, **_):
        assert args == [str(cwd / "gTR.exe"), "--health-check"]
        assert all((cwd / name).read_text() == name for name in builder.PORTABLE_PROGRAMS)
        assert Path(env["LOCALAPPDATA"]) == cwd.parent / "profile"
        assert env["QT_QPA_PLATFORM"] == "windows"
        return subprocess.CompletedProcess(args, status)

    monkeypatch.setattr(builder, "subprocess", SimpleNamespace(run=launch))
    if status:
        with pytest.raises(SystemExit, match="Windows Application Control blocked gTR-shell.exe"):
            builder.verify_packaged_launch(portable)
    else:
        builder.verify_packaged_launch(portable)
    assert list(tmp_path.iterdir()) == [portable]


@pytest.mark.parametrize(
    "status,blocked",
    [(None, None), (4551, "blocked gAssets before installation"), (43, "blocked gTR during")],
)
def test_release_checks_bundled_update_center_in_isolation(builder, monkeypatch, tmp_path, status, blocked):
    bundle = import_module("gassets_release")
    package = tmp_path / bundle.GSU_NAME
    package.touch()

    def launch(args, *, cwd, env, **_):
        assert args == [str(tmp_path / "gAssets.exe"), "--verify-install", str(package)]
        assert cwd == tmp_path
        scratch = Path(env["TEMP"])
        assert scratch.is_dir() and scratch.parent == tmp_path
        assert env["TMP"] == env["LOCALAPPDATA"] == str(scratch)
        assert env["QT_QPA_PLATFORM"] == "windows"
        if status == 4551:
            raise OSError(22, "launch blocked", args[0], 4551)
        return subprocess.CompletedProcess(args, status or 0)

    monkeypatch.setattr(bundle, "subprocess", SimpleNamespace(run=launch))
    if blocked:
        with pytest.raises(SystemExit, match=blocked):
            bundle.verify_update_center(tmp_path, package)
    else:
        bundle.verify_update_center(tmp_path, package)
    assert list(tmp_path.iterdir()) == [package]
