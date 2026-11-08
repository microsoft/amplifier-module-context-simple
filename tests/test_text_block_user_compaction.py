"""Text-only block prompts must compact without flattening canonical history."""
import copy

import pytest

from amplifier_module_context_simple import SimpleContextManager


def text_blocks(text):
    split = len(text) // 2
    return [{"type": "text", "text": text[:split]}, {"type": "text", "text": text[split:]}]


@pytest.mark.asyncio
async def test_long_text_block_history_fits_and_preserves_human_boundaries():
    context = SimpleContextManager(compaction_notice_enabled=False)
    history = [
        {"role": "user", "content": text_blocks(f"Request {i}: " + "x" * 12000)}
        for i in range(20)
    ]
    await context.set_messages(copy.deepcopy(history))
    canonical = copy.deepcopy(await context.get_messages())
    view = await context.get_messages_for_request(token_budget=20000)
    assert context._estimate_tokens(view) <= 20000
    assert context._last_compaction_stats["user_messages_stubbed"] > 0
    assert view[0]["content"] == history[0]["content"]
    assert view[-1]["content"] == history[-1]["content"]
    assert all(isinstance(m["content"], list) for m in view)
    assert await context.get_messages() == canonical


def test_stub_preserves_block_representation_and_message_metadata():
    context = SimpleContextManager()
    message = {"role": "user", "content": text_blocks("An old request\n" * 100),
               "metadata": {"_seq": 7, "source_id": "original"}}
    before = copy.deepcopy(message)
    result = context._stub_user_message(message)
    assert result["_stubbed"] is True
    assert result["metadata"] == message["metadata"]
    assert result["role"] == "user"
    assert result["content"][0]["type"] == "text"
    assert "User message compacted" in result["content"][0]["text"]
    assert result["_original_length"] == len("An old request\n" * 100)
    assert message == before


@pytest.mark.parametrize("content", [
    [{"type": "text", "text": "x" * 1000}, {"type": "image", "data": "opaque"}],
    [{"type": "text", "text": "x" * 1000, "annotations": {"retain": True}}],
    [{"type": "resource", "text": "x" * 1000}],
    [{"type": "text", "text": 123}],
    [{"type": "text", "text": "short"}],
    [],
])
def test_unknown_multimodal_and_short_content_is_not_stubbed(content):
    context = SimpleContextManager()
    message = {"role": "user", "content": content}
    assert context._stub_user_message(message) is message


@pytest.mark.asyncio
async def test_sticky_block_stubs_do_not_reinflate_or_mutate_transcript():
    context = SimpleContextManager(compaction_notice_enabled=False)
    await context.set_messages([
        {"role": "user", "content": text_blocks(str(i) + "x" * 12000), "metadata": {"source_id": str(i)}}
        for i in range(20)
    ])
    first = await context.get_messages_for_request(token_budget=20000)
    first_stubs = {m["metadata"]["source_id"]: copy.deepcopy(m) for m in first
                   if "User message compacted" in str(m["content"])}
    assert first_stubs
    latest = {"role": "user", "content": text_blocks("fresh" + "y" * 1000)}
    await context.add_message(latest)
    canonical = copy.deepcopy(await context.get_messages())
    second = await context.get_messages_for_request(token_budget=20000)
    by_seq = {m.get("metadata", {}).get("source_id"): m for m in second}
    for seq, stub in first_stubs.items():
        if seq in by_seq:
            assert by_seq[seq] == stub
    assert second[-1]["content"] == latest["content"]
    assert await context.get_messages() == canonical
