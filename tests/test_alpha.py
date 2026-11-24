import json
from datetime import datetime, timedelta
from zipfile import ZipFile

import pytest
from conftest import message, write_json

from gtr.conversion import load_bundle, plan_import, prepare
from gtr.core import analyze, catalogue, summary
from gtr.importer import Journal, run_import
from gtr.mock_api import MockAPI, offline_result, rehearse
from gtr.model import RecoveryError, quiet
from gtr.profiles import read_records
from gtr.reports import save_report
from gtr.verification import compare


@pytest.mark.parametrize("n", [2, 10, 20, 100, 1000, 10000])
def test_owner_experiment_sizes_are_rehearsed_offline(n, tmp_path):
    from datetime import UTC

    origin = datetime(2025, 3, 17, 14, 0, tzinfo=UTC)
    rows = []
    for i in range(1, n + 1):
        name, uid = ("Лея", "user100") if i % 2 else ("Мирон", "user101")
        row = message(
            i,
            f"Запись {i}",
            date=(origin + timedelta(seconds=i)).isoformat(),
            **{"from": name, "from_id": uid},
        )
        if n == 100 and i % 5 == 0:
            row.update(file="fixture.pdf", file_size=12)
        if i > 1:
            row["reply_to_message_id"] = i - 1
        rows.append(row)
    source = write_json(tmp_path / "export", rows)
    if n == 100:
        (source / "fixture.pdf").write_bytes(b"%PDF-1.4\nok\n")
    original = (source / "result.json").read_bytes()
    p = analyze(source, tmp_path / "p")
    result = summary(p)
    assert result["messages"] == n and result["participants"] == 2 and result["duplicates"] == 0
    assert result["replies"] == n - 1 and result["resolved_replies"] == n - 1 and result["errors"] == 0
    b = load_bundle(prepare(p, 1, tmp_path / "b", profile="android" if n == 100 else "ios").root)
    assert b.data["included"] == n and b.data["skipped"] == 0
    assert len(b.data["media"]) == (20 if n == 100 else 0)
    with b.transcript.open(encoding="utf-8", newline="\n") as stream:
        wire = list(read_records(stream, b.data["profile"]))
    assert [(r[1], r[2]) for r in wire] == [(r["from"], r["text"]) for r in rows]
    assert [r["source_ordinal"] for r in b.data["records"]] == list(range(1, n + 1))
    local = rehearse(b, "success", quiet, project=p)
    assert local["mode"] == "mock" and local["verification"]["state"] == "verified"
    assert all(view["observed"] == n for view in local["verification"]["views"])
    assert original == (source / "result.json").read_bytes()


@pytest.mark.parametrize(
    "scenario,field", [("one_sender", "server_author"), ("reordered", "order"), ("missing_on_second", None)]
)
def test_verification_detects_wrong_server_result(bundle, scenario, field):
    result = rehearse(bundle, scenario, quiet)["verification"]
    assert result["state"] == "differences"
    if field:
        assert all(view["metrics"][field]["different"] > 0 for view in result["views"])
    else:
        assert result["views"][1]["observed"] == bundle.data["included"] - 1
    if scenario == "one_sender":
        assert result["views"][0]["metrics"]["label"] == {"same": bundle.data["included"]}


def test_owner_verdict_requires_ui_observation_and_two_accounts(bundle):
    api = MockAPI()
    offline_result(run_import(bundle, api, "mock", True, lambda _: True))
    a, b, mapping = api.observations(bundle)
    for row in a["messages"]:
        row["visual_out"] = None
    result = compare(bundle, a, b, mapping)
    assert result["state"] == "inconclusive"
    with pytest.raises(RecoveryError):
        compare(bundle, a, a, mapping)


def test_plan_sample_and_repeat_guard_across_profile_and_account(tmp_path, project):
    p = plan_import(project, 1, sample=2)
    assert p["included"] == 2 and p["losses"]["sample_limit"] == 8
    first = prepare(project, 1, tmp_path / "one", sample=2)
    second = prepare(project, 1, tmp_path / "two", profile="android")
    assert first.data["text_sha256"] != second.data["text_sha256"]
    journal = Journal(tmp_path / "journal.sqlite", "pm:100/101")
    offline_result(run_import(first, MockAPI(), "mock", True, lambda _: True, journal=journal))
    api = MockAPI()
    with pytest.raises(RecoveryError):
        offline_result(run_import(second, api, "mock", True, lambda _: True, journal=journal))
    assert not api.calls


def test_contacts_locations_and_multi_asset_single_record(tmp_path):
    src = write_json(
        tmp_path / "source",
        [
            message(1, "", contact_information={"first_name": "Ada", "phone_number": "+0000"}),
            message(2, "", location_information={"latitude": 12.5, "longitude": 34.5}),
            message(3, "Приложение", photo="image.jpg", file="notes.pdf"),
        ],
    )
    (src / "image.jpg").write_bytes(b"\xff\xd8\xffsample")
    (src / "notes.pdf").write_bytes(b"%PDF-sample")
    p = analyze(src, tmp_path / "p")
    assert summary(p)["attachments"] == {"contact": 1, "location": 1, "document": 1, "photo": 1}
    b = load_bundle(prepare(p, 1, tmp_path / "bundle").root)
    assert b.data["included"] == 3 and len(b.data["media"]) == 3
    contact, point, multi = b.data["media"]
    assert (
        json.loads(b.media_path(contact).read_text(encoding="utf-8"))["exported_metadata"][
            "contact_information"
        ]["first_name"]
        == "Ada"
    )
    assert (
        json.loads(b.media_path(point).read_text(encoding="utf-8"))["exported_metadata"][
            "location_information"
        ]["latitude"]
        == 12.5
    )
    with ZipFile(b.media_path(multi)) as z:
        for item in multi["members"]:
            assert z.read(item["stored_as"]) == (src / item["source"]).read_bytes()
    assert b.data["losses"]["grouped_media"] == 1


def test_invalid_time_and_identity_evidence(tmp_path):
    src = write_json(
        tmp_path / "s",
        [
            message(1, "Сохранённая запись", date="bad", **{"from": None}),
            message(2, "Известный автор"),
            message(3, "Неопределённая запись", **{"from": None, "from_id": "user800"}),
        ],
    )
    p = analyze(src, tmp_path / "p")
    b = prepare(p, 1, tmp_path / "b", date_policy="ordered")
    assert (
        b.data["included"] == 2
        and b.data["losses"]["identity"] == 1
        and b.data["losses"]["synthetic_time"] == 1
    )
    with catalogue(p) as db:
        raw = db.execute("SELECT sender,date_raw,meta FROM messages WHERE source_id='1'").fetchone()
        assert raw["sender"] == "Лея" and raw["date_raw"] == "bad"
        assert json.loads(raw["meta"])["identity_basis"] == "json_id_matched"


def test_direct_json_copies_only_referenced_items(tmp_path, synthetic):
    (synthetic / "unrelated.dat").write_text("private", encoding="utf-8")
    renamed = synthetic / "conversation.json"
    (synthetic / "result.json").rename(renamed)
    p = analyze(renamed, tmp_path / "p")
    assert summary(p)["export_format"] == "json" and summary(p)["files"] == 9
    assert not (p / "source" / "unrelated.dat").exists()


def test_reports_are_local_and_recheck_hashes(tmp_path, project):
    attachment = project / "source" / "files" / "a.jpg"
    attachment.write_bytes(b"changed")
    dest = save_report(tmp_path / "report.json", project=project)
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["verification"]["counts"]["changed"] == 1
    assert data["verification"]["problems"] == [{"path": "files/a.jpg", "status": "changed"}]
    assert "До встречи" not in dest.read_text(encoding="utf-8")
    with pytest.raises(RecoveryError):
        save_report(project / "source" / "report.html", project=project)
