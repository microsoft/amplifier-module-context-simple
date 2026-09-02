"""Hybrid token meter tests (`token_meter: "hybrid"`).

The hybrid meter anchors on the provider's OWN reported total from the last
`llm:response` and applies the len(str)//4 heuristic ONLY to items appended
since that anchor (openai/codex's shape), then carries the PROVENANCE of the
resulting number -- `kind` in {'usage','estimated','none'} -- on every count
(deepseek-harness's shape), plus deepseek's conservatism and refuse-to-guess
guards.

Coverage:
  - anchor + un-billed tail is the hybrid number, and the split is by `_seq`
  - CONSERVATISM GUARD: an anchor below the heuristic price of the content it
    billed is rejected, and the count is honestly marked kind='estimated'
  - G-METER-PROVENANCE: an irreversible action (compaction trigger fire) is
    REFUSED on kind='estimated', and taken on kind='usage'
  - the one recorded escape (no anchor has ever arrived AND the count has hit
    the hard ceiling) fires but is counted separately as an override
  - REFUSE TO GUESS: cache aggregates are undefined unless EVERY usage event
    reported them
  - 100% of counts carry a `kind`, in all three modes
  - all three meters are computed simultaneously per request in every mode
    (the G-METER-DELTA measurement surface) and emitted as
    `context:token_meter`
  - set_messages() drops a stale anchor split (seqs are restamped from 0)
  - "hybrid" is accepted by mount(); unknown values still degrade to
    "estimate"
"""

import pytest
from amplifier_module_context_simple import (
    METER_KIND_ESTIMATED,
    METER_KIND_NONE,
    METER_KIND_USAGE,
    TOKEN_METER_HYBRID,
    SimpleContextManager,
    mount,
)

BIG = "lorem ipsum dolor sit amet consectetur " * 20


class _RecordingHooks:
    """Minimal hooks stand-in that records register() and emit() calls."""

    def __init__(self):
        self.registered: list[dict] = []
        self.emitted: list[tuple[str, dict]] = []

    def register(self, event, handler, priority=0, name=None):
        entry = {"event": event, "handler": handler, "priority": priority, "name": name}
        self.registered.append(entry)

        def unregister():
            self.registered.remove(entry)

        return unregister

    async def emit(self, event, data=None):
        self.emitted.append((event, data))
        return None


class _FakeCoordinator:
    def __init__(self, hooks=None):
        self.hooks = hooks
        self.mounted: dict[str, object] = {}

    async def mount(self, kind: str, instance: object) -> None:
        self.mounted[kind] = instance


async def _anchor(context, total: int) -> None:
    """Fire a synthetic llm:response carrying `total` as the provider's own
    reported prompt usage (input_tokens + cache_write_tokens)."""
    await context._on_llm_response(
        "llm:response", {"usage": {"input_tokens": total, "cache_write_tokens": 0}}
    )


# ---------------------------------------------------------------------------
# The hybrid number: anchor + un-billed tail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_is_provider_anchor_plus_tail_estimate_only():
    """The heuristic prices ONLY what was appended after the anchor -- the
    already-billed prefix is the provider's number, not ours."""
    context = SimpleContextManager(
        max_tokens=1_000_000, compact_threshold=0.99, token_meter=TOKEN_METER_HYBRID
    )
    for i in range(6):
        await context.add_message({"role": "user", "content": f"m{i} {BIG}"})

    # Send once so the meter knows what the heuristic said about the view that
    # is about to be billed, then anchor generously above it.
    await context.get_messages_for_request()
    sent_estimate = context._last_sent_estimate
    await _anchor(context, sent_estimate * 4)

    # Two NEW messages arrive after the anchor -- the un-billed tail.
    await context.add_message({"role": "user", "content": f"tail-a {BIG}"})
    await context.add_message({"role": "user", "content": f"tail-b {BIG}"})
    tail_estimate = context._estimate_tokens(context.messages[-2:])

    await context.get_messages_for_request()
    stats = context._last_token_meter_stats

    assert stats["hybrid_kind"] == METER_KIND_USAGE
    assert stats["anchor_tokens"] == sent_estimate * 4
    assert stats["tail_messages"] == 2
    assert stats["tail_estimated_tokens"] == tail_estimate
    assert stats["hybrid_tokens"] == sent_estimate * 4 + tail_estimate
    assert stats["used_tokens"] == stats["hybrid_tokens"]
    assert stats["source"] == "hybrid"
    # The whole point: the hybrid number is NOT the full heuristic.
    assert stats["hybrid_tokens"] != stats["estimated_tokens"]


@pytest.mark.asyncio
async def test_hybrid_with_no_tail_is_exactly_the_anchor():
    context = SimpleContextManager(
        max_tokens=1_000_000, compact_threshold=0.99, token_meter=TOKEN_METER_HYBRID
    )
    await context.add_message({"role": "user", "content": BIG})
    await context.get_messages_for_request()
    await _anchor(context, context._last_sent_estimate * 3)

    await context.get_messages_for_request()
    stats = context._last_token_meter_stats

    assert stats["tail_messages"] == 0
    assert stats["hybrid_tokens"] == stats["anchor_tokens"]
    assert stats["kind"] == METER_KIND_USAGE


@pytest.mark.asyncio
async def test_hybrid_sums_cache_write_into_the_anchor():
    """Cache-writes are part of context occupancy: a real session reported
    input_tokens=2 with cache_write_tokens=161,165 on its first call."""
    context = SimpleContextManager(
        max_tokens=1_000_000, compact_threshold=0.99, token_meter=TOKEN_METER_HYBRID
    )
    await context.add_message({"role": "user", "content": "hi"})
    await context.get_messages_for_request()
    await context._on_llm_response(
        "llm:response", {"usage": {"input_tokens": 2, "cache_write_tokens": 161_165}}
    )

    await context.get_messages_for_request()
    assert context._last_token_meter_stats["anchor_tokens"] == 161_167


# ---------------------------------------------------------------------------
# Conservatism guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conservatism_guard_rejects_anchor_below_heuristic_price():
    """If the provider total is BELOW what the heuristic priced for the very
    content it billed, the anchor is not a trustworthy floor: reject it,
    report the larger heuristic, and mark the count kind='estimated'."""
    context = SimpleContextManager(
        max_tokens=1_000_000, compact_threshold=0.99, token_meter=TOKEN_METER_HYBRID
    )
    for i in range(8):
        await context.add_message({"role": "user", "content": f"m{i} {BIG}"})
    await context.get_messages_for_request()
    sent_estimate = context._last_sent_estimate

    # Provider says the request cost HALF what the heuristic claimed.
    await _anchor(context, sent_estimate // 2)

    await context.get_messages_for_request()
    stats = context._last_token_meter_stats

    assert stats["anchor_rejected"] is True
    assert stats["hybrid_kind"] == METER_KIND_ESTIMATED
    assert stats["kind"] == METER_KIND_ESTIMATED
    assert stats["hybrid_tokens"] == stats["estimated_tokens"]
    assert stats["anchor_tokens"] == sent_estimate // 2
    assert stats["anchor_estimate"] == sent_estimate


@pytest.mark.asyncio
async def test_anchor_at_or_above_heuristic_price_is_accepted():
    context = SimpleContextManager(
        max_tokens=1_000_000, compact_threshold=0.99, token_meter=TOKEN_METER_HYBRID
    )
    await context.add_message({"role": "user", "content": BIG})
    await context.get_messages_for_request()
    await _anchor(context, context._last_sent_estimate)

    await context.get_messages_for_request()
    stats = context._last_token_meter_stats
    assert stats["anchor_rejected"] is False
    assert stats["hybrid_kind"] == METER_KIND_USAGE


# ---------------------------------------------------------------------------
# G-METER-PROVENANCE: no irreversible action on kind='estimated'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provenance_refuses_compaction_trigger_on_estimated_count():
    """THE gate. In hybrid mode the compaction trigger -- an irreversible
    action: it destroys the prompt cache and records sticky truncate/remove
    decisions -- must NOT fire on a count the provider never anchored."""
    context = SimpleContextManager(
        max_tokens=100_000,
        compact_threshold=0.5,
        target_usage=0.25,
        compaction_notice_enabled=False,
        token_meter=TOKEN_METER_HYBRID,
    )
    # Grow until the estimator is over threshold but well under the hard
    # ceiling, with NO llm:response ever seen -> kind='estimated'.
    while context._estimate_tokens(context.messages) < 60_000:
        await context.add_message({"role": "user", "content": BIG})

    await context.get_messages_for_request()
    stats = context._last_token_meter_stats

    assert stats["kind"] == METER_KIND_ESTIMATED
    assert stats["ratio"] >= 0.5, "gate would be vacuous if not over threshold"
    assert stats["ratio"] < 1.0, "must be below the hard-ceiling escape"
    assert context._last_compaction_stats is None, (
        "G-METER-PROVENANCE violated: compaction fired on kind='estimated'"
    )
    assert context._provenance_refusals == 1
    assert context._provenance_overrides == 0


@pytest.mark.asyncio
async def test_provenance_allows_compaction_trigger_on_anchored_count():
    """The other half of the gate: it must not be vacuous. With the SAME
    setup plus a real provider anchor, compaction does fire."""
    context = SimpleContextManager(
        max_tokens=100_000,
        compact_threshold=0.5,
        target_usage=0.25,
        compaction_notice_enabled=False,
        token_meter=TOKEN_METER_HYBRID,
    )
    while context._estimate_tokens(context.messages) < 60_000:
        await context.add_message({"role": "user", "content": BIG})

    await context.get_messages_for_request()  # refused (no anchor yet)
    assert context._last_compaction_stats is None
    await _anchor(context, context._last_sent_estimate + 1)  # >= heuristic: accepted

    await context.get_messages_for_request()
    stats = context._last_token_meter_stats

    assert stats["kind"] == METER_KIND_USAGE
    assert context._last_compaction_stats is not None, (
        "hybrid mode must still compact once the count is provider-anchored"
    )
    assert context._provenance_refusals == 1  # the first call only


@pytest.mark.asyncio
async def test_provenance_refuses_when_conservatism_guard_rejected_the_anchor():
    """A rejected anchor is kind='estimated' and therefore cannot authorise
    the trigger either -- the guard is not a formality."""
    context = SimpleContextManager(
        max_tokens=100_000,
        compact_threshold=0.5,
        target_usage=0.25,
        compaction_notice_enabled=False,
        token_meter=TOKEN_METER_HYBRID,
    )
    while context._estimate_tokens(context.messages) < 60_000:
        await context.add_message({"role": "user", "content": BIG})

    await context.get_messages_for_request()
    await _anchor(context, context._last_sent_estimate // 2)  # under-states: rejected

    await context.get_messages_for_request()

    assert context._last_token_meter_stats["anchor_rejected"] is True
    assert context._last_compaction_stats is None
    assert context._provenance_refusals == 2
    assert context._provenance_overrides == 0


@pytest.mark.asyncio
async def test_hard_ceiling_override_fires_but_is_recorded_separately(caplog):
    """The one deliberate escape: with NO anchor ever and the count at 100%
    of budget, refusing would guarantee a provider hard-failure. It fires --
    and is counted as an override, never as a clean anchored fire."""
    context = SimpleContextManager(
        max_tokens=20_000,
        compact_threshold=0.5,
        target_usage=0.25,
        compaction_notice_enabled=False,
        token_meter=TOKEN_METER_HYBRID,
    )
    while context._estimate_tokens(context.messages) < 21_000:
        await context.add_message({"role": "user", "content": BIG})

    await context.get_messages_for_request()

    assert context._last_token_meter_stats["kind"] == METER_KIND_ESTIMATED
    assert context._last_token_meter_stats["ratio"] >= 1.0
    assert context._last_compaction_stats is not None
    assert context._provenance_overrides == 1
    assert context._provenance_refusals == 0


@pytest.mark.asyncio
async def test_default_mode_never_refuses_anything():
    """G-METER-PROVENANCE applies to hybrid mode only: the default mode's
    trigger is untouched, refusal counters stay at zero."""
    context = SimpleContextManager(
        max_tokens=100_000,
        compact_threshold=0.5,
        target_usage=0.25,
        compaction_notice_enabled=False,
    )
    while context._estimate_tokens(context.messages) < 60_000:
        await context.add_message({"role": "user", "content": BIG})

    await context.get_messages_for_request()

    assert context._last_compaction_stats is not None
    assert context._provenance_refusals == 0
    assert context._provenance_overrides == 0


# ---------------------------------------------------------------------------
# Refuse to guess: optional cache aggregates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_aggregates_undefined_when_any_event_omitted_them():
    context = SimpleContextManager(token_meter=TOKEN_METER_HYBRID)
    await context._on_llm_response(
        "llm:response",
        {"usage": {"input_tokens": 10, "cache_read_tokens": 5, "cache_write_tokens": 1}},
    )
    assert context._cache_aggregates() is not None

    # One event without the optional fields makes the aggregate undefined --
    # a partial sum that silently under-reports is worse than no number.
    await context._on_llm_response("llm:response", {"usage": {"input_tokens": 10}})
    assert context._cache_aggregates() is None


@pytest.mark.asyncio
async def test_cache_aggregates_sum_when_every_event_reported_them():
    context = SimpleContextManager(token_meter=TOKEN_METER_HYBRID)
    for _ in range(3):
        await context._on_llm_response(
            "llm:response",
            {
                "usage": {
                    "input_tokens": 10,
                    "cache_read_tokens": 100,
                    "cache_write_tokens": 7,
                }
            },
        )
    assert context._cache_aggregates() == {
        "events": 3,
        "cache_read_tokens": 300,
        "cache_write_tokens": 21,
    }


@pytest.mark.asyncio
async def test_cache_aggregates_undefined_before_any_event():
    assert SimpleContextManager()._cache_aggregates() is None


# ---------------------------------------------------------------------------
# Provenance coverage and the three-meter measurement surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["estimate", "actual", "hybrid"])
async def test_every_count_carries_a_kind(mode):
    context = SimpleContextManager(
        max_tokens=1_000_000, compact_threshold=0.99, token_meter=mode
    )
    await context.get_messages_for_request()  # empty context
    assert context._last_token_meter_stats["kind"] == METER_KIND_NONE

    await context.add_message({"role": "user", "content": BIG})
    await context.get_messages_for_request()
    assert context._last_token_meter_stats["kind"] in (
        METER_KIND_USAGE,
        METER_KIND_ESTIMATED,
        METER_KIND_NONE,
    )

    await _anchor(context, 1_000)
    await context.get_messages_for_request()
    assert context._last_token_meter_stats["kind"] in (
        METER_KIND_USAGE,
        METER_KIND_ESTIMATED,
    )


@pytest.mark.asyncio
async def test_all_three_meters_are_computed_in_default_mode():
    """The G-METER-DELTA measurement surface: estimate, actual and hybrid are
    all present on every request even in the DEFAULT mode, so the divergence
    can be measured without changing which meter drives the trigger."""
    context = SimpleContextManager(max_tokens=1_000_000, compact_threshold=0.99)
    await context.add_message({"role": "user", "content": BIG})
    await context.get_messages_for_request()
    await _anchor(context, 4_242)
    await context.add_message({"role": "user", "content": BIG})

    await context.get_messages_for_request()
    stats = context._last_token_meter_stats

    assert stats["mode"] == "estimate"
    assert stats["source"] == "estimate"
    assert stats["used_tokens"] == stats["estimated_tokens"]  # trigger unchanged
    assert stats["measured_tokens"] == 4_242
    assert stats["hybrid_tokens"] == 4_242 + stats["tail_estimated_tokens"]
    assert stats["hybrid_kind"] == METER_KIND_USAGE


@pytest.mark.asyncio
async def test_token_meter_stats_are_emitted_per_request():
    hooks = _RecordingHooks()
    context = SimpleContextManager(
        max_tokens=1_000_000, compact_threshold=0.99, hooks=hooks
    )
    await context.add_message({"role": "user", "content": BIG})
    await context.get_messages_for_request()

    meter_events = [d for name, d in hooks.emitted if name == "context:token_meter"]
    assert len(meter_events) == 1
    for key in ("estimated_tokens", "measured_tokens", "hybrid_tokens", "kind"):
        assert key in meter_events[0]


@pytest.mark.asyncio
async def test_emit_failure_never_breaks_the_request():
    class _BrokenHooks(_RecordingHooks):
        async def emit(self, event, data=None):
            raise RuntimeError("hook bus is down")

    context = SimpleContextManager(
        max_tokens=1_000_000, compact_threshold=0.99, hooks=_BrokenHooks()
    )
    await context.add_message({"role": "user", "content": "hello"})
    view = await context.get_messages_for_request()
    assert len(view) == 1


# ---------------------------------------------------------------------------
# Anchor lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_messages_drops_a_stale_anchor_split():
    """set_messages() restamps every `_seq` from 0, so an anchor split
    recorded against the OLD numbering would silently mis-classify restored
    history as an un-billed tail. It must be dropped."""
    context = SimpleContextManager(
        max_tokens=1_000_000, compact_threshold=0.99, token_meter=TOKEN_METER_HYBRID
    )
    for i in range(5):
        await context.add_message({"role": "user", "content": f"m{i}"})
    await context.get_messages_for_request()
    await _anchor(context, 50_000)
    assert context._anchor_seq is not None

    await context.set_messages(await context.get_messages())

    assert context._anchor_seq is None
    assert context._last_measured_prompt_tokens == 50_000  # reading itself survives
    await context.get_messages_for_request()
    stats = context._last_token_meter_stats
    # No anchor split -> everything is prefix, nothing is a tail.
    assert stats["tail_messages"] == 0


@pytest.mark.asyncio
async def test_clear_resets_all_hybrid_state():
    context = SimpleContextManager(token_meter=TOKEN_METER_HYBRID)
    await context.add_message({"role": "user", "content": BIG})
    await context.get_messages_for_request()
    await _anchor(context, 12_345)

    await context.clear()

    assert context._anchor_seq is None
    assert context._anchor_estimate is None
    assert context._last_sent_estimate is None
    assert context._last_measured_prompt_tokens is None
    assert context._cache_aggregates() is None


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_accepts_hybrid():
    hooks = _RecordingHooks()
    coordinator = _FakeCoordinator(hooks)
    cleanup = await mount(coordinator, {"token_meter": "hybrid"})

    context = coordinator.mounted["context"]
    assert context.token_meter == TOKEN_METER_HYBRID
    assert [e["event"] for e in hooks.registered] == ["llm:response"]
    await cleanup()
    assert hooks.registered == []


@pytest.mark.asyncio
async def test_unknown_token_meter_still_degrades_to_estimate(caplog):
    context = SimpleContextManager(token_meter="hybird")  # typo on purpose
    assert context.token_meter == "estimate"


@pytest.mark.asyncio
async def test_non_positive_anchor_is_never_trusted():
    """A provider total of zero is not a measurement. Observed live during
    this feature's own divergence capture: a provider returned HTTP 200 with
    an all-zero usage block mid-run on a ~38k-token request. Trusting it
    would have asserted the context was empty."""
    context = SimpleContextManager(
        max_tokens=1_000_000, compact_threshold=0.99, token_meter=TOKEN_METER_HYBRID
    )
    await context.add_message({"role": "user", "content": BIG})
    # No prior send, so there is no comparand for the conservatism guard --
    # this must still be refused on its own merits.
    await _anchor(context, 0)

    await context.get_messages_for_request()
    stats = context._last_token_meter_stats

    assert stats["anchor_tokens"] == 0
    assert stats["anchor_rejected"] is True
    assert stats["hybrid_kind"] == METER_KIND_ESTIMATED
    assert stats["hybrid_tokens"] == stats["estimated_tokens"]


@pytest.mark.asyncio
async def test_zero_usage_report_after_real_traffic_is_rejected_by_the_guard():
    """The same anomaly, in the shape it was actually observed: several real
    responses, then one zero-usage response. The conservatism guard must
    reject it rather than let the count collapse to ~zero."""
    context = SimpleContextManager(
        max_tokens=1_000_000, compact_threshold=0.99, token_meter=TOKEN_METER_HYBRID
    )
    for i in range(4):
        await context.add_message({"role": "user", "content": f"m{i} {BIG}"})
    await context.get_messages_for_request()
    await _anchor(context, context._last_sent_estimate * 2)
    await context.get_messages_for_request()
    assert context._last_token_meter_stats["hybrid_kind"] == METER_KIND_USAGE

    await _anchor(context, 0)  # provider anomaly
    await context.get_messages_for_request()
    stats = context._last_token_meter_stats

    assert stats["anchor_rejected"] is True
    assert stats["hybrid_kind"] == METER_KIND_ESTIMATED
    assert stats["hybrid_tokens"] == stats["estimated_tokens"]
