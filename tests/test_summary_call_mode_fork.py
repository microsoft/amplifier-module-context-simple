"""Adversarial tests for `summary_call_mode: "fork"`.

STEP 0 finding this file exists to lock in: before this change the
summarizer sent a STANDALONE two-message request -- its own ~955-char
`role: "system"` prompt plus a freshly formatted plain-text rendering of
the span -- sharing not one byte of prefix with the main conversation.
Every token of the span was billed as fresh input while the provider was
already holding that exact span warm for the main line.

`summary_call_mode: "fork"` re-issues the same ask as a PURE APPEND onto
the prefix the main line already sent. Pure append is the one mutation
measured as a cache HIT under the grow-only rule (probe P4: identical
repeat 9,789 HIT, pure append 9,789 HIT, strict truncation 0 MISS,
middle-drop 0 MISS).

The tests below are written against the ways this can silently go wrong,
not against the way it is supposed to work:

  1. THE DEFAULT MUST NOT MOVE. A new branch that is "usually" inert is
     not inert. Group A pins the standalone request byte-for-byte and
     proves the fork bookkeeping is never even written unless armed.
  2. A SILENTLY UNFORKED FORK IS THE REAL FAILURE. A fork that does not
     reproduce the parent's prefix wins nothing AND pays for the whole
     conversation -- strictly worse than what it replaces. Group D proves
     every misalignment refuses, falls back to the standalone call, and
     SAYS SO (warning + counter + reported mode).
  3. THE MAIN LINE IS NOT THE SUMMARIZER'S SCRATCHPAD. Group C proves the
     fork consumes no `_seq`, appends nothing to history, does not move
     the hybrid meter's `_last_sent_estimate` comparand, and leaves the
     next served view byte-identical to an unforked control.
  4. TOOL PAIRS STAY WHOLE. Group E proves the absorb-boundary snapping
     (the donor's exact production failure) is not perturbed by the call
     mode -- the fork changes how the summarizer is CALLED, never what is
     selected.
"""

import asyncio
import hashlib
import json
import logging

import pytest
from amplifier_core import ChatResponse, Message, TextBlock
from amplifier_module_context_simple import SimpleContextManager, mount


class _FakeProvider:
    """Minimal stand-in for a Provider -- records every request it is
    handed so tests can assert on the exact shape that would go on the
    wire. Deliberately has neither `get_model_info` nor `get_info`, so
    `_calculate_budget` falls back to `self.max_tokens`."""

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
    """A tool spec list shaped like what an orchestrator actually sends."""
    return [
        {
            "name": "bash",
            "description": "run a command",
            "parameters": {"type": "object", "properties": {}},
        }
    ]


def _digest(messages) -> str:
    """Canonical hash of a message array, whatever form it arrives in."""
    dumps = []
    for m in messages:
        if isinstance(m, Message):
            dumps.append(m.model_dump())
        else:
            dumps.append(Message(**m).model_dump())
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
    """A manager whose summary trigger is reachable without needing a
    200k-token fixture. `max_tokens` is corrected per test once real
    estimator usage is known (same technique the existing summary tests
    use)."""
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
        await context.add_message(
            {"role": "user", "content": f"user turn {i} " + "x" * 40}
        )
        await context.add_message(
            {"role": "assistant", "content": f"assistant reply {i} " + "y" * 40}
        )


async def _arm_below_trigger(context: SimpleContextManager) -> list[dict]:
    """Serve one request with usage BELOW summary_trigger.

    This is what records the prefix a later fork appends to -- and it is
    also the honest ordering: on the very first request of a session there
    is no sent prefix yet, so there is nothing to fork onto.
    """
    raw = context._estimate_tokens(context.messages)
    context.max_tokens = int(raw / 0.1)  # usage ~0.10, below trigger 0.3
    view = await context.get_messages_for_request(provider=_FakeProvider())
    assert context._is_summarizing is False, "must not trigger below summary_trigger"
    return view


def _cross_trigger(context: SimpleContextManager) -> None:
    raw = context._estimate_tokens(context.messages)
    context.max_tokens = int(raw / 0.5)  # usage ~0.50: above 0.3, below 0.99


# ---------------------------------------------------------------------------
# Group A -- the default must not move
# ---------------------------------------------------------------------------


def test_default_summary_call_mode_is_standalone():
    assert SimpleContextManager().summary_call_mode == "standalone"
    assert (
        SimpleContextManager(compaction_strategy="summary").summary_call_mode
        == "standalone"
    )


def test_inline_is_an_accepted_alias_for_standalone(caplog):
    """The commissioning lane brief named the default mode "inline"; the
    work item named it "standalone". Both must mean today's behavior, and
    neither may produce a warning."""
    with caplog.at_level(logging.WARNING):
        context = SimpleContextManager(summary_call_mode="inline")
    assert context.summary_call_mode == "standalone"
    assert not [r for r in caplog.records if "summary_call_mode" in r.message]


def test_unknown_summary_call_mode_falls_back_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        context = SimpleContextManager(summary_call_mode="bogus")
    assert context.summary_call_mode == "standalone"
    assert any("unknown summary_call_mode" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_mount_threads_summary_call_mode_through():
    coordinator = _Coordinator()
    await mount(coordinator, {"compaction_strategy": "summary", "summary_call_mode": "fork"})
    assert coordinator.mounted["context"].summary_call_mode == "fork"

    coordinator = _Coordinator()
    await mount(coordinator, {"summary_call_mode": "inline"})
    assert coordinator.mounted["context"].summary_call_mode == "standalone"

    coordinator = _Coordinator()
    await mount(coordinator, {"summary_call_mode": "nonsense"})
    assert coordinator.mounted["context"].summary_call_mode == "standalone"


@pytest.mark.asyncio
async def test_default_mode_summarizer_request_is_byte_identical():
    """The standalone request must remain EXACTLY what it was before this
    feature existed: two messages, system prompt then formatted span,
    model from `summarization_model`. Asserted against independently
    rebuilt expected content, not against itself."""
    context = _summary_manager(summarization_model="gpt-test")
    await _fill(context)
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    assert len(provider.calls) == 1
    request = provider.calls[0]
    assert len(request.messages) == 2
    assert request.model == "gpt-test"
    assert request.tools is None

    seqs = sorted(context._pending_summary["seqs"])
    span = [m for m in context.messages if context._extract_seq(m) in set(seqs)]
    assert request.messages[0].role == "system"
    assert request.messages[0].content == context._get_summarization_prompt()
    assert request.messages[1].role == "user"
    assert request.messages[1].content == context._format_messages_for_summarization(span)
    assert context.last_summary_call_stats["mode_used"] == "standalone"
    assert context.last_summary_call_stats["reason"] is None


@pytest.mark.asyncio
async def test_default_mode_never_records_a_fork_prefix():
    """The fork bookkeeping must not merely be unused in the default mode
    -- it must never be WRITTEN. An always-on capture would be a silent
    per-request list allocation on the hot path."""
    for kwargs in ({}, {"compaction_strategy": "summary"}):
        context = SimpleContextManager(**kwargs)
        for i in range(5):
            await context.add_message({"role": "user", "content": f"m{i}"})
            await context.get_messages_for_request(provider=_FakeProvider())
        assert context._last_request_view is None
        assert context._sent_tools_supplied is False
        assert context._summary_fork_fallbacks == 0
        assert context._fork_prefix_source is None
        assert context.last_summary_call_stats is None


@pytest.mark.asyncio
async def test_note_request_sent_is_inert_when_fork_is_not_configured():
    """A caller that always calls the seam must not change behavior for
    every session that has not opted into forking."""
    context = _summary_manager()  # standalone
    await _fill(context)
    context.note_request_sent(tools=_tools(), model="pinned-model")
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    request = provider.calls[0]
    assert len(request.messages) == 2
    assert request.tools is None
    assert request.model is None
    assert context._last_request_view is None
    assert context._summary_fork_fallbacks == 0


@pytest.mark.asyncio
async def test_note_request_sent_never_touches_history():
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context, turns=3)
    before = json.dumps(_strip_timestamps(context.messages), default=str)
    seq_before = context._next_seq

    context.note_request_sent(
        [{"role": "user", "content": "an injected tail the orchestrator added"}],
        tools=_tools(),
        model="m",
    )

    assert json.dumps(_strip_timestamps(context.messages), default=str) == before
    assert context._next_seq == seq_before


# ---------------------------------------------------------------------------
# Group B -- the fork is a pure append onto what the main line actually sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_request_is_the_parent_prefix_plus_exactly_one_user_message():
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    context.note_request_sent(tools=_tools(), model="pinned-model")
    parent_view = await _arm_below_trigger(context)
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    request = provider.calls[0]
    assert context.last_summary_call_stats["mode_used"] == "fork"
    assert len(request.messages) == len(parent_view) + 1, (
        "a fork appends exactly one message -- no extra system message, no "
        "re-sent span"
    )
    # G-FORK-PREFIX, in unit form: the fork minus its trailing message is
    # byte-identical to the parent request.
    assert _digest(request.messages[:-1]) == _digest(parent_view)
    assert request.messages[-1].role == "user"


@pytest.mark.asyncio
async def test_fork_adds_no_system_message():
    """A per-summarization `role: "system"` message would be hoisted into
    the provider's single top-level system block and rewrite the cached
    system prefix -- the exact failure already measured for the summary
    tier and the compaction notice. The prompt must ride the appended user
    message instead."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    context.note_request_sent(tools=_tools())
    parent_view = await _arm_below_trigger(context)
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    request = provider.calls[0]
    parent_systems = sum(1 for m in parent_view if m.get("role") == "system")
    fork_systems = sum(1 for m in request.messages if m.role == "system")
    assert fork_systems == parent_systems
    assert context._get_summarization_prompt() in request.messages[-1].content


@pytest.mark.asyncio
async def test_fork_pins_tools_and_model_from_note_request_sent():
    """Tool specs are serialized ahead of the system block, and a
    summarizer routed to another model reads none of the cache the main
    line wrote. Both must come from what the caller says it sent."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    tools = _tools()
    context.note_request_sent(tools=tools, model="pinned-model")
    await _arm_below_trigger(context)
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    request = provider.calls[0]
    assert request.model == "pinned-model"
    assert request.tools is not None and len(request.tools) == len(tools)
    assert request.tools[0].name == "bash"


@pytest.mark.asyncio
async def test_fork_does_not_resend_the_span():
    """The entire point: the span is already inside the prefix. Re-sending
    it would cost exactly what standalone costs today PLUS the prefix --
    a regression dressed as an optimization."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    context.note_request_sent(tools=_tools())
    await _arm_below_trigger(context)
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    appended = provider.calls[0].messages[-1].content
    seqs = set(context._pending_summary["seqs"])
    span = [m for m in context.messages if context._extract_seq(m) in seqs]
    assert len(span) > 3, "fixture must produce a span worth not re-sending"
    formatted = context._format_messages_for_summarization(span)
    assert formatted not in appended
    # Only the boundary marker is quoted back, and it is bounded.
    assert len(appended) < len(formatted)


@pytest.mark.asyncio
async def test_fork_instruction_carries_the_prompt_and_explicit_scope():
    """A fork can SEE the whole conversation, unlike standalone which can
    only see the span. Scoping therefore has to be stated, and the span's
    end has to be identifiable."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    context.note_request_sent(tools=_tools())
    await _arm_below_trigger(context)
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    appended = provider.calls[0].messages[-1].content
    seqs = set(context._pending_summary["seqs"])
    span = [m for m in context.messages if context._extract_seq(m) in seqs]

    assert context._get_summarization_prompt() in appended
    assert f"the first {len(span)} message(s)" in appended
    assert "Do not summarize anything after it" in appended
    last_text = str(span[-1]["content"])[:60]
    assert last_text in appended, "the span's final message must be identifiable"


@pytest.mark.asyncio
async def test_fork_uses_caller_supplied_messages_verbatim_when_given():
    """An orchestrator may append hook-injected content AFTER this module
    returns its view. An implicit, match-forward-only cache misses on
    anything that is not a strict superset of what it cached, so when the
    caller tells us what actually went on the wire, THAT is what gets
    appended to."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    module_view = await _arm_below_trigger(context)
    wire = [*module_view, {"role": "user", "content": "<system-reminder>injected</system-reminder>"}]
    context.note_request_sent(wire, tools=_tools())
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    request = provider.calls[0]
    assert len(request.messages) == len(wire) + 1
    assert _digest(request.messages[:-1]) == _digest(wire)
    assert "injected" in request.messages[-2].content


# ---------------------------------------------------------------------------
# Group C -- the main line is not the summarizer's scratchpad
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_consumes_no_seq_and_appends_nothing_to_history():
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    context.note_request_sent(tools=_tools())
    await _arm_below_trigger(context)
    _cross_trigger(context)

    seq_before = context._next_seq
    history_before = json.dumps(_strip_timestamps(context.messages), default=str)
    removed_before = set(context._removed_seqs)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    assert context.last_summary_call_stats["mode_used"] == "fork"
    assert context._next_seq == seq_before, "the fork must not consume a _seq"
    assert (
        json.dumps(_strip_timestamps(context.messages), default=str) == history_before
    ), "the fork must not append to, reorder, or edit history"
    assert context._removed_seqs == removed_before


@pytest.mark.asyncio
async def test_fork_does_not_move_the_hybrid_meter_comparand():
    """`_last_sent_estimate` describes the view the MAIN line sent; it is
    what the hybrid meter's conservatism guard compares the next
    `llm:response` against. Building the fork through `_finalize_view`
    would silently rewrite it with the summarizer's own request."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    context.note_request_sent(tools=_tools())
    await _arm_below_trigger(context)
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    estimate_after_main_request = context._last_sent_estimate
    await _await_pending_task(context)

    assert context.last_summary_call_stats["mode_used"] == "fork"
    assert context._last_sent_estimate == estimate_after_main_request


@pytest.mark.asyncio
async def test_forked_and_unforked_sessions_serve_identical_views():
    """The call mode changes how the summarizer is CALLED. What the main
    line is served must be bit-for-bit the same either way."""
    forked = _summary_manager(summary_call_mode="fork")
    control = _summary_manager()
    for ctx in (forked, control):
        await _fill(ctx)
    forked.note_request_sent(tools=_tools())
    await _arm_below_trigger(forked)
    await _arm_below_trigger(control)
    for ctx in (forked, control):
        _cross_trigger(ctx)

    for ctx in (forked, control):
        await ctx.get_messages_for_request(provider=_FakeProvider("SAME SUMMARY"))
        await _await_pending_task(ctx)

    assert forked.last_summary_call_stats["mode_used"] == "fork"
    assert control.last_summary_call_stats["mode_used"] == "standalone"

    for ctx in (forked, control):
        ctx.compact_threshold = 0.3
    forked_view = await forked.get_messages_for_request(provider=_FakeProvider())
    control_view = await control.get_messages_for_request(provider=_FakeProvider())

    assert _strip_timestamps(forked_view) == _strip_timestamps(control_view)
    assert forked._removed_seqs == control._removed_seqs


# ---------------------------------------------------------------------------
# Group D -- every misalignment refuses, falls back, and says so
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_without_note_request_sent_falls_back_loudly(caplog):
    """Tool specs are part of the cached prefix and this module is never
    handed them. Guessing "probably no tools" is precisely how a fork
    silently misaligns and pays for the whole conversation."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    await _arm_below_trigger(context)
    _cross_trigger(context)

    provider = _FakeProvider()
    with caplog.at_level(logging.WARNING):
        await context.get_messages_for_request(provider=provider)
        await _await_pending_task(context)

    assert len(provider.calls[0].messages) == 2, "must be the standalone request"
    stats = context.last_summary_call_stats
    assert stats["mode_requested"] == "fork"
    assert stats["mode_used"] == "standalone"
    assert "note_request_sent()" in stats["reason"]
    assert context._summary_fork_fallbacks == 1
    assert any("ran STANDALONE instead" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_fork_with_summarization_model_falls_back_loudly(caplog):
    context = _summary_manager(summary_call_mode="fork", summarization_model="other")
    await _fill(context)
    context.note_request_sent(tools=_tools())
    await _arm_below_trigger(context)
    _cross_trigger(context)

    provider = _FakeProvider()
    with caplog.at_level(logging.WARNING):
        await context.get_messages_for_request(provider=provider)
        await _await_pending_task(context)

    assert len(provider.calls[0].messages) == 2
    assert provider.calls[0].model == "other", "the explicit model is still honored"
    assert "different model" in context.last_summary_call_stats["reason"]
    assert any("ran STANDALONE instead" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_fork_on_the_first_request_of_a_session_falls_back():
    """Nothing has been sent yet, so there is no prefix to append to. This
    is normal, not an error -- but it must not pretend to fork."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    context.note_request_sent(tools=_tools())
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    assert len(provider.calls[0].messages) == 2
    assert "no request has been recorded yet" in context.last_summary_call_stats["reason"]


@pytest.mark.asyncio
async def test_fork_refuses_when_the_prefix_ends_on_unanswered_tool_calls():
    """Appending a user message after an assistant turn whose tool results
    have not arrived interleaves between tool_use and tool_result -- the
    same atomicity the compaction notice already guards."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context, turns=10)
    context.note_request_sent(tools=_tools())
    await _arm_below_trigger(context)
    # The recorded prefix now ends on an assistant turn awaiting results.
    context._last_request_view = [
        *context._last_request_view,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "tool": "bash", "arguments": {}}],
        },
    ]
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    assert len(provider.calls[0].messages) == 2
    assert "unanswered" in context.last_summary_call_stats["reason"]


@pytest.mark.asyncio
async def test_a_stale_caller_message_record_refuses_loudly_not_silently(caplog):
    """A caller that wires note_request_sent() ONCE (startup helper, first
    turn only) would have turn N's fork append to turn 1's request.

    This test previously asserted the OPPOSITE resolution -- that the
    module silently substituted its own last returned view. That
    substitution is the defect this file now pins against
    (model_performance-jnt): the module cannot know whether its own view
    was ever sent, and under a real orchestrator that re-fetches the view
    within a single request, the view it substitutes is one that was
    superseded and never went on the wire. Trading an array KNOWN to have
    been sent for one whose fate is unknown is backwards, and it was
    invisible -- `mode_used` said "fork" either way.

    The correct resolution for a record too old to be usable is the one
    every other misalignment already gets: refuse, run standalone, and SAY
    SO. Caught exactly (the span is not in that prefix), not by a proxy.
    """
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context, turns=10)
    # Turn 1: the caller records what it sent.
    await _arm_below_trigger(context)
    context.note_request_sent(
        [{"role": "user", "content": "turn one, long ago"}], tools=_tools()
    )
    # Several more turns go by without the caller telling us anything.
    await _fill(context, turns=10)
    fresh_view = await _arm_below_trigger(context)
    _cross_trigger(context)

    provider = _FakeProvider()
    with caplog.at_level(logging.WARNING):
        await context.get_messages_for_request(provider=provider)
        await _await_pending_task(context)

    stats = context.last_summary_call_stats
    assert stats["mode_used"] == "standalone", (
        "a prefix too stale to contain the span must refuse, not fork onto "
        "a substituted view"
    )
    assert "not present in the recorded prefix" in stats["reason"]
    assert context._summary_fork_fallbacks == 1
    assert any("ran STANDALONE instead" in r.message for r in caplog.records)

    # The substitution specifically must not have happened: the standalone
    # request is the two-message one, not an append onto the fresh view.
    request = provider.calls[0]
    assert len(request.messages) == 2
    assert _digest(request.messages) != _digest(fresh_view)
    assert not any("long ago" in str(m.content) for m in request.messages)
    # And the summary still happened -- refusing costs today's price, never
    # the summary itself.
    assert context._pending_summary is not None


@pytest.mark.asyncio
async def test_fork_refuses_when_the_span_is_absent_from_the_prefix():
    """A fork does not re-send the span. If the recorded prefix no longer
    contains it, the model would be asked to summarize text it cannot
    see."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    context.note_request_sent(tools=_tools())
    await _arm_below_trigger(context)
    # A prefix that is real but unrelated to the span being absorbed.
    context.note_request_sent(
        [{"role": "user", "content": "an unrelated conversation"}], tools=_tools()
    )
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    assert len(provider.calls[0].messages) == 2
    assert "not present in the recorded prefix" in (
        context.last_summary_call_stats["reason"]
    )


@pytest.mark.asyncio
async def test_repeated_fallbacks_warn_once_per_reason_but_count_every_time(caplog):
    """A session that can never fork should cost a handful of log lines,
    not one per summarization -- while the counter still tells the truth."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context, turns=6)

    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            context._note_fork_fallback("reason one")
        context._note_fork_fallback("reason two")

    assert context._summary_fork_fallbacks == 5
    warnings = [r for r in caplog.records if "ran STANDALONE instead" in r.message]
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_a_failed_fork_build_falls_back_instead_of_losing_the_summary():
    """A prefix message this module never created can fail request
    validation. Losing the summary over a cache optimization would be a
    strictly worse outcome than paying today's price."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    await _arm_below_trigger(context)
    context.note_request_sent(
        [{"role": "not-a-real-role", "content": "x"}], tools=_tools()
    )
    # Make the span check pass so the failure comes from request building.
    context._prefix_contains_span = lambda prefix, span: True
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    assert len(provider.calls[0].messages) == 2
    assert "could not be built" in context.last_summary_call_stats["reason"]
    assert context._pending_summary is not None, "the summary still happened"


@pytest.mark.asyncio
async def test_hooks_report_the_mode_actually_used():
    """An eval arm has to be able to count real forks without patching the
    module."""
    events: list[tuple[str, dict]] = []

    class _Hooks:
        async def emit(self, event, data):
            events.append((event, data))

    context = _summary_manager(summary_call_mode="fork")
    context._hooks = _Hooks()
    await _fill(context)
    context.note_request_sent(tools=_tools())
    await _arm_below_trigger(context)
    _cross_trigger(context)

    await context.get_messages_for_request(provider=_FakeProvider())
    await _await_pending_task(context)

    modes = {
        name: data.get("call_mode")
        for name, data in events
        if name in ("context:pre_summarize", "context:post_summarize")
    }
    assert modes == {
        "context:pre_summarize": "fork",
        "context:post_summarize": "fork",
    }


# ---------------------------------------------------------------------------
# Group E -- tool-pair integrity is not perturbed by the call mode
# ---------------------------------------------------------------------------


async def _fill_with_tool_pairs(context: SimpleContextManager) -> None:
    for i in range(12):
        await context.add_message(
            {"role": "user", "content": f"do thing {i} " + "x" * 30}
        )
        await context.add_message(
            {
                "role": "assistant",
                "content": "working",
                "tool_calls": [{"id": f"call-{i}", "tool": "bash", "arguments": {}}],
            }
        )
        await context.add_message(
            {"role": "tool", "tool_call_id": f"call-{i}", "content": "out " + "y" * 30}
        )


@pytest.mark.asyncio
async def test_call_mode_does_not_change_which_span_is_selected():
    forked = _summary_manager(summary_call_mode="fork")
    control = _summary_manager()
    for ctx in (forked, control):
        await _fill_with_tool_pairs(ctx)

    assert forked._select_summary_absorb_seqs(500) == control._select_summary_absorb_seqs(
        500
    )


@pytest.mark.asyncio
async def test_fork_mode_never_serves_an_orphaned_tool_result():
    """The donor's exact production failure (a dropped `function_call`
    whose `function_call_output` survived) must stay impossible in fork
    mode too."""
    context = _summary_manager(summary_call_mode="fork", protected_recent=0.3)
    await _fill_with_tool_pairs(context)
    context.note_request_sent(tools=_tools())
    await _arm_below_trigger(context)
    _cross_trigger(context)

    await context.get_messages_for_request(provider=_FakeProvider())
    await _await_pending_task(context)
    context.compact_threshold = 0.3
    view = await context.get_messages_for_request(provider=_FakeProvider())

    call_ids = {
        tc.get("id")
        for m in view
        for tc in (m.get("tool_calls") or [])
        if isinstance(tc, dict)
    }
    result_ids = {m.get("tool_call_id") for m in view if m.get("role") == "tool"}
    assert result_ids <= call_ids, "a tool result was served without its call"


# ---------------------------------------------------------------------------
# Group F -- reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_clears_fork_alignment_state():
    """After set_messages()/clear() the recorded prefix is a prefix of
    nothing, and the caller's facts describe a request unrelated to this
    history. Keeping either is the stale-alignment bug fork mode exists to
    refuse."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context, turns=4)
    context.note_request_sent(tools=_tools(), model="m")
    await _arm_below_trigger(context)
    assert context._last_request_view is not None

    await context.clear()

    assert context._last_request_view is None
    assert context._sent_messages is None
    assert context._sent_tools is None
    assert context._sent_tools_supplied is False
    assert context._sent_model is None


@pytest.mark.asyncio
async def test_fork_state_survives_nothing_across_set_messages():
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context, turns=4)
    context.note_request_sent(tools=_tools())
    await _arm_below_trigger(context)

    await context.set_messages([{"role": "user", "content": "resumed session"}])

    assert context._last_request_view is None
    assert context._sent_tools_supplied is False


@pytest.mark.asyncio
async def test_fork_snapshot_is_taken_at_trigger_time_not_task_time():
    """The background task must append to the prefix chosen when the
    trigger fired, not to whatever `_last_request_view` happens to hold
    when the event loop gets around to running it."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    context.note_request_sent(tools=_tools())
    parent_view = await _arm_below_trigger(context)
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    # Simulate another request landing while the summarizer is in flight.
    context._last_request_view = [{"role": "user", "content": "a later, different view"}]
    await _await_pending_task(context)

    assert _digest(provider.calls[0].messages[:-1]) == _digest(parent_view)


@pytest.mark.asyncio
async def test_concurrent_forks_are_still_serialized_by_the_in_flight_guard():
    """Nothing about forking may weaken the single-summarization-in-flight
    invariant."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    context.note_request_sent(tools=_tools())
    await _arm_below_trigger(context)
    _cross_trigger(context)

    provider = _FakeProvider()
    await asyncio.gather(
        context.get_messages_for_request(provider=provider),
        context.get_messages_for_request(provider=provider),
        context.get_messages_for_request(provider=provider),
    )
    task = context._summarization_task
    if task is not None:
        await task

    assert len(provider.calls) == 1
    assert context._pending_summary is not None
    assert context.last_summary_call_stats["mode_used"] == "fork"


# ---------------------------------------------------------------------------
# Group F -- the fork prefix is an array that was actually SENT
#
# model_performance-jnt. Measured on the wire (model_performance-6da, 20
# forked calls): 9 appended to a request the provider actually saw, and 11
# appended to an array that was never sent as any request. The module
# reported `mode_used == "fork"` for all 20.
#
# Cause: `_capture_fork_prefix()` preferred the caller's recorded wire array
# only while `_sent_serial == _view_serial`, and substituted
# `_last_request_view` otherwise. A real orchestrator serves the view more
# than once per sent request (amplifier's loop-streaming re-fetches after
# persisting an ephemeral injection), the trigger is evaluated inside every
# one of those calls, and on the second the substituted view is one that was
# superseded by the re-fetch and never went on the wire.
#
# These tests are written against that substitution, not against the happy
# path -- which the existing Group B tests already cover.
# ---------------------------------------------------------------------------


async def _serve_below_trigger(context: SimpleContextManager) -> list[dict]:
    """Serve one more view without re-arming, i.e. a re-fetch within the
    same request. `_arm_below_trigger` is the first view of a request; this
    is the second one, which the orchestrator then supersedes."""
    view = await context.get_messages_for_request(provider=_FakeProvider())
    assert context._is_summarizing is False
    return view


@pytest.mark.asyncio
async def test_a_re_fetched_view_does_not_displace_the_recorded_wire_array():
    """THE REGRESSION. An extra view served since the caller confirmed its
    send is normal, not a reason to stop trusting the send. The wire array
    is the only one with positive evidence of having been on the wire; the
    intervening view has none."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    module_view = await _arm_below_trigger(context)
    wire = [
        *module_view,
        {"role": "user", "content": "<system-reminder>injected</system-reminder>"},
    ]
    context.note_request_sent(wire, tools=_tools())
    sent_at = context._view_serial

    # The orchestrator re-fetches the view within the same request. This
    # view is built, superseded, and never sent.
    superseded = await _serve_below_trigger(context)
    assert context._view_serial > sent_at, "the re-fetch must advance the view serial"

    _cross_trigger(context)
    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    stats = context.last_summary_call_stats
    assert stats["mode_used"] == "fork"
    assert stats["prefix_source"] == "wire_record"
    assert stats["prefix_views_since_send"] >= 1, (
        "the re-fetch must be visible in the stats, not silently acted on"
    )

    request = provider.calls[0]
    assert _digest(request.messages[:-1]) == _digest(wire), (
        "the fork must append to the array the caller said it sent"
    )
    assert _digest(request.messages[:-1]) != _digest(superseded)
    assert "injected" in request.messages[-2].content


@pytest.mark.asyncio
async def test_the_module_view_is_never_substituted_when_a_wire_record_exists():
    """Belt and braces on the same defect, asserted from the other side: a
    `_last_request_view` holding content that was demonstrably never sent
    must not be able to reach the forked request at all."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    module_view = await _arm_below_trigger(context)
    context.note_request_sent(module_view, tools=_tools())
    # A view that was built and discarded -- exactly what a re-fetch leaves
    # behind, and what the old code would have forked onto.
    context._last_request_view = [
        {"role": "user", "content": "a view that was never sent"}
    ]
    context._view_serial += 1

    _cross_trigger(context)
    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    request = provider.calls[0]
    assert context.last_summary_call_stats["mode_used"] == "fork"
    assert context.last_summary_call_stats["prefix_source"] == "wire_record"
    assert not any("never sent" in str(m.content) for m in request.messages)
    assert _digest(request.messages[:-1]) == _digest(module_view)


@pytest.mark.asyncio
async def test_prefix_source_names_the_module_view_path_honestly():
    """When the caller supplies tools but never a message array, the fork
    appends to this module's own view -- whose send this module cannot
    confirm. That is still allowed (it is what an explicit-breakpoint
    provider wants), but it must be REPORTED as what it is, so a
    measurement can separate the two populations without patching."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    context.note_request_sent(tools=_tools())
    parent_view = await _arm_below_trigger(context)
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    stats = context.last_summary_call_stats
    assert stats["mode_used"] == "fork"
    assert stats["prefix_source"] == "module_view"
    assert stats["prefix_views_since_send"] is None, (
        "no message array was ever recorded, so there is no send to count from"
    )
    assert _digest(provider.calls[0].messages[:-1]) == _digest(parent_view)


@pytest.mark.asyncio
async def test_prefix_source_is_none_when_the_call_did_not_fork():
    """A refused fork reports no prefix source. Reporting one would make a
    standalone call look byte-aligned with something."""
    context = _summary_manager(summary_call_mode="fork")
    await _fill(context)
    _cross_trigger(context)

    provider = _FakeProvider()
    await context.get_messages_for_request(provider=provider)
    await _await_pending_task(context)

    stats = context.last_summary_call_stats
    assert stats["mode_used"] == "standalone"
    assert stats["prefix_source"] is None


@pytest.mark.asyncio
async def test_tool_pair_integrity_and_seq_stability_survive_the_re_fetch_path():
    """The re-fetch path must change WHICH array is appended to and nothing
    else: the same span is selected, no `_seq` is consumed, history is
    untouched, and the next served view matches an unforked control."""
    forked = _summary_manager(summary_call_mode="fork")
    control = _summary_manager()
    for ctx in (forked, control):
        await _fill(ctx)

    view = await _arm_below_trigger(forked)
    forked.note_request_sent([*view, {"role": "user", "content": "tail"}], tools=_tools())
    await _serve_below_trigger(forked)  # the superseding re-fetch
    await _arm_below_trigger(control)
    for ctx in (forked, control):
        _cross_trigger(ctx)

    seq_before = forked._next_seq
    history_before = json.dumps(_strip_timestamps(forked.messages), default=str)

    for ctx in (forked, control):
        await ctx.get_messages_for_request(provider=_FakeProvider("SAME SUMMARY"))
        await _await_pending_task(ctx)

    assert forked.last_summary_call_stats["mode_used"] == "fork"
    assert forked.last_summary_call_stats["prefix_source"] == "wire_record"
    assert forked._next_seq == seq_before, "the fork must not consume a _seq"
    assert (
        json.dumps(_strip_timestamps(forked.messages), default=str) == history_before
    ), "the fork must not append to, reorder, or edit history"
    assert set(forked._pending_summary["seqs"]) == set(control._pending_summary["seqs"]), (
        "the call mode must not change WHICH span is absorbed"
    )

    for ctx in (forked, control):
        ctx.compact_threshold = 0.3
    forked_view = await forked.get_messages_for_request(provider=_FakeProvider())
    control_view = await control.get_messages_for_request(provider=_FakeProvider())
    assert _strip_timestamps(forked_view) == _strip_timestamps(control_view)
    assert forked._removed_seqs == control._removed_seqs
