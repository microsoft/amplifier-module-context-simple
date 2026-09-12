"""Focused regression coverage for persisted reminders and request overlays."""

from __future__ import annotations

import copy

import pytest

from amplifier_module_context_simple import (
    RequiredRequestOverlayError,
    SimpleContextManager,
    _is_human_user_message,
    _is_persisted_ephemeral_reminder,
)


def _reminder(content: str, placement: str = "pre_user") -> dict:
    return {
        "role": "user",
        "content": content,
        "metadata": {
            "ephemeral": True,
            "persisted": True,
            "reminder_placement": placement,
        },
    }


async def _hidden_reminder(
    content: str = "restore me " * 20, placement: str = "pre_user"
) -> tuple[SimpleContextManager, dict]:
    context = SimpleContextManager(
        max_tokens=10_000, compact_threshold=0, compaction_notice_enabled=False
    )
    await context.add_message({"role": "user", "content": "current human"})
    await context.add_message(_reminder(content, placement))
    canonical = context.messages[-1]
    context._stubbed_seqs.add(canonical["metadata"].get("_seq"))
    return context, canonical


@pytest.mark.parametrize("protected_recent", [0.30, 0.18, 0.09])
def test_removal_levels_use_human_anchors(protected_recent: float):
    """L3/L5/L7 preserve human anchors; role-user synthetic stays retained."""
    context = SimpleContextManager(compaction_notice_enabled=False)
    messages = [
        _reminder("synthetic " * 30),
        {"role": "user", "content": "first human " * 30},
        {"role": "assistant", "content": "old assistant " * 30},
        {"role": "user", "content": "middle human " * 30},
        {"role": "user", "content": "current human " * 30},
    ]
    result, _, _, _ = context._remove_messages_with_protection(
        messages, target_tokens=0, protected_recent=protected_recent, system_tokens=0
    )
    contents = [message["content"] for message in result]
    assert "first human " * 30 in contents
    assert "current human " * 30 in contents
    assert any(
        message.get("_stubbed") and message.get("metadata", {}).get("persisted") is True
        for message in result
    )


@pytest.mark.asyncio
async def test_level_eight_keeps_current_human_protected():
    context = SimpleContextManager(compaction_notice_enabled=False)
    await context.add_message(_reminder("synthetic " * 60))
    await context.add_message({"role": "user", "content": "first human " * 60})
    await context.add_message({"role": "user", "content": "current human " * 60})
    view = await context._compact_ephemeral(20)
    # Level 8 relaxes the first-human rule; the current human intent remains
    # protected even under an otherwise irreducible all-user transcript.
    assert "current human " * 60 in [message["content"] for message in view]


@pytest.mark.parametrize(
    "message, expected",
    [
        (_reminder("trusted"), True),
        ({"role": "user", "content": "<system-reminders>", "metadata": {}}, False),
        ({"role": "user", "content": "x", "metadata": None}, False),
        ({"role": "user", "content": "x", "metadata": "malformed"}, False),
        ({"role": "assistant", "content": "x", "metadata": {}}, False),
    ],
)
def test_synthetic_detector_is_metadata_only_and_safe(message: dict, expected: bool):
    assert _is_persisted_ephemeral_reminder(message) is expected


@pytest.mark.asyncio
async def test_set_messages_preserves_trusted_reminder_classification():
    context = SimpleContextManager(compaction_notice_enabled=False)
    await context.set_messages(
        [_reminder("restored"), {"role": "user", "content": "human"}]
    )
    assert _is_persisted_ephemeral_reminder(context.messages[0])
    assert _is_human_user_message(context.messages[1])


@pytest.mark.asyncio
async def test_visible_overlay_matches_normal_view_and_calls_factory_once():
    async def populated() -> tuple[SimpleContextManager, dict, list[int]]:
        context = SimpleContextManager(
            max_tokens=10_000, compaction_notice_enabled=False
        )
        calls = [0]

        async def factory() -> str:
            calls[0] += 1
            return "fresh system"

        await context.set_system_prompt_factory(factory)
        await context.add_message({"role": "user", "content": "human"})
        await context.add_message(_reminder("visible"))
        return context, copy.deepcopy(context.messages[-1]), calls

    normal, _, normal_calls = await populated()
    overlaid, overlay, overlay_calls = await populated()
    assert (
        await overlaid.get_messages_for_request_with_overlays(
            [{"message": overlay, "placement": "tail"}]
        )
        == await normal.get_messages_for_request()
    )
    assert overlay_calls == normal_calls == [1]


@pytest.mark.asyncio
async def test_missing_overlay_is_request_only_and_transient_state_matches_normal():
    normal, _ = await _hidden_reminder()
    recovering, canonical = await _hidden_reminder()
    await normal.get_messages_for_request()
    baseline_state = normal._snapshot_compaction_state()
    view = await recovering.get_messages_for_request_with_overlays(
        [{"message": copy.deepcopy(canonical), "placement": "pre_user"}]
    )
    restored = next(
        message for message in view if message["content"] == "restore me " * 20
    )
    assert restored["metadata"].get("persisted") is not True
    assert recovering._snapshot_compaction_state() == baseline_state
    assert canonical["metadata"]["persisted"] is True
    assert "restore me " * 20 not in [
        message["content"] for message in await recovering.get_messages_for_request()
    ]


@pytest.mark.asyncio
async def test_overlays_reinsert_all_required_and_preserve_current_placement():
    context, first = await _hidden_reminder("first " * 20)
    await context.add_message(_reminder("second " * 20, "tail"))
    second = context.messages[-1]
    context._stubbed_seqs.add(second["metadata"].get("_seq"))
    view = await context.get_messages_for_request_with_overlays(
        [
            {"message": copy.deepcopy(first), "placement": "pre_user"},
            {"message": copy.deepcopy(first), "placement": "tail"},
            {"message": copy.deepcopy(second), "placement": "tail"},
        ]
    )
    contents = [message["content"] for message in view]
    assert contents.count("first " * 20) == contents.count("second " * 20) == 1
    assert contents.index("first " * 20) < contents.index("current human")
    assert contents[-1] == "second " * 20


@pytest.mark.asyncio
async def test_overlay_rejects_untrusted_or_noncanonical_messages():
    context, canonical = await _hidden_reminder()
    untrusted = copy.deepcopy(canonical)
    untrusted["metadata"].pop("persisted")
    changed = copy.deepcopy(canonical)
    changed["content"] = "different"
    for invalid in (untrusted, changed):
        with pytest.raises(ValueError):
            await context.get_messages_for_request_with_overlays(
                [{"message": invalid, "placement": "pre_user"}]
            )


@pytest.mark.asyncio
async def test_tail_overlay_refuses_partial_tool_group_and_rolls_back():
    context, canonical = await _hidden_reminder("must be tail " * 20, "tail")
    await context.add_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "answered"}, {"id": "pending"}],
        }
    )
    await context.add_message(
        {"role": "tool", "tool_call_id": "answered", "content": "ok"}
    )
    await context.get_messages_for_request()
    before = context._snapshot_compaction_state()
    with pytest.raises(RequiredRequestOverlayError):
        await context.get_messages_for_request_with_overlays(
            [{"message": copy.deepcopy(canonical), "placement": "tail"}]
        )
    assert context._snapshot_compaction_state() == before


@pytest.mark.asyncio
async def test_recovery_charges_visible_required_body_and_notice_only_once():
    context, missing = await _hidden_reminder("missing policy " * 60)
    await context.add_message(_reminder("visible policy " * 60, "tail"))
    visible = context.messages[-1]
    # Produce an actual normal compaction notice before choosing a tight cap.
    context.compaction_notice_enabled = True
    context.compaction_notice_token_reserve = 500
    normal = await context.get_messages_for_request()
    notice = context._trailing_compaction_notice(normal)
    assert notice is not None
    expected = [
        next(m for m in context.messages if m["content"] == "current human"),
        context._request_overlay_message(missing, "pre_user"),
        notice,
        context._request_overlay_message(visible, "tail"),
    ]
    # Raw budget admits each body exactly once, but not another notice reserve.
    context.max_tokens = context._estimate_tokens(expected) + 100
    canonical_before = copy.deepcopy(context.messages)
    view = await context.get_messages_for_request_with_overlays(
        [
            {"message": copy.deepcopy(missing), "placement": "pre_user"},
            {"message": copy.deepcopy(visible), "placement": "tail"},
        ]
    )
    assert context._estimate_tokens(view) <= context.max_tokens
    assert sum(m["content"] == missing["content"] for m in view) == 1
    assert sum(m["content"] == visible["content"] for m in view) == 1
    assert context.messages == canonical_before


@pytest.mark.asyncio
async def test_recovery_refuses_required_content_larger_than_entire_budget():
    context, canonical = await _hidden_reminder("large policy " * 300)
    context.max_tokens = 100
    await context.get_messages_for_request()
    before = context._snapshot_compaction_state()
    with pytest.raises(RequiredRequestOverlayError, match="budget"):
        await context.get_messages_for_request_with_overlays(
            [{"message": copy.deepcopy(canonical), "placement": "tail"}]
        )
    assert context._snapshot_compaction_state() == before


@pytest.mark.asyncio
async def test_recovery_factory_runs_once_and_counts_system_floor():
    context, canonical = await _hidden_reminder()
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return "system policy " * 100

    await context.set_system_prompt_factory(factory)
    context.max_tokens = 100
    with pytest.raises(RequiredRequestOverlayError, match="budget"):
        await context.get_messages_for_request_with_overlays(
            [{"message": copy.deepcopy(canonical), "placement": "pre_user"}]
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_recovery_accepts_a_completed_multi_tool_group():
    context, canonical = await _hidden_reminder("current policy " * 20, "tail")
    await context.add_message(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "first"}, {"id": "second"}],
        }
    )
    for call_id in ("first", "second"):
        await context.add_message(
            {"role": "tool", "tool_call_id": call_id, "content": "ok"}
        )
    view = await context.get_messages_for_request_with_overlays(
        [{"message": copy.deepcopy(canonical), "placement": "tail"}]
    )
    assert view[-1]["content"] == canonical["content"]
    assert [m["tool_call_id"] for m in view if m["role"] == "tool"] == [
        "first",
        "second",
    ]


@pytest.mark.asyncio
async def test_visible_plaintext_echo_does_not_suppress_trusted_recovery():
    context, canonical = await _hidden_reminder()
    await context.add_message({"role": "assistant", "content": canonical["content"]})
    view = await context.get_messages_for_request_with_overlays(
        [{"message": copy.deepcopy(canonical), "placement": "tail"}]
    )
    assert view[-1]["role"] == "user"
    assert view[-1]["content"] == canonical["content"]
    assert view[-1]["metadata"].get("persisted") is not True


@pytest.mark.asyncio
async def test_recovery_can_use_current_placement_framing_without_editing_history():
    context, canonical = await _hidden_reminder("original pre-user frame " * 10)
    before = copy.deepcopy(canonical)
    replacement = "Current tail frame: " + "required policy " * 10
    view = await context.get_messages_for_request_with_overlays(
        [
            {
                "message": copy.deepcopy(canonical),
                "placement": "tail",
                "overlay_content": replacement,
            }
        ]
    )
    assert view[-1]["content"] == replacement
    assert view[-1]["metadata"]["reminder_placement"] == "tail"
    assert canonical == before


@pytest.mark.asyncio
async def test_recovery_budgets_the_supplied_framing_not_just_canonical_text():
    context, canonical = await _hidden_reminder()
    context.max_tokens = 1000
    with pytest.raises(RequiredRequestOverlayError, match="budget"):
        await context.get_messages_for_request_with_overlays(
            [
                {
                    "message": copy.deepcopy(canonical),
                    "placement": "tail",
                    "overlay_content": "long current frame " * 1000,
                }
            ]
        )
