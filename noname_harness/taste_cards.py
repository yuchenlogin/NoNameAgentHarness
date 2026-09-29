"""Taste cards: a review experience over taste evidence, never a new fact.

A card is a *view* that groups related taste records so a person can
periodically ask "is this still me?"  It is not a new kind of fact and never
enters the factual layers of a context package.  Cards are append-only and
versioned like everything else; correction writes a new card that supersedes
the old one.

What this prototype deliberately does and does not do:

- **Clustering** is deterministic (time-window + scope + track grouping of
  *reviewed, active* taste records).  It is a conservative placeholder for a
  future semantic/embedding clusterer, which must satisfy the same storage and
  review contract without bypassing it.  Grouping and naming stay explainable.
- **No image generation.**  A card carries an *image metadata contract*
  (model / prompt / seed / version) so a generated visual metaphor can be
  recorded and rebuilt later, but the default is pure typography (``image`` is
  ``None``).  An image is always optional decoration; it is never used to
  infer taste.
- **Review queue is deterministic.**  Cards surface by auditable priority --
  longest-unconfirmed, most-recently-changed taste evidence -- never by random
  rarity, streaks, or paid mechanics.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .store import (
    VALID_CARD_ACTIONS,
    VALID_CARD_STATUSES,
    VALID_CARD_TRACKS,
    HarnessStore,
    _decode,
    _id,
    _json,
    _now,
)
from .taste import TasteService

# Lifecycle transitions for a card.  ``split`` produces new cards and retires
# the original; it is handled specially rather than as a single status change.
_CARD_TRANSITIONS: dict[str, set[str]] = {
    "candidate": {"accept", "edit", "retire", "split"},
    "active": {"edit", "pause", "retire", "split"},
    "paused": {"edit", "resume", "retire", "split"},
    "retired": set(),
}


class TasteCardService:
    """Form, review and project taste cards on top of the taste layer."""

    def __init__(self, store: HarnessStore):
        self.store = store
        self.taste = TasteService(store)

    # ------------------------------------------------------------------
    # clustering (deterministic placeholder)
    # ------------------------------------------------------------------
    def propose_clusters(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        """Group active taste records into candidate card clusters.

        The deterministic rule groups active taste records by (scope, track).
        It is explainable and produces no surprising merges; a future semantic
        clusterer can replace it while honouring the same review contract.
        """

        active = self.taste.active(scope=scope)
        clusters: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in active:
            key = (record["scope"], record["track"])
            clusters.setdefault(key, []).append(record)
        proposals = []
        for (card_scope, track), records in sorted(clusters.items()):
            proposals.append(
                {
                    "track": track,
                    "scope": card_scope,
                    "taste_ids": [r["id"] for r in records],
                    "records": records,
                    "reason": (
                        f"{len(records)} active {track} taste record(s) in "
                        f"{card_scope} scope share a track and scope"
                    ),
                }
            )
        return proposals

    # ------------------------------------------------------------------
    # card creation & review
    # ------------------------------------------------------------------
    def create_card(
        self,
        *,
        title: str,
        attitude: str,
        track: str,
        scope: str,
        taste_ids: Sequence[str],
        representative_evidence: Sequence[str] | None = None,
        tensions: str | None = None,
        influence: str | None = None,
        image: dict[str, Any] | None = None,
        actor_id: str = "clusterer",
        status: str = "candidate",
    ) -> dict[str, Any]:
        """Create a card candidate from a cluster.  It starts unconfirmed."""

        if track not in VALID_CARD_TRACKS:
            raise ValueError(f"invalid card track: {track}")
        if scope not in {"user", "project"}:
            raise ValueError(f"invalid card scope: {scope}")
        if status not in VALID_CARD_STATUSES:
            raise ValueError(f"invalid card status: {status}")
        if not title.strip():
            raise ValueError("card title cannot be empty")
        if not attitude.strip():
            raise ValueError("card attitude cannot be empty")
        if not taste_ids:
            raise ValueError("a card must group at least one taste record")
        # Every grouped taste id must resolve -- a card cannot point at
        # evidence the ledger does not contain.
        for taste_id in taste_ids:
            self.taste.get(taste_id)
        if image is not None:
            self._validate_image(image)
        card_id = _id("crd")
        now = _now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO taste_cards "
                "(id, title, attitude, track, scope, taste_ids_json, "
                "representative_evidence_json, tensions, influence, image_json, "
                "status, valid_from, valid_to, last_confirmed_at, supersedes_id, "
                "origin, actor_id, recorded_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?)",
                (
                    card_id,
                    title,
                    attitude,
                    track,
                    scope,
                    _json(list(dict.fromkeys(taste_ids))),
                    _json(list(representative_evidence or [])),
                    tensions,
                    influence,
                    _json(image) if image is not None else None,
                    status,
                    now,
                    "cluster",
                    actor_id,
                    now,
                ),
            )
            self.store.record_event(
                connection,
                "system",
                "taste.card.generated",
                {
                    "card_id": card_id,
                    "title": title,
                    "track": track,
                    "scope": scope,
                    "status": status,
                    "taste_ids": list(dict.fromkeys(taste_ids)),
                    "has_image": image is not None,
                },
                occurred_at=now,
            )
        return self.get(card_id)

    def review(
        self,
        card_id: str,
        action: str,
        reviewer_id: str,
        *,
        edited: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Review a card.  Confirmation and lifecycle are explicit, never silent.

        ``split`` is the only action that returns a list (the new cards); every
        other action returns the single new head card.
        """

        if action not in VALID_CARD_ACTIONS:
            raise ValueError(f"invalid card action: {action}")
        if not reviewer_id.strip():
            raise ValueError("reviewer_id cannot be empty")
        row = self._get_row(card_id)
        if self._has_child(card_id):
            raise ValueError(f"card {card_id} has been superseded; review the current head")
        allowed = _CARD_TRANSITIONS.get(row["status"], set())
        if action not in allowed:
            raise ValueError(f"cannot '{action}' a card in status '{row['status']}'")

        if action == "split":
            return self._split(row, reviewer_id, edited)

        new_status = {
            "accept": "active",
            "edit": row["status"],
            "pause": "paused",
            "resume": "active",
            "retire": "retired",
        }[action]
        # accept/edit/resume count as confirmation and bump last_confirmed_at.
        confirm = action in {"accept", "edit", "resume"}

        fields = {
            "title": row["title"],
            "attitude": row["attitude"],
            "tensions": row["tensions"],
            "influence": row["influence"],
            "image": _decode(row["image_json"]) if row["image_json"] else None,
        }
        if action == "edit":
            if edited is None:
                raise ValueError("edited content is required for edit")
            for key in ("title", "attitude", "tensions", "influence", "image"):
                if key in edited:
                    fields[key] = edited[key]
            if fields["image"] is not None:
                self._validate_image(fields["image"])

        new_id = _id("crd")
        now = _now()
        last_confirmed = now if confirm else row["last_confirmed_at"]
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO taste_cards "
                "(id, title, attitude, track, scope, taste_ids_json, "
                "representative_evidence_json, tensions, influence, image_json, "
                "status, valid_from, valid_to, last_confirmed_at, supersedes_id, "
                "origin, actor_id, recorded_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id,
                    fields["title"],
                    fields["attitude"],
                    row["track"],
                    row["scope"],
                    row["taste_ids_json"],
                    row["representative_evidence_json"],
                    fields["tensions"],
                    fields["influence"],
                    _json(fields["image"]) if fields["image"] is not None else None,
                    new_status,
                    row["valid_from"],
                    row["valid_to"],
                    last_confirmed,
                    card_id,
                    row["origin"],
                    reviewer_id,
                    now,
                ),
            )
            self.store.record_event(
                connection,
                "system",
                "taste.reviewed",
                {
                    "card_id": card_id,
                    "new_card_id": new_id,
                    "action": action,
                    "reviewer_id": reviewer_id,
                    "new_status": new_status,
                },
                occurred_at=now,
            )
        return self.get(new_id)

    def _split(
        self, row: Any, reviewer_id: str, edited: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Split a card into multiple cards, retiring the original.

        ``edited`` must be ``{"cards": [{title, attitude, taste_ids}, ...]}``.
        The original is retired; the new cards start as candidates referencing
        disjoint subsets of the original's taste records.
        """

        if not edited or "cards" not in edited or not isinstance(edited["cards"], list):
            raise ValueError("split requires edited={'cards': [...]}")
        parts = edited["cards"]
        if len(parts) < 2:
            raise ValueError("split requires at least two resulting cards")
        original_taste_ids = set(_decode(row["taste_ids_json"]))
        assigned: set[str] = set()
        new_cards = []
        for part in parts:
            part_ids = list(dict.fromkeys(part.get("taste_ids", [])))
            if not part_ids:
                raise ValueError("each split part must keep at least one taste record")
            unknown = set(part_ids) - original_taste_ids
            if unknown:
                raise ValueError(f"split part references taste records not in the card: {sorted(unknown)}")
            overlap = assigned & set(part_ids)
            if overlap:
                raise ValueError(f"split parts must be disjoint; duplicated: {sorted(overlap)}")
            assigned |= set(part_ids)
        if assigned != original_taste_ids:
            raise ValueError("split must account for every taste record in the card")

        # Retire the original, then create the new candidate cards.
        self.review(row["id"], "retire", reviewer_id)
        for part in parts:
            new_cards.append(
                self.create_card(
                    title=part["title"],
                    attitude=part["attitude"],
                    track=row["track"],
                    scope=row["scope"],
                    taste_ids=part["taste_ids"],
                    tensions=part.get("tensions", row["tensions"]),
                    influence=part.get("influence", row["influence"]),
                    actor_id=reviewer_id,
                    status="candidate",
                )
            )
        return new_cards

    # ------------------------------------------------------------------
    # queries & review queue
    # ------------------------------------------------------------------
    def get(self, card_id: str) -> dict[str, Any]:
        row = self._get_row(card_id)
        return self._project(row)

    def by_status(self, status: str, scope: str | None = None) -> list[dict[str, Any]]:
        if status not in VALID_CARD_STATUSES:
            raise ValueError(f"invalid card status: {status}")
        return [
            self._project(row)
            for row in self._head_rows()
            if row["status"] == status and (scope is None or row["scope"] == scope)
        ]

    def review_queue(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """A deterministic review queue, ordered by auditable priority.

        Priority: (1) candidate cards awaiting a first decision, then
        (2) active cards ordered by longest time since last confirmation.
        No randomness, no rarity -- the queue exists to prompt judgement.
        """

        heads = self._head_rows()
        candidates = [r for r in heads if r["status"] == "candidate"]
        active = sorted(
            (r for r in heads if r["status"] == "active"),
            key=lambda r: (r["last_confirmed_at"], r["id"]),
        )
        ordered = candidates + active
        return [self._project(row) for row in ordered[:limit]]

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _head_rows(self) -> list[Any]:
        rows = self.store.query("SELECT * FROM taste_cards ORDER BY recorded_at, rowid")
        superseded = {row["supersedes_id"] for row in rows if row["supersedes_id"]}
        return [row for row in rows if row["id"] not in superseded]

    def _get_row(self, card_id: str) -> Any:
        row = self.store.query_one("SELECT * FROM taste_cards WHERE id = ?", (card_id,))
        if row is None:
            raise KeyError(f"unknown taste card: {card_id}")
        return row

    def _has_child(self, card_id: str) -> bool:
        return (
            self.store.query_one(
                "SELECT 1 FROM taste_cards WHERE supersedes_id = ? LIMIT 1", (card_id,)
            )
            is not None
        )

    def _project(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "attitude": row["attitude"],
            "track": row["track"],
            "scope": row["scope"],
            "taste_ids": _decode(row["taste_ids_json"]),
            "representative_evidence": _decode(row["representative_evidence_json"]),
            "tensions": row["tensions"],
            "influence": row["influence"],
            "image": _decode(row["image_json"]) if row["image_json"] else None,
            "status": row["status"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "last_confirmed_at": row["last_confirmed_at"],
            "supersedes_id": row["supersedes_id"],
            "origin": row["origin"],
            "actor_id": row["actor_id"],
            "recorded_at": row["recorded_at"],
        }

    @staticmethod
    def _validate_image(image: dict[str, Any]) -> None:
        """Validate the image metadata contract (rebuildable, never evidentiary).

        An image is a visual metaphor only: it must record enough to be
        regenerated (model, prompt, seed, version) and is never used to infer
        taste.  This validates the contract, not the image content.
        """

        if not isinstance(image, dict):
            raise ValueError("image must be an object")
        for key in ("model", "prompt", "seed", "version"):
            if key not in image:
                raise ValueError(f"image metadata requires '{key}' for rebuildability")
