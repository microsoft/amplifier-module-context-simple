"""Required request content must survive actual compaction, not just storage."""

import copy

import pytest
from amplifier_core.llm_errors import ContextLengthError

from amplifier_module_context_simple import SimpleContextManager, mount


def reminder(content):
    return {
        "role": "user",
        "content": content,
        "metadata": {"ephemeral": True, "persisted": True},
    }


def human(content):
    return {
        "role": "user",
        "content": content,
        "metadata": {"source": "human"},
    }


def public_envelope(message):
    """The request view strips only the manager's internal sequence number."""
    return {
        **message,
        "metadata": {
            key: value
            for key, value in message["metadata"].items()
            if key != "_seq"
        },
    }


async def pressured_context():
    context = SimpleContextManager(max_tokens=1800, compaction_notice_enabled=False)
    await context.add_message({"role": "user", "content": "Original task " + "a" * 150})
    body = "<system-reminders>" + "policy " * 180 + " REQUIRED_FACT</system-reminders>"
    await context.add_message(reminder(body))
    for i in range(40):
        await context.add_message(
            {"role": "assistant", "content": f"Output {i} " + "x" * 1200}
        )
    await context.add_message(
        {"role": "user", "content": "Current correction " + "b" * 150}
    )
    return context, body


@pytest.mark.asyncio
async def test_retention_restores_a_sticky_stub_without_changing_history():
    context, body = await pressured_context()
    canonical = copy.deepcopy(await context.get_messages())
    ordinary = await context.get_messages_for_request()
    assert not any(m["content"] == body for m in ordinary)

    retained = await context.get_messages_for_request_retaining(retain_contents=[body])
    assert sum(m["content"] == body for m in retained) == 1
    assert context._estimate_tokens(retained) <= context.max_tokens
    assert await context.get_messages() == canonical
    assert (
        await context.get_messages_for_request_retaining(retain_contents=[body])
        == retained
    )

    # Retention is per request, not a permanent pin that outlives its producer.
    released = await context.get_messages_for_request_retaining(retain_contents=[])
    assert not any(m["content"] == body for m in released)


@pytest.mark.asyncio
async def test_machine_messages_cannot_take_human_boundary_protection():
    context = SimpleContextManager(max_tokens=1400, compaction_notice_enabled=False)
    first = "Original human request " + "details " * 60 + "ORIGINAL_FACT"
    last = "Latest human correction " + "details " * 60 + "CURRENT_FACT"
    await context.add_message(reminder("Earlier machine state " + "m" * 1200))
    await context.add_message({"role": "user", "content": first})
    for i in range(35):
        await context.add_message(
            {"role": "assistant", "content": f"Work {i} " + "x" * 1000}
        )
    await context.add_message({"role": "user", "content": last})
    await context.add_message(reminder("Later machine state " + "m" * 1200))
    view = await context.get_messages_for_request()
    assert any(m["content"] == first for m in view)
    assert any(m["content"] == last for m in view)
    assert await context.get_messages_for_request() == view


@pytest.mark.asyncio
async def test_only_newest_matching_copy_is_retained():
    context, body = await pressured_context()
    await context.add_message(reminder(body))
    view = await context.get_messages_for_request_retaining(retain_contents=[body])
    assert sum(m["content"] == body for m in view) == 1
    assert sum(m["content"] == body for m in await context.get_messages()) == 2


@pytest.mark.asyncio
async def test_retention_survives_transcript_round_trip():
    context, body = await pressured_context()
    transcript = await context.get_messages()
    resumed = SimpleContextManager(max_tokens=1800, compaction_notice_enabled=False)
    await resumed.set_messages(transcript)
    view = await resumed.get_messages_for_request_retaining(retain_contents=[body])
    assert any(m["content"] == body for m in view)


@pytest.mark.asyncio
async def test_missing_content_fails_without_leaking_a_retention_requirement():
    context, body = await pressured_context()
    with pytest.raises(ValueError, match="not in admitted history"):
        await context.get_messages_for_request_retaining(
            retain_contents=["not admitted"]
        )
    assert not any(
        m["content"] == body for m in await context.get_messages_for_request()
    )


@pytest.mark.asyncio
async def test_impossible_retention_fails_before_returning_an_overfull_view():
    context, body = await pressured_context()
    huge = "<system-reminders>" + "x" * 12000 + "</system-reminders>"
    await context.add_message(reminder(huge))
    with pytest.raises(ContextLengthError, match="Required content was not discarded"):
        await context.get_messages_for_request_retaining(retain_contents=[huge])
    assert any(m["content"] == huge for m in await context.get_messages())
    assert not context._stubbed_seqs and not context._removed_seqs
    # Let the withdrawn content age out of the ordinary recent-message window.
    for i in range(30):
        await context.add_message(
            {"role": "assistant", "content": f"Later work {i} " + "x" * 200}
        )
    # A later request with a feasible requirement recovers normally.
    view = await context.get_messages_for_request_retaining(retain_contents=[body])
    assert any(m["content"] == body for m in view)
    assert context._estimate_tokens(view) <= context.max_tokens


@pytest.mark.asyncio
async def test_post_compaction_failure_rolls_back_request_state_and_decisions():
    context = SimpleContextManager(max_tokens=1000, compaction_notice_enabled=False)
    required = "<system-reminders>Active retention requirement.</system-reminders>"
    await context.add_message(human("First human request."))
    await context.add_message(reminder(required))
    await context.add_message(
        {
            "role": "assistant",
            "content": "Unremovable loaded tool state: " + "x" * 5000,
            "metadata": {"openai:tool_search_items": ["read_file"]},
        }
    )
    await context.add_message(human("Latest human correction."))

    with pytest.raises(ContextLengthError, match="Required content was not discarded"):
        await context.get_messages_for_request_retaining(retain_contents=[required])

    assert context._last_compaction_stats is None
    assert context._last_token_meter_stats is None
    assert not context._removed_seqs
    assert not context._truncated_seqs
    assert not context._stubbed_seqs
    assert context._sticky_level == 0


@pytest.mark.asyncio
async def test_mount_advertises_additive_capability_and_keeps_old_getter():
    class Coordinator:
        def __init__(self):
            self.capabilities = {}

        async def mount(self, name, module):
            self.context = module

        def register_capability(self, name, capability):
            self.capabilities[name] = capability

    coordinator = Coordinator()
    cleanup = await mount(coordinator)
    assert callable(coordinator.capabilities["context.request_retention"])
    assert await coordinator.context.get_messages_for_request() == []
    assert (
        await coordinator.capabilities["context.request_retention"](retain_contents=[])
        == []
    )
    await cleanup()


@pytest.mark.asyncio
async def test_stale_actual_meter_cannot_prevent_feasible_retention_compaction():
    context, body = await pressured_context()
    context.token_meter = "actual"
    context._last_measured_prompt_tokens = 100
    view = await context.get_messages_for_request_retaining(retain_contents=[body])
    assert any(m["content"] == body for m in view)
    assert context._estimate_tokens(view) <= context.max_tokens


@pytest.mark.asyncio
async def test_changed_then_withdrawn_requirements_do_not_leave_a_stale_pin():
    context = SimpleContextManager(max_tokens=1800, compaction_notice_enabled=False)
    original = "<system-reminders>" + "original policy " * 180 + "</system-reminders>"
    replacement = (
        "<system-reminders>" + "replacement policy " * 180 + "</system-reminders>"
    )
    await context.add_message(human("First human request: preserve current requirements."))
    await context.add_message(reminder(original))
    for i in range(40):
        await context.add_message(
            {"role": "assistant", "content": f"Earlier work {i}: " + "x" * 1200}
        )
    await context.add_message(human("Latest human correction: use the replacement."))
    await context.add_message(reminder(replacement))
    for i in range(30):
        await context.add_message(
            {"role": "assistant", "content": f"Later work {i}: " + "z" * 500}
        )

    changed_view = await context.get_messages_for_request_retaining(
        retain_contents=[replacement]
    )
    assert sum(m["content"] == original for m in changed_view) == 0
    assert sum(m["content"] == replacement for m in changed_view) == 1

    # A new, empty requirement set must permit the formerly selected envelope
    # to age out on a real subsequent compaction.
    for i in range(30):
        await context.add_message(
            {"role": "assistant", "content": f"Even later work {i}: " + "z" * 500}
        )
    withdrawn_view = await context.get_messages_for_request_retaining(
        retain_contents=[]
    )
    assert not any(m["content"] == original for m in withdrawn_view)
    assert not any(m["content"] == replacement for m in withdrawn_view)


@pytest.mark.asyncio
async def test_level_5_retains_complete_current_reminder_and_human_boundaries():
    """A real Level 5 pass keeps the selected envelope and both human bodies."""
    context = SimpleContextManager(
        max_tokens=1800,
        compact_threshold=0.92,
        target_usage=0.50,
        protected_recent=0.30,
        compaction_notice_enabled=False,
    )
    first = human("First human request: preserve the complete acceptance criteria.")
    active_reminder = reminder(
        "<system-reminders>"
        "Keep the current deployment region unchanged. "
        "Do not use an unpublished credential. "
        "Return the complete migration plan."
        "</system-reminders>"
    )
    latest = human("Latest human correction: the migration must remain reversible.")
    await context.add_message(first)
    await context.add_message(active_reminder)
    for i in range(14):
        await context.add_message(
            {"role": "assistant", "content": f"Machine output {i}: " + "x" * 800}
        )
    await context.add_message(latest)

    canonical = copy.deepcopy(await context.get_messages())
    first_envelope, reminder_envelope, *_, latest_envelope = map(
        public_envelope, canonical
    )
    view = await context.get_messages_for_request_retaining(
        retain_contents=[active_reminder["content"]]
    )

    assert context._last_compaction_stats is not None
    assert context._last_compaction_stats["strategy_level"] == 5
    assert first_envelope in view
    assert reminder_envelope in view
    assert latest_envelope in view
    assert await context.get_messages() == canonical


@pytest.mark.asyncio
async def test_level_8_keeps_quoted_xml_human_prompt_and_reminder_complete():
    """A quoted system-reminder is human content, not injected provenance."""
    context = SimpleContextManager(
        max_tokens=1800,
        compact_threshold=0.92,
        target_usage=0.50,
        protected_recent=0.30,
        compaction_notice_enabled=False,
    )
    first = human(
        "<system-reminders>"
        "A human quoted this XML while asking whether it was safe to publish."
        "</system-reminders>"
    )
    active_reminder = reminder(
        "<system-reminders>"
        + "Active retention policy. " * 130
        + "</system-reminders>"
    )
    latest = human("Latest human correction: do not remove the quoted evidence.")
    await context.add_message(first)
    await context.add_message(active_reminder)
    for i in range(8):
        await context.add_message(
            {"role": "assistant", "content": f"Machine output {i}: " + "x" * 800}
        )
    await context.add_message(latest)

    canonical = await context.get_messages()
    first_envelope, reminder_envelope, *_, latest_envelope = map(
        public_envelope, canonical
    )
    view = await context.get_messages_for_request_retaining(
        retain_contents=[active_reminder["content"]]
    )

    assert context._last_compaction_stats is not None
    assert context._last_compaction_stats["strategy_level"] == 8
    assert first_envelope in view
    assert reminder_envelope in view
    assert latest_envelope in view
    assert context._last_compaction_stats["after_tokens"] > context._last_compaction_stats[
        "target_tokens"
    ]