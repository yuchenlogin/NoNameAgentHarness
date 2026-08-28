from __future__ import annotations

import json

import pytest

from noname_harness.cli import main
from noname_harness.store import HarnessStore


def read_json(capsys):
    return json.loads(capsys.readouterr().out)


def test_cli_round_trip(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".noname" / "harness.db"

    assert main(["init", "--db", str(db), "--root", str(root), "--name", "cli project"]) == 0
    read_json(capsys)

    assert main(
        [
            "event",
            "--db",
            str(db),
            "--session",
            "s1",
            "--type",
            "project.goal",
            "--payload",
            '{"key":"goal","content":{"text":"keep continuity"}}',
        ]
    ) == 0
    event = read_json(capsys)

    assert main(
        [
            "curate",
            "--db",
            str(db),
            "--event-id",
            event["id"],
        ]
    ) == 0
    proposal = read_json(capsys)
    assert proposal["layer"] == "high"
    assert main(["proposals", "--db", str(db)]) == 0
    assert read_json(capsys)[0]["id"] == proposal["id"]

    assert main(["snapshot", "--db", str(db), "--session", "s1"]) == 0
    snapshot = read_json(capsys)
    assert snapshot["event_type"] == "workspace.snapshot"

    assert main(
        [
            "review",
            "--db",
            str(db),
            "--proposal-id",
            proposal["id"],
            "--action",
            "accept",
            "--reviewer",
            "user",
        ]
    ) == 0
    read_json(capsys)

    assert main(
        [
            "package",
            "--db",
            str(db),
            "--session",
            "s1",
            "--task",
            "continue",
            "--format",
            "markdown",
        ]
    ) == 0
    rendered = capsys.readouterr().out
    assert "keep continuity" in rendered
    assert "## High layer" in rendered

    assert main(["search", "keep continuity", "--db", str(db)]) == 0
    assert read_json(capsys)[0]["id"] == event["id"]
    assert main(["verify", "--db", str(db)]) == 0
    assert read_json(capsys)["ok"] is True


def test_cli_rejects_outside_package_before_recording_a_handoff(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".noname" / "harness.db"

    assert main(["init", "--db", str(db), "--root", str(root)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "package",
                "--db",
                str(db),
                "--session",
                "s1",
                "--task",
                "continue",
                "--out",
                str(tmp_path / "outside.md"),
            ]
        )
        == 2
    )
    capsys.readouterr()

    with HarnessStore(db) as store:
        assert store.list_events("s1") == []

    assert (
        main(
            [
                "package",
                "--db",
                str(db),
                "--session",
                "s1",
                "--task",
                "continue",
                "--out",
                str(db),
            ]
        )
        == 2
    )
    capsys.readouterr()
    with HarnessStore(db) as store:
        assert store.list_events("s1") == []


def test_cli_does_not_create_orphan_events_before_init(tmp_path, capsys):
    db = tmp_path / "uninitialized.db"
    assert (
        main(
            [
                "event",
                "--db",
                str(db),
                "--session",
                "s1",
                "--type",
                "note",
                "--payload",
                '{"text":"orphan"}',
            ]
        )
        == 2
    )
    capsys.readouterr()
    with HarnessStore(db) as store:
        with pytest.raises(RuntimeError):
            store.project()
