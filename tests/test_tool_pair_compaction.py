"""
Tests for tool pair preservation during context compaction.

Verifies that tool_use and tool_result messages are kept as atomic pairs
during compaction, preventing Anthropic API errors.

NOTE: Compaction is triggered via get_messages_for_request() when context
exceeds the compact_threshold. Tests use low max_tokens to force compaction.
"""

import pytest
from amplifier_module_context_simple import SimpleContextManager


@pytest.mark.asyncio
async def test_compact_preserves_tool_pairs_scenario_a():
    """Compaction preserves tool pair when tool_use is in keep window but tool_result is outside.

    Scenario: 11 messages total, compaction keeps last 10
    - Messages 0-8: Regular conversation (9 messages)
    - Message 9: Assistant with tool_calls (IN last 10)
    - Message 10: Tool result (NOT in last 10 without fix)

    Without fix: Keeps message 9, drops message 10 → API error
    With fix: Keeps both 9 and 10 (tool pair preserved)
    """
    # Use low max_tokens to force compaction
    context = SimpleContextManager(max_tokens=100, compact_threshold=0.5)

    # Add 9 regular messages
    for i in range(9):
        await context.add_message({"role": "user", "content": f"message {i}"})

    # Add tool pair at messages 9-10
    await context.add_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_test", "tool": "bash", "arguments": {"cmd": "ls"}}],
        }
    )
    await context.add_message({"role": "tool", "tool_call_id": "toolu_test", "content": "bash output"})

    # Verify we have 11 messages
    assert len(context.messages) == 11

    # Trigger compaction via get_messages_for_request()
    messages = await context.get_messages_for_request()

    # Verify tool pair preserved
    has_tool_use = any(m.get("role") == "assistant" and m.get("tool_calls") for m in messages)
    has_tool_result = any(m.get("role") == "tool" for m in messages)

    assert has_tool_use == has_tool_result, (
        f"Tool pair broken! has_tool_use={has_tool_use}, has_tool_result={has_tool_result}"
    )

    # If tool_use present, verify tool_result immediately follows
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            assert i + 1 < len(messages), f"Tool_use at message {i} but no next message"
            next_msg = messages[i + 1]
            assert next_msg.get("role") == "tool", (
                f"Tool_use at message {i} not followed by tool message (found role={next_msg.get('role')})"
            )


@pytest.mark.asyncio
async def test_compact_preserves_tool_pairs_scenario_b():
    """Compaction preserves tool pair when tool_result is in keep window but tool_use is outside.

    Scenario: 12 messages total, compaction keeps last 10
    - Messages 0-7: Regular conversation (8 messages)
    - Message 8: Assistant with tool_calls (NOT in last 10 without fix)
    - Message 9: Tool result (IN last 10)
    - Messages 10-11: More conversation (2 messages)

    Without fix: Keeps message 9, drops message 8 → API error
    With fix: Keeps both 8 and 9 (tool pair preserved)
    """
    # Use low max_tokens to force compaction
    context = SimpleContextManager(max_tokens=100, compact_threshold=0.5)

    # Add 8 regular messages
    for i in range(8):
        await context.add_message({"role": "user", "content": f"message {i}"})

    # Add tool pair at messages 8-9
    await context.add_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_test2", "tool": "read", "arguments": {"path": "file.txt"}}],
        }
    )
    await context.add_message({"role": "tool", "tool_call_id": "toolu_test2", "content": "file content"})

    # Add 2 more messages
    await context.add_message({"role": "user", "content": "message 10"})
    await context.add_message({"role": "assistant", "content": "response 10"})

    # Verify we have 12 messages
    assert len(context.messages) == 12

    # Trigger compaction via get_messages_for_request()
    messages = await context.get_messages_for_request()

    # Verify tool pair preserved
    tool_use_count = sum(1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))
    tool_result_count = sum(1 for m in messages if m.get("role") == "tool")

    assert tool_use_count == tool_result_count, (
        f"Tool pair count mismatch! tool_use={tool_use_count}, tool_result={tool_result_count}"
    )

    # Verify adjacency
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            assert i + 1 < len(messages), f"Tool_use at message {i} but no next message"
            next_msg = messages[i + 1]
            assert next_msg.get("role") == "tool", f"Tool_use at message {i} not followed by tool message"


@pytest.mark.asyncio
async def test_compact_never_deduplicates_tool_messages():
    """Tool messages are never deduplicated since each has unique tool_call_id.

    With progressive compaction, older tool pairs may be removed entirely (as atomic units).
    This test verifies that tool pairs with identical content are NOT deduplicated -
    if multiple pairs exist, they remain separate (not merged into one).
    """
    # Use high max_tokens so compaction only truncates, doesn't remove
    # This ensures both tool pairs survive and we can verify no deduplication
    context = SimpleContextManager(
        max_tokens=500,  # Higher budget - compaction truncates but doesn't remove
        compact_threshold=0.5,
        protected_recent=0.9,  # Protect 90% of messages
    )

    # Add tool pair twice with same content but different IDs
    await context.add_message({"role": "user", "content": "test"})

    await context.add_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_1", "tool": "bash", "arguments": {"cmd": "ls"}}],
        }
    )
    await context.add_message({"role": "tool", "tool_call_id": "toolu_1", "content": "file1.txt"})

    await context.add_message({"role": "user", "content": "test again"})

    await context.add_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_2", "tool": "bash", "arguments": {"cmd": "ls"}}],
        }
    )
    await context.add_message({"role": "tool", "tool_call_id": "toolu_2", "content": "file1.txt"})

    # Trigger compaction via get_messages_for_request()
    messages = await context.get_messages_for_request()

    # Both tool pairs should be preserved (not deduplicated despite same content)
    tool_result_count = sum(1 for m in messages if m.get("role") == "tool")
    assert tool_result_count == 2, f"Tool messages were deduplicated! Expected 2, got {tool_result_count}"


@pytest.mark.asyncio
async def test_compact_with_multiple_tool_pairs():
    """Multiple tool pairs are all preserved correctly."""
    # Use low max_tokens to force compaction
    context = SimpleContextManager(max_tokens=100, compact_threshold=0.5)

    # Add 3 tool pairs
    for i in range(3):
        await context.add_message({"role": "user", "content": f"request {i}"})
        await context.add_message(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"toolu_{i}", "tool": "bash", "arguments": {}}],
            }
        )
        await context.add_message({"role": "tool", "tool_call_id": f"toolu_{i}", "content": f"result {i}"})

    # Add more messages to push first pair outside window
    for i in range(10):
        await context.add_message({"role": "user", "content": f"later message {i}"})

    # Trigger compaction via get_messages_for_request()
    messages = await context.get_messages_for_request()

    # Verify all remaining tool pairs are complete
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # Must have following tool message
            assert i + 1 < len(messages), f"Tool_use at {i} without following message"
            assert messages[i + 1].get("role") == "tool", f"Tool_use at {i} not followed by tool message"

    # Verify counts match
    tool_use_count = sum(1 for m in messages if m.get("tool_calls"))
    tool_result_count = sum(1 for m in messages if m.get("role") == "tool")
    assert tool_use_count == tool_result_count, "Tool pair counts don't match"


@pytest.mark.asyncio
async def test_compact_with_multiple_tool_calls_in_one_message():
    """Assistant with MULTIPLE tool_calls in one message preserves ALL tool results.

    This tests the critical bug fix: when an assistant makes 6 tool calls in one message,
    all 6 tool result messages must be preserved during compaction.

    Regression test for: "Message 7 has tool_use IDs without matching tool_result blocks"

    With progressive compaction, we use protected_recent to keep the tool pair in the
    protected zone, ensuring the assistant + all 6 tool results are preserved together.
    """
    # Use protected_recent=0.5 to keep the multi-tool-call pair in protected zone
    context = SimpleContextManager(
        max_tokens=200,
        compact_threshold=0.5,
        protected_recent=0.5,  # Protect last 50% to include the tool pair
    )

    # Add conversation to fill context
    for i in range(8):
        await context.add_message({"role": "user", "content": f"request {i}"})
        await context.add_message({"role": "assistant", "content": f"response {i}"})

    # Add assistant with 6 tool calls (like web_search + 5 web_fetch)
    await context.add_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "toolu_1", "tool": "web_search", "arguments": {"query": "test"}},
                {"id": "toolu_2", "tool": "web_fetch", "arguments": {"url": "http://example.com/1"}},
                {"id": "toolu_3", "tool": "web_fetch", "arguments": {"url": "http://example.com/2"}},
                {"id": "toolu_4", "tool": "web_fetch", "arguments": {"url": "http://example.com/3"}},
                {"id": "toolu_5", "tool": "web_fetch", "arguments": {"url": "http://example.com/4"}},
                {"id": "toolu_6", "tool": "web_fetch", "arguments": {"url": "http://example.com/5"}},
            ],
        }
    )

    # Add 6 separate tool result messages (one for each tool call)
    await context.add_message({"role": "tool", "tool_call_id": "toolu_1", "content": "search results"})
    await context.add_message({"role": "tool", "tool_call_id": "toolu_2", "content": "page 1 content"})
    await context.add_message({"role": "tool", "tool_call_id": "toolu_3", "content": "page 2 content"})
    await context.add_message({"role": "tool", "tool_call_id": "toolu_4", "content": "page 3 content"})
    await context.add_message({"role": "tool", "tool_call_id": "toolu_5", "content": "page 4 content"})
    await context.add_message({"role": "tool", "tool_call_id": "toolu_6", "content": "page 5 content"})

    # Add more messages
    await context.add_message({"role": "user", "content": "what did you find?"})

    # Trigger compaction via get_messages_for_request()
    messages = await context.get_messages_for_request()

    # Find the assistant message with 6 tool_calls
    assistant_idx = None
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls") and len(msg["tool_calls"]) == 6:
            assistant_idx = i
            break

    assert assistant_idx is not None, "Assistant message with 6 tool_calls not found after compaction"

    # Verify ALL 6 tool results are preserved
    tool_result_count = 0
    for offset in range(1, 7):
        if assistant_idx + offset < len(messages):
            next_msg = messages[assistant_idx + offset]
            if next_msg.get("role") == "tool":
                tool_result_count += 1

    assert tool_result_count == 6, (
        f"Expected 6 tool results after assistant with 6 tool_calls, "
        f"but found only {tool_result_count}. This is the bug!"
    )


@pytest.mark.asyncio
async def test_incomplete_tool_results_not_removed():
    """Test that assistant with incomplete tool results is NOT removed during compaction.
    
    Regression test for bug where compaction would remove assistant messages
    even when not all tool_results had been added yet, causing orphaned
    tool_results and API errors.
    """
    context = SimpleContextManager(
        max_tokens=1000,
        compact_threshold=0.01,  # Force compaction
        target_usage=0.5,
    )
    
    # Scenario: Assistant makes 2 tool calls, but only 1 result added so far
    await context.add_message({
        "role": "assistant",
        "content": "Let me check both",
        "tool_calls": [
            {"id": "call_A", "type": "function", "function": {"name": "tool_a", "arguments": "{}"}},
            {"id": "call_B", "type": "function", "function": {"name": "tool_b", "arguments": "{}"}},
        ]
    })
    
    # Only result A added
    await context.add_message({
        "role": "tool",
        "tool_call_id": "call_A",
        "content": "Result from tool A"
    })
    
    # User interrupts (like typing "continue")
    await context.add_message({
        "role": "user",
        "content": "continue"
    })
    
    # Result B hasn't been added yet!
    
    # Force compaction
    compacted = await context._compact_ephemeral(budget=1000, source_messages=context.messages)
    
    # CRITICAL: Assistant message should still be present
    # because call_B is missing its result
    assistant_messages = [m for m in compacted if m.get("role") == "assistant" and m.get("tool_calls")]
    
    assert len(assistant_messages) == 1, "Assistant with incomplete tool results should NOT be removed"
    
    # Verify the assistant message has both tool_calls
    assert len(assistant_messages[0]["tool_calls"]) == 2


@pytest.mark.asyncio  
async def test_complete_tool_results_can_be_removed():
    """Test that assistant with ALL tool results CAN be removed during compaction."""
    context = SimpleContextManager(
        max_tokens=1000,
        compact_threshold=0.01,  # Force compaction
        target_usage=0.5,
    )
    
    # Scenario: Assistant makes 2 tool calls, BOTH results added
    await context.add_message({
        "role": "assistant",
        "content": "Let me check both",
        "tool_calls": [
            {"id": "call_A", "type": "function", "function": {"name": "tool_a", "arguments": "{}"}},
            {"id": "call_B", "type": "function", "function": {"name": "tool_b", "arguments": "{}"}},
        ]
    })
    
    # Both results added
    await context.add_message({
        "role": "tool",
        "tool_call_id": "call_A",
        "content": "Result A"
    })
    await context.add_message({
        "role": "tool",
        "tool_call_id": "call_B",
        "content": "Result B"
    })
    
    # Add more messages to trigger compaction
    for i in range(20):
        await context.add_message({"role": "user", "content": f"Message {i}" * 100})
        await context.add_message({"role": "assistant", "content": f"Response {i}" * 100})
    
    # Force compaction - should be able to remove the old tool pair
    compacted = await context._compact_ephemeral(budget=1000, source_messages=context.messages)
    
    # The old assistant with tool_calls might be removed (that's OK now)
    # We just verify no orphaned tool_results exist
    tool_use_ids = set()
    for msg in compacted:
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id") or tc.get("tool_call_id")
                if tc_id:
                    tool_use_ids.add(tc_id)
    
    # Check all tool results have matching tool_uses
    for msg in compacted:
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id")
            assert tc_id in tool_use_ids, f"Orphaned tool_result with tool_call_id: {tc_id}"
