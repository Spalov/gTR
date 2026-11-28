"""Fix 2 package, disk-budget and diagnostics regressions without Telegram access."""

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import message, write_json
from PySide6.QtCore import QCoreApplication, QEvent
from test_universal_media_recovery import webp

from gtr import conversion, gui, updates
from gtr.conversion import load_bundle, prepare
from gtr.core import analyze, encode
from gtr.faults import ImportFault
from gtr.importer import ImportController
from gtr.incremental_import import recovery_root
from gtr.journal import Journal, State
from gtr.mock_api import MockAPI, offline_result
from gtr.package_errors import PackageValidationError
from gtr.server import OwnerSession


def sticker_project(tmp_path, *, messages=2, attachments=1):
    rows = [
        message(1, "Синтетическая подпись", file="synthetic.webp", media_type="sticker"),
        *(message(i, f"Тестовая запись {i}", **({"file": "report.pdf"} if i <= attachments else {}))
          for i in range(2, messages + 1)),
    ]
    source = write_json(tmp_path / "source", rows)
    (source / "synthetic.webp").write_bytes(webp())
    (source / "report.pdf").write_bytes(b"%PDF-1.7\n")
    return analyze(source, tmp_path / "project")


def test_312_messages_and_218_attachments_use_one_snapshot_and_retry_after_disk_failure(tmp_path, monkeypatch):
    project = sticker_project(tmp_path, messages=312, attachments=218)
    bundle = prepare(project, 1, tmp_path / "first", "source", video_policy="original")
    copied = load_bundle(bundle.root)
    assert copied.data["included"] == 312 and copied.data["media_count"] == 218
    assert copied.data["media"][0]["attributes"] == {"type": "sticker", "emoji": ""}
    require = (bundle.transcript.stat().st_size + len(encode(bundle.data).encode())
               + sum(item["size"] for item in bundle.data["media"]))
    registry = tmp_path / "registry"
    journal = Journal(registry / "imports.sqlite", "synthetic-target")
    old_tmp = tmp_path / "owner-temp"
    old_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(old_tmp))
    original_disk_usage = shutil.disk_usage
    budgets = []
    budget = require - 1

    def bounded_disk(path):
        result = original_disk_usage(path)
        if not (Path(path).is_relative_to(registry) or Path(path).is_relative_to(old_tmp)):
            return result
        allocated = sum(
            f.stat().st_size for active in (old_tmp, registry / "recovery")
            for f in active.rglob("*") if f.is_file()
        )
        budgets.append(allocated)
        return result._replace(free=max(0, budget - allocated))

    monkeypatch.setattr(conversion.shutil, "disk_usage", bounded_disk)
    blocked_api = MockAPI()
    blocked = ImportController(blocked_api, "synthetic-target", mode="owner", journal=journal)
    with pytest.raises(ImportFault) as rejection:
        offline_result(blocked.validate(bundle))
    assert rejection.value.code == "PACKAGE_STORAGE_INSUFFICIENT"
    failed = journal.operation(blocked.job)
    assert failed["state"] == State.FAILED and not failed["transcript_upload_done"] and not failed["start_request_sent"]
    assert not blocked_api.calls and not recovery_root(journal, bundle.manifest_hash()).exists()
    saved = journal.diagnostic_report(blocked.job, private=True)["package_issue"]
    assert (saved["stage"], saved["field"], saved["expected"], saved["actual"]) == (
        "copy_snapshot", "free_bytes", require, require - 1,
    )

    budget = require + 1024
    retry = prepare(project, 1, tmp_path / "retry", "source", video_policy="original")
    assert retry.manifest_hash() == bundle.manifest_hash()
    api = MockAPI()
    controller = ImportController(api, "synthetic-target", mode="owner", journal=journal)
    last = offline_result(controller.validate(retry))
    assert (last.messages, last.media) == (312, 218)
    assert controller.bundle.root == recovery_root(journal, retry.manifest_hash())
    assert not list(old_tmp.iterdir()) and budgets == [0, 0]
    assert api.calls == ["check_history", "check_peer"]
    assert journal.diagnostic_report(blocked.job, private=True)["package_issue"] == saved

    # Editing the original after review cannot change the protected copy.
    retry.media_path(retry.data["media"][0]).write_bytes(b"changed")
    assert load_bundle(controller.bundle.root).manifest_hash() == last.manifest_sha256
    # The durable copy is still reverified before even initializing the mock operation.
    controller.bundle.media_path(controller.bundle.data["media"][0]).unlink()
    with pytest.raises(ImportFault) as changed:
        offline_result(controller.execute(last, approved=True))
    assert changed.value.code == "FILE_NOT_FOUND" and api.calls == ["check_history", "check_peer"]
    assert journal.operation(controller.job)["start_request_sent"] is False


@pytest.mark.parametrize(("damage", "code", "field", "with_record"), [
    ("version", "UNSUPPORTED_PACKAGE_VERSION", "schema", False),
    ("count", "PACKAGE_SCHEMA_INVALID", "media_count", False),
    ("attributes", "MEDIA_ATTRIBUTES_INVALID", "attributes.emoji", True),
    ("hash", "MEDIA_HASH_MISMATCH", "sha256", True),
    ("size", "MEDIA_HASH_MISMATCH", "size", True),
    ("missing", "FILE_NOT_FOUND", "file", True),
    ("manifest", "FILE_NOT_FOUND", "file", False),
    ("text", "MANIFEST_MISMATCH", "text_sha256", False),
    ("binding", "ATTACHMENT_BINDING_INVALID", "attached", True),
])
def test_damage_reports_field_expected_actual_and_source(tmp_path, damage, code, field, with_record):
    project = sticker_project(tmp_path)
    bundle = prepare(project, 1, tmp_path / "package", "source", video_policy="original")
    manifest = bundle.root / "manifest.json"
    item = bundle.data["media"][0]
    attachment = bundle.media_path(item)
    if damage == "version":
        bundle.data["schema"] = 999
    elif damage == "count":
        bundle.data["media_count"] = 8
    elif damage == "attributes":
        del item["attributes"]["emoji"]
    elif damage == "hash":
        before = attachment.read_bytes()
        attachment.write_bytes(before[:-1] + bytes([before[-1] ^ 1]))
    elif damage == "size":
        with attachment.open("ab") as output:
            output.write(b"x")
    elif damage == "missing":
        attachment.unlink()
    elif damage == "manifest":
        manifest.unlink()
    elif damage == "text":
        bundle.transcript.write_bytes(b"Different synthetic history")
    elif damage == "binding":
        text = bundle.transcript.read_bytes().replace(item["wire_name"].encode(), b"renamed-sticker.webp", 1)
        bundle.transcript.write_bytes(text)
        bundle.data["text_sha256"] = hashlib.sha256(text).hexdigest()
    if damage != "manifest":
        manifest.write_text(encode(bundle.data), encoding="utf-8")
    with pytest.raises(PackageValidationError) as failure:
        load_bundle(bundle.root)
    private = failure.value.details(private=True)
    assert (private["code"], private["field"]) == (code, field)
    assert private["stage"] and private["file"] and private["action"] and private["expected"] is not None
    assert private["source_id"] == ("1" if with_record else None)
    if damage == "binding":
        assert private["expected"] == item["wire_name"] and private["actual"] == "renamed-sticker.webp"
    public = failure.value.details()
    assert "source_id" not in public and "file" not in public and "actual" not in public


def test_prepare_does_not_publish_package_that_its_validator_rejects(tmp_path, monkeypatch):
    project = sticker_project(tmp_path)
    real_bind = conversion.bind

    def false_binding(*args):
        real_bind(*args)
        args[3]["wire_name"] = "synthetic-mismatch.webp"

    monkeypatch.setattr(conversion, "bind", false_binding)
    dest = tmp_path / "unpublished"
    with pytest.raises(PackageValidationError) as error:
        prepare(project, 1, dest, "source", video_policy="original")
    assert error.value.code == "ATTACHMENT_BINDING_INVALID"
    assert not dest.exists()


def test_validation_diagnostic_can_be_saved_before_operation_id_exists(tmp_path, monkeypatch):
    app = gui.application()
    window = gui.Window()
    try:
        report = tmp_path / "diagnostic.json"
        error = PackageValidationError(
            "MEDIA_ATTRIBUTES_INVALID", "verify_media", "attributes.emoji", file="synthetic.webp",
            record={"source_id": "202"}, expected="строка", actual="число",
        )
        monkeypatch.setattr(gui.QInputDialog, "getItem", lambda *args: (args[3][1], True))
        monkeypatch.setattr(gui.QFileDialog, "getSaveFileName", lambda *args: (str(report), ""))
        window.telegram._failure("preflight", error)
        assert window.telegram.operation_id is None and window.telegram.diagnostic_button.isEnabled()
        assert "202" in window.telegram.details.text()
        window.telegram.diagnostic_button.click()
        evidence = json.loads(report.read_text(encoding="utf-8"))
        assert evidence["operation"] is None and evidence["redacted"] is False
        assert evidence["package_issue"] == error.details(private=True)
        app.processEvents()
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_corrupt_package_leaves_a_diagnostic_before_any_server_checks(tmp_path):
    journal = Journal(tmp_path / "imports.sqlite", "synthetic-target")

    def no_api(*args, **kwargs):
        pytest.fail("Corrupt packages must be refused before creating a Telegram adapter")

    owner = OwnerSession(manager=SimpleNamespace(account=object()), api_factory=no_api,
                         journal_factory=lambda *_: journal)
    with pytest.raises(ImportFault) as blocked:
        offline_result(owner.validate(tmp_path / "missing", object(), consented=True))
    job = blocked.value.operation_id
    assert job and blocked.value.code == "FILE_NOT_FOUND"
    report = journal.diagnostic_report(job, private=True)
    assert report["operation"]["state"] == State.FAILED and not report["operation"]["start_request_sent"]
    assert report["package_issue"]["file"] == "manifest.json" and report["package_issue"]["stage"] == "load_manifest"
    journal.check("safe-new-package", "independent-source")


def test_fix_2_follows_fix_1_in_update_order(tmp_path):
    first = tmp_path / "gTR-Beta-6-Fix-Windows-x64.zip"
    second = tmp_path / "gTR-Beta-6-Fix-2-Windows-x64.zip"
    for path in (first, second):
        path.write_bytes(b"placeholder")
    assert updates.find_download(tmp_path, current=(6, False)).path == second
    update = updates.candidate(second, current=(6, True))
    assert update.version == (6, 2) and update.tag == "beta-6-fix-2" and update.label == "Beta 6 (Fix 2)"
    assert updates.find_download(tmp_path, current=(6, 2)) is None
