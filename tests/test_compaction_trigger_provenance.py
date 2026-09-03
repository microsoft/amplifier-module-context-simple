"""Characterization tests for WHERE the compaction trigger actually comes from.

Why this file exists
--------------------
The compaction trigger is::

    trigger = compact_threshold * effective_budget

and `effective_budget` is derived **from the provider**, not from the
configured `max_tokens`.  `_calculate_budget()` only falls back to
`self.max_tokens` when no provider is passed *or* the provider exposes no
usable window information -- and the orchestrator always passes a provider
(`loop-streaming` calls `context.get_messages_for_request(provider=provider)`).

That makes `max_tokens` a **silently dead knob in production**: the shipped
foundation bundle sets `context.config.max_tokens: 300000`, and it has no
effect on when compaction fires.  An operator who lowers or raises it to move
the trigger gets no wire effect at all.

This is not hypothetical.  The cadence probe ("PROBE 4", capture root
`.amplifier/evaluation/treatment-validation/20260901-cadence/`) could not move
the trigger with config alone: its harness had to patch the module source
in-container to add `budget = min(budget, self.max_tokens)` before its arms
would compact at all.  Its own note records why -- "the loop always passes the
provider ... so the configured max_tokens is dead and compaction never fires
in a bounded run" (`scenarios/_harness/configure_cell.py`).

Before this file, **no test in the suite exercised the provider-derived budget
path at all** -- every existing test constructs the manager with `max_tokens`
and no provider, i.e. exclusively the fallback branch.  So the trap was
invisible to the suite: a change that "raised the compaction trigger" by
editing `max_tokens` would have passed the whole suite green while doing
nothing on the wire.

These tests pin current behavior so the next reader hits the trap here rather
than in a $12 measurement run.  They deliberately assert what the module
*does*, not what it arguably *should* do; changing the semantics of
`max_tokens` is a behavior change that needs its own measurement.
"""

import pytest

from amplifier_module_context_simple import SimpleContextManager

# ---------------------------------------------------------------------------
# Values quoted from named sources, not invented here.
# ---------------------------------------------------------------------------

#: The value the shipped foundation bundle puts in `context.config.max_tokens`
#: (amplifier-foundation `bundle.md`, context-simple config block).
FOUNDATION_CONFIGURED_MAX_TOKENS = 300_000

#: The S5-CRAC cadence harness forcing values.  `cad-today` ran at 45,000 and
#: `cad-fewer` raised it to 70,000 (PROBE4-VERDICT.md, arm table).  Both are
#: *scenario forcing knobs*, not production defaults.
CAD_TODAY_MAX_TOKENS = 45_000
CAD_FEWER_MAX_TOKENS = 70_000

#: `_calculate_budget`'s fixed buffer.
SAFETY_MARGIN = 4_096


class _ModelInfo:
    def __init__(self, context_window: int, max_output_tokens: int) -> None:
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens


class _ProviderWithModelInfo:
    """Provider exposing `get_model_info()` -- priority 2 in `_calculate_budget`."""

    def __init__(self, context_window: int, max_output_tokens: int) -> None:
        self._info = _ModelInfo(context_window, max_output_tokens)

    def get_model_info(self) -> _ModelInfo:
        return self._info


class _Info:
    def __init__(self, defaults: dict) -> None:
        self.defaults = defaults


class _ProviderWithDefaultsOnly:
    """Provider exposing only `get_info().defaults` -- priority 3."""

    def __init__(self, context_window: int, max_output_tokens: int) -> None:
        self._info = _Info(
            {
                "context_window": context_window,
                "max_output_tokens": max_output_tokens,
            }
        )

    def get_info(self) -> _Info:
        return self._info


class _ProviderWithNoWindowInfo:
    """Provider that knows nothing about its window -- forces priority 4."""

    def get_info(self) -> _Info:
        return _Info({})


def _expected_budget(context_window: int, max_output_tokens: int) -> int:
    """The formula in `_calculate_budget`, spelled out."""
    return context_window - int(max_output_tokens * 0.5) - SAFETY_MARGIN


# ---------------------------------------------------------------------------
# 1. The provider wins. `max_tokens` does not.
# ---------------------------------------------------------------------------


def test_provider_model_info_budget_overrides_configured_max_tokens():
    """A provider that reports a window makes `max_tokens` irrelevant."""
    context = SimpleContextManager(max_tokens=FOUNDATION_CONFIGURED_MAX_TOKENS)
    provider = _ProviderWithModelInfo(context_window=1_000_000, max_output_tokens=128_000)

    budget = context._calculate_budget(None, provider)

    assert budget == _expected_budget(1_000_000, 128_000) == 931_904
    assert budget != FOUNDATION_CONFIGURED_MAX_TOKENS, (
        "The configured max_tokens must not be what sets the budget when a "
        "provider is present -- if this ever becomes true, the dead-knob trap "
        "documented in this file has been fixed and the README section "
        "'Where the compaction trigger comes from' needs updating."
    )


def test_provider_defaults_budget_overrides_configured_max_tokens():
    """Same, via the legacy `get_info().defaults` path."""
    context = SimpleContextManager(max_tokens=FOUNDATION_CONFIGURED_MAX_TOKENS)
    provider = _ProviderWithDefaultsOnly(context_window=200_000, max_output_tokens=64_000)

    budget = context._calculate_budget(None, provider)

    assert budget == _expected_budget(200_000, 64_000) == 163_904
    assert budget != FOUNDATION_CONFIGURED_MAX_TOKENS


def test_max_tokens_is_used_only_when_the_provider_reports_no_window():
    """`max_tokens` is a *fallback*, and only a fallback."""
    context = SimpleContextManager(max_tokens=FOUNDATION_CONFIGURED_MAX_TOKENS)

    assert context._calculate_budget(None, None) == FOUNDATION_CONFIGURED_MAX_TOKENS
    assert (
        context._calculate_budget(None, _ProviderWithNoWindowInfo())
        == FOUNDATION_CONFIGURED_MAX_TOKENS
    )


def test_explicit_token_budget_still_wins_over_everything():
    """Priority 1 is unchanged: an explicit budget short-circuits the rest."""
    context = SimpleContextManager(max_tokens=FOUNDATION_CONFIGURED_MAX_TOKENS)
    provider = _ProviderWithModelInfo(context_window=1_000_000, max_output_tokens=128_000)

    assert context._calculate_budget(70_000, provider) == 70_000


# ---------------------------------------------------------------------------
# 2. The trap itself, end to end.
# ---------------------------------------------------------------------------


async def _fill(context: SimpleContextManager, pairs: int = 20, chars: int = 5_000) -> None:
    """~50,000 estimated tokens (len(str(msg))//4) of removable history."""
    for i in range(pairs):
        await context.add_message({"role": "user", "content": f"u{i} " + ("x" * chars)})
        await context.add_message(
            {"role": "assistant", "content": f"a{i} " + ("y" * chars)}
        )


@pytest.mark.asyncio
async def test_lowering_max_tokens_does_not_move_the_trigger_when_a_provider_is_present():
    """THE TRAP, demonstrated.

    Two managers configured with the two cadence-harness forcing values
    (45,000 and 70,000) see the *same* provider and the *same* history, and
    neither compacts -- because the provider's budget, not `max_tokens`, is
    what the threshold is applied to.  The config knob that the cadence probe
    "moved" has no effect through the shipped code path.
    """
    provider = _ProviderWithModelInfo(context_window=200_000, max_output_tokens=64_000)

    for configured in (CAD_TODAY_MAX_TOKENS, CAD_FEWER_MAX_TOKENS):
        context = SimpleContextManager(max_tokens=configured)
        await _fill(context)
        await context.get_messages_for_request(provider=provider)

        assert context._last_compaction_stats is None, (
            f"max_tokens={configured:,} must not move the compaction trigger "
            f"while a provider reporting a 200,000-token window is present; "
            f"the effective budget is {_expected_budget(200_000, 64_000):,}."
        )


@pytest.mark.asyncio
async def test_the_same_history_and_config_does_compact_once_the_provider_is_gone():
    """The contrast that proves the previous test is not vacuous.

    Identical config, identical history -- drop the provider and `max_tokens`
    becomes live, so compaction fires.  The only difference between "trigger
    dead" and "trigger live" is whether a provider was passed.
    """
    context = SimpleContextManager(max_tokens=CAD_TODAY_MAX_TOKENS)
    await _fill(context)
    await context.get_messages_for_request()  # no provider -> fallback budget

    stats = context._last_compaction_stats
    assert stats is not None, (
        "With no provider, max_tokens is the budget and this history is well "
        "over compact_threshold * max_tokens -- compaction must fire."
    )
    assert stats["after_tokens"] < stats["before_tokens"]


# ---------------------------------------------------------------------------
# 3. The shipped trigger fraction, pinned.
# ---------------------------------------------------------------------------


def test_default_compact_threshold_is_0_92():
    """Pin the only shipped knob that expresses "compact late" as a fraction.

    `compact_threshold` has been 0.92 for the whole life of this repository
    and was NOT the knob the cadence probe overrode (`cad-fewer` varied the
    budget, holding threshold and `target_usage` at stock).  Pinning it means
    a future silent flip has to argue with a test.
    """
    assert SimpleContextManager().compact_threshold == 0.92


def test_compact_threshold_override_moves_the_trigger():
    """The old value stays reachable via config -- in both directions."""
    assert SimpleContextManager(compact_threshold=0.8).compact_threshold == 0.8
    assert SimpleContextManager(compact_threshold=0.95).compact_threshold == 0.95

    budget = 163_904
    early = SimpleContextManager(compact_threshold=0.8)
    late = SimpleContextManager(compact_threshold=0.95)

    assert early._should_compact(int(budget * 0.85), budget) is True
    assert late._should_compact(int(budget * 0.85), budget) is False


@pytest.mark.parametrize(
    ("context_window", "max_output_tokens"),
    [(200_000, 64_000), (1_000_000, 128_000)],
)
def test_capping_the_budget_at_cad_fewer_value_would_compact_EARLIER_not_later(
    context_window: int, max_output_tokens: int
):
    """Why 70,000 must not become a shipped default.

    `cad-fewer`'s 70,000 is a forcing value that made a bounded 10-turn
    scenario compact *at all*; it is not "the late trigger".  Turned into a
    budget cap for every session it moves the trigger EARLIER than today for
    any provider whose derived budget exceeds 70,000 -- i.e. it produces MORE
    boundaries, inverting the very win (-29% requests, -14% wall) that was
    measured by having FEWER of them.
    """
    threshold = SimpleContextManager().compact_threshold

    shipped_budget = _expected_budget(context_window, max_output_tokens)
    shipped_trigger = threshold * shipped_budget
    capped_trigger = threshold * CAD_FEWER_MAX_TOKENS

    assert capped_trigger < shipped_trigger
    assert shipped_trigger / capped_trigger > 2.0, (
        "Capping the budget at cad-fewer's 70,000 would move the compaction "
        f"trigger from {shipped_trigger:,.0f} tokens to {capped_trigger:,.0f} "
        "-- earlier, not later."
    )
