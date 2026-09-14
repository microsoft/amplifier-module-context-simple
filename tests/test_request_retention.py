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
