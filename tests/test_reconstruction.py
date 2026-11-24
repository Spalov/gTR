import json
from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import message, write_json

from gtr.conversion import prepare
from gtr.core import analyze, catalogue, summary
from gtr.parser import timestamp


def html_msg(sid, author="", body="", *, joined=False, stamp="03.02.2025 14:25:26 UTC-03:30"):
    sender = f'<div class="from_name">{author}</div>' if author else ""
    return (
        f'<div class="message default clearfix {"joined" if joined else ""}" id="message{sid}"><div class="body">'
        f'<div class="pull_right date details" title="{stamp}"></div>{sender}{body}</div></div>'
    )


def test_html_pages_grouping_forwarding_replies_and_whitespace(tmp_path):
    source = tmp_path / "chat"
    source.mkdir()
    day = '<div class="message service" id="message-1"><div class="body details">3 February 2025</div></div>'
    plain = '<div class="text">\n  начало <b>жирный</b><br>вторая 🤍  \n       </div>'
    first = html_msg(8, "Алла", plain)
    (source / "messages.html").write_text(day + first, encoding="utf-8")
    forward = (
        '<div class="forwarded body"><div class="from_name">Другой человек'
        '<span class="date">вчера</span></div><div class="text">Переслано</div></div>'
    )
    reply = '<div class="reply_to"><a href="messages.html#go_to_message8">this message</a></div>'
    (source / "messages2.html").write_text(
        day + html_msg(9, body=reply + forward, joined=True) + html_msg(10, body=plain), encoding="utf-8"
    )
    p = analyze(source, tmp_path / "project")
    with catalogue(p) as db:
        a, b, c = db.execute("SELECT * FROM messages ORDER BY id")
        assert a["text"] == "  начало жирный\nвторая 🤍  "
        assert "<b>жирный</b>" in a["rich"]
        assert a["date_iso"] == "2025-02-03T14:25:26-03:30"
        assert (b["sender"], b["text"], b["target"]) == ("Алла", "Переслано", a["id"])
        assert json.loads(b["meta"])["forwarded_from"] == "Другой человек"
        assert c["sender"] == ""
    assert summary(p)["issues"] == {"unknown_sender": 1}


def test_missing_page_breaks_group_and_finds_gap(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "messages.html").write_text(html_msg(1, "A"), encoding="utf-8")
    (src / "messages10.html").write_text(html_msg(3, joined=True), encoding="utf-8")
    result = summary(analyze(src, tmp_path / "p"))
    assert result["issues"] == {"page_gap": 1, "unknown_sender": 1}


def test_columns_types_and_replies(project):
    s = summary(project)
    assert (s["messages"], s["participants"], s["resolved_replies"], s["errors"]) == (10, 2, 1, 0)
    assert s["attachments"] == dict.fromkeys(
        ("photo", "video", "round_video", "voice", "sticker", "animation", "audio", "document"), 1
    )
    assert s["attachment_status"] == {"ok": 8}
    with catalogue(project) as db:
        m = db.execute("SELECT * FROM messages WHERE source_id='1'").fetchone()
        assert m["text"] == "До встречи 👋\nв 12:00."
        assert m["rich"] == "До <b>встречи 👋</b><br>в 12:00."
        assert m["sender_key"] == "user10"


def test_duplicate_conflict_and_repeated_text(tmp_path):
    src = write_json(
        tmp_path / "src",
        [
            message(1, "один"),
            message(1, "один"),
            message(1, "другой"),
            message(2, "один"),
            message(3, "ответ", reply_to_message_id=1),
        ],
    )
    p = analyze(src, tmp_path / "p")
    s = summary(p)
    assert (s["duplicates"], s["conflicting_ids"], s["messages"], s["resolved_replies"]) == (2, 1, 3, 0)
    b = prepare(p, 1, tmp_path / "b")
    assert b.data["included"] == 2 and b.data["losses"]["conflict"] == 1
    with catalogue(p) as db:
        assert (
            json.loads(db.execute("SELECT extra_record FROM duplicates WHERE identical=0").fetchone()[0])[
                "text"
            ]
            == "другой"
        )


def test_multiple_chats_do_not_share_ids_or_replies(tmp_path):
    s = tmp_path / "e"
    s.mkdir()
    chats = [
        {"id": 5, "name": "A", "messages": [message(1, "A")]},
        {
            "id": 6,
            "name": "B",
            "messages": [message(1, "B"), message(2, "X", reply_to_message_id=1, reply_to_peer_id="user5")],
        },
    ]
    (s / "result.json").write_text(json.dumps({"chats": {"list": chats}}), encoding="utf-8")
    p = analyze(s, tmp_path / "p")
    assert summary(p)["duplicates"] == 0
    assert summary(p)["resolved_replies"] == 0
    assert prepare(p, 1, tmp_path / "b").transcript.read_text(encoding="utf-8").endswith("A\n")


def test_date_offset_from_json_epoch_and_dst():
    zone = ZoneInfo("Europe/Kyiv")
    raw = "2025-03-12T04:05:06"
    moment = int(datetime.fromisoformat(raw + "+05:45").timestamp())
    flags = []
    assert timestamp(raw, moment, zone, flags) == raw + "+05:45"
    assert not flags
    assert timestamp("2025-10-26T03:15:00", None, zone, flags) == ""
    assert "ambiguous_local_time" in flags
    assert timestamp("2025-03-30T03:15:00", None, zone, []) == ""
    assert timestamp(raw, None, zone, flags).endswith("+02:00")
    assert "assumed_timezone" in flags
    assert timestamp(raw + "+00:00", moment, zone, flags).endswith("+00:00")
    assert "date_conflict" in flags


def test_html_attachment_original_not_thumbnail_and_unavailable(tmp_path):
    src = tmp_path / "s"
    src.mkdir()
    (src / "my photo.jpg").write_bytes(b"\xff\xd8\xffx")
    wrap = (
        '<div class="media_wrap"><a class="photo_wrap" href="my%20photo.jpg"><img src="thumb.jpg"></a></div>'
    )
    omitted = '<div class="media_wrap"><div class="media media_video"><div class="title">Video message</div></div></div>'
    (src / "messages.html").write_text(html_msg(1, "A", wrap) + html_msg(2, "B", omitted), encoding="utf-8")
    s = summary(analyze(src, tmp_path / "p"))
    assert s["attachments"] == {"photo": 1, "round_video": 1} and s["attachment_status"] == {
        "ok": 1,
        "missing": 1,
    }


def test_br_boundaries_and_reply_to_other_chat(tmp_path):
    s = tmp_path / "s"
    s.mkdir()
    body = (
        '<div class="reply_to"><a href="../other/messages.html#go_to_message1">source</a></div>'
        '<div class="text"><br>Line<br></div>'
    )
    (s / "messages.html").write_text(html_msg(1, "A") + html_msg(2, "B", body), encoding="utf-8")
    p = analyze(s, tmp_path / "p")
    with catalogue(p) as db:
        m = db.execute("SELECT text,target,meta FROM messages WHERE source_id='2'").fetchone()
        assert m["text"] == "\nLine\n" and m["target"] is None
        assert json.loads(m["meta"])["unresolved_reply"]
    assert summary(p)["replies"] == 1


def test_date_uses_surviving_source_field():
    flags = []
    assert timestamp("unknown", "1735689600", ZoneInfo("UTC"), flags) == "2025-01-01T00:00:00+00:00"
    assert flags == ["invalid_wall_date"]
    flags = []
    assert (
        timestamp("2025-01-01T02:00:00+02:00", "bad", ZoneInfo("UTC"), flags) == "2025-01-01T02:00:00+02:00"
    )
    assert flags == ["invalid_epoch"]


def test_cross_chat_reply_requires_namespace_and_unique_source(tmp_path):
    root = tmp_path / "full"
    root.mkdir()
    chats = [
        {"id": 5, "type": "personal_chat", "messages": [message(1, "Автор из личного чата")]},
        {
            "id": 5,
            "type": "private_channel",
            "messages": [
                message(1, "Публикация"),
                message(2, "Ответ", reply_to_peer_id="user5", reply_to_message_id=1),
            ],
        },
    ]
    (root / "result.json").write_text(json.dumps({"chats": {"list": chats}}), encoding="utf-8")
    with catalogue(analyze(root, tmp_path / "p")) as db:
        sender_chat, target_chat = db.execute("""SELECT m.chat,t.chat FROM messages m
            JOIN messages t ON m.target=t.id WHERE m.source_id='2'""").fetchone()
        assert sender_chat == 2 and target_chat == 1


def test_null_source_ids_preserve_distinct_records_without_inventing_ids(tmp_path):
    src = write_json(tmp_path / "s", [message(None, "one"), message(None, "two"), message(1, "three")])
    p = analyze(src, tmp_path / "p")
    s = summary(p)
    assert (s["messages"], s["duplicates"], s["issues"]["missing_id"]) == (3, 0, 2)
    manifest = prepare(p, 1, tmp_path / "b").data
    assert manifest["included"] == 3
    assert [r["source_id"] for r in manifest["records"]] == ["", "", "1"]
    assert manifest["losses"]["missing_source_id"] == 2
