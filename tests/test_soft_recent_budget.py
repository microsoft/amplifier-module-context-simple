"""Historical reminder density must not turn soft recency into a hard floor."""

import copy

import pytest
from amplifier_core.llm_errors import ContextLengthError

from amplifier_module_context_simple import SimpleContextManager


async def dense_history(*, tool_size=100, required_size=1000, tool_count=5):
    context = SimpleContextManager(max_tokens=10000, compaction_notice_enabled=False)
    await context.add_message({'role': 'system', 'content': 'Required system instructions.'})
    await context.add_message({'role': 'user', 'content': 'Original human requirements.'})
    for i in range(100):
        await context.add_message({
            'role': 'user', 'content': f'Historical reminder {i}: ' + 'r' * 16000,
            'metadata': {'ephemeral': True, 'persisted': True},
        })
        await context.add_message({'role': 'assistant', 'content': 'Earlier work ' + 'a' * 400})
    # An older tool-state carrier must survive even after soft recency relaxes.
    await context.add_message({
        'role': 'assistant', 'content': 'Loaded tools must remain available.',
        'metadata': {'openai:tool_search_items': [{'id': 'loaded-read-file'}]},
    })
    for i in range(tool_count):
        await context.add_message({
            'role': 'assistant', 'content': '',
            'tool_calls': [{'id': f'call-{i}', 'name': 'read_file', 'arguments': {}}],
        })
        await context.add_message({
            'role': 'tool', 'tool_call_id': f'call-{i}', 'content': 't' * tool_size,
        })
    required = 'Current required reminder: ' + 'c' * required_size
    await context.add_message({
        'role': 'user', 'content': required,
        'metadata': {'ephemeral': True, 'persisted': True},
    })
    await context.add_message({'role': 'user', 'content': 'Latest human correction.'})
    return context, required


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_count', [0, 5])
async def test_dense_historical_reminders_fit_without_losing_requirements(tool_count):
    context, required = await dense_history(tool_count=tool_count)
    canonical = copy.deepcopy(await context.get_messages())
    view = await context.get_messages_for_request_retaining(retain_contents=[required])
    assert context._estimate_tokens(view) <= 10000
    for body in ('Required system instructions.', 'Original human requirements.',
                 'Latest human correction.', required, 'Loaded tools must remain available.'):
        assert any(message.get('content') == body for message in view)
    assert [m['tool_call_id'] for m in view if m['role'] == 'tool'] == [f'call-{i}' for i in range(tool_count)]
    assert all(m['content'] == 't' * 100 for m in view if m['role'] == 'tool')
    assert {c['id'] for m in view for c in m.get('tool_calls', [])} == {f'call-{i}' for i in range(tool_count)}
    assert await context.get_messages() == canonical
    assert await context.get_messages_for_request_retaining(retain_contents=[required]) == view


@pytest.mark.asyncio
@pytest.mark.parametrize('tool_size,required_size', [(10000, 1000), (100, 44000)])
async def test_required_floor_still_fails_without_history_or_decision_changes(tool_size, required_size):
    context, required = await dense_history(tool_size=tool_size, required_size=required_size)
    canonical = copy.deepcopy(await context.get_messages())
    with pytest.raises(ContextLengthError):
        await context.get_messages_for_request_retaining(retain_contents=[required])
    assert await context.get_messages() == canonical
    assert not context._removed_seqs
    assert not context._truncated_seqs
    assert not context._stubbed_seqs
