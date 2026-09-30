"""Card images: injectable visual metaphor, never evidence, multi-modal safe."""

from __future__ import annotations

import pytest

from noname_harness.card_images import (
    GeneratedImage,
    card_image_for,
    local_typographic_image,
)
from noname_harness.store import HarnessStore
from noname_harness.taste import TasteService
from noname_harness.taste_cards import TasteCardService


def make_store(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    store = HarnessStore(root / ".noname" / "harness.db")
    store.initialize_project(root, "image project")
    return store, root


def _card(store):
    taste = TasteService(store)
    record = taste.record_authored({"judgement": "工具要克制、信息密度高"}, scope="user")
    cards = TasteCardService(store)
    card = cards.create_card(
        title="克制", attitude="工具要克制、改动小而可回退",
        track="authored", scope="user", taste_ids=[record["id"]],
    )
    return cards, card


def test_local_renderer_is_deterministic_and_rebuildable():
    summary = {"title": "克制", "attitude": "工具克制", "track": "authored"}
    first = local_typographic_image(summary)
    second = local_typographic_image(summary)
    assert first.image_bytes == second.image_bytes  # deterministic
    # Metadata satisfies the rebuildable contract.
    for key in ("model", "prompt", "seed", "version"):
        assert key in first.metadata
    assert first.media_type == "image/svg+xml"


def test_local_renderer_is_abstract_no_faces_no_photo():
    image = local_typographic_image({"title": "t", "attitude": "a", "track": "authored"})
    assert image.metadata["abstract"] is True
    assert image.metadata["no_faces"] is True
    # Pure typography SVG, no raster photo references.
    assert b"<svg" in image.image_bytes
    assert b"face" not in image.image_bytes.lower()


def test_image_is_labelled_visual_explanation_not_evidence():
    image = local_typographic_image({"title": "t", "attitude": "a", "track": "x"})
    svg = image.image_bytes.decode("utf-8")
    assert "视觉解释" in svg
    assert "非事实" in svg


def test_card_image_uses_text_evidence_only():
    # The generator receives only the card's text (title/attitude/track), never
    # other user data -- so even a real image model works from explainable text.
    captured = {}
    def spy(summary):
        captured.update(summary)
        return local_typographic_image(summary)
    card_image_for({"title": "t", "attitude": "a", "track": "x", "secret": "must-not-leak"}, spy)
    assert "secret" not in captured
    assert set(captured) == {"title", "attitude", "track"}


def test_generate_image_persists_svg_and_versions_card(tmp_path):
    store, root = make_store(tmp_path)
    try:
        cards, card = _card(store)
        updated = cards.generate_image(card["id"], "yuchen")
        # The card is versioned with the image reference + metadata.
        assert updated["supersedes_id"] == card["id"]
        image = updated["image"]
        assert image["model"] == "local-typographic-v1"
        assert image["media_type"] == "image/svg+xml"
        assert image["note"].startswith("visual explanation")
        # The SVG file exists inside the workspace.
        svg_path = root / image["path"]
        assert svg_path.exists()
        assert "克制" in svg_path.read_text(encoding="utf-8")
        # The card passes the rebuildable image metadata contract.
        for key in ("model", "prompt", "seed", "version"):
            assert key in image
    finally:
        store.close()


def test_generate_image_requires_head(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        cards, card = _card(store)
        accepted = cards.review(card["id"], "accept", "user")
        with pytest.raises(ValueError):
            cards.generate_image(card["id"], "user")  # stale head
        # The current head works.
        cards.generate_image(accepted["id"], "user")
    finally:
        store.close()


def test_generate_image_uses_injectable_generator(tmp_path):
    store, root = make_store(tmp_path)
    try:
        cards, card = _card(store)
        def custom(summary):
            return GeneratedImage(
                image_bytes=b"<svg xmlns='http://www.w3.org/2000/svg'><text>custom</text></svg>",
                media_type="image/svg+xml",
                metadata={"model": "custom-model", "prompt": "p", "seed": 1, "version": 1},
            )
        updated = cards.generate_image(card["id"], "user", generator=custom)
        assert updated["image"]["model"] == "custom-model"
    finally:
        store.close()
