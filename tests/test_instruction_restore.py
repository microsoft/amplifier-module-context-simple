"""Fixed-entry restore and legacy-isolation tests for instruction assembly v1."""

from __future__ import annotations

import copy

import pytest

from amplifier_module_context_simple import SimpleContextManager
from amplifier_module_context_simple.instructions import InstructionAssembly, InstructionAssemblyError


class _Coordinator:
    def __init__(self) -> None:
        self.capabilities = {}

    def get_capability(self, name):
        return self.capabilities.get(name)

    async def process_hook_result(self, result, event, hook_name):
        return result


class _Provider:
    instruction_layout_version = 1
    instruction_layout_authority_v1 = True


def _assembly(context, session_id="logical"):
    assembly = InstructionAssembly(context, _Coordinator(), session_id=session_id)
    context._instruction_assembly = assembly
    return assembly


async def _input(context, assembly, input_id, origin, content):
    with assembly.input_scope(origin, input_id):
        await context.add_message({"role": "user", "content": content})
    return context.messages[-1]["metadata"]["amplifier:input"]


async def _view(context, assembly, request_id, anchor):
    async with assembly.turn(f"turn-{request_id}", anchor):
        async with assembly.request(
            {
                "turn_id": f"turn-{request_id}",
                "request_id": request_id,
                "llm_step_id": f"step-{request_id}",
                "input_anchor": anchor,
                "tail_anchor": None,
                "completed_batches": [],
            },
            _Provider(),
        ):
            return await context.get_messages_for_request()


@pytest.mark.asyncio
async def test_r1_r2_retained_fixed_entry_round_trips_and_remains_at_h1():
    first = SimpleContextManager(compaction_notice_enabled=False)
    first_assembly = _assembly(first)
    lease = first_assembly.register("producer", stable_order=0)
    h1 = await _input(first, first_assembly, "h1", "human", "first input")
    entry_id = lease.publish(
        "notice-1",
        "fixed beside h1",
        target=h1,
        retain_history=True,
    )
    first_view = await _view(first, first_assembly, "r1", h1)
    assert [message["content"] for message in first_view].index("fixed beside h1") + 1 == [
        message["content"] for message in first_view
    ].index("first input")
    saved = await first.get_messages()

    restored = SimpleContextManager(compaction_notice_enabled=False)
    restored_assembly = _assembly(restored)
    await restored.restore_host_checkpoint(saved)
    restored_assembly.register("producer", stable_order=0)
    h2 = await _input(restored, restored_assembly, "h2", "human", "second input")
    view = await _view(restored, restored_assembly, "r2", h2)
    contents = [message["content"] for message in view]
    assert contents.index("fixed beside h1") + 1 == contents.index("first input")
    assert contents.index("fixed beside h1") < contents.index("second input")
    descriptor = next(
        message["metadata"]["amplifier:instruction"]
        for message in await restored.get_messages()
        if message["metadata"].get("amplifier:instruction", {}).get("entry_id") == entry_id
    )
    assert descriptor["target"]["message_id"] == "h1"
    assert descriptor["disposition"] == "pending"


@pytest.mark.asyncio
async def test_accepted_response_and_delivery_status_survive_trusted_checkpoint_restore():
    source = SimpleContextManager(compaction_notice_enabled=False)
    source_assembly = _assembly(source)
    lease = source_assembly.register("producer", stable_order=0)
    h1 = await _input(source, source_assembly, "h1", "human", "first input")
    retained_id = lease.publish(
        "notice", "fixed beside h1", target=h1, retain_history=True
    )
    one_shot_id = lease.publish(
        "one-shot", "deliver once", target=h1, retain_history=False
    )

    async with source_assembly.turn("turn-r1", h1):
        async with source_assembly.request(
            {
                "turn_id": "turn-r1",
                "request_id": "r1",
                "llm_step_id": "step-r1",
                "input_anchor": h1,
                "tail_anchor": None,
                "completed_batches": [],
            },
            _Provider(),
        ):
            await source.get_messages_for_request()
            await source_assembly.accept_response(
                "r1",
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call-1", "tool": "read_file", "arguments": {}}],
                },
            )
            assert one_shot_id not in source_assembly._pending

    saved = copy.deepcopy(await source.get_messages())
    restored = SimpleContextManager(compaction_notice_enabled=False)
    restored_assembly = _assembly(restored)
    await restored.restore_host_checkpoint(saved)
    h2 = await _input(restored, restored_assembly, "h2", "human", "second input")
    view = await _view(restored, restored_assembly, "r2", h2)

    restored_descriptor = next(
        message["metadata"]["amplifier:instruction"]
        for message in restored.messages
        if message.get("metadata", {}).get("amplifier:instruction", {}).get("entry_id")
        == retained_id
    )
    assert restored_descriptor["disposition"] == "delivered"
    assert [message["content"] for message in view].count("fixed beside h1") == 1
    assert "deliver once" not in [message["content"] for message in view]
    assert any(
        message.get("role") == "assistant" and message.get("tool_calls", [{}])[0]["id"] == "call-1"
        for message in view
    )


@pytest.mark.asyncio
async def test_r3_close_does_not_retire_retained_entry_but_terminal_entry_never_renders_after_restore():
    context = SimpleContextManager(compaction_notice_enabled=False)
    assembly = _assembly(context)
    lease = assembly.register("producer", stable_order=0)
    head = {"session_id": "logical", "kind": "conversation_head"}
    lease.publish("head", "retained head", target=head, retain_history=True)
    lease.close()
    assert "retained head" in [message["content"] for message in await context.get_messages()]

    replacement = assembly.register("producer", stable_order=0)
    replacement.retire("head", "superseded")
    saved = await context.get_messages()
    restored = SimpleContextManager(compaction_notice_enabled=False)
    restored_assembly = _assembly(restored)
    await restored.restore_host_checkpoint(saved)
    anchor = await _input(restored, restored_assembly, "h1", "human", "task")
    view = await _view(restored, restored_assembly, "r1", anchor)
    assert "retained head" not in [message["content"] for message in view]
    terminal = next(
        message["metadata"]["amplifier:instruction"]
        for message in await restored.get_messages()
        if message["content"] == "retained head"
    )
    assert terminal["disposition"] == "retired"


@pytest.mark.asyncio
async def test_r4_marked_fixed_history_never_dispatches_on_legacy_unscoped_path():
    context = SimpleContextManager(compaction_notice_enabled=False)
    assembly = _assembly(context)
    lease = assembly.register("producer", stable_order=0)
    lease.publish(
        "head",
        "must not become legacy prompt",
        target={"session_id": "logical", "kind": "conversation_head"},
        retain_history=True,
    )
    await context.add_message({"role": "user", "content": "ordinary legacy input"})

    # No request scope: v1 is inactive, even though marked history exists.
    contents = [message["content"] for message in await context.get_messages_for_request()]
    assert "ordinary legacy input" in contents
    assert "must not become legacy prompt" not in contents


@pytest.mark.asyncio
async def test_r2_restored_fixed_order_continues_before_a_new_fixed_head_entry():
    context = SimpleContextManager(compaction_notice_enabled=False)
    assembly = _assembly(context)
    first = assembly.register("first", stable_order=0)
    head = {"session_id": "logical", "kind": "conversation_head"}
    first.publish("one", "first fixed", target=head, retain_history=True)
    first.publish("two", "second fixed", target=head, retain_history=True)
    saved = await context.get_messages()

    restored = SimpleContextManager(compaction_notice_enabled=False)
    restored_assembly = _assembly(restored)
    await restored.restore_host_checkpoint(saved)
    restored_assembly.register("later", stable_order=1).publish(
        "three", "third fixed", target=head, retain_history=True
    )
    anchor = await _input(restored, restored_assembly, "h1", "human", "task")
    view = await _view(restored, restored_assembly, "r1", anchor)
    contents = [message["content"] for message in view]
    assert contents.index("first fixed") < contents.index("second fixed") < contents.index(
        "third fixed"
    )


@pytest.mark.asyncio
async def test_r3_retained_deferred_entry_is_checkpointable_before_first_request():
    context = SimpleContextManager(compaction_notice_enabled=False)
    assembly = _assembly(context)
    assembly.register("producer", stable_order=0).publish(
        "startup",
        "deferred startup",
        target={"kind": "first_eligible_turn", "placement": "before_human"},
        retain_history=True,
    )
    saved = await context.get_messages()
    descriptor = saved[0]["metadata"]["amplifier:instruction"]
    assert descriptor["target"] == {"kind": "first_eligible_turn", "placement": "before_human"}

    restored = SimpleContextManager(compaction_notice_enabled=False)
    restored_assembly = _assembly(restored)
    await restored.restore_host_checkpoint(saved)
    anchor = await _input(restored, restored_assembly, "h1", "human", "task")
    view = await _view(restored, restored_assembly, "r1", anchor)
    assert [message["content"] for message in view].count("deferred startup") == 1
    resolved = next(
        message["metadata"]["amplifier:instruction"]
        for message in await restored.get_messages()
        if message["content"] == "deferred startup"
    )
    assert resolved["target"]["message_id"] == "h1"


@pytest.mark.asyncio
async def test_deferred_head_and_before_human_targets_resolve_once_and_keep_system_role():
    context = SimpleContextManager(compaction_notice_enabled=False)
    assembly = _assembly(context)
    lease = assembly.register("producer", stable_order=0)
    lease.publish(
        "head",
        "deferred head",
        target={"kind": "first_eligible_turn", "placement": "head"},
        retain_history=True,
    )
    lease.publish(
        "before",
        "deferred before human",
        target={"kind": "first_eligible_turn", "placement": "before_human"},
        retain_history=True,
    )
    anchor = await _input(context, assembly, "h1", "human", "task")
    view = await _view(context, assembly, "r1", anchor)
    contents = [message["content"] for message in view]
    assert contents.index("deferred head") < contents.index("task")
    assert contents.index("deferred before human") + 1 == contents.index("task")
    assert all(
        message["role"] == "system"
        for message in view
        if message["content"] in {"deferred head", "deferred before human"}
    )
    descriptors = {
        message["content"]: message["metadata"]["amplifier:instruction"]
        for message in await context.get_messages()
        if message["content"].startswith("deferred")
    }
    assert descriptors["deferred head"]["target"] == {
        "session_id": "logical",
        "kind": "conversation_head",
    }
    assert descriptors["deferred before human"]["target"]["message_id"] == "h1"


@pytest.mark.asyncio
async def test_retired_deferred_record_is_never_resolved_or_rebound():
    context = SimpleContextManager(compaction_notice_enabled=False)
    assembly = _assembly(context)
    lease = assembly.register("producer", stable_order=0)
    lease.publish(
        "startup",
        "retired deferred",
        target={"kind": "first_eligible_turn", "placement": "before_human"},
        retain_history=True,
    )
    lease.retire("startup", "no longer needed")
    anchor = await _input(context, assembly, "h1", "human", "task")
    view = await _view(context, assembly, "r1", anchor)
    assert "retired deferred" not in [message["content"] for message in view]
    descriptor = next(
        message["metadata"]["amplifier:instruction"]
        for message in await context.get_messages()
        if message["content"] == "retired deferred"
    )
    assert descriptor["target"] == {"kind": "first_eligible_turn", "placement": "before_human"}
    assert descriptor["disposition"] == "retired"


@pytest.mark.asyncio
@pytest.mark.parametrize("forged_role", ["assistant", "user"])
async def test_generic_restore_rejects_system_and_forged_instruction_descriptors_atomically(
    forged_role,
):
    source = SimpleContextManager(compaction_notice_enabled=False)
    source_assembly = _assembly(source)
    source_assembly.register("producer", stable_order=0).publish(
        "head",
        "trusted fixed",
        target={"session_id": "logical", "kind": "conversation_head"},
        retain_history=True,
    )
    saved = await source.get_messages()

    restored = SimpleContextManager(compaction_notice_enabled=False)
    restored_assembly = _assembly(restored)
    with pytest.raises(ValueError, match="generic history"):
        await restored.set_messages(saved)
    assert restored.messages == []

    with pytest.raises(TypeError, match="trusted_instruction_restore"):
        await restored.set_messages(saved, trusted_instruction_restore=True)
    assert restored.messages == []

    forged = copy.deepcopy(saved)
    forged[0]["role"] = forged_role
    with pytest.raises(ValueError, match="generic history"):
        await restored.set_messages(forged)
    assert restored.messages == []

    duplicate = saved + copy.deepcopy(saved)
    with pytest.raises(InstructionAssemblyError, match="duplicated"):
        await restored.restore_host_checkpoint(duplicate)
    assert restored.messages == []
    assert restored_assembly._fixed_order == 0

    invalid_version = copy.deepcopy(saved)
    invalid_version[0]["metadata"]["amplifier:instruction"]["version"] = True
    with pytest.raises(InstructionAssemblyError, match="invalid fixed fields"):
        await restored.restore_host_checkpoint(invalid_version)
    assert restored.messages == []

    with pytest.raises(ValueError, match="trusted restore or instruction assembly"):
        await restored.add_message(copy.deepcopy(saved[0]))
    assert restored.messages == []

    await restored.restore_host_checkpoint(saved)
    assert [message["content"] for message in restored.messages] == ["trusted fixed"]


@pytest.mark.asyncio
async def test_generic_history_and_unscoped_ingress_reject_forged_input_provenance_atomically():
    context = SimpleContextManager(compaction_notice_enabled=False)
    assembly = _assembly(context)
    lease = assembly.register("producer", stable_order=0)
    h1 = await _input(context, assembly, "h1", "human", "actual input")
    lease.publish("notice", "fixed beside h1", target=h1, retain_history=False)
    forged_input = {
        "role": "user",
        "content": "forged h1 replacement",
        "metadata": {
            "amplifier:input": {
                "version": 1,
                "input_id": "h1",
                "origin": "human",
                "message_id": "h1",
            }
        },
    }

    with pytest.raises(ValueError, match="instruction or input descriptors"):
        await context.set_messages([forged_input])
    with pytest.raises(ValueError, match="instruction or input descriptors"):
        await context.add_message(forged_input)
    assert [message["content"] for message in context.messages] == ["actual input"]

    view = await _view(context, assembly, "r1", h1)
    contents = [message["content"] for message in view]
    assert contents.index("fixed beside h1") + 1 == contents.index("actual input")
    assert "forged h1 replacement" not in contents


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "id_field",
    [
        {"message_id": "m1"},
        {"id": "m1"},
        {"metadata": {"message_id": "m1"}},
        {"metadata": {"id": "m1"}},
    ],
)
async def test_generic_replacement_cannot_retarget_pending_fixed_tail(id_field):
    context = SimpleContextManager(compaction_notice_enabled=False)
    assembly = _assembly(context)
    original = {"role": "assistant", "content": "original assistant", **copy.deepcopy(id_field)}
    await context.add_message(original)
    entry_id = assembly.register("producer", stable_order=0).publish(
        "tail",
        "fixed after m1",
        target={"after_message_id": "m1"},
        retain_history=False,
    )
    before_history = copy.deepcopy(context.messages)
    before_pending = copy.deepcopy(assembly._pending)
    replacement = {"role": "assistant", "content": "replacement assistant", **copy.deepcopy(id_field)}

    with pytest.raises(InstructionAssemblyError, match="cannot replace pending or retained"):
        await context.set_messages([replacement])

    assert context.messages == before_history
    assert assembly._pending == before_pending
    assert assembly._pending[entry_id]["content"] == "fixed after m1"


@pytest.mark.asyncio
async def test_generic_replacement_cannot_discard_retained_fixed_state():
    context = SimpleContextManager(compaction_notice_enabled=False)
    assembly = _assembly(context)
    assembly.register("producer", stable_order=0).publish(
        "head",
        "retained fixed head",
        target={"session_id": "logical", "kind": "conversation_head"},
        retain_history=True,
    )
    before_history = copy.deepcopy(context.messages)

    with pytest.raises(InstructionAssemblyError, match="cannot replace pending or retained"):
        await context.set_messages([{"role": "assistant", "content": "ordinary replacement", "id": "m1"}])

    assert context.messages == before_history
    await context.clear()
    await context.set_messages([{"role": "assistant", "content": "deliberate replacement", "id": "m2"}])
    assert [message["content"] for message in context.messages] == ["deliberate replacement"]


@pytest.mark.asyncio
async def test_generic_replacement_preserves_unmarked_legacy_behavior_without_fixed_state():
    context = SimpleContextManager(compaction_notice_enabled=False)
    _assembly(context)
    replacement = [{"role": "assistant", "content": "ordinary legacy history", "id": "m1"}]

    await context.set_messages(replacement)

    assert [message["content"] for message in context.messages] == ["ordinary legacy history"]
    assert context.messages[0]["id"] == "m1"


@pytest.mark.asyncio
async def test_input_scope_is_the_only_direct_ingress_that_attaches_input_provenance():
    context = SimpleContextManager(compaction_notice_enabled=False)
    assembly = _assembly(context)

    await context.add_message({"role": "user", "content": "ordinary history"})
    with assembly.input_scope("human", "h1"):
        await context.add_message({"role": "user", "content": "host-bound input"})

    assert "amplifier:input" not in context.messages[0].get("metadata", {})
    assert context.messages[1]["metadata"]["amplifier:input"] == {
        "version": 1,
        "input_id": "h1",
        "origin": "human",
        "message_id": "h1",
    }


@pytest.mark.asyncio
async def test_trusted_restore_rejects_incoherent_deferred_and_anchor_pruned_records_atomically():
    source = SimpleContextManager(compaction_notice_enabled=False)
    source_assembly = _assembly(source)
    source_assembly.register("producer", stable_order=0).publish(
        "startup",
        "deferred startup",
        target={"kind": "first_eligible_turn", "placement": "before_human"},
        retain_history=True,
    )
    symbolic = await source.get_messages()
    restored = SimpleContextManager(compaction_notice_enabled=False)
    _assembly(restored)

    missing_origin = copy.deepcopy(symbolic)
    del missing_origin[0]["metadata"]["amplifier:instruction"]["deferred_origin"]
    with pytest.raises(InstructionAssemblyError, match="requires deferred_origin"):
        await restored.restore_host_checkpoint(missing_origin)
    assert restored.messages == []

    for disposition in ("delivered", "anchor_pruned"):
        unresolved = copy.deepcopy(symbolic)
        unresolved[0]["metadata"]["amplifier:instruction"]["disposition"] = disposition
        with pytest.raises(InstructionAssemblyError, match="unresolved deferred"):
            await restored.restore_host_checkpoint(unresolved)
        assert restored.messages == []

    head = {"session_id": "logical", "kind": "conversation_head"}
    source = SimpleContextManager(compaction_notice_enabled=False)
    source_assembly = _assembly(source)
    source_assembly.register("producer", stable_order=0).publish(
        "head", "head fixed", target=head, retain_history=True
    )
    pruned_head = copy.deepcopy(await source.get_messages())
    pruned_head[0]["metadata"]["amplifier:instruction"]["disposition"] = "anchor_pruned"
    with pytest.raises(InstructionAssemblyError, match="anchor_pruned"):
        await restored.restore_host_checkpoint(pruned_head)
    assert restored.messages == []

    anchor = await _input(source, source_assembly, "h1", "human", "input")
    source_assembly.register("tail-source", stable_order=1).publish(
        "tail", "tail fixed", target={"after_message_id": anchor["message_id"]}, retain_history=True
    )
    deferred_tail = await source.get_messages()
    deferred_tail[-1]["metadata"]["amplifier:instruction"]["deferred_origin"] = True
    with pytest.raises(InstructionAssemblyError, match="deferred-origin"):
        await restored.restore_host_checkpoint(deferred_tail)
    assert restored.messages == []


@pytest.mark.asyncio
async def test_trusted_restore_rejects_an_instruction_targeting_forged_user_provenance():
    source = SimpleContextManager(compaction_notice_enabled=False)
    source_assembly = _assembly(source)
    anchor = await _input(source, source_assembly, "h1", "human", "actual input")
    source_assembly.register("producer", stable_order=0).publish(
        "notice",
        "fixed beside h1",
        target=anchor,
        retain_history=True,
    )
    saved = await source.get_messages()
    saved[0]["role"] = "assistant"

    restored = SimpleContextManager(compaction_notice_enabled=False)
    _assembly(restored)
    with pytest.raises(InstructionAssemblyError, match="target is not in restored context"):
        await restored.restore_host_checkpoint(saved)
    assert restored.messages == []

    invalid_anchor_version = copy.deepcopy(await source.get_messages())
    invalid_anchor_version[1]["metadata"]["amplifier:instruction"]["target"]["version"] = True
    with pytest.raises(InstructionAssemblyError, match="unsupported version"):
        await restored.restore_host_checkpoint(invalid_anchor_version)
    assert restored.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["head", "before_human"])
async def test_retained_deferred_republication_after_restore_reuses_its_bound_anchor(placement):
    source = SimpleContextManager(compaction_notice_enabled=False)
    source_assembly = _assembly(source)
    lease = source_assembly.register("producer")
    symbolic_target = {"kind": "first_eligible_turn", "placement": placement}
    entry_id = lease.publish(
        "startup",
        f"deferred {placement}",
        target=symbolic_target,
        retain_history=True,
    )
    h1 = await _input(source, source_assembly, "h1", "human", "first input")
    await _view(source, source_assembly, "r1", h1)
    saved = await source.get_messages()
    bound_target = copy.deepcopy(saved[0]["metadata"]["amplifier:instruction"]["target"])

    restored = SimpleContextManager(compaction_notice_enabled=False)
    restored_assembly = _assembly(restored)
    await restored.restore_host_checkpoint(saved)
    replacement = restored_assembly.register("producer")

    assert replacement.publish(
        "startup",
        f"deferred {placement}",
        target=symbolic_target,
        retain_history=True,
    ) == entry_id
    descriptor = restored.messages[0]["metadata"]["amplifier:instruction"]
    assert descriptor["target"] == bound_target
    assert sum(
        message.get("metadata", {}).get("amplifier:instruction", {}).get("entry_id") == entry_id
        for message in restored.messages
    ) == 1

    h2 = await _input(restored, restored_assembly, "h2", "human", "second input")
    view = await _view(restored, restored_assembly, "r2", h2)
    contents = [message["content"] for message in view]
    assert contents.count(f"deferred {placement}") == 1
    if placement == "before_human":
        assert contents.index(f"deferred {placement}") + 1 == contents.index("first input")
        with pytest.raises(InstructionAssemblyError, match="different content or target"):
            replacement.publish(
                "startup",
                f"deferred {placement}",
                target=h2,
                retain_history=True,
            )
    else:
        assert contents.index(f"deferred {placement}") < contents.index("first input")
        with pytest.raises(InstructionAssemblyError, match="different content or target"):
            replacement.publish(
                "startup",
                f"deferred {placement}",
                target={"kind": "first_eligible_turn", "placement": "before_human"},
                retain_history=True,
            )
    with pytest.raises(InstructionAssemblyError, match="different content or target"):
        replacement.publish(
            "startup",
            "changed content",
            target=symbolic_target,
            retain_history=True,
        )


@pytest.mark.asyncio
async def test_trusted_restore_defaults_missing_historical_authority_without_mutating_input():
    source = SimpleContextManager(compaction_notice_enabled=False)
    source_assembly = _assembly(source)
    source_assembly.register("producer", stable_order=0).publish(
        "head",
        "historical authority",
        target={"session_id": "logical", "kind": "conversation_head"},
        retain_history=True,
    )
    historical = copy.deepcopy(await source.get_messages())
    del historical[0]["metadata"]["amplifier:instruction"]["authority"]
    original_historical = copy.deepcopy(historical)

    restored = SimpleContextManager(compaction_notice_enabled=False)
    restored_assembly = _assembly(restored)
    await restored.restore_host_checkpoint(historical)

    assert historical == original_historical
    descriptor = restored.messages[0]["metadata"]["amplifier:instruction"]
    assert descriptor["authority"] == "authoritative"
    anchor = await _input(restored, restored_assembly, "h1", "human", "task")
    view = await _view(restored, restored_assembly, "r1", anchor)
    assert next(
        message["metadata"]["amplifier:instruction"]["authority"]
        for message in view
        if message["content"] == "historical authority"
    ) == "authoritative"


@pytest.mark.asyncio
async def test_trusted_restore_preserves_explicit_advisory_authority():
    source = SimpleContextManager(compaction_notice_enabled=False)
    source_assembly = _assembly(source)
    source_assembly.register("producer", stable_order=0).publish(
        "head",
        "advisory authority",
        target={"session_id": "logical", "kind": "conversation_head"},
        retain_history=True,
        authority="advisory",
    )

    restored = SimpleContextManager(compaction_notice_enabled=False)
    _assembly(restored)
    await restored.restore_host_checkpoint(await source.get_messages())

    assert restored.messages[0]["metadata"]["amplifier:instruction"]["authority"] == "advisory"


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", [True, None, 1, "unknown"])
async def test_trusted_restore_rejects_invalid_authority_atomically(authority):
    source = SimpleContextManager(compaction_notice_enabled=False)
    source_assembly = _assembly(source)
    source_assembly.register("producer", stable_order=0).publish(
        "head",
        "fixed authority",
        target={"session_id": "logical", "kind": "conversation_head"},
        retain_history=True,
    )
    malformed = copy.deepcopy(await source.get_messages())
    malformed[0]["metadata"]["amplifier:instruction"]["authority"] = authority

    restored = SimpleContextManager(compaction_notice_enabled=False)
    _assembly(restored)
    with pytest.raises(InstructionAssemblyError, match="authority"):
        await restored.restore_host_checkpoint(malformed)
    assert restored.messages == []


@pytest.mark.asyncio
async def test_trusted_restore_authority_keeps_descriptor_shape_closed():
    source = SimpleContextManager(compaction_notice_enabled=False)
    source_assembly = _assembly(source)
    source_assembly.register("producer", stable_order=0).publish(
        "head",
        "fixed authority",
        target={"session_id": "logical", "kind": "conversation_head"},
        retain_history=True,
        authority="advisory",
    )
    malformed = copy.deepcopy(await source.get_messages())
    malformed[0]["metadata"]["amplifier:instruction"]["unexpected"] = "field"

    restored = SimpleContextManager(compaction_notice_enabled=False)
    _assembly(restored)
    with pytest.raises(InstructionAssemblyError, match="closed shape"):
        await restored.restore_host_checkpoint(malformed)
    assert restored.messages == []