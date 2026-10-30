"""Canonical public originals never become execution context or token pressure."""

import copy

import pytest

from amplifier_module_context_simple import SimpleContextManager
from amplifier_module_context_simple._text_estimate import estimate_messages


ORIGINAL = "PUBLIC_ORIGINAL_SENTINEL" * 10000
HISTORY_ONLY = "HISTORY_ONLY_EXECUTION_SENTINEL" * 10000


def rows():
    return [
        {"role": "user", "content": "expanded execution", "metadata": {
            "amplifier_public_message": {"version": 1, "blocks": [ORIGINAL]},
            "amplifier_public_copy_source": {"source": ORIGINAL},
            "legacy": {"keep": "unrelated metadata"},
        }},
        {"role": "assistant", "content": "execution response"},
        {"role": "user", "content": HISTORY_ONLY, "metadata": {
            "amplifier_public_reference_only": True,
            "amplifier_public_message": {"blocks": [ORIGINAL]},
        }},
    ]


def assert_execution(view):
    assert ORIGINAL not in str(view)
    assert HISTORY_ONLY not in str(view)
    assert "amplifier_public_" not in str(view)
    assert view[0]["content"] == "expanded execution"
    assert view[0]["metadata"]["legacy"] == {"keep": "unrelated metadata"}
    assert len(view) == 2


def test_one_projection_preserves_legacy_identity_and_canonical_metadata():
    from amplifier_module_context_simple.request_view import request_view
    original = rows()
    before = copy.deepcopy(original)
    projected = request_view(original)
    assert_execution(projected)
    assert original == before
    projected[0]["metadata"]["new"] = "view only"
    assert "new" not in original[0]["metadata"]
    legacy = {"role": "user", "content": "plain", "metadata": {"unrelated": 7}}
    assert request_view([legacy])[0] is legacy
    false_flag = {"role": "user", "content": "keep", "metadata": {
        "amplifier_public_reference_only": False}}
    assert request_view([false_flag])[0]["content"] == "keep"


def test_text_estimate_ignores_history_facts_and_history_only_rows():
    original = rows()
    expected = [
        {"role": "user", "content": "expanded execution", "metadata": {
            "legacy": {"keep": "unrelated metadata"}}},
        {"role": "assistant", "content": "execution response"},
    ]
    assert estimate_messages(original) == estimate_messages(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["ordinary", "retaining", "measured"])
async def test_all_request_paths_exclude_originals_before_fitting_and_counting(route):
    context = SimpleContextManager(max_tokens=300, token_meter="actual",
                                   compaction_notice_enabled=False)
    await context.set_messages(rows())
    canonical = copy.deepcopy(await context.get_messages())
    counted = []
    async def count(view):
        assert_execution(view)
        counted.append(view)
        return {"dispatch": object(), "budget_decision": {
            "estimated_input_tokens": 30, "input_limit_tokens": 300,
            "measurement": {"kind": "provider_count", "source": "fixture", "input_tokens": 30}}}
    if route == "ordinary":
        view = await context.get_messages_for_request()
    elif route == "retaining":
        view = await context.get_messages_for_request_retaining(retain_contents=[])
    else:
        result = await context.get_measured_request_view(
            provider=None, retain_contents=[], count_view=count)
        view = result["base_view"]
        assert counted
    assert_execution(view)
    assert context._last_compaction_stats is None
    assert await context.get_messages() == canonical
