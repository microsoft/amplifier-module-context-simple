"""Tests for optional timestamp injection feature."""

import pytest
from datetime import datetime, UTC

from amplifier_module_context_simple import SimpleContextManager


@pytest.mark.asyncio
async def test_timestamps_enabled_by_default():
    """Verify timestamps ARE added by default (add_timestamps=True)."""
    context = SimpleContextManager()  # No explicit add_timestamps - uses default True

    # Add message without timestamp
    message = {"role": "user", "content": "Hello"}
    await context.add_message(message)

    # Verify timestamp WAS added (default behavior)
    messages = await context.get_messages()
    assert len(messages) == 1
    assert "timestamp" in messages[0]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello"


@pytest.mark.asyncio
async def test_timestamps_can_be_disabled():
    """Verify timestamps can be disabled via add_timestamps=False."""
    context = SimpleContextManager(add_timestamps=False)

    # Add message without timestamp
    message = {"role": "user", "content": "Hello"}
    await context.add_message(message)

    # Verify NO timestamp was added when disabled
    messages = await context.get_messages()
    assert len(messages) == 1
    assert "timestamp" not in messages[0]
    assert messages[0] == {"role": "user", "content": "Hello"}


@pytest.mark.asyncio
async def test_timestamps_enabled():
    """Verify timestamps are added when add_timestamps=True."""
    context = SimpleContextManager(add_timestamps=True)

    # Add message without timestamp
    message = {"role": "user", "content": "Hello"}
    await context.add_message(message)

    # Verify timestamp was added
    messages = await context.get_messages()
    assert len(messages) == 1
    assert "timestamp" in messages[0]

    # Verify timestamp format is ISO 8601 with milliseconds
    timestamp = messages[0]["timestamp"]
    # Should be parseable as ISO 8601
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    assert parsed is not None
    # Should be recent (within last second)
    now = datetime.now(UTC)
    delta = (now - parsed).total_seconds()
    assert delta < 1.0, f"Timestamp {timestamp} is not recent (delta: {delta}s)"


@pytest.mark.asyncio
async def test_timestamps_preserved_when_present():
    """Verify existing timestamps are preserved, not overwritten."""
    context = SimpleContextManager(add_timestamps=True)

    # Add message WITH existing timestamp
    original_timestamp = "2026-02-06T10:00:00.123Z"
    message = {"role": "user", "content": "Hello", "timestamp": original_timestamp}
    await context.add_message(message)

    # Verify original timestamp was preserved
    messages = await context.get_messages()
    assert len(messages) == 1
    assert messages[0]["timestamp"] == original_timestamp


@pytest.mark.asyncio
async def test_timestamps_all_message_types():
    """Verify timestamps work for all message types."""
    context = SimpleContextManager(add_timestamps=True)

    # User message
    await context.add_message({"role": "user", "content": "Question"})

    # Assistant message
    await context.add_message({"role": "assistant", "content": "Answer"})

    # Tool message
    await context.add_message(
        {"role": "tool", "tool_call_id": "1", "content": "Result"}
    )

    # System message
    await context.add_message({"role": "system", "content": "Instructions"})

    messages = await context.get_messages()
    assert len(messages) == 4

    # All should have timestamps
    for msg in messages:
        assert "timestamp" in msg
        # Verify format
        timestamp = msg["timestamp"]
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        assert parsed is not None


@pytest.mark.asyncio
async def test_timestamps_preserved_on_resume():
    """Verify timestamps from set_messages() are preserved."""
    context = SimpleContextManager(add_timestamps=True)

    # Restore messages from a saved session (with existing timestamps)
    saved_messages = [
        {
            "role": "user",
            "content": "Message 1",
            "timestamp": "2026-02-06T10:00:00.123Z",
        },
        {
            "role": "assistant",
            "content": "Response 1",
            "timestamp": "2026-02-06T10:00:05.456Z",
        },
    ]

    await context.set_messages(saved_messages)

    # Verify timestamps were preserved
    messages = await context.get_messages()
    assert len(messages) == 2
    assert messages[0]["timestamp"] == "2026-02-06T10:00:00.123Z"
    assert messages[1]["timestamp"] == "2026-02-06T10:00:05.456Z"

    # Add new message - should get new timestamp
    await context.add_message({"role": "user", "content": "Message 2"})

    messages = await context.get_messages()
    assert len(messages) == 3
    # First two preserved
    assert messages[0]["timestamp"] == "2026-02-06T10:00:00.123Z"
    assert messages[1]["timestamp"] == "2026-02-06T10:00:05.456Z"
    # Third got new timestamp
    assert "timestamp" in messages[2]
    assert messages[2]["timestamp"] != "2026-02-06T10:00:00.123Z"


@pytest.mark.asyncio
async def test_timestamp_format():
    """Verify timestamp format is ISO 8601 with milliseconds."""
    context = SimpleContextManager(add_timestamps=True)

    await context.add_message({"role": "user", "content": "Test"})

    messages = await context.get_messages()
    timestamp = messages[0]["timestamp"]

    # Should match format: 2026-02-06T10:00:00.123+00:00
    # Year-Month-DayTHour:Minute:Second.Milliseconds+00:00
    assert "T" in timestamp
    assert timestamp.endswith("+00:00")
    assert "." in timestamp  # Has milliseconds

    # Should be parseable
    parsed = datetime.fromisoformat(timestamp)
    assert isinstance(parsed, datetime)
