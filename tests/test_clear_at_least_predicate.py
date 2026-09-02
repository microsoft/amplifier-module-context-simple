"""Adversarial tests for the worth-the-rebuild predicate
(`compact_clear_at_least`) and the summary shrink guard.

WHY THE PREDICATE EXISTS
------------------------
The compaction trigger fires on a usage THRESHOLD and never asks how many
tokens the compaction will actually free. Every compaction shrinks the
request, and a shrink is a guaranteed cold prompt-cache rebuild on the OpenAI
path (the cache matches forward from a cached entry, never backward into one:
a strict byte-identical PREFIX of a cached request measured 0 cache_read).
So a boundary that frees very little pays a full rebuild of an ~18k-token
pinned head to buy almost nothing.

`compact_clear_at_least` refuses those boundaries. It is Anthropic's
context-editing parameter of the same name, implemented client-side.

WHAT THESE TESTS ARE ADVERSARIAL ABOUT
--------------------------------------
The predicate's own failure mode is STARVATION: refuse forever, usage climbs,
and the provider eventually hard-fails with an opaque context-overflow error.
So the tests below deliberately try to:

  1. make it skip a call that decided nothing new (which must NOT count as a
     skip -- otherwise a quiet session fails loud for no reason);
  2. make it starve silently (it must raise, naming the protected set);
  3. make it leave state behind after a refusal (sticky decisions, stats, the
     tail notice, and the emitted events must all look as if the escalation
     never happened);
  4. make the DEFAULT path behave differently from before the feature existed
     (it must not -- byte-identical, no new events, predicate state never
     even populated);
  5. split a tool_use/tool_result pair across a refusal.

The shrink-guard tests do the same for a summary that would GROW the context:
a swap that pays a cache rebuild to make the request bigger is a pure loss
twice over, and nothing in this module previously checked for it.
"""

from typing import Any

import pytest
from amplifier_module_context_simple import SimpleContextManager


class _FakeHooks:
    """Records every emitted event so tests can assert on the compaction /
    compaction-skipped lifecycle without real HookRegistry internals."""

    def __init__(self):
        self.emitted: list[tuple[str, dict]] = []

    async def emit(self, event, data):
        self.emitted.append((event, data))

    def names(self) -> list[str]:
        return [name for name, _ in self.emitted]

    def payloads(self, event: str) -> list[dict]:
        return [data for name, data in self.emitted if name == event]


def _padded(i: int, role: str, size: int = 80) -> dict:
    """A message with enough bulk to move the token counter meaningfully."""
    return {"role": role, "content": f"{role} message {i} " + ("x" * size)}


def _tool_call(call_id: str, tool: str = "bash") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "tool": tool, "arguments": {}}],
    }


def _tool_result(call_id: str, content: str = "result") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _make_context(**overrides: Any) -> SimpleContextManager:
    """Same shape the sticky/notice suite uses, so these tests exercise the
    real ladder rather than a bespoke configuration."""
    config: dict[str, Any] = {
        "max_tokens": 2000,
        "compact_threshold": 0.5,
        "target_usage": 0.3,
        "protected_recent": 0.2,
        "protected_tool_results": 1,
        "truncate_chars": 40,
        "compaction_notice_enabled": True,
        "compaction_notice_min_level": 1,
    }
    config.update(overrides)
    return SimpleContextManager(**config)


async def _fill_until_compacted(
    context: SimpleContextManager, turns: int = 40
) -> None:
    for i in range(turns):
        await context.add_message(_padded(i, "user"))
        await context.add_message(_padded(i, "assistant"))


def _normalize(messages: list[dict]) -> list[dict]:
    """Drop the two fields that legitimately differ between two separately
    built contexts (wall-clock `timestamp`) or between stored history and a
    returned view (`_seq`, which `_finalize_view` strips at the module
    boundary). Everything else must match exactly.
    """
    out: list[dict] = []
    for msg in messages:
        meta = {
            k: v
            for k, v in (msg.get("metadata") or {}).items()
            if k not in ("timestamp", "_seq")
        }
        copy = {k: v for k, v in msg.items() if k != "metadata"}
        if meta or "metadata" in msg:
            copy["metadata"] = meta
        out.append(copy)
    return out


def _strip_ephemeral(messages: list[dict]) -> list[dict]:
    """Drop the trailing ephemeral compaction notice, which is deliberately
    outside the cached prefix and expected to vary."""
    out = list(messages)
    while out and (out[-1].get("metadata") or {}).get("ephemeral"):
        out.pop()
    return out


# ---------------------------------------------------------------------------
# DEFAULT OFF: today's behaviour exactly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_is_disabled_and_never_populates_predicate_state():
    """With no config, the predicate must not merely pass -- it must never be
    evaluated at all. `_clear_at_least_pending` staying None is the proof that
    the guarded code path is not entered, not just that it agreed."""
    context = _make_context()
    assert context.compact_clear_at_least is None
    await _fill_until_compacted(context)

    for _ in range(3):
        await context.get_messages_for_request()
        assert context._clear_at_least_pending is None
        assert context._clear_at_least_skips == 0

    assert context._last_compaction_stats is not None, (
        "setup must actually compact for this test to mean anything"
    )


@pytest.mark.asyncio
async def test_default_view_is_identical_to_explicit_zero_and_to_disabled():
    """`None` (default), `0`, and a negative value must all produce the exact
    same returned view AND the exact same emitted events as each other."""
    views: list[list[dict]] = []
    event_names: list[list[str]] = []

    for value in (None, 0, -5000):
        hooks = _FakeHooks()
        context = _make_context(compact_clear_at_least=value, hooks=hooks)
        await _fill_until_compacted(context)
        # Several calls, with history growing in between: exercises the sticky
        # path and the escalation path, not just one call.
        collected: list[dict] = []
        for i in range(3):
            collected = await context.get_messages_for_request()
            await context.add_message(_padded(5000 + i, "user"))
            await context.add_message(_padded(5000 + i, "assistant"))
        views.append(collected)
        event_names.append(hooks.names())

    assert _normalize(views[0]) == _normalize(views[1]) == _normalize(views[2])
    assert event_names[0] == event_names[1] == event_names[2]
    assert "context:compaction-skipped" not in event_names[0]


@pytest.mark.asyncio
async def test_disabled_predicate_emits_no_skipped_event_ever():
    hooks = _FakeHooks()
    context = _make_context(hooks=hooks)
    await _fill_until_compacted(context)
    for _ in range(5):
        await context.get_messages_for_request()
        await context.add_message(_padded(1, "user"))
    assert "context:compaction-skipped" not in hooks.names()
    assert "context:compaction" in hooks.names()


# ---------------------------------------------------------------------------
# BLOCK: a low-yield boundary is refused, and refused CLEANLY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_predicate_blocks_a_low_yield_boundary():
    """An unreachable floor must refuse the boundary: the view comes back
    uncompacted-by-this-call, and a `context:compaction-skipped` event carries
    what was freed vs. what was required."""
    hooks = _FakeHooks()
    context = _make_context(compact_clear_at_least=10_000_000, hooks=hooks)
    await _fill_until_compacted(context)

    baseline_uncompacted = [
        m for m in context.messages if m.get("role") != "system"
    ]
    view = await context.get_messages_for_request()

    assert "context:compaction" not in hooks.names(), (
        "a refused escalation must not emit a compaction event -- no "
        "compaction happened"
    )
    skipped = hooks.payloads("context:compaction-skipped")
    assert len(skipped) == 1
    assert skipped[0]["required_tokens"] == 10_000_000
    assert skipped[0]["freed_tokens"] < 10_000_000
    assert skipped[0]["consecutive_skips"] == 1
    assert skipped[0]["level_reached"] >= 1, (
        "the ladder must actually have run -- the predicate judges a real "
        "result, not a guess made before doing the work"
    )

    # The returned view is the untouched history (no truncation, no removal).
    assert _normalize(_strip_ephemeral(view)) == _normalize(baseline_uncompacted)


@pytest.mark.asyncio
async def test_refusal_rolls_back_every_sticky_decision():
    """The escalation's truncate/remove/stub decisions are the only durable
    state written before the predicate runs. A refusal must undo all three --
    otherwise the NEXT call silently replays a compaction that was refused."""
    context = _make_context(compact_clear_at_least=10_000_000)
    await _fill_until_compacted(context)

    before = (
        set(context._removed_seqs),
        set(context._truncated_seqs),
        set(context._stubbed_seqs),
        context._sticky_level,
    )
    await context.get_messages_for_request()
    after = (
        set(context._removed_seqs),
        set(context._truncated_seqs),
        set(context._stubbed_seqs),
        context._sticky_level,
    )
    assert before == after
    assert context._last_compaction_stats is None, (
        "a refused escalation must leave no stats behind -- otherwise the "
        "tail notice would announce a compaction that never happened"
    )


@pytest.mark.asyncio
async def test_refusal_leaves_no_compaction_notice():
    context = _make_context(compact_clear_at_least=10_000_000)
    await _fill_until_compacted(context)
    view = await context.get_messages_for_request()
    assert not any(
        (m.get("metadata") or {}).get("source") == "context-compaction"
        for m in view
    )


@pytest.mark.asyncio
async def test_refusal_preserves_tool_pair_integrity():
    """The refused view is raw history, so every tool_calls message must still
    be answered by its tool result. A refusal must never be able to strand a
    tool_use without its tool_result."""
    context = _make_context(compact_clear_at_least=10_000_000)
    for i in range(30):
        await context.add_message(_padded(i, "user"))
        await context.add_message(_tool_call(f"call-{i}"))
        await context.add_message(_tool_result(f"call-{i}", "r" * 200))

    view = await context.get_messages_for_request()

    called = [
        tc["id"]
        for m in view
        for tc in (m.get("tool_calls") or [])
    ]
    answered = [
        m["tool_call_id"] for m in view if m.get("role") == "tool"
    ]
    assert called, "setup must produce tool calls"
    assert sorted(called) == sorted(answered)


# ---------------------------------------------------------------------------
# ALLOW: a high-yield boundary passes, byte-identically to disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_predicate_allows_a_high_yield_boundary():
    hooks = _FakeHooks()
    context = _make_context(compact_clear_at_least=1, hooks=hooks)
    await _fill_until_compacted(context)
    await context.get_messages_for_request()

    assert "context:compaction" in hooks.names()
    assert "context:compaction-skipped" not in hooks.names()
    assert context._clear_at_least_skips == 0
    assert context._last_compaction_stats is not None


@pytest.mark.asyncio
async def test_allowed_boundary_is_identical_to_the_disabled_path():
    """A floor of 1 token is satisfiable by any real compaction, so the
    resulting view must be byte-identical to running with the predicate off.
    This is what proves the predicate ALLOWS rather than perturbs."""
    allowed = _make_context(compact_clear_at_least=1)
    disabled = _make_context()
    for ctx in (allowed, disabled):
        await _fill_until_compacted(ctx)

    for _ in range(3):
        a = await allowed.get_messages_for_request()
        d = await disabled.get_messages_for_request()
        assert _normalize(a) == _normalize(d)
        for ctx in (allowed, disabled):
            await ctx.add_message(_padded(7001, "user"))
            await ctx.add_message(_padded(7001, "assistant"))


@pytest.mark.asyncio
async def test_freed_is_marginal_not_measured_against_raw_history():
    """The predicate must judge the MARGINAL reclaim of this boundary, not the
    distance from raw history. If it used raw history, an already-compacted
    session would report a huge (stale) "freed" every call and the predicate
    would approve boundaries that free nothing.

    Here: allow the first escalation with a low floor, then raise the floor
    beyond anything a later marginal escalation can free, and assert the later
    call is judged on the marginal number.
    """
    hooks = _FakeHooks()
    context = _make_context(compact_clear_at_least=1, hooks=hooks)
    await _fill_until_compacted(context)
    await context.get_messages_for_request()
    assert "context:compaction" in hooks.names()

    stats = context._last_compaction_stats
    assert stats is not None
    already_freed = stats["before_tokens"] - stats["after_tokens"]
    assert already_freed > 0

    # Now require MORE than the whole first compaction freed. Any further
    # marginal escalation frees far less than that, so it must be refused.
    context.compact_clear_at_least = already_freed + 100_000
    for i in range(20):
        await context.add_message(_padded(8000 + i, "user"))
        await context.add_message(_padded(8000 + i, "assistant"))
    await context.get_messages_for_request()

    skipped = hooks.payloads("context:compaction-skipped")
    assert skipped, (
        "the second boundary must be judged on its own marginal reclaim, "
        "which is far below the raw-history delta"
    )
    assert skipped[-1]["freed_tokens"] < already_freed


# ---------------------------------------------------------------------------
# STARVATION: the predicate's own failure mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_op_calls_never_count_as_skips():
    """THE trap. Once sticky state alone keeps the view under threshold, the
    ladder decides nothing new and returns early. Those calls must NOT be
    judged: nothing was refused, so counting them would fail loud on a session
    that is behaving perfectly."""
    context = _make_context(compact_clear_at_least=1, compact_max_consecutive_skips=2)
    await _fill_until_compacted(context)
    await context.get_messages_for_request()  # a real, allowed escalation

    # Many further calls with no growth: sticky state alone suffices.
    for _ in range(10):
        await context.get_messages_for_request()

    assert context._clear_at_least_skips == 0


@pytest.mark.asyncio
async def test_fails_loud_after_max_consecutive_skips():
    """Silent starvation is the one outcome this must never produce. After the
    cap it raises, naming what was freed, what was required, and the protected
    set that is holding the floor -- i.e. which knob to move."""
    context = _make_context(
        compact_clear_at_least=10_000_000, compact_max_consecutive_skips=3
    )
    await _fill_until_compacted(context)

    await context.get_messages_for_request()
    assert context._clear_at_least_skips == 1
    await context.get_messages_for_request()
    assert context._clear_at_least_skips == 2

    with pytest.raises(RuntimeError) as exc:
        await context.get_messages_for_request()

    message = str(exc.value)
    assert "compact_clear_at_least" in message
    assert "10000000" in message.replace(",", "")
    assert "protected_recent" in message
    assert "protected_tool_results" in message
    assert "freed only" in message


@pytest.mark.asyncio
async def test_fail_loud_still_rolls_back_state_before_raising():
    """Even on the fatal path the escalation is undone first, so a caller that
    catches the error is not left with half-applied compaction decisions."""
    context = _make_context(
        compact_clear_at_least=10_000_000, compact_max_consecutive_skips=1
    )
    await _fill_until_compacted(context)
    before = set(context._removed_seqs), set(context._truncated_seqs)

    with pytest.raises(RuntimeError):
        await context.get_messages_for_request()

    assert (set(context._removed_seqs), set(context._truncated_seqs)) == before
    assert context._clear_at_least_pending is None, (
        "call-scoped state must never leak past a raise"
    )


@pytest.mark.asyncio
async def test_skip_streak_resets_after_an_accepted_compaction():
    context = _make_context(
        compact_clear_at_least=10_000_000, compact_max_consecutive_skips=5
    )
    await _fill_until_compacted(context)
    await context.get_messages_for_request()
    await context.get_messages_for_request()
    assert context._clear_at_least_skips == 2

    context.compact_clear_at_least = 1
    await context.get_messages_for_request()
    assert context._clear_at_least_skips == 0


@pytest.mark.asyncio
async def test_max_consecutive_skips_zero_is_clamped_to_one_not_to_infinity():
    """A cap of 0 read literally means "tolerate unlimited refusals" -- which
    is exactly the silent hang the cap exists to prevent. It must clamp UP to
    1, never be honoured as "never fail"."""
    context = _make_context(
        compact_clear_at_least=10_000_000, compact_max_consecutive_skips=0
    )
    assert context.compact_max_consecutive_skips == 1
    await _fill_until_compacted(context)
    with pytest.raises(RuntimeError):
        await context.get_messages_for_request()


@pytest.mark.asyncio
async def test_skip_streak_does_not_survive_clear_or_resume():
    """A streak accumulated against one message set says nothing about a
    different one; inheriting it would fail loud on the first refusal after a
    resume."""
    context = _make_context(
        compact_clear_at_least=10_000_000, compact_max_consecutive_skips=3
    )
    await _fill_until_compacted(context)
    await context.get_messages_for_request()
    assert context._clear_at_least_skips == 1

    await context.clear()
    assert context._clear_at_least_skips == 0

    await _fill_until_compacted(context)
    await context.get_messages_for_request()
    assert context._clear_at_least_skips == 1

    await context.set_messages([_padded(i, "user") for i in range(3)])
    assert context._clear_at_least_skips == 0
    assert context._clear_at_least_pending is None


# ---------------------------------------------------------------------------
# PREFIX / _seq STABILITY across a refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refusal_is_prefix_stable_and_append_only():
    """Two consecutive refused calls with one turn of growth in between must
    share a byte-identical prefix -- the whole point of refusing is to keep
    the cached prefix intact, so a refusal that reshuffled the view would be
    worse than compacting."""
    context = _make_context(
        compact_clear_at_least=10_000_000, compact_max_consecutive_skips=99
    )
    await _fill_until_compacted(context)

    first = _strip_ephemeral(await context.get_messages_for_request())
    await context.add_message(_padded(9001, "user"))
    await context.add_message(_padded(9001, "assistant"))
    second = _strip_ephemeral(await context.get_messages_for_request())

    assert len(second) == len(first) + 2
    assert second[: len(first)] == first, (  # exact: same context, no normalisation
        "a refusal must be strictly append-only: the shared prefix cannot move"
    )


@pytest.mark.asyncio
async def test_seq_identity_is_untouched_by_a_refusal():
    """`_seq` is compaction identity. A refusal must not renumber, drop, or
    duplicate any of them."""
    context = _make_context(
        compact_clear_at_least=10_000_000, compact_max_consecutive_skips=99
    )
    await _fill_until_compacted(context)
    before = [m["metadata"]["_seq"] for m in context.messages]

    for _ in range(3):
        await context.get_messages_for_request()

    after = [m["metadata"]["_seq"] for m in context.messages]
    assert before == after
    assert len(set(after)) == len(after)


@pytest.mark.asyncio
async def test_refusal_then_acceptance_still_compacts_correctly():
    """A refusal must not poison the next real escalation: once the floor is
    satisfiable, compaction proceeds and produces a genuinely smaller view."""
    context = _make_context(
        compact_clear_at_least=10_000_000, compact_max_consecutive_skips=99
    )
    await _fill_until_compacted(context)
    refused = await context.get_messages_for_request()

    context.compact_clear_at_least = 1
    accepted = await context.get_messages_for_request()

    assert context._estimate_tokens(accepted) < context._estimate_tokens(refused)
    assert context._last_compaction_stats is not None


# ---------------------------------------------------------------------------
# Floor resolution: absolute vs fraction, and malformed values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, budget, expected",
    [
        (None, 100_000, 0),
        (0, 100_000, 0),
        (-1, 100_000, 0),
        (-0.5, 100_000, 0),
        (20_000, 100_000, 20_000),
        (1, 100_000, 1),
        (0.25, 100_000, 25_000),
        (0.25, 0, 0),
        (0.999, 1_000, 999),
        (1.0, 100_000, 1),
        ("nonsense", 100_000, 0),
        (object(), 100_000, 0),
    ],
)
def test_floor_resolution(raw, budget, expected):
    """A float in (0, 1) is a FRACTION of the budget -- a hardcoded token floor
    silently means something different on a 200k window than a 45k one.

    1.0 is deliberately ABSOLUTE (1 token), not "100% of budget": a floor of
    one whole budget can never be met, so reading it as a fraction would turn
    a plausible-looking config into a guaranteed fail-loud.
    """
    context = _make_context(compact_clear_at_least=raw)
    assert context._clear_at_least_required(budget) == expected


@pytest.mark.asyncio
async def test_malformed_floor_disables_rather_than_crashing_a_session():
    """Consistent with how this module handles every other unrecognized config
    value: warn and fall back, never take a session down on config alone."""
    context = _make_context(compact_clear_at_least="twenty thousand")
    await _fill_until_compacted(context)
    view = await context.get_messages_for_request()
    assert view
    assert context._last_compaction_stats is not None
    assert context._clear_at_least_skips == 0


@pytest.mark.asyncio
async def test_fraction_floor_scales_with_the_budget():
    """Same fraction, two budgets: the small-budget session's boundary clears
    a floor the large-budget session's does not."""
    small = _make_context(max_tokens=2000, compact_clear_at_least=0.05)
    assert small._clear_at_least_required(2000) == 100
    assert small._clear_at_least_required(200_000) == 10_000


# ---------------------------------------------------------------------------
# Summary shrink guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shrink_guard_refuses_a_summary_larger_than_what_it_replaces():
    """A summary that GROWS the context is a pure loss twice over: it pays a
    full cache rebuild AND ends up with a bigger request. Refuse it."""
    context = SimpleContextManager(compaction_strategy="summary", protected_recent=0.9)
    for i in range(5):
        await context.add_message({"role": "user", "content": f"t{i}"})

    seqs = [m["metadata"]["_seq"] for m in context.messages[:2]]
    context._pending_summary = {"seqs": frozenset(seqs), "text": "V" * 20_000}

    non_system = [m for m in context.messages if m.get("role") != "system"]
    messages_before = list(context.messages)

    result, did_swap = await context._swap_in_pending_summary(non_system)

    assert did_swap is False
    assert result == non_system
    assert context.messages == messages_before, (
        "a refused summary must never be appended to history"
    )
    assert context._pending_summary is None
    assert context._summary_absorbed_count == 0
    assert context._removed_seqs == set(), (
        "a refused summary must not record its span as removed"
    )
    assert context._summarization_failures == 0, (
        "refusing an unprofitable summary is a graceful fallback, not a "
        "summarizer failure"
    )


@pytest.mark.asyncio
async def test_shrink_guard_allows_a_summary_that_is_genuinely_smaller():
    """The guard must not block the case the feature exists for."""
    context = SimpleContextManager(compaction_strategy="summary", protected_recent=0.9)
    for i in range(5):
        await context.add_message({"role": "user", "content": f"turn {i} " + "x" * 400})

    seqs = [m["metadata"]["_seq"] for m in context.messages[:2]]
    context._pending_summary = {"seqs": frozenset(seqs), "text": "tiny summary"}

    non_system = [m for m in context.messages if m.get("role") != "system"]
    result, did_swap = await context._swap_in_pending_summary(non_system)

    assert did_swap is True
    assert context._summary_absorbed_count == 2
    assert set(seqs) <= context._removed_seqs
    assert result[-1]["metadata"]["type"] == "context_summary"


@pytest.mark.asyncio
async def test_shrink_guard_refuses_an_equal_sized_summary():
    """"Not smaller" includes "exactly the same size": paying a cache rebuild
    to swap content for content of identical cost buys nothing."""
    context = SimpleContextManager(compaction_strategy="summary", protected_recent=0.9)
    await context.add_message({"role": "user", "content": "x"})

    seqs = [context.messages[0]["metadata"]["_seq"]]
    absorbed_tokens = context._estimate_tokens(context.messages[:1])

    # Binary-search a summary text whose full message dict prices at exactly
    # the absorbed span's estimate, so the >= boundary itself is exercised.
    text = ""
    while True:
        probe_seq = context._next_seq
        probe = context._make_summary_message(text)
        context._next_seq = probe_seq  # probe only; do not consume the id
        if context._estimate_tokens([probe]) >= absorbed_tokens:
            break
        text += "y"

    context._pending_summary = {"seqs": frozenset(seqs), "text": text}
    non_system = list(context.messages)
    _result, did_swap = await context._swap_in_pending_summary(non_system)
    assert did_swap is False


@pytest.mark.asyncio
async def test_progressive_mode_never_reaches_the_shrink_guard():
    """The guard lives on the summary path only; the default mode must be
    untouched by it."""
    context = _make_context()
    await _fill_until_compacted(context)
    await context.get_messages_for_request()
    assert context._pending_summary is None
    assert context._summary_absorbed_count == 0
    assert context._last_compaction_stats is not None
    assert "messages_absorbed_by_summary" not in context._last_compaction_stats


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_predicate_composes_with_hybrid_token_meter():
    """The predicate acts on the ladder's own estimator units, so it must not
    break when the TRIGGER is driven by a provider-anchored count instead."""
    context = _make_context(compact_clear_at_least=1, token_meter="hybrid")
    await _fill_until_compacted(context)
    view = await context.get_messages_for_request()
    assert view


@pytest.mark.asyncio
async def test_predicate_survives_a_context_with_no_hooks():
    """Event emission is optional; refusing must not depend on it."""
    context = _make_context(
        compact_clear_at_least=10_000_000, compact_max_consecutive_skips=99, hooks=None
    )
    await _fill_until_compacted(context)
    view = await context.get_messages_for_request()
    assert view
    assert context._clear_at_least_skips == 1


@pytest.mark.asyncio
async def test_a_refusal_is_not_a_boundary_for_the_last_user_replay():
    """`replay_last_user_on_compaction` fires once per compaction BOUNDARY,
    identified by (sticky_level, summary_absorbed_count). A refusal leaves
    both unchanged by design -- so on the first-ever refusal that identity
    still differs from the initial `None` and would look like a fresh
    boundary. Appending a verbatim user replay after a compaction that did
    not happen would both mislead the model and spend the tokens the refusal
    exists to save.
    """
    context = _make_context(
        compact_clear_at_least=10_000_000,
        compact_max_consecutive_skips=99,
        replay_last_user_on_compaction=True,
    )
    await _fill_until_compacted(context)
    view = await context.get_messages_for_request()

    assert context._clear_at_least_last_refused is True
    assert not any(
        (m.get("metadata") or {}).get("source") == "context-replay"
        for m in view
    ), "a refused compaction must not mark or replay a boundary"
    assert context._last_replayed_boundary is None


@pytest.mark.asyncio
async def test_replay_still_fires_on_an_allowed_boundary():
    """The guard above must not disable the feature it is protecting."""
    context = _make_context(
        compact_clear_at_least=1, replay_last_user_on_compaction=True
    )
    await _fill_until_compacted(context)
    view = await context.get_messages_for_request()

    assert context._clear_at_least_last_refused is False
    assert any(
        (m.get("metadata") or {}).get("source") == "context-replay"
        for m in view
    )
