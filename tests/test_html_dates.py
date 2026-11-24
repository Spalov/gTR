from datetime import UTC, datetime

from gtr.conversion import load_bundle, prepare
from gtr.core import analyze, catalogue, summary
from gtr.reports import project_report, save_report


def message(sid, header, *, author="A", extra="", attrs=""):
    label = f'<div class="from_name">{author}</div>' if author else ""
    joined = " joined" if not author else ""
    return (
        f'<div class="message default{joined}" id="message{sid}" {attrs}><div class="body">'
        f'{header}{label}<div class="text">fixture-{sid}</div>{extra}</div></div>'
    )


def export(tmp_path, *messages, zone="UTC"):
    src = tmp_path / "source"
    src.mkdir()
    (src / "messages.html").write_text("".join(messages), encoding="utf-8")
    (src / "photo.jpg").write_bytes(b"\xff\xd8\xfffixture")
    return analyze(src, tmp_path / "project", zone)


def test_gmt_title_preserves_offset_ties_order_and_media(tmp_path):
    t = '<div class="date" title="31.07.2026 20:01:04 GMT+02:00">20:01</div>'
    media = '<div class="media_wrap"><a class="photo_wrap" href="photo.jpg">Photo</a></div>'
    p = export(
        tmp_path,
        message(90, t, extra=media),
        message(14, t, author=""),
        message(20, t, author="B"),
        zone="Asia/Kathmandu",
    )
    bundle = load_bundle(prepare(p, 1, tmp_path / "package", "source").root)
    assert summary(p)["errors"] == 0
    assert bundle.data["included"] == 3 and bundle.data["skipped"] == 0 and len(bundle.data["media"]) == 1
    assert bundle.data["timeline"] == {"equal": 2, "regression": 0, "cascade": 0, "max_shift_seconds": 0}
    assert [
        (r["source_id"], r["author"], r["import_date"], r["import_utc"]) for r in bundle.data["records"]
    ] == [
        (sid, sender, "2026-07-31T20:01:04", "2026-07-31T18:01:04+00:00")
        for sid, sender in (("90", "A"), ("14", "A"), ("20", "B"))
    ]
    assert bundle.media_path(bundle.data["media"][0]).read_bytes() == b"\xff\xd8\xfffixture"
    dates = project_report(p)["date_diagnostics"]
    assert dates["counts"] == {"parsed": 3}
    assert dates["records"][0]["parsed"] == "2026-07-31T20:01:04+02:00"
    assert dates["records"][0]["selected"] == "date.title" and not dates["records"][0]["issues"]


def test_html_datetime_and_unix_fields_back_up_title_and_keep_evidence(tmp_path):
    unix = int(datetime(2026, 7, 31, 18, 1, 4, tzinfo=UTC).timestamp())
    p = export(
        tmp_path,
        message(
            1,
            '<div class="date" title="unknown"><time datetime="2026-07-31T20:01:04+02:00">20:01</time></div>',
        ),
        message(2, '<div class="date">18:01</div>', attrs=f'data-timestamp="{unix * 1000}"'),
        message(3, '<time datetime="2026-07-31T18:01:04Z">18:01</time>'),
        message(
            4, '<div class="date" title="31.07.2026 20:01:04">20:01</div>', attrs=f'data-unixtime="{unix}"'
        ),
        message(5, '<div class="date" title="31.07.2026 14:31:04 UTC−03:30">14:31</div>'),
    )
    records = project_report(p)["date_diagnostics"]["records"]
    assert [r["selected"] for r in records] == [
        "time.datetime",
        "message.data-timestamp",
        "time.datetime",
        "date.title",
        "date.title",
    ]
    assert [r["utc"] for r in records] == ["2026-07-31T18:01:04+00:00"] * 5
    assert records[0]["raw"] == "unknown" and records[0]["fields"][0]["status"] == "invalid_wall_date"
    assert (
        records[3]["parsed"] == "2026-07-31T20:01:04+02:00" and records[3]["resolution"] == "derived_offset"
    )
    assert summary(p)["issues"] == {"invalid_wall_date": 1}


def test_ambiguity_gap_conflict_and_partial_date_reported_separately(tmp_path):
    p = export(
        tmp_path,
        message(1, '<div class="date" title="25.10.2026 03:30:00">03:30</div>'),
        message(2, '<div class="date" title="29.03.2026 03:30:00">03:30</div>'),
        message(
            3,
            '<div class="date" title="31.07.2026 20:01:04 GMT+02:00" datetime="2026-07-31T20:01:04Z">20:01</div>',
        ),
        message(
            4,
            '<time datetime="2026-07-31">20:01</time>',
            extra='<div class="reply_to"><time datetime="2026-07-30T12:00:00Z"></time></div>',
        ),
        message(5, '<div class="date" title="25.10.2026 03:30:00 GMT+03:00">03:30</div>'),
        zone="Europe/Kyiv",
    )
    bundle = prepare(p, 1, tmp_path / "bundle", "source")
    assert bundle.data["included"] == 1
    for key in ("ambiguous_local_time", "nonexistent_local_time", "date_conflict", "date"):
        assert bundle.data["losses"][key] == 1
    with catalogue(p) as db:
        assert [r[0] for r in db.execute("SELECT date_iso FROM messages ORDER BY id")] == [
            "",
            "",
            "",
            "",
            "2026-10-25T03:30:00+03:00",
        ]
    dates = project_report(p)["date_diagnostics"]
    assert dates["counts"] == {"unresolved": 4, "parsed": 1}
    assert [r["issues"][0]["code"] for r in dates["records"][:4]] == [
        "ambiguous_local_time",
        "nonexistent_local_time",
        "date_conflict",
        "invalid_date",
    ]
    assert dates["records"][3]["fields"][-1]["status"] == "clock_only"
    report = save_report(tmp_path / "dates.html", project=p).read_text(encoding="utf-8")
    assert "Диагностика дат HTML" in report and "GMT+02:00" in report and "пропущенный интервал" in report
    assert "fixture-" not in report
