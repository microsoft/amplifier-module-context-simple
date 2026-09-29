"""Regression coverage for signed-thinking replay after ephemeral compaction."""

from copy import deepcopy

import pytest

from amplifier_module_context_simple import SimpleContextManager


@pytest.mark.asyncio
async def test_unchanged_replay_preserves_signed_thinking_exactly():
    """Without a rewritten prefix, signed provider history leaves unchanged."""
    context = SimpleContextManager(
        max_tokens=100_000,
        compact_threshold=1.0,
        compaction_notice_enabled=False,
    )
    await context.add_message({"role": "user", "content": "Keep this history intact."})
    await context.add_message(
        {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "private", "signature": "sig"}],
            "thinking": {"text": "private", "signature": "sig"},
            "redacted_thinking": {"data": "opaque"},
            "thinking_block": {"legacy": True},
        }
    )

    first_view = await context.get_messages_for_request()
    second_view = await context.get_messages_for_request()

    assert second_view == first_view
    replayed = second_view[-1]
    assert replayed["thinking"] == {"text": "private", "signature": "sig"}
    assert replayed["redacted_thinking"] == {"data": "opaque"}
    assert replayed["thinking_block"] == {"legacy": True}
    assert replayed["content"] == [
        {"type": "thinking", "thinking": "private", "signature": "sig"}
    ]
    assert context._last_token_meter_stats["signed_thinking_blocks_stripped"] == 0


@pytest.mark.asyncio
async def test_rewritten_prefix_strips_signed_thinking_without_mutating_history():
    """A removed earlier turn invalidates later signatures only in the wire view."""
    context = SimpleContextManager(
        max_tokens=100_000,
        compact_threshold=1.0,
        compaction_notice_enabled=False,
    )
    await context.add_message({"role": "user", "content": "Original task"})
    await context.add_message({"role": "assistant", "content": "old removable prefix"})
    await context.add_message(
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "private", "signature": "sig"},
                {"type": "redacted_thinking", "data": "opaque"},
                {"type": "text", "text": "Visible response"},
            ],
            "thinking": {"text": "private", "signature": "sig"},
            "redacted_thinking": {"data": "opaque"},
            "thinking_block": {"legacy": True},
        }
    )
    canonical = deepcopy(context.messages)

    # This is the sticky state recorded by the removal phase of compaction.
    context._record_removed(context.messages[1])

    view = await context.get_messages_for_request()

    assert context.messages == canonical
    assert all(message.get("content") != "old removable prefix" for message in view)
    replayed = view[-1]
    assert "thinking" not in replayed
    assert "redacted_thinking" not in replayed
    assert "thinking_block" not in replayed
    assert replayed["content"] == [{"type": "text", "text": "Visible response"}]
    assert context._last_signed_thinking_blocks_stripped == 5
    assert context._last_token_meter_stats["signed_thinking_blocks_stripped"] == 5


@pytest.mark.asyncio
async def test_structured_tool_turn_is_retained_atomically_after_prefix_rewrite():
    """Structured tool_use/tool_call blocks pair with structured results atomically."""
    context = SimpleContextManager(
        protected_tool_results=1,
        max_tokens=100_000,
        compact_threshold=1.0,
        compaction_notice_enabled=False,
    )
    await context.add_message({"role": "user", "content": "Original task"})
    await context.add_message({"role": "assistant", "content": "old removable prefix"})
    await context.add_message(
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "private", "signature": "sig"},
                {"type": "tool_use", "id": "use-1", "name": "read_file", "input": {}},
                {"type": "tool_call", "id": "call-1", "name": "list_files", "input": {}},
            ],
            "thinking": {"text": "private", "signature": "sig"},
        }
    )
    await context.add_message(
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "use-1", "content": "read"},
                {"type": "tool_result", "tool_call_id": "call-1", "content": "listed"},
            ],
        }
    )
    await context.add_message({"role": "user", "content": "Continue"})
    canonical = deepcopy(context.messages)

    compacted, removed, stubbed, _ = context._remove_messages_with_protection(
        context.messages,
        target_tokens=1,
        protected_recent=0,
        system_tokens=0,
    )

    assert context.messages == canonical
    assert removed == 1
    assert stubbed == 0
    assert any(message.get("content") == "Continue" for message in compacted)
    retained_assistant = next(
        message
        for message in compacted
        if message.get("role") == "assistant"
        and any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in message.get("content", [])
        )
    )
    retained_result = next(
        message
        for message in compacted
        if any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in message.get("content", [])
        )
    )
    assert retained_assistant["thinking"] == {"text": "private", "signature": "sig"}
    assert {block["tool_use_id"] for block in retained_result["content"] if "tool_use_id" in block} == {
        "use-1"
    }
    assert {
        block["tool_call_id"] for block in retained_result["content"] if "tool_call_id" in block
    } == {"call-1"}

    view = await context.get_messages_for_request()

    replayed_assistant = next(
        message
        for message in view
        if message.get("role") == "assistant"
        and any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in message.get("content", [])
        )
    )
    assert "thinking" not in replayed_assistant
    assert [block["type"] for block in replayed_assistant["content"]] == [
        "tool_use",
        "tool_call",
    ]
    assert any(
        any(
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("tool_use_id") == "use-1"
            for block in message.get("content", [])
        )
        for message in view
    )
    assert context.messages == canonical