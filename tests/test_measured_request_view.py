"""Focused contract tests for Context's provider-count request-view capability."""

import asyncio

import pytest
from amplifier_core.llm_errors import ContextLengthError

from amplifier_module_context_simple import SimpleContextManager, mount


class _Coordinator:
    def __init__(self):
        self.capabilities = {}

    async def mount(self, kind, instance):
        self.context = instance

    def register_capability(self, name, capability):
        self.capabilities[name] = capability


def _decision(count, *, estimated=None, limit=1_000):
    return {
        "estimated_input_tokens": count if estimated is None else estimated,
        "input_limit_tokens": limit,
        "measurement": {
            "kind": "provider_count",
            "source": "test.provider.count",
            "input_tokens": count,
        },
    }


@pytest.mark.asyncio
async def test_measured_capability_is_actual_mode_only_and_foreground_is_always_additive():
    estimate = _Coordinator()
    await mount(estimate, {"token_meter": "estimate"})
    assert "context.measured_request_view" not in estimate.capabilities
    assert callable(estimate.capabilities["context.foreground_usage"])

    actual = _Coordinator()
    await mount(actual, {"token_meter": "actual"})
    assert callable(actual.capabilities["context.measured_request_view"])


@pytest.mark.asyncio
async def test_provider_count_controls_trigger_target_and_returns_exact_final_envelope():
    context = SimpleContextManager(
        max_tokens=1_000,
        compact_threshold=0.8,
        target_usage=0.5,
        protected_tool_results=0,
        compaction_notice_enabled=False,
        token_meter="actual",
    )
    await context.add_message({"role": "user", "content": "first human boundary"})
    for index in range(4):
        await context.add_message(
            {"role": "tool", "tool_call_id": str(index), "content": "x" * 800}
        )
    await context.add_message({"role": "user", "content": "latest human boundary"})

    calls = []

    async def count_view(view):
        calls.append(view)
        # chars/4 remains very different from this native count.  The first
        # native count triggers, and one legal truncation rung reaches target.
        count = 900 if not any(message.get("_truncated") for message in view) else 400
        return {"dispatch": object(), "budget_decision": _decision(count)}

    result = await context.get_measured_request_view(
        provider=None, retain_contents=[], count_view=count_view
    )

    assert result["outcome"] == "target_reached"
    assert result["measured_before"] == 900
    assert result["measured_after"] == 400
    assert result["trigger"] == 800
    assert result["target"] == 500
    assert result["count_calls"] == 2
    assert result["final_attempt"]["dispatch"] is not None
    assert result["base_view"] is calls[-1]
    assert result["transaction"] is not None
    assert context._last_compaction_stats["measurement_source"] == "test.provider.count"
    await result["transaction"].commit()


@pytest.mark.asyncio
async def test_protected_floor_with_no_legal_change_has_one_count_and_no_sticky_event_state():
    context = SimpleContextManager(
        max_tokens=1_000,
        compact_threshold=0.8,
        target_usage=0.5,
        compaction_notice_enabled=False,
        token_meter="actual",
    )
    await context.add_message({"role": "user", "content": "only protected human prompt"})
    calls = 0

    async def count_view(view):
        nonlocal calls
        calls += 1
        return {"dispatch": object(), "budget_decision": _decision(900)}

    result = await context.get_measured_request_view(
        provider=None, retain_contents=[], count_view=count_view
    )

    assert result["outcome"] == "protected_floor"
    assert result["count_calls"] == calls == 1
    assert result["transaction"] is None
    assert context._last_compaction_stats is None


@pytest.mark.asyncio
async def test_unavailable_recount_rolls_back_to_original_counted_view():
    context = SimpleContextManager(
        max_tokens=1_000,
        compact_threshold=0.8,
        target_usage=0.5,
        protected_tool_results=0,
        compaction_notice_enabled=False,
        token_meter="actual",
    )
    await context.add_message({"role": "user", "content": "first"})
    for index in range(4):
        await context.add_message({"role": "tool", "content": "x" * 800, "name": str(index)})
    await context.add_message({"role": "user", "content": "last"})
    views = []

    async def count_view(view):
        views.append(view)
        decision = _decision(900) if len(views) == 1 else None
        return {"dispatch": object(), "budget_decision": decision}

    result = await context.get_measured_request_view(
        provider=None, retain_contents=[], count_view=count_view
    )

    assert result["outcome"] == "measurement_unavailable"
    assert result["base_view"] is views[0]
    assert result["count_calls"] == 2
    assert not context._truncated_seqs
    assert context._last_compaction_stats is None


@pytest.mark.asyncio
async def test_initial_unavailable_uses_one_snapshot_legacy_fallback_and_recounts_it():
    context = SimpleContextManager(
        max_tokens=1_000,
        compact_threshold=0.1,
        protected_tool_results=0,
        compaction_notice_enabled=False,
        token_meter="actual",
    )
    await context.add_message({"role": "user", "content": "first"})
    for _ in range(4):
        await context.add_message({"role": "tool", "content": "x" * 800})
    await context.add_message({"role": "user", "content": "last"})
    factory_calls = 0

    async def factory():
        nonlocal factory_calls
        factory_calls += 1
        return "dynamic system"

    await context.set_system_prompt_factory(factory)
    calls = []

    async def count_view(view):
        calls.append(view)
        return {"dispatch": object(), "budget_decision": None}

    result = await context.get_measured_request_view(
        provider=None, retain_contents=[], count_view=count_view
    )

    assert result["outcome"] == "measurement_unavailable"
    assert result["count_calls"] == len(calls) == 2
    assert factory_calls == 1
    assert result["transaction"] is not None
    result["transaction"].rollback()
    assert not context._truncated_seqs


@pytest.mark.asyncio
async def test_unavailable_recount_of_known_hard_oversize_fails_closed():
    context = SimpleContextManager(
        max_tokens=1_000,
        compact_threshold=0.8,
        protected_tool_results=0,
        compaction_notice_enabled=False,
        token_meter="actual",
    )
    await context.add_message({"role": "user", "content": "first"})
    for _ in range(4):
        await context.add_message({"role": "tool", "content": "x" * 800})
    await context.add_message({"role": "user", "content": "last"})
    calls = 0

    async def count_view(_view):
        nonlocal calls
        calls += 1
        return {
            "dispatch": object(),
            "budget_decision": _decision(900, estimated=1_001, limit=1_000)
            if calls == 1
            else None,
        }

    with pytest.raises(ContextLengthError, match="recount became unavailable"):
        await context.get_measured_request_view(
            provider=None, retain_contents=[], count_view=count_view
        )
    assert not context._truncated_seqs


@pytest.mark.asyncio
async def test_cancellation_from_count_callback_propagates_without_sticky_state():
    context = SimpleContextManager(max_tokens=1_000, token_meter="actual")
    await context.add_message({"role": "user", "content": "prompt"})

    async def cancelled_count(_view):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await context.get_measured_request_view(
            provider=None, retain_contents=[], count_view=cancelled_count
        )
    assert not context._removed_seqs
    assert not context._truncated_seqs


@pytest.mark.asyncio
async def test_cancellation_during_commit_leaves_staged_decisions_rollbackable():
    started = asyncio.Event()
    release = asyncio.Event()

    class Hooks:
        async def emit(self, _event, _data):
            started.set()
            await release.wait()

    context = SimpleContextManager(
        max_tokens=1_000,
        compact_threshold=0.8,
        target_usage=0.5,
        protected_tool_results=0,
        compaction_notice_enabled=False,
        token_meter="actual",
        hooks=Hooks(),
    )
    await context.add_message({"role": "user", "content": "first"})
    for _ in range(4):
        await context.add_message({"role": "tool", "content": "x" * 800})
    await context.add_message({"role": "user", "content": "last"})

    async def count_view(view):
        count = 400 if any(message.get("_truncated") for message in view) else 900
        return {"dispatch": object(), "budget_decision": _decision(count)}

    result = await context.get_measured_request_view(
        provider=None, retain_contents=[], count_view=count_view
    )
    task = asyncio.create_task(result["transaction"].commit())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    result["transaction"].rollback()
    assert not context._truncated_seqs
    assert context._last_compaction_stats is None


@pytest.mark.asyncio
async def test_foreground_claim_blocks_generic_hook_after_owned_recording():
    context = SimpleContextManager(token_meter="actual")
    await context._on_llm_response("llm:response", {"usage": {"input_tokens": 17}})
    recorder = context.claim_foreground_usage()
    assert context._last_measured_prompt_tokens is None
    assert recorder(input_tokens=20, cache_write_tokens=3)
    await context._on_llm_response("llm:response", {"usage": {"input_tokens": 999}})
    assert context._last_measured_prompt_tokens == 23


@pytest.mark.asyncio
async def test_foreground_recorder_rejects_malformed_usage_without_replacing_owned_reading():
    context = SimpleContextManager()
    recorder = context.claim_foreground_usage()
    assert recorder(input_tokens=4)
    assert not recorder(input_tokens=True)
    assert context._last_measured_prompt_tokens == 4
    assert context._foreground_usage_stale is True


@pytest.mark.asyncio
async def test_owned_stale_reading_is_labeled_not_fresh_measured_usage():
    context = SimpleContextManager(token_meter="actual", max_tokens=10_000)
    recorder = context.claim_foreground_usage()
    assert recorder(input_tokens=4)
    assert not recorder(input_tokens=-1)
    await context.add_message({"role": "user", "content": "prompt"})
    await context.get_messages_for_request()
    assert context._last_token_meter_stats["source"] == "owned_stale"
    assert context._last_token_meter_stats["foreground_usage_stale"] is True


@pytest.mark.asyncio
async def test_unowned_generic_hook_keeps_legacy_meter_behavior():
    context = SimpleContextManager()
    await context._on_llm_response("llm:response", {"usage": {"input_tokens": 12}})
    assert context._last_measured_prompt_tokens == 12


@pytest.mark.asyncio
async def test_unowned_hook_rejects_malformed_cache_write_without_crashing():
    context = SimpleContextManager()
    await context._on_llm_response("llm:response", {"usage": {"input_tokens": 12}})
    await context._on_llm_response(
        "llm:response", {"usage": {"input_tokens": 99, "cache_write_tokens": "bad"}}
    )
    assert context._last_measured_prompt_tokens == 12