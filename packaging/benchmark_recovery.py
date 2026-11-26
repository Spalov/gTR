"""Measure local recovery stages on generated data in disposable directories."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, process_time

SIZES = (10, 100, 233, 1_000, 6_000, 10_000, 50_000, 100_000)


def disk(root):
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def peak_rss():
    if sys.platform == "win32":
        import win32api
        import win32process

        return win32process.GetProcessMemoryInfo(win32api.GetCurrentProcess())["PeakWorkingSetSize"]
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)


def run(count, media=False, code=None):
    startup = perf_counter()
    cpu = process_time()
    sys.path.insert(0, str(code or Path(__file__).resolve().parents[1]))
    from gtr import __version__
    from gtr.conversion import load_bundle, plan_import, prepare
    from gtr.core import analyze, catalogue, summary
    from gtr.integrity import check_local
    startup = perf_counter() - startup

    with TemporaryDirectory(prefix="gtr-synthetic-benchmark-") as temporary:
        root = Path(temporary)
        source = root / "export"
        source.mkdir()
        if media:
            (source / "documents").mkdir()
            for index in range(count):
                path = source / "documents" / f"test-{index:02}.pdf"
                with path.open("wb") as output:
                    output.write(b"%PDF-1.7\n")
                    output.write(bytes([index]) * 2 * 1024**2)
        with (source / "result.json").open("w", encoding="utf-8") as out:
            out.write('{"messages":[')
            for index in range(count):
                row = {
                    "id": index + 1, "type": "message", "from": "Тестовый автор",
                    "from_id": "user1234", "date": "2025-02-03T07:44:02+02:00",
                    "text": f"Синтетическое сообщение {index + 1:06d} 🌱",
                }
                if index:
                    row["reply_to_message_id"] = index
                if media:
                    row["file"] = f"documents/test-{index:02}.pdf"
                out.write((",\n" if index else "") + json.dumps(row, ensure_ascii=False))
            out.write('],"id":42,"type":"personal_chat","name":"Синтетическое испытание"}')
        input_bytes = disk(source)
        started = perf_counter()
        project = analyze(source, root / "project")
        catalog_seconds = perf_counter() - started
        assert summary(project)["messages"] == count
        started = perf_counter()
        with catalogue(project) as db:
            rows = db.execute(
                "SELECT id FROM messages WHERE chat=1 AND instr(text, ?) > 0 ORDER BY id DESC LIMIT 80",
                ("000001",),
            ).fetchall()
        assert len(rows) == 1
        search = perf_counter() - started
        started = perf_counter()
        plan = plan_import(project, 1)
        assert plan["included"] == count
        del plan
        plan_seconds = perf_counter() - started
        started = perf_counter()
        bundle = prepare(project, 1, root / "package")
        assert bundle.data["included"] == count
        package_seconds = perf_counter() - started
        del bundle
        started = perf_counter()
        prepared = load_bundle(root / "package")
        assert check_local(prepared, project)["status"] == "verified"
        verify_seconds = perf_counter() - started
        return {
            "release": __version__, "records": count, "attachments": count if media else 0,
            "startup_s": round(startup, 3), "search_ms": round(search * 1000, 2),
            "cpu_s": round(process_time() - cpu, 2),
            "analyze_s": round(catalog_seconds, 2), "plan_s": round(plan_seconds, 2),
            "package_s": round(package_seconds, 2), "verify_s": round(verify_seconds, 2),
            "peak_mib": round(peak_rss() / 1024**2, 1), "input_mib": round(input_bytes / 1024**2, 2),
            "workspace_mib": round(disk(root) / 1024**2, 2),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, choices=(*SIZES, 32))
    parser.add_argument("--with-media", action="store_true")
    parser.add_argument("--code", type=Path, help="Source tree to benchmark")
    args = parser.parse_args()
    if args.size is not None:
        print(json.dumps(run(args.size, args.with_media, args.code)), flush=True)
        return
    for size in SIZES + (32,):
        flags = ["--with-media"] if size == 32 else []
        output = subprocess.check_output(
            [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--size", str(size), *flags],
        )
        print(output.decode("utf-8").strip(), flush=True)


if __name__ == "__main__":
    main()
