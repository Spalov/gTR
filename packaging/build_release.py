"""Assemble the portable CPython app and the source distribution. Build-time tools only."""

import argparse
import difflib
import io
import os
import re
import shutil
import subprocess
import sys
from hashlib import file_digest, sha256
from importlib.metadata import distribution
from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release"
release_version = run_path(str(ROOT / "gtr/__init__.py"))
VERSION = release_version["__version__"]
FILE_VERSION = (release_version["BETA_NUMBER"], 0, int(release_version["BETA_FIX"]), 0)
BASELINE = "gTR-1.0.1"
BASELINE_SHA = "8b478cc4fe76c92a54e900374cae00e5c646959518c90f61374cb81c426b0dfa"
NAME = release_version["RELEASE_NAME"]
PREFIX = f"{NAME}/"
PYTHON_VERSION = "3.14.10"
EMBED = f"python-{PYTHON_VERSION}-embed-amd64.zip"
EMBED_SHA256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"
REQUIREMENTS = ROOT / "requirements-win64.txt"
PACKAGES = (
    "beautifulsoup4",
    "soupsieve",
    "typing_extensions",
    "PySide6-Essentials",
    "shiboken6",
    "tzdata",
    "Telethon",
    "rsa",
    "pyasn1",
    "pyaes",
    "pywin32",
)
LOCAL_METADATA = {"direct_url.json", "INSTALLER", "RECORD", "REQUESTED"}
QT_VERSION = "6.12.0"
QT_RUNTIME = (
    ("bin/Qt6Multimedia.dll", "Qt6Multimedia.dll"),
    ("bin/Qt6MultimediaWidgets.dll", "Qt6MultimediaWidgets.dll"),
    ("plugins/multimedia/windowsmediaplugin.dll", "plugins/multimedia/windowsmediaplugin.dll"),
)


def archive_entry(package, name, source):
    info = ZipInfo(PREFIX + name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.compress_level = 5
    with package.open(info, "w") as dest:
        shutil.copyfileobj(source, dest, length=256 * 1024)


def archive_file(package, name, file):
    with file.open("rb") as source:
        archive_entry(package, name, source)


def app_files():
    return [
        ROOT / "README.md",
        ROOT / "gTR.cmd",
        REQUIREMENTS,
        *(
            ROOT / name
            for name in (
                "LICENSE",
                "NOTICE",
                "THIRD_PARTY.md",
                "TRADEMARKS.md",
                "CHANGELOG.md",
                "TEST_REPORT.md",
                "LIMITATIONS.md",
                "UPGRADE.md",
                "KNOWN_LIMITATIONS.md",
                "PRIVACY.md",
                "SECURITY.md",
            )
        ),
        *sorted(ROOT.glob("gtr/*.py")),
        ROOT / "gtr/assets/gtr.ico",
        ROOT / "gtr/assets/cat.png",
        ROOT / "gtr/assets/export-guide.md",
        ROOT / "licenses/GPL-3.0.txt",
        ROOT / "licenses/LGPL-3.0.txt",
        *(
            ROOT / "docs" / name
            for name in (
                "OWNER.md",
                "SAFE_IMPORT.md",
                "ARCHITECTURE.md",
                "BUILD.md",
                "ENGINEERING.md",
                "BETA_ENGINEERING.md",
                "INCREMENTAL_IMPORT.md",
                "MEDIA_FILENAME_HANDLING.md",
                "RECOVERY_ENGINE.md",
                "MEDIA_RECOVERY.md",
                "FORWARDED_MESSAGES.md",
                "VERIFICATION.md",
                "API_LIMITATIONS.md",
                "DATABASE_MIGRATION.md",
                "SECURITY_AUDIT.md",
                "BETA_5_TEST_PLAN.md",
                "HYBRID_ARCHITECTURE.md",
                "CPP_SHELL.md",
                "IPC_PROTOCOL.md",
                "MEDIA_ENGINE.md",
                "ATTACHMENT_BINDING.md",
                "FORWARD_RECOVERY.md",
                "CLIENT_COMPATIBILITY.md",
                "BENCHMARKS.md",
                "BETA_6_TEST_PLAN.md",
                "GGE_PROVENANCE.md",
            )
        ),
    ]


def source_archive(path):
    files = [
        *app_files(),
        ROOT / "pyproject.toml",
        ROOT / ".gitignore",
        ROOT / "setup.cmd",
        *ROOT.glob("tests/*.py"),
        ROOT / "tests/fixtures/synthetic.rar",
        ROOT / "packaging/build_release.py",
        ROOT / "packaging/benchmark_recovery.py",
        ROOT / "packaging/make_icon.py",
        ROOT / "packaging/smoke_release.py",
        ROOT / "packaging/audit_beta6.py",
        ROOT / "native/CMakeLists.txt",
        *ROOT.glob("native/*.cpp"),
        *ROOT.glob("native/*.hpp"),
        ROOT / "native/release.hpp.in",
        ROOT / "native/windows.rc.in",
        *ROOT.glob(".github/workflows/*.yml"),
    ]
    with ZipFile(path, "w") as package:
        for file in sorted(files):
            archive_file(package, file.relative_to(ROOT).as_posix(), file)


def locate_compiler(program_files):
    finder = program_files / "Microsoft Visual Studio/Installer/vswhere.exe"
    if finder.is_file():
        result = subprocess.check_output(
            [
                str(finder),
                "-utf8",
                "-latest",
                "-products",
                "*",
                "-requires",
                "Microsoft.Component.MSBuild",
                "-find",
                r"MSBuild\Current\Bin\Roslyn\csc.exe",
            ],
            encoding="utf-8-sig",
        ).splitlines()
        if result:
            return Path(result[0])
    raise SystemExit(
        "Install Visual Studio Build Tools with .NET Framework 4.8 targeting, or pass --compiler."
    )


def compile_launcher(target, compiler=None):
    program_files = Path(os.environ["ProgramFiles(x86)"])
    compiler = compiler or locate_compiler(program_files)
    reference = program_files / "Reference Assemblies/Microsoft/Framework/.NETFramework/v4.8"
    assemblies = [reference / f"{name}.dll" for name in ("mscorlib", "System", "System.Windows.Forms")]
    if not all(path.is_file() for path in assemblies):
        raise SystemExit("Install the .NET Framework 4.8 targeting pack.")
    numeric_version = ".".join(map(str, FILE_VERSION))
    with TemporaryDirectory() as temp:
        version_attributes = Path(temp) / "ReleaseVersion.cs"
        version_attributes.write_text(
            "using System.Reflection;\n"
            f'[assembly: AssemblyDescription("gTR {VERSION}")]\n'
            f'[assembly: AssemblyVersion("{numeric_version}")]\n'
            f'[assembly: AssemblyFileVersion("{numeric_version}")]\n'
            f'[assembly: AssemblyInformationalVersion("{VERSION}")]\n',
            encoding="utf-8",
        )
        subprocess.run(
            [
                str(compiler),
                "/nologo",
                "/noconfig",
                "/nostdlib+",
                "/deterministic+",
                "/codepage:65001",
                "/target:winexe",
                "/platform:x64",
                "/optimize+",
                *(f"/reference:{assembly}" for assembly in assemblies),
                "/win32icon:" + str(ROOT / "gtr/assets/gtr.ico"),
                "/out:" + str(target),
                str(ROOT / "packaging/Launcher.cs"),
                str(version_attributes),
            ],
            check=True,
        )


def sign_launcher(target, sign_tool, thumbprint, timestamp_url):
    subprocess.run(
        [
            str(sign_tool),
            "sign",
            "/sha1",
            thumbprint,
            "/fd",
            "SHA256",
            "/tr",
            timestamp_url,
            "/td",
            "SHA256",
            str(target),
        ],
        check=True,
    )
    subprocess.run([str(sign_tool), "verify", "/pa", "/tw", str(target)], check=True)


def build_native(sdk, *, allow_blocked_test=False):
    sdk = sdk.resolve()
    if not (sdk / "lib/cmake/Qt6/Qt6Config.cmake").is_file():
        raise SystemExit(f"Install Qt {QT_VERSION} MSVC x64 with Qt Multimedia or specify --qt-sdk.")
    version = sdk / "lib/cmake/Qt6/Qt6ConfigVersionImpl.cmake"
    if f'set(PACKAGE_VERSION "{QT_VERSION}")' not in version.read_text(encoding="utf-8"):
        raise SystemExit(f"Use the Qt SDK version {QT_VERSION} from requirements-win64.txt.")
    for source, _ in QT_RUNTIME:
        if not (sdk / source).is_file():
            raise SystemExit("Qt Multimedia with the Windows backend is required.")
    work = ROOT / "build/beta6-native"
    test_python = ROOT / ".venv/Scripts/python.exe"
    if not test_python.is_file():
        test_python = Path(sys.executable)
    subprocess.run(
        ["cmake", "-S", str(ROOT / "native"), "-B", str(work), "-G", "Visual Studio 17 2022", "-A", "x64",
         f"-DCMAKE_PREFIX_PATH={sdk}", f"-DGTR_TEST_PYTHON={test_python}"],
        check=True,
    )
    subprocess.run(["cmake", "--build", str(work), "--config", "Release", "--parallel", "8"], check=True)
    if allow_blocked_test:
        env = {**os.environ, "PATH": str(sdk / "bin") + os.pathsep + os.environ["PATH"]}
        try:
            subprocess.run([str(work / "Release/protocol-test.exe")], check=True, env=env)
        except OSError as error:
            if error.winerror != 4551:
                raise
            print("Windows Application Control (4551) blocked the C++ test. Native runtime remains unverified.")
            return work / "Release"
    subprocess.run(["ctest", "--test-dir", str(work), "-C", "Release", "--output-on-failure"], check=True)
    return work / "Release"


def portable_archive(path, runtime, launcher=None, shell=None, sdk=None):
    expected_python = tuple(map(int, PYTHON_VERSION.split(".")))
    if sys.platform != "win32" or sys.version_info[:3] != expected_python or sys.maxsize < 2**32:
        raise SystemExit(f"The portable build requires 64-bit CPython {PYTHON_VERSION} on Windows.")
    if not runtime.exists():
        runtime.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{EMBED}", timeout=90) as response:
            content = response.read()
        if sha256(content).hexdigest() != EMBED_SHA256:
            raise SystemExit("Контрольная сумма дистрибутива Python не совпала.")
        runtime.write_bytes(content)
    if sha256(runtime.read_bytes()).hexdigest() != EMBED_SHA256:
        raise SystemExit("Проверьте SHA-256 дистрибутива Python.")
    expected = dict(line.split("==") for line in REQUIREMENTS.read_text().splitlines() if "==" in line)
    expected = {canonicalize_name(name): version for name, version in expected.items()}
    distributions = [distribution(name) for name in PACKAGES]
    for name, installed in zip(PACKAGES, distributions):
        if installed.version != expected[canonicalize_name(name)]:
            raise SystemExit(f"Установите версию {name} из {REQUIREMENTS.name}.")
    with ZipFile(path, "w") as package:
        if launcher:
            archive_file(package, "gTR.exe", launcher)
        if shell:
            qt = "runtime/Lib/site-packages/PySide6/"
            archive_file(package, qt + "gTR-shell.exe", shell)
            archive_entry(package, qt + "qt.conf", io.BytesIO(b"[Paths]\nPrefix=.\nPlugins=plugins\nTranslations=translations\n"))
            for source, name in QT_RUNTIME:
                archive_file(package, qt + name, sdk / source)
        shippable = {
            entry.as_posix()
            for installed in distributions
            for entry in installed.files
        }
        for file in sorted(app_files()):
            archive_file(package, file.relative_to(ROOT).as_posix(), file)
        with ZipFile(runtime) as python:
            for entry in sorted(python.infolist(), key=lambda item: item.filename):
                if entry.filename != "python314._pth":
                    with python.open(entry) as source:
                        archive_entry(package, "runtime/" + entry.filename, source)
                    if (shell and entry.filename in {"vcruntime140.dll", "vcruntime140_1.dll"}
                            and "PySide6/" + entry.filename not in shippable):
                        with python.open(entry) as source:
                            archive_entry(package, qt + entry.filename, source)
        archive_entry(
            package,
            "runtime/python314._pth",
            io.BytesIO(b"python314.zip\n.\n..\nLib/site-packages\nimport site\n"),
        )
        for installed in distributions:
            for entry in sorted(installed.files, key=lambda item: item.as_posix()):
                if ".." in entry.parts or "__pycache__" in entry.parts or entry.suffix == ".pyc":
                    continue
                if entry.parts[:2] == ("PySide6", "qml"):
                    # gTR uses Qt Widgets. The wheel's QML tree also includes long build-only paths.
                    continue
                if entry.parent.name.endswith(".dist-info") and entry.name in LOCAL_METADATA:
                    continue
                file = Path(entry.locate())
                if file.is_file():
                    archive_file(package, "runtime/Lib/site-packages/" + entry.as_posix(), file)


def source_patch(baseline, source, output):
    if not baseline.is_file() or sha256(baseline.read_bytes()).hexdigest() != BASELINE_SHA:
        raise ValueError("Provide the original gTR 1.0.1 source archive with --baseline-source.")
    with ZipFile(baseline) as old, ZipFile(source) as new, output.open("wb") as patch:
        before = {n.removeprefix(BASELINE + "/"): n for n in old.namelist()}
        after = {n.removeprefix(PREFIX): n for n in new.namelist()}
        for path in sorted(before.keys() | after.keys()):
            a = old.read(before[path]) if path in before else b""
            b = new.read(after[path]) if path in after else b""
            if a == b:
                continue
            if b"\0" in a or b"\0" in b:
                raise ValueError(f"Binary change requires a binary patch: {path}")
            lines = difflib.diff_bytes(
                difflib.unified_diff,
                a.splitlines(True),
                b.splitlines(True),
                ("a/" + path if path in before else "/dev/null").encode(),
                ("b/" + path if path in after else "/dev/null").encode(),
            )
            for line in lines:
                patch.write(line if line.endswith(b"\n") else line + b"\n\\ No newline at end of file\n")


def checksums(directory, output, *, include_patch=True):
    lines = []
    for suffix in ("-Windows-x64.zip", "-source.zip", ".patch"):
        if suffix == ".patch" and not include_patch:
            continue
        artifact = directory / f"{NAME}{suffix}"
        if artifact.is_file():
            with artifact.open("rb") as source:
                lines.append(f"{file_digest(source, 'sha256').hexdigest()}  {artifact.name}\n")
    output.write_text("".join(lines), encoding="ascii", newline="\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    only = parser.add_mutually_exclusive_group()
    only.add_argument("--source-only", action="store_true")
    only.add_argument("--portable-only", action="store_true")
    parser.add_argument("--python-embed", type=Path, default=ROOT / "build" / EMBED)
    parser.add_argument("--baseline-source", type=Path, help="Create a patch against the specified legacy ZIP")
    parser.add_argument("--archives-only", action="store_true", help="Build ZIPs without generating a baseline patch")
    parser.add_argument("--without-exe", action="store_true")
    parser.add_argument("--qt-sdk", type=Path, default=Path(os.environ.get("GTR_QT_SDK") or ROOT / "build/beta6-qt-sdk"))
    parser.add_argument("--allow-blocked-native-test", action="store_true",
                        help="Build local archives if Windows Application Control blocks the test executable")
    parser.add_argument("--sign-tool", type=Path)
    parser.add_argument("--certificate-thumbprint")
    parser.add_argument("--timestamp-url")
    args = parser.parse_args()
    make_patch = args.baseline_source is not None and not args.archives_only and not args.portable_only
    signing = (args.sign_tool, args.certificate_thumbprint, args.timestamp_url)
    if any(signing):
        if not all(signing) or args.source_only or args.without_exe:
            parser.error("Signing requires all three signing options and a Windows launcher build.")
        url = urlsplit(args.timestamp_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or not re.fullmatch(r"[0-9a-fA-F]{40}", args.certificate_thumbprint)
        ):
            parser.error("Use an HTTPS timestamp service and a 40-character certificate thumbprint.")
    RELEASE.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=RELEASE) as tmp:
        if not args.source_only:
            portable = Path(tmp) / f"{NAME}-Windows-x64.zip"
            launcher = Path(tmp) / "gTR.exe" if not args.without_exe else None
            shell = None
            if launcher:
                compiled = build_native(args.qt_sdk, allow_blocked_test=args.allow_blocked_native_test)
                shutil.copyfile(compiled / "gTR.exe", launcher)
                shell = Path(tmp) / "gTR-shell.exe"
                shutil.copyfile(compiled / "gTR-shell.exe", shell)
                if all(signing):
                    sign_launcher(launcher, *signing)
                    sign_launcher(shell, *signing)
            portable_archive(portable, args.python_embed, launcher, shell, args.qt_sdk)
        if not args.portable_only:
            source = Path(tmp) / f"{NAME}-source.zip"
            source_archive(source)
            if make_patch:
                patch = Path(tmp) / f"{NAME}.patch"
                source_patch(args.baseline_source, source, patch)
        if not args.source_only:
            portable.replace(RELEASE / portable.name)
        if not args.portable_only:
            source.replace(RELEASE / source.name)
            if make_patch:
                patch.replace(RELEASE / patch.name)
        sums = Path(tmp) / f"{NAME}-SHA256SUMS.txt"
        checksums(RELEASE, sums, include_patch=make_patch)
        sums.replace(RELEASE / sums.name)
