"""Compaction arithmetic must be done in ONE unit.

The estimator fix (content-aware counting, so a base64 payload is not measured
as prose) landed in `_estimate_tokens` but NOT in the two hot paths that do
delta arithmetic on top of it. Those kept the old `len(str(msg)) // 4`.

The result was a second, opposite defect introduced by the fix for the first:

    baseline (content-aware, whole list) :     1,621
    per-message delta (old formula)      :   100,031   <- what the loop subtracted
    running total after one removal      :   -98,410   <- hard negative

`_remove_messages_with_protection` exits as soon as `current_tokens <=
target_tokens`, so a single image-bearing removal drove the total negative and
the loop stopped on its first candidate. Compaction silently UNDER-shot on
exactly the conversations the estimator fix was written for -- and
`final_tokens` is honestly re-measured at the end, so the reported stats looked
correct while the loop that produced them had been flying on garbage.

These tests pin the invariant rather than the incident: whatever the estimator
does, the whole-list figure and the per-message figures must be the same
quantity, because the removal and truncation loops subtract one from the other.
"""

from __future__ import annotations

from typing import Any

from amplifier_module_context_simple import SimpleContextManager


def _probe() -> SimpleContextManager:
    return SimpleContextManager.__new__(SimpleContextManager)


def _image_message(payload_chars: int = 400_000) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "here is the mockup"},
            {
                "type": "image",
                "source": {"type": "base64", "data": "A" * payload_chars},
            },
        ],
    }


def _conversation() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "lets build it"},
        _image_message(),
        {"role": "assistant", "content": "looking at the mockup"},
        {"role": "tool", "tool_call_id": "c1", "content": "R" * 5_000},
        _image_message(120_000),
        {"role": "assistant", "content": "done"},
    ]


def test_per_message_estimates_sum_to_the_whole_list_estimate() -> None:
    """The invariant both hot loops depend on.

    `_remove_messages_with_protection` seeds a running total from the whole-list
    estimate and then subtracts per-message figures; `_truncate_tool_wave` does
    the same with a before/after pair. If the two disagree by even a constant
    factor the running total is meaningless, and on image-bearing messages they
    disagreed by ~60x.
    """
    probe = _probe()
    messages = _conversation()

    whole = probe._estimate_tokens(messages)
    parts = sum(probe._estimate_message_tokens(message) for message in messages)

    assert whole == parts, (
        f"whole-list estimate {whole:,} != sum of per-message estimates {parts:,}; "
        "the removal and truncation loops subtract one from the other"
    )


def test_removing_any_message_leaves_the_running_total_sane() -> None:
    """The concrete failure: one removal drove the total hard negative.

    A negative running total satisfies `current_tokens <= target_tokens`
    immediately, so the loop stopped after its first candidate and compaction
    under-shot -- silently, because the final figure is re-measured honestly.
    """
    probe = _probe()
    messages = _conversation()
    total = probe._estimate_tokens(messages)

    for index, message in enumerate(messages):
        remaining = total - probe._estimate_message_tokens(message)
        assert remaining >= 0, (
            f"removing message {index} ({message.get('role')}) drove the running "
            f"total to {remaining:,} against a baseline of {total:,}"
        )
        # And it must equal what a fresh estimate of the shortened list says.
        rest = messages[:index] + messages[index + 1 :]
        assert remaining == probe._estimate_tokens(rest)


def test_truncating_a_tool_result_moves_the_total_by_its_own_delta() -> None:
    """The second hot path: `_truncate_tool_wave`'s before/after pair."""
    # A real instance: `_truncate_tool_result` reads configured state
    # (`truncate_chars`), unlike the pure estimator methods above.
    probe = SimpleContextManager(max_tokens=40_000, truncate_chars=100)
    message = {"role": "tool", "tool_call_id": "c1", "content": "R" * 20_000}
    messages = [{"role": "user", "content": "go"}, message]

    before_total = probe._estimate_tokens(messages)
    old_len = probe._estimate_message_tokens(message)
    truncated = probe._truncate_tool_result(message)
    new_len = probe._estimate_message_tokens(truncated)

    predicted = before_total + (new_len - old_len)
    actual = probe._estimate_tokens([messages[0], truncated])

    assert predicted == actual, (
        f"delta arithmetic predicted {predicted:,} but a fresh estimate says {actual:,}"
    )
    assert new_len < old_len, "truncation must actually reduce the estimate"
