"""Exercise GAU, GSU and the GitHub transport double in disposable local directories."""

import argparse
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from build_release import (
    APPLICATION_CONTROL_ERRORS,
    APPLICATION_CONTROL_EXIT,
    Authenticode,
)
from gassets_release import GSU_NAME, SOURCE, compile_center, icons


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--kit", type=Path, help="Additionally install the exact local test kit into isolation")
    parser.add_argument("--public-feed", action="store_true", help="Read only the public GitHub releases feed")
    parser.add_argument("--allow-blocked-health", action="store_true", help="Report a blocked Windows launch as pending")
    Authenticode.add_options(parser)
    args = parser.parse_args()
    signing = Authenticode.from_options(parser, args)
    with TemporaryDirectory(prefix="gassets-qa-", dir=args.scratch) as root:
        stage = Path(root).resolve()
        test = "gau-tests.exe"
        application = icons(stage)
        compile_center(stage, test=True, files=[SOURCE / name for name in (
            "Gsu.cs", "Gau.cs", "GitHub.cs", "Catalog.cs", "tests/GauTests.cs",
        )], console=True, output=test)
        if signing:
            signing.sign(stage / test)
        flags = ["--public-feed"] if args.public_feed else []
        subprocess.run([str(stage / test), str(stage / "fixtures"), *flags], cwd=stage, check=True, timeout=90)
        del application
        if args.kit:
            kit = args.kit.resolve()
            center = stage / "center"
            with ZipFile(kit / "gAssets-UpdateCenter-Windows-x64.zip") as archive:
                for name in ("gAssets.exe", "gAssets.exe.config", "gassets-catalog.json", "gassets-catalog.sig"):
                    archive.extract(name, center)
            profile = stage / "profile"
            profile.mkdir()
            user = profile / "gAssets" / "gTR"
            user.mkdir(parents=True)
            marker = user / "synthetic-project.json"
            marker.write_bytes(b'{"synthetic": true}')
            executable = profile / "Programs" / "gAssets-Test" / "gTR" / "current" / "gTR.exe"
            blocked = None
            try:
                result = subprocess.run([str(center / "gAssets.exe"), "--first-install", str(kit / GSU_NAME),
                                         "--quiet", "--test-root", str(profile)], cwd=center, timeout=90, check=False)
                if result.returncode == APPLICATION_CONTROL_EXIT and args.allow_blocked_health:
                    blocked = "gTR during the installation health check"
                else:
                    result.check_returncode()
            except OSError as error:
                if not args.allow_blocked_health or getattr(error, "winerror", None) not in APPLICATION_CONTROL_ERRORS:
                    raise
                blocked = "gAssets before installation started"
            if blocked:
                if executable.exists() or marker.read_bytes() != b'{"synthetic": true}':
                    raise RuntimeError("Blocked launch left unexpected changes in the isolated test profile.")
                print(f"Windows Application Control blocked {blocked}. The installation check remains pending.", flush=True)
                return
            if not executable.is_file() or marker.read_bytes() != b'{"synthetic": true}':
                raise RuntimeError("Isolated app installation did not preserve the test project")
            subprocess.run([str(executable), "--health-check"], cwd=executable.parent, check=True, timeout=30)
            print("Exact local GSU installed. gTR IPC health check passed. User files retained.", flush=True)


if __name__ == "__main__":
    main()
