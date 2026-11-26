import shutil

import pytest
from conftest import message, write_json
from test_hotfix_recovery import _legacy_database

from gtr.account import Account, Target
from gtr.conversion import prepare
from gtr.core import analyze
from gtr.faults import ImportFault
from gtr.fixtures import _png
from gtr.importer import ImportController
from gtr.incremental_import import (
    history,
    learn_legacy,
    local_preview,
    recover_package,
    recovery_root,
    remember_profile,
    save_verification_preference,
)
from gtr.journal import Endpoint, Journal, RecoveryRequired, State
from gtr.mock_api import MockAPI, offline_result
from gtr.model import RecoveryError
from gtr.verification import compare


def project(root, name, rows, **kwargs):
    source = write_json(root / ("source-" + name), rows, **kwargs)
    (source / "sample.png").write_bytes(_png())
    return analyze(source, root / ("project-" + name))


def journal_at(root, account=100, peer=101, kind="pm"):
    return Journal(root / "registry.sqlite", Endpoint.telegram(account, peer, kind))


def accepted(journal, bundle, project=None):
    api = MockAPI()
    controller = ImportController(api, "target", journal=journal, mode="owner")
    controller.source_project = project
    ticket = offline_result(controller.validate(bundle))
    offline_result(controller.execute(ticket, approved=True))
    return ticket.operation_id, api



def test_regeneration_after_restart_keeps_media_preferences_and_record_filter(tmp_path):
    source = write_json(tmp_path / "source-filtered", [
        message(1, "История", forwarded_from="Архивная подпись", media_type="video_file", file="synthetic.mp4"),
        message(2, "Вне выборки"),
    ])
    (source / "synthetic.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom")
    catalogue = analyze(source, tmp_path / "recovery-catalogue")
    plan = prepare(catalogue, 1, tmp_path / "prepared-choices", "source",
                   video_policy="document", annotate=True, record_kind="forward")
    journal = journal_at(tmp_path)
    job, _ = accepted(journal, plan, catalogue)
    retained = recovery_root(journal, plan.manifest_hash())
    assert retained.resolve().is_relative_to(tmp_path.resolve())
    shutil.rmtree(retained)

    restored = recover_package(journal_at(tmp_path), job)
    assert restored.manifest_hash() == plan.manifest_hash()
    assert restored.data["media"][0]["force_file"]
    assert restored.data["included"] == 1
    assert "Историческая пометка" in restored.transcript.read_text(encoding="utf-8")

def test_updated_archive_recognizes_partial_overlap_after_restart(tmp_path):
    p = project(tmp_path, "first", [message(n, "Снова") for n in range(11, 15)])
    old = prepare(p, 1, tmp_path / "first")
    journal = journal_at(tmp_path)
    job, _ = accepted(journal, old)
    journal.transition(job, State.COMPLETED, "verification")
    moved = project(tmp_path, "updated", [message(n, "Снова") for n in range(13, 18)])
    new = prepare(moved, 1, tmp_path / "updated")
    assert new.scope() != old.scope()
    review = journal_at(tmp_path, 101, 100).review_bundle(new)
    assert review.decisions == {
        1: "CONFIRMED_IMPORTED",
        2: "CONFIRMED_IMPORTED",
        3: "NEW",
        4: "NEW",
        5: "NEW",
    }
    assert journal.review_bundle(old).eligible_count == 0
    api = MockAPI()
    with pytest.raises(RecoveryRequired):
        offline_result(ImportController(api, "target", journal=journal, mode="owner").validate(new))
    assert api.calls == []
    assert journal_at(tmp_path, 100, 210).review_bundle(new).eligible == ((1, 5),)
    different = project(tmp_path, "other", [message(n, "Снова") for n in range(11, 15)], id=99)
    assert journal.review_bundle(prepare(different, 1, tmp_path / "other")).eligible == ((1, 4),)


def test_id_content_change_and_idless_occurrences_are_not_merged(tmp_path):
    first = [message(10, "До"), message(None, "Повтор"), message(None, "Повтор"), message(12, "После")]
    p = project(tmp_path, "a", first)
    old = prepare(p, 1, tmp_path / "a", first_record=2, last_record=2)
    journal = journal_at(tmp_path)
    accepted(journal, old)
    stable = prepare(project(tmp_path, "b", [*first, message(13, "Ещё")]), 1, tmp_path / "b")
    review = journal.review_bundle(stable)
    assert review.decisions[2] == "POSSIBLY_IMPORTED" and review.decisions[3] == "NEW"
    ambiguous = [*first[:2], message(None, "Повтор"), *first[2:]]
    changed = prepare(project(tmp_path, "c", ambiguous), 1, tmp_path / "c")
    assert [journal.review_bundle(changed).decisions[i] for i in (2, 3, 4)] == ["CONFLICT"] * 3
    numbered = prepare(p, 1, tmp_path / "number", first_record=1, last_record=1)
    accepted(journal, numbered)
    altered = prepare(project(tmp_path, "d", [message(10, "Изменено")]), 1, tmp_path / "d")
    assert journal.review_bundle(altered).decisions == {1: "CONFLICT"}


def test_durable_copy_rebuild_missing_source_and_profile_restart(tmp_path):
    p = project(tmp_path, "original", [message(1, "Текст", photo="sample.png")])
    previous = prepare(p, 1, tmp_path / "temporary", "source")
    journal = journal_at(tmp_path)
    remember_profile(
        journal,
        Account(100, "Лея"),
        Target(101, "Мирон", "pm", None),
        p,
        p.parent / "first",
        "text_media",
        {"as_document": False},
    )
    job, _ = accepted(journal, previous, p)
    sha = previous.manifest_hash()
    shutil.rmtree(previous.root)
    reopened = journal_at(tmp_path)
    saved = recover_package(reopened, job)
    assert saved.manifest_hash() == sha and saved.media_path(saved.data["media"][0]).read_bytes() == _png()
    shutil.rmtree(recovery_root(journal, sha))
    assert recover_package(reopened, job).manifest_hash() == sha
    profiles, rows = history(reopened)
    assert len(rows) == 1 and profiles[0]["title"] == "Лея ↔ Мирон" and profiles[0]["pending"] == 1
    assert profiles[0]["confirmed"] == 0 and profiles[0]["last_success"] and not profiles[0]["last_verified"]
    shutil.rmtree(recovery_root(journal, sha))
    (p / "source" / "sample.png").write_bytes(b"altered")
    with pytest.raises(RecoveryError, match="изменились"):
        recover_package(journal, job)
    p.rename(tmp_path / "unavailable")
    with pytest.raises(RecoveryError, match="отсутствует"):
        recover_package(journal, job)


def test_legacy_package_discovery_exact_only_and_no_directory_prompt(tmp_path):
    p = project(tmp_path, "legacy", [message(n, f"Запись {n}") for n in range(1, 6)])
    old = prepare(p, 1, tmp_path / "gTR-import-past", last_record=2)
    new = prepare(p, 1, tmp_path / "gTR-import-later")
    endpoint = Endpoint.telegram(100, 101, "pm")
    _legacy_database(tmp_path / "registry.sqlite", endpoint, old)
    journal = journal_at(tmp_path)
    assert journal.review_bundle(new).unknown == 5
    learn_legacy(journal, new, [tmp_path])
    assert journal.review_bundle(new).eligible == ((3, 5),)
    shutil.rmtree(old.root)
    assert recover_package(journal, "prior").manifest_hash() == old.manifest_hash()
    profiles, _ = history(journal)
    assert profiles[0]["account"] == 100 and profiles[0]["target_id"] == 101


def test_changed_archive_unknown_stays_locked_and_stale_group_preflight_never_sends(tmp_path):
    p = project(tmp_path, "early", [message(1, "Запись")])
    old = prepare(p, 1, tmp_path / "earlier")
    updated = prepare(
        project(tmp_path, "late", [message(1, "Запись"), message(2, "Новая")]), 1, tmp_path / "later"
    )
    first = journal_at(tmp_path, 100, -777, "group")
    second = journal_at(tmp_path, 102, -777, "group")
    api, other = MockAPI("lost_reply"), MockAPI()
    one = ImportController(api, "group", journal=first, mode="owner")
    two = ImportController(other, "group", journal=second, mode="owner")
    a, b = offline_result(one.validate(old)), offline_result(two.validate(updated))
    with pytest.raises(ImportFault):
        offline_result(one.execute(a, approved=True))
    with pytest.raises(RecoveryRequired):
        offline_result(two.execute(b, approved=True))
    assert other.calls == ["check_history", "check_peer"] and api.calls.count("start_import") == 1
    assert set(second.review_bundle(updated).decisions.values()) == {"UNKNOWN_SERVER_STATE"}


def test_optional_verification_preserves_all_prior_records_and_prepares_only_new(tmp_path):
    records = [message(i, f"Запись {i}") for i in range(1, 4)]
    source = project(tmp_path, "old", records)
    journal = journal_at(tmp_path)
    account, target = Account(100, "Лея"), Target(101, "Мирон", "pm", None)
    remember_profile(journal, account, target, source, None, "text_only", {"chat": 1})

    confirmed, _ = accepted(journal, prepare(source, 1, tmp_path / "confirmed", last_record=1))
    journal.transition(confirmed, State.COMPLETED, "verification")
    pending, _ = accepted(journal, prepare(source, 1, tmp_path / "pending", first_record=2, last_record=2))
    uncertain = ImportController(MockAPI("lost_reply"), "target", journal=journal, mode="owner")
    third = prepare(source, 1, tmp_path / "uncertain", first_record=3)
    receipt = offline_result(uncertain.validate(third))
    with pytest.raises(ImportFault, match="неизвестен"):
        offline_result(uncertain.execute(receipt, approved=True))
    previous_operation = journal.operation(receipt.operation_id)
    previous_events = journal.events(receipt.operation_id)

    updated = project(tmp_path, "updated", [*records, message(4, "Новая")])
    _, strict, _ = local_preview(journal, updated, 1, "text_only")
    assert not strict.eligible
    profiles, _ = history(journal)
    save_verification_preference(journal, profiles[0], False)

    reopened = journal_at(tmp_path)
    _, review, _ = local_preview(reopened, updated, 1, "text_only")
    assert review.decisions == {
        1: "CONFIRMED_IMPORTED",
        2: "POSSIBLY_IMPORTED",
        3: "UNKNOWN_SERVER_STATE",
        4: "NEW",
    }
    assert review.eligible == ((4, 4),)
    clean = prepare(updated, 1, tmp_path / "new", record_ranges=review.eligible)
    assert clean.record_intervals() == ((4, 4),)
    assert not reopened.review_bundle(clean).conflicts
    assert not journal_at(tmp_path, 101, 100).review_bundle(clean).eligible
    _, transport = accepted(reopened, clean)
    assert transport.calls.count("start_import") == 1
    assert len(transport.content) == 1
    assert reopened.operation(receipt.operation_id) == previous_operation
    assert reopened.events(receipt.operation_id) == previous_events
    assert recover_package(reopened, receipt.operation_id).manifest_hash() == third.manifest_hash()

    replay = MockAPI()
    with pytest.raises(RecoveryRequired):
        offline_result(ImportController(replay, "target", journal=reopened, mode="owner").validate(third))
    assert replay.calls == []
    profiles, rows = history(reopened)
    assert {confirmed, pending, receipt.operation_id} <= {r["id"] for r in rows}
    assert next(r for r in rows if r["id"] == receipt.operation_id)["status"] == "UNKNOWN_SERVER_STATE"
    save_verification_preference(reopened, profiles[0], True)
    assert not reopened.review_bundle(clean).eligible


def test_optional_verification_keeps_active_lock_and_incomplete_legacy_coverage(tmp_path):
    records = [message(i, f"Запись {i}") for i in range(1, 3)]
    source = project(tmp_path, "earlier", records)
    old = prepare(source, 1, tmp_path / "earlier")
    updated = project(tmp_path, "later", [*records, message(3, "Новая")])
    new = prepare(updated, 1, tmp_path / "later", first_record=3)
    journal = journal_at(tmp_path)
    account, target = Account(100, "Лея"), Target(101, "Мирон", "pm", None)
    remember_profile(
        journal, account, target, source, None, "text_media", {"verify_previous_operations": False}
    )

    api = MockAPI()
    active = ImportController(api, "target", journal=journal, mode="owner")
    receipt = offline_result(active.validate(old))
    job = receipt.operation_id
    journal.transition(job, State.CONFIRMED, "confirmed", reserve=True)
    journal.transition(job, State.INITIALIZING, "init_import")
    journal.transition(job, State.INITIALIZING, "initialized", server_id=1)
    journal.transition(job, State.UPLOADING, "upload")
    journal.transition(job, State.STARTING, "start_import", outcome="uncertain", start_sent=None)
    other = MockAPI()
    with pytest.raises(RecoveryRequired):
        offline_result(ImportController(other, "target", journal=journal, mode="owner").validate(new))
    assert other.calls == []
    journal.transition(job, State.START_UNCERTAIN, "start_import")
    assert journal.review_bundle(new).eligible == ((3, 3),)

    with journal.connect(lock=True) as db:
        db.execute("DELETE FROM registry_records WHERE operation=? AND ordinal=2", (job,))
    review = journal.review_bundle(new)
    assert not review.eligible and review.unknown == 1 and review.overlap is None
    with pytest.raises(RecoveryRequired):
        offline_result(ImportController(other, "target", journal=journal, mode="owner").validate(new))
    assert other.calls == []
    active.close()


def test_optional_verification_honors_telegram_wait_after_a_terminal_unknown_start(tmp_path):
    source = project(tmp_path, "source", [message(1, "Прошлая"), message(2, "Новая")])
    first = prepare(source, 1, tmp_path / "old", last_record=1)
    new = prepare(source, 1, tmp_path / "new", first_record=2)
    journal = journal_at(tmp_path)
    remember_profile(
        journal,
        Account(100, "Лея"),
        Target(101, "Мирон", "pm", None),
        source,
        None,
        "text_media",
        {"verify_previous_operations": False},
    )
    previous = ImportController(MockAPI("lost_reply"), "target", journal=journal, mode="owner")
    receipt = offline_result(previous.validate(first))
    with pytest.raises(ImportFault, match="неизвестен"):
        offline_result(previous.execute(receipt, approved=True))

    api = MockAPI("active")
    current = ImportController(api, "target", journal=journal, mode="owner")
    ready = offline_result(current.validate(new))
    with pytest.raises(ImportFault) as wait:
        offline_result(current.execute(ready, approved=True))
    assert wait.value.code == "PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT" and wait.value.seconds == 30
    assert api.calls.count("init_import") == 1 and "start_import" not in api.calls
    retry = MockAPI()
    with pytest.raises(RecoveryError, match="интервал ожидания"):
        offline_result(ImportController(retry, "target", journal=journal, mode="owner").validate(new))
    assert retry.calls == []
    assert journal.operation(receipt.operation_id)["state"] == State.START_UNCERTAIN
    assert journal.review_bundle(first).unknown_records == ((1, 1),)


def test_optional_verification_keeps_ambiguous_id_after_export_metadata_changes(tmp_path):
    source = project(tmp_path, "old", [message(7, "Прежняя", from_id="")], id=None)
    first = prepare(source, 1, tmp_path / "old")
    revised = write_json(
        tmp_path / "updated", [message(7, "Прежняя"), message(8, "Новая")], name="Переименованный диалог"
    )
    new = prepare(analyze(revised, tmp_path / "project"), 1, tmp_path / "new")
    journal = journal_at(tmp_path)
    remember_profile(
        journal,
        Account(100, "Лея"),
        Target(101, "Мирон", "pm", None),
        source,
        None,
        "text_media",
        {"verify_previous_operations": False},
    )
    previous = ImportController(MockAPI("lost_reply"), "target", journal=journal, mode="owner")
    ticket = offline_result(previous.validate(first))
    with pytest.raises(ImportFault, match="неизвестен"):
        offline_result(previous.execute(ticket, approved=True))
    assert journal.review_bundle(new).decisions == {1: "CONFLICT", 2: "NEW"}


@pytest.mark.parametrize(
    "scenario,expected", [("peer", "FAILED_BEFORE_START"), ("media", "UNKNOWN_SERVER_STATE")]
)
def test_prestart_decisions_after_updated_export(tmp_path, scenario, expected):
    records = [message(1, "Запись", photo="sample.png")]
    old = prepare(project(tmp_path, "old", records), 1, tmp_path / "old")
    new = prepare(project(tmp_path, "new", [*records, message(2, "Новая")]), 1, tmp_path / "new")
    journal = journal_at(tmp_path)
    run = ImportController(MockAPI(scenario), "target", journal=journal, mode="owner")
    with pytest.raises(ImportFault):
        ticket = offline_result(run.validate(old))
        offline_result(run.execute(ticket, approved=True))
    assert journal.review_bundle(new).decisions[1] == expected
    if scenario == "media":
        journal.owner_disconnected(run.job)
        journal.recover_before_start(run.job, acknowledged=True)
        assert journal.review_bundle(new).decisions == {1: "READY_FOR_RETRY", 2: "NEW"}


def test_10_then_20_then_90_including_83_media_never_count_acceptance_as_proof(tmp_path):
    first = project(tmp_path, "first", [message(n, f"Запись {n}") for n in range(1, 11)])
    journal = journal_at(tmp_path)
    accepted(journal, prepare(first, 1, tmp_path / "ten", "source", media_mode="text_only"))
    rows = [message(n, f"Запись {n}", **({"photo": "sample.png"} if n >= 38 else {})) for n in range(1, 121)]
    second = project(tmp_path, "second", rows[:30])
    _, review, _ = local_preview(journal, second, 1, "text_media")
    assert review.eligible == ((11, 30),) and review.unknown == 10
    accepted(journal, prepare(second, 1, tmp_path / "twenty", "source", record_ranges=review.eligible))
    p = project(tmp_path, "third", rows)
    _, review, _ = local_preview(journal, p, 1, "text_media")
    assert review.eligible == ((31, 120),) and review.unknown == 30
    final = prepare(p, 1, tmp_path / "ninety", "source", record_ranges=review.eligible)
    job, api = accepted(journal, final)
    assert final.data["included"] == 90 and api.calls.count("upload") == 83
    assert len({r["display_name"] for r in final.data["media"]}) == 1
    assert len({r["name"] for r in final.data["media"]}) == 83
    assert journal.operation(job)["media_done"] == 83 and journal.review_bundle(final).confirmed == 0
    assert journal.review_bundle(final).eligible_count == 0 and api.calls.count("start_import") == 1


def test_historical_group_pair_checks_filename_and_destination(tmp_path):
    p = project(tmp_path, "history", [message(5, "Отчёт", file="sample.png", file_name="Копия.png")])
    bundle = prepare(p, 1, tmp_path / "document", "source", media_mode="document")
    journal = journal_at(tmp_path, 100, -900, "group")
    job, api = accepted(journal, bundle)
    a, b, authors = api.observations(bundle)
    for snapshot in (a, b):
        snapshot.update(
            schema=3,
            mode="telegram_observation",
            peer_id=-900,
            peer_kind="group",
            after_id=0,
            through_id=None,
            captured_at="2090-01-01T00:00:00+00:00",
            scope={
                "kind": "historical_date",
                "after_id": 0,
                "through_id": None,
                "historical_from": "2025-01-02",
                "historical_through": "2025-01-03",
                "truncated": False,
                "scanned": 1,
                "returned": 1,
                "scan_limit": 15,
                "baseline_captured_at": None,
            },
        )
        snapshot["messages"][0].update(
            document_name="Другой.png", document_mime="image/png", media_constructor="MessageMediaDocument"
        )
    result = compare(bundle, a, b, authors, project=p)
    assert (
        result["views"][0]["metrics"]["document_name"] == {"different": 1}
        and not result["confirmed_source_ordinals"]
    )
    for snapshot in (a, b):
        snapshot["messages"][0]["document_name"] = "Копия.png"
    result = compare(bundle, a, b, authors, project=p)
    assert result["confirmed_source_ordinals"] == [1] and not result["accepted_evidence"]
    assert journal_at(tmp_path, 100, -901, "group").reconcile(result) is None
    assert journal.reconcile(result) == job and journal.review_bundle(bundle).confirmed == 1


def test_318_selected_with_100_confirmed_automatically_offers_218(tmp_path):
    p = project(tmp_path, "all", [message(i, f"Строка {i}") for i in range(1, 319)])
    journal = journal_at(tmp_path)
    old = prepare(p, 1, tmp_path / "hundred", "source", last_record=100)
    job, _ = accepted(journal, old)
    journal.transition(job, State.COMPLETED, "verification")
    _, review, _ = local_preview(journal, p, 1, "text_media")
    assert (review.selected, review.confirmed, review.eligible_count, review.unknown) == (318, 100, 218, 0)
    assert review.eligible == ((101, 318),)


def test_manual_acceptance_generator_produces_three_cumulative_exports(tmp_path):
    import json

    from gtr.fixtures import incremental_fixture

    target = tmp_path / "series"
    assert incremental_fixture(target)["новые_записи"] == [10, 20, 90]
    groups = [
        json.loads((target / f"Экспорт-{n}" / "result.json").read_text(encoding="utf-8"))["messages"]
        for n in (10, 30, 120)
    ]
    assert groups[0] == groups[1][:10] and groups[1] == groups[2][:30]
    assert sum("photo" in r or "file" in r for r in groups[2]) == 83
    with pytest.raises(RecoveryError):
        incremental_fixture(target)


def test_hash_only_alpha_cannot_replay_changed_export_and_exact_scoped_package_recovers(tmp_path):
    import sqlite3
    from contextlib import closing

    old_project = project(tmp_path, "alpha", [message(1, "Старое"), message(2, "Позже")])
    old = prepare(old_project, 1, tmp_path / "gTR-import-alpha", last_record=1)
    updated = prepare(
        project(tmp_path, "next", [message(1, "Старое"), message(2, "Позже"), message(3, "Ещё")]),
        1,
        tmp_path / "updated",
    )
    with closing(sqlite3.connect(tmp_path / "registry.sqlite")) as db, db:
        db.execute("CREATE TABLE starts(peer TEXT,fingerprint TEXT,state TEXT)")
        db.execute("CREATE TABLE plans(peer TEXT,scope TEXT,fingerprint TEXT)")
        db.execute("INSERT INTO starts VALUES (?,?,?)", ("100/101", old.content_fingerprint(), "accepted"))
        db.execute("INSERT INTO plans VALUES (?,?,?)", ("100/101", old.scope(), old.content_fingerprint()))
    journal = journal_at(tmp_path)
    assert journal.review_bundle(updated).eligible_count == 0
    learn_legacy(journal, updated, [tmp_path])
    assert journal.review_bundle(updated).decisions == {1: "POSSIBLY_IMPORTED", 2: "NEW", 3: "NEW"}
    fresh = prepare(old_project, 1, tmp_path / "fresh", first_record=2)
    accepted(journal, fresh)


def test_unnumbered_conversation_uses_context_outside_selection_or_marks_conflict(tmp_path):
    journal = journal_at(tmp_path)
    rows = [message(i, f"Запись {i}") for i in range(1, 4)]
    first = project(tmp_path, "a", rows, id=None)
    old = prepare(first, 1, tmp_path / "a", last_record=1)
    job, _ = accepted(journal, old)
    journal.transition(job, State.COMPLETED, "verification")
    updated = project(tmp_path, "b", [*rows, message(4, "Ещё")], id=None)
    partial = prepare(updated, 1, tmp_path / "b", last_record=1)
    assert journal.review_bundle(partial).decisions == {1: "CONFIRMED_IMPORTED"}
    short = project(tmp_path, "c", [rows[0], message(50, "Другое")], id=None)
    assert journal.review_bundle(prepare(short, 1, tmp_path / "c")).decisions == {1: "CONFLICT", 2: "NEW"}


@pytest.mark.parametrize("has_unknown_operation", [False, True])
def test_concurrent_owner_windows_reserve_one_server_start(tmp_path, has_unknown_operation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    records = [message(1, "Встреча")]
    peers = [(100, 101), (101, 100)]
    if has_unknown_operation:
        previous = prepare(project(tmp_path, "old", records), 1, tmp_path / "old")
        old_journal = journal_at(tmp_path)
        controller = ImportController(MockAPI("lost_reply"), "peer", journal=old_journal, mode="owner")
        ticket = offline_result(controller.validate(previous))
        with pytest.raises(ImportFault, match="неизвестен"):
            offline_result(controller.execute(ticket, approved=True))
        for account, peer in peers:
            remember_profile(
                journal_at(tmp_path, account, peer),
                Account(account, "Владелец"),
                Target(peer, "Собеседник", "pm", None),
                None,
                None,
                "text_media",
                {"verify_previous_operations": False},
            )
        records.append(message(2, "Дополнительная запись"))
    project_path = project(tmp_path, "race", records)
    bundle = prepare(project_path, 1, tmp_path / "race", first_record=len(records))
    simultaneous = Barrier(2, timeout=12)

    def window(ids):
        api = MockAPI()
        journal = journal_at(tmp_path, *ids)
        controller = ImportController(api, "peer", journal=journal, mode="owner")
        ticket = offline_result(controller.validate(bundle))
        simultaneous.wait()
        try:
            offline_result(controller.execute(ticket, approved=True))
        except RecoveryRequired:
            pass
        return api.calls

    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = list(pool.map(window, peers))
    assert sum(c.count("init_import") for c in calls) == 1
    assert sum(c.count("start_import") for c in calls) == 1
