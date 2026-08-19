"""Never delete everything eligible to chase a target that cannot be reached.

`_remove_messages_with_protection` stops when `current_tokens <= target_tokens`.
When the target is unreachable that condition never becomes true, so the loop
runs to exhaustion and removes every eligible candidate -- permanently, because
`_removed_seqs` is re-applied on every later rebuild -- for no gain at all.

The system-message floor is guarded before the level ladder, but it is not the
only un-compactable content: the last user message and the last
`protected_tool_results` tool results are equally immune. One `read_file` on a
large file puts an enormous tool result inside that protected window and arms
this, with a 16-token system prompt and no images anywhere.

Measured on this module before the clamp:

    call  hist  view   tokens  removed
       1    64     6   35,273       58
       2    66     5      229       61      <- 229 tokens against a 40,000 budget

and after:

       1    64    62   39,777        2
       2    66    62   39,745        4

The condition that causes it is TRANSIENT -- the blob leaves the protected tool
window within a few turns and becomes truncatable -- but the removals are not.
"""

from __future__ import annotations

from typing import Any

import pytest

from amplifier_module_context_simple import SimpleContextManager

BUDGET = 40_000
BIG_TOOL_RESULT = "X" * 140_000


def _manager(**overrides: Any) -> SimpleContextManager:
    config: dict[str, Any] = {
        "max_tokens": BUDGET,
        "target_usage": 0.5,  # target 20,000
        "compact_threshold": 0.5,
        "protected_recent": 0.30,
        "protected_tool_results": 5,
        "compaction_notice_enabled": False,
    }
    config.update(overrides)
    return SimpleContextManager(**config)


async def _ordinary_session_then_one_big_read(context: SimpleContextManager) -> None:
    """A small system prompt, 30 turns of chat, one large tool result.

    Deliberately mundane: nothing here is an image, and the system prompt is far
    below the target, so the system-floor guard cannot fire.
    """
    await context.add_message({"role": "system", "content": "You are helpful."})
    await context.add_message(
        {"role": "user", "content": "PROJECT PATH IS ~/Desktop/ora"}
    )
    for i in range(30):
        await context.add_message({"role": "user", "content": f"turn {i} " * 30})
        await context.add_message({"role": "assistant", "content": f"reply {i} " * 30})
    await context.add_message(
        {
            "role": "assistant",
            "content": "reading",
            "tool_calls": [{"id": "t1", "name": "read_file"}],
        }
    )
    await context.add_message(
        {"role": "tool", "tool_call_id": "t1", "content": BIG_TOOL_RESULT}
    )


@pytest.mark.asyncio
async def test_one_large_tool_result_does_not_delete_the_conversation() -> None:
    """The defect: a routine `read_file` destroyed 58 of 64 messages on call one."""
    context = _manager()
    await _ordinary_session_then_one_big_read(context)

    view = await context.get_messages_for_request()

    assert len(context._removed_seqs) < 20, (
        f"removed {len(context._removed_seqs)} messages on the first call while "
        f"chasing an unreachable target"
    )
    assert len(view) > 40, f"view collapsed to {len(view)} messages"


@pytest.mark.asyncio
async def test_the_view_never_collapses_far_below_the_budget() -> None:
    """A view of 229 tokens against a 40,000 budget is not compaction, it is loss.

    Aiming at an unreachable target made the loop remove everything it was
    allowed to; the result undershot the budget by two orders of magnitude.
    """
    context = _manager()
    await _ordinary_session_then_one_big_read(context)

    for _ in range(3):
        view = await context.get_messages_for_request()
        tokens = context._estimate_tokens(view)
        assert tokens > BUDGET // 10, (
            f"view fell to {tokens:,} tokens against a {BUDGET:,} budget -- "
            f"far more was deleted than the budget ever required"
        )
        await context.add_message({"role": "user", "content": "next " * 30})
        await context.add_message({"role": "assistant", "content": "ok " * 30})


@pytest.mark.asyncio
async def test_removals_stay_bounded_as_the_session_continues() -> None:
    """The transient condition must not keep ratcheting `_removed_seqs`."""
    context = _manager()
    await _ordinary_session_then_one_big_read(context)

    for _ in range(10):
        await context.get_messages_for_request()
        await context.add_message({"role": "user", "content": "next " * 30})
        await context.add_message({"role": "assistant", "content": "ok " * 30})

    assert len(context._removed_seqs) < 45, (
        f"{len(context._removed_seqs)} messages permanently removed; the "
        f"condition that caused it lasts only a few turns"
    )


@pytest.mark.asyncio
async def test_a_reachable_target_is_still_pursued() -> None:
    """The clamp must not become an excuse to stop compacting.

    When the target IS achievable the loop must still drive to it, or the fix
    for over-removal becomes a cause of under-removal.
    """
    context = _manager(max_tokens=200_000, target_usage=0.5)
    await context.add_message({"role": "system", "content": "You are helpful."})
    for i in range(120):
        await context.add_message({"role": "user", "content": f"turn {i} " * 300})
        await context.add_message({"role": "assistant", "content": f"reply {i} " * 300})

    view = await context.get_messages_for_request()

    assert context._estimate_tokens(view) <= 200_000, "view must fit the budget"
    assert context._removed_seqs, "a reachable target must still be pursued"


@pytest.mark.asyncio
async def test_the_clamp_is_inert_without_a_budget() -> None:
    """`budget=0` (the default) must behave exactly as before.

    The parameter is threaded from the three call sites; a caller that does not
    supply it must not silently change behaviour.
    """
    context = _manager()
    await _ordinary_session_then_one_big_read(context)
    messages = [dict(m) for m in context.messages]

    kept, removed, _stubbed, _tokens = context._remove_messages_with_protection(
        messages, target_tokens=20_000, protected_recent=0.30, system_tokens=16
    )

    assert removed > 0, "without a budget the old exhaustive behaviour stands"
    assert len(kept) < len(messages)
