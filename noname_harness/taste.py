"""Taste layer service: authored and adopted tracks, never a fact.

Taste is a first-class citizen in NoName, but it is strictly separated from
memory/facts.  It lives in its own tables, is reviewed through its own
actions, and only ever appears in a context package inside an explicit
``preference`` section marked as a soft influence.  It must never be
presented as evidence for a factual claim.

Two source tracks are honoured:

- **authored**: attitudes the user wrote, confirmed or maintains directly.
  Writing one is itself the act of confirmation, so it becomes active
  immediately, but it is still versioned and traceable.
- **adopted**: a tendency the model expressed that the user explicitly chose
  to keep.  It always starts as a ``candidate`` and only becomes ``active``
  after an explicit ``adopt`` review, so an adopted taste can never pretend
  to be the user's own words.

All taste rows are append-only.  Correction writes a new row that supersedes
the old one; nothing is hard-deleted.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .store import (
    VALID_TASTE_ACTIONS,
    VALID_TASTE_SCOPES,
    VALID_TASTE_TRACKS,
    HarnessStore,
    _decode,
    _id,
    _json,
    _now,
)


class TasteService:
    """Manage the taste layer without ever touching fact projections."""

    def __init__(self, store: HarnessStore):
        self.store = store

    # ------------------------------------------------------------------
    # authoring / proposing
    # ------------------------------------------------------------------
    def record_authored(
        self,
        content: Any,
        *,
        scope: str = "user",
        source_event_ids: Sequence[str] | None = None,
        actor_id: str = "user",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Record an authored taste; writing it is the act of confirmation.

        Authored taste carries the highest authority, so it is created
        ``active`` directly.  Source events are optional for authored taste
        (the user *is* the source), but providing them keeps provenance
        complete when the attitude was first expressed in a session.
        """

        self._validate(scope=scope, track="authored")
        if source_event_ids:
            self.store._check_event_ids(source_event_ids)
        return self._insert_taste(
            track="authored",
            scope=scope,
            content=content,
            status="active",
            source_event_ids=list(source_event_ids or []),
            origin="authored",
            actor_id=actor_id,
            reason=reason,
        )

    def propose_adopted(
        self,
        content: Any,
        *,
        scope: str = "user",
        source_event_ids: Sequence[str],
        proposed_by: str = "model",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Propose an adopted taste candidate from a model-observed moment.

        Adopted taste must cite at least one source event (the answer, work
        or emergence that caught the user's eye) and always enters as a
        ``candidate``.  It only becomes active after an explicit ``adopt``.
        """

        self._validate(scope=scope, track="adopted")
        if not source_event_ids:
            raise ValueError("adopted taste must cite at least one source event")
        self.store._check_event_ids(source_event_ids)
        return self._insert_taste(
            track="adopted",
            scope=scope,
            content=content,
            status="candidate",
            source_event_ids=list(source_event_ids),
            origin="adopted",
            actor_id=proposed_by,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # review
    # ------------------------------------------------------------------
    def review(
        self,
        taste_id: str,
        action: str,
        reviewer_id: str,
        *,
        edited_content: Any | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Apply a review action to a taste record, writing a new version.

        Actions:
        - ``adopt``: candidate -> active (adopted track only);
        - ``edit``: any status -> active with new content (new version);
        - ``pause`` / ``resume`` / ``retire``: lifecycle transitions.
        """

        if action not in VALID_TASTE_ACTIONS:
            raise ValueError(f"invalid taste action: {action}")
        if not reviewer_id.strip():
            raise ValueError("reviewer_id cannot be empty")
        row = self._get_row(taste_id)
        if action == "adopt" and row["track"] != "adopted":
            raise ValueError("only adopted taste can be adopted")
        if action == "adopt" and row["status"] != "candidate":
            raise ValueError("only a candidate can be adopted")
        if action == "edit" and edited_content is None:
            raise ValueError("edited_content is required for edit")

        new_content = edited_content if action == "edit" else _decode(row["content_json"])
        new_status = {
            "adopt": "active",
            "edit": "active",
            "pause": "paused",
            "resume": "active",
            "retire": "retired",
        }[action]

        review_id = _id("trv")
        reviewed_at = _now()
        with self.store._transaction() as connection:
            connection.execute(
                "INSERT INTO taste_reviews "
                "(id, taste_id, action, reviewer_id, edited_content_json, reason, reviewed_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    taste_id,
                    action,
                    reviewer_id,
                    _json(edited_content) if action == "edit" else None,
                    reason,
                    reviewed_at,
                ),
            )
            # A review produces a new immutable record that supersedes the old
            # one; the previous row stays in place for provenance.
            new_id = _id("tst")
            connection.execute(
                "INSERT INTO taste_records "
                "(id, track, scope, content_json, status, source_event_ids_json, "
                "supersedes_id, origin, actor_id, reason, recorded_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id,
                    row["track"],
                    row["scope"],
                    _json(new_content),
                    new_status,
                    row["source_event_ids_json"],
                    taste_id,
                    row["origin"],
                    reviewer_id,
                    reason,
                    reviewed_at,
                ),
            )
            # Keep the ledger honest: every taste transition is also an event.
            source_session = self._source_session(connection, row)
            self.store._insert_event(
                connection,
                source_session,
                "taste.reviewed",
                {
                    "taste_id": taste_id,
                    "new_taste_id": new_id,
                    "track": row["track"],
                    "action": action,
                    "reviewer_id": reviewer_id,
                    "new_status": new_status,
                    "reason": reason,
                },
                occurred_at=reviewed_at,
            )
        return self.get(new_id)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def get(self, taste_id: str) -> dict[str, Any]:
        row = self._get_row(taste_id)
        reviews = self.store._connection.execute(
            "SELECT * FROM taste_reviews WHERE taste_id = ? ORDER BY reviewed_at, rowid",
            (taste_id,),
        ).fetchall()
        return {
            "id": row["id"],
            "track": row["track"],
            "scope": row["scope"],
            "content": _decode(row["content_json"]),
            "status": row["status"],
            "source_event_ids": _decode(row["source_event_ids_json"]),
            "supersedes_id": row["supersedes_id"],
            "origin": row["origin"],
            "actor_id": row["actor_id"],
            "reason": row["reason"],
            "recorded_at": row["recorded_at"],
            "reviews": [
                {
                    "id": review["id"],
                    "action": review["action"],
                    "reviewer_id": review["reviewer_id"],
                    "edited_content": _decode(review["edited_content_json"])
                    if review["edited_content_json"] is not None
                    else None,
                    "reason": review["reason"],
                    "reviewed_at": review["reviewed_at"],
                }
                for review in reviews
            ],
        }

    def active(self, scope: str | None = None) -> list[dict[str, Any]]:
        """Return the current active taste projection, optionally by scope."""

        return self._latest_by_status("active", scope=scope)

    def pending(self) -> list[dict[str, Any]]:
        """Return adopted candidates still waiting for an explicit adopt."""

        return self._latest_by_status("candidate", scope=None)

    def _latest_by_status(
        self, status: str, scope: str | None
    ) -> list[dict[str, Any]]:
        rows = self.store._connection.execute(
            "SELECT * FROM taste_records ORDER BY recorded_at, rowid"
        ).fetchall()
        superseded = {row["supersedes_id"] for row in rows if row["supersedes_id"]}
        latest: dict[str, Any] = {}
        for row in rows:
            if row["id"] in superseded:
                continue
            # The newest non-superseded row per (track, scope, content-root)
            # wins.  Because every version supersedes its parent, following
            # the chain leaves exactly one head per logical taste line.
            key = self._lineage_key(row)
            previous = latest.get(key)
            if previous is None or (row["recorded_at"], row["id"]) > (
                previous["recorded_at"],
                previous["id"],
            ):
                latest[key] = row
        result = []
        for row in latest.values():
            if row["status"] != status:
                continue
            if scope is not None and row["scope"] != scope:
                continue
            result.append(self.get(row["id"]))
        return sorted(result, key=lambda item: (item["track"], item["id"]))

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _insert_taste(
        self,
        *,
        track: str,
        scope: str,
        content: Any,
        status: str,
        source_event_ids: list[str],
        origin: str,
        actor_id: str,
        reason: str | None,
    ) -> dict[str, Any]:
        taste_id = _id("tst")
        recorded_at = _now()
        with self.store._transaction() as connection:
            connection.execute(
                "INSERT INTO taste_records "
                "(id, track, scope, content_json, status, source_event_ids_json, "
                "supersedes_id, origin, actor_id, reason, recorded_at) "
                "VALUES(?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (
                    taste_id,
                    track,
                    scope,
                    _json(content),
                    status,
                    _json(list(dict.fromkeys(source_event_ids))),
                    origin,
                    actor_id,
                    reason,
                    recorded_at,
                ),
            )
            source_session = (
                self._source_session_from_ids(connection, source_event_ids)
                if source_event_ids
                else "user"
            )
            self.store._insert_event(
                connection,
                source_session,
                "taste.proposed",
                {
                    "taste_id": taste_id,
                    "track": track,
                    "scope": scope,
                    "status": status,
                    "source_event_ids": list(dict.fromkeys(source_event_ids)),
                    "reason": reason,
                },
                occurred_at=recorded_at,
            )
        return self.get(taste_id)

    def _get_row(self, taste_id: str) -> Any:
        row = self.store._connection.execute(
            "SELECT * FROM taste_records WHERE id = ?", (taste_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown taste record: {taste_id}")
        return row

    def _lineage_key(self, row: Any) -> str:
        """Return the id of the root record of this taste's version chain."""

        current = row
        seen = {row["id"]}
        while current["supersedes_id"] is not None:
            parent = self.store._connection.execute(
                "SELECT * FROM taste_records WHERE id = ?",
                (current["supersedes_id"],),
            ).fetchone()
            if parent is None or parent["id"] in seen:  # pragma: no cover - defensive
                break
            seen.add(parent["id"])
            current = parent
        return current["id"]

    @staticmethod
    def _validate(*, scope: str, track: str) -> None:
        if track not in VALID_TASTE_TRACKS:
            raise ValueError(f"invalid taste track: {track}")
        if scope not in VALID_TASTE_SCOPES:
            raise ValueError(f"invalid taste scope: {scope}")

    @staticmethod
    def _source_session(connection: Any, row: Any) -> str:
        ids = _decode(row["source_event_ids_json"])
        return TasteService._source_session_from_ids(connection, ids)

    @staticmethod
    def _source_session_from_ids(connection: Any, ids: Iterable[str]) -> str:
        ids = list(ids)
        if not ids:
            return "user"
        row = connection.execute(
            "SELECT session_id FROM session_events WHERE id = ?", (ids[0],)
        ).fetchone()
        return row["session_id"] if row else "user"
