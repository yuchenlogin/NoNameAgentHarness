"""Resume: a waiting_approval run continues from the event stream, not a new run.

The gate rules under test:

- only a run whose last ``loop.finished`` has stop_reason=waiting_approval may
  resume (COMPLETED / FAILED / user-cancelled runs are rejected loudly);
- one resume per pause, seq-scoped: ``loop.resumed`` vetoes only the pause
  it answered; a later pause gets a fresh resume (checked + claimed inside a
  single BEGIN IMMEDIATE transaction, so concurrent actors cannot both pass);
- the approval token must be a live grant for the pending tool, and is
  consumed only when the resumed run actually executes the call;
- rounds are inherited from the recorded transitions OF THE CURRENT RUN
  (after the latest loop.started), so max_rounds binds across the pause and
  earlier runs in the same session never spend this run's budget -- resume is
  never a budget backdoor;
- the whole continuation works across store connections (cross-actor/process).
"""

from __future__ import annotations

import pytest

from noname_harness.agent_loop import AgentLoop, AgentLoopError, LoopResult
from noname_harness.store import HarnessStore
from noname_harness.tools import Tool, ToolRegistry, ToolSchema

SESSION = "s"

def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "resume project")
    return store, root

def make_registry(store, executed=None):
    """A registry with one free read tool and one gated destructive tool."""

    def _track(name, fn):
        def run(a):
            if executed is not None:
                executed.append(name)
            return fn(a)
        return run

    registry = ToolRegistry(store)
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

class ResumingDriver:
    """Deterministic stub modelling the production continuation pattern.

    Behaviour per act() call:
    - ``pre_rounds`` ordinary rounds first (to bank rounds before the pause);
    - on a turn whose context carries ``resume`` (first turn of a resumed
      run), re-issue the pending gated call carrying the token id;
    - otherwise request the gated call (pauses the run on waiting_approval);
    - after the gated call executed (last_tool_result present), complete.
    """

    def __init__(self, pre_rounds=0):
        self._pre = pre_rounds
        self.seen_resume = None
        self._resume_consumed = False
        self.calls = 0

    def act(self, context, last_tool_result=None):
        self.calls += 1
        if self._pre > 0:
            self._pre -= 1
            return LoopResult(output="working")
        # The loop injects context['resume'] on the first resumed turn; a real
        # driver acts on it once and moves on, so the stub consumes it once.
        resume = context.get("resume")
        if resume is not None and not self._resume_consumed:
            self._resume_consumed = True
            self.seen_resume = dict(resume)
            return LoopResult(tool_call={
                "name": resume["pending_tool"],
                "arguments": {"path": "/tmp/x"},
                "approval_token": resume["approval_token_id"],
            })
        if last_tool_result is not None:
            # The gated call executed: the task is done.
            return LoopResult(task_complete=True)
        if self.seen_resume is not None:
            # Resumed but the gated call never produced a result: stop cleanly
            # rather than re-requesting it without a token.
            return LoopResult(task_complete=True)
        return LoopResult(tool_call={"name": "delete", "arguments": {"path": "/tmp/x"}})

class ResolvingDriver:
    """Wrap a stub driver, rehydrating approval_token id strings into live
    tokens -- the exact step AdapterDriver._map_tool_call performs on the real
    path, so the loop under test is driven identically."""

    def __init__(self, inner, registry):
        self.inner = inner
        self.registry = registry

    def act(self, context, last_tool_result=None):
        result = self.inner.act(context, last_tool_result)
        for call in ([result.tool_call] if result.tool_call else (result.tool_calls or [])):
            token = call.get("approval_token")
            if isinstance(token, str):
                call["approval_token"] = self.registry.get_live_token(token)
        return result

def pause_a_run(store, registry, session_id=SESSION, driver=None, max_rounds=10):
    """Run a loop until it pauses on the gated tool; return the driver."""
    driver = driver or ResumingDriver()
    loop = AgentLoop(
        store=store, session_id=session_id, driver=driver,
        max_rounds=max_rounds, tool_registry=registry,
    )
    summary = loop.run("t")
    assert summary["final_state"] == "CANCELLED"
    assert summary["stop_reason"] == "waiting_approval"
    assert summary["output"]["pending_tool"] == "delete"
    return driver

def grant(registry, session_id=SESSION):
    return registry.grant_approval(
        "delete", {"path": "/tmp/x"}, approver_id="user", session_id=session_id,
    )

def test_waiting_approval_run_resumes_to_completed(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        executed = []
        registry = make_registry(store, executed)
        driver = pause_a_run(store, registry)
        token = grant(registry)
        summary = AgentLoop.resume(
            store, SESSION, ResolvingDriver(driver, registry),
            approval_token=token, tool_registry=registry, actor_id="user",
        )
        assert summary["final_state"] == "COMPLETED"
        assert summary["stop_reason"] == "task_complete"
        # Rounds continue: 1 paused round + gated re-issue + completion.
        assert summary["rounds"] == 3
        assert executed == ["delete"]
        # The driver saw the pending-tool info on the resumed first turn.
        assert driver.seen_resume == {
            "pending_tool": "delete",
            "pending_index": 0,
            "approval_token_id": token.id,
        }
        # The machine re-entered legally from CANCELLED, rounds inherited.
        transitions = [
            e.payload for e in store.list_events(SESSION, limit=200)
            if e.event_type == "loop.transition"
        ]
        reentry = next(
            t for t in transitions
            if t["from"] == "CANCELLED" and t["to"] == "ASSEMBLING_CONTEXT"
        )
        assert reentry["round"] == 1
        # No new run was started: exactly one loop.started in the whole stream.
        started = [e for e in store.list_events(SESSION, limit=200) if e.event_type == "loop.started"]
        assert len(started) == 1
    finally:
        store.close()

def test_rounds_are_inherited_and_round_limit_binds_across_resume(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = make_registry(store)
        # 3 ordinary rounds, then the gated call pauses the run at round 4.
        driver = ResumingDriver(pre_rounds=3)
        pause_a_run(store, registry, driver=driver, max_rounds=5)
        token = grant(registry)

        class NeverDone:
            def __init__(self):
                self.first = True
            def act(self, context, last_tool_result=None):
                resume = context.get("resume")
                if self.first and resume is not None:
                    self.first = False
                    return LoopResult(tool_call={
                        "name": resume["pending_tool"],
                        "arguments": {"path": "/tmp/x"},
                        "approval_token": resume["approval_token_id"],
                    })
                return LoopResult(output="again")

        summary = AgentLoop.resume(
            store, SESSION, ResolvingDriver(NeverDone(), registry),
            approval_token=token, tool_registry=registry,
        )
        # 4 banked rounds + exactly 1 more: max_rounds=5 binds across resume.
        assert summary["final_state"] == "FAILED"
        assert summary["stop_reason"] == "round_limit"
        assert summary["rounds"] == 5
    finally:
        store.close()

@pytest.mark.parametrize("terminal", ["completed", "failed", "user_cancelled"])
def test_resume_rejects_non_waiting_approval_runs(tmp_path, terminal):
    store, _ = make_store(tmp_path)
    try:
        registry = make_registry(store)
        if terminal == "completed":
            class Done:
                def act(self, context, last_tool_result=None):
                    return LoopResult(task_complete=True)
            AgentLoop(store=store, session_id=SESSION, driver=Done(), tool_registry=registry).run("t")
        else:
            class Forever:
                def act(self, context, last_tool_result=None):
                    return LoopResult(output="x")
            loop = AgentLoop(store=store, session_id=SESSION, driver=Forever(), max_rounds=1)
            if terminal == "user_cancelled":
                loop.max_rounds = 10
                loop.cancel("user cancelled")
            loop.run("t")
        with pytest.raises(AgentLoopError, match="waiting_approval"):
            AgentLoop.resume(
                store, SESSION, ResumingDriver(),
                approval_token=grant(registry), tool_registry=registry,
            )
        # Rejection is loud and clean: no resume/transition events written.
        types = [e.event_type for e in store.list_events(SESSION, limit=200)]
        assert "loop.resumed" not in types
    finally:
        store.close()

def test_resume_rejects_session_with_no_run(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = make_registry(store)
        with pytest.raises(AgentLoopError, match="no finished run"):
            AgentLoop.resume(
                store, SESSION, ResumingDriver(),
                approval_token="tok", tool_registry=registry,
            )
    finally:
        store.close()

def test_replay_guard_vetoes_second_resume_of_same_pause(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = make_registry(store)
        pause_a_run(store, registry)
        token = grant(registry)
        # Resume into a SECOND pause: the resumed driver requests the gated
        # call again without a token, so the run ends waiting_approval once
        # more -- with loop.resumed already in the ledger.
        class SecondPause:
            def __init__(self):
                self.first = True
            def act(self, context, last_tool_result=None):
                resume = context.get("resume")
                if self.first and resume is not None:
                    self.first = False
                    return LoopResult(tool_call={
                        "name": resume["pending_tool"],
                        "arguments": {"path": "/tmp/x"},
                        "approval_token": resume["approval_token_id"],
                    })
                # Token already consumed: this pauses the run again.
                return LoopResult(tool_call={"name": "delete", "arguments": {"path": "/tmp/x"}})
        summary = AgentLoop.resume(
            store, SESSION, ResolvingDriver(SecondPause(), registry),
            approval_token=token, tool_registry=registry,
        )
        assert summary["stop_reason"] == "waiting_approval"
        # Per-pause scoping (F3): the SECOND pause is a new pause (a newer
        # waiting_approval finish), so the first pause's loop.resumed does NOT
        # veto it -- a fresh grant resumes it successfully.  (Double-resuming
        # the SAME pause is covered by test_double_resume_of_same_pause_rejected.)
        summary2 = AgentLoop.resume(
            store, SESSION, ResolvingDriver(ResumingDriver(), registry),
            approval_token=grant(registry), tool_registry=registry,
        )
        assert summary2["final_state"] == "COMPLETED"
    finally:
        store.close()

def test_resume_event_is_recorded_and_replayable(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = make_registry(store)
        pause_a_run(store, registry)
        token = grant(registry)
        AgentLoop.resume(
            store, SESSION, ResolvingDriver(ResumingDriver(), registry),
            approval_token=token, tool_registry=registry, actor_id="operator-1",
        )
        events = store.list_events(SESSION, limit=200)
        resumed = next(e for e in events if e.event_type == "loop.resumed")
        assert resumed.payload["actor_id"] == "operator-1"
        assert resumed.payload["resumed_from"] == "waiting_approval"
        assert resumed.payload["pending_tool"] == "delete"
        assert resumed.payload["approval_token_id"] == token.id
        assert resumed.payload["inherited_rounds"] == 1
        # reconstruct() sees the resume and the new terminal state.
        recovered = AgentLoop.reconstruct(store, SESSION, ResumingDriver())
        assert recovered.state == "COMPLETED"
        assert recovered.rounds == 3
        assert any(
            e.event_type == "loop.resumed" for e in store.list_events(SESSION, limit=200)
        )
    finally:
        store.close()

def test_token_is_consumed_by_resume_execution_and_needs_a_new_grant(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        executed = []
        registry = make_registry(store, executed)
        pause_a_run(store, registry)
        token = grant(registry)
        summary = AgentLoop.resume(
            store, SESSION, ResolvingDriver(ResumingDriver(), registry),
            approval_token=token, tool_registry=registry,
        )
        assert summary["final_state"] == "COMPLETED"
        assert executed == ["delete"]
        # The one-time token is consumed: re-presenting it must not authorise.
        assert registry.get_live_token(token.id) is None
        assert registry.requires_approval_for("delete", token) is True
        # A fresh request with the same arguments and the spent token pauses
        # again -- approval semantics were not weakened by the resume.
        driver = ResumingDriver()
        driver._pre = 0
        summary2 = AgentLoop(
            store=store, session_id="s2", driver=driver, tool_registry=registry,
        ).run("t")
        assert summary2["stop_reason"] == "waiting_approval"
        # Presenting the CONSUMED token to resume is rejected loudly.
        with pytest.raises(AgentLoopError, match="not a live grant"):
            AgentLoop.resume(
                store, "s2", ResumingDriver(),
                approval_token=token, tool_registry=registry,
            )
        # A NEW grant for the same call resumes fine.
        token2 = registry.grant_approval(
            "delete", {"path": "/tmp/x"}, approver_id="user", session_id="s2",
        )
        summary3 = AgentLoop.resume(
            store, "s2", ResolvingDriver(ResumingDriver(), registry),
            approval_token=token2, tool_registry=registry,
        )
        assert summary3["final_state"] == "COMPLETED"
        assert executed == ["delete", "delete"]
    finally:
        store.close()

def test_resume_across_store_connections(tmp_path):
    """Pause on one HarnessStore connection; resume from a fresh connection
    (a new actor/process) with a registry rebuilt from the ledger."""
    root = tmp_path / "project"
    root.mkdir()
    db = root / ".noname" / "harness.db"
    token_id = None
    with HarnessStore(db) as store1:
        store1.initialize_project(root, "resume project")
        registry1 = make_registry(store1)
        pause_a_run(store1, registry1)
        token_id = grant(registry1).id
    # New connection, new registry: grants rebuilt from the event stream.
    with HarnessStore(db) as store2:
        registry2 = make_registry(store2)
        token = registry2.get_live_token(token_id)
        assert token is not None
        summary = AgentLoop.resume(
            store2, SESSION, ResolvingDriver(ResumingDriver(), registry2),
            approval_token=token, tool_registry=registry2, actor_id="other-process",
        )
        assert summary["final_state"] == "COMPLETED"
        assert summary["rounds"] == 3
        events = store2.list_events(SESSION, limit=200)
        assert any(e.event_type == "loop.resumed" for e in events)

def test_resumed_run_can_be_cancelled_and_stale_cancels_stay_dead(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = make_registry(store)
        # A cancel from BEFORE the original finish must not poison the resume.
        store.append_event(SESSION, "loop.cancel_requested", {"reason": "旧请求"})
        pause_a_run(store, registry)
        token = grant(registry)
        summary = AgentLoop.resume(
            store, SESSION, ResolvingDriver(ResumingDriver(), registry),
            approval_token=token, tool_registry=registry,
        )
        # The stale cancel belonged to the finished (paused) run; the resumed
        # run completes normally.
        assert summary["final_state"] == "COMPLETED"
    finally:
        store.close()

def test_live_cancel_stops_a_resumed_run(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = make_registry(store)
        pause_a_run(store, registry)
        token = grant(registry)
        # A NEW cancel after the pause belongs to the resumed run.
        store.append_event(SESSION, "loop.cancel_requested", {"reason": "别跑了"})
        summary = AgentLoop.resume(
            store, SESSION, ResolvingDriver(ResumingDriver(), registry),
            approval_token=token, tool_registry=registry,
        )
        assert summary["final_state"] == "CANCELLED"
        assert summary["stop_reason"] == "别跑了"
    finally:
        store.close()

def test_resume_requires_live_token_for_the_pending_tool(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        registry = make_registry(store)
        pause_a_run(store, registry)
        # A token for DIFFERENT arguments is not a grant for the pending call.
        other = registry.grant_approval(
            "delete", {"path": "/tmp/other"}, approver_id="user", session_id=SESSION,
        )
        with pytest.raises(AgentLoopError, match="not a live grant"):
            AgentLoop.resume(
                store, SESSION, ResumingDriver(),
                approval_token=other, tool_registry=registry,
            )
        # No partial resume happened.
        types = [e.event_type for e in store.list_events(SESSION, limit=200)]
        assert "loop.resumed" not in types
        # And resume without a registry at all is rejected.
        token = grant(registry)
        with pytest.raises(AgentLoopError, match="tool_registry"):
            AgentLoop.resume(store, SESSION, ResumingDriver(), approval_token=token)
    finally:
        store.close()


# ----------------------------------------------------------------------
# Regression tests for the adversarial review findings (F1/F3/F4/F5).
# ----------------------------------------------------------------------

class RePauseDriver:
    """Resume-turn: re-issue the pending gated call with the token id; after
    the gated result lands, request the gated call WITHOUT a token (pauses
    again); completes when ``done`` is set (mutate between resumes)."""

    def __init__(self):
        self.first = True
        self.done = False

    def act(self, context, last_tool_result=None):
        resume = context.get("resume")
        if self.first and resume is not None:
            self.first = False
            return LoopResult(tool_call={
                "name": resume["pending_tool"],
                "arguments": {"path": "/tmp/x"},
                "approval_token": resume["approval_token_id"],
            })
        if self.done:
            return LoopResult(task_complete=True)
        # No token left: this pauses the run on waiting_approval again.
        return LoopResult(tool_call={"name": "delete", "arguments": {"path": "/tmp/x"}})


def test_legitimate_pause_resume_pause_resume_cycle(tmp_path):
    """F3 (probe A3): gate 2 is per-pause, not per-session.  A run that pauses
    AGAIN after a successful resume gets its own resume with a fresh grant."""
    store, _ = make_store(tmp_path)
    try:
        registry = make_registry(store)
        pause_a_run(store, registry)
        driver = RePauseDriver()
        summary = AgentLoop.resume(
            store, SESSION, ResolvingDriver(driver, registry),
            approval_token=grant(registry), tool_registry=registry,
        )
        assert summary["stop_reason"] == "waiting_approval"
        resumed_events = [
            e for e in store.list_events(SESSION, limit=300)
            if e.event_type == "loop.resumed"
        ]
        assert len(resumed_events) == 1
        # The second pause is resumeable: loop.resumed #1 predates pause #2's
        # finish, so gate 2 does not veto it.
        driver.done = True
        summary2 = AgentLoop.resume(
            store, SESSION, ResolvingDriver(driver, registry),
            approval_token=grant(registry), tool_registry=registry,
        )
        assert summary2["final_state"] == "COMPLETED"
        resumed_events = [
            e for e in store.list_events(SESSION, limit=300)
            if e.event_type == "loop.resumed"
        ]
        assert len(resumed_events) == 2
    finally:
        store.close()


def test_double_resume_of_same_pause_rejected(tmp_path):
    """Gate 2 still vetoes resuming the SAME pause twice: a second resume
    attempt while the pause is still the latest finish (claim committed,
    drive abandoned before re-pausing) is refused, even with a fresh grant."""
    store, _ = make_store(tmp_path)
    try:
        registry = make_registry(store)
        pause_a_run(store, registry)
        token = grant(registry)

        # Abandon the resumed run's drive BEFORE any transition is written:
        # loop.resumed is committed by the gate transaction, but the latest
        # loop.finished remains the ORIGINAL pause.  (Letting a driver raise
        # mid-flight would write an unrecoverable_error finish, which gate 1
        # -- not gate 2 -- would veto; that is gate 1's job.)
        original = AgentLoop.resume.__globals__["AgentLoop"]._drive

        def skipped_drive(self, task, *, task_type=None):
            return {"final_state": "ABANDONED", "stop_reason": "test_abort"}

        # A fresh grant for the SAME pending call: gate 3 would pass, so only
        # gate 2 can veto the retry below.
        fresh_token = grant(registry)
        try:
            AgentLoop._drive = skipped_drive
            summary = AgentLoop.resume(
                store, SESSION, ResumingDriver(),
                approval_token=token, tool_registry=registry,
            )
        finally:
            AgentLoop._drive = original
        assert summary["final_state"] == "ABANDONED"
        # The claim was committed even though the drive was abandoned.
        assert any(
            e.event_type == "loop.resumed"
            for e in store.list_events(SESSION, limit=300)
        )
        # The claim survived the abandoned drive and vetoes re-resuming the
        # SAME pause -- this is exactly the F1 double-run guard.
        with pytest.raises(AgentLoopError, match="already resumed"):
            AgentLoop.resume(
                store, SESSION, ResumingDriver(),
                approval_token=fresh_token, tool_registry=registry,
            )
    finally:
        store.close()


def test_resume_round_inheritance_is_scoped_to_the_current_run(tmp_path):
    """F4 (probe C4): run1 banks 9 rounds and COMPLETES; run2 (same session,
    fresh loop) pauses at its own round 1.  Resume inherits run2's rounds
    only -- run1's budget does not bleed into run2."""
    store, _ = make_store(tmp_path)
    try:
        registry = make_registry(store)

        class NineRounds:
            def __init__(self):
                self.n = 0

            def act(self, context, last_tool_result=None):
                self.n += 1
                if self.n >= 9:
                    return LoopResult(task_complete=True)
                return LoopResult(output=f"r{self.n}")

        s1 = AgentLoop(store=store, session_id=SESSION, driver=NineRounds(),
                       max_rounds=10, tool_registry=registry).run("run1")
        assert s1["rounds"] == 9 and s1["final_state"] == "COMPLETED"

        # run2: a NEW AgentLoop (rounds restart at 0) pauses at its round 1.
        pause_a_run(store, registry)
        summary = AgentLoop.resume(
            store, SESSION, ResolvingDriver(ResumingDriver(), registry),
            approval_token=grant(registry), tool_registry=registry,
        )
        # Inherited 1 (run2's own pause round), +2 continuation rounds
        # (gated call + task_complete) -- NOT 9 inherited from run1.
        assert summary["final_state"] == "COMPLETED"
        assert summary["rounds"] == 3
        resumed = next(
            e for e in store.list_events(SESSION, limit=300)
            if e.event_type == "loop.resumed"
        )
        assert resumed.payload["inherited_rounds"] == 1
    finally:
        store.close()


def test_concurrent_resume_allows_exactly_one_winner(tmp_path):
    """F1 (probe e1_race): two actor processes race AgentLoop.resume on the
    same paused session and token.  The gate check + loop.resumed claim run
    inside one BEGIN IMMEDIATE transaction, so exactly one actor commits the
    claim; the loser blocks on the write lock, re-reads the ledger, and is
    rejected by gate 2 BEFORE driving -- no duplicate run at all."""
    import multiprocessing as mp
    import time

    root = tmp_path / "project"
    root.mkdir()
    db = root / ".noname" / "harness.db"
    store = HarnessStore(db)
    store.initialize_project(root, "race")
    registry = make_registry(store)
    pause_a_run(store, registry)
    token_id = grant(registry).id
    store.close()

    ctx = mp.get_context("spawn")
    result_q = ctx.Queue()
    start_at = time.time() + 1.0
    procs = [
        ctx.Process(
            target=_resume_race_child,
            args=(db, start_at, token_id, result_q, f"actor-{i}"),
        )
        for i in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=90)
        if p.is_alive():
            p.terminate()
            p.join()
    results = []
    while not result_q.empty():
        results.append(result_q.get())

    store = HarnessStore(db)
    try:
        winners = [r for r in results if r[1] == "OK"]
        losers = [
            r for r in results
            if r[1] == "ERR" and r[2].startswith("AgentLoopError") and "already resumed" in r[2]
        ]
        assert len(results) == 2, f"missing child results: {results}"
        assert len(winners) == 1 and len(losers) == 1, (
            "exactly one actor must commit the claim; the other must be "
            f"rejected by gate 2 (not crash): {results}"
        )
        resumed_events = [
            e for e in store.list_events(SESSION, limit=500)
            if e.event_type == "loop.resumed"
        ]
        assert len(resumed_events) == 1, "exactly one resume claim may be committed"
        executed = [
            e for e in store.list_events(SESSION, limit=500)
            if e.event_type == "tool.executed"
        ]
        assert len(executed) <= 1, "the one-time token arbitrates the gated call"
    finally:
        store.close()


def _resume_race_child(db_path, start_at, token_id, result_q, name):
    """Child actor: barrier-synchronised resume attempt on its own connection."""
    import time

    try:
        while time.time() < start_at:
            time.sleep(0.001)
        store = HarnessStore(db_path)
        try:
            registry = make_registry(store)
            token = registry.get_live_token(token_id)
            # The claim (loop.resumed) is committed inside the gate
            # transaction before _drive runs, so the loser raises here with
            # gate 2's "already resumed" instead of driving a duplicate run.
            AgentLoop.resume(
                store, SESSION, ResolvingDriver(ResumingDriver(), registry),
                approval_token=token, tool_registry=registry, actor_id=name,
            )
            result_q.put((name, "OK", "", "winner"))
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 - reported to the parent
        result_q.put((name, "ERR", f"{type(exc).__name__}: {exc}", None))


def test_private_drive_rejects_terminal_state_reentry(tmp_path):
    """F5 (probe D3): _drive is private, but as defence in depth it refuses
    to re-enter from a terminal state without a resume()-prepared scratch
    context.  The public resume() path is unaffected."""
    store, _ = make_store(tmp_path)
    try:
        registry = make_registry(store)
        loop = AgentLoop(store=store, session_id=SESSION, driver=ResumingDriver(),
                         tool_registry=registry)
        loop.run("t")  # pauses -> terminal CANCELLED, no resume scratch
        with pytest.raises(AgentLoopError, match="_drive"):
            loop._drive("t")
    finally:
        store.close()
