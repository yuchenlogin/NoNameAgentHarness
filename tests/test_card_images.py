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


# --- 对抗性审查发现的回归 ---

def test_upper_applied_before_escape_no_entity_corruption():
    # A track containing '&' must not corrupt the escaped entity on .upper().
    image = local_typographic_image({"title": "t", "attitude": "a", "track": "a&b"})
    svg = image.image_bytes.decode("utf-8")
    import xml.etree.ElementTree as ET
    ET.fromstring(svg)  # must be well-formed XML
    assert "&AMP;" not in svg


def test_control_characters_produce_wellformed_xml():
    image = local_typographic_image({"title": "bad\x00title\x07", "attitude": "a", "track": "x"})
    svg = image.image_bytes.decode("utf-8")
    import xml.etree.ElementTree as ET
    ET.fromstring(svg)  # control chars stripped, well-formed
    assert "\x00" not in svg


def test_svg_uses_fill_opacity_not_rgba():
    image = local_typographic_image({"title": "t", "attitude": "a", "track": "x"})
    svg = image.image_bytes.decode("utf-8")
    assert "rgba(" not in svg
    assert 'fill-opacity="0.72"' in svg


def test_seed_includes_track_for_full_reproducibility():
    base = {"title": "t", "attitude": "a"}
    first = local_typographic_image({**base, "track": "authored"})
    second = local_typographic_image({**base, "track": "adopted"})
    # Different tracks -> different seeds (and different prompts).
    assert first.metadata["seed"] != second.metadata["seed"]


def test_generate_image_is_atomic_no_orphan_on_review_failure(tmp_path):
    store, root = make_store(tmp_path)
    try:
        cards, card = _card(store)
        retired = cards.review(card["id"], "retire", "user")
        # generate on a retired card fails at review; the ledger stays consistent
        # and no card head is left pointing at a stale file.
        with pytest.raises(ValueError):
            cards.generate_image(retired["id"], "user")
        # The image event may exist (it is evidence), but no card version was written.
        heads = cards.by_status("retired")
        assert all(h["image"] is None for h in heads)
    finally:
        store.close()


def test_abstract_no_faces_come_from_generator_not_stamped(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        cards, card = _card(store)
        def photorealistic(summary):
            return GeneratedImage(
                image_bytes=b"\x89PNG fake",
                media_type="image/png",
                metadata={"model": "dalle", "prompt": "photorealistic face", "seed": 1, "version": 1},
            )
        updated = cards.generate_image(card["id"], "user", generator=photorealistic)
        # The service must NOT stamp no_faces=True on a photorealistic generator.
        assert updated["image"]["no_faces"] is False
        assert updated["image"]["abstract"] is False
    finally:
        store.close()


def test_png_generator_gets_png_extension_and_bytes(tmp_path):
    store, root = make_store(tmp_path)
    try:
        cards, card = _card(store)
        png_bytes = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
        def png_gen(summary):
            return GeneratedImage(
                image_bytes=png_bytes,
                media_type="image/png",
                metadata={"model": "png-model", "prompt": "p", "seed": 1, "version": 1},
            )
        updated = cards.generate_image(card["id"], "user", generator=png_gen)
        path = updated["image"]["path"]
        assert path.endswith(".png")
        written = root / path
        assert written.read_bytes() == png_bytes
        # The image is also recorded as an evidence event (atomic, traceable).
        assert updated["image"]["event_id"]
    finally:
        store.close()


def test_image_recorded_as_traceable_evidence_event(tmp_path):
    store, _ = make_store(tmp_path)
    try:
        cards, card = _card(store)
        updated = cards.generate_image(card["id"], "user")
        event = store.get_event(updated["image"]["event_id"])
        assert event.event_type == "card.image.generated"
        evidence = store.evidence_for_event(event.id)
        assert evidence, "image bytes must be stored as evidence"
        assert store.verify_integrity()["ok"] is True
    finally:
        store.close()
