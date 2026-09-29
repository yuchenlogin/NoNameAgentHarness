"""CLI coverage for the taste-card commands."""

from __future__ import annotations

import json

from noname_harness.cli import main


def read_json(capsys):
    return json.loads(capsys.readouterr().out)


def test_card_cli_round_trip(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".noname" / "harness.db"

    assert main(["init", "--db", str(db), "--root", str(root)]) == 0
    read_json(capsys)
    assert main([
        "taste-add", "--db", str(db), "--scope", "user",
        "--content", '{"judgement":"克制"}',
    ]) == 0
    taste = read_json(capsys)

    # propose clusters
    assert main(["card-propose", "--db", str(db)]) == 0
    clusters = read_json(capsys)
    assert clusters[0]["track"] == "authored"

    # create a card
    assert main([
        "card-create", "--db", str(db), "--title", "克制", "--attitude", "工具要克制",
        "--track", "authored", "--scope", "user", "--taste-id", taste["id"],
    ]) == 0
    card = read_json(capsys)
    assert card["status"] == "candidate"

    # queue shows the candidate first
    assert main(["card-queue", "--db", str(db)]) == 0
    assert read_json(capsys)[0]["id"] == card["id"]

    # review -> accept
    assert main([
        "card-review", "--db", str(db), "--card-id", card["id"],
        "--action", "accept", "--reviewer", "user",
    ]) == 0
    assert read_json(capsys)["status"] == "active"

    # list active cards
    assert main(["card", "--db", str(db), "--status", "active"]) == 0
    assert any(c["id"] != card["id"] or c["status"] == "active" for c in read_json(capsys))


def test_card_cli_split_via_edited_json(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".noname" / "harness.db"
    assert main(["init", "--db", str(db), "--root", str(root)]) == 0
    read_json(capsys)
    ids = []
    for judgement in ("克制", "可回退"):
        assert main([
            "taste-add", "--db", str(db), "--scope", "user", "--content",
            json.dumps({"judgement": judgement}),
        ]) == 0
        ids.append(read_json(capsys)["id"])
    create_args = ["card-create", "--db", str(db), "--title", "合", "--attitude", "合",
                   "--track", "authored", "--scope", "user"]
    for tid in ids:
        create_args += ["--taste-id", tid]
    assert main(create_args) == 0
    card = read_json(capsys)

    split = json.dumps({"cards": [
        {"title": "克制", "attitude": "克制", "taste_ids": [ids[0]]},
        {"title": "可回退", "attitude": "可回退", "taste_ids": [ids[1]]},
    ]})
    assert main([
        "card-review", "--db", str(db), "--card-id", card["id"],
        "--action", "split", "--reviewer", "user", "--edited", split,
    ]) == 0
    parts = read_json(capsys)
    assert len(parts) == 2
    assert all(p["status"] == "candidate" for p in parts)
