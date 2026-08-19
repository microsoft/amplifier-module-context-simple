"""The stub call sites must not re-impose the shape exemption the helper dropped.

Commit `56fecd6` made `_stub_user_message` shape-agnostic: a message whose
content is a list of blocks can be compacted, with non-text blocks preserved.
Both *callers* kept an `isinstance(content, str)` guard, so that branch of the
helper was unreachable from production -- the fix was live in the unit under
test and dead in the code path.

The guards also computed savings as `(len(content) - 70) // 4`. On block content
`len()` is a BLOCK COUNT, not a character count, so lifting the guard without
fixing the arithmetic would have produced a savings figure in the wrong unit --
the same class of defect as `ad7936a`, where per-message deltas and the baseline
disagreed.

HONEST SCOPE: these tests pin the call sites' behaviour directly. Driving the
same difference end-to-end through `get_messages_for_request()` was attempted
four times and could not be reproduced -- `stub_candidates` excludes the first
and last user message at levels 1-7, and in every fixture built, removal reached
the target before the stub stage ran at all. So this is a latent-defect fix:
the inconsistency and the unit error are real and verified here; a user-visible
symptom was not demonstrated.
"""

from __future__ import annotations

from typing import Any

import pytest

from amplifier_module_context_simple import SimpleContextManager

LONG_TEXT = (
    "the project lives at ~/Desktop/ora and the overseer app is inside it, " * 40
)


def _manager() -> SimpleContextManager:
    return SimpleContextManager(
        max_tokens=40_000,
        target_usage=0.5,
        protected_recent=0.10,
        compaction_notice_enabled=False,
    )


def _user(content: Any, *, subject: bool = False) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "user", "content": content}
    if subject:
        # Removal shifts indices, so the subject is located by marker, never by
        # position -- an earlier version of this file asserted on kept[2] and
        # was reading whichever message happened to land there.
        msg["_probe_subject"] = True
    return msg


def _blocks(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


def _find_subject(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for msg in messages:
        if msg.get("_probe_subject"):
            return msg
    return None


def _conversation(subject: dict[str, Any]) -> list[dict[str, Any]]:
    """`subject` sits mid-history so it is a stub candidate, not first or last."""
    return [
        _user("first user message"),
        {"role": "assistant", "content": "ok"},
        subject,
        {"role": "assistant", "content": "ok"},
        _user("last user message"),
    ]


@pytest.mark.parametrize(
    ("label", "make_content"),
    [
        pytest.param("string", lambda: LONG_TEXT, id="string-content"),
        pytest.param("blocks", lambda: _blocks(LONG_TEXT), id="block-content"),
    ],
)
def test_both_content_shapes_are_stubbable_from_the_call_site(
    label: str, make_content: Any
) -> None:
    """The defect: only one of these two shapes could ever be stubbed."""
    context = _manager()
    messages = _conversation(_user(make_content(), subject=True))

    kept, _removed, stubbed, _tokens = context._remove_messages_with_protection(
        messages, target_tokens=1, protected_recent=0.10, system_tokens=0
    )

    assert stubbed >= 1, f"{label} content was never offered to the stubber"
    found = _find_subject(kept)
    assert found is not None, f"{label} subject was removed entirely, not stubbed"
    assert found.get("_stubbed") is True, f"{label} subject not stubbed: {found}"


def test_a_stubbed_block_message_actually_gets_smaller() -> None:
    """Reachability is not enough -- the compaction has to save something."""
    context = _manager()
    subject = _user(_blocks(LONG_TEXT), subject=True)
    before = context._estimate_message_tokens(subject)

    kept, _removed, _stubbed, _tokens = context._remove_messages_with_protection(
        _conversation(subject), target_tokens=1, protected_recent=0.10, system_tokens=0
    )

    found = _find_subject(kept)
    assert found is not None
    after = context._estimate_message_tokens(found)
    assert after < before, f"stubbing block content saved nothing: {before} -> {after}"


def test_savings_are_measured_in_tokens_not_len() -> None:
    """`len()` on block content is a block count, not a character count.

    The old `(len(content) - 70) // 4` on a one-block message yields a large
    NEGATIVE number, which would have been subtracted from the running total --
    inflating it, and driving further compaction. This asserts the reported
    post-compaction figure agrees with a fresh estimate of what was returned.
    """
    context = _manager()
    messages = _conversation(_user(_blocks(LONG_TEXT), subject=True))

    kept, _removed, _stubbed, reported = context._remove_messages_with_protection(
        messages, target_tokens=1, protected_recent=0.10, system_tokens=0
    )

    assert reported >= 0, f"reported token count went negative: {reported}"
    fresh = context._estimate_tokens(kept)
    assert abs(reported - fresh) < max(200, fresh // 2), (
        f"reported {reported:,} disagrees with a fresh estimate {fresh:,} of the "
        f"returned view -- the savings arithmetic is in the wrong unit"
    )


def test_a_short_block_message_is_left_alone() -> None:
    """Deferring to the helper must not mean stubbing everything in sight."""
    context = _manager()
    subject = _user(_blocks("short"), subject=True)

    kept, _removed, stubbed, _tokens = context._remove_messages_with_protection(
        _conversation(subject), target_tokens=1, protected_recent=0.10, system_tokens=0
    )

    found = _find_subject(kept)
    assert found is not None
    assert found.get("_stubbed") is not True
    assert stubbed == 0, "a message with nothing worth compacting was stubbed anyway"
