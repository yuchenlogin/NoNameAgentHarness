"""Regression tests for the three defects found by the DSH plugin acceptance run.

The run drove the 12 model-visible `noname_*` tools on a live DeepSeek Harness
profile and found:

1. `search_events` returned ``[]`` for Chinese substrings the caller had just
   written -- the ledger looked empty when it was full.
2. `verify_integrity` hashed only ``{event_type, payload}``, so rewriting
   ``session_id``/``seq`` -- the provenance fields this ledger exists to
   guarantee -- still verified as ``ok: true``.
3. An event with no payload content and no evidence was accepted silently.

Each test below fails against the pre-fix code.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from noname_harness.models import EvidenceInput
from noname_harness.store import SCHEMA_VERSION, HarnessStore


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "regression project")
    return store, root


def _legacy_event_hash(event_type: str, payload) -> str:
    """The v1 formula, pinned.  Old ledgers must keep verifying under it."""

    material = json.dumps(
        {"event_type": event_type, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# --- 1. CJK substring search -------------------------------------------------


def test_chinese_substring_query_finds_the_event_that_was_just_written(tmp_path):
    """The exact live failure: searching a word you just recorded returned []."""

    store, _ = make_store(tmp_path)
    try:
        event = store.append_event(
            "s",
            "decision",
            {"text": "插件验收测试启动：验证 12 个模型可见工具。"},
        )
        # The whole token, the prefix of the token, and an interior substring
        # must all find it.  Pre-fix only the first returned anything.
        for query in ("插件验收测试启动", "插件验收测试", "验收测试", "模型可见工具"):
            assert [hit.id for hit in store.search_events(query)] == [event.id], query
    finally:
        store.close()


def test_chinese_search_reads_evidence_as_well_as_payload(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        event = store.append_event(
            "s",
            "finding",
            {"text": "english payload"},
            [EvidenceInput("账本完整性校验失败", None, None, None)],
        )
        assert [hit.id for hit in store.search_events("完整性")] == [event.id]
    finally:
        store.close()


def test_chinese_multi_term_query_requires_every_term(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        both = store.append_event("s", "note", {"text": "账本 的 粒度 是 工作区"})
        only_one = store.append_event("s", "note", {"text": "账本 只有 一个 词"})
        hits = {hit.id for hit in store.search_events("账本 粒度")}
        assert both.id in hits
        assert only_one.id not in hits
    finally:
        store.close()


def test_non_cjk_search_keeps_fts_token_semantics(tmp_path):
    """ASCII queries still go through FTS: an unrelated query stays empty."""

    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "expected 3"})
        assert store.search_events("expected 3")
        assert store.search_events("nothing-here-at-all") == []
    finally:
        store.close()


def test_like_wildcards_in_a_query_are_literal(tmp_path):
    """`100%` means the text "100%", not "match everything"."""

    store, _ = make_store(tmp_path)
    try:
        percent = store.append_event("s", "note", {"text": "进度 100% 完成"})
        unrelated = store.append_event("s", "note", {"text": "完全无关的一条"})
        assert [hit.id for hit in store.search_events("100%")] == [percent.id]

        # A lone "%" is a literal character: it may only match rows that really
        # contain one, never the whole ledger.  (The FTS path tokenizes "%" away
        # entirely, so the invariant asserted here is the safe direction: the
        # unrelated row is never returned, on either path.)
        assert unrelated.id not in {hit.id for hit in store.search_events("%")}
        assert store.search_events("_") == []

        # Same guarantee on the non-FTS path, where "%" is a literal substring.
        store._fts_available = False
        assert [hit.id for hit in store.search_events("100%")] == [percent.id]
        assert [hit.id for hit in store.search_events("%")] == [percent.id]
        assert store.search_events("_") == []
    finally:
        store.close()


# --- 2. provenance is signed ------------------------------------------------


def _drop_append_only_triggers(store: HarnessStore) -> None:
    """Model an attacker who has the file and removes the in-database guard.

    The triggers are the first line of defence; the hash is the second, and it
    has to hold on its own once the first is gone.
    """

    store._connection.executescript(
        """
        DROP TRIGGER IF EXISTS session_events_append_only_update;
        DROP TRIGGER IF EXISTS session_events_append_only_delete;
        DROP TRIGGER IF EXISTS evidence_spans_append_only_update;
        DROP TRIGGER IF EXISTS evidence_spans_append_only_delete;
        """
    )
    store._connection.commit()


@pytest.mark.parametrize(
    "column, value",
    [
        ("session_id", "session-FORGED"),
        ("seq", 9999),
        ("occurred_at", "1999-01-01T00:00:00Z"),
        ("id", "evt_forged000000000000000000000000"),
    ],
)
def test_verify_detects_forged_event_provenance(tmp_path, column, value):
    """Pre-fix, every one of these rewrites verified as ok: true."""

    store, _ = make_store(tmp_path)
    try:
        event = store.append_event("s", "note", {"text": "original"})
        assert store.verify_integrity()["ok"] is True

        _drop_append_only_triggers(store)
        store._connection.execute(
            f"UPDATE session_events SET {column} = ? WHERE id = ?", (value, event.id)
        )
        store._connection.commit()

        report = store.verify_integrity()
        assert report["ok"] is False, f"forging {column} went undetected"
        assert event.id in report["bad_event_ids"] or column == "id"
    finally:
        store.close()


def test_verify_detects_forged_evidence_provenance(tmp_path):
    """artifact_uri / offsets were outside the v1 evidence hash too."""

    store, _ = make_store(tmp_path)
    try:
        store.append_event(
            "s",
            "note",
            {"text": "with evidence"},
            [EvidenceInput("the quoted span", "file://src/a.py", 0, 14)],
        )
        evidence_id = store._connection.execute(
            "SELECT id FROM evidence_spans"
        ).fetchone()["id"]
        assert store.verify_integrity()["ok"] is True

        _drop_append_only_triggers(store)
        store._connection.execute(
            "UPDATE evidence_spans SET artifact_uri = 'file://etc/passwd' WHERE id = ?",
            (evidence_id,),
        )
        store._connection.commit()

        report = store.verify_integrity()
        assert report["ok"] is False
        assert evidence_id in report["bad_evidence_ids"]
    finally:
        store.close()


def test_forged_payload_is_still_detected(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        event = store.append_event("s", "note", {"text": "original"})
        _drop_append_only_triggers(store)
        store._connection.execute(
            "UPDATE session_events SET payload_json = ? WHERE id = ?",
            ('{"text":"tampered"}', event.id),
        )
        store._connection.commit()
        report = store.verify_integrity()
        assert report["ok"] is False
        assert event.id in report["bad_event_ids"]
    finally:
        store.close()


def test_unknown_hash_version_is_unverified_never_ok(tmp_path):
    """A row from a future build must not read as valid on an older binary."""

    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "from the future"})
        _drop_append_only_triggers(store)
        store._connection.execute("UPDATE session_events SET hash_version = 99")
        store._connection.commit()

        report = store.verify_integrity()
        assert report["ok"] is False
        assert report["bad_event_ids"] == []
        assert len(report["unverified_event_ids"]) == 1
    finally:
        store.close()


def test_full_hash_covers_every_append_only_column(tmp_path):
    """Guard the guard: every column of the row must change the hash."""

    from noname_harness.store import _event_hash, HASH_VERSION_FULL

    store, _ = make_store(tmp_path)
    try:
        store.append_event("s", "note", {"text": "original"})
        row = store._connection.execute("SELECT * FROM session_events").fetchone()
        baseline = _event_hash(HASH_VERSION_FULL, row)
        assert baseline == row["content_hash"]
        for column in ("id", "session_id", "seq", "event_type", "payload_json", "occurred_at"):
            mutated = {key: row[key] for key in row.keys()}
            if column == "seq":
                mutated[column] = row["seq"] + 1
            elif column == "payload_json":
                mutated[column] = '{"text":"mutated"}'
            else:
                mutated[column] = "mutated"
            assert _event_hash(HASH_VERSION_FULL, mutated) != baseline, column
    finally:
        store.close()


# --- 3. empty events are refused --------------------------------------------


@pytest.mark.parametrize(
    "payload", [{}, {"text": ""}, {"text": "   "}, {"nested": {"text": ""}}, [], None, ""]
)
def test_empty_payload_without_evidence_is_refused(tmp_path, payload):
    store, _ = make_store(tmp_path)
    try:
        with pytest.raises(ValueError):
            store.append_event("s", "note", payload)
        assert store.list_events("s") == []
        assert store.verify_integrity()["ok"] is True
    finally:
        store.close()


def test_empty_payload_with_evidence_is_still_accepted(tmp_path):
    """Evidence *is* content; the guard must not reject a cited span."""

    store, _ = make_store(tmp_path)
    try:
        event = store.append_event("s", "note", {}, [EvidenceInput("cited text")])
        assert store.evidence_for_event(event.id)
    finally:
        store.close()


def test_zero_and_false_are_real_content(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        zero = store.append_event("s", "metric", {"count": 0})
        flag = store.append_event("s", "flag", {"enabled": False})
        assert store.list_events("s", limit=10)
        assert zero.id and flag.id
    finally:
        store.close()


# --- 4. the v7 -> v8 migration ----------------------------------------------


def test_v7_database_migrates_and_keeps_old_rows_verifiable(tmp_path):
    """Legacy rows stay v1 (append-only: never rewritten) and still verify."""

    store, root = make_store(tmp_path)
    db_path = root / ".noname" / "harness.db"
    store.append_event("s", "note", {"text": "written before v8"})
    store.append_event("s", "note", {"text": "second"}, [EvidenceInput("span text")])
    store.close()

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        DROP TRIGGER IF EXISTS session_events_append_only_update;
        DROP TRIGGER IF EXISTS session_events_append_only_delete;
        DROP TRIGGER IF EXISTS evidence_spans_append_only_update;
        DROP TRIGGER IF EXISTS evidence_spans_append_only_delete;
        """
    )
    # Rewind the file to a genuine v7 shape: no hash_version column, and the
    # v1 hashes that v7 would have written for every row.
    connection.execute("ALTER TABLE session_events DROP COLUMN hash_version")
    connection.execute("ALTER TABLE evidence_spans DROP COLUMN hash_version")
    for row in connection.execute("SELECT id, event_type, payload_json FROM session_events"):
        connection.execute(
            "UPDATE session_events SET content_hash = ? WHERE id = ?",
            (_legacy_event_hash(row["event_type"], json.loads(row["payload_json"])), row["id"]),
        )
    for row in connection.execute("SELECT id, content FROM evidence_spans"):
        connection.execute(
            "UPDATE evidence_spans SET content_hash = ? WHERE id = ?",
            (hashlib.sha256(row["content"].encode("utf-8")).hexdigest(), row["id"]),
        )
    connection.execute("UPDATE harness_meta SET value = '7' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()

    upgraded = HarnessStore(db_path)
    try:
        version = upgraded._connection.execute(
            "SELECT value FROM harness_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        assert version == str(SCHEMA_VERSION)

        report = upgraded.verify_integrity()
        assert report["ok"] is True, report
        assert report["hash_coverage"]["events"]["v1_payload_only"] == 2
        assert report["hash_coverage"]["events"]["v2_full_provenance"] == 0
        assert report["hash_coverage"]["evidence"]["v1_content_only"] == 1

        # New rows in the upgraded file are signed under v2.
        upgraded.append_event("s", "note", {"text": "written after v8"})
        report = upgraded.verify_integrity()
        assert report["ok"] is True
        assert report["hash_coverage"]["events"]["v2_full_provenance"] == 1
        assert report["hash_coverage"]["events"]["v1_payload_only"] == 2
    finally:
        upgraded.close()
