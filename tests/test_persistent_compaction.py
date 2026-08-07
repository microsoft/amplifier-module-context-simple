"""
Tests for the persistent compaction primitives:

- should_compact(): reports on the STORED history (not ephemeral view)
- compact(force=...): COMMITS a compacted history back to self.messages
- usage_report(): snapshot of token usage vs budget
- pre/post compaction events are emitted on the hooks bus

These are distinct from the ephemeral compaction applied during
get_messages_for_request(), which never mutates self.messages.
"""

import pytest
from amplifier_module_context_simple import SimpleContextManager


class _RecordingHooks:
    """Minimal hooks bus that records emitted events."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))


async def _fill(context: SimpleContextManager, pairs: int) -> None:
    for i in range(pairs):
        await context.add_message(
            {"role": "user", "content": f"message {i} with some padding content here"}
        )
        await context.add_message(
            {"role": "assistant", "content": f"response {i} with some padding content here"}
        )


@pytest.mark.asyncio
async def test_should_compact_reflects_stored_history():
    context = SimpleContextManager(max_tokens=1000, compact_threshold=0.9, target_usage=0.5)
    assert await context.should_compact() is False
    await _fill(context, 50)
    assert await context.should_compact() is True


@pytest.mark.asyncio
async def test_compact_persists_and_shrinks_history():
    hooks = _RecordingHooks()
    context = SimpleContextManager(
        max_tokens=1000,
        compact_threshold=0.9,
        target_usage=0.5,
        protected_recent=0.1,
        hooks=hooks,
    )
    await _fill(context, 50)

    before = len(context.messages)
    before_tokens = context._estimate_tokens(context.messages)

    stats = await context.compact()

    assert stats["compacted"] is True
    assert stats["persistent"] is True
    # Persistent: self.messages is actually mutated (unlike ephemeral path).
    assert len(context.messages) < before
    assert context._estimate_tokens(context.messages) < before_tokens
    assert stats["after_tokens"] <= stats["target_tokens"] or stats["strategy_level"] >= 1

    # Pre/post events emitted.
    names = [e[0] for e in hooks.events]
    assert "context:pre_compact" in names
    assert "context:post_compact" in names


@pytest.mark.asyncio
async def test_compact_noop_below_threshold():
    context = SimpleContextManager(max_tokens=100_000, compact_threshold=0.9, target_usage=0.5)
    await _fill(context, 3)
    before = len(context.messages)

    stats = await context.compact()  # not forced, well below threshold

    assert stats["compacted"] is False
    assert stats["reason"] == "below_threshold"
    assert len(context.messages) == before  # untouched


@pytest.mark.asyncio
async def test_force_compact_reports_already_compact_when_under_target():
    context = SimpleContextManager(max_tokens=100_000, compact_threshold=0.9, target_usage=0.5)
    await _fill(context, 3)
    before = len(context.messages)

    stats = await context.compact(force=True)

    # Forced past the threshold gate, but history is already under the target,
    # so nothing is removed.
    assert stats["compacted"] is False
    assert stats["reason"] == "already_compact"
    assert len(context.messages) == before


@pytest.mark.asyncio
async def test_system_messages_preserved_across_persistent_compaction():
    context = SimpleContextManager(
        max_tokens=1000, compact_threshold=0.9, target_usage=0.5, protected_recent=0.1
    )
    await context.add_message(
        {"role": "system", "content": "IDENTITY", "metadata": {"source": "hook"}}
    )
    await _fill(context, 50)

    await context.compact()

    system_msgs = [m for m in context.messages if m.get("role") == "system"]
    assert any(m.get("content") == "IDENTITY" for m in system_msgs), (
        "System/identity messages must survive persistent compaction"
    )


@pytest.mark.asyncio
async def test_usage_report_shape():
    context = SimpleContextManager(max_tokens=1000, compact_threshold=0.9, target_usage=0.5)
    await _fill(context, 10)
    report = context.usage_report()
    assert report["budget"] == 1000
    assert report["threshold_tokens"] == 900
    assert report["target_tokens"] == 500
    assert report["tokens"] > 0
    assert 0.0 <= report["pct"]
    assert report["messages"] == len(context.messages)


def test_tool_truncation_rejects_token_growth():
    context = SimpleContextManager(truncate_chars=374)
    message = {
        "role": "tool",
        "content": "x" * 411,
        "tool_call_id": "call-1",
    }
    messages = [message]
    before_tokens = context._estimate_tokens(messages)
    candidate = context._truncate_tool_result(message)

    assert before_tokens == 117
    assert context._estimate_tokens([candidate]) == 133

    truncated, after_tokens = context._truncate_tool_wave(
        messages,
        indices=[0],
        protected_indices=set(),
        target_tokens=0,
        current_tokens=before_tokens,
    )

    assert truncated == 0
    assert after_tokens == before_tokens
    assert messages == [message]


@pytest.mark.asyncio
async def test_compact_rejects_fewer_messages_when_tokens_grow(monkeypatch):
    hooks = _RecordingHooks()
    context = SimpleContextManager(
        max_tokens=10,
        compact_threshold=0.9,
        target_usage=0.5,
        hooks=hooks,
    )
    context.messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    original = list(context.messages)
    candidate = [{"role": "user", "content": "x" * 200}]
    assert len(candidate) < len(original)
    assert context._estimate_tokens(candidate) > context._estimate_tokens(original)

    async def growing_candidate(_budget, _messages):
        return candidate

    monkeypatch.setattr(context, "_compact_ephemeral", growing_candidate)

    stats = await context.compact(force=True)

    assert stats["compacted"] is False
    assert stats["reason"] == "no_reduction"
    assert stats["after_tokens"] > stats["before_tokens"]
    assert context.messages == original
    assert "context:post_compact" not in [event for event, _ in hooks.events]
