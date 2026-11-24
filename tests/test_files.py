import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from conftest import message, write_json

from gtr import archive
from gtr.conversion import load_bundle, prepare
from gtr.core import analyze, summary, verify
from gtr.model import Cancelled, RecoveryError


@pytest.mark.parametrize(
    "name", ["../outside", "/outside", "nested/CON.txt", "C:drive", "file:stream", "trail."]
)
def test_zip_path_rejected_without_writing_outside(tmp_path, name):
    z = tmp_path / "source.zip"
    with zipfile.ZipFile(z, "w") as archive_file:
        archive_file.writestr(name, b"synthetic")
    with pytest.raises(RecoveryError):
        analyze(z, tmp_path / "p")
    assert set(tmp_path.iterdir()) == {z}


def test_zip_symlink_and_case_collisions(tmp_path):
    z = tmp_path / "source.zip"
    with zipfile.ZipFile(z, "w") as f:
        link = zipfile.ZipInfo("link")
        link.external_attr = 0o120777 << 16
        f.writestr(link, "target")
    with pytest.raises(RecoveryError):
        analyze(z, tmp_path / "p")
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Dir/File", "one")
        f.writestr("dir/file", "two")
    with pytest.raises(RecoveryError):
        analyze(z, tmp_path / "p")


def test_zip_roundtrip_and_budgets(tmp_path, synthetic, monkeypatch):
    z = tmp_path / "nested.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as f:
        for item in synthetic.rglob("*"):
            if item.is_file():
                f.write(item, "wrapped/" + item.relative_to(synthetic).as_posix())
    before = hashlib.sha256(z.read_bytes()).hexdigest()
    s = summary(analyze(z, tmp_path / "p"))
    assert s["sha256"] == before == hashlib.sha256(z.read_bytes()).hexdigest()
    assert s["messages"] == 10 and s["attachment_status"] == {"ok": 8}
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(RecoveryError):
        analyze(z, tmp_path / "other")
    assert not (tmp_path / "other").exists()


@pytest.mark.parametrize("extension", ["rar", "zip"])
def test_damaged_archive(tmp_path, extension):
    p = tmp_path / ("broken." + extension)
    p.write_bytes(b"synthetic incomplete file")
    with pytest.raises(RecoveryError):
        analyze(p, tmp_path / "p")
    assert list(tmp_path.iterdir()) == [p]


def test_tiny_rar(tmp_path):
    z = Path(__file__).parent / "fixtures" / "synthetic.rar"
    result = summary(analyze(z, tmp_path / "p"))
    assert (result["source_type"], result["messages"], result["files"]) == ("rar", 2, 1)


def test_cancellation_keeps_original_and_retry_is_clean(tmp_path, synthetic):
    target = tmp_path / "p"
    before = (synthetic / "result.json").read_bytes()

    def stop(stage, done, total):
        if stage == "reconstruct":
            raise Cancelled()

    with pytest.raises(Cancelled):
        analyze(synthetic, target, progress=stop)
    assert not target.exists() and not list(tmp_path.glob(".gtr-*"))
    assert (synthetic / "result.json").read_bytes() == before
    assert summary(analyze(synthetic, target))["messages"] == 10
    with pytest.raises(RecoveryError):
        analyze(synthetic, target)


def test_output_guards(tmp_path, synthetic, project):
    with pytest.raises(RecoveryError):
        analyze(synthetic, synthetic / "new")
    for bad in (synthetic / "nested", project / "source" / "nested", project):
        with pytest.raises(RecoveryError):
            prepare(project, 1, bad)


def test_integrity_and_copy_recheck(project, tmp_path):
    assert verify(project) == {"ok": 9}
    (project / "source/files/a.jpg").write_bytes(b"\xff\xd8\xff\xe0FAKE")
    (project / "source/files/a.pdf").unlink()
    assert verify(project) == {"ok": 7, "changed": 1, "missing": 1}
    with pytest.raises(RecoveryError):
        prepare(project, 1, tmp_path / "b")
    assert not (tmp_path / "b").exists()


def test_bad_media_evidence(tmp_path):
    src = write_json(
        tmp_path / "src",
        [
            message(1, "caption", photo="../outside"),
            message(2, "caption", photo="empty.jpg"),
            message(3, "caption", photo="fake.jpg"),
            message(4, "caption", file="size.pdf", file_size=999),
            message(5, "caption", file="unknown.bin"),
            message(6, "caption", photo="absent.jpg"),
        ],
    )
    for name, data in [
        ("empty.jpg", b""),
        ("fake.jpg", b"%PDF-data"),
        ("size.pdf", b"%PDF-1.7"),
        ("unknown.bin", b"data"),
    ]:
        (src / name).write_bytes(data)
    p = analyze(src, tmp_path / "p")
    assert summary(p)["attachment_status"] == dict.fromkeys(
        ("unsafe", "empty", "type_mismatch", "size_mismatch", "unconfirmed_type", "missing"), 1
    )
    b = prepare(p, 1, tmp_path / "b")
    assert (
        b.data["included"] == 6 and len(b.data["media"]) == 6 and b.data["losses"]["media_unavailable"] == 5
    )
    assert b.data["losses"]["reference_document"] == 5


def test_bundle_tamper_refused(bundle):
    assert load_bundle(bundle.root).fingerprint() == bundle.fingerprint()
    path = bundle.root / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["media"][0]["name"] = "../another"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(RecoveryError):
        load_bundle(bundle.root)


def test_changed_transcript_refused(bundle):
    bundle.transcript.write_text("modified", encoding="utf-8")
    with pytest.raises(RecoveryError):
        load_bundle(bundle.root)
