"""Tests for the tool-result budget, shape, per-tool rules, and spill.

Everything this feature adds is OFF by default. The single most important
test in this file is `test_default_config_replacement_text_is_byte_identical`:
it pins the exact replacement string this module has emitted since before the
feature existed, character for character. If that test ever fails, the
"defaults are a no-op" claim in the README and the module docstring is false.

The other three load-bearing groups:

  - TOOL-PAIR INTEGRITY: turning any of these knobs on must not break the
    tool_use/tool_result atomicity the provider APIs require. A donor engine
    measured 29 of 30 turns dying on exactly this.

  - _seq / PREFIX STABILITY: the replacement text (including the spill
    pointer) is re-derived for every sticky-truncated message on every single
    request. It must be byte-identical every time, or the shared prefix
    mutates and -- under a grow-only prompt cache -- every mutation is a full
    cold rebuild.

  - SPILL WRITE FAILURE: a failed write must NOT change the emitted bytes.
    See `test_spill_write_failure_still_emits_stable_pointer`.
"""

import logging
from pathlib import Path
from typing import Any

import pytest
from amplifier_module_context_simple import (
    TOOL_RESULT_SHAPE_HEAD,
    TOOL_RESULT_SHAPE_HEAD_TAIL,
    SimpleContextManager,
)

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _make_context(**overrides: Any) -> SimpleContextManager:
    """A context manager tuned to compact aggressively, like the sticky tests."""
    config: dict[str, Any] = {
        "max_tokens": 2000,
        "compact_threshold": 0.5,
        "target_usage": 0.3,
        "protected_recent": 0.2,
        "protected_tool_results": 1,
        "compaction_notice_enabled": False,
    }
    config.update(overrides)
    return SimpleContextManager(**config)


def _make_gentle_context(**overrides: Any) -> SimpleContextManager:
    """Enough pressure to reach the TRUNCATION rungs (levels 1-2) and stop.

    The aggressive `_make_context` settings escalate all the way to level 8,
    where every tool result has been REMOVED rather than truncated -- which
    makes any assertion about truncated content vacuously true. Calibrated
    against the ladder (measured: 11 truncated results, sticky level 2).
    """
    config: dict[str, Any] = {
        "max_tokens": 20_000,
        "compact_threshold": 0.5,
        "target_usage": 0.45,
        "protected_recent": 0.2,
        "protected_tool_results": 1,
        "compaction_notice_enabled": False,
    }
    config.update(overrides)
    return SimpleContextManager(**config)


def _tool_msg(
    call_id: str, content: str, seq: int | None = 0, **extra: Any
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
        **extra,
    }
    if seq is not None:
        msg["metadata"] = {**(msg.get("metadata") or {}), "_seq": seq}
    return msg


def _assistant_call(call_id: str, tool_name: str, openai_shape: bool = False) -> dict:
    if openai_shape:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        }
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "tool": tool_name, "arguments": {}}],
    }


def _legacy_expected(content: str, truncate_chars: int) -> str:
    """The exact string this module emitted BEFORE this feature existed.

    Transcribed from the pre-change source, not from the new implementation --
    so this is an independent oracle, not a tautology.
    """
    original_tokens = len(content) // 4
    return (
        f"[truncated: ~{original_tokens:,} tokens - call tool again if needed] "
        f"{content[:truncate_chars]}..."
    )


# --------------------------------------------------------------------------
# 1. BYTE IDENTITY -- the defaults-are-a-no-op contract
# --------------------------------------------------------------------------


def test_default_config_replacement_text_is_byte_identical():
    """THE byte-identity test. Default config, literal expected bytes."""
    context = SimpleContextManager()
    content = "A" * 5000
    out = context._truncate_tool_result(_tool_msg("c1", content))

    assert out["content"] == _legacy_expected(content, 250)
    # And spelled out literally once, so a change to _legacy_expected() alone
    # cannot silently re-baseline this test.
    assert out["content"].startswith(
        "[truncated: ~1,250 tokens - call tool again if needed] AAA"
    )
    assert out["content"].endswith("...")
    assert len(out["content"]) == len(
        "[truncated: ~1,250 tokens - call tool again if needed] "
    ) + 250 + 3
    assert out["_truncated"] is True
    assert out["_original_tokens"] == 1250


def test_default_config_honors_custom_truncate_chars():
    """A pre-existing `truncate_chars` override must keep working untouched."""
    context = SimpleContextManager(truncate_chars=40)
    content = "B" * 900
    out = context._truncate_tool_result(_tool_msg("c1", content))
    assert out["content"] == _legacy_expected(content, 40)


def test_default_config_short_result_untouched_and_identical_object():
    context = SimpleContextManager()
    msg = _tool_msg("c1", "short")
    assert context._truncate_tool_result(msg) is msg


def test_default_config_non_string_content_untouched():
    context = SimpleContextManager()
    msg = _tool_msg("c1", "")
    msg["content"] = [{"type": "text", "text": "x" * 5000}]
    assert context._truncate_tool_result(msg) is msg


@pytest.mark.asyncio
async def test_default_config_full_compaction_view_is_byte_identical():
    """End-to-end: every truncated result in a real compacted view matches
    the pre-change formula exactly."""
    context = _make_gentle_context(truncate_chars=60)
    originals: dict[str, str] = {}
    for i in range(30):
        await context.add_message({"role": "user", "content": f"turn {i} " + "u" * 200})
        await context.add_message(_assistant_call(f"call_{i}", "bash"))
        body = f"result {i} " + "z" * 800
        originals[f"call_{i}"] = body
        await context.add_message(
            {"role": "tool", "tool_call_id": f"call_{i}", "content": body}
        )

    view = await context.get_messages_for_request()
    assert context._last_compaction_stats is not None, "test must actually compact"

    truncated_seen = 0
    for msg in view:
        if msg.get("role") != "tool":
            continue
        if not msg.get("_truncated"):
            continue
        truncated_seen += 1
        original = originals[msg["tool_call_id"]]
        assert msg["content"] == _legacy_expected(original, 60)
    assert truncated_seen > 0, "test must actually truncate something"


@pytest.mark.asyncio
async def test_default_config_writes_no_files_anywhere(tmp_path):
    """With no spill dir configured, nothing is ever written to disk."""
    context = _make_gentle_context(truncate_chars=60)
    for i in range(30):
        await context.add_message({"role": "user", "content": f"t{i} " + "u" * 200})
        await context.add_message(_assistant_call(f"call_{i}", "bash"))
        await context.add_message(
            {"role": "tool", "tool_call_id": f"call_{i}", "content": f"r{i} " + "z" * 800}
        )
    await context.get_messages_for_request()

    assert context._truncated_seqs, "test must actually truncate something"
    assert context.tool_result_spill_dir is None
    assert context._spilled_paths == set()
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# 2. TOKEN-DENOMINATED BUDGET
# --------------------------------------------------------------------------


def test_token_budget_keeps_four_chars_per_token():
    context = SimpleContextManager(tool_result_budget_tokens=1000)
    content = "C" * 20_000
    out = context._truncate_tool_result(_tool_msg("c1", content))
    body = out["content"].split("] ", 1)[1]
    assert body == "C" * 4000 + "..."


def test_token_budget_leaves_under_budget_results_alone():
    context = SimpleContextManager(tool_result_budget_tokens=1000)
    msg = _tool_msg("c1", "D" * 3999)
    assert context._truncate_tool_result(msg) is msg


def test_token_budget_header_shape_matches_legacy_head_shape():
    """Switching only the UNIT must not change the FORMAT."""
    context = SimpleContextManager(tool_result_budget_tokens=10)
    content = "E" * 900
    out = context._truncate_tool_result(_tool_msg("c1", content))
    assert out["content"] == _legacy_expected(content, 40)


# --------------------------------------------------------------------------
# 3. HEAD + TAIL -- the measured quality gap
# --------------------------------------------------------------------------


def test_head_tail_keeps_the_tail():
    """G-TRB-TAIL, as a unit test: the tail of the ORIGINAL survives."""
    context = SimpleContextManager(
        tool_result_budget_tokens=100, tool_result_shape=TOOL_RESULT_SHAPE_HEAD_TAIL
    )
    content = "HEAD" + "m" * 10_000 + "FAILED: assertion at line 42"
    out = context._truncate_tool_result(_tool_msg("c1", content))["content"]

    assert out.endswith("FAILED: assertion at line 42")
    assert "HEAD" in out
    assert "...[" in out and "chars omitted]..." in out


def test_head_tail_splits_budget_in_half_and_counts_omission_exactly():
    context = SimpleContextManager(
        tool_result_budget_tokens=100, tool_result_shape=TOOL_RESULT_SHAPE_HEAD_TAIL
    )
    content = "".join(str(i % 10) for i in range(10_000))
    out = context._truncate_tool_result(_tool_msg("c1", content))["content"]

    body = out.split("] ", 1)[1]
    head, rest = body.split("\n...[", 1)
    omitted_str, tail = rest.split(" chars omitted]...\n", 1)

    assert head == content[:200]
    assert tail == content[-200:]
    assert omitted_str.replace(",", "") == str(10_000 - 400)


def test_head_tail_smallest_budget_does_not_leak_whole_content():
    """content[-0:] is the WHOLE string. Guard against that class of bug."""
    context = SimpleContextManager(
        tool_result_budget_tokens=1, tool_result_shape=TOOL_RESULT_SHAPE_HEAD_TAIL
    )
    content = "X" * 5000
    out = context._truncate_tool_result(_tool_msg("c1", content))["content"]
    assert len(out) < 200, f"replacement should be tiny, got {len(out)} chars"


def test_head_tail_under_budget_result_untouched():
    context = SimpleContextManager(
        tool_result_budget_tokens=100, tool_result_shape=TOOL_RESULT_SHAPE_HEAD_TAIL
    )
    msg = _tool_msg("c1", "Y" * 399)
    assert context._truncate_tool_result(msg) is msg


# --------------------------------------------------------------------------
# 4. PER-TOOL BUDGETS AND EXEMPTIONS
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_tool_budget_resolved_via_harvested_tool_call_id():
    context = _make_context(
        tool_result_budget_tokens=10,
        tool_result_budget_by_tool={"grep": 500},
    )
    await context.add_message(_assistant_call("call_g", "grep"))
    await context.add_message(_assistant_call("call_b", "bash"))

    grep_result = _tool_msg("call_g", "G" * 9000, seq=None)
    bash_result = _tool_msg("call_b", "B" * 9000, seq=None)

    grep_out = context._truncate_tool_result(grep_result)["content"]
    bash_out = context._truncate_tool_result(bash_result)["content"]

    assert grep_out.split("] ", 1)[1] == "G" * 2000 + "..."  # 500 tokens
    assert bash_out.split("] ", 1)[1] == "B" * 40 + "..."  # 10 tokens (global)


@pytest.mark.asyncio
async def test_per_tool_budget_resolved_via_openai_function_shape():
    context = _make_context(tool_result_budget_by_tool={"read_file": 300})
    await context.add_message(_assistant_call("c1", "read_file", openai_shape=True))
    out = context._truncate_tool_result(_tool_msg("c1", "R" * 9000, seq=None))
    assert out["content"].split("] ", 1)[1] == "R" * 1200 + "..."


def test_per_tool_budget_resolved_via_name_field_on_the_result():
    context = SimpleContextManager(tool_result_budget_by_tool={"grep": 5})
    out = context._truncate_tool_result(_tool_msg("c1", "G" * 900, name="grep"))
    assert out["content"].split("] ", 1)[1] == "G" * 20 + "..."


def test_per_tool_budget_resolved_via_metadata_tool_name():
    context = SimpleContextManager(tool_result_budget_by_tool={"grep": 5})
    msg = _tool_msg("c1", "G" * 900)
    msg["metadata"] = {**msg["metadata"], "tool_name": "grep"}
    out = context._truncate_tool_result(msg)
    assert out["content"].split("] ", 1)[1] == "G" * 20 + "..."


def test_unresolvable_tool_name_falls_back_to_the_global_budget():
    context = SimpleContextManager(
        tool_result_budget_tokens=10, tool_result_budget_by_tool={"grep": 500}
    )
    out = context._truncate_tool_result(_tool_msg("unknown_id", "Z" * 900))
    assert out["content"].split("] ", 1)[1] == "Z" * 40 + "..."


def test_per_tool_budget_alone_leaves_other_tools_on_the_legacy_path():
    """Setting ONLY the per-tool map must not change any other tool's bytes."""
    context = SimpleContextManager(tool_result_budget_by_tool={"grep": 500})
    content = "Q" * 900
    out = context._truncate_tool_result(_tool_msg("c1", content, name="bash"))
    assert out["content"] == _legacy_expected(content, 250)


def test_exempt_tool_is_never_truncated():
    context = SimpleContextManager(
        tool_result_budget_tokens=10, tool_result_exempt_tools=["load_skill"]
    )
    msg = _tool_msg("c1", "S" * 50_000, name="load_skill")
    assert context._truncate_tool_result(msg) is msg


@pytest.mark.asyncio
async def test_exempt_tool_survives_full_compaction_pressure():
    """The skill output is still whole after the ladder has run everywhere."""
    skill_body = "SKILL-BODY " + "s" * 800
    # NOTE: protected_tool_results=1, not 0. `tool_result_indices[-0:]` is the
    # WHOLE list, so setting 0 protects EVERY tool result from truncation --
    # the exact opposite of what it reads like. Pre-existing behavior, filed
    # separately; this test just avoids the trap.
    context = _make_gentle_context(
        tool_result_budget_tokens=10,
        tool_result_exempt_tools=["load_skill"],
        protected_tool_results=1,
    )
    await context.add_message(_assistant_call("call_skill", "load_skill"))
    await context.add_message(
        {"role": "tool", "tool_call_id": "call_skill", "content": skill_body}
    )
    for i in range(30):
        await context.add_message({"role": "user", "content": f"t{i} " + "u" * 200})
        await context.add_message(_assistant_call(f"call_{i}", "bash"))
        await context.add_message(
            {"role": "tool", "tool_call_id": f"call_{i}", "content": f"r{i} " + "z" * 800}
        )

    view = await context.get_messages_for_request()
    assert context._last_compaction_stats is not None, "test must actually compact"

    skill_msgs = [m for m in view if m.get("tool_call_id") == "call_skill"]
    if skill_msgs:  # may be removed entirely at high pressure -- but never trimmed
        assert skill_msgs[0]["content"] == skill_body
        assert not skill_msgs[0].get("_truncated")
    other_truncated = [
        m
        for m in view
        if m.get("role") == "tool" and m.get("_truncated") and m["tool_call_id"] != "call_skill"
    ]
    assert other_truncated, "test must actually truncate the non-exempt results"


# --------------------------------------------------------------------------
# 5. SPILL TO DISK
# --------------------------------------------------------------------------


def test_spill_writes_full_content_and_points_at_it(tmp_path):
    context = SimpleContextManager(
        tool_result_budget_tokens=50,
        tool_result_shape=TOOL_RESULT_SHAPE_HEAD_TAIL,
        tool_result_spill_dir=str(tmp_path / "spill"),
    )
    content = "HEAD" + "m" * 20_000 + "TAIL"
    out = context._truncate_tool_result(_tool_msg("c1", content))["content"]

    written = list((tmp_path / "spill").iterdir())
    assert len(written) == 1
    assert written[0].read_text() == content
    assert str(written[0]) in out
    assert "read " in out and "for the full result" in out


def test_spill_pointer_is_content_addressed_and_idempotent(tmp_path):
    context = SimpleContextManager(
        tool_result_budget_tokens=50, tool_result_spill_dir=str(tmp_path)
    )
    msg = _tool_msg("c1", "N" * 9000)
    first = context._truncate_tool_result(msg)["content"]
    written = list(tmp_path.iterdir())
    mtime = written[0].stat().st_mtime_ns

    # Re-derive 5 more times, exactly as _apply_sticky_decisions does.
    for _ in range(5):
        assert context._truncate_tool_result(msg)["content"] == first
    assert list(tmp_path.iterdir()) == written
    assert written[0].stat().st_mtime_ns == mtime, "spill file was rewritten"


def test_spill_paths_differ_for_different_content(tmp_path):
    context = SimpleContextManager(
        tool_result_budget_tokens=50, tool_result_spill_dir=str(tmp_path)
    )
    context._truncate_tool_result(_tool_msg("c1", "P" * 9000, seq=1))
    context._truncate_tool_result(_tool_msg("c2", "Q" * 9000, seq=2))
    assert len(list(tmp_path.iterdir())) == 2


def test_spill_write_failure_still_emits_stable_pointer(tmp_path):
    """A failed write must NOT change the emitted bytes.

    Byte-stability outranks pointer validity: a dangling pointer is visible
    and recoverable, a silently mutated prefix is a cold cache rebuild.
    """
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("i am a file")
    context = SimpleContextManager(
        tool_result_budget_tokens=50, tool_result_spill_dir=str(blocker / "spill")
    )
    msg = _tool_msg("c1", "F" * 9000)

    first = context._truncate_tool_result(msg)["content"]
    second = context._truncate_tool_result(msg)["content"]

    assert first == second
    assert "for the full result" in first
    assert not (blocker / "spill").exists()


def test_spill_disabled_by_default_writes_nothing(tmp_path):
    context = SimpleContextManager(tool_result_budget_tokens=50)
    out = context._truncate_tool_result(_tool_msg("c1", "G" * 9000))["content"]
    assert "call tool again if needed" in out
    assert list(tmp_path.iterdir()) == []


def test_spill_file_content_is_the_untruncated_original(tmp_path):
    context = SimpleContextManager(
        tool_result_budget_tokens=50,
        tool_result_shape=TOOL_RESULT_SHAPE_HEAD_TAIL,
        tool_result_spill_dir=str(tmp_path),
    )
    content = "\n".join(f"line {i}" for i in range(5000))
    out = context._truncate_tool_result(_tool_msg("c1", content))["content"]
    spilled = next(tmp_path.iterdir())
    assert spilled.read_text() == content
    # The middle really is missing from the message but present in the file.
    assert "line 2500" not in out
    assert "line 2500" in spilled.read_text()


# --------------------------------------------------------------------------
# 6. TOOL-PAIR INTEGRITY under the new configuration
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_pairs_stay_atomic_with_every_knob_enabled(tmp_path):
    context = _make_gentle_context(
        tool_result_budget_tokens=40,
        tool_result_shape=TOOL_RESULT_SHAPE_HEAD_TAIL,
        tool_result_budget_by_tool={"grep": 10, "bash": 80},
        tool_result_exempt_tools=["load_skill"],
        tool_result_spill_dir=str(tmp_path),
    )
    for i in range(30):
        tool = ["grep", "bash", "load_skill"][i % 3]
        await context.add_message({"role": "user", "content": f"t{i} " + "u" * 200})
        await context.add_message(_assistant_call(f"call_{i}", tool))
        await context.add_message(
            {"role": "tool", "tool_call_id": f"call_{i}", "content": f"out {i} " + "z" * 800}
        )

    view = await context.get_messages_for_request()
    assert context._last_compaction_stats is not None, "test must actually compact"
    assert context._truncated_seqs, "test must actually truncate something"

    requested = {
        tc["id"]
        for m in view
        for tc in (m.get("tool_calls") or [])
        if isinstance(tc, dict) and tc.get("id")
    }
    answered = {m["tool_call_id"] for m in view if m.get("role") == "tool"}
    assert requested == answered, (
        "tool_use/tool_result atomicity broken: "
        f"unanswered={requested - answered} orphaned={answered - requested}"
    )


@pytest.mark.asyncio
async def test_exempt_results_are_not_counted_as_truncated(tmp_path):
    context = _make_gentle_context(
        tool_result_budget_tokens=20,
        tool_result_exempt_tools=["load_skill"],
    )
    for i in range(30):
        await context.add_message({"role": "user", "content": f"t{i} " + "u" * 200})
        await context.add_message(_assistant_call(f"call_{i}", "load_skill"))
        await context.add_message(
            {"role": "tool", "tool_call_id": f"call_{i}", "content": f"s{i} " + "s" * 800}
        )

    await context.get_messages_for_request()
    assert context._truncated_seqs == set(), (
        "an exempt tool result was recorded as truncated -- it would inflate "
        "the reported count and pin a sticky decision that does nothing"
    )


# --------------------------------------------------------------------------
# 7. _seq / PREFIX STABILITY
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prefix_stays_byte_stable_with_head_tail_and_spill(tmp_path):
    context = _make_gentle_context(
        tool_result_budget_tokens=40,
        tool_result_shape=TOOL_RESULT_SHAPE_HEAD_TAIL,
        tool_result_spill_dir=str(tmp_path),
    )
    for i in range(30):
        await context.add_message({"role": "user", "content": f"t{i} " + "u" * 200})
        await context.add_message(_assistant_call(f"call_{i}", "bash"))
        await context.add_message(
            {"role": "tool", "tool_call_id": f"call_{i}", "content": f"o{i} " + "z" * 800}
        )

    call1 = await context.get_messages_for_request()
    assert context._last_compaction_stats is not None, "test must actually compact"
    assert context._truncated_seqs, "test must actually truncate something"
    assert list(tmp_path.iterdir()), "test must actually spill something"

    await context.add_message({"role": "user", "content": "next turn " + "u" * 200})
    await context.add_message({"role": "assistant", "content": "ack"})
    call2 = await context.get_messages_for_request()

    assert call2[: len(call1)] == call1, (
        "shared prefix changed after appending one turn -- the truncated "
        "replacement text (or its spill pointer) is not deterministic"
    )


@pytest.mark.asyncio
async def test_truncation_is_idempotent_across_many_requests(tmp_path):
    context = _make_gentle_context(
        tool_result_budget_tokens=40,
        tool_result_shape=TOOL_RESULT_SHAPE_HEAD_TAIL,
        tool_result_spill_dir=str(tmp_path),
    )
    for i in range(30):
        await context.add_message({"role": "user", "content": f"t{i} " + "u" * 200})
        await context.add_message(_assistant_call(f"call_{i}", "bash"))
        await context.add_message(
            {"role": "tool", "tool_call_id": f"call_{i}", "content": f"o{i} " + "z" * 800}
        )

    baseline = await context.get_messages_for_request()
    assert context._truncated_seqs, "test must actually truncate something"
    for _ in range(5):
        assert await context.get_messages_for_request() == baseline

    # And exactly one spill file per distinct truncated result, no churn.
    spilled = sorted(p.name for p in tmp_path.iterdir())
    assert len(spilled) == len(set(spilled))
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


@pytest.mark.asyncio
async def test_set_messages_rebuilds_the_per_tool_name_map():
    """A resumed session must resolve per-tool budgets as the original did."""
    context = _make_context(tool_result_budget_by_tool={"grep": 5})
    await context.set_messages(
        [
            _assistant_call("call_g", "grep"),
            {"role": "tool", "tool_call_id": "call_g", "content": "G" * 900},
        ]
    )
    assert context._tool_name_by_call_id == {"call_g": "grep"}
    out = context._truncate_tool_result(context.messages[1])
    assert out["content"].split("] ", 1)[1] == "G" * 20 + "..."


@pytest.mark.asyncio
async def test_clear_resets_the_per_tool_name_map():
    context = _make_context(tool_result_budget_by_tool={"grep": 5})
    await context.add_message(_assistant_call("call_g", "grep"))
    assert context._tool_name_by_call_id
    await context.clear()
    assert context._tool_name_by_call_id == {}
    assert context._spilled_paths == set()


@pytest.mark.asyncio
async def test_default_config_does_no_tool_name_harvesting():
    """The default path must not pay for a feature nobody turned on."""
    context = _make_context()
    await context.add_message(_assistant_call("call_g", "grep"))
    assert context._tool_name_by_call_id == {}


# --------------------------------------------------------------------------
# 8. CONFIG VALIDATION -- warn and fall back, never crash mount()
# --------------------------------------------------------------------------


def test_unknown_shape_falls_back_to_head_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        context = SimpleContextManager(tool_result_shape="middle_out")
    assert context.tool_result_shape == TOOL_RESULT_SHAPE_HEAD
    assert "tool_result_shape" in caplog.text


@pytest.mark.parametrize("bad", [0, -5, "1000", 3.5, True])
def test_unusable_budget_tokens_falls_back_to_legacy(bad, caplog):
    with caplog.at_level(logging.WARNING):
        context = SimpleContextManager(tool_result_budget_tokens=bad)
    assert context.tool_result_budget_tokens is None
    assert "tool_result_budget_tokens" in caplog.text
    content = "A" * 900
    out = context._truncate_tool_result(_tool_msg("c1", content))
    assert out["content"] == _legacy_expected(content, 250)


def test_bad_per_tool_entries_are_dropped_individually(caplog):
    with caplog.at_level(logging.WARNING):
        context = SimpleContextManager(
            tool_result_budget_by_tool={"grep": 500, "bash": -1, "ls": "big"}
        )
    assert context.tool_result_budget_by_tool == {"grep": 500}
    assert "bash" in caplog.text and "ls" in caplog.text


def test_non_dict_per_tool_map_is_ignored(caplog):
    with caplog.at_level(logging.WARNING):
        context = SimpleContextManager(tool_result_budget_by_tool=["grep"])
    assert context.tool_result_budget_by_tool == {}
    assert "tool_result_budget_by_tool" in caplog.text


def test_exempt_tools_coerced_to_frozenset():
    context = SimpleContextManager(tool_result_exempt_tools=["a", "a", "b"])
    assert context.tool_result_exempt_tools == frozenset({"a", "b"})


def test_defaults_are_all_inert():
    context = SimpleContextManager()
    assert context.tool_result_budget_tokens is None
    assert context.tool_result_shape == TOOL_RESULT_SHAPE_HEAD
    assert context.tool_result_budget_by_tool == {}
    assert context.tool_result_exempt_tools == frozenset()
    assert context.tool_result_spill_dir is None


# --------------------------------------------------------------------------
# 9. mount() plumbing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_passes_every_new_flag_through(tmp_path):
    from amplifier_module_context_simple import mount

    mounted: dict[str, Any] = {}

    class _Coordinator:
        hooks = None

        async def mount(self, slot: str, instance: Any) -> None:
            mounted[slot] = instance

    await mount(
        _Coordinator(),  # type: ignore[arg-type]
        {
            "tool_result_budget_tokens": 4000,
            "tool_result_shape": "head_tail",
            "tool_result_budget_by_tool": {"grep": 2000},
            "tool_result_exempt_tools": ["load_skill"],
            "tool_result_spill_dir": str(tmp_path),
        },
    )
    context = mounted["context"]
    assert context.tool_result_budget_tokens == 4000
    assert context.tool_result_shape == TOOL_RESULT_SHAPE_HEAD_TAIL
    assert context.tool_result_budget_by_tool == {"grep": 2000}
    assert context.tool_result_exempt_tools == frozenset({"load_skill"})
    assert context.tool_result_spill_dir == str(tmp_path)


@pytest.mark.asyncio
async def test_mount_defaults_leave_every_flag_inert():
    from amplifier_module_context_simple import mount

    mounted: dict[str, Any] = {}

    class _Coordinator:
        hooks = None

        async def mount(self, slot: str, instance: Any) -> None:
            mounted[slot] = instance

    await mount(_Coordinator(), {})  # type: ignore[arg-type]
    context = mounted["context"]
    assert context.tool_result_budget_tokens is None
    assert context.tool_result_shape == TOOL_RESULT_SHAPE_HEAD
    assert context.tool_result_budget_by_tool == {}
    assert context.tool_result_exempt_tools == frozenset()
    assert context.tool_result_spill_dir is None
    assert context.truncate_chars == 250


def test_spill_dir_is_not_created_until_something_spills(tmp_path):
    target = tmp_path / "never"
    SimpleContextManager(
        tool_result_budget_tokens=50, tool_result_spill_dir=str(target)
    )
    assert not target.exists()


def test_spill_dir_created_lazily_on_first_spill(tmp_path):
    target = tmp_path / "made_on_demand" / "nested"
    context = SimpleContextManager(
        tool_result_budget_tokens=50, tool_result_spill_dir=str(target)
    )
    context._truncate_tool_result(_tool_msg("c1", "H" * 9000))
    assert Path(target).is_dir()
    assert len(list(Path(target).iterdir())) == 1
