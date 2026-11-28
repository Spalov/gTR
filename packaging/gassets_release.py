"""Build offline GSU packages, signed release metadata and the Windows update center."""

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from build_release import (
    APPLICATION_CONTROL_ERRORS,
    APPLICATION_CONTROL_EXIT,
    FILE_VERSION,
    NAME,
    PORTABLE_PROGRAMS,
    RELEASE,
    ROOT,
    Authenticode,
)

SOURCE = ROOT / "gassets"
MAX_FILE = 384 * 1024**2
MAX_EXPANDED = 2 * 1024**3
PRODUCT = "gassets.gtr"
REPO = "Spalov/gTR"
GSU_NAME = f"{NAME}-Windows-x64.gsu"
SETUP_NAME = f"{NAME}-Setup-Windows-x64.exe"
REFERENCES = (
    "mscorlib", "System", "System.Core", "System.Drawing", "System.Windows.Forms",
    "System.Net.Http", "System.IO.Compression",
    "System.Web.Extensions", "Microsoft.CSharp",
)
CHANGELOG = {
    "ru": ["Редактор выборки архива", "Единая оболочка C++", "Установка и обновления через gAssets"],
    "uk": ["Редактор вибірки архіву", "Єдина оболонка C++", "Встановлення й оновлення через gAssets"],
    "en": ["Archive selection editor", "Unified C++ shell", "Installation and updates through gAssets"],
}


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def openssl():
    found = shutil.which("openssl")
    if found:
        return Path(found)
    git = shutil.which("git")
    if git:
        candidate = Path(git).resolve().parents[1] / "mingw64" / "bin" / "openssl.exe"
        if candidate.is_file():
            return candidate
    raise SystemExit("Нужен OpenSSL с поддержкой ECDSA P-256.")


def command(exe, *args, input_bytes=None):
    return subprocess.run([str(exe), *map(str, args)], input=input_bytes, stdout=subprocess.PIPE, check=True).stdout


def public_blob(exe, key):
    der = command(exe, "pkey", "-in", key, "-outform", "DER", "-pubout")
    prefix = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d03010703420004")
    if len(der) != 91 or not der.startswith(prefix):
        raise ValueError("Ключ должен быть ECDSA P-256 с несжатой открытой точкой.")
    return struct.pack("<II", 0x31534345, 32) + der[-64:]


def sign(exe, key, data):
    der = command(exe, "dgst", "-sha256", "-sign", key, input_bytes=data)
    if len(der) < 8 or der[0] != 0x30 or der[1] != len(der) - 2:
        raise ValueError("Некорректная подпись OpenSSL.")
    values, pos = [], 2
    for _ in range(2):
        if der[pos] != 2:
            raise ValueError("Некорректная подпись OpenSSL.")
        count = der[pos + 1]
        start = pos + 2
        pos = start + count
        values.append(int.from_bytes(der[start:pos], "big").to_bytes(32, "big"))
    if pos != len(der):
        raise ValueError("Некорректная подпись OpenSSL.")
    return b"".join(values)


def sha_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def entry(out, name, source):
    info = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.compress_level = 3
    info.external_attr = 0o100644 << 16
    with out.open(info, "w") as target:
        shutil.copyfileobj(source, target, length=256 * 1024)


def safe_payload(name):
    return len(name) <= 230 and all(
        re.fullmatch(r"[A-Za-z0-9_+. -]{1,150}", part)
        and not part.endswith((".", " "))
        and not re.match(r"(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)", part, re.IGNORECASE)
        for part in name.split("/")
    ) and not name.lower().startswith(".gassets-")


def pack(portable, destination, exe, key, publisher):
    version = {"channel": "beta", "number": FILE_VERSION[0], "fix": FILE_VERSION[2], "build": sha_file(portable)}
    total = 0
    files = []
    seen = set()
    with ZipFile(portable) as source, ZipFile(destination, "x", allowZip64=False) as out:
        for item in source.infolist():
            if item.is_dir():
                continue
            prefix = NAME + "/"
            if not item.filename.startswith(prefix):
                raise ValueError("Неожиданное расположение файла в портативном комплекте.")
            name = item.filename.removeprefix(prefix)
            if not safe_payload(name) or name.casefold() in seen or item.file_size > MAX_FILE:
                raise ValueError(f"Недопустимое имя или размер файла поставки: {name[:100]}")
            seen.add(name.casefold())
            total += item.file_size
            if total > MAX_EXPANDED or len(seen) > 11998:
                raise ValueError("Поставляемый комплект превышает ограничения GSU 1.")
            with source.open(item) as stream:
                sha = hashlib.file_digest(stream, "sha256").hexdigest()
            with source.open(item) as stream:
                entry(out, "payload/" + name, stream)
            files.append({"path": name, "size": item.file_size, "sha256": sha})
        if "gtr.exe" not in seen:
            raise ValueError("Для GSU необходим готовый комплект Windows x64 с gTR.exe.")
        manifest = {
            "schema_version": 1, "product_id": PRODUCT, "package_id": version["build"],
            "version": version, "platform": "windows", "architecture": "x64", "entry": "gTR.exe",
            "kind": "full", "min_gau": 1, "windows_build": 17763, "data_schema": 1,
            "disk_bytes": total + 64 * 1024**2, "user_data": "preserve",
            "files": files, "signing_key_id": publisher,
        }
        data = json_bytes(manifest)
        out.writestr("manifest.json", data)
        out.writestr("manifest.sig", sign(exe, key, data))
    return manifest


def metadata(package, manifest, *, ids=None, tag=None, sequence=1, published=None):
    now = datetime.now(UTC).replace(microsecond=0)
    published = published or now
    info = {
        "schema_version": 1, "product_id": PRODUCT, "product_name": "gTR", "repository": REPO,
        "version": manifest["version"], "platform": "windows", "architecture": "x64",
        "min_gau": 1, "windows_build": manifest["windows_build"], "disk_bytes": manifest["disk_bytes"],
        "sequence": sequence, "published_at": published.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (published + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "delivery": "github" if ids else "local",
        "package": {"name": package.name, "size_bytes": package.stat().st_size, "sha256": sha_file(package)},
        "changelog": CHANGELOG, "signing_key_id": manifest["signing_key_id"],
    }
    if ids:
        info["github_tag"], info["github_release_id"] = tag, ids[0]
        info["package"]["asset_id"] = ids[1]
    return info


def write_metadata(info, destination, exe, key):
    data = json_bytes(info)
    (destination / "gassets-release.json").write_bytes(data)
    (destination / "gassets-release.sig").write_bytes(sign(exe, key, data))


def csharp():
    vswhere = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft Visual Studio/Installer/vswhere.exe"
    found = subprocess.check_output([str(vswhere), "-latest", "-products", "*", "-find",
                                     "MSBuild/Current/Bin/Roslyn/csc.exe"], text=True).splitlines()
    if not found:
        raise SystemExit("Нужны Visual Studio Build Tools с компилятором C# и .NET Framework 4.8 SDK.")
    return Path(found[0])


def compile_center(stage, *, test, files=None, console=False, output="gAssets.exe", resource=None):
    refs = Path(os.environ["ProgramFiles(x86)"]) / "Reference Assemblies/Microsoft/Framework/.NETFramework/v4.8"
    args = [
        "/nologo", "/utf8output", "/nostdlib+", "/noconfig", "/w:4", "/optimize+", "/platform:x64",
        "/langversion:latest", "/target:exe" if console else "/target:winexe", "/deterministic+",
        f"/out:{stage / output}", f"/win32manifest:{SOURCE / 'app.manifest'}",
        f"/win32icon:{stage / 'gassets.ico'}",
        *[f"/reference:{refs / (lib + '.dll')}" for lib in REFERENCES],
    ]
    if resource:
        args.append(f"/resource:{resource},gAssets.Publisher")
    if test:
        args.append("/define:LOCAL_TEST")
    subprocess.run([str(csharp()), *args, *(str(file) for file in (files or sorted(SOURCE.glob("*.cs"))))], check=True)
    shutil.copyfile(SOURCE / "gAssets.exe.config", stage / (output + ".config"))


def icons(stage):
    # Stable flat artwork, separate from the original gTR cat.
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import (
        QColor,
        QGuiApplication,
        QIcon,
        QImage,
        QPainter,
        QPen,
        QPixmap,
        QPolygonF,
    )

    app = QGuiApplication.instance() or QGuiApplication(["gAssets artwork", "-platform", "offscreen"])
    for name, package in (("gassets", False), ("gsu", True)):
        image = QImage(256, 256, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        paint = QPainter(image)
        paint.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint.setPen(Qt.PenStyle.NoPen)
        paint.setBrush(QColor("#15272f"))
        paint.drawRoundedRect(12, 12, 232, 232, 49, 49)
        if package:
            paint.setBrush(QColor("#89d9e8"))
            paint.drawPolygon(QPolygonF([QPointF(x, y) for x, y in
                                        ((53, 78), (128, 42), (203, 78), (203, 172), (128, 211), (53, 172))]))
            paint.setPen(QPen(QColor("#15272f"), 9))
            paint.drawLine(128, 116, 128, 206)
            paint.drawLine(58, 82, 128, 116)
            paint.drawLine(128, 116, 198, 82)
        else:
            for x, y, color in ((55, 58, "#8be4ee"), (132, 58, "#508ea6"), (55, 136, "#508ea6")):
                paint.setBrush(QColor(color))
                paint.drawRoundedRect(x, y, 64, 64, 13, 13)
            pen = QPen(QColor("#efc078"), 12)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            paint.setPen(pen)
            paint.drawLine(139, 169, 160, 191)
            paint.drawLine(160, 191, 204, 144)
        paint.end()
        frames = []
        for side in (16, 24, 32, 48, 64, 128, 256):
            from PySide6.QtCore import QBuffer, QIODevice

            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation).save(buffer, "PNG")
            frames.append((side, bytes(buffer.data())))
        offset = 6 + 16 * len(frames)
        directory = bytearray(struct.pack("<HHH", 0, 1, len(frames)))
        for side, frame in frames:
            directory += struct.pack("<BBBBHHII", side % 256, side % 256, 0, 0, 1, 32, len(frame), offset)
            offset += len(frame)
        (stage / (name + ".ico")).write_bytes(directory + b"".join(frame for _, frame in frames))
        # Exercise icon parsing in the same runtime used to render the assets.
        if QIcon(QPixmap(str(stage / (name + ".ico")))).isNull():
            raise ValueError("Неверный значок Windows.")
    return app


def verify_portable(archive_path, stage, signing):
    with ZipFile(archive_path) as archive:
        for relative in PORTABLE_PROGRAMS:
            checked = stage / Path(relative).name
            try:
                with archive.open(f"{NAME}/{relative}") as source, checked.open("xb") as target:
                    shutil.copyfileobj(source, target)
                signing.verify(checked)
            except (KeyError, subprocess.CalledProcessError):
                raise SystemExit(f"Windows ZIP: требуется действительная подпись с отметкой времени для {checked.name}.") from None
            finally:
                checked.unlink(missing_ok=True)


def verify_update_center(stage, package):
    """Run the exact updater with its embedded trust key and the bundled GSU."""
    with TemporaryDirectory(prefix="install-check-", dir=stage) as scratch:
        env = {**os.environ, "TEMP": scratch, "TMP": scratch, "LOCALAPPDATA": scratch, "QT_QPA_PLATFORM": "windows"}
        try:
            result = subprocess.run([str(stage / "gAssets.exe"), "--verify-install", str(package)], cwd=stage,
                                    env=env, check=False, timeout=120)
        except OSError as error:
            if getattr(error, "winerror", None) not in APPLICATION_CONTROL_ERRORS:
                raise
            raise SystemExit("Windows Application Control blocked gAssets before installation started.") from None
    if result.returncode == APPLICATION_CONTROL_EXIT:
        raise SystemExit("Windows Application Control blocked gTR during the gAssets installation check.")
    result.check_returncode()
    print("Bundled gAssets installed and verified gTR in a temporary profile.")


def build(args, stage, exe, key, public, signing, portable):
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    stage_key = stage / "publisher-public.blob"
    stage_key.write_bytes(public)
    publisher = hashlib.sha256(public).hexdigest()
    package = stage / GSU_NAME
    manifest = pack(portable, package, exe, key, publisher)
    write_metadata(metadata(package, manifest, sequence=args.sequence), stage, exe, key)
    catalog = json_bytes({
        "schema_version": 1, "sequence": args.sequence, "signing_key_id": publisher,
        "expires_at": (datetime.now(UTC) + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "products": [{"product_id": PRODUCT, "name": "gTR", "repository": REPO,
                      "platforms": ["windows-x64"], "channels": ["stable", "rc", "beta"],
                      "package_format": "GSU-1", "entry": "gTR.exe", "folder": "gTR",
                      "signing_key_id": publisher}],
    })
    (stage / "gassets-catalog.json").write_bytes(catalog)
    (stage / "gassets-catalog.sig").write_bytes(sign(exe, key, catalog))
    gui = icons(stage)
    compile_center(stage, test=args.test, resource=stage_key)
    if signing:
        signing.sign(stage / "gAssets.exe")
        verify_update_center(stage, package)
    text = ("gAssets Update Center and GSU specification\nCopyright © 2026 Gruzin\n"
            "Separate licensing remains subject to the author's decision.\n"
            + ("Local test distribution. This key is for the isolated test kit only.\n" if args.test else ""))
    (stage / "TERMS.txt").write_text(text, encoding="utf-8")
    center = stage / "gAssets-UpdateCenter-Windows-x64.zip"
    with ZipFile(center, "x", compression=ZIP_DEFLATED) as archive:
        for name in ("gAssets.exe", "gAssets.exe.config", "gassets.ico", "gsu.ico", "TERMS.txt",
                     "gassets-catalog.json", "gassets-catalog.sig"):
            archive.write(stage / name, name)
    compiler = args.iscc or Path(os.environ["ProgramFiles(x86)"]) / "Inno Setup 6/ISCC.exe"
    setup = stage / SETUP_NAME
    if not args.no_setup:
        signing_flags = []
        if signing:
            command_line = subprocess.list2cmdline(signing.sign_command()).replace("$", "$$").replace('"', "$q")
            signing_flags = ["/DUseAuthenticode=1", f"/Sgtrpublisher={command_line} $f"]
        subprocess.run([str(compiler), "/Qp", f"/DOutput={stage}", f"/DInput={stage}",
                        f"/DVersion={NAME.removeprefix('gTR-Beta-')}", *(["/DLocalTest=1"] if args.test else []),
                        *signing_flags, str(SOURCE / "setup.iss")], check=True)
        if signing:
            signing.verify(setup)
    del gui
    names = [GSU_NAME, center.name, "gassets-release.json", "gassets-release.sig",
             "gassets-catalog.json", "gassets-catalog.sig"]
    if not args.no_setup:
        names.append(setup.name)
    for name in names:
        shutil.copyfile(stage / name, out / name)
    if args.test:
        (out / "LOCAL-TEST.txt").write_text(text, encoding="utf-8")
    names += [path.name for path in (out / f"{NAME}-Windows-x64.zip", out / f"{NAME}-source.zip") if path.is_file()]
    sums = "".join(f"{sha_file(out / name)}  {name}\n" for name in sorted(names))
    (out / f"{NAME}-SHA256SUMS.txt").write_text(sums, encoding="ascii")
    print(f"Built {len(names)} artifacts. Test trust: {args.test}. Entries: {len(manifest['files'])}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portable", type=Path, default=RELEASE / f"{NAME}-Windows-x64.zip")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, default=ROOT / "build")
    parser.add_argument("--iscc", type=Path)
    parser.add_argument("--no-setup", action="store_true")
    parser.add_argument("--sequence", type=int, default=1)
    parser.add_argument("--test", action="store_true", help="Isolated Windows settings and a temporary test key")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--release-ids", type=int, nargs=2, metavar=("RELEASE", "ASSET"))
    parser.add_argument("--tag")
    parser.add_argument("--published-at")
    parser.add_argument("--package", type=Path)
    Authenticode.add_options(parser)
    args = parser.parse_args()
    if args.test == bool(args.key_file) or args.sequence < 1:
        parser.error("Укажите --test или --key-file и положительный порядковый номер.")
    signing = Authenticode.from_options(parser, args)
    if signing and args.release_ids:
        parser.error("Authenticode signing is available when building the Windows binaries.")
    if not args.test and not args.release_ids and not signing:
        parser.error("A public Windows bundle requires Authenticode. Use --test for an isolated local build.")
    exe = openssl()
    with TemporaryDirectory(prefix="gassets-release-", dir=args.scratch) as root:
        stage = Path(root)
        portable = args.portable.resolve()
        if signing:
            frozen_portable = stage / "portable-input.zip"
            shutil.copyfile(portable, frozen_portable)
            verify_portable(frozen_portable, stage, signing)
            portable = frozen_portable
        key = args.key_file
        if args.test:
            key = stage / "test-key.pem"
            command(exe, "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", key)
        public = public_blob(exe, key)
        if args.release_ids:
            if args.test or not args.tag or args.package is None or not args.published_at or min(args.release_ids) < 1:
                parser.error("Для GitHub нужны рабочий ключ, --package, --tag, --published-at и реальные идентификаторы.")
            with ZipFile(args.package) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            if manifest["signing_key_id"] != hashlib.sha256(public).hexdigest() or manifest["product_id"] != PRODUCT:
                raise ValueError("Пакет подписан для другого издателя или продукта.")
            published = datetime.fromisoformat(args.published_at)
            if published.tzinfo is None:
                parser.error("--published-at должен включать часовой пояс.")
            info = metadata(args.package, manifest, ids=args.release_ids, tag=args.tag,
                            sequence=args.sequence, published=published.astimezone(UTC))
            args.output.mkdir(parents=True, exist_ok=True)
            write_metadata(info, args.output, exe, key)
        else:
            build(args, stage, exe, key, public, signing, portable)


if __name__ == "__main__":
    main()
