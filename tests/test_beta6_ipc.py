import json
import subprocess
import sys
from contextlib import contextmanager
from queue import Queue
from threading import Thread

from conftest import message, write_json

from gtr.files import digest
from gtr.fixtures import _png
from gtr.ipc import MAX_FRAME, VERSION


@contextmanager
def service():
    bootstrap = """
import runpy
import socket
import sys

def block_network(event, args):
    if event in {"socket.connect", "socket.bind", "socket.getaddrinfo"}:
        raise AssertionError("network disabled")
    if event == "socket.__new__" and args[1] in (socket.AF_INET, socket.AF_INET6):
        raise AssertionError("network disabled")

sys.addaudithook(block_network)
runpy.run_module("gtr.ipc", run_name="__main__")
"""
    with subprocess.Popen(
        [sys.executable, "-X", "utf8", "-u", "-c", bootstrap],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as worker:
        packets = Queue()
        sequence = 0

        def reader():
            for line in worker.stdout:
                packets.put(json.loads(line))
            packets.put(None)

        Thread(target=reader, daemon=True).start()

        def begin(method, params=None, version=VERSION):
            nonlocal sequence
            sequence += 1
            worker.stdin.write(json.dumps({
                "v": version, "id": sequence, "method": method, "params": params or {},
            }).encode("ascii") + b"\n")
            worker.stdin.flush()
            return sequence

        def call(method, params=None, version=VERSION):
            request = begin(method, params, version)
            while packet := packets.get(timeout=20):
                assert packet["id"] == request
                if "event" not in packet:
                    return packet.get("result") if "result" in packet else packet["error"]
            raise AssertionError("Service exited unexpectedly")

        try:
            yield worker, begin, call, packets
        finally:
            worker.stdin.close()
            try:
                worker.wait(timeout=4)
            except subprocess.TimeoutExpired:
                worker.kill()


def test_ipc_schema_and_archive_to_verified_package(tmp_path):
    source = write_json(tmp_path / "synthetic", [
        message(1, "Оригинал", photo="image.png"),
        message(2, "Привіт", reply_to_message_id=1,
                forwarded_from="Исторический автор", forwarded_from_hidden=True),
    ])
    (source / "image.png").write_bytes(_png())
    before = digest(source / "image.png")
    with service() as (_, _, call, _):
        assert call("project.list") == {"code": "HANDSHAKE_REQUIRED"}
        assert call("hello", version=900) == {"code": "VERSION_MISMATCH"}
        assert call("hello")["protocol"] == VERSION
        assert call("project.list", {"token": "not a credential"}) == {"code": "INVALID_PARAMS"}
        assert call("unknown") == {"code": "UNKNOWN_METHOD"}
        assert call("update.install", {"path": str(source), "confirmed": False}) == {"code": "INVALID_PARAMS"}
        assert call("project.list") == {"projects": [], "saved_session": False}
        assert call("operation.list") == {"operations": []}
        recorded = call("archive.analyze", {"source": str(source)})
        project = recorded["path"]
        assert recorded["summary"]["messages"] == 2
        assert recorded["summary"]["unique_media_files"] == 1
        assert call("project.open", {"project": project}) == recorded
        params = {"project": project, "chat": 1}
        latest = call("chat.page", {**params, "limit": 1})
        assert latest["before"] == 2
        assert latest["records"][0]["reply"] == 1
        assert latest["records"][0]["forward_privacy"] == "hidden"
        older = call("chat.page", {**params, "before": latest["before"]})
        assert len(older["records"]) == 1 and older["records"][0]["id"] == 1
        assert len(call("chat.page", {**params, "query": "Привіт"})["records"]) == 1
        asset = older["records"][0]["attachment"]
        facts = call("media.inspect", {"project": project, "asset": asset})
        assert facts["image"] and facts["mime"] == "image/png" and "sha256" not in facts
        choices = {**params, "notes": "compact", "language": "uk"}
        assert call("recovery.plan", choices)["summary"]["included"] == 2
        assert call("recovery.prepare", {**choices, "confirmed": False}) == {"code": "INVALID_PARAMS"}
        created = call("recovery.prepare", {**choices, "confirmed": True})
        assert created["summary"]["included"] == 2
        assert call("verify.local", {"project": project, "bundle": created["path"]})["status"] == "verified"
        assert call("storage.info")["originals"] > 0
        assert call("cache.clear", {"confirmed": False}) == {"code": "INVALID_PARAMS"}
        assert call("cancel", {"target": 1}) == {"cancelling": False}
    assert digest(source / "image.png") == before


def test_ipc_rejects_duplicate_keys_and_limits_frames():
    with service() as (worker, _, call, packets):
        assert call("hello")["protocol"] == VERSION
        worker.stdin.write(b'{"v":1,"id":2,"id":3,"method":"project.list","params":{}}\n')
        worker.stdin.flush()
        assert packets.get(timeout=10)["error"]["code"] == "INVALID_REQUEST"
        worker.stdin.write(b" " * (MAX_FRAME + 2))
        worker.stdin.flush()
        assert packets.get(timeout=10)["error"]["code"] == "FRAME_LIMIT"
        assert worker.wait(timeout=10) == 2


def test_interrupted_worker_keeps_existing_project_and_can_restart(project, tmp_path):
    source = write_json(tmp_path / "other", [message(n, "Архив" * 100) for n in range(1, 6501)])
    original = project / "history.sqlite"
    before = digest(original)
    with service() as (worker, begin, call, packets):
        call("hello")
        target = begin("archive.analyze", {"source": str(source)})
        event = packets.get(timeout=10)
        assert event["id"] == target and event["event"] == "progress"
        worker.kill()
        assert worker.wait(timeout=10) != 0
    assert digest(original) == before
    with service() as (_, _, call, _):
        call("hello")
        assert call("project.list")["projects"] == []
        assert call("project.open", {"project": str(project)})["summary"]["messages"] == 10
        assert call("operation.list")["operations"] == []
    assert (source / "result.json").is_file()
