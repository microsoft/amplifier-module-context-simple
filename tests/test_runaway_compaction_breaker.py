"""Stop compacting when compaction is demonstrably not working.

The incident ran **235 compactions across 266 model calls** -- 88% of every
request preceded by a full compaction, all pinned at maximum strategy, every one
finishing at roughly 1.84x the budget. Nothing anywhere counted the repetition,
and the only signal was an INFO line that fired 235 times and nobody saw.

Defining "not working" took three attempts, and each wrong definition was caught
by `test_going_over_budget_still_escalates` rather than by reasoning:

1. **"escalated N times in a row"** punishes a session that is genuinely over
   budget and *must* compact on every call. Under it the view grew to 61,190
   tokens against a 40,000 budget -- the breaker turned "destroying
   conversation" into "guaranteed provider rejection", which is worse.
2. **"did not reduce versus the previous pass"** punishes compaction that is
   correctly holding the line while new turns arrive. A result that plateaus
   just *under* budget is compaction working, not failing.
3. **"finished still over the real budget"** -- the definition here. The view
   being returned will not fit, so the pass did not accomplish the one thing it
   exists to do.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from amplifier_module_context_simple import SimpleContextManager

MODULE_LOGGER = "amplifier_module_context_simple"
BUDGET = 40_000
# Larger than the whole budget: no amount of conversation compaction can make a
# view containing it fit, so every pass lands over budget.
UNCOMPACTABLE_SYSTEM = "S" * 200_000


def _manager(**overrides: Any) -> SimpleContextManager:
    config: dict[str, Any] = {
        "max_tokens": BUDGET,
        "target_usage": 0.5,
        "compact_threshold": 0.5,
        "protected_recent": 0.30,
        "compaction_notice_enabled": False,
    }
    config.update(overrides)
    return SimpleContextManager(**config)


async def _grow(context: SimpleContextManager, turns: int, words: int = 300) -> None:
    for i in range(turns):
        await context.add_message({"role": "user", "content": f"turn {i} " * words})
        await context.add_message(
            {"role": "assistant", "content": f"reply {i} " * words}
        )
        await context.get_messages_for_request()


@pytest.mark.asyncio
async def test_the_breaker_trips_when_every_pass_lands_over_budget() -> None:
    """The incident's shape: compaction that never once produces a usable view."""
    context = _manager()
    await context.add_message({"role": "system", "content": UNCOMPACTABLE_SYSTEM})

    await _grow(context, turns=20)

    assert context._escalation_breaker_reported is True, (
        "compaction finished over budget on every pass and nothing ever stopped it"
    )


@pytest.mark.asyncio
async def test_the_breaker_says_so_once(caplog: pytest.LogCaptureFixture) -> None:
    """235 identical INFO lines is why nobody saw the incident.

    One ERROR that names the knobs is the whole point; repeating it per request
    would recreate the noise it replaces.
    """
    context = _manager()
    await context.add_message({"role": "system", "content": UNCOMPACTABLE_SYSTEM})

    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        await _grow(context, turns=20)

    tripped = [r for r in caplog.records if "times in a row" in r.message]
    assert len(tripped) == 1, f"expected exactly one ERROR, got {len(tripped)}"
    message = tripped[0].message
    assert "system prompt size" in message
    assert "token budget" in message


@pytest.mark.asyncio
async def test_a_session_that_compacts_successfully_never_trips() -> None:
    """Holding the line under budget is compaction working, not failing.

    This is the case definition (2) got wrong: the result plateaus because new
    turns keep arriving, not because the compactor is stuck.
    """
    context = _manager(max_tokens=200_000)
    await context.add_message({"role": "system", "content": "You are helpful."})

    await _grow(context, turns=40)

    assert context._escalation_breaker_reported is False
    assert context._ineffective_escalations == 0


@pytest.mark.asyncio
async def test_one_successful_pass_re_arms_the_breaker() -> None:
    """The pathology is a consecutive run, so recovery must reset the count."""
    context = _manager()
    await context.add_message({"role": "system", "content": "You are helpful."})
    await _grow(context, turns=10)

    context._ineffective_escalations = 5  # pretend a rough patch
    await context.add_message({"role": "user", "content": "small"})
    await context.get_messages_for_request()

    assert context._ineffective_escalations == 0, (
        "a pass that produced a view inside the budget must clear the count"
    )


@pytest.mark.asyncio
async def test_the_count_is_reported_for_observability() -> None:
    """A breaker nobody can see the approach of is a breaker nobody trusts."""
    context = _manager()
    await context.add_message({"role": "system", "content": UNCOMPACTABLE_SYSTEM})
    await _grow(context, turns=6)

    stats = context._last_compaction_stats or {}
    assert "ineffective_escalations" in stats
    assert stats["ineffective_escalations"] > 0


@pytest.mark.asyncio
async def test_tripping_freezes_rather_than_re_deriving() -> None:
    """Prefix safety: the breaker must return the decisions already made.

    Freezing re-applies the existing sticky set unchanged, which is strictly
    more stable for prompt caching than re-deriving the whole decision.
    """
    context = _manager()
    await context.add_message({"role": "system", "content": UNCOMPACTABLE_SYSTEM})
    await _grow(context, turns=20)
    assert context._escalation_breaker_reported is True

    removed_at_trip = set(context._removed_seqs)
    level_at_trip = context._sticky_level

    await context.add_message({"role": "user", "content": "another turn " * 300})
    await context.get_messages_for_request()

    # Compare KEY SETS: `_removed_seqs` maps seq -> why it was removed, so a
    # bare `== removed_at_trip` compares a dict against a set and is always
    # False. The invariant under test is "no new removals", not "the reason
    # strings are byte-identical".
    assert set(context._removed_seqs) == removed_at_trip, "kept deleting after the trip"
    assert context._sticky_level == level_at_trip, "kept escalating after the trip"
