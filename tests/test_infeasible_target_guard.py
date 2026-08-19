"""Never escalate toward a target that arithmetic says is unreachable.

System messages are never compacted. Once their share alone exceeds the
compaction target, no escalation level can reach it -- the predicate never
clears, so compaction re-decides on every request, pinned at maximum level,
deleting real conversation to chase a number that cannot come down.

Reproduced on this module before the check existed, with a 12,153-token system
prompt against a 40,000 budget (target 10,000) and **no images anywhere**:

    call  level  after_tokens  removed  view
       5      8        14,475        6     4
      13      8        14,776       18     8
      29      8        14,776       54     4      <- 54 of 58 messages ever added

`after_tokens` never moved. The view sawtoothed as history regrew and was
destroyed again. And it was silent: the existing over-budget warning is gated on
`final_tokens > budget`, while this state sits at 37% of budget.

The guard only declines to escalate while the view still fits the ACTUAL budget.
Over budget a partial reduction beats none, so escalation proceeds as before.
"""

from __future__ import annotations

import logging

import pytest
from amplifier_module_context_simple import SimpleContextManager

MODULE_LOGGER = "amplifier_module_context_simple"

# ~12,153 tokens: larger than the 10,000 target, smaller than the 40,000 budget.
BIG_SYSTEM = "S" * 48_524
BUDGET = 40_000
TARGET = 10_000


def _manager(**overrides) -> SimpleContextManager:
    config = {
        "max_tokens": BUDGET,
        "target_usage": 0.25,  # target = 10,000
        "compact_threshold": 0.5,  # compaction considered from 20,000
        "protected_recent": 0.30,
        "compaction_notice_enabled": False,
    }
    config.update(overrides)
    return SimpleContextManager(**config)


async def _grow(context: SimpleContextManager, turns: int, words: int = 600) -> None:
    for i in range(turns):
        await context.add_message({"role": "user", "content": f"turn {i} " * words})
        await context.add_message(
            {"role": "assistant", "content": f"reply {i} " * words}
        )
        await context.get_messages_for_request()


@pytest.mark.asyncio
async def test_an_unreachable_target_does_not_destroy_a_context_that_fits() -> None:
    """The defect: deleting conversation while comfortably inside the budget."""
    context = _manager()
    await context.add_message({"role": "system", "content": BIG_SYSTEM})
    await context.add_message(
        {"role": "user", "content": "PROJECT PATH IS ~/Desktop/ora"}
    )

    system_tokens = context._estimate_tokens([context.messages[0]])
    assert system_tokens > TARGET, "fixture must make the target unreachable"

    await _grow(context, turns=4)
    view = await context.get_messages_for_request()

    assert context._estimate_tokens(view) <= BUDGET, (
        "fixture must stay inside the budget"
    )
    assert not context._removed_seqs, (
        f"removed {len(context._removed_seqs)} messages while the view still fit the "
        f"budget, chasing a target that no level can reach"
    )
    assert any("PROJECT PATH IS" in str(m.get("content", "")) for m in view)


@pytest.mark.asyncio
async def test_it_says_so_once_and_names_the_knob(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Silence is the failure mode this module cannot afford; so is 235 repeats."""
    context = _manager()
    await context.add_message({"role": "system", "content": BIG_SYSTEM})

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        await _grow(context, turns=6)

    unreachable = [r for r in caplog.records if "target is unreachable" in r.message]
    assert len(unreachable) == 1, (
        f"expected exactly one warning, got {len(unreachable)} -- a warning repeated "
        f"per request trains operators to ignore it"
    )
    message = unreachable[0].message
    assert "system prompt alone" in message
    assert f"{TARGET:,}" in message
    assert "Reduce the system prompt or raise the budget" in message, (
        "the warning must name the knob that actually moves"
    )


@pytest.mark.asyncio
async def test_going_over_budget_still_escalates() -> None:
    """Declining to chase the target must not become declining to compact.

    Over the real budget a partial reduction beats none, even when the target
    stays out of reach.
    """
    context = _manager()
    await context.add_message({"role": "system", "content": BIG_SYSTEM})
    await _grow(context, turns=30)

    view = await context.get_messages_for_request()

    assert context._removed_seqs, "compaction must still act once genuinely over budget"
    assert context._estimate_tokens(view) <= BUDGET, (
        "compaction ran but did not bring the view back inside the budget"
    )


@pytest.mark.asyncio
async def test_a_reachable_target_is_unaffected() -> None:
    """The guard must be invisible whenever the target is actually achievable."""
    context = _manager(max_tokens=400_000, target_usage=0.5)  # target 200,000
    await context.add_message({"role": "system", "content": BIG_SYSTEM})

    system_tokens = context._estimate_tokens([context.messages[0]])
    assert system_tokens < 200_000, "fixture must make the target reachable"

    await _grow(context, turns=10)
    view = await context.get_messages_for_request()

    assert context._estimate_tokens(view) <= 400_000
