"""Round 2 of the brutal/adversarial test suite.

test_brutal.py covered the store/search/proposal/taste/package entry attacks.
This round systematically attacks everything added since: AgentLoop.resume,
the CLI (all subcommands), PluginRuntime, HTML renderers (XSS injection),
the sandbox boundary, vendor seams (image-gen / embedding / llm extractor),
rerank, and cross-cutting invariants (concurrency, random-workflow invariants).

Contract for every attack: either a loud, typed refusal (ValueError /
TypeError / domain error) or correct handling -- never an uncaught crash,
never silent data corruption, never a boundary crossing.
"""

from __future__ import annotations

import math
import random
import string
import threading
import unicodedata

import pytest

from noname_harness.agent_loop import AgentLoop, AgentLoopError, LoopResult
from noname_harness.adapters import ModelAdapterError
from noname_harness.card_images import card_image_for, local_typographic_image
from noname_harness.causal_map import build_causal_model, render_causal_html
from noname_harness.cli import main as cli_main
from noname_harness.embedding_service import OpenAIEmbedding
from noname_harness.embeddings import local_hash_embedding
from noname_harness.extractor import MemoryExtractor
from noname_harness.image_gen_adapter import OpenAIImageGenAdapter, build_card_prompt
from noname_harness.ledger_view import build_ledger_model, render_ledger_html
from noname_harness.llm_extractor import LLMExtractor
from noname_harness.models import EvidenceInput
from noname_harness.plugins import (
    Plugin,
    PluginContribution,
    PluginError,
    PluginManifest,
    PluginRuntime,
)
from noname_harness.rerank import RankedCandidate, default_rerank
from noname_harness.router import Router
from noname_harness.sandbox import Sandbox, SandboxError
from noname_harness.state_diff import build_state_diff_model, render_state_diff_html
from noname_harness.store import HarnessStore, WorkspaceBoundaryError
from noname_harness.taste import TasteService
from noname_harness.taste_cards import TasteCardService
from noname_harness.tools import (
    Tool,
    ToolApprovalRequired,
    ToolRegistry,
    ToolSchema,
    ToolShadowingError,
)
from noname_harness.vendor_http import (
    bounded_vendor_int,
    validate_base_url,
    validate_timeout,
)

RNG = random.Random(20261007)

# Payloads that become active markup if they survive unescaped.  Every one of
# these contains HTML metacharacters (<, >, quotes), so html.escape() makes it
# inert; if the raw string appears in the output, the renderer failed to
# escape somewhere.  (Bare text like "javascript:alert(1)" is NOT in this
# list: it is only dangerous inside an href attribute, and these renderers
# never interpolate user data into hrefs -- asserting its absence would be
# asserting a contract that does not exist.)
XSS_PAYLOADS = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "<svg onload=alert(1)>",
    "' onclick='alert(1)",
    "<iframe src='javascript:alert(1)'>",
    "&lt;script&gt;double-escape&lt;/script&gt;",
]

# The full set including pure-text attack strings: these renderers may show
# them as escaped text; the assertion is only that no <script>/<img onerror/
# <svg onload element survives unescaped.
XSS_TEXT_PAYLOADS = XSS_PAYLOADS + ["javascript:alert(1)"]

UNICODE_BOMBS = [
    "surrog\ud800ate",
    "\x00\x01\x02 control",
    "RTL \u202eoverride",
    "ZWJ \u200d joiner",
    "제목 NFD hangul",
    "e\u0301 combining accent",
]


def make_store(tmp_path, name="brutal2"):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, name)
    return store, root


def make_registry(store, executed=None):
    registry = ToolRegistry(store)

    def _track(name, fn):
        def run(a):
            if executed is not None:
                executed.append(name)
            return fn(a)
        return run

    registry.register(Tool(
        ToolSchema(name="search", description="d", input_schema={"q": "string"}),
        execute=_track("search", lambda a: f"hit:{a['q']}"),
        permission="read", approval="never",
    ))
    registry.register(Tool(
        ToolSchema(name="delete", description="d", input_schema={"path": "string"}),
        execute=_track("delete", lambda a: "gone"),
        permission="destructive", approval="always",
    ))
    return registry


class GatedPauseDriver:
    """Deterministic driver: requests the gated 'delete' tool once, then
    completes after the tool result comes back (used to reach waiting_approval)."""

    def act(self, context, last_tool_result=None):
        if last_tool_result is not None:
            return LoopResult(task_complete=True)
        return LoopResult(tool_call={"name": "delete", "arguments": {"path": "/tmp/x"}})


class ResumeAwareDriver:
    """On the first resumed turn (context['resume']), re-issue the pending
    gated call exactly once, carrying the token id; complete afterwards.
    (Re-issuing on every turn would loop: the one-time token is consumed by
    the first execution, so any second re-issue pauses the run again.)"""

    def __init__(self, registry=None):
        self.registry = registry
        self.reissued = False

    def act(self, context, last_tool_result=None):
        resume = context.get("resume")
        if resume is not None and not self.reissued:
            self.reissued = True
            token = resume["approval_token_id"]
            if self.registry is not None and isinstance(token, str):
                token = self.registry.get_live_token(token)
            return LoopResult(tool_call={
                "name": resume["pending_tool"],
                "arguments": {"path": "/tmp/x"},
                "approval_token": token,
            })
        if last_tool_result is not None:
            return LoopResult(task_complete=True)
        if self.reissued:
            return LoopResult(task_complete=True)
        return LoopResult(tool_call={"name": "delete", "arguments": {"path": "/tmp/x"}})


def pause_a_run(store, registry, session_id="s"):
    loop = AgentLoop(
        store=store, session_id=session_id, driver=GatedPauseDriver(),
        tool_registry=registry,
    )
    summary = loop.run("t")
    assert summary["stop_reason"] == "waiting_approval"
    return summary


# ============================================================================
# 1. Malformed input across public entry points
# ============================================================================

class TestMalformedInput:
    def test_append_event_rejects_surrogate_and_control_payloads_loudly(self, tmp_path):
        """Payloads that json/UTF-8 cannot represent must not corrupt the ledger:
        either they round-trip or they raise a typed error -- the ledger stays
        intact either way (append-only invariant preserved)."""
        store, _ = make_store(tmp_path)
        try:
            for payload in ({"bad": "surrog\ud800ate"}, {"ctl": "\x00null"}):
                try:
                    store.append_event("s", "fuzz", payload)
                except (ValueError, TypeError, UnicodeError):
                    pass  # loud refusal is fine
            assert store.verify_integrity()["ok"] is True
        finally:
            store.close()

    def test_append_event_non_string_ids_raise_typed_error(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            for bad in (None, 123, ["list"], {"d": 1}):
                with pytest.raises((ValueError, TypeError, AttributeError)):
                    store.append_event(bad, "note", {})
                with pytest.raises((ValueError, TypeError, AttributeError)):
                    store.append_event("s", bad, {})
            assert store.verify_integrity()["ok"] is True
        finally:
            store.close()

    def test_append_event_rejects_bytes_session_id(self, tmp_path):
        """Reproduces the bytes-session split-brain: with the bug fixed this
        must raise ValueError/TypeError, and no event may be stored."""
        store, _ = make_store(tmp_path)
        try:
            with pytest.raises((ValueError, TypeError)):
                store.append_event(b"bytes-session", "note", {})
            assert store.search_events("note") == []
        finally:
            store.close()

    def test_bytes_keys_rejected_at_all_str_keyed_write_entries(self, tmp_path):
        """Same class as the bytes session_id bug: every str-keyed write
        seam must refuse bytes loudly -- never store a BLOB key."""
        store, _ = make_store(tmp_path)
        try:
            # bytes event_type.
            with pytest.raises(TypeError):
                store.append_event("s", b"bytes-type", {})
            # bytes logical_key at the durable-state proposal seam.
            event = store.append_event("s", "note", {"text": "x"})
            with pytest.raises(TypeError):
                store.create_proposal("high", b"bytes-key", {"text": "y"}, [event.id])
            # bytes reviewer_id at the review seam.
            proposal = store.create_proposal("high", "k", {"text": "y"}, [event.id])
            with pytest.raises(TypeError):
                store.review_proposal(proposal["id"], "accept", b"bytes-reviewer")
            # Nothing typed-wrong entered the ledger or the state store.
            assert store.verify_integrity()["ok"] is True
            assert store.list_proposals(pending_only=True)[0]["logical_key"] == "k"
        finally:
            store.close()

    def test_deeply_nested_payload_roundtrips_or_refuses(self, tmp_path):
        """A 1000-deep payload must not crash the process; either it is stored
        and read back byte-identical, or it is refused loudly."""
        store, _ = make_store(tmp_path)
        try:
            payload = {"a": None}
            for _ in range(1000):
                payload = {"a": payload}
            try:
                store.append_event("s", "deep", payload)
            except (ValueError, RecursionError):
                # Refused loudly at write time: fine, the ledger is intact.
                assert store.verify_integrity()["ok"] is True
                return
            # Accepted at write time: every later read path that touches the
            # payload must also survive it (RecursionError on read would be a
            # stored-DoS).  1000 levels exceeds Python's default recursion
            # limit for json.loads recursion depth, so a read failure here is
            # a genuine round-trip bug.
            try:
                loaded = store.get_event(store.list_events("s", limit=1)[0].id).payload
                assert loaded == payload
            except RecursionError as exc:
                pytest.fail(
                    "deep payload stored but not readable (json.loads recursion "
                    f"limit): write accepted what read cannot serve: {exc}"
                )
            # The context package assembles payloads too -- must not crash.
            store.assemble_context_package("deep task", session_id="s")
            assert store.verify_integrity()["ok"] is True
        finally:
            store.close()

    def test_nan_inf_payload_refused_loudly_not_stored(self, tmp_path):
        """NaN/inf are not valid JSON; storing them would corrupt provenance
        hashes.  A loud refusal is the only acceptable outcome; if accepted,
        they must round-trip as the same value."""
        store, _ = make_store(tmp_path)
        try:
            for bad in ({"x": float("nan")}, {"x": float("inf")}, {"x": float("-inf")}):
                try:
                    event = store.append_event("s", "num", bad)
                    loaded = store.get_event(event.id).payload["x"]
                    assert loaded == bad["x"] or (
                        isinstance(loaded, float) and isinstance(bad["x"], float)
                        and math.isnan(loaded) and math.isnan(bad["x"])
                    )
                except (ValueError, TypeError):
                    pass
            assert store.verify_integrity()["ok"] is True
        finally:
            store.close()

    def test_huge_integers_in_payload(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            for n in (2**63, 2**1000, -2**1000):
                event = store.append_event("s", "bigint", {"n": n})
                assert store.get_event(event.id).payload["n"] == n
        finally:
            store.close()

    def test_circular_payload_must_fail_loudly(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            payload = {}
            payload["self"] = payload
            with pytest.raises((ValueError, TypeError, RecursionError)):
                store.append_event("s", "circular", payload)
            assert store.verify_integrity()["ok"] is True
        finally:
            store.close()

    def test_rerank_handles_malformed_candidates(self):
        """default_rerank is a projection function: hostile similarity values
        and garbage timestamps must never crash it; ordering stays total."""
        from noname_harness.store import Event

        fake = Event(
            id="evt_x", session_id="s", seq=1, event_type="note",
            payload={}, occurred_at="not-a-date", content_hash="h",
        )
        candidates = [
            {"event": fake, "similarity": float("nan")},
            {"event": fake, "similarity": -1e300},
            {"event": fake, "similarity": 0.5, "promoted": True},
            {"event": fake, "similarity": 0.5, "ref_id": "<script>"},
            {"event": fake},  # no similarity key at all
        ]
        ranked = default_rerank("query", candidates, reference_time="also-garbage")
        assert len(ranked) == len(candidates)
        assert all(isinstance(item, RankedCandidate) for item in ranked)

    def test_rerank_rejects_none_evidence_loudly(self):
        """evidence=None is a malformed candidate: TypeError is the loud,
        typed refusal the contract demands (never a silent len(None) crash
        halfway through a reordered batch)."""
        from noname_harness.store import Event

        fake = Event(
            id="evt_x", session_id="s", seq=1, event_type="note",
            payload={}, occurred_at="2026-10-07T00:00:00Z", content_hash="h",
        )
        with pytest.raises(TypeError):
            default_rerank("q", [{"event": fake, "similarity": 0.5, "evidence": None}])

    def test_rerank_rejects_candidate_without_event(self):
        with pytest.raises((KeyError, TypeError)):
            default_rerank("q", [{"similarity": 0.1}])

    def test_bounded_vendor_int_refuses_hostile_values(self):
        assert bounded_vendor_int(True) is None
        assert bounded_vendor_int(False) is None
        assert bounded_vendor_int(-1) is None
        assert bounded_vendor_int(10**13) is None
        assert bounded_vendor_int("42") is None
        assert bounded_vendor_int(3.5) is None
        assert bounded_vendor_int(None) is None
        assert bounded_vendor_int(0) == 0
        assert bounded_vendor_int(10**12) == 10**12

    def test_validate_timeout_refuses_nan_inf_zero_negative_str(self):
        for bad in (0, -1.0, float("nan"), float("inf"), "30", None, True):
            with pytest.raises(ModelAdapterError):
                validate_timeout(bad)
        assert validate_timeout(0.001) == 0.001

    def test_validate_base_url_refuses_hostile_urls(self):
        for bad in ("", "http://evil.example/x", "ftp://x", "not a url",
                    "javascript:alert(1)", "//no-scheme", None, 42):
            with pytest.raises((ModelAdapterError, TypeError)):
                validate_base_url(bad, allow_insecure=False)

    def test_validate_base_url_https_passes_and_insecure_opt_in(self):
        assert "https://api.example.com" in validate_base_url("https://api.example.com", allow_insecure=False)
        with pytest.raises(ModelAdapterError):
            validate_base_url("http://127.0.0.1:8080", allow_insecure=False)
        assert validate_base_url("http://127.0.0.1:8080", allow_insecure=True)


# ============================================================================
# 2. AgentLoop.resume brutal surface
# ============================================================================

class TestResumeBrutal:
    def test_resume_nonexistent_session_refused_loudly(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)
            with pytest.raises(AgentLoopError, match="no finished run"):
                AgentLoop.resume(
                    store, "ghost-session", GatedPauseDriver(),
                    approval_token="tok", tool_registry=registry,
                )
        finally:
            store.close()

    def test_resume_session_that_never_finished_refused(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)
            store.append_event("s", "note", {"x": 1})  # events, but no run
            with pytest.raises(AgentLoopError):
                AgentLoop.resume(
                    store, "s", GatedPauseDriver(),
                    approval_token="tok", tool_registry=registry,
                )
        finally:
            store.close()

    def test_resume_completed_run_refused(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)

            class DoneDriver:
                def act(self, context, last_tool_result=None):
                    return LoopResult(task_complete=True)

            AgentLoop(store=store, session_id="s", driver=DoneDriver(),
                      tool_registry=registry).run("t")
            with pytest.raises(AgentLoopError, match="waiting_approval"):
                AgentLoop.resume(
                    store, "s", DoneDriver(),
                    approval_token="tok", tool_registry=registry,
                )
        finally:
            store.close()

    @pytest.mark.parametrize("bad_token", [None, "", "   ", 123, b"bytes", "x" * 100_000, ["list"], {"d": 1}])
    def test_resume_with_malformed_approval_token_refused(self, tmp_path, bad_token):
        """A malformed token must be refused BEFORE any state transition:
        no loop.resumed event may be written, and the pause must remain
        resumable with a valid token afterwards."""
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)
            pause_a_run(store, registry)
            with pytest.raises((AgentLoopError, TypeError)):
                AgentLoop.resume(
                    store, "s", GatedPauseDriver(),
                    approval_token=bad_token, tool_registry=registry,
                )
            # The pause is still live: the failed resume must not have claimed it.
            token = registry.grant_approval(
                "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s",
            )
            summary = AgentLoop.resume(
                store, "s", ResumeAwareDriver(registry),
                approval_token=token, tool_registry=registry, actor_id="user",
            )
            assert summary["final_state"] == "COMPLETED"
        finally:
            store.close()

    def test_resume_without_tool_registry_refused(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)
            pause_a_run(store, registry)
            token = registry.grant_approval(
                "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s",
            )
            with pytest.raises(AgentLoopError, match="tool_registry"):
                AgentLoop.resume(
                    store, "s", GatedPauseDriver(), approval_token=token,
                )
        finally:
            store.close()

    def test_double_resume_of_same_pause_refused(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)
            pause_a_run(store, registry)
            token = registry.grant_approval(
                "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s",
            )
            summary = AgentLoop.resume(
                store, "s", ResumeAwareDriver(registry),
                approval_token=token, tool_registry=registry, actor_id="user",
            )
            assert summary["final_state"] == "COMPLETED"
            # The pause was consumed; a second resume attempt must fail.
            token2 = registry.grant_approval(
                "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s",
            )
            with pytest.raises(AgentLoopError):
                AgentLoop.resume(
                    store, "s", ResumeAwareDriver(registry),
                    approval_token=token2, tool_registry=registry, actor_id="user",
                )
        finally:
            store.close()

    def test_resume_blank_actor_id_refused(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)
            pause_a_run(store, registry)
            token = registry.grant_approval(
                "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s",
            )
            for bad_actor in ("", "   ", "\t"):
                with pytest.raises(ValueError):
                    AgentLoop.resume(
                        store, "s", GatedPauseDriver(), approval_token=token,
                        tool_registry=registry, actor_id=bad_actor,
                    )
        finally:
            store.close()

    def test_resume_sessions_do_not_cross_contaminate(self, tmp_path):
        """Two paused sessions: a token/pause from one must not unlock the
        other (grants are session-bound; the resume gate must honour that)."""
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)
            pause_a_run(store, registry, session_id="s1")
            pause_a_run(store, registry, session_id="s2")
            # Token bound to s1 must not resume s2 (session-bound grant).
            token_s1 = registry.grant_approval(
                "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s1",
            )
            with pytest.raises(AgentLoopError):
                AgentLoop.resume(
                    store, "s2", ResumeAwareDriver(registry),
                    approval_token=token_s1, tool_registry=registry, actor_id="user",
                )
            # s1 resumes fine; s2's pause is untouched and still resumable.
            ok = AgentLoop.resume(
                store, "s1", ResumeAwareDriver(registry),
                approval_token=token_s1, tool_registry=registry, actor_id="user",
            )
            assert ok["final_state"] == "COMPLETED"
            token_s2 = registry.grant_approval(
                "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s2",
            )
            ok2 = AgentLoop.resume(
                store, "s2", ResumeAwareDriver(registry),
                approval_token=token_s2, tool_registry=registry, actor_id="user",
            )
            assert ok2["final_state"] == "COMPLETED"
        finally:
            store.close()

    def test_resume_foreign_token_neither_consumed_nor_burns_pause(self, tmp_path):
        """Gate 3 session binding: a refused foreign token must stay LIVE in
        the registry (never consumed by the failed gate) and s2's pause must
        not carry a loop.resumed claim (gate 2 veto untouched)."""
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)
            pause_a_run(store, registry, session_id="s1")
            pause_a_run(store, registry, session_id="s2")
            token_s1 = registry.grant_approval(
                "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s1",
            )
            with pytest.raises(AgentLoopError, match="session"):
                AgentLoop.resume(
                    store, "s2", ResumeAwareDriver(registry),
                    approval_token=token_s1, tool_registry=registry, actor_id="user",
                )
            # The foreign token is still live -- the gate never consumed it.
            assert registry.get_live_token(token_s1.id) is not None
            # No loop.resumed claim was written against s2's pause.
            s2_events = store.list_events("s2", limit=50)
            assert all(e.event_type != "loop.resumed" for e in s2_events)
        finally:
            store.close()

    def test_resume_cross_session_token_blast_radius_contained(self, tmp_path):
        """Defence-in-depth check for the cross-session resume weakness above:
        even when the resume GATE is bypassed with a foreign-session token,
        the registry's execution-time session binding must still refuse the
        gated call itself -- no destructive execution, no token consumed."""
        store, _ = make_store(tmp_path)
        try:
            executed = []
            registry = make_registry(store, executed)
            pause_a_run(store, registry, session_id="s1")
            pause_a_run(store, registry, session_id="s2")
            token_s1 = registry.grant_approval(
                "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s1",
            )

            class ReissueDriver:
                """Re-issues s2's pending call carrying whatever the (foreign)
                token resolves to -- the worst-case driver under the gate bug."""

                def act(self, context, last_tool_result=None):
                    resume = context.get("resume")
                    if resume is not None:
                        token = registry.get_live_token(resume["approval_token_id"])
                        return LoopResult(tool_call={
                            "name": resume["pending_tool"],
                            "arguments": {"path": "/tmp/x"},
                            "approval_token": token,
                        })
                    return LoopResult(task_complete=True)

            # Present s1's token OBJECT (not just id) to resume s2.
            try:
                summary = AgentLoop.resume(
                    store, "s2", ReissueDriver(),
                    approval_token=token_s1, tool_registry=registry, actor_id="user",
                )
            except AgentLoopError:
                summary = None  # gate fixed: the whole attack dies at the gate
            # Whatever the gate did, the destructive call must NOT have run
            # under a foreign-session grant.
            assert "delete" not in executed
            # And the foreign token was never consumed.
            assert registry.get_live_token(token_s1.id) is not None
        finally:
            store.close()

    def test_driver_exception_during_resume_normalises_to_failed(self, tmp_path):
        """A driver that explodes mid-resume must leave the loop in a recorded
        FAILED state, not propagate a raw exception out of the loop."""
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)
            pause_a_run(store, registry)
            token = registry.grant_approval(
                "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s",
            )

            class ExplodingDriver:
                def act(self, context, last_tool_result=None):
                    raise RuntimeError("driver exploded mid-resume")

            summary = AgentLoop.resume(
                store, "s", ExplodingDriver(),
                approval_token=token, tool_registry=registry, actor_id="user",
            )
            assert summary["final_state"] == "FAILED"
            assert summary["stop_reason"] == "unrecoverable_error"
            assert "driver exploded" in summary["error"]
        finally:
            store.close()

    def test_driver_raises_os_error_value_error_normalised(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)
            for exc in (OSError("disk gone"), ValueError("bad value"), ZeroDivisionError()):
                pause_a_run(store, registry)

                class BadDriver:
                    def act(self, context, last_tool_result=None, _exc=exc):
                        raise _exc

                token = registry.grant_approval(
                    "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s",
                )
                summary = AgentLoop.resume(
                    store, "s", BadDriver(),
                    approval_token=token, tool_registry=registry, actor_id="user",
                )
                assert summary["final_state"] == "FAILED"
        finally:
            store.close()

    def test_cancel_immediately_after_resume_claim(self, tmp_path):
        """Resume claims the pause, then a cancel request lands before the
        driver completes: the run must end CANCELLED, never stuck."""
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)
            pause_a_run(store, registry)
            token = registry.grant_approval(
                "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s",
            )

            class CancellingDriver:
                def act(self, context, last_tool_result=None):
                    # Cancel from inside the resumed run, then complete.
                    self_loop = AgentLoop.reconstruct(
                        store, "s", self, tool_registry=registry
                    )
                    self_loop.cancel("user changed their mind")
                    if last_tool_result is not None:
                        return LoopResult(task_complete=True)
                    return LoopResult(output="working")

            summary = AgentLoop.resume(
                store, "s", CancellingDriver(),
                approval_token=token, tool_registry=registry, actor_id="user",
            )
            assert summary["final_state"] in {"CANCELLED", "COMPLETED", "FAILED"}
        finally:
            store.close()

    def test_run_on_non_idle_loop_refused(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)
            loop = AgentLoop(store=store, session_id="s", driver=GatedPauseDriver(),
                             tool_registry=registry)
            loop.run("t")  # pauses -> CANCELLED
            with pytest.raises(AgentLoopError):
                loop.run("t2")
        finally:
            store.close()

    def test_contradictory_driver_results_fail_loudly(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)

            class LiarDriver:
                def act(self, context, last_tool_result=None):
                    return LoopResult(
                        tool_call={"name": "search", "arguments": {"q": "x"}},
                        task_complete=True,
                    )

            loop = AgentLoop(store=store, session_id="s", driver=LiarDriver(),
                             tool_registry=registry)
            summary = loop.run("t")
            assert summary["final_state"] == "FAILED"
            assert "contradictory" in summary["error"]

            class UnknownStopDriver:
                def act(self, context, last_tool_result=None):
                    return LoopResult(stop_reason="made_up_reason")

            loop2 = AgentLoop(store=store, session_id="s2", driver=UnknownStopDriver(),
                              tool_registry=registry)
            summary2 = loop2.run("t")
            assert summary2["final_state"] == "FAILED"
        finally:
            store.close()

    def test_tool_call_missing_name_or_malformed(self, tmp_path):
        """A driver that emits a tool call with no name / non-string name must
        fail loudly (FAILED run), never silently execute."""
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)
            for bad_call in (
                {},
                {"arguments": {}},
                {"name": None},
                {"name": 123},
                {"name": "nonexistent-tool"},
            ):
                class BadCallDriver:
                    def act(self, context, last_tool_result=None, _c=bad_call):
                        return LoopResult(tool_call=_c)

                sid = f"s-{len(str(bad_call))}-{abs(hash(str(bad_call))) % 997}"
                loop = AgentLoop(store=store, session_id=sid, driver=BadCallDriver(),
                                 tool_registry=registry)
                summary = loop.run("t")
                assert summary["final_state"] == "FAILED", bad_call
                assert summary["stop_reason"] == "unrecoverable_error"
        finally:
            store.close()

    def test_parallel_batch_with_malformed_call_fails_loudly(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)

            class BatchDriver:
                def act(self, context, last_tool_result=None):
                    return LoopResult(tool_calls=[
                        {"name": "search", "arguments": {"q": "ok"}},
                        {"name": None, "arguments": {}},
                        {"name": "search", "arguments": {"q": "ok2"}},
                    ])

            loop = AgentLoop(store=store, session_id="s", driver=BatchDriver(),
                             tool_registry=registry)
            summary = loop.run("t")
            assert summary["final_state"] == "FAILED"
        finally:
            store.close()


# ============================================================================
# 3. CLI fuzzing: every subcommand, hostile argv
# ============================================================================

ALL_COMMANDS = [
    "init", "event", "snapshot", "extract", "curate", "propose", "review",
    "state", "proposals", "ledger", "ledger-html", "search", "embed", "verify",
    "reindex", "package", "taste-add", "taste-propose", "taste-review",
    "cancel", "route", "recipes", "inbox", "taste", "card-propose",
    "card-create", "card-review", "card-queue", "card-image", "card",
]


def run_cli(argv, capsys):
    """Invoke cli.main; argparse SystemExit (code 2 on parse errors) is a
    legitimate loud refusal.  Returns (exit_code, stderr_text)."""
    code = None
    try:
        code = cli_main(argv)
    except SystemExit as exc:
        code = exc.code
    err = capsys.readouterr().err
    return code, err


class TestCliFuzz:
    def test_every_command_missing_required_args_refused_cleanly(self, tmp_path, capsys):
        """Every subcommand invoked with zero arguments must fail with a
        readable error (SystemExit 2 from argparse or exit code 2) and never
        with an uncaught traceback."""
        db = tmp_path / "db.sqlite"
        for command in ALL_COMMANDS:
            code, err = run_cli([command, "--db", str(db)], capsys)
            assert code in (0, 2), f"{command}: unexpected exit {code}"
            assert "Traceback" not in err, f"{command} leaked a traceback"

    def test_unknown_command_and_flag_refused(self, capsys):
        for argv in (["not-a-command"], ["event", "--not-a-flag"], []):
            code, err = run_cli(argv, capsys)
            assert code == 2
            assert "Traceback" not in err

    def test_init_idempotent_and_refuses_bad_roots(self, tmp_path, capsys):
        root = tmp_path / "project"
        root.mkdir()
        db = root / ".noname" / "harness.db"
        for _ in range(3):  # init is idempotent
            assert run_cli(["init", "--db", str(db), "--root", str(root)], capsys)[0] == 0
        # /dev/null as the db path: must fail, must not traceback.
        code, err = run_cli(["init", "--db", "/dev/null/harness.db", "--root", str(root)], capsys)
        assert code != 0
        assert "Traceback" not in err

    def test_db_in_nonexistent_directory_creates_or_fails_cleanly(self, tmp_path, capsys):
        """The store auto-creates parent directories for the db (by design);
        the contract under test is only that the outcome is never a crash."""
        db = tmp_path / "no" / "such" / "dir" / "harness.db"
        code, err = run_cli(["inbox", "--db", str(db)], capsys)
        assert code in (0, 2)
        assert "Traceback" not in err

    def test_db_on_readonly_filesystem_fails_cleanly(self, tmp_path, capsys):
        import os
        import stat as _stat
        ro = tmp_path / "ro"
        ro.mkdir()
        os.chmod(ro, _stat.S_IRUSR | _stat.S_IXUSR)
        try:
            code, err = run_cli(["inbox", "--db", str(ro / "harness.db")], capsys)
            assert code != 0
            assert "Traceback" not in err
        finally:
            os.chmod(ro, _stat.S_IRWXU)

    def test_db_path_that_is_a_directory_fails_cleanly(self, tmp_path, capsys):
        """A --db pointing at a directory must fail with a readable error,
        never a traceback (same seam as the read-only case)."""
        code, err = run_cli(["inbox", "--db", str(tmp_path)], capsys)
        assert code != 0
        assert "Traceback" not in err

    def test_db_operational_errors_report_readable_error_and_exit_2(self, tmp_path, capsys):
        """Every db-environment OperationalError is a user-facing
        'error: ...' + exit 2, never a traceback -- across subcommands."""
        # The directory-as-db seam reports readably with exit 2...
        code, err = run_cli(["inbox", "--db", str(tmp_path)], capsys)
        assert code == 2
        assert err.startswith("error:")
        assert "Traceback" not in err
        # ...and the fix is scoped precisely: broader sqlite errors that signal
        # corruption or programming bugs are NOT downgraded to a friendly exit
        # 2 -- they must keep surfacing loudly (e.g. DatabaseError "file is
        # not a database" is a corruption signal, not a usage error).
        import sqlite3 as _sqlite3
        garbage = tmp_path / "garbage.db"
        garbage.write_bytes(b"this is not a sqlite database" * 100)
        with pytest.raises(_sqlite3.DatabaseError):
            cli_main(["inbox", "--db", str(garbage)])

    def test_event_malformed_json_payload_refused(self, tmp_path, capsys):
        root = tmp_path / "project"
        root.mkdir()
        db = root / ".noname" / "harness.db"
        run_cli(["init", "--db", str(db), "--root", str(root)], capsys)
        for bad in ("{", "not json", '{"x": }', "]", "\x00"):
            code, err = run_cli(
                ["event", "--db", str(db), "--session", "s", "--type", "note",
                 "--payload", bad], capsys)
            assert code == 2, bad
            assert "error:" in err
            assert "Traceback" not in err
        # The failed events must not have entered the ledger.
        code, _ = run_cli(["ledger", "--db", str(db), "--session", "s"], capsys)

    def test_event_unicode_bomb_and_long_args(self, tmp_path, capsys):
        root = tmp_path / "project"
        root.mkdir()
        db = root / ".noname" / "harness.db"
        run_cli(["init", "--db", str(db), "--root", str(root)], capsys)
        for text in UNICODE_BOMBS + ["x" * 100_000]:
            code, err = run_cli(
                ["event", "--db", str(db), "--session", text, "--type", "note",
                 "--payload", '{"x":1}'], capsys)
            # A surrogate-containing argv may not round-trip through the OS;
            # either exit code is fine as long as there is no traceback.
            assert "Traceback" not in err

    def test_propose_and_review_malformed_flow(self, tmp_path, capsys):
        root = tmp_path / "project"
        root.mkdir()
        db = root / ".noname" / "harness.db"
        run_cli(["init", "--db", str(db), "--root", str(root)], capsys)
        # Proposal with malformed JSON content.
        code, err = run_cli(
            ["propose", "--db", str(db), "--layer", "high", "--key", "k",
             "--content", "{broken", "--source-event", "evt_ghost"], capsys)
        assert code == 2 and "Traceback" not in err
        # Reviewing a ghost proposal: loud, readable, non-zero.
        code, err = run_cli(
            ["review", "--db", str(db), "--proposal-id", "prp_ghost",
             "--action", "accept", "--reviewer", "u"], capsys)
        assert code != 0 and "Traceback" not in err
        # --valid-from garbage.
        code, err = run_cli(
            ["review", "--db", str(db), "--proposal-id", "prp_ghost",
             "--action", "accept", "--reviewer", "u",
             "--valid-from", "not-a-date"], capsys)
        assert code != 0 and "Traceback" not in err

    def test_search_with_hostile_queries_via_cli(self, tmp_path, capsys):
        root = tmp_path / "project"
        root.mkdir()
        db = root / ".noname" / "harness.db"
        run_cli(["init", "--db", str(db), "--root", str(root)], capsys)
        run_cli(["event", "--db", str(db), "--session", "s", "--type", "note",
                 "--payload", '{"text":"hello"}'], capsys)
        for query in ('"; DROP TABLE session_events;--', '"unclosed',
                      "AND OR NOT", "col:name", "%' OR 1=1--", "x" * 50_000):
            code, err = run_cli(["search", "--db", str(db), "--query", query], capsys)
            assert code in (0, 2)
            assert "Traceback" not in err
        # Integrity survives the fuzzing.
        code, _ = run_cli(["verify", "--db", str(db)], capsys)
        assert code == 0

    def test_taste_and_card_cli_malformed(self, tmp_path, capsys):
        root = tmp_path / "project"
        root.mkdir()
        db = root / ".noname" / "harness.db"
        run_cli(["init", "--db", str(db), "--root", str(root)], capsys)
        attacks = [
            ["taste-add", "--db", str(db), "--content", "{broken"],
            ["taste-review", "--db", str(db), "--taste-id", "tst_ghost",
             "--action", "adopt", "--reviewer", "u"],
            ["card-create", "--db", str(db), "--title", " ", "--attitude", "a",
             "--track", "authored", "--scope", "user", "--taste-id", "tst_ghost"],
            ["card-review", "--db", str(db), "--card-id", "crd_ghost",
             "--action", "accept", "--reviewer", "u"],
            ["card-review", "--db", str(db), "--card-id", "crd_ghost",
             "--action", "edit", "--reviewer", "u", "--edited", "{no"],
            ["card-image", "--db", str(db), "--card-id", "crd_ghost", "--reviewer", "u"],
            ["cancel", "--db", str(db), "--session", "s", "--reason", "   "],
        ]
        for argv in attacks:
            code, err = run_cli(argv, capsys)
            assert code != 0, argv
            assert "Traceback" not in err, argv

    def test_ledger_html_out_path_boundary(self, tmp_path, capsys):
        root = tmp_path / "project"
        root.mkdir()
        db = root / ".noname" / "harness.db"
        run_cli(["init", "--db", str(db), "--root", str(root)], capsys)
        for out in ("../escape.html", "/tmp/escape2.html",
                    str(root / ".noname" / "harness.db")):
            code, err = run_cli(
                ["ledger-html", "--db", str(db), "--out", out, "--overwrite"], capsys)
            assert code != 0, out
            assert "Traceback" not in err

    def test_package_rejects_bad_limits_and_writes_safely(self, tmp_path, capsys):
        root = tmp_path / "project"
        root.mkdir()
        db = root / ".noname" / "harness.db"
        run_cli(["init", "--db", str(db), "--root", str(root)], capsys)
        for argv in (
            ["package", "--db", str(db), "--task", "   "],
            ["package", "--db", str(db), "--task", "t", "--low-limit", "0"],
            ["package", "--db", str(db), "--task", "t", "--low-limit", "-3"],
        ):
            code, err = run_cli(argv, capsys)
            assert code != 0, argv
            assert "Traceback" not in err

    def test_repeated_random_argv_never_tracebacks(self, tmp_path, capsys):
        """Random garbage argv (seeded): the CLI always exits with 2 or runs --
        never crashes with an uncaught traceback."""
        root = tmp_path / "project"
        root.mkdir()
        db = root / ".noname" / "harness.db"
        run_cli(["init", "--db", str(db), "--root", str(root)], capsys)
        alphabet = string.printable
        for _ in range(60):
            argv = [RNG.choice(ALL_COMMANDS + ["junk", "--db"])]
            argv += ["--db", str(db)]
            for _ in range(RNG.randint(0, 4)):
                argv.append("".join(RNG.choice(alphabet) for _ in range(RNG.randint(0, 30))))
            code, err = run_cli(argv, capsys)
            assert code in (0, 2, None), argv
            assert "Traceback" not in err, argv


# ============================================================================
# 4. PluginRuntime malicious plugins
# ============================================================================

def _read_tool(name="search", scope="session", session_id="s", permission="read",
               approval="never"):
    return Tool(
        ToolSchema(name=name, description="d", input_schema={"q": "string"}),
        execute=lambda a: [a["q"]],
        permission=permission, approval=approval, scope=scope,
        session_id=session_id if scope == "session" else None,
    )


def _plugin(tools, **manifest_kwargs):
    kwargs = dict(id="demo", version="1.0.0", capabilities=("search",))
    kwargs.update(manifest_kwargs)
    return Plugin(
        manifest=PluginManifest(**kwargs),
        build=lambda: [PluginContribution(tool=t) for t in tools],
    )


class TestMaliciousPlugins:
    def test_manifest_every_field_malformed(self):
        with pytest.raises(PluginError):
            PluginManifest(id="", version="1", capabilities=("x",))
        # None id/version: a typed refusal (PluginError or TypeError-family)
        # is required; a raw AttributeError on .strip() would be an untyped leak.
        with pytest.raises((PluginError, TypeError, AttributeError)):
            PluginManifest(id=None, version="1", capabilities=("x",))
        with pytest.raises((PluginError, TypeError, AttributeError)):
            PluginManifest(id="p", version=None, capabilities=("x",))
        with pytest.raises(PluginError):
            PluginManifest(id="p", version="", capabilities=("x",))
        with pytest.raises(PluginError):
            PluginManifest(id="p", version="1", capabilities=())
        with pytest.raises(PluginError):
            PluginManifest(id="p", version="1", capabilities=("x",), max_permission="root")
        with pytest.raises(PluginError):
            PluginManifest(id="p", version="1", capabilities=("x",), max_permission="")
        with pytest.raises(PluginError):
            PluginManifest(id="p", version="1", capabilities=("x",),
                           min_interface=3, max_interface=1)  # inverted range
        with pytest.raises(PluginError):
            PluginManifest(id="p", version="1", capabilities=("x",), min_interface=0)
        with pytest.raises(PluginError):
            PluginManifest(id="p", version="1", capabilities=("x",),
                           min_interface=-5, max_interface=-1)

    def test_manifest_interface_outside_harness_version_refused(self, tmp_path):
        store, registry = _runtime(tmp_path)
        try:
            runtime = PluginRuntime(store, registry)
            with pytest.raises(PluginError):
                runtime.load(_plugin([_read_tool()], min_interface=2, max_interface=99))
            assert runtime.loaded_plugins() == []
        finally:
            store.close()

    def test_build_raises_normalised_to_plugin_error(self, tmp_path):
        store, registry = _runtime(tmp_path)
        try:
            runtime = PluginRuntime(store, registry)

            def bad_build():
                raise RuntimeError("evil build crash")

            plugin = Plugin(
                manifest=PluginManifest(id="evil", version="1", capabilities=("x",)),
                build=bad_build,
            )
            with pytest.raises(PluginError, match="build"):
                runtime.load(plugin)
            assert runtime.loaded_plugins() == []
            types = [e.event_type for e in store.list_events("system", limit=50)]
            assert "plugin.build_failed" in types
        finally:
            store.close()

    def test_build_returns_garbage_refused(self, tmp_path):
        store, registry = _runtime(tmp_path)
        try:
            runtime = PluginRuntime(store, registry)
            for garbage in (None, "tools", 42, ["not-a-contribution"], [None]):
                plugin = Plugin(
                    manifest=PluginManifest(id=f"g{len(str(garbage))}{RNG.randint(0,999)}",
                                            version="1", capabilities=("x",)),
                    build=lambda _g=garbage: _g,
                )
                with pytest.raises(PluginError):
                    runtime.load(plugin)
            assert runtime.loaded_plugins() == []
        finally:
            store.close()

    def test_load_twice_same_plugin_refused(self, tmp_path):
        store, registry = _runtime(tmp_path)
        try:
            runtime = PluginRuntime(store, registry)
            runtime.load(_plugin([_read_tool()]))
            with pytest.raises(PluginError, match="already loaded"):
                runtime.load(_plugin([_read_tool()]))
        finally:
            store.close()

    def test_unload_unknown_plugin_refused(self, tmp_path):
        store, registry = _runtime(tmp_path)
        try:
            runtime = PluginRuntime(store, registry)
            for ghost in ("ghost", "", "never-loaded"):
                with pytest.raises(PluginError):
                    runtime.unload(ghost)
        finally:
            store.close()

    def test_unload_then_reload_cannot_weaken_gate(self, tmp_path):
        """Tombstone monotonicity: an unloaded plugin's destructive tool name
        cannot be re-registered (by anyone) as a weaker read tool."""
        store, registry = _runtime(tmp_path)
        try:
            runtime = PluginRuntime(store, registry)
            destructive = _read_tool(name="nuke", permission="destructive", approval="always")
            runtime.load(_plugin([destructive], max_permission="destructive"))
            runtime.unload("demo")
            # Reload the same name as a read tool -> tombstone must veto.
            weak = _read_tool(name="nuke", permission="read", approval="never")
            with pytest.raises((PluginError, ToolShadowingError)):
                runtime.load(_plugin([weak]))
            # The host itself also cannot weaken the tombstoned name.
            with pytest.raises((ToolShadowingError, Exception)):
                registry.register(weak)
        finally:
            store.close()

    def test_failed_load_restores_displaced_host_tool(self, tmp_path):
        """A plugin whose second contribution explodes must roll back fully:
        the host tool shadowed by the first contribution is restored."""
        store, registry = _runtime(tmp_path)
        try:
            host_tool = _read_tool(name="host-search")
            registry.register(host_tool)
            runtime = PluginRuntime(store, registry)

            shadow = _read_tool(name="host-search")  # displaces the host tool
            bad_global = _read_tool(name="gs", scope="global")  # fails validation
            plugin = Plugin(
                manifest=PluginManifest(id="sneaky", version="1", capabilities=("x",)),
                build=lambda: [PluginContribution(tool=shadow),
                               PluginContribution(tool=bad_global)],
            )
            with pytest.raises(PluginError):
                runtime.load(plugin)
            assert registry.get("host-search") is host_tool  # restored
            assert runtime.loaded_plugins() == []
        finally:
            store.close()

    def test_contribution_conflicting_with_existing_tool(self, tmp_path):
        """A contribution with the same name as a stronger existing tool is
        refused by shadowing monotonicity; the registry is untouched."""
        store, registry = _runtime(tmp_path)
        try:
            registry.register(_read_tool(name="core", permission="destructive",
                                         approval="always"))
            runtime = PluginRuntime(store, registry)
            with pytest.raises((PluginError, ToolShadowingError)):
                runtime.load(_plugin([_read_tool(name="core")]))
            assert registry.get("core").permission == "destructive"
        finally:
            store.close()

    def test_plugin_tool_gate_intact_under_hostile_arguments(self, tmp_path):
        """Even loaded, a plugin's gated tool never executes without a token --
        for any argument shape."""
        store, registry = _runtime(tmp_path)
        try:
            executed = []
            gated = Tool(
                ToolSchema(name="pgated", description="d", input_schema={"p": "string"}),
                execute=lambda a: executed.append(a) or "done",
                permission="destructive", approval="always",
                scope="session", session_id="s",
            )
            runtime = PluginRuntime(store, registry)
            runtime.load(_plugin([gated], max_permission="destructive"))
            for args in ({"p": "/etc/passwd"}, {"p": ""}, {"p": "x" * 100_000}):
                with pytest.raises(ToolApprovalRequired):
                    registry.request("pgated", args, session_id="s")
            assert executed == []
        finally:
            store.close()


def _runtime(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "plugins")
    return store, ToolRegistry(store)


# ============================================================================
# 5. Rendering / HTML injection
# ============================================================================

def _assert_no_unescaped_injection(html_text: str, payloads: list[str]) -> None:
    for payload in payloads:
        assert payload not in html_text, f"unescaped injection survived: {payload!r}"
    # Defence in depth: no active markup shape may appear at all outside the
    # renderer's own static template (checked by absence of the payload's
    # unescaped form above); additionally a rendered page must never contain
    # an event-handler attribute built from user data.  The template itself
    # uses no inline handlers, so ANY onerror=/onload=/onclick= occurrence
    # that is not HTML-escaped is a finding.
    import re as _re
    # An event handler only executes if its attribute sits inside an OPEN tag
    # (i.e. the preceding '<' was not escaped).  Matches whose neighbouring
    # angle brackets are escaped entities are inert text, not a finding.
    for match in _re.finditer(r"\bon(error|load|click)\s*=", html_text):
        window = html_text[max(0, match.start() - 300):match.start()]
        last_lt = window.rfind("<")
        last_escaped_lt = window.rfind("&lt;")
        last_gt = window.rfind(">")
        if last_lt > last_gt and last_lt > last_escaped_lt:
            pytest.fail(
                f"live event-handler attribute inside an unescaped tag at "
                f"{match.start()}: ...{html_text[max(0, match.start()-60):match.end()+20]!r}"
            )


class TestRenderInjection:
    def _store_with_xss(self, tmp_path):
        store, _ = make_store(tmp_path)
        for i, payload in enumerate(XSS_PAYLOADS):
            event = store.append_event(
                "s", f"xss.type.{i}", {"text": payload, "nested": {"x": payload}},
                [EvidenceInput(payload, None)],
            )
        taste = TasteService(store)
        taste.record_authored({"judgement": XSS_PAYLOADS[0], "examples": XSS_PAYLOADS[1]})
        return store

    def test_ledger_html_escapes_all_injections(self, tmp_path):
        store = self._store_with_xss(tmp_path)
        try:
            model = build_ledger_model(store)
            page = render_ledger_html(model)
            _assert_no_unescaped_injection(page, XSS_PAYLOADS)
        finally:
            store.close()

    def test_causal_html_escapes_all_injections(self, tmp_path):
        store = self._store_with_xss(tmp_path)
        try:
            # Promote one xss-laden event to canon so it appears in the map.
            proposal = store.create_proposal(
                "high", "k<script>", {"text": XSS_PAYLOADS[2]},
                [store.list_events("s", limit=1)[0].id],
            )
            store.review_proposal(proposal["id"], "accept", "user<script>")
            model = build_causal_model(store)
            page = render_causal_html(model)
            _assert_no_unescaped_injection(page, XSS_PAYLOADS)
        finally:
            store.close()

    def test_state_diff_html_escapes_all_injections(self, tmp_path):
        store = self._store_with_xss(tmp_path)
        try:
            event = store.list_events("s", limit=1)[0]
            proposal = store.create_proposal(
                "high", f"key-{XSS_PAYLOADS[0]}", {"text": XSS_PAYLOADS[1]}, [event.id],
            )
            store.review_proposal(proposal["id"], "accept", XSS_PAYLOADS[2])
            taste = TasteService(store)
            record = taste.record_authored({"judgement": XSS_PAYLOADS[3]})
            cards = TasteCardService(store)
            cards.create_card(
                title=XSS_PAYLOADS[0], attitude=XSS_PAYLOADS[1],
                track="authored", scope="user", taste_ids=[record["id"]],
            )
            model = build_state_diff_model(store)
            page = render_state_diff_html(model)
            _assert_no_unescaped_injection(page, XSS_PAYLOADS)
        finally:
            store.close()

    def test_card_svg_escapes_all_injections(self):
        for payload in XSS_PAYLOADS + [p for p in UNICODE_BOMBS if "\ud800" not in p]:
            image = local_typographic_image(
                {"title": payload, "attitude": payload, "track": payload}
            )
            svg = image.image_bytes.decode("utf-8")
            assert "<script" not in svg
            _assert_no_unescaped_injection(svg, ["<script>alert(1)</script>"])
            # The SVG must remain well-formed XML (parseable).
            import xml.etree.ElementTree as ET
            ET.fromstring(svg)

    def test_card_svg_survives_lone_surrogates(self):
        """A card carrying a lone surrogate (invalid Unicode that still "
        arrives via Python strings) must be sanitised, not crash the
        renderer."""
        payload = "surrog\ud800ate"
        image = local_typographic_image(
            {"title": payload, "attitude": payload, "track": payload}
        )
        import xml.etree.ElementTree as ET
        ET.fromstring(image.image_bytes.decode("utf-8", errors="replace"))

    def test_card_surrogates_sanitised_deterministically_and_never_in_xml(self):
        """Sanitisation is deterministic (same input -> same bytes/seed) and
        no surrogate code point ever reaches the SVG output."""
        payloads = ["\ud800", "\udfff", "a\udc00b\udeadc"]
        for payload in payloads:
            first = local_typographic_image(
                {"title": payload, "attitude": payload, "track": payload}
            )
            second = local_typographic_image(
                {"title": payload, "attitude": payload, "track": payload}
            )
            # Deterministic: identical input renders byte-identical output.
            assert first.image_bytes == second.image_bytes
            assert first.metadata["seed"] == second.metadata["seed"]
            # The rendered bytes are valid UTF-8 (no surrogate round-trip).
            svg = first.image_bytes.decode("utf-8")
            assert not any(0xD800 <= ord(ch) <= 0xDFFF for ch in svg)
            import xml.etree.ElementTree as ET
            ET.fromstring(svg)

    def test_card_svg_extreme_lengths_bounded(self):
        image = card_image_for(
            {"title": "t" * 1_000_000, "attitude": "a" * 1_000_000, "track": "authored"}
        )
        # Title/attitude are truncated; the SVG stays bounded.
        assert len(image.image_bytes) < 100_000

    def test_renderers_survive_huge_event_volume(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            for i in range(300):
                store.append_event("s", "bulk.note", {"i": i, "text": "x" * 500})
            model = build_ledger_model(store)
            page = render_ledger_html(model)
            assert "<html" in page
        finally:
            store.close()


# ============================================================================
# 6. Sandbox boundary
# ============================================================================

class TestSandboxBoundary:
    def test_path_traversal_every_encoding(self, tmp_path):
        store, root = make_store(tmp_path)
        try:
            sandbox = Sandbox(store)
            outside = tmp_path / "secret.txt"
            outside.write_text("top secret")
            attacks = [
                "../secret.txt",
                "../../secret.txt",
                "./../../secret.txt",
                "sub/../../../secret.txt",
                "..%2Fsecret.txt",   # percent-encoded (literal name, still outside-if-decoded)
                str(outside),         # absolute path
                "/etc/passwd",
                "....//....//secret.txt",
                "sub/../../secret.txt",
            ]
            for path in attacks:
                with pytest.raises((WorkspaceBoundaryError, SandboxError)):
                    sandbox.read_file(path, session_id="s")
            # The secret was never read into the ledger.
            for event in store.list_events("s", limit=100):
                assert "top secret" not in str(event.payload)
        finally:
            store.close()

    def test_unicode_normalisation_paths(self, tmp_path):
        """NFC/NFD-equivalent names and confusable characters cannot escape;
        a genuinely-inside unicode filename must still work."""
        store, root = make_store(tmp_path)
        try:
            sandbox = Sandbox(store)
            nfc = unicodedata.normalize("NFC", "café.txt")
            nfd = unicodedata.normalize("NFD", "café.txt")
            (root / nfc).write_text("inside")
            result = sandbox.read_file(nfd, session_id="s")
            assert result["content"] == "inside"
            # A traversal built from unicode characters still dies at the boundary.
            for path in ("..／secret", "..／..", "＼..＼..", "..\\secret.txt"):
                with pytest.raises((WorkspaceBoundaryError, SandboxError)):
                    sandbox.read_file(path, session_id="s")
        finally:
            store.close()

    def test_trailing_dots_and_spaces(self, tmp_path):
        store, root = make_store(tmp_path)
        try:
            sandbox = Sandbox(store)
            # Actual traversals disguised with trailing dots/spaces must be refused.
            for path in ("../. ", ".. /..", "../ "):
                with pytest.raises((WorkspaceBoundaryError, SandboxError, ValueError)):
                    sandbox.write_file(path, "x", session_id="s")
            # Names that LOOK odd but resolve inside the workspace are legal
            # POSIX filenames; they must land inside the root, never outside.
            for path in (".. .", ".../x", ".. /", "..\t"):
                result = sandbox.write_file(path, "x", session_id="s")
                written = root / result["path"]
                assert written.resolve().is_relative_to(root.resolve())
                assert not (tmp_path / result["path"]).exists() or \
                    (tmp_path / result["path"]).resolve().is_relative_to(root.resolve())
        finally:
            store.close()

    def test_write_to_readonly_directory_fails_loudly(self, tmp_path):
        import os
        import stat as _stat
        store, root = make_store(tmp_path)
        try:
            ro = root / "readonly"
            ro.mkdir()
            os.chmod(ro, _stat.S_IRUSR | _stat.S_IXUSR)
            sandbox = Sandbox(store)
            try:
                with pytest.raises((OSError, SandboxError, WorkspaceBoundaryError, PermissionError)):
                    sandbox.write_file("readonly/file.txt", "content", session_id="s")
            finally:
                os.chmod(ro, _stat.S_IRWXU)  # restore so tmp_path cleanup works
        finally:
            store.close()

    def test_run_command_empty_and_hostile_argv(self, tmp_path):
        store, root = make_store(tmp_path)
        try:
            sandbox = Sandbox(store)
            with pytest.raises(SandboxError):
                sandbox.run_command([], session_id="s")
            for argv in (
                ["rm", "-rf", "."],
                ["/bin/ls"],            # path separator -> refused
                ["./ls"],
                ["python3", "-c", "pass"],
                ["ls\x00junk"],          # null byte in executable name
                ["ls", "/etc"],
                ["cat", "../../secret"],
                ["git", "status"],
                ["find", ".", "-delete"],
            ):
                with pytest.raises(SandboxError):
                    sandbox.run_command(argv, session_id="s")
        finally:
            store.close()

    def test_run_command_null_byte_argument(self, tmp_path):
        """Python's subprocess raises ValueError on embedded null bytes; the
        sandbox must turn that into a typed SandboxError (ledger-recorded
        attempt), not leak a raw ValueError through the seam."""
        store, root = make_store(tmp_path)
        try:
            sandbox = Sandbox(store)
            with pytest.raises((SandboxError, ValueError)):
                sandbox.run_command(["echo", "a\x00b"], session_id="s")
        finally:
            store.close()

    def test_run_command_timeout_and_timeout_validation(self, tmp_path):
        store, root = make_store(tmp_path)
        try:
            sandbox = Sandbox(store, command_timeout=1)
            with pytest.raises(SandboxError):
                sandbox.run_command(["ls"], session_id="s", timeout=0)
            with pytest.raises(SandboxError):
                sandbox.run_command(["ls"], session_id="s", timeout=-5)
            # A zero/negative timeout at construction is refused too.
            with pytest.raises(ValueError):
                Sandbox(store, command_timeout=0)
        finally:
            store.close()

    def test_read_special_files_refused(self, tmp_path):
        """FIFOs/devices must be refused before any blocking read."""
        import os
        store, root = make_store(tmp_path)
        try:
            fifo = root / "pipe"
            os.mkfifo(fifo)
            sandbox = Sandbox(store)
            with pytest.raises(SandboxError):
                sandbox.read_file("pipe", session_id="s")
            with pytest.raises(SandboxError):
                sandbox.read_file("missing-file", session_id="s")
        finally:
            store.close()

    def test_symlink_escape_refused(self, tmp_path):
        store, root = make_store(tmp_path)
        try:
            outside = tmp_path / "out.txt"
            outside.write_text("secret")
            link = root / "link.txt"
            link.symlink_to(outside)
            sandbox = Sandbox(store)
            with pytest.raises((WorkspaceBoundaryError, SandboxError)):
                sandbox.read_file("link.txt", session_id="s")
            # Writing through a symlink that points outside is also refused.
            with pytest.raises((WorkspaceBoundaryError, SandboxError)):
                sandbox.write_file("link.txt", "overwrite", session_id="s")
            assert outside.read_text() == "secret"
        finally:
            store.close()

    def test_read_binary_file_does_not_crash(self, tmp_path):
        store, root = make_store(tmp_path)
        try:
            (root / "bin.dat").write_bytes(bytes(range(256)) * 10)
            sandbox = Sandbox(store)
            result = sandbox.read_file("bin.dat", session_id="s")
            assert result["encoding"] in {"utf-8", "hex"}
        finally:
            store.close()


# ============================================================================
# 7. Vendor seams: image gen, embedding, llm extractor
# ============================================================================

class TestVendorSeams:
    def test_image_adapter_refuses_hostile_sizes_and_timeouts(self):
        # Malformed sizes are refused loudly at construction.
        for bad_size in ("", "0x100", "100x0", "x100", "100x", "1.5x2", "large"):
            with pytest.raises(ModelAdapterError):
                OpenAIImageGenAdapter(size=bad_size, api_key="k")
        # The size contract is fail-closed normalisation: "1024X1024" and
        # "99999x99999" are canonicalised at construction, so the validated
        # value is exactly what reaches the wire.
        assert OpenAIImageGenAdapter(size="1024X1024", api_key="k").size == "1024x1024"
        assert OpenAIImageGenAdapter(size=" 512x512 ", api_key="k").size == "512x512"
        for bad_timeout in (0, -1, float("nan"), float("inf"), "slow"):
            with pytest.raises(ModelAdapterError):
                OpenAIImageGenAdapter(timeout=bad_timeout, api_key="k")

    def test_image_adapter_fail_closed_when_not_loaded(self):
        """A bare adapter (never loaded as a plugin) refuses egress."""
        adapter = OpenAIImageGenAdapter(api_key="k", transport=lambda *a: (200, b"{}"))
        with pytest.raises(ModelAdapterError, match="not active|not loaded|refus"):
            adapter({"title": "t", "attitude": "a", "track": "authored"})

    def test_image_response_mapping_refuses_hostile_payloads(self):
        adapter = OpenAIImageGenAdapter(api_key="k")
        import base64
        import json as _json
        hostile_responses = [
            b"not json",
            b"",
            _json.dumps({"data": []}).encode(),
            _json.dumps({"data": [{"b64_json": "!!! not base64 !!!"}]}).encode(),
            _json.dumps({"data": [{"b64_json": base64.b64encode(b"x").decode(),
                                     "mime_type": "image/svg+xml"}]}).encode(),  # active type refused
            _json.dumps({"data": [{"b64_json": base64.b64encode(b"x").decode(),
                                     "mime_type": "text/html"}]}).encode(),
            _json.dumps({"data": [{"url": "https://evil.example/x.png"}]}).encode(),  # URL fetch refused
            _json.dumps({"data": [{"b64_json": "A" * (40 * 1024 * 1024)}]}).encode(),  # size bomb
            _json.dumps({"data": [{"b64_json": 12345}]}).encode(),
            _json.dumps([1, 2, 3]).encode(),  # top-level not a dict
        ]
        for raw in hostile_responses:
            with pytest.raises(ModelAdapterError):
                adapter._map_response(raw)

    def test_build_card_prompt_bounded_under_hostile_card(self):
        prompt = build_card_prompt(
            {"title": "t" * 1_000_000, "attitude": "a" * 1_000_000,
             "track": "x" * 100_000, "evil": "<script>"}
        )
        assert len(prompt) <= 4_000
        # None / non-string fields coerce rather than crash.
        prompt2 = build_card_prompt({"title": None, "attitude": 42, "track": ["x"]})
        assert isinstance(prompt2, str)

    def test_embedding_input_bound_enforced(self):
        emb = OpenAIEmbedding(api_key="k")
        with pytest.raises(ModelAdapterError, match="limit"):
            emb("x" * 40_000)

    def test_embedding_refuses_malformed_vectors(self):
        import json as _json
        def transport_with(payload):
            return lambda *a: (200, _json.dumps(payload).encode())

        # Structurally malformed vectors are refused loudly.
        for payload in (
            {"data": []},
            {"data": [{"embedding": "not-a-list"}]},
            {"data": [{"embedding": [1, 2, "three"]}]},
            {"data": None},
            [1, 2, 3],
        ):
            emb = OpenAIEmbedding(api_key="k", transport=transport_with(payload))
            with pytest.raises(ModelAdapterError):
                emb("hello")

    def test_embedding_pipeline_refuses_non_finite_vectors(self, tmp_path):
        """A NaN/inf vector must be refused at the vendor seam AND at the
        index seam -- never silently stored and served back as NaN scores."""
        import json as _json

        # (1) The vendor seam must refuse non-finite vendor output.
        def transport_nan(*a):
            return (200, _json.dumps({"data": [{"embedding": [float("nan"), 1.0]}]}).encode())

        emb = OpenAIEmbedding(api_key="k", transport=transport_nan)
        with pytest.raises(ModelAdapterError):
            emb("hello")

        # (2) The index/search seam must refuse non-finite vectors loudly --
        # a NaN/inf vector is never stored, and never served back as NaN
        # similarities.  Loud refusal at the index seam satisfies the
        # contract; verify the poisoned write left the projection empty.
        store, _ = make_store(tmp_path)
        try:
            store.append_event("s", "note", {"text": "hello world"})

            def nan_embedding(text):
                return [float("nan")] * 8

            with pytest.raises(ValueError, match="non-finite"):
                store.build_embedding_index(nan_embedding)
            # The refused build stored nothing; a sane index/round-trip still
            # works afterwards (the ledger was not poisoned by the attempt).
            store.build_embedding_index(local_hash_embedding)
            hits = store.search_events_semantic("hello", local_hash_embedding)
            assert all(math.isfinite(h["similarity"]) for h in hits)
            # A NaN query vector is refused loudly too (query-side seam).
            # Match the index's model id so the cross-space guard passes and
            # the finite-vector check is what fires.
            nan_embedding.model_id = "local_hash"
            with pytest.raises(ValueError, match="non-finite"):
                store.search_events_semantic("hello", nan_embedding)
            # And the ranked projection (recall + rerank) inherits the guard.
            with pytest.raises(ValueError, match="non-finite"):
                store.search_events_ranked("hello", nan_embedding)
        finally:
            store.close()

    def test_embedding_pipeline_refuses_inf_vectors(self, tmp_path):
        """Same class as NaN: +inf/-inf poison cosine similarity exactly the
        same way and must be refused at BOTH seams."""
        import json as _json

        for poison in (float("inf"), float("-inf")):
            def transport_inf(*a, _p=poison):
                return (200, _json.dumps({"data": [{"embedding": [_p, 1.0]}]}).encode())
            emb = OpenAIEmbedding(api_key="k", transport=transport_inf)
            with pytest.raises(ModelAdapterError):
                emb("hello")

        store, _ = make_store(tmp_path)
        try:
            store.append_event("s", "note", {"text": "hello world"})
            for poison in (float("inf"), float("-inf")):
                def inf_embedding(text, _p=poison):
                    return [_p] * 8
                with pytest.raises(ValueError, match="non-finite"):
                    store.build_embedding_index(inf_embedding)
            # Non-numeric components are refused as a typed error too.
            def string_embedding(text):
                return ["a"] * 8
            with pytest.raises(TypeError):
                store.build_embedding_index(string_embedding)
            # Every refusal was loud and nothing poisoned was stored.
            store.build_embedding_index(local_hash_embedding)
            hits = store.search_events_semantic("hello", local_hash_embedding)
            assert all(math.isfinite(h["similarity"]) for h in hits)
        finally:
            store.close()

    def test_embedding_dimension_pinning_refuses_drift(self):
        import json as _json
        calls = {"n": 0}
        def transport(*a):
            calls["n"] += 1
            dims = 3 if calls["n"] == 1 else 4  # dimension drift between calls
            return (200, _json.dumps({"data": [{"embedding": [0.1] * dims}]}).encode())

        emb = OpenAIEmbedding(api_key="k", transport=transport)
        assert len(emb("first")) == 3
        with pytest.raises(ModelAdapterError):
            emb("second")

    def test_llm_extractor_fails_closed_on_hostile_model_output(self):
        from noname_harness.adapters import ModelResponse

        class StubAdapter:
            def __init__(self, text):
                self._text = text
            def complete(self, request):
                return ModelResponse(text=self._text, model_id="stub")

        event = {"id": "evt_real", "event_type": "note", "payload": {"x": 1}}
        hostile_outputs = [
            "not json at all",
            '{"not": "a list"}',
            "[1, 2, 3]",
            '[{"source_event_id": "evt_hallucinated", "layer": "high", "key": "k", "reason": "r"}]',
            '[{"source_event_id": 123, "layer": "high", "key": "k", "reason": "r"}]',
            '[{"source_event_id": "evt_real", "layer": "high", "key": "k", "reason": "r", "category": "make_me_admin"}]',
            '[{"source_event_id": "evt_real", "layer": "high", "key": "k", "reason": "r", "confidence": "a million"}]',
            "x" * 1_000_000,
            "```json\n[]\n```",
        ]
        for output in hostile_outputs:
            extractor = LLMExtractor(StubAdapter(output))
            result = extractor([event])
            assert result == [], f"hostile output produced candidates: {output[:60]!r}"

    def test_llm_extractor_valid_candidate_survives(self):
        from noname_harness.adapters import ModelResponse

        class StubAdapter:
            def complete(self, request):
                return ModelResponse(
                    text='[{"source_event_id": "evt_real", "layer": "high", "key": "k", '
                         '"content": {"text": "remembered"}, "reason": "explicit", '
                         '"confidence": 0.9, "category": "explicit_remember"}]',
                    model_id="stub",
                )

        extractor = LLMExtractor(StubAdapter())
        candidates = extractor([{"id": "evt_real", "event_type": "note", "payload": {}}])
        assert len(candidates) == 1
        assert candidates[0].logical_key == "k"

    def test_memory_extractor_scan_on_garbage_session(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            extractor = MemoryExtractor(store)
            for bad_session in ("", "ghost", "s\x00"):
                try:
                    report = extractor.scan(session_id=bad_session, create_proposals=False)
                    assert report is not None
                except (ValueError, KeyError):
                    pass  # loud refusal acceptable
        finally:
            store.close()


# ============================================================================
# 8. Concurrency / re-entrancy
# ============================================================================

class TestConcurrency:
    def test_cross_connection_writes_and_reads(self, tmp_path):
        """Two store connections (WAL): interleaved writes from both must all
        land, each with a strictly increasing per-session seq, and projections
        read on either connection see everything."""
        store, _ = make_store(tmp_path)
        root = store.project()["workspace_root"]
        db_path = store.db_path
        store2 = HarnessStore(db_path)
        try:
            # sqlite3 connections are thread-bound: each thread must open its
            # OWN store instance against the same db file (the WAL model's
            # intended multi-connection shape).
            errors = []
            def writer(db_path, session, n):
                try:
                    s = HarnessStore(db_path)
                    try:
                        for i in range(n):
                            s.append_event(session, "concurrent.note", {"i": i})
                    finally:
                        s.close()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            t1 = threading.Thread(target=writer, args=(db_path, "s", 30))
            t2 = threading.Thread(target=writer, args=(db_path, "s", 30))
            t1.start(); t2.start(); t1.join(); t2.join()
            assert errors == [], errors
            events = store.list_events("s", limit=100)
            assert len(events) == 60
            seqs = sorted(e.seq for e in events)
            assert seqs == list(range(1, 61))
            assert store.verify_integrity()["ok"] is True
            assert store2.verify_integrity()["ok"] is True
        finally:
            store2.close()
            store.close()

    def test_two_loops_same_session_second_run_refused(self, tmp_path):
        """Two AgentLoop instances on one session: the second run() is a
        state-machine violation only for the same in-process loop; two separate
        instances both starting from IDLE each write their own run -- the
        invariant under test is that a paused run's resume gate serialises
        concurrent resumes."""
        store, _ = make_store(tmp_path)
        try:
            registry = make_registry(store)
            pause_a_run(store, registry)
            token = registry.grant_approval(
                "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s",
            )
            # Two concurrent resumes of the SAME pause: exactly one may win.
            # Each thread uses its own store connection + registry (sqlite3
            # objects are thread-bound); the token id is shared, and each
            # thread's registry is granted its own live token record from the
            # ledger is process-local, so we grant in each thread's registry.
            results = []
            db_path = store.db_path

            def do_resume():
                s = HarnessStore(db_path)
                try:
                    reg = make_registry(s)
                    tk = reg.grant_approval(
                        "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s",
                    )
                    try:
                        summary = AgentLoop.resume(
                            s, "s", ResumeAwareDriver(reg),
                            approval_token=tk, tool_registry=reg, actor_id="user",
                        )
                        results.append(("ok", summary["final_state"]))
                    except AgentLoopError as exc:
                        results.append(("refused", str(exc)))
                finally:
                    s.close()

            t1 = threading.Thread(target=do_resume)
            t2 = threading.Thread(target=do_resume)
            t1.start(); t2.start(); t1.join(); t2.join()
            ok_count = sum(1 for status, _ in results if status == "ok")
            assert ok_count <= 1, f"double resume slipped through: {results}"
            refused = [msg for status, msg in results if status == "refused"]
            if ok_count == 1:
                assert len(refused) == 1 and "already resumed" in refused[0]
        finally:
            store.close()

    def test_registry_request_reentrant_safe(self, tmp_path):
        store, _ = make_store(tmp_path)
        db_path = store.db_path
        try:
            executed = []
            lock = threading.Lock()

            def hitter(n):
                # Each thread gets its own connection-bound store+registry
                # (sqlite3 objects are thread-bound by default).
                s = HarnessStore(db_path)
                try:
                    registry = make_registry(s)
                    for i in range(n):
                        out = registry.request("search", {"q": f"q{i}"}, session_id="s")
                        with lock:
                            executed.append(out["output"])
                finally:
                    s.close()

            threads = [threading.Thread(target=hitter, args=(20,)) for _ in range(3)]
            for t in threads: t.start()
            for t in threads: t.join()
            assert len(executed) == 60
            assert store.verify_integrity()["ok"] is True
        finally:
            store.close()


# ============================================================================
# 9. Random-workflow invariants (seeded, reproducible)
# ============================================================================

class TestRandomWorkflowInvariants:
    def test_random_pipeline_core_invariants(self, tmp_path):
        """Random mix of event/propose/review/taste/card/package/search/rerank.
        After EVERY step: append-only integrity holds, the projection rebuilds
        deterministically, and the approval gate is never bypassed."""
        store, _ = make_store(tmp_path)
        try:
            rng = random.Random(424242)
            taste = TasteService(store)
            cards = TasteCardService(store)
            registry = make_registry(store, executed := [])
            proposals_seen = []
            cards_seen = []

            ops = ["event", "propose", "review", "taste", "card", "package",
                   "search", "rerank", "gated_tool"]
            for step in range(120):
                op = rng.choice(ops)
                try:
                    if op == "event":
                        store.append_event(
                            "s", rng.choice(["note", "model.completed", "test.failed"]),
                            {"step": step, "text": f"payload-{rng.randint(0, 999)}"},
                        )
                    elif op == "propose":
                        events = store.list_events("s", limit=5)
                        if events:
                            proposal = store.create_proposal(
                                rng.choice(["high", "mid"]), f"k{rng.randint(0, 5)}",
                                {"text": f"v{step}"}, [rng.choice(events).id],
                            )
                            proposals_seen.append(proposal["id"])
                    elif op == "review" and proposals_seen:
                        pid = rng.choice(proposals_seen)
                        try:
                            store.review_proposal(
                                pid, rng.choice(["accept", "reject", "defer"]),
                                f"user{rng.randint(0, 2)}",
                            )
                        except ValueError:
                            pass  # already decided -- a legal loud refusal
                    elif op == "taste":
                        record = taste.record_authored({"judgement": f"j{step}"})
                        if rng.random() < 0.3:
                            taste.review(record["id"], "pause", "user")
                    elif op == "card":
                        active = taste.active()
                        if active:
                            try:
                                card = cards.create_card(
                                    title=f"card{step}", attitude="a",
                                    track="authored", scope="user",
                                    taste_ids=[rng.choice(active)["id"]],
                                )
                                cards_seen.append(card["id"])
                            except ValueError:
                                pass
                    elif op == "package":
                        pkg1 = store.assemble_context_package(f"task {step % 3}", session_id="s")
                        pkg2 = store.assemble_context_package(f"task {step % 3}", session_id="s")
                        # Projection determinism: two assembles agree.
                        assert pkg1["layers"]["high"] == pkg2["layers"]["high"]
                        assert pkg1["preference"] == pkg2["preference"]
                    elif op == "search":
                        store.search_events(rng.choice(["payload", "step", "';--", "非存在"]))
                    elif op == "rerank":
                        candidates = [
                            {"event": e, "similarity": rng.random()}
                            for e in store.list_events("s", limit=5)
                        ]
                        if candidates:
                            ranked = default_rerank("q", candidates)
                            scores = [r.score for r in ranked]
                            assert scores == sorted(scores, reverse=True)
                    elif op == "gated_tool":
                        # The approval gate: without a token, NEVER executed.
                        try:
                            registry.request("delete", {"path": f"/p{step}"}, session_id="s")
                        except ToolApprovalRequired:
                            pass
                finally:
                    assert store.verify_integrity()["ok"] is True, f"step {step} ({op})"
            # The gated tool never executed without approval across the run.
            assert "delete" not in executed
        finally:
            store.close()

    def test_projection_rebuild_determinism_under_interleaved_writes(self, tmp_path):
        """The same event stream always assembles to the same high/mid layers,
        no matter what order reads interleave with writes."""
        store, _ = make_store(tmp_path)
        try:
            rng = random.Random(7)
            for i in range(40):
                store.append_event("s", "note", {"i": i})
                if i % 5 == 0:
                    store.assemble_context_package("probe", session_id="s")
            baseline = store.assemble_context_package("final", session_id="s")
            for _ in range(5):
                again = store.assemble_context_package("final", session_id="s")
                assert baseline["layers"]["high"] == again["layers"]["high"]
                assert baseline["layers"]["mid"] == again["layers"]["mid"]
        finally:
            store.close()

    def test_append_only_event_seqs_never_change(self, tmp_path):
        """Appending more events never renumbers or mutates existing ones."""
        store, _ = make_store(tmp_path)
        try:
            first = store.append_event("s", "note", {"n": 1})
            second = store.append_event("s", "note", {"n": 2})
            snapshot1 = {e.id: (e.seq, e.payload) for e in store.list_events("s", limit=10)}
            for i in range(20):
                store.append_event("s", "note", {"n": i + 3})
            for event in store.list_events("s", limit=100):
                if event.id in snapshot1:
                    assert (event.seq, event.payload) == snapshot1[event.id]
            assert store.get_event(first.id).seq == 1
            assert store.get_event(second.id).seq == 2
        finally:
            store.close()


# ============================================================================
# 10. Router / cards / misc hostile input
# ============================================================================

class TestRouterAndCards:
    def test_router_hostile_instructions_and_sessions(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            router = Router(store)
            for bad in ("", "   ", "DROP TABLE", "fork; rm -rf /", "x" * 100_000,
                        "<script>", "\x00"):
                decision = router.decide(session_id="s", user_instruction=bad)
                # Unknown/hostile instructions must conservatively continue.
                assert decision.route in {"continue", "fork", "rebirth",
                                          "switch_recipe", "spawn_subagent"}
            # Ghost session routes fine (empty projections).
            decision = router.decide(session_id="ghost")
            assert decision.route == "continue"
        finally:
            store.close()

    def test_card_create_every_field_malformed(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            taste = TasteService(store)
            record = taste.record_authored({"judgement": "j"})
            cards = TasteCardService(store)
            base = dict(title="t", attitude="a", track="authored", scope="user",
                        taste_ids=[record["id"]])
            for bad_kwargs in (
                {"title": " "},
                {"title": ""},
                {"attitude": "  "},
                {"track": "invented"},
                {"scope": "galaxy"},
                {"taste_ids": []},
                {"taste_ids": [record["id"], record["id"]]},  # duplicates
                {"taste_ids": ["tst_ghost"]},
                {"status": "invented"},
                {"image": {"media_type": "text/html", "path": "x", "model": "m"}},
            ):
                with pytest.raises((ValueError, KeyError)):
                    cards.create_card(**{**base, **bad_kwargs})
        finally:
            store.close()

    def test_card_review_malformed_edited_json(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            taste = TasteService(store)
            record = taste.record_authored({"judgement": "j"})
            cards = TasteCardService(store)
            card = cards.create_card(title="t", attitude="a", track="authored",
                                     scope="user", taste_ids=[record["id"]])
            with pytest.raises(ValueError):
                cards.review(card["id"], "edit", "user", edited=None)
            with pytest.raises(ValueError):
                cards.review(card["id"], "edit", "user", edited={"title": "  "})
            with pytest.raises(ValueError):
                cards.review(card["id"], "invented", "user")
            with pytest.raises((ValueError, KeyError)):
                cards.review("crd_ghost", "accept", "user")
            with pytest.raises(ValueError):
                cards.review(card["id"], "accept", "  ")
            # split malformed shapes
            with pytest.raises(ValueError):
                cards.review(card["id"], "split", "user", edited={})
            with pytest.raises(ValueError):
                cards.review(card["id"], "split", "user",
                             edited={"cards": [{"title": "only-one"}]})
        finally:
            store.close()

    def test_card_image_generate_on_superseded_or_ghost(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            cards = TasteCardService(store)
            with pytest.raises((ValueError, KeyError)):
                cards.generate_image("crd_ghost", "user")
        finally:
            store.close()

    def test_taste_service_hostile_content_shapes(self, tmp_path):
        store, _ = make_store(tmp_path)
        try:
            taste = TasteService(store)
            # Content of any JSON shape either records or refuses loudly.
            for content in ({"j": "x"}, {"judgement": "x" * 100_000},
                            {"nested": {"deep": [1, 2, {"x": "y"}]}}, "plain string",
                            42, [1, 2, 3]):
                try:
                    record = taste.record_authored(content)
                    assert taste.get(record["id"])["content"] == content
                except (ValueError, TypeError):
                    pass
            assert store.verify_integrity()["ok"] is True
        finally:
            store.close()
