"""Live ledger reviews cross real HTTP and durable SQLite boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

from noname_harness import ledger_server
from noname_harness.cli import main as core_main
from noname_harness.codex_bridge import main as codex_main
from noname_harness.ledger_review import review_version
from noname_harness.ledger_server import create_ledger_server
from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService
from noname_harness.taste_cards import TasteCardService


@contextmanager
def running_server(db_path):
    server = create_ledger_server(db_path, port=0)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.fixture
def ledger(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    db_path = root / ".noname" / "harness.db"
    with HarnessStore(db_path) as store:
        store.initialize_project(root, "live review project")
    with running_server(db_path) as server:
        yield db_path, server


def request(server, payload=None, *, path=None, headers=None, raw=None):
    url = server.url if path is None else server.url + path
    request_headers = {}
    data = None
    if payload is not None or raw is not None:
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        request_headers = {
            "Content-Type": "application/json",
            "X-NoName-Token": server.token,
            "Origin": f"http://{urlsplit(server.url).netloc}",
        }
    if headers:
        request_headers.update(headers)
    req = Request(url, data=data, headers=request_headers)
    try:
        response = urlopen(req, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, response.read().decode("utf-8")


def proposal(db_path, *, key="storage", content=None):
    with HarnessStore(db_path) as store:
        event = store.append_event("browser-session", "codex.user", {"text": key})
        return store.create_proposal(
            "high", key, content or {"text": "Use SQLite"}, [event.id]
        )


def review_payload(record, action="accept", *, kind="state_proposal", **fields):
    return {
        "kind": kind,
        "id": record["id"],
        "action": action,
        "reviewer_id": "browser-human",
        "expected_version": review_version(record),
        **({"expected_head_id": ""} if kind == "state_proposal" else {}),
        **fields,
    }


def durable_counts(db_path):
    with HarnessStore(db_path) as store:
        return {
            name: store._connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in (
                "session_events", "state_proposals", "proposal_reviews",
                "state_revisions", "taste_records", "taste_reviews", "taste_cards",
            )
        }


def test_review_version_is_canonical_and_covers_unicode_and_history():
    record = {"content": {"text": "中文"}, "reviews": [], "id": "prp_test"}
    canonical = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    assert review_version(record) == hashlib.sha256(canonical).hexdigest()
    assert review_version(record) == review_version(dict(reversed(list(record.items()))))
    assert review_version(record) != review_version({**record, "reviews": [{"action": "defer"}]})


def test_empty_initialized_project_serves_a_page_and_updates_live(ledger):
    db_path, server = ledger
    assert urlsplit(server.url).hostname == "127.0.0.1"
    status, page = request(server)
    assert status == 200
    assert "<html" in page.lower()
    assert "live review project" in page
    candidate = proposal(db_path)
    status, updated_page = request(server)
    assert status == 200
    assert candidate["id"] in updated_page
    assert "Use SQLite" in updated_page
    with HarnessStore(db_path) as store:
        assert store.list_proposals(pending_only=True)[0]["id"] == candidate["id"]


def test_health_responds_while_page_render_is_blocked(ledger, monkeypatch):
    _, server = ledger
    entered = threading.Event()
    release = threading.Event()
    original_render = ledger_server.render_ledger_html

    def blocked_render(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5), "test did not release the blocked page render"
        return original_render(*args, **kwargs)

    monkeypatch.setattr(ledger_server, "render_ledger_html", blocked_render)
    with ThreadPoolExecutor(max_workers=1) as executor:
        page_future = executor.submit(request, server)
        try:
            assert entered.wait(timeout=2)
            status, body = request(server, path="api/health")
            assert status == 200
            assert json.loads(body) == {"ok": True}
            assert not page_future.done()
        finally:
            release.set()
        assert page_future.result(timeout=2)[0] == 200


def test_health_requires_the_local_token_path_and_does_not_write(ledger):
    db_path, server = ledger
    before = durable_counts(db_path)
    status, body = request(server, path="api/health")
    assert status == 200
    assert json.loads(body) == {"ok": True}
    assert request(server, path="api/health", headers={"Host": "attacker.example"})[0] == 403
    base = f"http://{urlsplit(server.url).netloc}/"
    for path in ("api/health", "wrong-token/api/health"):
        with pytest.raises(HTTPError) as exc:
            urlopen(base + path, timeout=2)
        assert exc.value.code == 404
        exc.value.close()
    assert durable_counts(db_path) == before


def test_foreground_server_stops_after_its_idle_timeout(ledger):
    db_path, _ = ledger
    server = create_ledger_server(db_path, port=0, idle_timeout=0.03)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        thread.join(timeout=1)
        assert not thread.is_alive(), "idle server did not shut down"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for_process_exit(pid, timeout=3):
    deadline = time.monotonic() + timeout
    pause = threading.Event()
    while process_exists(pid) and time.monotonic() < deadline:
        pause.wait(0.01)
    return not process_exists(pid)


@pytest.mark.parametrize("entrypoint", ["core", "codex"])
def test_background_cli_returns_a_ready_service_and_authenticated_stop_exits(ledger, entrypoint):
    db_path, _ = ledger
    if entrypoint == "core":
        args = [
            "-m", "noname_harness", "ledger-serve", "--db", str(db_path),
            "--background", "--idle-timeout", "3",
        ]
    else:
        args = [
            "-m", "noname_harness.codex_bridge", "--root", str(db_path.parent.parent),
            "ledger",
        ]
    result = subprocess.run(
        [sys.executable, *args], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    ready = json.loads(result.stdout)
    pid = ready["pid"]
    assert isinstance(pid, int) and pid > 0 and pid != os.getpid()
    background = SimpleNamespace(url=ready["url"], token=urlsplit(ready["url"]).path.strip("/"))
    try:
        assert urlsplit(background.url).hostname == "127.0.0.1"
        assert process_exists(pid)
        status, body = request(background, path="api/health")
        assert status == 200
        assert json.loads(body) == {"ok": True}
        assert not (db_path.parent / "ledger.html").exists()
        assert request(
            background, {}, path="api/stop", headers={"X-NoName-Token": "wrong-token"}
        )[0] == 403
        assert request(background, path="api/health")[0] == 200
        status, body = request(background, {}, path="api/stop")
        assert status == 200
        assert json.loads(body) == {"stopped": True}
        assert wait_for_process_exit(pid), "authenticated stop left the service process running"
    finally:
        if process_exists(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            assert wait_for_process_exit(pid), "failed to clean up background ledger process"


def test_codex_static_ledger_exports_a_snapshot_without_starting_a_service(ledger, monkeypatch, capsys):
    db_path, _ = ledger

    def unexpected_service(*args, **kwargs):
        pytest.fail("static export attempted to start a local HTTP service")

    monkeypatch.setattr(ledger_server, "start_background_ledger", unexpected_service)
    monkeypatch.setattr(ledger_server, "create_ledger_server", unexpected_service)
    assert codex_main(["--root", str(db_path.parent.parent), "ledger", "--static"]) == 0
    result = json.loads(capsys.readouterr().out)
    destination = db_path.parent / "ledger.html"
    assert Path(result["path"]) == destination
    assert destination.is_file()
    assert "<html" in destination.read_text(encoding="utf-8").lower()
    assert "url" not in result and "pid" not in result


@pytest.mark.parametrize("options", [
    ["--idle-timeout", "nan"],
    ["--idle-timeout", "inf"],
    ["--port", "-1"],
    ["--port", "65536"],
])
def test_invalid_background_cli_options_fail_before_spawning(ledger, monkeypatch, capsys, options):
    db_path, _ = ledger
    before = durable_counts(db_path)

    def unexpected_spawn(*args, **kwargs):
        pytest.fail("invalid service options spawned a child process")

    monkeypatch.setattr(ledger_server.subprocess, "Popen", unexpected_spawn)
    assert core_main(["ledger-serve", "--db", str(db_path), "--background", *options]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err
    assert durable_counts(db_path) == before


@pytest.mark.parametrize("action", ["accept", "reject", "defer", "edit"])
def test_state_review_persists_the_explicit_human_decision(ledger, action):
    db_path, server = ledger
    candidate = proposal(db_path)
    fields = {"reason": "Reviewed against the source"}
    if action == "edit":
        fields["edited_content"] = {"text": "Use SQLite with WAL"}
    status, _ = request(
        server, review_payload(candidate, action, **fields), path="api/review"
    )
    assert status == 200
    with HarnessStore(db_path) as store:
        reviewed = store.get_proposal(candidate["id"])
        assert len(reviewed["reviews"]) == 1
        review = reviewed["reviews"][0]
        assert review["action"] == action
        assert review["reviewer_id"] == "browser-human"
        assert review["reason"] == fields["reason"]
        active = store.active_state("high")
        if action in {"accept", "edit"}:
            assert len(active) == 1
            assert active[0]["content"] == fields.get("edited_content", candidate["content"])
        else:
            assert active == []
        assert bool(store.list_proposals(pending_only=True)) == (action == "defer")
        assert store.verify_integrity()["ok"] is True


def test_deferred_candidate_requires_a_fresh_version_before_acceptance(ledger):
    db_path, server = ledger
    candidate = proposal(db_path)
    assert request(server, review_payload(candidate, "defer"), path="api/review")[0] == 200
    before = durable_counts(db_path)
    assert request(server, review_payload(candidate), path="api/review")[0] == 409
    assert durable_counts(db_path) == before
    with HarnessStore(db_path) as store:
        refreshed = store.get_proposal(candidate["id"])
    assert request(server, review_payload(refreshed), path="api/review")[0] == 200
    with HarnessStore(db_path) as store:
        assert [r["action"] for r in store.get_proposal(candidate["id"])["reviews"]] == ["defer", "accept"]


@pytest.mark.parametrize("action", ["accept", "defer"])
@pytest.mark.parametrize("separate_servers", [False, True])
def test_same_version_concurrent_reviews_write_once(ledger, action, separate_servers):
    db_path, server = ledger
    candidate = proposal(db_path)
    payload = review_payload(candidate, action)
    barrier = threading.Barrier(2)

    def submit(target):
        barrier.wait(timeout=5)
        return request(target, payload, path="api/review")[0]

    @contextmanager
    def targets():
        if separate_servers:
            with running_server(db_path) as other:
                yield [server, other]
        else:
            yield [server, server]

    with targets() as servers, ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(submit, servers))
    assert sorted(statuses) == [200, 409]
    with HarnessStore(db_path) as store:
        reviews = store.get_proposal(candidate["id"])["reviews"]
        assert len(reviews) == 1
        assert reviews[0]["action"] == action
        assert len(store.active_state("high")) == (1 if action == "accept" else 0)


@pytest.mark.parametrize("headers", [
    {"X-NoName-Token": "wrong-token"},
    {"X-NoName-Token": "\u00e9"},
    {"X-NoName-Token": ""},
    {"Origin": "https://attacker.example"},
    {"Origin": "null"},
    {"Origin": ""},
    {"Host": "attacker.example"},
    {"Host": "127.0.0.1:1"},
])
def test_bad_token_origin_or_host_never_writes(ledger, headers):
    db_path, server = ledger
    candidate = proposal(db_path)
    before = durable_counts(db_path)
    status, _ = request(
        server, review_payload(candidate), path="api/review", headers=headers
    )
    assert status in {403, 404}
    assert durable_counts(db_path) == before


def test_page_and_review_route_require_the_token_path(ledger):
    db_path, server = ledger
    candidate = proposal(db_path)
    before = durable_counts(db_path)
    base = f"http://{urlsplit(server.url).netloc}/"
    for route in ("", "wrong-token/", "api/review"):
        req = Request(base + route)
        with pytest.raises(HTTPError) as exc:
            urlopen(req, timeout=5)
        assert exc.value.code in {403, 404}
        assert candidate["id"] not in exc.value.read().decode("utf-8")
        exc.value.close()
    assert durable_counts(db_path) == before


@pytest.mark.parametrize("override", [
    {"reviewer_id": ""},
    {"reviewer_id": "  "},
    {"reviewer_id": 1},
    {"kind": "unknown_kind"},
    {"kind": ["state_proposal"]},
    {"action": "unknown_action"},
    {"action": ["accept"]},
    {"action": "retire"},
    {"id": ["prp_fake"]},
    {"reason": {"text": "not a reason string"}},
    {"action": "edit"},
])
def test_invalid_review_inputs_are_400_without_side_effects(ledger, override):
    db_path, server = ledger
    candidate = proposal(db_path)
    before = durable_counts(db_path)
    payload = review_payload(candidate, **override)
    assert request(server, payload, path="api/review")[0] == 400
    assert durable_counts(db_path) == before


@pytest.mark.parametrize("raw", [b"{", b"[]", b"null", b'"accept"', b"\xff"])
def test_malformed_or_non_object_json_never_writes(ledger, raw):
    db_path, server = ledger
    proposal(db_path)
    before = durable_counts(db_path)
    assert request(server, path="api/review", raw=raw)[0] == 400
    assert durable_counts(db_path) == before


def test_unknown_candidate_is_404_without_writes(ledger):
    db_path, server = ledger
    candidate = proposal(db_path)
    before = durable_counts(db_path)
    payload = review_payload(candidate, id="prp_unknown")
    assert request(server, payload, path="api/review")[0] == 404
    assert durable_counts(db_path) == before


@pytest.mark.parametrize("version", [None, "", "0" * 64])
def test_missing_or_mismatched_version_is_409_without_writes(ledger, version):
    db_path, server = ledger
    candidate = proposal(db_path)
    payload = review_payload(candidate)
    if version is None:
        del payload["expected_version"]
    else:
        payload["expected_version"] = version
    before = durable_counts(db_path)
    assert request(server, payload, path="api/review")[0] == 409
    assert durable_counts(db_path) == before


@pytest.mark.parametrize("head", ["missing", None, 1, [], {}])
def test_missing_or_non_string_state_head_is_409_without_writes(ledger, head):
    db_path, server = ledger
    candidate = proposal(db_path)
    payload = review_payload(candidate)
    if head == "missing":
        del payload["expected_head_id"]
    else:
        payload["expected_head_id"] = head
    before = durable_counts(db_path)
    assert request(server, payload, path="api/review")[0] == 409
    assert durable_counts(db_path) == before


@pytest.mark.parametrize("initial_head", [False, True])
def test_competing_proposal_changed_head_blocks_an_unchanged_candidate(ledger, initial_head):
    db_path, server = ledger
    initial_head_id = ""
    if initial_head:
        baseline = proposal(db_path, content={"text": "Use local files"})
        with HarnessStore(db_path) as store:
            store.review_proposal(baseline["id"], "accept", "cli-human")
            initial_head_id = store.active_state("high")[0]["id"]
    candidate = proposal(db_path, content={"text": "Use SQLite"})
    open_browser_payload = review_payload(candidate, expected_head_id=initial_head_id)
    competing = proposal(db_path, content={"text": "Use PostgreSQL"})
    with HarnessStore(db_path) as store:
        store.review_proposal(competing["id"], "accept", "cli-human")
        new_head = store.active_state("high")[0]
        # The candidate token alone cannot detect a decision on another proposal.
        assert review_version(store.get_proposal(candidate["id"])) == open_browser_payload["expected_version"]
    before = durable_counts(db_path)
    assert request(server, open_browser_payload, path="api/review")[0] == 409
    assert durable_counts(db_path) == before
    with HarnessStore(db_path) as store:
        assert store.active_state("high")[0] == new_head
        assert store.get_proposal(candidate["id"])["reviews"] == []
    # A refreshed page can submit a new decision against the actual current head.
    refreshed_payload = review_payload(candidate, expected_head_id=new_head["id"])
    assert request(server, refreshed_payload, path="api/review")[0] == 200
    with HarnessStore(db_path) as store:
        assert store.active_state("high")[0]["content"] == candidate["content"]


def test_different_proposals_racing_the_same_state_head_cannot_both_replace_it(ledger):
    db_path, server = ledger
    candidates = [
        proposal(db_path, content={"text": "Use SQLite"}),
        proposal(db_path, content={"text": "Use PostgreSQL"}),
    ]
    barrier = threading.Barrier(2)

    def submit(target_and_candidate):
        target, candidate = target_and_candidate
        barrier.wait(timeout=5)
        return request(target, review_payload(candidate), path="api/review")[0]

    with running_server(db_path) as other, ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(submit, zip([server, other], candidates)))
    assert sorted(statuses) == [200, 409]
    winner = candidates[statuses.index(200)]
    loser = candidates[statuses.index(409)]
    with HarnessStore(db_path) as store:
        assert len(store.get_proposal(winner["id"])["reviews"]) == 1
        assert store.get_proposal(loser["id"])["reviews"] == []
        assert store.active_state("high")[0]["content"] == winner["content"]


def test_external_cli_review_invalidates_an_open_browser_decision(ledger):
    db_path, server = ledger
    candidate = proposal(db_path)
    result = subprocess.run(
        [
            sys.executable, "-m", "noname_harness", "review", "--db", str(db_path),
            "--proposal-id", candidate["id"], "--action", "defer",
            "--reviewer", "cli-human", "--reason", "Need more source evidence",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    before = durable_counts(db_path)
    assert request(server, review_payload(candidate), path="api/review")[0] == 409
    assert durable_counts(db_path) == before
    with HarnessStore(db_path) as store:
        assert store.get_proposal(candidate["id"])["reviews"][0]["reviewer_id"] == "cli-human"
        assert store.active_state("high") == []


def test_already_decided_proposal_cannot_be_reopened_with_a_fresh_version(ledger):
    db_path, server = ledger
    candidate = proposal(db_path)
    assert request(server, review_payload(candidate), path="api/review")[0] == 200
    with HarnessStore(db_path) as store:
        fresh_record = store.get_proposal(candidate["id"])
    before = durable_counts(db_path)
    assert request(server, review_payload(fresh_record, "defer"), path="api/review")[0] == 409
    assert durable_counts(db_path) == before


@pytest.mark.parametrize("action", ["adopt", "retire"])
def test_taste_candidate_reviews_create_a_persisted_head(ledger, action):
    db_path, server = ledger
    with HarnessStore(db_path) as store:
        event = store.append_event("browser-session", "codex.assistant", {"text": "A clear explanation"})
        candidate = TasteService(store).propose_adopted(
            {"attitude": "Prefer precise explanations"}, source_event_ids=[event.id]
        )
    payload = review_payload(candidate, action, kind="taste_candidate", reason="Explicit human decision")
    assert request(server, payload, path="api/review")[0] == 200
    with HarnessStore(db_path) as store:
        tastes = TasteService(store)
        assert tastes.pending() == []
        heads = tastes.active() if action == "adopt" else tastes.by_status("retired")
        assert len(heads) == 1
        head = heads[0]
        assert head["supersedes_id"] == candidate["id"]
        assert head["content"] == candidate["content"]
        assert head["reviews"][0]["reviewer_id"] == "browser-human"
        assert head["reviews"][0]["reason"] == "Explicit human decision"
        assert store.active_state("high") == []
        assert store.verify_integrity()["ok"] is True
    before = durable_counts(db_path)
    assert request(server, payload, path="api/review")[0] == 409
    assert durable_counts(db_path) == before


@pytest.mark.parametrize("action", ["accept", "retire", "edit"])
def test_taste_card_candidate_reviews_preserve_versioned_cards(ledger, action):
    db_path, server = ledger
    with HarnessStore(db_path) as store:
        taste = TasteService(store).record_authored({"attitude": "Clear tools"})
        candidate = TasteCardService(store).create_card(
            title="Clarity", attitude="Prefer clear tools", track="authored",
            scope="user", taste_ids=[taste["id"]],
        )
    fields = {"edited_content": {"title": "Careful clarity", "attitude": "Clear, careful tools"}} if action == "edit" else {}
    reason = "Confirmed against the original taste evidence"
    payload = review_payload(candidate, action, kind="taste_card_candidate", reason=reason, **fields)
    assert request(server, payload, path="api/review")[0] == 200
    with HarnessStore(db_path) as store:
        cards = TasteCardService(store)
        expected_status = {"accept": "active", "retire": "retired", "edit": "candidate"}[action]
        heads = cards.by_status(expected_status)
        assert len(heads) == 1
        head = heads[0]
        assert head["id"] != candidate["id"]
        assert head["supersedes_id"] == candidate["id"]
        assert head["actor_id"] == "browser-human"
        assert head["taste_ids"] == [taste["id"]]
        if action == "edit":
            assert head["title"] == "Careful clarity"
            assert head["attitude"] == "Clear, careful tools"
        assert cards.get(candidate["id"])["title"] == "Clarity"
        audit_events = [
            event for event in store.list_events("system", limit=50)
            if event.event_type == "taste.card.reviewed"
            and event.payload["card_id"] == candidate["id"]
        ]
        assert len(audit_events) == 1
        assert audit_events[0].payload["new_card_id"] == head["id"]
        assert audit_events[0].payload["action"] == action
        assert audit_events[0].payload["reviewer_id"] == "browser-human"
        assert audit_events[0].payload["reason"] == reason
        assert store.verify_integrity()["ok"] is True
    before = durable_counts(db_path)
    assert request(server, payload, path="api/review")[0] == 409
    assert durable_counts(db_path) == before


def test_changed_taste_evidence_invalidates_the_open_card_version(ledger):
    db_path, server = ledger
    with HarnessStore(db_path) as store:
        taste = TasteService(store).record_authored({"attitude": "Clear tools"})
        candidate = TasteCardService(store).create_card(
            title="Clarity", attitude="Prefer clear tools", track="authored",
            scope="user", taste_ids=[taste["id"]],
        )
        assert candidate["stale"] is False
        TasteService(store).review(taste["id"], "retire", "cli-human")
        assert TasteCardService(store).get(candidate["id"])["stale"] is True
    before = durable_counts(db_path)
    assert request(
        server, review_payload(candidate, kind="taste_card_candidate"), path="api/review"
    )[0] == 409
    assert durable_counts(db_path) == before


@pytest.mark.parametrize("transition", ["retire", "resume"])
def test_already_stale_card_rejects_changed_taste_evidence_and_current_heads(ledger, transition):
    db_path, server = ledger
    with HarnessStore(db_path) as store:
        tastes = TasteService(store)
        taste_a = tastes.record_authored({"attitude": "Clear tools"})
        taste_b = tastes.record_authored({"attitude": "Careful decisions"})
        cards = TasteCardService(store)
        created = cards.create_card(
            title="Clarity", attitude="Prefer clear and careful tools", track="authored",
            scope="user", taste_ids=[taste_a["id"], taste_b["id"]],
        )
        tastes.review(taste_a["id"], "retire", "cli-human")
        if transition == "resume":
            paused_b = tastes.review(taste_b["id"], "pause", "cli-human")
        browser_snapshot = cards.get(created["id"])
        original_b_at_snapshot = tastes.get(taste_b["id"])
        assert browser_snapshot["stale"] is True
        if transition == "resume":
            tastes.review(paused_b["id"], "resume", "cli-human")
            # This review applies to the child: hashing the original evidence
            # alone does not detect the current head's changed status.
            assert tastes.get(taste_b["id"]) == original_b_at_snapshot
        else:
            tastes.review(taste_b["id"], "retire", "cli-human")
        updated = cards.get(created["id"])
        assert updated["stale"] is True
        assert {
            key: value for key, value in browser_snapshot.items() if key != "taste_versions"
        } == {key: value for key, value in updated.items() if key != "taste_versions"}
        assert browser_snapshot["taste_versions"][taste_b["id"]]["head"] != updated["taste_versions"][taste_b["id"]]["head"]
        assert review_version(browser_snapshot) != review_version(updated)
    before = durable_counts(db_path)
    assert request(
        server, review_payload(browser_snapshot, kind="taste_card_candidate"), path="api/review"
    )[0] == 409
    assert durable_counts(db_path) == before
    with HarnessStore(db_path) as store:
        assert TasteCardService(store).get(created["id"])["status"] == "candidate"


@pytest.mark.parametrize("edited", [
    {"title": None},
    {"title": 3},
    {"title": []},
    {"attitude": {}},
    {"tensions": []},
])
def test_malformed_card_edit_is_400_without_writes(ledger, edited):
    db_path, server = ledger
    with HarnessStore(db_path) as store:
        taste = TasteService(store).record_authored({"attitude": "Clear tools"})
        candidate = TasteCardService(store).create_card(
            title="Clarity", attitude="Prefer clear tools", track="authored",
            scope="user", taste_ids=[taste["id"]],
        )
    before = durable_counts(db_path)
    assert request(
        server, review_payload(candidate, "edit", kind="taste_card_candidate", edited_content=edited),
        path="api/review",
    )[0] == 400
    assert durable_counts(db_path) == before


def test_updates_are_read_only_authorized_and_detect_review_without_new_dialogue(ledger):
    db_path, server = ledger
    status, body = request(server, path="api/updates")
    initial = json.loads(body)["revision"]
    assert status == 200 and len(initial) == 64
    before = durable_counts(db_path)
    last_activity = server.last_activity
    assert request(server, path="api/updates")[0] == 200
    assert durable_counts(db_path) == before
    assert server.last_activity == last_activity
    candidate = proposal(db_path)
    after_proposal = json.loads(request(server, path="api/updates")[1])["revision"]
    assert after_proposal != initial
    with HarnessStore(db_path) as store:
        dialogue_count = sum(event.event_type == "codex.user" for event in store.list_events())
        store.review_proposal(candidate["id"], "defer", "isolated-test-user")
        assert sum(event.event_type == "codex.user" for event in store.list_events()) == dialogue_count
    after_defer = json.loads(request(server, path="api/updates")[1])["revision"]
    assert after_defer != after_proposal
    assert request(server, path="api/updates", headers={"Host": "attacker.example"})[0] == 403
    url = server.origin + "/api/updates"
    with pytest.raises(HTTPError) as error:
        urlopen(url, timeout=2)
    assert error.value.code == 404
    status, page = request(server)
    assert status == 200 and f'data-ledger-revision="{after_defer}"' in page


def test_updates_detect_activation_and_expiry_without_writes(ledger, monkeypatch):
    from datetime import datetime, timezone
    db_path, server = ledger
    candidate = proposal(db_path)
    with HarnessStore(db_path) as store:
        store.review_proposal(candidate["id"], "accept", "isolated-test-user",
            valid_from="2030-01-01T00:00:00Z", valid_to="2030-01-02T00:00:00Z")
    before = durable_counts(db_path)
    clock = ["2029-12-31T23:59:59Z"]

    class ControlledDateTime:
        @classmethod
        def now(cls, tz):
            return datetime.fromisoformat(clock[0].replace("Z", "+00:00")).astimezone(tz)

    monkeypatch.setattr(ledger_server, "datetime", ControlledDateTime)
    def revision():
        status, body = request(server, path="api/updates")
        assert status == 200
        return json.loads(body)["revision"]

    pending = revision()
    with HarnessStore(db_path) as store:
        assert not store.active_state(as_of=clock[0])
    clock[0] = "2030-01-01T00:00:00Z"
    active = revision()
    assert active != pending
    with HarnessStore(db_path) as store:
        assert len(store.active_state(as_of=clock[0])) == 1
    clock[0] = "2030-01-02T00:00:00Z"
    assert revision() == active  # valid_to is inclusive, matching the kernel.
    clock[0] = "2030-01-02T00:00:01Z"
    assert revision() != active
    with HarnessStore(db_path) as store:
        assert not store.active_state(as_of=clock[0])
    assert durable_counts(db_path) == before
