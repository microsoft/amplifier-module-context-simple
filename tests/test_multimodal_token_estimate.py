"""Token estimation must not measure a base64 payload as if it were prose.

Regression cover for a session (`eec9ae98`) destroyed by this defect. Two
pasted screenshots were persisted as raw base64 inside the transcript. The
estimator counted them at ``len(str(msg)) // 4``, so 9.8M characters of image
data read as ~2.46M tokens against a 978,720 budget -- while the provider's own
usage accounting showed the model actually receiving 9,187-58,428 input tokens.

The consequences compounded: because an image-bearing message is structurally
protected from shrinking, the compactor could never reach its target, so its
predicate never cleared and it re-ran on 88% of all model calls -- 235 times,
pinned at maximum strategy, deleting 663 of 686 messages and permanently
stubbing the user instructions that carried the project path. The session then
could not find the project it had been working on for hours.

The arithmetic below is taken verbatim from that report so the numbers stay
falsifiable.
"""

from __future__ import annotations

from typing import Any

import pytest
from amplifier_module_context_simple import SimpleContextManager

# The two screenshots, in characters, exactly as persisted.
FIRST_IMAGE_CHARS = 7_182_876
SECOND_IMAGE_CHARS = 2_647_967

# What the old estimator produced, and the observed compaction floor it matched.
OLD_ESTIMATE_FIRST_IMAGE = FIRST_IMAGE_CHARS // 4  # 1,795,719
OBSERVED_STUCK_FLOOR = 1_797_300

# The session's real configuration.
BUDGET = 978_720
TARGET = 489_360


def _probe() -> SimpleContextManager:
    """The estimator reads only class constants, so it needs no built state."""
    return SimpleContextManager.__new__(SimpleContextManager)


def _image_message(payload_chars: int) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "here is the mockup, the project is at ~/Desktop/ora",
            },
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "A" * payload_chars,
                },
            },
        ],
        "metadata": {"source": "tui-clipboard", "attachment_count": 1},
    }


def test_base64_image_is_not_measured_by_payload_length() -> None:
    """The single defect: an image counted as prose."""
    estimate = _probe()._estimate_tokens([_image_message(FIRST_IMAGE_CHARS)])

    assert estimate < 5_000, (
        f"A single screenshot estimated at {estimate:,} tokens. The old formula "
        f"gave {OLD_ESTIMATE_FIRST_IMAGE:,}, which matched the observed compaction "
        f"floor of {OBSERVED_STUCK_FLOOR:,} that never came down."
    )


def test_both_screenshots_fit_well_inside_the_budget_they_used_to_blow() -> None:
    """Together the two images read as 2.5x the budget; they cost ~3k."""
    messages = [_image_message(FIRST_IMAGE_CHARS), _image_message(SECOND_IMAGE_CHARS)]

    estimate = _probe()._estimate_tokens(messages)

    assert estimate < TARGET, (
        f"Two screenshots estimated at {estimate:,} tokens against a compaction "
        f"target of {TARGET:,}. When this exceeded the target, the target became "
        f"arithmetically unreachable and compaction looped forever."
    )


def test_an_image_nested_in_a_tool_result_is_also_bounded() -> None:
    """A screenshot returned by a tool is the same payload in a different place."""
    message = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call-1",
                "content": [
                    {"type": "text", "text": "captured"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "data": "A" * FIRST_IMAGE_CHARS},
                    },
                ],
            }
        ],
    }

    assert _probe()._estimate_tokens([message]) < 5_000


def test_text_estimation_is_unchanged() -> None:
    """The fix must not quietly re-scale ordinary text."""
    probe = _probe()
    body = "word " * 4_000  # 20,000 chars -> ~5,000 tokens

    plain = probe._estimate_tokens([{"role": "user", "content": body}])
    blocked = probe._estimate_tokens(
        [{"role": "user", "content": [{"type": "text", "text": body}]}]
    )

    assert 4_900 <= plain <= 5_200, plain
    # Block form carries a little structural overhead but must not diverge.
    assert abs(blocked - plain) < 200, (plain, blocked)


@pytest.mark.asyncio
async def test_image_heavy_conversation_does_not_trigger_compaction() -> None:
    """End to end: the session that shredded itself now compacts zero times.

    Real content here is a few hundred tokens. Before the fix, the first
    compaction fired on the exact timestamp the first image arrived, and 235
    more followed.
    """
    context = SimpleContextManager(
        max_tokens=BUDGET,
        target_usage=0.50,
        protected_recent=0.30,
        protected_tool_results=5,
        compaction_notice_enabled=False,
    )

    await context.add_message(
        {"role": "system", "content": "You are a helpful assistant."}
    )
    await context.add_message(
        {"role": "user", "content": "lets build the ora overseer"}
    )
    await context.add_message(_image_message(FIRST_IMAGE_CHARS))
    await context.add_message(
        {"role": "assistant", "content": "Looking at the mockup now."}
    )
    await context.add_message(_image_message(SECOND_IMAGE_CHARS))
    for i in range(8):
        await context.add_message({"role": "user", "content": f"continue {i}"})
        await context.add_message({"role": "assistant", "content": f"working {i}"})

    view = await context.get_messages_for_request()

    assert context._last_compaction_stats is None, (
        "Compaction fired on a conversation whose real content is a few hundred "
        f"tokens: {context._last_compaction_stats}"
    )
    # Nothing was stubbed, so the message carrying the project path survives.
    assert any(
        "~/Desktop/ora" in str(message.get("content", "")) for message in view
    ), "The user message carrying the project path did not survive."
