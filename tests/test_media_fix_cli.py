import json

from gtr import cli
from gtr.media_queue import MediaDecisionRequired


def test_cli_applies_batch_consent_and_per_file_decisions(project, tmp_path, monkeypatch, capsys):
    settings = {"c" * 64: "document"}
    decisions = tmp_path / "choices.json"
    decisions.write_text(json.dumps(settings), encoding="utf-8")
    observed = {}

    def prepare(*args, **kwargs):
        observed.update(kwargs)
        raise MediaDecisionRequired([{"key": "example", "name": "clip.mp4"}])

    monkeypatch.setattr(cli, "prepare", prepare)
    assert cli.main([
        "prepare", str(project), "--chat", "1", "--out", str(tmp_path / "prepared"),
        "--auto-compatible-videos", "--video-decisions", str(decisions),
    ]) == 2
    assert observed["video_policy"] == "compatible"
    assert observed["video_decisions"] == settings
    response = json.loads(capsys.readouterr().err)
    assert response["status"] == "media_decision_required"
    assert response["media_decisions_required"][0]["key"] == "example"
