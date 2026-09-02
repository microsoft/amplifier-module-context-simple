"""Adversarial tests for `compaction_strategy: "summary"`.

This strategy lifts IDEAS from amplifier-bundle-context-managed's rolling
summarizer (the structured 5-section prompt, the early-async-trigger design)
but rebuilds ALL plumbing on this module's own sticky/_seq machinery. The
donor module's own 5,890 LOC of tests missed two showstoppers found by
actually running it (see .amplifier/evaluation/treatment-validation/
20260901-t4-ctxmanaged/PROBE5-VERDICT.md):

  1. It drops a `function_call` while keeping its `function_call_output`
     (tool-pair atomicity violation) -- `_snap_to_tool_pair_boundary` only
     checked adjacency, not actual id-based pairing, and did no
     protected-boundary accounting at all.
  2. Its summary tiers are `role: "system"`, which gets hoisted into the
     provider's system block and busts the system-prompt cache breakpoint
     (measured: 7 distinct instruction hashes across one run vs. 1 for the
     control).

Every test class below is named for the specific failure mode it guards
against, treating the donor's design adversarially rather than assuming its
ideas are safe just because the PROMPT is good.

Coverage:
  - the strategy fires (async, early) and absorbs on swap-in
  - absorbed messages are sticky-recorded -- byte-identical re-serialization
    across repeated calls, and prefix-stable (append-only) as history grows
  - tool_calls/tool_result pairs are NEVER split at the absorb boundary
    (the donor's exact production failure), at both the unit
    (_snap_absorb_boundary) and integration (full swap) level
  - the summary message is role="user", enveloped in
    <system-reminder source="context-summary">, and NOT ephemeral
  - fallback to progressive compaction on summarizer failure/timeout/absent
    provider -- a turn is never blocked and never fails
  - config validation never crashes on a bad compaction_strategy value
  - compaction_strategy="summary" composes with token_meter in both modes
  - default ("progressive") mode remains completely untouched -- see also
    test_default_mode_byte_identical_with_summary_fields_present below,
    which is this file's own contribution to the "existing 76 tests green"
    guarantee (the full existing suite is run unmodified against this same
    source tree as a separate step).
"""

import asyncio
import logging

import pytest
from amplifier_core import ChatResponse, TextBlock
from amplifier_module_context_simple import SimpleContextManager, mount


class _FakeProvider:
    """Minimal stand-in for a Provider -- just enough of `.complete()` to
    drive the summarizer, with knobs for failure/timeout/echo-back testing.
    Deliberately has neither `get_model_info` nor `get_info`, so
    `_calculate_budget` falls back to `self.max_tokens` (matching how the
    rest of this module's test suite avoids needing a real provider)."""

    def __init__(self, response_text: str = "SUMMARY TEXT", delay: float = 0.0, raise_exc: Exception | None = None):
        self.response_text = response_text
        self.delay = delay
        self.raise_exc = raise_exc
        self.calls: list = []

    async def complete(self, request):
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_exc is not None:
            raise self.raise_exc
        return ChatResponse(content=[TextBlock(type="text", text=self.response_text)])


class _FakeHooks:
    """Minimal stand-in for amplifier_core.hooks.HookRegistry -- records
    every emitted event so tests can assert on the summarization lifecycle
    without depending on real HookRegistry internals."""

    def __init__(self):
        self.emitted: list[tuple[str, dict]] = []

    async def emit(self, event, data):
        self.emitted.append((event, data))


def _tool_call(call_id: str, tool: str = "bash") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "tool": tool, "arguments": {}}],
    }


def _tool_result(call_id: str, content: str = "result") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


async def _await_pending_task(context: SimpleContextManager) -> None:
    """Wait for an in-flight background summarization task to finish,
    grabbing the reference before it gets cleared in the task's own
    `finally` block."""
    task = context._summarization_task
    assert task is not None, "expected a background summarization task to be in flight"
    await task


def _strip_timestamps(messages: list[dict]) -> list[dict]:
    """Normalize out add_message()'s wall-clock timestamp so byte-stability
    comparisons focus on content/structure, not incidental timing."""
    result = []
    for msg in messages:
        meta = dict(msg.get("metadata") or {})
        meta.pop("timestamp", None)
        result.append({**msg, "metadata": meta})
    return result


# ---------------------------------------------------------------------------
# Config validation: never crash on a bad compaction_strategy value
# ---------------------------------------------------------------------------


def test_default_compaction_strategy_is_progressive():
    context = SimpleContextManager()
    assert context.compaction_strategy == "progressive"


def test_invalid_compaction_strategy_falls_back_to_progressive_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        context = SimpleContextManager(compaction_strategy="bogus")

    assert context.compaction_strategy == "progressive"
    assert any("unknown compaction_strategy" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_mount_invalid_compaction_strategy_falls_back_with_warning(caplog):
    class _Coordinator:
        def __init__(self):
            self.hooks = None
            self.mounted = {}

        async def mount(self, kind, instance):
            self.mounted[kind] = instance

    coordinator = _Coordinator()
    with caplog.at_level(logging.WARNING):
        await mount(coordinator, {"compaction_strategy": "bogus"})

    assert coordinator.mounted["context"].compaction_strategy == "progressive"
    assert any("unknown compaction_strategy" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_mount_threads_summary_config_through():
    class _Coordinator:
        def __init__(self):
            self.hooks = None
            self.mounted = {}

        async def mount(self, kind, instance):
            self.mounted[kind] = instance

    coordinator = _Coordinator()
    await mount(
        coordinator,
        {
            "compaction_strategy": "summary",
            "summary_trigger": 0.45,
            "summarization_model": "gpt-test",
            "summarization_timeout_s": 5.0,
        },
    )
    context = coordinator.mounted["context"]
    assert context.compaction_strategy == "summary"
    assert context.summary_trigger == 0.45
    assert context.summarization_model == "gpt-test"
    assert context.summarization_timeout_s == 5.0


# ---------------------------------------------------------------------------
# Default mode ("progressive") is completely untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_mode_byte_identical_with_summary_fields_present():
    """A manager with compaction_strategy left at its default must produce
    byte-identical output to one explicitly asking for "progressive" --
    the new fields/branches must be true no-ops, never just usually-empty."""
    baseline = SimpleContextManager(
        max_tokens=1_000, compact_threshold=0.5, compaction_notice_enabled=False
    )
    explicit = SimpleContextManager(
        max_tokens=1_000,
        compact_threshold=0.5,
        compaction_notice_enabled=False,
        compaction_strategy="progressive",
    )
    for i in range(20):
        msg = {"role": "user", "content": f"message {i} " + "x" * 50}
        await baseline.add_message(dict(msg))
        await explicit.add_message(dict(msg))

    baseline_view = await baseline.get_messages_for_request()
    explicit_view = await explicit.get_messages_for_request()

    assert _strip_timestamps(baseline_view) == _strip_timestamps(explicit_view)
    # Never even glances at a provider or spawns a task in default mode.
    assert baseline._cached_provider is None
    assert baseline._summarization_task is None
    assert baseline._pending_summary is None


@pytest.mark.asyncio
async def test_progressive_mode_never_triggers_summarizer_even_with_provider():
    """Passing a provider to get_messages_for_request() in the default
    ("progressive") mode must never cache it or touch any summary state --
    those branches are gated on compaction_strategy == "summary" only."""
    context = SimpleContextManager(
        max_tokens=200, compact_threshold=0.5, compaction_notice_enabled=False
    )
    provider = _FakeProvider()
    for i in range(20):
        await context.add_message({"role": "user", "content": f"msg {i} " + "x" * 30})

    await context.get_messages_for_request(provider=provider)

    assert context._cached_provider is None
    assert context._is_summarizing is False
    assert provider.calls == []


# ---------------------------------------------------------------------------
# Strategy fires (early, async) and absorbs on swap-in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_strategy_fires_absorbs_and_swaps_in():
    context = SimpleContextManager(
        compaction_strategy="summary",
        compaction_notice_enabled=False,
        protected_recent=0.5,
        summary_trigger=0.3,
        target_usage=0.2,
        compact_threshold=0.99,  # keep the outer progressive gate CLOSED for now
        max_tokens=1_000_000,  # corrected below once real usage is known
    )
    for i in range(30):
        await context.add_message({"role": "user", "content": f"user turn {i} " + "x" * 40})
        await context.add_message(
            {"role": "assistant", "content": f"assistant reply {i} " + "y" * 40}
        )

    raw_tokens = context._estimate_tokens(context.messages)
    # Usage sits comfortably above summary_trigger (0.3) but below
    # compact_threshold (0.99): the summary trigger should fire; the outer
    # progressive gate should not.
    context.max_tokens = int(raw_tokens / 0.5)

    provider = _FakeProvider(response_text="COMPACT SUMMARY OF EARLY TURNS")
    view1 = await context.get_messages_for_request(provider=provider)

    assert context._is_summarizing is True
    assert context._pending_summary is None, "must not resolve synchronously"
    assert context._last_compaction_stats is None, "outer gate must stay closed"
    assert view1 is not None

    await _await_pending_task(context)

    assert len(provider.calls) == 1
    assert context._pending_summary is not None
    assert context._is_summarizing is False
    assert context._removed_seqs == set(), "must not absorb until the outer gate actually fires"

    # Now open the outer gate so the pending summary gets swapped in. Set
    # comfortably ABOVE target_usage (0.2) so the post-absorption level no
    # longer "exceeds threshold" and no progressive level is also needed --
    # but still below the pre-swap ~0.5 usage, so the gate actually opens.
    context.compact_threshold = 0.3
    view2 = await context.get_messages_for_request(provider=provider)

    assert context._last_compaction_stats is not None
    assert context._last_compaction_stats["strategy_level"] == 0, (
        "summary alone should resolve this pass with no progressive level needed"
    )
    assert context._removed_seqs, "absorbed messages must be recorded removed"

    summary_msgs = [
        m
        for m in view2
        if (m.get("metadata") or {}).get("type") == "context_summary"
    ]
    assert len(summary_msgs) == 1
    assert "COMPACT SUMMARY OF EARLY TURNS" in summary_msgs[0]["content"]

    # The absorbed originals must be gone from the served view.
    assert not any(
        isinstance(m.get("content"), str) and "user turn 0 " in m["content"] for m in view2
    )


@pytest.mark.asyncio
async def test_summary_never_re_absorbs_its_own_past_summary_message():
    """A prior summary message (metadata.type == "context_summary") must
    never itself become a candidate for a later absorption round -- this
    PR deliberately does not implement tier merging (see module docstring);
    each escalation produces its own standalone summary."""
    context = SimpleContextManager(compaction_strategy="summary", protected_recent=0.1)
    await context.add_message({"role": "user", "content": "hello"})
    summary_msg = context._make_summary_message("a past summary")
    context.messages.append(summary_msg)
    for i in range(10):
        await context.add_message({"role": "user", "content": f"turn {i} " + "z" * 30})

    seqs = context._select_summary_absorb_seqs(excess_tokens=10_000)
    summary_seq = summary_msg["metadata"]["_seq"]
    assert seqs is None or summary_seq not in seqs


# ---------------------------------------------------------------------------
# Sticky recording + byte/prefix stability across repeated calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_absorbed_span_is_byte_identical_across_repeated_calls():
    """Once absorbed, repeated get_messages_for_request() calls (with no
    new pending summary) must reproduce the exact same view -- this is what
    _record_removed + _apply_sticky_decisions guarantee by construction."""
    context = SimpleContextManager(
        compaction_strategy="summary",
        compaction_notice_enabled=False,
        protected_recent=0.5,
        summary_trigger=0.1,
        target_usage=0.05,
        compact_threshold=0.05,
        max_tokens=1_000_000,
    )
    for i in range(20):
        await context.add_message({"role": "user", "content": f"turn {i} " + "x" * 40})
        await context.add_message({"role": "assistant", "content": f"reply {i} " + "y" * 40})

    raw_tokens = context._estimate_tokens(context.messages)
    context.max_tokens = int(raw_tokens / 0.9)  # comfortably above every threshold above

    provider = _FakeProvider(response_text="STABLE SUMMARY")
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)
    view_a = await context.get_messages_for_request(provider=provider)
    view_b = await context.get_messages_for_request(provider=provider)

    assert _strip_timestamps(view_a) == _strip_timestamps(view_b)


@pytest.mark.asyncio
async def test_prefix_is_append_only_as_new_turns_arrive_after_a_swap():
    """After a summary swap, growing the conversation further must only
    ever APPEND to the previously-served view, never reorder or rewrite
    the shared prefix -- the property prompt caching depends on."""
    context = SimpleContextManager(
        compaction_strategy="summary",
        compaction_notice_enabled=False,
        protected_recent=0.5,
        summary_trigger=0.1,
        target_usage=0.05,
        compact_threshold=0.99,  # closed until the summarizer has had time to finish
        max_tokens=1_000_000,
    )
    for i in range(20):
        await context.add_message({"role": "user", "content": f"turn {i} " + "x" * 40})
        await context.add_message({"role": "assistant", "content": f"reply {i} " + "y" * 40})

    raw_tokens = context._estimate_tokens(context.messages)
    context.max_tokens = int(raw_tokens / 0.9)

    provider = _FakeProvider(response_text="STABLE SUMMARY")
    await context.get_messages_for_request(provider=provider)  # fires the trigger only
    await _await_pending_task(context)

    # Open the outer gate (target_usage=0.05 leaves ample headroom under it,
    # so growing by one small message afterward will NOT re-open escalation).
    context.compact_threshold = 0.3
    view_before = _strip_timestamps(await context.get_messages_for_request(provider=provider))
    assert context._last_compaction_stats is not None  # the swap actually ran

    await context.add_message({"role": "user", "content": "one more turn"})
    view_after = _strip_timestamps(await context.get_messages_for_request(provider=provider))

    assert view_after[: len(view_before)] == view_before
    assert view_after[len(view_before) :] == [
        {"role": "user", "content": "one more turn", "metadata": {}}
    ]


# ---------------------------------------------------------------------------
# Tool-pair atomicity at the absorb boundary -- the donor's exact failure
# ---------------------------------------------------------------------------


class TestSnapAbsorbBoundaryUnit:
    """Direct unit tests for _snap_absorb_boundary -- the fix for the
    donor's `_snap_to_tool_pair_boundary`, which only checked adjacency and
    did no protected-boundary accounting, and in production dropped a
    `function_call` while keeping its `function_call_output`."""

    def test_extends_to_include_a_straddling_result(self):
        context = SimpleContextManager(compaction_strategy="summary")
        live = [
            {"role": "user", "content": "u0"},
            _tool_call("call_1"),
            _tool_result("call_1"),
            {"role": "user", "content": "u1"},
        ]
        # end_idx=2 would include the call but exclude its own result.
        assert context._snap_absorb_boundary(live, 2, protected_boundary=4) == 3

    def test_extends_to_include_a_non_adjacent_straggler_result(self):
        """The donor's adjacency-only heuristic misses this: the result is
        not the message immediately following the call."""
        context = SimpleContextManager(compaction_strategy="summary")
        live = [
            _tool_call("call_1"),
            {"role": "assistant", "content": "unrelated narration"},
            _tool_result("call_1"),
        ]
        assert context._snap_absorb_boundary(live, 1, protected_boundary=3) == 3

    def test_shrinks_to_exclude_a_call_whose_result_is_protected(self):
        """When extending would cross into the protected tail, the whole
        pair must be excluded -- never split, never absorbed partially."""
        context = SimpleContextManager(compaction_strategy="summary")
        live = [
            {"role": "user", "content": "u0"},
            _tool_call("call_1"),
            _tool_result("call_1"),
            {"role": "user", "content": "u1"},
        ]
        # protected_boundary=2: the result at index 2 is already protected.
        assert context._snap_absorb_boundary(live, 2, protected_boundary=2) == 1

    def test_multiple_results_one_straddling_excludes_the_whole_call(self):
        context = SimpleContextManager(compaction_strategy="summary")
        live = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "a", "tool": "x", "arguments": {}}, {"id": "b", "tool": "y", "arguments": {}}],
            },
            _tool_result("a"),
            _tool_result("b"),
        ]
        # protected_boundary=2 protects the second result (index 2) -> the
        # whole call (and its first, otherwise-includable result) must go.
        assert context._snap_absorb_boundary(live, 2, protected_boundary=2) == 0

    def test_clean_pair_within_bounds_is_unchanged(self):
        context = SimpleContextManager(compaction_strategy="summary")
        live = [_tool_call("call_1"), _tool_result("call_1"), {"role": "user", "content": "u1"}]
        assert context._snap_absorb_boundary(live, 2, protected_boundary=3) == 2

    def test_zero_boundary_returns_zero(self):
        context = SimpleContextManager(compaction_strategy="summary")
        assert context._snap_absorb_boundary([], 0, protected_boundary=0) == 0


@pytest.mark.asyncio
async def test_integration_tool_pair_never_split_at_absorb_boundary():
    """End-to-end: a tool_calls/tool_result pair sitting right at the
    natural absorb boundary must be absorbed (or not) as an atomic unit --
    never left with one half served and the other half gone, which is
    exactly the InvalidRequestError the donor shipped in production."""
    context = SimpleContextManager(
        compaction_strategy="summary",
        compaction_notice_enabled=False,
        protected_recent=0.3,
        summary_trigger=0.1,
        target_usage=0.05,
        compact_threshold=0.05,
        max_tokens=1_000_000,
    )
    for i in range(10):
        await context.add_message({"role": "user", "content": f"turn {i} " + "x" * 60})
        await context.add_message({"role": "assistant", "content": f"reply {i} " + "y" * 60})
    # A tool pair positioned squarely in what will become the absorb
    # candidate pool (well before the protected tail).
    await context.add_message(_tool_call("straddle_call"))
    await context.add_message(_tool_result("straddle_call", "tool output payload"))
    for i in range(10, 20):
        await context.add_message({"role": "user", "content": f"turn {i} " + "x" * 60})
        await context.add_message({"role": "assistant", "content": f"reply {i} " + "y" * 60})

    raw_tokens = context._estimate_tokens(context.messages)
    context.max_tokens = int(raw_tokens / 0.9)

    provider = _FakeProvider(response_text="SUMMARY")
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)
    view = await context.get_messages_for_request(provider=provider)

    has_call = any(m.get("role") == "assistant" and m.get("tool_calls") for m in view)
    has_result = any(m.get("role") == "tool" for m in view)
    assert has_call == has_result, (
        f"tool pair split at the absorb boundary! has_call={has_call} has_result={has_result}"
    )
    if has_call:
        for i, m in enumerate(view):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                assert i + 1 < len(view) and view[i + 1].get("role") == "tool"


# ---------------------------------------------------------------------------
# Summary message shape: role=user, enveloped, NOT ephemeral
# ---------------------------------------------------------------------------


def test_summary_message_is_user_role_enveloped_and_non_ephemeral():
    context = SimpleContextManager(compaction_strategy="summary")
    msg = context._make_summary_message("the summary body")

    assert msg["role"] == "user", "must never be role=system -- see module docstring"
    assert msg["content"].startswith('<system-reminder source="context-summary">')
    assert msg["content"].endswith("</system-reminder>")
    assert "the summary body" in msg["content"]
    assert msg["metadata"]["type"] == "context_summary"
    assert "ephemeral" not in msg["metadata"], (
        "must NOT be marked ephemeral -- it is meant to persist as stable history"
    )
    assert "_seq" in msg["metadata"]


def test_summary_message_gets_a_fresh_seq_like_add_message_would():
    context = SimpleContextManager(compaction_strategy="summary")
    before = context._next_seq
    msg = context._make_summary_message("text")
    assert msg["metadata"]["_seq"] == before
    assert context._next_seq == before + 1


# ---------------------------------------------------------------------------
# Fallback to progressive compaction: failure / timeout / no provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_to_progressive_on_summarizer_exception():
    context = SimpleContextManager(
        compaction_strategy="summary",
        compaction_notice_enabled=False,
        protected_recent=0.3,
        summary_trigger=0.1,
        target_usage=0.2,
        compact_threshold=0.1,
        max_tokens=1_000_000,
    )
    for i in range(20):
        await context.add_message({"role": "user", "content": f"turn {i} " + "x" * 40})
        await context.add_message({"role": "assistant", "content": f"reply {i} " + "y" * 40})

    raw_tokens = context._estimate_tokens(context.messages)
    context.max_tokens = int(raw_tokens / 0.9)

    provider = _FakeProvider(raise_exc=RuntimeError("summarizer is down"))
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    assert context._pending_summary is None
    assert context._summarization_failures == 1

    # A second call must still make forward progress via the progressive
    # ladder -- never blocked, never raised, even though summarization just
    # failed.
    view2 = await context.get_messages_for_request(provider=provider)
    assert context._last_compaction_stats is not None
    assert context._last_compaction_stats["strategy_level"] >= 1
    assert len(view2) <= len(context.messages)


@pytest.mark.asyncio
async def test_fallback_to_progressive_on_summarizer_timeout():
    context = SimpleContextManager(
        compaction_strategy="summary",
        compaction_notice_enabled=False,
        protected_recent=0.3,
        summary_trigger=0.1,
        target_usage=0.2,
        compact_threshold=0.1,
        max_tokens=1_000_000,
        summarization_timeout_s=0.01,
    )
    for i in range(20):
        await context.add_message({"role": "user", "content": f"turn {i} " + "x" * 40})
        await context.add_message({"role": "assistant", "content": f"reply {i} " + "y" * 40})

    raw_tokens = context._estimate_tokens(context.messages)
    context.max_tokens = int(raw_tokens / 0.9)

    provider = _FakeProvider(delay=1.0)  # far longer than summarization_timeout_s
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    assert context._pending_summary is None
    assert context._summarization_failures == 1

    view2 = await context.get_messages_for_request(provider=provider)
    assert context._last_compaction_stats is not None
    assert context._last_compaction_stats["strategy_level"] >= 1
    assert len(view2) <= len(context.messages)


@pytest.mark.asyncio
async def test_fallback_to_progressive_when_no_provider_ever_passed():
    """compaction_strategy="summary" but the caller never passes a
    provider (e.g. a code path that doesn't support it yet) -- must behave
    exactly like progressive compaction, never raise, never hang."""
    context = SimpleContextManager(
        compaction_strategy="summary",
        max_tokens=100,
        compact_threshold=0.5,
        compaction_notice_enabled=False,
    )
    for i in range(20):
        await context.add_message({"role": "user", "content": f"msg {i}"})
        await context.add_message({"role": "assistant", "content": f"reply {i} " + "z" * 20})

    view = await context.get_messages_for_request()  # provider=None (default)

    assert context._cached_provider is None
    assert context._is_summarizing is False
    assert context._last_compaction_stats is not None
    assert "messages_absorbed_by_summary" in context._last_compaction_stats
    assert context._last_compaction_stats["messages_absorbed_by_summary"] == 0
    assert len(view) < len(context.messages)


@pytest.mark.asyncio
async def test_never_triggers_twice_while_one_is_already_in_flight():
    context = SimpleContextManager(
        compaction_strategy="summary",
        compaction_notice_enabled=False,
        protected_recent=0.5,
        summary_trigger=0.1,
        target_usage=0.05,
        compact_threshold=0.99,
        max_tokens=1_000_000,
    )
    for i in range(20):
        await context.add_message({"role": "user", "content": f"turn {i} " + "x" * 40})
        await context.add_message({"role": "assistant", "content": f"reply {i} " + "y" * 40})
    raw_tokens = context._estimate_tokens(context.messages)
    context.max_tokens = int(raw_tokens / 0.5)

    provider = _FakeProvider(delay=0.05)
    await context.get_messages_for_request(provider=provider)
    assert context._is_summarizing is True
    await asyncio.sleep(0)  # let the task actually start running (still in-flight, delay=0.05)
    await context.get_messages_for_request(provider=provider)
    await context.get_messages_for_request(provider=provider)
    assert context._is_summarizing is True, "still in flight -- the delay hasn't elapsed yet"

    await _await_pending_task(context)
    assert len(provider.calls) == 1, "must not fire a second concurrent summarization call"


@pytest.mark.asyncio
async def test_stale_pending_summary_discarded_gracefully_not_a_failure():
    """If the absorbed span was already fully resolved by an intervening
    escalation before the swap runs, the pending summary must be discarded
    quietly -- NOT counted as a failure, and NOT crash."""
    context = SimpleContextManager(compaction_strategy="summary", protected_recent=0.9)
    for i in range(5):
        await context.add_message({"role": "user", "content": f"turn {i}"})

    seqs = [m["metadata"]["_seq"] for m in context.messages[:2]]
    context._pending_summary = {"seqs": frozenset(seqs), "text": "irrelevant"}
    # Simulate an intervening escalation that already removed these seqs.
    for s in seqs:
        context._removed_seqs.add(s)

    non_system = [m for m in context.messages if m.get("role") != "system"]
    result, did_swap = await context._swap_in_pending_summary(non_system)

    assert did_swap is False
    assert result == non_system
    assert context._pending_summary is None
    assert context._summarization_failures == 0


# ---------------------------------------------------------------------------
# Composes with the real-usage token meter, both modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_strategy_works_with_token_meter_estimate_mode():
    """token_meter left at its default ("estimate"): the summary trigger
    must be driven by the same estimator the outer gate uses -- no crash,
    no divergence in which signal feeds which check."""
    context = SimpleContextManager(
        compaction_strategy="summary",
        token_meter="estimate",
        compaction_notice_enabled=False,
        protected_recent=0.5,
        summary_trigger=0.3,
        target_usage=0.2,
        compact_threshold=0.99,
        max_tokens=1_000_000,
    )
    for i in range(20):
        await context.add_message({"role": "user", "content": f"turn {i} " + "x" * 40})
        await context.add_message({"role": "assistant", "content": f"reply {i} " + "y" * 40})
    raw_tokens = context._estimate_tokens(context.messages)
    context.max_tokens = int(raw_tokens / 0.5)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)

    assert context._last_token_meter_stats["mode"] == "estimate"
    assert context._is_summarizing is True
    await _await_pending_task(context)


@pytest.mark.asyncio
async def test_summary_strategy_works_with_token_meter_actual_mode():
    """token_meter="actual": a real llm:response measurement, once
    observed, must be able to drive the EARLY summary trigger too (not just
    the outer progressive gate) -- _maybe_trigger_summary_compaction reuses
    the same already-meter-aware `token_count`."""
    context = SimpleContextManager(
        compaction_strategy="summary",
        token_meter="actual",
        compaction_notice_enabled=False,
        protected_recent=0.5,
        summary_trigger=0.3,
        target_usage=0.2,
        compact_threshold=0.99,
        max_tokens=100_000,
    )
    for i in range(6):
        await context.add_message({"role": "user", "content": f"msg {i}"})
        await context.add_message({"role": "assistant", "content": f"reply {i}"})

    # Estimator alone would NOT cross summary_trigger (0.3) -- real usage
    # must be what drives the trigger here.
    estimate = context._estimate_tokens(context.messages)
    assert estimate / 100_000 < 0.3

    await context._on_llm_response("llm:response", {"usage": {"input_tokens": 40_000}})

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)

    assert context._last_token_meter_stats["source"] == "measured"
    assert context._is_summarizing is True, (
        "the real measurement (40_000/100_000 = 0.4) should have crossed "
        "summary_trigger (0.3) even though the estimator alone would not"
    )
    await _await_pending_task(context)


# ---------------------------------------------------------------------------
# Observability: hook events fire around the summarization lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hooks_receive_pre_and_post_summarize_events():
    hooks = _FakeHooks()
    context = SimpleContextManager(
        compaction_strategy="summary",
        compaction_notice_enabled=False,
        protected_recent=0.5,
        summary_trigger=0.3,
        target_usage=0.2,
        compact_threshold=0.99,
        max_tokens=1_000_000,
        hooks=hooks,
    )
    for i in range(20):
        await context.add_message({"role": "user", "content": f"turn {i} " + "x" * 40})
        await context.add_message({"role": "assistant", "content": f"reply {i} " + "y" * 40})
    raw_tokens = context._estimate_tokens(context.messages)
    context.max_tokens = int(raw_tokens / 0.5)

    provider = _FakeProvider(response_text="SUMMARY")
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    events = [name for name, _ in hooks.emitted]
    assert "context:pre_summarize" in events
    assert "context:post_summarize" in events
