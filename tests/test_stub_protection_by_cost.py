"""Stubbing must protect by cost, not by content shape.

Companion to `test_multimodal_token_estimate.py`, covering the second half of
report section 1. `_stub_user_message` guarded on `isinstance(content, str)`, so
a message whose content was a list of blocks was returned unchanged -- it could
not be stubbed at all.

In the incident that meant the two largest messages in the context were
structurally exempt from the only mechanism that could shrink them, while small
text-only messages carrying the user's actual instructions were stubbed on all
235 passes. Protection ran by TYPE; it should run by COST.
"""

from __future__ import annotations

from typing import Any

from amplifier_module_context_simple import SimpleContextManager

LONG_TEXT = "the project lives at ~/Desktop/ora and the overseer app is inside it, " * 3


def _probe() -> SimpleContextManager:
    """Stubbing reads no constructed state."""
    return SimpleContextManager.__new__(SimpleContextManager)


def _image_block(payload: str = "A" * 2048) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "data": payload}}


def test_string_content_still_stubs() -> None:
    """The path that already worked must keep working."""
    msg = {"role": "user", "content": LONG_TEXT}

    stubbed = _probe()._stub_user_message(msg)

    assert stubbed is not msg, "must return a new dict, never mutate"
    assert stubbed["_stubbed"] is True
    assert stubbed["_original_length"] == len(LONG_TEXT)
    assert "User message compacted" in stubbed["content"]


def test_short_string_content_is_left_alone() -> None:
    msg = {"role": "user", "content": "short"}
    assert _probe()._stub_user_message(msg) is msg


def test_block_content_with_long_text_is_now_stubbable() -> None:
    """The defect: this message used to be returned unchanged."""
    msg = {
        "role": "user",
        "content": [{"type": "text", "text": LONG_TEXT}],
    }

    stubbed = _probe()._stub_user_message(msg)

    assert stubbed is not msg, (
        "block-shaped content was exempt from stubbing -- the largest messages "
        "could not be shrunk while small ones carrying instructions could"
    )
    assert stubbed["_stubbed"] is True
    assert stubbed["_original_length"] == len(LONG_TEXT)
    assert stubbed["content"][0]["type"] == "text"
    assert "User message compacted" in stubbed["content"][0]["text"]


def test_attachments_survive_stubbing() -> None:
    """Only the text is compacted; a non-text block is not worth dropping.

    Non-text blocks are counted at a flat cost by the estimator, so removing one
    buys almost nothing and loses the attachment outright.
    """
    image = _image_block()
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": LONG_TEXT},
            image,
            {"type": "text", "text": " and one more note"},
        ],
    }

    stubbed = _probe()._stub_user_message(msg)

    blocks = stubbed["content"]
    assert len(blocks) == 2, blocks
    assert blocks[0]["type"] == "text"
    assert blocks[1] == image, "the attachment must survive verbatim"
    # Both text runs are accounted for in the recorded original length.
    assert stubbed["_original_length"] == len(LONG_TEXT) + len(" and one more note")


def test_block_content_with_little_text_is_left_alone() -> None:
    """An image with a short caption has nothing worth compacting."""
    msg = {
        "role": "user",
        "content": [{"type": "text", "text": "look"}, _image_block()],
    }
    assert _probe()._stub_user_message(msg) is msg


def test_unexpected_content_shapes_pass_through() -> None:
    """Never guess at a shape we do not recognise."""
    probe = _probe()
    for content in (None, 42, {"type": "text"}):
        msg = {"role": "user", "content": content}
        assert probe._stub_user_message(msg) is msg, content
