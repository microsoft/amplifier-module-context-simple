"""
Tests for the two-part cache-stability fix in SimpleContextManager:

(a) Notice placement: the compaction notice must be appended at the TAIL of
    the returned message list (never inserted into the prefix), with
    role="user" (not "system") and metadata={"ephemeral": True}, so that:
      - every provider's caching (explicit breakpoints or automatic prefix
        matching) sees a stable prefix across calls, and
      - the Anthropic provider's ephemeral walk-back logic
        (_count_trailing_ephemeral_messages) actually recognizes and skips it
        -- which requires role != "system" (system-role messages never reach
        the "conversation" list the walk-back operates on at all) AND
        metadata.ephemeral=True on a TRAILING message.

(b) Sticky compaction decisions: once a message is removed, truncated, or
    stubbed, that decision must never be re-derived or reversed on a later
    call. This is what keeps the prefix byte-stable across calls where
    history only grew by a turn or two, instead of continuously re-deriving
    (and potentially shifting) the whole compaction decision every call.

The most important test here is `test_prefix_stability_regression` -- this is
the test that would have caught the original bug (notice at index 1 + fully
re-derived compaction on every call).
"""

from typing import Any

import pytest
from amplifier_module_context_simple import SimpleContextManager


def _padded(i: int, role: str, size: int = 80) -> dict:
    """A message with enough bulk to move the token counter meaningfully."""
    return {"role": role, "content": f"{role} message {i} " + ("x" * size)}


async def _fill_until_compacted(context: SimpleContextManager, turns: int = 40) -> None:
    """Add enough user/assistant turns to push past compact_threshold at least once."""
    for i in range(turns):
        await context.add_message(_padded(i, "user"))
        await context.add_message(_padded(i, "assistant"))


def _make_context(**overrides: Any) -> SimpleContextManager:
    config: dict[str, Any] = {
        "max_tokens": 2000,
        "compact_threshold": 0.5,
        "target_usage": 0.3,
        "protected_recent": 0.2,
        "protected_tool_results": 1,
        "truncate_chars": 40,
        "compaction_notice_enabled": True,
        "compaction_notice_min_level": 1,
    }
    config.update(overrides)
    return SimpleContextManager(**config)


@pytest.mark.asyncio
async def test_prefix_stability_regression():
    """THE regression test: two consecutive get_messages_for_request() calls,
    where history grew by exactly one turn in between, must produce a
    byte-identical shared prefix (everything except the trailing ephemeral
    notice, which is expected to change only when a NEW escalation happens).

    This is exactly the scenario the original bug broke: the compaction
    notice was inserted at index 1 (inside the prefix) and the entire
    compaction decision was re-derived from scratch on every call, so the
    prefix shifted on every single turn once compaction started firing.
    """
    context = _make_context()
    await _fill_until_compacted(context)

    call1 = await context.get_messages_for_request()
    assert context._last_compaction_stats is not None, (
        "Test setup must actually trigger compaction for this test to be meaningful"
    )

    # Grow history by exactly one turn.
    await context.add_message(_padded(9001, "user"))
    await context.add_message(_padded(9001, "assistant"))

    call2 = await context.get_messages_for_request()

    # Strip any trailing ephemeral notice (identified by metadata.ephemeral) --
    # that tail element is EXPECTED to be present/absent/reworded depending on
    # whether a new escalation happened; it is deliberately outside the
    # "prefix" this test is protecting.
    def strip_trailing_notice(messages: list[dict]) -> list[dict]:
        if messages and (messages[-1].get("metadata") or {}).get("ephemeral"):
            return messages[:-1]
        return messages

    prefix1 = strip_trailing_notice(call1)
    prefix2 = strip_trailing_notice(call2)

    # call2 grew by one turn (2 messages) relative to call1's prefix region.
    # The OVERLAPPING region (everything call1 had) must be byte-identical.
    shared_len = len(prefix1)
    assert prefix2[:shared_len] == prefix1, (
        "Shared prefix changed between two calls where history grew by only "
        "one turn -- this is exactly the cache-busting bug this fix exists "
        "to prevent. Diff:\n"
        f"call1 prefix: {prefix1}\n"
        f"call2 prefix (truncated to same length): {prefix2[:shared_len]}"
    )

    # And the new call should indeed have grown (sanity check the test itself
    # isn't vacuously true because nothing was appended).
    assert len(prefix2) >= shared_len


@pytest.mark.asyncio
async def test_prefix_stability_across_many_incremental_turns():
    """Stronger version: run many turns past the first escalation and assert
    the prefix only ever grows (never rewrites) except right at an escalation
    boundary. Detects any residual per-call churn the single-step test above
    might miss.
    """
    context = _make_context()
    await _fill_until_compacted(context)

    previous = None
    rewrites = 0
    escalations = 0
    last_stats = None
    for i in range(20):
        await context.add_message(_padded(1000 + i, "user"))
        await context.add_message(_padded(1000 + i, "assistant"))
        current = await context.get_messages_for_request()

        stats = context._last_compaction_stats
        new_escalation = stats is not last_stats
        if new_escalation:
            escalations += 1
            last_stats = stats

        if previous is not None:
            prev_body = (
                previous[:-1]
                if (previous[-1].get("metadata") or {}).get("ephemeral")
                else previous
            )
            curr_body = (
                current[:-1]
                if (current[-1].get("metadata") or {}).get("ephemeral")
                else current
            )
            shared = min(len(prev_body), len(curr_body))
            if curr_body[:shared] != prev_body[:shared] and not new_escalation:
                rewrites += 1

        previous = current

    assert rewrites == 0, (
        f"Prefix was rewritten {rewrites} time(s) on a call that was NOT a "
        f"new compaction escalation -- decisions are not sticky."
    )


@pytest.mark.asyncio
async def test_notice_is_appended_at_tail_not_prefix():
    """The compaction notice must be the LAST message, not inserted at
    position 1 (the original bug's placement)."""
    context = _make_context()
    await _fill_until_compacted(context)

    messages = await context.get_messages_for_request()
    assert context._last_compaction_stats is not None

    notice_candidates = [
        m
        for m in messages
        if (m.get("metadata") or {}).get("source") == "context-compaction"
    ]
    assert len(notice_candidates) == 1, "Expected exactly one compaction notice"
    notice = notice_candidates[0]

    assert messages[-1] is notice, (
        "Compaction notice must be the LAST message (tail), not mid-prefix"
    )
    assert messages[1] is not notice, (
        "Regression: notice must not land at position 1 (the old bug)"
    )


@pytest.mark.asyncio
async def test_notice_carries_ephemeral_metadata_and_non_system_role():
    """The notice must be recognizable to the Anthropic provider's ephemeral
    walk-back logic: metadata.ephemeral=True AND role != "system" (a
    "system"-role message never reaches the "conversation" list that walk-back
    operates on -- see amplifier_module_provider_anthropic._complete_chat_request,
    which extracts ALL role=="system" messages out of the conversation before
    the ephemeral walk-back ever runs)."""
    context = _make_context()
    await _fill_until_compacted(context)

    messages = await context.get_messages_for_request()
    notice = messages[-1]

    assert notice.get("metadata", {}).get("ephemeral") is True, (
        "Notice must carry metadata.ephemeral=True for the provider's "
        "trailing-ephemeral walk-back to recognize and skip it"
    )
    assert notice.get("role") != "system", (
        "Notice must NOT be role='system' -- system-role messages are "
        "extracted out of the conversation entirely by the Anthropic "
        "provider (merged into the single system content block), so an "
        "ephemeral system-role notice would (a) never be seen by the "
        "conversation-region ephemeral walk-back at all, and (b) corrupt "
        "the SYSTEM cache breakpoint too, since it would change the "
        "aggregate system text on every compaction."
    )


@pytest.mark.asyncio
async def test_notice_is_still_visible_and_informative():
    """Moving the notice to the tail must not silently make it ineffective --
    it must still contain real, informative content the model can act on."""
    context = _make_context()
    await _fill_until_compacted(context)

    messages = await context.get_messages_for_request()
    notice = messages[-1]
    content = notice.get("content", "")

    assert isinstance(content, str) and content, "Notice must have non-empty content"
    assert "compact" in content.lower(), "Notice should mention compaction happened"
    assert "system-reminder" in content, (
        "Notice should use the established system-reminder convention"
    )


@pytest.mark.asyncio
async def test_sticky_removal_decisions_never_reversed():
    """Once a message is decided as removed, it must stay removed on every
    subsequent call -- even as more history is added and the raw-history
    percentages that originally selected it shift."""
    context = _make_context()
    await _fill_until_compacted(context, turns=30)

    await context.get_messages_for_request()
    stats1 = context._last_compaction_stats
    assert stats1 is not None
    removed_seqs_after_call1 = set(context._removed_seqs)
    assert removed_seqs_after_call1, (
        "Test setup should have actually removed some messages"
    )

    # Grow more, forcing potential re-evaluation.
    for i in range(5):
        await context.add_message(_padded(2000 + i, "user"))
        await context.add_message(_padded(2000 + i, "assistant"))
    await context.get_messages_for_request()

    # Every seq removed after call 1 must still be removed now -- sticky
    # decisions are additive only, never reversed.
    assert removed_seqs_after_call1.issubset(context._removed_seqs), (
        "A previously-removed message was un-removed -- compaction decisions "
        "must be permanent (sticky), not re-derived from scratch each call."
    )


@pytest.mark.asyncio
async def test_large_system_message_counts_toward_compaction_trigger():
    """Regression test: the sticky escalation-decision threshold check must
    include the SYSTEM message's token count, not just the non-system
    (conversation) portion.

    This guards against a real bug introduced during development of the
    sticky-decision mechanism: the escalation check was computed from
    `_estimate_tokens(working_messages)` where `working_messages` was only
    the non-system portion, silently dropping the system message's token
    contribution. Whenever the system message is a large fraction of the
    budget (a common real-world case -- e.g. a big bundle-composed system
    prompt), that omission means the conversation-only portion may never
    cross the threshold on its own, so compaction silently never fires even
    though total usage (system + conversation) is well over budget.
    """
    large_system_content = "x" * 4000  # ~1000 tokens, dominates a small budget
    context = _make_context(
        max_tokens=1200,
        compact_threshold=0.6,
        target_usage=0.4,
        protected_recent=0.2,
    )
    await context.add_message({"role": "system", "content": large_system_content})

    # Add modest conversation growth -- alone, this would stay well under
    # compact_threshold * budget, but the system message alone already
    # exceeds it (1000 tokens vs 0.6*1200=720).
    for i in range(6):
        await context.add_message({"role": "user", "content": f"turn {i}"})
        await context.add_message({"role": "assistant", "content": f"ack {i}"})
        await context.get_messages_for_request()

    assert context._last_compaction_stats is not None, (
        "Compaction never triggered even though system message tokens alone "
        "exceed compact_threshold * budget -- the escalation check is "
        "silently ignoring system message size."
    )


@pytest.mark.asyncio
async def test_no_escalation_when_sticky_view_already_under_threshold():
    """Adding a single small turn after an escalation should NOT trigger a new
    escalation (and therefore must not change _last_compaction_stats) as long
    as the sticky (already-decided) view remains comfortably under the
    compact_threshold. This is the core of "prefix only changes at a genuine
    compaction step, not continuously."
    """
    context = _make_context(compact_threshold=0.92, target_usage=0.5)
    await _fill_until_compacted(context, turns=40)

    await context.get_messages_for_request()
    stats_after_first = context._last_compaction_stats
    assert stats_after_first is not None

    # A single small turn shouldn't be enough to cross 92% again immediately.
    await context.add_message({"role": "user", "content": "ok"})
    await context.add_message({"role": "assistant", "content": "ok"})
    await context.get_messages_for_request()

    assert context._last_compaction_stats is stats_after_first, (
        "A single small turn triggered a brand new compaction escalation -- "
        "expected the sticky view to already be under threshold, meaning no "
        "new decisions (and no stats/notice change) should have occurred."
    )


@pytest.mark.asyncio
async def test_system_messages_never_compacted_under_stickiness():
    """System messages must always survive compaction regardless of how many
    escalations have occurred."""
    context = _make_context()
    await context.add_message({"role": "system", "content": "You are a helpful agent."})
    await _fill_until_compacted(context, turns=40)

    for i in range(10):
        await context.add_message(_padded(3000 + i, "user"))
        await context.add_message(_padded(3000 + i, "assistant"))
        messages = await context.get_messages_for_request()
        system_msgs = [m for m in messages if m.get("role") == "system"]
        assert len(system_msgs) == 1
        assert system_msgs[0]["content"] == "You are a helpful agent."


@pytest.mark.asyncio
async def test_last_user_message_always_protected_under_stickiness():
    """The most recent user message must never be removed or stubbed, even
    after many rounds of sticky escalation."""
    context = _make_context()
    await _fill_until_compacted(context, turns=50)

    last_content = "THE CURRENT TASK: do not lose me"
    await context.add_message({"role": "user", "content": last_content})

    messages = await context.get_messages_for_request()
    # Ignore the trailing ephemeral notice when looking for the last "real" user msg.
    body = (
        messages[:-1]
        if (messages[-1].get("metadata") or {}).get("ephemeral")
        else messages
    )
    user_msgs = [m for m in body if m.get("role") == "user"]
    assert user_msgs, "Expected at least one user message to survive"
    assert user_msgs[-1]["content"] == last_content, (
        "The last user message must be preserved verbatim (never stubbed/removed)"
    )


@pytest.mark.asyncio
async def test_tool_pairs_remain_atomic_under_sticky_removal():
    """No tool_result may survive in the returned view without its matching
    tool_use, and vice versa -- across many sticky escalations."""
    context = _make_context(max_tokens=1500, compact_threshold=0.5, target_usage=0.3)

    for i in range(30):
        await context.add_message(
            {"role": "user", "content": f"do task {i}" + "x" * 30}
        )
        await context.add_message(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": "do_thing"},
                    }
                ],
            }
        )
        await context.add_message(
            {"role": "tool", "tool_call_id": f"call_{i}", "content": "result " * 20}
        )

    for i in range(30, 45):
        await context.add_message({"role": "user", "content": f"more {i}" + "x" * 30})
        await context.add_message({"role": "assistant", "content": f"ack {i}"})
        messages = await context.get_messages_for_request()

        tool_use_ids = set()
        for m in messages:
            for tc in m.get("tool_calls") or []:
                tc_id = tc.get("id")
                if tc_id:
                    tool_use_ids.add(tc_id)
        tool_result_ids = {
            m.get("tool_call_id") for m in messages if m.get("role") == "tool"
        }
        orphans = tool_result_ids - tool_use_ids
        assert not orphans, f"Orphaned tool_result(s) found at iteration {i}: {orphans}"
        orphan_calls = tool_use_ids - tool_result_ids
        assert not orphan_calls, (
            f"Orphaned tool_use(s) found at iteration {i}: {orphan_calls}"
        )


@pytest.mark.asyncio
async def test_get_messages_returns_full_uncompacted_history():
    """Non-destructive guarantee must still hold: get_messages() always
    returns the complete, unmodified history regardless of sticky state."""
    context = _make_context()
    await _fill_until_compacted(context, turns=40)
    await context.get_messages_for_request()  # trigger at least one escalation

    full_history = await context.get_messages()
    assert len(full_history) == len(context.messages)
    # None of the stored messages should carry compaction markers -- those
    # only ever appear on the ephemeral VIEW, never on stored history.
    assert not any(m.get("_truncated") for m in full_history)
    assert not any(m.get("_stubbed") for m in full_history)
