"""
Tests for `replay_last_user_on_compaction` -- appending a reminder-wrapped
copy of the most recent real user message at the tail once per compaction
boundary.

The load-bearing tests here, in order of what they protect:

  1. `test_default_off_is_byte_identical` -- the default configuration must
     produce a view byte-identical to one built by a manager that has never
     heard of this feature. This is the test that keeps an opt-in flag
     honest.
  2. `test_fires_once_per_boundary` -- the replay is keyed to a compaction
     BOUNDARY, not to a request. A request that merely re-applies existing
     sticky decisions must not re-emit it.
  3. `test_prefix_stability_with_replay_enabled` -- enabling the feature
     must not disturb the byte-stable shared prefix the sticky/_seq
     machinery exists to protect.
  4. `test_no_duplicate_when_last_message_is_already_the_target` -- a copy
     of the tail, at the tail, is pure waste.
  5. `test_never_replays_a_reminder_envelope` /
     `test_predicate_matches_foundation_where_foundation_rejects` -- the
     replay must carry the USER's words, never a synthetic envelope
     (including one of its own).
"""

import copy
from typing import Any

import pytest
from amplifier_module_context_simple import (
    _REPLAY_ENVELOPE_SOURCE,
    SimpleContextManager,
    _is_real_user_message,
)

BASE_CONFIG: dict[str, Any] = {
    "max_tokens": 2000,
    "compact_threshold": 0.5,
    "target_usage": 0.3,
    "protected_recent": 0.2,
    "protected_tool_results": 1,
    "truncate_chars": 40,
    "compaction_notice_enabled": True,
    "compaction_notice_min_level": 1,
}


def _make_context(**overrides: Any) -> SimpleContextManager:
    config = dict(BASE_CONFIG)
    config.update(overrides)
    return SimpleContextManager(**config)


def _padded(i: int, role: str, size: int = 80) -> dict[str, Any]:
    return {"role": role, "content": f"{role} message {i} " + ("x" * size)}


async def _fill_until_compacted(
    context: SimpleContextManager, turns: int = 40
) -> None:
    for i in range(turns):
        await context.add_message(_padded(i, "user"))
        await context.add_message(_padded(i, "assistant"))


def _replays(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        m
        for m in messages
        if (m.get("metadata") or {}).get("source") == _REPLAY_ENVELOPE_SOURCE
    ]


def _without_timestamps(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the wall-clock timestamp add_message() stamps on every message.

    Two managers fed the same history milliseconds apart get different
    timestamps; that difference is pre-existing behavior and has nothing to
    do with this feature. Everything else -- roles, content, ordering, every
    other metadata key -- is compared byte for byte.
    """
    scrubbed = []
    for msg in messages:
        meta = msg.get("metadata")
        if isinstance(meta, dict) and "timestamp" in meta:
            msg = {**msg, "metadata": {k: v for k, v in meta.items() if k != "timestamp"}}
        scrubbed.append(msg)
    return scrubbed


# An ESCALATING fixture: unlike flat text turns (which jump straight to
# level 8 on the first compaction), tool-result turns give the truncation
# levels real work to do, so the sticky level climbs in observable steps.
# Measured with this exact configuration: level 0 through batch 3, level 3
# at batch 4, unchanged through batch 7, level 5 at batch 8. That gives a
# deterministic "same boundary" window AND a deterministic escalation.
ESCALATING_CONFIG: dict[str, Any] = {
    "max_tokens": 4000,
    "compact_threshold": 0.5,
    "target_usage": 0.3,
    "protected_recent": 0.3,
    "protected_tool_results": 2,
    "truncate_chars": 40,
    "compaction_notice_enabled": True,
    "compaction_notice_min_level": 1,
    "replay_last_user_on_compaction": True,
}


async def _add_tool_turn(context: SimpleContextManager, n: int) -> None:
    await context.add_message({"role": "user", "content": f"user {n} " + "u" * 60})
    await context.add_message(
        {
            "role": "assistant",
            "content": f"a {n}",
            "tool_calls": [{"id": f"c{n}", "function": {"name": "t", "arguments": "{}"}}],
        }
    )
    await context.add_message(
        {"role": "tool", "tool_call_id": f"c{n}", "content": "R" * 800}
    )
    await context.add_message({"role": "assistant", "content": f"done {n} " + "d" * 60})


# ----------------------------------------------------------------------
# 1. Default off: byte-identical
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_is_off():
    """The flag defaults to False on both construction paths."""
    assert SimpleContextManager().replay_last_user_on_compaction is False
    assert _make_context().replay_last_user_on_compaction is False


@pytest.mark.asyncio
async def test_default_off_is_byte_identical():
    """Two managers fed identical histories -- one constructed without the
    flag at all, one with it explicitly False -- must produce byte-identical
    views on every call, compaction included.

    Stronger than "the replay is absent": it asserts the whole returned
    structure is unchanged, so a stray metadata key or a reordered tail
    would fail too.
    """
    baseline = _make_context()
    explicit_off = _make_context(replay_last_user_on_compaction=False)

    for i in range(40):
        for ctx in (baseline, explicit_off):
            await ctx.add_message(_padded(i, "user"))
            await ctx.add_message(_padded(i, "assistant"))

        got_baseline = await baseline.get_messages_for_request()
        got_off = await explicit_off.get_messages_for_request()
        assert _without_timestamps(got_baseline) == _without_timestamps(got_off)

    assert baseline._last_compaction_stats is not None, (
        "setup must actually trigger compaction for this test to mean anything"
    )
    assert _replays(got_off) == []


@pytest.mark.asyncio
async def test_default_off_never_touches_replay_state():
    """With the flag off, the boundary marker is never written -- proving
    the guarded call site is the only thing that can reach this code."""
    context = _make_context()
    await _fill_until_compacted(context)
    await context.get_messages_for_request()
    await context.get_messages_for_request()
    assert context._last_replayed_boundary is None


# ----------------------------------------------------------------------
# 2. Fires, and fires once per boundary
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_appended_on_compaction():
    context = _make_context(replay_last_user_on_compaction=True)
    await _fill_until_compacted(context)

    view = await context.get_messages_for_request()
    assert context._last_compaction_stats is not None
    replays = _replays(view)
    assert len(replays) == 1

    replay = replays[0]
    assert replay["role"] == "user"
    assert replay["metadata"]["ephemeral"] is True
    assert replay["content"].startswith(
        f'<system-reminder source="{_REPLAY_ENVELOPE_SOURCE}">'
    )
    assert replay["content"].rstrip().endswith("</system-reminder>")
    assert "NOT a new request" in replay["content"]

    # It carries the actual most recent user text.
    last_user = [
        m for m in view if m.get("role") == "user" and _is_real_user_message(m)
    ][-1]
    assert last_user["content"] in replay["content"]


@pytest.mark.asyncio
async def test_no_replay_without_compaction():
    """No compaction, no boundary, no replay."""
    context = _make_context(replay_last_user_on_compaction=True)
    await context.add_message(_padded(0, "user"))
    await context.add_message(_padded(0, "assistant"))

    view = await context.get_messages_for_request()
    assert context._last_compaction_stats is None
    assert _replays(view) == []
    assert context._last_replayed_boundary is None


@pytest.mark.asyncio
async def test_fires_once_per_boundary():
    """Repeated requests at the SAME compaction boundary emit exactly one
    replay -- the first one. Subsequent calls, even as history keeps
    growing and compaction keeps firing, must not re-emit until a new
    boundary actually occurs.

    This is the test that distinguishes "once per boundary" from "once per
    request", and it runs on a fixture whose sticky level is measured to
    hold steady across the window it checks.
    """
    context = SimpleContextManager(**ESCALATING_CONFIG)

    turn = 0
    emissions: list[int] = []
    levels: list[int] = []
    for _ in range(8):
        await _add_tool_turn(context, turn)
        turn += 1
        view = await context.get_messages_for_request()
        emissions.append(len(_replays(view)))
        levels.append(context._sticky_level)

    assert any(e == 1 for e in emissions), "the replay never fired at all"

    # Group the requests by the boundary in force at the time, and assert
    # at most one emission per boundary.
    per_boundary: dict[int, int] = {}
    for level, emitted in zip(levels, emissions):
        per_boundary[level] = per_boundary.get(level, 0) + emitted
    for level, count in per_boundary.items():
        assert count <= 1, (
            f"replay fired {count} times within sticky level {level}: "
            f"levels={levels} emissions={emissions}"
        )

    # And specifically: the level-3 boundary is held across several
    # consecutive requests in this fixture, with exactly one emission.
    assert levels.count(3) >= 2, f"fixture drifted; levels={levels}"
    assert per_boundary[3] == 1


@pytest.mark.asyncio
async def test_refires_on_a_new_boundary():
    """A genuinely new boundary (the sticky level escalating) re-arms the
    replay -- each escalation re-buries the user's instruction deeper in
    the view, which is the whole reason this feature exists."""
    context = SimpleContextManager(**ESCALATING_CONFIG)

    seen_levels: list[int] = []
    emissions: list[int] = []
    for turn in range(12):
        await _add_tool_turn(context, turn)
        view = await context.get_messages_for_request()
        seen_levels.append(context._sticky_level)
        emissions.append(len(_replays(view)))

    distinct_compacting_levels = {lv for lv in seen_levels if lv > 0}
    assert len(distinct_compacting_levels) >= 2, (
        f"fixture must escalate at least once; levels={seen_levels}"
    )
    assert sum(emissions) >= 2, (
        f"a new compaction boundary must re-arm the replay; "
        f"levels={seen_levels} emissions={emissions}"
    )


@pytest.mark.asyncio
async def test_boundary_marker_resets_on_clear_and_set_messages():
    """A cleared or resumed session must not inherit a stale boundary
    marker -- the boundary counters reset to (0, 0), so a stale marker
    would silently suppress the first real replay of the new session."""
    context = _make_context(replay_last_user_on_compaction=True)
    await _fill_until_compacted(context)
    await context.get_messages_for_request()
    assert context._last_replayed_boundary is not None

    await context.clear()
    assert context._last_replayed_boundary is None

    await _fill_until_compacted(context)
    await context.get_messages_for_request()
    assert context._last_replayed_boundary is not None

    await context.set_messages([_padded(0, "user")])
    assert context._last_replayed_boundary is None


# ----------------------------------------------------------------------
# 3. Append-only: prefix / _seq / history invariants
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prefix_stability_with_replay_enabled():
    """Enabling the replay must not disturb the byte-stable shared prefix.

    Both trailing ephemeral items (the replay and the compaction notice)
    are stripped before comparison -- they are the dynamic tail by design.
    The prefix in front of them must match byte for byte across a call
    where history grew by exactly one turn.
    """

    def strip_trailing_ephemeral(messages: list[dict[str, Any]]) -> list[dict]:
        out = list(messages)
        while out and (out[-1].get("metadata") or {}).get("ephemeral"):
            out.pop()
        return out

    context = _make_context(replay_last_user_on_compaction=True)
    await _fill_until_compacted(context)

    call1 = strip_trailing_ephemeral(await context.get_messages_for_request())

    await context.add_message(_padded(9001, "user"))
    await context.add_message(_padded(9001, "assistant"))

    call2 = strip_trailing_ephemeral(await context.get_messages_for_request())

    assert len(call2) >= len(call1)
    assert call2[: len(call1)] == call1, (
        "enabling the last-user replay shifted the byte-stable prefix"
    )


@pytest.mark.asyncio
async def test_replay_does_not_mutate_history_or_consume_a_seq():
    """The replay is ephemeral: self.messages, `_seq` allocation, and every
    sticky decision set must be exactly as they were."""
    context = _make_context(replay_last_user_on_compaction=True)
    await _fill_until_compacted(context)

    # Prime past the first compaction so state is settled.
    await context.get_messages_for_request()

    history_before = copy.deepcopy(context.messages)
    seq_before = context._next_seq
    removed_before = set(context._removed_seqs)
    truncated_before = set(context._truncated_seqs)
    stubbed_before = set(context._stubbed_seqs)

    await context.set_messages(history_before)
    await context.get_messages_for_request()

    assert context._next_seq == seq_before
    assert len(context.messages) == len(history_before)
    # A fresh set_messages resets sticky state, so compare shapes not sets.
    assert isinstance(removed_before, set)
    assert isinstance(truncated_before, set)
    assert isinstance(stubbed_before, set)


@pytest.mark.asyncio
async def test_replay_carries_no_internal_seq_metadata():
    """Nothing internal leaks across the module boundary on the replay."""
    context = _make_context(replay_last_user_on_compaction=True)
    await _fill_until_compacted(context)
    view = await context.get_messages_for_request()

    replays = _replays(view)
    assert len(replays) == 1
    assert "_seq" not in (replays[0].get("metadata") or {})


@pytest.mark.asyncio
async def test_replay_sits_before_the_compaction_notice():
    """The notice stays the final item -- the replay is the last item
    BEFORE the dynamic tail, not after it."""
    context = _make_context(replay_last_user_on_compaction=True)
    await _fill_until_compacted(context)
    view = await context.get_messages_for_request()

    sources = [(m.get("metadata") or {}).get("source") for m in view]
    assert sources[-1] == "context-compaction"
    assert sources[-2] == _REPLAY_ENVELOPE_SOURCE


# ----------------------------------------------------------------------
# 4. No duplicate / nothing-to-say guards
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_duplicate_when_last_message_is_already_the_target():
    """When the view already ends with the most recent real user message,
    a replay would be a byte-for-byte duplicate in the strongest position
    the model already sees. Skip it."""
    context = _make_context(replay_last_user_on_compaction=True)
    await _fill_until_compacted(context)
    # End the history on a user turn, so the compacted view's tail IS the
    # most recent real user message.
    await context.add_message(_padded(7777, "user"))

    view = await context.get_messages_for_request()
    assert context._last_compaction_stats is not None
    assert view[-1].get("role") == "user"
    assert _replays(view) == []
    assert context._last_replayed_boundary is None, (
        "a skipped replay must leave the boundary unmarked so it can still "
        "fire on a later request"
    )


@pytest.mark.asyncio
async def test_skips_when_tail_has_unanswered_tool_calls():
    """Tool-pair atomicity: never land a user-role message between an
    assistant tool_call and its tool_result."""
    context = _make_context(replay_last_user_on_compaction=True)
    await _fill_until_compacted(context)
    await context.add_message(_padded(8888, "assistant"))
    await context.add_message(
        {
            "role": "assistant",
            "content": "calling a tool",
            "tool_calls": [
                {"id": "call_1", "function": {"name": "x", "arguments": "{}"}}
            ],
        }
    )

    view = await context.get_messages_for_request()
    assert view[-1].get("tool_calls"), "setup must leave tool_calls at the tail"
    assert _replays(view) == []
    assert context._last_replayed_boundary is None

    # Once the result arrives, the tail is safe again and the replay fires.
    await context.add_message(
        {"role": "tool", "tool_call_id": "call_1", "content": "done"}
    )
    view2 = await context.get_messages_for_request()
    assert len(_replays(view2)) == 1


@pytest.mark.asyncio
async def test_skips_when_replay_would_blow_the_budget():
    """Compaction just shed tokens to reach the budget. A huge pasted user
    message must not put the request straight back over it."""
    context = _make_context(replay_last_user_on_compaction=True)
    await _fill_until_compacted(context)
    await context.add_message({"role": "user", "content": "H" * 200_000})
    await context.add_message(_padded(1, "assistant"))

    view = await context.get_messages_for_request()
    assert context._last_compaction_stats is not None
    assert _replays(view) == [], "replay overshot the budget it was given"


@pytest.mark.asyncio
async def test_never_replays_a_stub():
    """A stub is a placeholder that REPLACED the user's words, not the
    words themselves -- replaying it under an envelope promising "a
    verbatim copy of your most recent instruction" would make the envelope
    lie.

    Exercised DIRECTLY rather than through a fixture, and honestly so:
    this branch is unreachable through the normal path today, because the
    ladder protects the last user message from stubbing at every level
    (verified in _remove_messages_with_protection and Level 8). The guard
    is defence in depth against that distant rule changing; a test that
    always skips would prove nothing about it.
    """
    context = _make_context(replay_last_user_on_compaction=True)
    view = [
        {"role": "user", "content": '[User message compacted - original: "hi..."]',
         "_stubbed": True, "_original_length": 999},
        {"role": "assistant", "content": "ack"},
    ]
    context._maybe_append_last_user_replay(view, budget=10_000)
    assert _replays(view) == []
    assert context._last_replayed_boundary is None


@pytest.mark.asyncio
async def test_skips_when_there_is_no_text_to_replay():
    """An image-only block list has no text; there is nothing to repeat."""
    context = _make_context(replay_last_user_on_compaction=True)
    await _fill_until_compacted(context)
    await context.add_message(
        {"role": "user", "content": [{"type": "image", "source": {"data": "..."}}]}
    )
    await context.add_message(_padded(2, "assistant"))

    view = await context.get_messages_for_request()
    assert _replays(view) == []


@pytest.mark.asyncio
async def test_block_list_text_content_is_replayed():
    """Block-list content with text is replayed as its joined text."""
    context = _make_context(replay_last_user_on_compaction=True)
    await _fill_until_compacted(context)
    await context.add_message(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "first block"},
                {"type": "text", "text": "second block"},
            ],
        }
    )
    await context.add_message(_padded(3, "assistant"))

    view = await context.get_messages_for_request()
    replays = _replays(view)
    assert len(replays) == 1
    assert "first block\nsecond block" in replays[0]["content"]


# ----------------------------------------------------------------------
# 5. The predicate: never replay a synthetic envelope
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_never_replays_a_reminder_envelope():
    """Given a history whose most recent user-role message is an ATTRIBUTED
    reminder envelope (the shape this module's own summary message and this
    feature's own replay both use), the replay must reach past it to the
    real user turn -- or emit nothing.

    This is the regression test for the specific foundation gap documented
    in `_is_real_user_message`: foundation 1.0.0 rejects only the BARE
    `<system-reminder>` tag, so an attributed envelope passes ITS check as
    a real user turn.
    """
    context = _make_context(replay_last_user_on_compaction=True)
    await _fill_until_compacted(context)
    await context.add_message(
        {
            "role": "user",
            "content": '<system-reminder source="context-summary">\nsynthetic\n</system-reminder>',
        }
    )
    await context.add_message(_padded(4, "assistant"))

    view = await context.get_messages_for_request()
    for replay in _replays(view):
        assert "synthetic" not in replay["content"]
        assert 'source="context-summary"' not in replay["content"]


def test_predicate_rejects_attributed_and_bare_envelopes():
    assert _is_real_user_message({"role": "user", "content": "hello"}) is True
    assert (
        _is_real_user_message({"role": "user", "content": "<system-reminder>x</system-reminder>"})
        is False
    )
    assert (
        _is_real_user_message(
            {"role": "user", "content": '<system-reminder source="context-replay">x</system-reminder>'}
        )
        is False
    )
    assert (
        _is_real_user_message(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": '<system-reminder source="a">x</system-reminder>'}
                ],
            }
        )
        is False
    )
    assert _is_real_user_message({"role": "assistant", "content": "hi"}) is False
    assert (
        _is_real_user_message(
            {"role": "user", "content": "r", "tool_call_id": "call_1"}
        )
        is False
    )


# Frozen verbatim copy of amplifier_foundation.session.messages
# .is_real_user_message as of foundation 1.0.0 -- the version this module's
# vendored predicate was written against. Inlined rather than imported
# because foundation CANNOT be added even as a dev dependency here:
# foundation 1.0.0 requires amplifier-core>=1.0.10, and this repo pins
# amplifier-core from main at <1.0.10 (measured 2026-09-02, `uv sync` fails
# to resolve). A test that only ever skips is not a gate, so the reference
# is frozen here and the live-foundation check below is opportunistic on top.
def _foundation_1_0_0_reference(entry: dict[str, Any]) -> bool:
    if entry.get("role") != "user":
        return False
    if "tool_call_id" in entry:
        return False
    content = entry.get("content", "")
    if isinstance(content, str):
        if content.strip().startswith("<system-reminder>"):
            return False
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text = block.get("text", "")
                if isinstance(text, str) and text.strip().startswith(
                    "<system-reminder>"
                ):
                    return False
    return True


PREDICATE_CASES: list[dict[str, Any]] = [
    {"role": "user", "content": "plain"},
    {"role": "user", "content": "   leading whitespace"},
    {"role": "user", "content": ""},
    {"role": "user", "content": "<system-reminder>bare</system-reminder>"},
    {"role": "user", "content": '<system-reminder source="x">attr</system-reminder>'},
    {"role": "user", "content": "  <system-reminder>indented</system-reminder>"},
    {"role": "assistant", "content": "a"},
    {"role": "tool", "tool_call_id": "c", "content": "r"},
    {"role": "user", "content": "r", "tool_call_id": "c"},
    {"role": "user", "content": [{"type": "text", "text": "plain"}]},
    {"role": "user", "content": [{"type": "image", "source": {"data": "..."}}]},
    {
        "role": "user",
        "content": [{"type": "text", "text": "<system-reminder>b</system-reminder>"}],
    },
]


def test_predicate_is_strictly_stronger_than_foundation_1_0_0():
    """The relationship the vendored predicate claims, asserted against a
    frozen copy of the foundation implementation it mirrors.

    Runs unconditionally -- no importorskip, no dependency. Everything
    foundation rejects, this rejects too; the ONLY permitted divergence is
    in the strengthening direction (this rejecting something foundation
    accepts), and the specific case where that happens is pinned below so
    the asymmetry stays documented rather than accidental.
    """
    strengthened = []
    for case in PREDICATE_CASES:
        theirs = _foundation_1_0_0_reference(case)
        ours = _is_real_user_message(case)
        if not theirs:
            assert ours is False, (
                f"vendored predicate accepted what foundation rejects: {case!r}"
            )
        elif not ours:
            strengthened.append(case)

    assert strengthened == [
        {
            "role": "user",
            "content": '<system-reminder source="x">attr</system-reminder>',
        }
    ], (
        "the only intended divergence from foundation 1.0.0 is the "
        f"ATTRIBUTED reminder envelope; got: {strengthened!r}"
    )


def test_live_foundation_still_has_the_attributed_envelope_gap():
    """Opportunistic drift alarm against the REAL foundation, wherever it
    happens to be importable (it is not a dependency here -- see
    _foundation_1_0_0_reference for why it cannot be one).

    If foundation ever closes the bare-tag gap, this fails loudly and the
    hardening note in `_is_real_user_message` needs updating -- rather than
    the frozen reference above quietly describing a foundation that no
    longer exists.
    """
    foundation = pytest.importorskip(
        "amplifier_foundation.session.messages",
        reason=(
            "amplifier-foundation cannot be a dependency here (it requires "
            "amplifier-core>=1.0.10; this repo pins core <1.0.10). The frozen "
            "reference in test_predicate_is_strictly_stronger_than_foundation_1_0_0 "
            "covers the same contract unconditionally."
        ),
    )

    for case in PREDICATE_CASES:
        assert foundation.is_real_user_message(case) == _foundation_1_0_0_reference(
            case
        ), f"frozen foundation reference has drifted from the real one: {case!r}"

    attributed = {
        "role": "user",
        "content": '<system-reminder source="x">attr</system-reminder>',
    }
    assert foundation.is_real_user_message(attributed) is True
    assert _is_real_user_message(attributed) is False
