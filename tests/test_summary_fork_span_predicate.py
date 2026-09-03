"""Adversarial tests for the span-size predicate in front of the summarizer fork.

WHAT MEASUREMENT THIS FILE EXISTS TO ENCODE. Lane `model_performance-6da`
ran `summary_call_mode: "fork"` end-to-end (S5-CRAC, n=3/arm balanced across
two containers, gpt-5.6-terra@medium) and found it COST-NEUTRAL: total run
cost -0.8%, i.e. noise. The mechanism visibly works -- a forked call reads a
median 85.7% of its own prompt from cache, a standalone one reads 0.0% --
but it pays for itself exactly:

    forked call:     $0.00077 / 1k own-prompt tokens, median prompt 26,620 tok
    standalone call: $0.00350 / 1k own-prompt tokens, median prompt  5,129 tok
    4.5x cheaper per token x 5.2x more tokens ~= 1.0

The trade is strongly span-size dependent, and THAT is the lever:

    standalone, span > 30,000 tok:  $0.1410 mean (n=17)
    standalone, span <= 15,000 tok: $0.0151 mean (n=40)
    forked, any span:               $0.0265 mean (n=20) -- roughly FLAT

A fork wins ~5x on the tail and loses on the median. So gate it. The
break-even is DERIVED, not chosen: a standalone call pays for the SPAN at
the uncached rate, a fork pays for the PREFIX at the cached rate, so a fork
is cheaper exactly when span/prefix > 0.00077/0.00350 = 0.22.

Evidence: `.amplifier/evaluation/probes/6da-summary-fork/FINDINGS.md` §6 in
the openai-evals-team-ci repo.

The tests below are written against the ways this can silently go wrong:

  1. THE DEFAULT MUST NOT MOVE -- TWICE OVER. Not only must plain
     `standalone` be untouched, `fork` WITHOUT an explicit ratio must also
     be byte-identical to fork mode before this feature existed. A
     predicate that quietly turns itself on is a behavior change wearing an
     opt-in's clothes. Group A.
  2. A PREDICATE THAT NEVER FIRES IS A NO-OP THAT PASSES ITS TESTS. Group B
     pins the boundary from BOTH sides against a ratio measured from the
     fixture itself, so "fires above, does not fire below" is asserted at
     the exact threshold rather than somewhere vaguely near it.
  3. A DECLINE IS NOT A FAILURE, AND MUST NOT LOOK LIKE ONE. `fork_declines`
     and `fork_fallbacks` answer different questions -- "the predicate
     worked" vs "the fork broke". Group C proves they never contaminate
     each other, in either direction, and that ordering puts alignment
     first so no span size can buy a misaligned fork.
  4. NOTHING ELSE MOVES. Group D proves a declined fork produces the
     standalone request byte-for-byte, consumes no `_seq`, and leaves span
     selection and tool-pair integrity exactly where the control leaves them.
"""

import hashlib
import json
import logging

import pytest
from amplifier_core import ChatResponse, Message, TextBlock
from amplifier_module_context_simple import (
    DEFAULT_FORK_MIN_SPAN_RATIO,
    SimpleContextManager,
    mount,
)


class _FakeProvider:
    """Records every request handed to it so tests can assert the exact
    shape that would go on the wire."""

    def __init__(self, response_text: str = "SUMMARY TEXT"):
        self.response_text = response_text
        self.calls: list = []

    async def complete(self, request):
        self.calls.append(request)
        return ChatResponse(content=[TextBlock(type="text", text=self.response_text)])


class _Coordinator:
    def __init__(self):
        self.hooks = None
        self.mounted = {}

    async def mount(self, kind, instance):
        self.mounted[kind] = instance


def _tools():
    return [
        {
            "name": "bash",
            "description": "run a command",
            "parameters": {"type": "object", "properties": {}},
        }
    ]


def _digest(messages) -> str:
    dumps = []
    for m in messages:
        dumps.append(m.model_dump() if isinstance(m, Message) else Message(**m).model_dump())
    return hashlib.sha256(
        json.dumps(dumps, sort_keys=True, default=str).encode()
    ).hexdigest()


def _strip_timestamps(messages: list[dict]) -> list[dict]:
    result = []
    for msg in messages:
        meta = dict(msg.get("metadata") or {})
        meta.pop("timestamp", None)
        result.append({**msg, "metadata": meta})
    return result


async def _await_pending_task(context: SimpleContextManager) -> None:
    task = context._summarization_task
    assert task is not None, "expected a background summarization task in flight"
    await task


def _summary_manager(**overrides) -> SimpleContextManager:
    kwargs = dict(
        compaction_strategy="summary",
        compaction_notice_enabled=False,
        protected_recent=0.5,
        summary_trigger=0.3,
        target_usage=0.2,
        compact_threshold=0.99,  # keep the outer progressive gate CLOSED
        max_tokens=1_000_000,
    )
    kwargs.update(overrides)
    return SimpleContextManager(**kwargs)


async def _fill(context: SimpleContextManager, turns: int = 30) -> None:
    for i in range(turns):
        await context.add_message({"role": "user", "content": f"user turn {i} " + "x" * 40})
        await context.add_message(
            {"role": "assistant", "content": f"assistant reply {i} " + "y" * 40}
        )


async def _arm_below_trigger(context: SimpleContextManager) -> list[dict]:
    """Serve one request with usage BELOW summary_trigger -- this is what
    records the prefix a later fork appends to."""
    raw = context._estimate_tokens(context.messages)
    context.max_tokens = int(raw / 0.1)
    view = await context.get_messages_for_request(provider=_FakeProvider())
    assert context._is_summarizing is False, "must not trigger below summary_trigger"
    return view


def _cross_trigger(context: SimpleContextManager) -> None:
    raw = context._estimate_tokens(context.messages)
    context.max_tokens = int(raw / 0.5)


async def _run_one_summarization(context: SimpleContextManager) -> _FakeProvider:
    """Arm the fork seam, cross the trigger, and drain the background task."""
    await _fill(context)
    context.note_request_sent(tools=_tools(), model="pinned-model")
    await _arm_below_trigger(context)
    _cross_trigger(context)
    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)
    return provider


async def _measure_realized_ratio(**overrides) -> float:
    """Run one summarization with the predicate armed at a ratio nothing can
    meet, purely to read back what span:prefix the fixture ACTUALLY produces.

    Every boundary test below is anchored to this measured number rather
    than to a constant guessed by the test author -- otherwise "fires above
    the threshold" degrades into "fires somewhere, probably".
    """
    context = _summary_manager(
        summary_call_mode="fork", summary_fork_min_span_ratio=99.0, **overrides
    )
    await _run_one_summarization(context)
    measure = context.last_summary_call_stats["span_measure"]
    assert measure is not None and measure["span_ratio"] is not None
    return measure["span_ratio"]


# ---------------------------------------------------------------------------
# Group A -- the default must not move, in EITHER mode
# ---------------------------------------------------------------------------


def test_default_ratio_is_none_in_every_default_configuration():
    assert SimpleContextManager().summary_fork_min_span_ratio is None
    assert _summary_manager().summary_fork_min_span_ratio is None
    assert _summary_manager(summary_call_mode="fork").summary_fork_min_span_ratio is None


def test_plain_fork_mode_resolves_to_no_predicate_at_all():
    """`fork` without an explicit ratio must keep forking unconditionally.
    Turning the predicate on for it would silently change PR #27's shipped
    behavior for anyone already using it."""
    assert _summary_manager(summary_call_mode="fork")._effective_fork_min_span_ratio() is None
    assert _summary_manager()._effective_fork_min_span_ratio() is None


def test_auto_resolves_to_the_measured_break_even():
    context = _summary_manager(summary_call_mode="auto")
    assert context.summary_call_mode == "auto"
    assert context._effective_fork_min_span_ratio() == DEFAULT_FORK_MIN_SPAN_RATIO


def test_the_shipped_default_is_the_number_6da_measured():
    """0.22 is $0.00077/$0.00350 -- the cached rate over the uncached rate --
    from FINDINGS.md §6, not a round number someone liked. Pinned so a
    future edit to the constant has to argue with this test."""
    assert DEFAULT_FORK_MIN_SPAN_RATIO == pytest.approx(0.00077 / 0.00350, abs=0.005)


def test_an_explicit_ratio_overrides_auto_in_both_directions():
    assert (
        _summary_manager(
            summary_call_mode="auto", summary_fork_min_span_ratio=0.9
        )._effective_fork_min_span_ratio()
        == 0.9
    )
    assert (
        _summary_manager(
            summary_call_mode="fork", summary_fork_min_span_ratio=0.05
        )._effective_fork_min_span_ratio()
        == 0.05
    )


def test_auto_is_a_real_mode_and_never_warns(caplog):
    with caplog.at_level(logging.WARNING):
        context = SimpleContextManager(summary_call_mode="auto")
    assert context.summary_call_mode == "auto"
    assert not [r for r in caplog.records if "summary_call_mode" in r.message]


@pytest.mark.parametrize("bad", ["banana", -1.0, float("nan"), object()])
def test_an_unusable_ratio_disables_the_predicate_loudly(caplog, bad):
    with caplog.at_level(logging.WARNING):
        context = SimpleContextManager(summary_fork_min_span_ratio=bad)
    assert context.summary_fork_min_span_ratio is None
    assert any("summary_fork_min_span_ratio" in r.message for r in caplog.records)


def test_zero_disables_the_predicate_silently(caplog):
    """0 is how `compact_clear_at_least` already spells "off"; spelling it
    the same way here must not be treated as a mistake."""
    with caplog.at_level(logging.WARNING):
        context = SimpleContextManager(summary_fork_min_span_ratio=0)
    assert context.summary_fork_min_span_ratio is None
    assert not [r for r in caplog.records if "summary_fork_min_span_ratio" in r.message]


@pytest.mark.asyncio
async def test_standalone_mode_never_evaluates_the_predicate():
    """The default path must not so much as write a predicate attribute."""
    context = _summary_manager(summary_fork_min_span_ratio=0.5)
    await _fill(context)
    _cross_trigger(context)
    await context.get_messages_for_request(provider=_FakeProvider())
    await _await_pending_task(context)

    assert context._last_fork_span_measure is None
    assert context._summary_fork_declines == 0
    stats = context.last_summary_call_stats
    assert stats["mode_used"] == "standalone"
    assert stats["span_measure"] is None
    assert stats["fork_declines"] == 0


@pytest.mark.asyncio
async def test_fork_without_a_ratio_is_unchanged_from_before_the_predicate():
    """The regression that matters most: fork mode with the predicate off
    must fork, and must not record a measurement it never took."""
    context = _summary_manager(summary_call_mode="fork")
    provider = await _run_one_summarization(context)

    stats = context.last_summary_call_stats
    assert stats["mode_used"] == "fork"
    assert stats["reason"] is None
    assert stats["span_measure"] is None, "predicate off must not measure"
    assert stats["fork_declines"] == 0
    assert stats["fork_fallbacks"] == 0
    assert len(provider.calls[0].messages) > 2, "a fork, not a standalone pair"


@pytest.mark.asyncio
async def test_mount_threads_the_new_knobs_through():
    coordinator = _Coordinator()
    await mount(
        coordinator,
        {
            "compaction_strategy": "summary",
            "summary_call_mode": "auto",
            "summary_fork_min_span_ratio": 0.4,
        },
    )
    context = coordinator.mounted["context"]
    assert context.summary_call_mode == "auto"
    assert context.summary_fork_min_span_ratio == 0.4

    coordinator = _Coordinator()
    await mount(coordinator, {"compaction_strategy": "summary", "summary_call_mode": "auto"})
    assert (
        coordinator.mounted["context"]._effective_fork_min_span_ratio()
        == DEFAULT_FORK_MIN_SPAN_RATIO
    )

    coordinator = _Coordinator()
    await mount(coordinator, {"compaction_strategy": "summary"})
    assert coordinator.mounted["context"].summary_fork_min_span_ratio is None


# ---------------------------------------------------------------------------
# Group B -- the predicate fires above the threshold and not below it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_span_below_the_threshold_declines_the_fork():
    context = _summary_manager(summary_call_mode="fork", summary_fork_min_span_ratio=99.0)
    provider = await _run_one_summarization(context)

    stats = context.last_summary_call_stats
    assert stats["mode_used"] == "standalone"
    assert "below the summary_fork_min_span_ratio" in stats["reason"]
    assert stats["fork_declines"] == 1
    assert stats["fork_fallbacks"] == 0
    assert len(provider.calls[0].messages) == 2, "the standalone two-message pair"


@pytest.mark.asyncio
async def test_a_span_above_the_threshold_forks():
    context = _summary_manager(
        summary_call_mode="fork", summary_fork_min_span_ratio=0.000_001
    )
    provider = await _run_one_summarization(context)

    stats = context.last_summary_call_stats
    assert stats["mode_used"] == "fork"
    assert stats["reason"] is None
    assert stats["fork_declines"] == 0
    assert stats["span_measure"]["span_ratio"] > 0.000_001
    assert len(provider.calls[0].messages) > 2


@pytest.mark.asyncio
async def test_the_threshold_is_an_inclusive_boundary_not_a_vague_region():
    """Pinned from both sides against the fixture's OWN realized ratio.

    A predicate tested only at 0.000001 and 99.0 would pass while being off
    by an order of magnitude. These two assertions are separated by a single
    epsilon around the real number.
    """
    ratio = await _measure_realized_ratio()

    at_threshold = _summary_manager(
        summary_call_mode="fork", summary_fork_min_span_ratio=ratio
    )
    await _run_one_summarization(at_threshold)
    assert at_threshold.last_summary_call_stats["mode_used"] == "fork", (
        "ratio == threshold must FORK: the comparison is >=, so the "
        "break-even case takes the cache win rather than discarding it"
    )

    just_above = _summary_manager(
        summary_call_mode="fork", summary_fork_min_span_ratio=ratio * 1.001
    )
    await _run_one_summarization(just_above)
    assert just_above.last_summary_call_stats["mode_used"] == "standalone"


@pytest.mark.asyncio
async def test_auto_mode_actually_gates_on_the_default():
    """`auto` must be the predicate wired to DEFAULT_FORK_MIN_SPAN_RATIO --
    not a third name for unconditional forking."""
    ratio = await _measure_realized_ratio()
    context = _summary_manager(summary_call_mode="auto")
    await _run_one_summarization(context)

    stats = context.last_summary_call_stats
    assert stats["mode_requested"] == "auto"
    assert stats["span_measure"]["min_span_ratio"] == DEFAULT_FORK_MIN_SPAN_RATIO
    expected = "fork" if ratio >= DEFAULT_FORK_MIN_SPAN_RATIO else "standalone"
    assert stats["mode_used"] == expected


@pytest.mark.asyncio
async def test_the_measurement_is_reported_whichever_way_it_goes():
    """An eval arm has to be able to plot the realized span:prefix
    distribution and re-derive its own threshold, including from the calls
    the predicate let through."""
    for ratio, expected in ((99.0, "standalone"), (0.000_001, "fork")):
        context = _summary_manager(
            summary_call_mode="fork", summary_fork_min_span_ratio=ratio
        )
        await _run_one_summarization(context)
        measure = context.last_summary_call_stats["span_measure"]
        assert context.last_summary_call_stats["mode_used"] == expected
        assert measure["span_tokens"] > 0
        assert measure["prefix_tokens"] > 0
        assert measure["min_span_ratio"] == ratio
        assert measure["span_ratio"] == pytest.approx(
            measure["span_tokens"] / measure["prefix_tokens"]
        )


def test_the_default_classifies_6das_own_measured_populations_correctly():
    """The sharpest available check on the CONSTANT itself: replay the two
    populations 6da actually measured through the shipped default and
    require the verdict that matched the money.

      • the median pair (span 5,129 tok inside a 26,620 tok fork prompt,
        ratio 0.193) is where the two arms cancelled -- it must DECLINE
      • the tail (spans >30k, mean implied ~40k; standalone cost $0.1410
        against a flat ~$0.027 forked) must FORK

    A default that got either of these backwards would be shipping the
    opposite of the finding.
    """
    context = _summary_manager(summary_call_mode="auto")
    threshold = context._effective_fork_min_span_ratio()

    median_ratio = 5_129 / 26_620
    assert (
        context._fork_span_declined_reason(5_129, 26_620, median_ratio, threshold)
        is not None
    ), "6da's median pair is BELOW break-even and must decline"

    tail_ratio = 40_000 / 60_000
    assert (
        context._fork_span_declined_reason(40_000, 60_000, tail_ratio, threshold) is None
    ), "6da's >30k tail is where the fork wins ~5x and must fork"


def test_a_zero_token_prefix_does_not_divide_by_zero():
    """Degenerate input the ladder can hand us. No opinion is the right
    answer -- a zero-token prefix has no cache worth protecting."""
    context = _summary_manager(summary_call_mode="auto")
    span_tokens, prefix_tokens, ratio = context._fork_span_measure(
        [{"role": "user", "content": "hi"}], []
    )
    assert prefix_tokens == 0
    assert ratio is None
    assert (
        context._fork_span_declined_reason(span_tokens, prefix_tokens, None, 0.22) is None
    )


def test_a_ratio_above_one_is_legal_and_can_never_be_met():
    """The span is a SUBSET of the prefix, so >1.0 is a deliberate "arm the
    plumbing, never fork" setting -- not an error to be clamped away."""
    context = _summary_manager(summary_call_mode="fork", summary_fork_min_span_ratio=2.0)
    assert context._effective_fork_min_span_ratio() == 2.0
    assert context._fork_span_declined_reason(500, 1000, 0.5, 2.0) is not None


# ---------------------------------------------------------------------------
# Group C -- a decline is not a fallback, and alignment is checked first
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_decline_never_moves_the_fallback_counter():
    """`fork_fallbacks` is the signal an eval arm reads to detect SILENT
    UNFORKING. A working predicate incrementing it would make the feature
    indistinguishable from the defect."""
    context = _summary_manager(summary_call_mode="fork", summary_fork_min_span_ratio=99.0)
    await _run_one_summarization(context)
    assert context._summary_fork_declines == 1
    assert context._summary_fork_fallbacks == 0


@pytest.mark.asyncio
async def test_a_misalignment_is_still_a_fallback_even_with_the_predicate_on():
    """The mirror image, and the ordering proof: no span size may buy a
    misaligned fork. `note_request_sent()` is never called here, so the
    fork is impossible for a reason that has nothing to do with cost -- and
    that reason, not the predicate, must be the one reported."""
    context = _summary_manager(
        summary_call_mode="fork", summary_fork_min_span_ratio=0.000_001
    )
    await _fill(context)
    _cross_trigger(context)
    await context.get_messages_for_request(provider=_FakeProvider())
    await _await_pending_task(context)

    stats = context.last_summary_call_stats
    assert stats["mode_used"] == "standalone"
    assert "note_request_sent() has never been called" in stats["reason"]
    assert stats["fork_fallbacks"] == 1
    assert stats["fork_declines"] == 0
    assert stats["span_measure"] is None, (
        "the predicate must not even measure a fork that could not have "
        "happened -- otherwise the recorded distribution is contaminated "
        "with calls the threshold never governed"
    )


@pytest.mark.asyncio
async def test_a_decline_speaks_at_info_and_never_at_warning(caplog):
    """A warning means something is wrong. The predicate declining is the
    feature working, and must not train anyone to ignore fork warnings."""
    context = _summary_manager(summary_call_mode="fork", summary_fork_min_span_ratio=99.0)
    with caplog.at_level(logging.DEBUG, logger="amplifier_module_context_simple"):
        await _run_one_summarization(context)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"unexpected warning(s): {[r.message for r in warnings]}"
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "DECLINED" in r.message]
    assert len(infos) == 1


@pytest.mark.asyncio
async def test_repeated_declines_announce_once_but_still_count():
    """Every decline message carries its own token counts, so deduping on
    the message would dedupe nothing and emit a line per summarization."""
    context = _summary_manager(summary_call_mode="fork", summary_fork_min_span_ratio=99.0)
    await _run_one_summarization(context)
    with caplog_at_info() as records:
        # Drive two more declines directly -- distinct messages, same kind.
        context._note_fork_declined("the span is 10 tok against a 900 tok prefix")
        context._note_fork_declined("the span is 11 tok against a 950 tok prefix")
    assert context._summary_fork_declines == 3, "every decline is counted"
    assert not [r for r in records if "DECLINED" in r.message], "announced only once"


class caplog_at_info:
    """Minimal record collector -- pytest's caplog fixture cannot be
    re-entered inside a test that already used it in a helper."""

    def __enter__(self):
        self.records: list[logging.LogRecord] = []
        self.handler = logging.Handler()
        self.handler.emit = lambda record: self.records.append(record)
        self.logger = logging.getLogger("amplifier_module_context_simple")
        self.previous = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)
        return self.records

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.previous)
        return False


# ---------------------------------------------------------------------------
# Group D -- nothing else moves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_declined_fork_sends_the_standalone_request_byte_for_byte():
    """The whole safety argument: a decline is not a third request shape.
    Asserted against a control manager that was never in fork mode at all."""
    declined = _summary_manager(summary_call_mode="fork", summary_fork_min_span_ratio=99.0)
    control = _summary_manager()

    declined_provider = await _run_one_summarization(declined)

    await _fill(control)
    await _arm_below_trigger(control)
    _cross_trigger(control)
    control_provider = _FakeProvider()
    await control.get_messages_for_request(provider=control_provider)
    await _await_pending_task(control)

    assert _digest(declined_provider.calls[0].messages) == _digest(
        control_provider.calls[0].messages
    )
    assert declined_provider.calls[0].model == control_provider.calls[0].model is None
    assert declined_provider.calls[0].tools == control_provider.calls[0].tools is None


@pytest.mark.asyncio
async def test_a_declined_fork_still_produces_the_summary():
    """Declining must cost the summary nothing. A predicate that saves money
    by losing summaries is not a cost win."""
    context = _summary_manager(summary_call_mode="fork", summary_fork_min_span_ratio=99.0)
    await _run_one_summarization(context)
    assert context._pending_summary is not None
    assert context._summarization_failures == 0


@pytest.mark.asyncio
async def test_the_predicate_consumes_no_seq_and_does_not_touch_history():
    context = _summary_manager(summary_call_mode="auto")
    await _fill(context)
    context.note_request_sent(tools=_tools(), model="pinned-model")
    await _arm_below_trigger(context)
    before = json.dumps(_strip_timestamps(context.messages), default=str)
    seq_before = context._next_seq

    _cross_trigger(context)
    await context.get_messages_for_request(provider=_FakeProvider())
    await _await_pending_task(context)

    # The summarizer call has completed; the swap-in happens on the NEXT
    # served view. So at this instant nothing may have moved at all.
    assert context._next_seq == seq_before, "the predicate must consume no _seq"
    assert json.dumps(_strip_timestamps(context.messages), default=str) == before


@pytest.mark.asyncio
async def test_the_predicate_does_not_change_which_span_is_selected():
    """The predicate changes how the summarizer is CALLED, never what is
    selected -- including the tool-pair boundary snapping."""
    gated = _summary_manager(summary_call_mode="fork", summary_fork_min_span_ratio=99.0)
    control = _summary_manager()
    for ctx in (gated, control):
        for i in range(12):
            await ctx.add_message({"role": "user", "content": f"do thing {i} " + "x" * 30})
            await ctx.add_message(
                {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": [{"id": f"call-{i}", "tool": "bash", "arguments": {}}],
                }
            )
            await ctx.add_message(
                {"role": "tool", "tool_call_id": f"call-{i}", "content": "out " + "y" * 30}
            )

    gated.note_request_sent(tools=_tools(), model="pinned-model")
    await _arm_below_trigger(gated)
    await _arm_below_trigger(control)
    _cross_trigger(gated)
    _cross_trigger(control)

    for ctx in (gated, control):
        await ctx.get_messages_for_request(provider=_FakeProvider())
        await _await_pending_task(ctx)

    assert gated.last_summary_call_stats["mode_used"] == "standalone", "predicate declined"
    assert sorted(gated._pending_summary["seqs"]) == sorted(control._pending_summary["seqs"])


@pytest.mark.asyncio
async def test_a_declined_fork_leaves_the_next_served_view_identical():
    gated = _summary_manager(summary_call_mode="fork", summary_fork_min_span_ratio=99.0)
    control = _summary_manager()

    await _run_one_summarization(gated)
    await _fill(control)
    await _arm_below_trigger(control)
    _cross_trigger(control)
    await control.get_messages_for_request(provider=_FakeProvider())
    await _await_pending_task(control)

    gated_view = await gated.get_messages_for_request(provider=_FakeProvider())
    control_view = await control.get_messages_for_request(provider=_FakeProvider())
    assert _digest(_strip_timestamps(gated_view)) == _digest(_strip_timestamps(control_view))
