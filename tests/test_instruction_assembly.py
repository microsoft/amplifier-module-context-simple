"""Focused v1 instruction-assembly contract tests.

These tests use a coordinator fake.  They prove context-side ordering,
normalization, filtering, and transaction behavior; provider lowering and
cross-repository loop bindings remain consumer integration work.
"""

from __future__ import annotations

import asyncio
import copy
import time

import pytest

from amplifier_module_context_simple import SimpleContextManager, mount
from amplifier_module_context_simple.instructions import (
    CAPABILITY,
    FILTER_CAPABILITY,
    InstructionAssembly,
    InstructionAssemblyError,
)

_FILTER_CONFIG = {"capability": FILTER_CAPABILITY, "policy_id": "test-policy"}


class _Coordinator:
    def __init__(self) -> None:
        self.capabilities: dict[str, object] = {}
        self.admissions = []

    def register_capability(self, name: str, value: object) -> None:
        self.capabilities[name] = value

    def get_capability(self, name: str):
        return self.capabilities.get(name)

    async def process_hook_result(self, result, event: str, hook_name: str):
        self.admissions.append((result.context_injection, event, hook_name))
        return result


class _MountCoordinator(_Coordinator):
    async def mount(self, name: str, value: object) -> None:
        self.mounted = (name, value)


class _Provider:
    instruction_layout_version = 1


class _FailAfterResponseIngressContext(SimpleContextManager):
    """Exercise an error after normal ingress has already appended a response."""

    fail_response_ingress = False
    yield_response_ingress = False

    async def add_message(self, message):
        await super().add_message(message)
        if self.fail_response_ingress and message.get("role") == "assistant":
            raise RuntimeError("response ingress observer failed")
        if self.yield_response_ingress and message.get("role") == "assistant":
            await asyncio.sleep(0)


class _ReentrantResponseIngressContext(SimpleContextManager):
    """Attempt same-task acceptance again from a response-ingress observer."""

    reentry_request_id: str | None = None
    reentry_error: InstructionAssemblyError | None = None

    async def add_message(self, message):
        await super().add_message(message)
        if self.reentry_request_id is not None and message.get("role") == "assistant":
            request_id = self.reentry_request_id
            self.reentry_request_id = None
            try:
                assert self._instruction_assembly is not None
                await self._instruction_assembly.accept_response(request_id, message)
            except InstructionAssemblyError as error:
                self.reentry_error = error


class _PausedResponseIngressContext(SimpleContextManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.response_stored = asyncio.Event()
        self.allow_response_return = asyncio.Event()

    async def add_message(self, message):
        await super().add_message(message)
        if message.get("role") == "assistant":
            self.response_stored.set()
            await self.allow_response_return.wait()


class _ChildAllowanceAttemptContext(SimpleContextManager):
    child_error: BaseException | None = None

    async def add_message(self, message):
        if message.get("role") == "assistant" and self.child_error is None:
            child = asyncio.create_task(super().add_message(copy.deepcopy(message)))
            self.child_error = (await asyncio.gather(child, return_exceptions=True))[0]
        await super().add_message(message)


class _FailDeliveryObservationContext(SimpleContextManager):
    fail_delivery_observation = False

    def _record_instruction_observation(self, observation):
        if self.fail_delivery_observation and observation.get("phase") == "delivery":
            raise RuntimeError("delivery observer failed")
        super()._record_instruction_observation(observation)


class _Filter:
    async def apply(self, records, request_id, phase):
        return {
            "records": [{**record, "content": record["content"].replace("raw", "filtered")} for record in records],
            "receipt": {
                "capability": FILTER_CAPABILITY,
                "policy_id": "test-policy",
                "request_id": request_id,
                "phase": phase,
            },
        }


class _LegacyFilter:
    def __init__(self, policy_id: str) -> None:
        self.policy_id = policy_id

    async def apply(self, records, request_id, phase):
        return {
            "records": [
                {**record, "content": record["content"].replace("raw", "legacy-filtered")}
                for record in records
            ],
            "receipt": {
                "policy_id": self.policy_id,
                "request_id": request_id,
                "phase": phase,
            },
        }


class _MalformedFilter:
    async def apply(self, records, request_id, phase):
        return {
            "records": [{**record, "metadata": {}} for record in records],
            "receipt": {
                "capability": FILTER_CAPABILITY,
                "policy_id": "test-policy",
                "request_id": request_id,
                "phase": phase,
            },
        }


class _InPlaceFilter:
    async def apply(self, records, request_id, phase):
        records[0]["metadata"]["amplifier:instruction"]["placement"] = "head"
        return {
            "records": records,
            "receipt": {
                "capability": FILTER_CAPABILITY,
                "policy_id": "test-policy",
                "request_id": request_id,
                "phase": phase,
            },
        }


class _NeverFilter:
    async def apply(self, records, request_id, phase):
        await asyncio.Event().wait()


class _BlockingFilter:
    def apply(self, records, request_id, phase):
        time.sleep(0.05)


class _BlockingAsyncFilter:
    async def apply(self, records, request_id, phase):
        time.sleep(0.05)


class _WrongReceiptFilter:
    async def apply(self, records, request_id, phase):
        return {
            "records": records,
            "receipt": {
                "capability": "wrong.capability",
                "policy_id": "wrong-policy",
                "request_id": request_id,
                "phase": phase,
            },
        }


class _RetainedMutationFilter:
    async def apply(self, records, request_id, phase):
        records[0]["metadata"]["amplifier:instruction"]["target"]["message_id"] = "forged"
        return {
            "records": records,
            "receipt": {
                "capability": FILTER_CAPABILITY,
                "policy_id": "test-policy",
                "request_id": request_id,
                "phase": phase,
            },
        }


def _bound_context(
    *,
    filters=(),
    max_tokens=2_000,
    callback_timeout_s=5.0,
    context_type=SimpleContextManager,
):
    context = context_type(
        max_tokens=max_tokens,
        compact_threshold=0.2,
        target_usage=0.1,
        compaction_notice_enabled=False,
    )
    coordinator = _Coordinator()
    assembly = InstructionAssembly(
        context,
        coordinator,
        required_filters=filters,
        session_id="logical-session",
        callback_timeout_s=callback_timeout_s,
    )
    context._instruction_assembly = assembly
    coordinator.register_capability(CAPABILITY, assembly)
    return context, coordinator, assembly


@pytest.mark.asyncio
async def test_i1_mount_advertises_the_structural_capability():
    coordinator = _MountCoordinator()
    await mount(coordinator, {})

    assembly = coordinator.capabilities[CAPABILITY]
    assert coordinator.mounted == ("context", assembly._context)
    lease = assembly.register("producer", stable_order=0)
    assert callable(lease.publish)
    assert callable(lease.retire)
    assert callable(lease.close)
    assert lease.route == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_id", ["legacy-policy", ""])
async def test_legacy_string_filter_configuration_mounts_and_keeps_legacy_receipts(policy_id):
    coordinator = _MountCoordinator()
    with pytest.deprecated_call(match="string instruction_filters"):
        await mount(coordinator, {"instruction_filters": [FILTER_CAPABILITY]})
    context = coordinator.mounted[1]
    assembly = coordinator.capabilities[CAPABILITY]
    coordinator.register_capability(FILTER_CAPABILITY, _LegacyFilter(policy_id))
    assembly.register(
        "producer",
        lambda _scope: [{"key": "state", "content": "raw text", "placement": "before_human"}],
    )
    anchor = await _add_input(context, assembly, "h1", "human", "task")

    view = await _request(context, assembly, "r1", anchor)

    assert "legacy-filtered text" in [message["content"] for message in view]


async def _add_input(context, assembly, input_id: str, origin: str, content: str) -> dict:
    with assembly.input_scope(origin, input_id):
        await context.add_message({"role": "user", "content": content})
    return context.messages[-1]["metadata"]["amplifier:input"]


async def _request(context, assembly, request_id: str, input_anchor: dict):
    async with assembly.turn(f"turn-{request_id}", input_anchor):
        async with assembly.request(
            {
                "turn_id": f"turn-{request_id}",
                "request_id": request_id,
                "llm_step_id": f"step-{request_id}",
                "input_anchor": input_anchor,
                "completed_batches": [],
                "tail_anchor": None,
            },
            _Provider(),
        ):
            return await context.get_messages_for_request()


@pytest.mark.asyncio
async def test_i1_live_instruction_is_refreshed_and_survives_three_compacted_requests():
    context, coordinator, assembly = _bound_context()
    state = {"content": "keep this instruction"}

    async def snapshot(_scope):
        return [{"key": "current", "content": state["content"], "placement": "before_human"}]

    assembly.register("source", snapshot, stable_order=0)
    # Force the legacy history reducer to operate on every prepared base view.
    for i in range(12):
        await context.add_message({"role": "user", "content": "old " + ("x" * 300)})
        await context.add_message({"role": "assistant", "content": "old " + ("x" * 300)})

    for i in range(3):
        anchor = await _add_input(context, assembly, f"h{i}", "human", f"task {i}")
        view = await _request(context, assembly, f"request-{i}", anchor)
        assert [message["content"] for message in view].count("keep this instruction") == 1
        assert context._last_compaction_stats is not None

    # One final admission per final record/request -- no raw or duplicate pass.
    assert [text for text, _, _ in coordinator.admissions].count("keep this instruction") == 3


@pytest.mark.asyncio
async def test_i2_complete_snapshots_replace_prior_state_without_stale_replay():
    context, _, assembly = _bound_context()
    state = [{"key": "mode", "content": "mode A", "placement": "before_human"}]
    assembly.register("modes", lambda _scope: state, stable_order=0)

    h1 = await _add_input(context, assembly, "h1", "human", "first")
    assert "mode A" in [m["content"] for m in await _request(context, assembly, "r1", h1)]

    state[:] = [{"key": "mode", "content": "mode B", "placement": "before_human"}]
    h2 = await _add_input(context, assembly, "h2", "human", "second")
    contents = [m["content"] for m in await _request(context, assembly, "r2", h2)]
    assert "mode B" in contents
    assert "mode A" not in contents

    state[:] = []
    h3 = await _add_input(context, assembly, "h3", "human", "third")
    assert "mode B" not in [m["content"] for m in await _request(context, assembly, "r3", h3)]


@pytest.mark.asyncio
async def test_i4_before_human_uses_explicit_human_provenance_not_newest_synthetic_input():
    context, _, assembly = _bound_context()
    assembly.register(
        "source",
        lambda _scope: [{"key": "state", "content": "current state", "placement": "before_human"}],
        stable_order=0,
    )
    human = await _add_input(context, assembly, "human-1", "human", "human prompt")
    await _add_input(context, assembly, "synthetic-1", "synthetic", "goal continuation")

    view = await _request(context, assembly, "request", human)
    contents = [message["content"] for message in view]
    assert contents.index("current state") + 1 == contents.index("human prompt")


@pytest.mark.asyncio
async def test_c1_filters_then_normalizes_final_text_and_admits_it_once():
    context, coordinator, assembly = _bound_context(filters=(_FILTER_CONFIG,))
    coordinator.register_capability(FILTER_CAPABILITY, _Filter())
    assembly.register(
        "source",
        lambda _scope: [{"key": "state", "content": "raw\x00 value", "placement": "before_human"}],
        stable_order=0,
    )
    anchor = await _add_input(context, assembly, "h1", "human", "task")

    view = await _request(context, assembly, "r1", anchor)
    assert "filtered value" in [message["content"] for message in view]
    assert coordinator.admissions == [("filtered value", "context:instructions", "source")]


@pytest.mark.asyncio
async def test_c2_filter_cannot_change_closed_record_shape():
    context, coordinator, assembly = _bound_context(filters=(_FILTER_CONFIG,))
    coordinator.register_capability(FILTER_CAPABILITY, _MalformedFilter())
    assembly.register(
        "source",
        lambda _scope: [{"key": "state", "content": "text", "placement": "before_human"}],
        stable_order=0,
    )
    anchor = await _add_input(context, assembly, "h1", "human", "task")

    with pytest.raises(InstructionAssemblyError, match="shape|identity"):
        await _request(context, assembly, "r1", anchor)
    assert coordinator.admissions == []


@pytest.mark.asyncio
async def test_c2_filter_cannot_mutate_record_identity_in_place():
    context, coordinator, assembly = _bound_context(filters=(_FILTER_CONFIG,))
    coordinator.register_capability(FILTER_CAPABILITY, _InPlaceFilter())
    assembly.register(
        "source",
        lambda _scope: [{"key": "state", "content": "text", "placement": "before_human"}],
        stable_order=0,
    )
    anchor = await _add_input(context, assembly, "h1", "human", "task")

    with pytest.raises(InstructionAssemblyError, match="identity"):
        await _request(context, assembly, "r1", anchor)
    assert coordinator.admissions == []


@pytest.mark.asyncio
async def test_d3_request_preparation_is_memoized_and_recursive_collection_fails():
    context, _, assembly = _bound_context()
    calls = 0

    async def snapshot(_scope):
        nonlocal calls
        calls += 1
        return [{"key": "state", "content": "once", "placement": "before_human"}]

    assembly.register("source", snapshot, stable_order=0)
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    async with assembly.turn("turn", anchor):
        async with assembly.request(
            {"turn_id": "turn", "request_id": "r1", "llm_step_id": "step", "input_anchor": anchor},
            _Provider(),
        ):
            first = await context.get_messages_for_request()
            second = await context.get_messages_for_request()
            with pytest.raises(InstructionAssemblyError, match="immutable"):
                await context.add_message({"role": "assistant", "content": "late mutation"})
    assert first == second
    assert calls == 1

    async def recursive(_scope):
        await context.get_messages_for_request()
        return []

    assembly.register("recursive", recursive, stable_order=1)
    with pytest.raises(InstructionAssemblyError, match="recursive"):
        await _request(context, assembly, "r2", anchor)


@pytest.mark.asyncio
async def test_d5_final_fit_fails_instead_of_silently_dropping_active_instruction():
    context, _, assembly = _bound_context(max_tokens=12)
    assembly.register(
        "source",
        lambda _scope: [{"key": "large", "content": "x" * 200, "placement": "before_human"}],
        stable_order=0,
    )
    anchor = await _add_input(context, assembly, "h1", "human", "task")

    with pytest.raises(InstructionAssemblyError, match="cannot fit"):
        await _request(context, assembly, "r1", anchor)


@pytest.mark.asyncio
async def test_fixed_and_live_tail_entries_are_positioned_after_their_anchor():
    context, _, assembly = _bound_context()
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    lease = assembly.register(
        "source",
        lambda _scope: [
            {
                "key": "live-tail",
                "content": "live after input",
                "placement": "tail",
                "after": {"after_message_id": "h1"},
            }
        ],
        stable_order=0,
    )
    lease.publish(
        "fixed-tail",
        "fixed after input",
        target={"after_message_id": "h1"},
        retain_history=False,
    )

    view = await _request(context, assembly, "r1", anchor)
    contents = [message["content"] for message in view]
    assert contents.index("task") < contents.index("fixed after input")
    assert contents.index("task") < contents.index("live after input")


@pytest.mark.asyncio
async def test_request_only_fixed_entry_is_removed_only_after_matching_acceptance():
    context, _, assembly = _bound_context()
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    lease = assembly.register("source", stable_order=0)
    lease.publish(
        "one-shot",
        "deliver once",
        target=anchor,
        retain_history=False,
    )
    async with assembly.turn("turn-r1", anchor):
        async with assembly.request(
            {"turn_id": "turn-r1", "request_id": "r1", "llm_step_id": "s1", "input_anchor": anchor},
            _Provider(),
        ):
            assert "deliver once" in [m["content"] for m in await context.get_messages_for_request()]
            await assembly.accept_response("r1", {"role": "assistant", "content": "provider reply"})
    view = await _request(context, assembly, "r2", anchor)
    assert "deliver once" not in [message["content"] for message in view]


@pytest.mark.asyncio
async def test_content_free_observations_distinguish_admission_and_delivery():
    context, _, assembly = _bound_context()
    assembly.register(
        "source",
        lambda _scope: [{"key": "current", "content": "observe me", "placement": "before_human"}],
        stable_order=0,
    )
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    async with assembly.turn("turn-r1", anchor):
        async with assembly.request(
            {"turn_id": "turn-r1", "request_id": "r1", "llm_step_id": "s1", "input_anchor": anchor},
            _Provider(),
        ):
            await context.get_messages_for_request()
            await assembly.accept_response("r1", {"role": "assistant", "content": "provider reply"})

    assert context._instruction_observations == [
        {
            "phase": "admission",
            "request_id": "r1",
            "source": "source",
            "key": "current",
            "placement": "before_human",
            "delivery": "pending",
        },
        {"phase": "delivery", "request_id": "r1", "delivery": "accepted"},
    ]
    assert all("content" not in item for item in context._instruction_observations)


@pytest.mark.asyncio
async def test_accept_response_stores_tool_reply_before_delivery_and_next_request():
    context, _, assembly = _bound_context()
    lease = assembly.register("source", stable_order=0)
    h1 = await _add_input(context, assembly, "h1", "human", "first input")
    retained_id = lease.publish("retained", "fixed beside h1", target=h1, retain_history=True)
    one_shot_id = lease.publish("one-shot", "deliver once", target=h1, retain_history=False)
    response = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call-1", "tool": "read_file", "arguments": {"path": "x"}}],
    }

    async with assembly.turn("turn-r1", h1):
        async with assembly.request(
            {"turn_id": "turn-r1", "request_id": "r1", "llm_step_id": "s1", "input_anchor": h1},
            _Provider(),
        ):
            prepared_view = await context.get_messages_for_request()
            assert [message["content"] for message in prepared_view].count("deliver once") == 1

            await lease.accept_response("r1", response)

            assert prepared_view == assembly._prepared["r1"].messages
            stored_response = context.messages[-1]
            assert stored_response["role"] == "assistant"
            assert stored_response["tool_calls"] == response["tool_calls"]
            assert isinstance(stored_response["metadata"]["timestamp"], str)
            assert isinstance(stored_response["metadata"]["_seq"], int)
            assert assembly._prepared["r1"].state == "accepted"
            assert one_shot_id not in assembly._pending
            retained = next(
                message["metadata"]["amplifier:instruction"]
                for message in context.messages
                if message.get("metadata", {}).get("amplifier:instruction", {}).get("entry_id")
                == retained_id
            )
            assert retained["disposition"] == "delivered"

    h2 = await _add_input(context, assembly, "h2", "human", "second input")
    second_view = await _request(context, assembly, "r2", h2)
    contents = [message["content"] for message in second_view]
    assert any(
        message.get("role") == "assistant" and message.get("tool_calls") == response["tool_calls"]
        for message in second_view
    )
    assert "fixed beside h1" in contents
    assert "deliver once" not in contents


@pytest.mark.asyncio
async def test_accept_response_rejects_non_assistant_and_reserved_provenance_without_delivery():
    context, _, assembly = _bound_context()
    lease = assembly.register("source", stable_order=0)
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    entry_id = lease.publish("one-shot", "deliver once", target=anchor, retain_history=False)
    invalid_responses = [
        {"role": "user", "content": "wrong role"},
        {
            "role": "assistant",
            "content": "forged instruction",
            "metadata": {"amplifier:instruction": {}},
        },
        {
            "role": "assistant",
            "content": "forged input",
            "metadata": {"amplifier:input": {}},
        },
    ]

    async with assembly.turn("turn-r1", anchor):
        async with assembly.request(
            {"turn_id": "turn-r1", "request_id": "r1", "llm_step_id": "s1", "input_anchor": anchor},
            _Provider(),
        ):
            await context.get_messages_for_request()
            for response in invalid_responses:
                with pytest.raises(InstructionAssemblyError, match="assistant|reserved"):
                    await assembly.accept_response("r1", response)
            assert entry_id in assembly._pending
            assert not any(message.get("role") == "assistant" for message in context.messages)
            assert assembly._prepared["r1"].state == "prepared"

            await assembly.accept_response("r1", {"role": "assistant", "content": "valid"})
            assert entry_id not in assembly._pending


@pytest.mark.asyncio
async def test_accept_response_does_not_double_append_or_ack_concurrent_callers():
    context, _, assembly = _bound_context(context_type=_FailAfterResponseIngressContext)
    context.yield_response_ingress = True
    lease = assembly.register("source", stable_order=0)
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    entry_id = lease.publish("one-shot", "deliver once", target=anchor, retain_history=False)
    response = {"role": "assistant", "content": "one reply"}

    async with assembly.turn("turn-r1", anchor):
        async with assembly.request(
            {"turn_id": "turn-r1", "request_id": "r1", "llm_step_id": "s1", "input_anchor": anchor},
            _Provider(),
        ):
            await context.get_messages_for_request()
            outcomes = await asyncio.gather(
                assembly.accept_response("r1", response),
                assembly.accept_response("r1", response),
                return_exceptions=True,
            )
            with pytest.raises(InstructionAssemblyError, match="accepted"):
                await assembly.accept_response("r1", response)

    assert sum(outcome is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome, InstructionAssemblyError) for outcome in outcomes) == 1
    assert [message["content"] for message in context.messages].count("one reply") == 1
    assert entry_id not in assembly._pending
    assert context._instruction_observations[-1] == {
        "phase": "delivery",
        "request_id": "r1",
        "delivery": "accepted",
    }


@pytest.mark.asyncio
async def test_accept_response_rejects_same_task_reentry_without_blocking_delivery():
    context, _, assembly = _bound_context(context_type=_ReentrantResponseIngressContext)
    lease = assembly.register("source", stable_order=0)
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    entry_id = lease.publish("one-shot", "deliver once", target=anchor, retain_history=False)
    response = {"role": "assistant", "content": "one reply"}

    async with assembly.turn("turn-r1", anchor):
        async with assembly.request(
            {"turn_id": "turn-r1", "request_id": "r1", "llm_step_id": "s1", "input_anchor": anchor},
            _Provider(),
        ):
            await context.get_messages_for_request()
            context.reentry_request_id = "r1"
            await assembly.accept_response("r1", response)

    assert isinstance(context.reentry_error, InstructionAssemblyError)
    assert "cannot re-enter" in str(context.reentry_error)
    assert [message["content"] for message in context.messages].count("one reply") == 1
    assert entry_id not in assembly._pending


@pytest.mark.asyncio
async def test_accept_response_records_partial_storage_and_retries_without_duplicate():
    context = _FailAfterResponseIngressContext(compaction_notice_enabled=False)
    coordinator = _Coordinator()
    assembly = InstructionAssembly(context, coordinator, session_id="logical-session")
    context._instruction_assembly = assembly
    lease = assembly.register("source", stable_order=0)
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    entry_id = lease.publish("one-shot", "deliver once", target=anchor, retain_history=False)
    response = {"role": "assistant", "content": "stored once"}

    async with assembly.turn("turn-r1", anchor):
        async with assembly.request(
            {"turn_id": "turn-r1", "request_id": "r1", "llm_step_id": "s1", "input_anchor": anchor},
            _Provider(),
        ):
            await context.get_messages_for_request()
            context.fail_response_ingress = True
            with pytest.raises(RuntimeError, match="observer failed"):
                await assembly.accept_response("r1", response)
            assert assembly._prepared["r1"].state == "stored"
            assert entry_id in assembly._pending
            assert [message["content"] for message in context.messages].count("stored once") == 1

            context.fail_response_ingress = False
            with pytest.raises(InstructionAssemblyError, match="different response"):
                await assembly.accept_response("r1", {"role": "assistant", "content": "different reply"})
            assert [message["content"] for message in context.messages].count("stored once") == 1
            await assembly.accept_response("r1", response)
            assert assembly._prepared["r1"].state == "accepted"
            assert entry_id not in assembly._pending
            assert [message["content"] for message in context.messages].count("stored once") == 1


@pytest.mark.asyncio
async def test_accept_response_blocks_unrelated_context_mutation_until_delivery_commits():
    context, _, assembly = _bound_context(context_type=_PausedResponseIngressContext)
    lease = assembly.register("source", stable_order=0)
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    entry_id = lease.publish("one-shot", "deliver once", target=anchor, retain_history=False)

    async with assembly.turn("turn-r1", anchor):
        async with assembly.request(
            {"turn_id": "turn-r1", "request_id": "r1", "llm_step_id": "s1", "input_anchor": anchor},
            _Provider(),
        ):
            prepared = await context.get_messages_for_request()
            acceptance = asyncio.create_task(
                assembly.accept_response("r1", {"role": "assistant", "content": "provider reply"})
            )
            await context.response_stored.wait()
            with pytest.raises(InstructionAssemblyError, match="immutable"):
                await context.clear()
            frozen = await context.get_messages_for_request()
            assert frozen == prepared
            assert "provider reply" not in [message["content"] for message in frozen]
            frozen[0]["content"] = "caller mutation"
            assert await context.get_messages_for_request() == prepared
            assert entry_id in assembly._pending
            context.allow_response_return.set()
            await acceptance

    assert [message["content"] for message in context.messages].count("provider reply") == 1
    assert entry_id not in assembly._pending


@pytest.mark.asyncio
async def test_accept_response_allowance_rejects_child_task_before_owned_ingress():
    context, _, assembly = _bound_context(context_type=_ChildAllowanceAttemptContext)
    lease = assembly.register("source", stable_order=0)
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    entry_id = lease.publish("one-shot", "deliver once", target=anchor, retain_history=False)

    async with assembly.turn("turn-r1", anchor):
        async with assembly.request(
            {"turn_id": "turn-r1", "request_id": "r1", "llm_step_id": "s1", "input_anchor": anchor},
            _Provider(),
        ):
            await context.get_messages_for_request()
            await assembly.accept_response("r1", {"role": "assistant", "content": "provider reply"})

    assert isinstance(context.child_error, InstructionAssemblyError)
    assert [message["content"] for message in context.messages].count("provider reply") == 1
    assert entry_id not in assembly._pending


@pytest.mark.asyncio
async def test_accept_response_keeps_delivery_pending_when_its_observer_fails():
    context, coordinator, assembly = _bound_context(context_type=_FailDeliveryObservationContext)
    lease = assembly.register("source", stable_order=0)
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    entry_id = lease.publish("one-shot", "deliver once", target=anchor, retain_history=False)
    response = {"role": "assistant", "content": "provider reply"}

    async with assembly.turn("turn-r1", anchor):
        async with assembly.request(
            {"turn_id": "turn-r1", "request_id": "r1", "llm_step_id": "s1", "input_anchor": anchor},
            _Provider(),
        ):
            prepared = await context.get_messages_for_request()
            expected_view = copy.deepcopy(prepared)
            admission_count = len(coordinator.admissions)
            context.fail_delivery_observation = True
            with pytest.raises(RuntimeError, match="delivery observer failed"):
                await assembly.accept_response("r1", response)
            assert assembly._prepared["r1"].state == "stored"
            assert entry_id in assembly._pending
            assert [message["content"] for message in context.messages].count("provider reply") == 1

            # A stored response never changes the provider-facing request
            # snapshot, and rereading it never recollects or readmits records.
            reread = await context.get_messages_for_request()
            assert reread == expected_view
            assert "provider reply" not in [message["content"] for message in reread]
            assert len(coordinator.admissions) == admission_count
            reread[0]["content"] = "caller mutation"
            reread[0].setdefault("metadata", {})["caller_mutation"] = True
            assert await context.get_messages_for_request() == expected_view

            context.fail_delivery_observation = False
            await assembly.accept_response("r1", response)
            assert entry_id not in assembly._pending
            assert [message["content"] for message in context.messages].count("provider reply") == 1


@pytest.mark.asyncio
async def test_registration_uses_explicit_stable_order_and_cannot_change_a_prepared_request():
    context, _, assembly = _bound_context()
    assembly.register(
        "late",
        lambda _scope: [{"key": "late", "content": "late", "placement": "before_human"}],
        stable_order=20,
    )
    assembly.register(
        "early",
        lambda _scope: [{"key": "early", "content": "early", "placement": "before_human"}],
        stable_order=10,
    )
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    async with assembly.turn("turn-r1", anchor):
        async with assembly.request(
            {"turn_id": "turn-r1", "request_id": "r1", "llm_step_id": "s1", "input_anchor": anchor},
            _Provider(),
        ):
            first = await context.get_messages_for_request()
            assert [message["content"] for message in first].index("early") < [
                message["content"] for message in first
            ].index("late")
            with pytest.raises(InstructionAssemblyError, match="immutable"):
                assembly.register("new", stable_order=30)
            assert await context.get_messages_for_request() == first


@pytest.mark.asyncio
async def test_omitted_source_orders_follow_explicit_orders_then_source_id():
    context, _, assembly = _bound_context()
    for source_id, stable_order in (
        ("zeta", None),
        ("late", 20),
        ("alpha", None),
        ("early", 10),
    ):
        assembly.register(
            source_id,
            lambda _scope, source_id=source_id: [
                {"key": source_id, "content": source_id, "placement": "before_human"}
            ],
            stable_order=stable_order,
        )
    anchor = await _add_input(context, assembly, "h1", "human", "task")

    view = await _request(context, assembly, "r1", anchor)
    contents = [message["content"] for message in view]

    assert [content for content in contents if content in {"early", "late", "alpha", "zeta"}] == [
        "early",
        "late",
        "alpha",
        "zeta",
    ]


@pytest.mark.asyncio
async def test_registration_from_a_snapshot_callback_is_rejected():
    context, _, assembly = _bound_context()

    async def snapshot(_scope):
        with pytest.raises(InstructionAssemblyError, match="callbacks"):
            assembly.register("new", stable_order=1)
        return [{"key": "safe", "content": "safe", "placement": "before_human"}]

    assembly.register("source", snapshot, stable_order=0)
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    assert "safe" in [message["content"] for message in await _request(context, assembly, "r1", anchor)]
    assert "new" not in assembly._sources


@pytest.mark.asyncio
async def test_lease_close_from_a_snapshot_callback_cannot_mutate_pending_records():
    context, _, assembly = _bound_context()
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    other = assembly.register("other", stable_order=1)
    entry_id = other.publish("pending", "must remain", target=anchor, retain_history=False)

    async def snapshot(_scope):
        with pytest.raises(InstructionAssemblyError, match="callbacks"):
            other.close()
        return [{"key": "safe", "content": "safe", "placement": "before_human"}]

    assembly.register("source", snapshot, stable_order=0)
    assert "must remain" in [
        message["content"] for message in await _request(context, assembly, "r1", anchor)
    ]
    assert entry_id in assembly._pending


@pytest.mark.asyncio
async def test_required_snapshot_and_filter_callbacks_time_out():
    context, _, assembly = _bound_context(callback_timeout_s=0.01)

    async def never_snapshot(_scope):
        await asyncio.Event().wait()

    assembly.register("source", never_snapshot, stable_order=0)
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    with pytest.raises(InstructionAssemblyError, match="snapshot callback.*timed out"):
        await _request(context, assembly, "snapshot-timeout", anchor)

    context, coordinator, assembly = _bound_context(
        filters=(_FILTER_CONFIG,), callback_timeout_s=0.01
    )
    coordinator.register_capability(FILTER_CAPABILITY, _NeverFilter())
    assembly.register(
        "source",
        lambda _scope: [{"key": "safe", "content": "safe", "placement": "before_human"}],
        stable_order=0,
    )
    anchor = await _add_input(context, assembly, "h2", "human", "task")
    with pytest.raises(InstructionAssemblyError, match="instruction filter.*timed out"):
        await _request(context, assembly, "filter-timeout", anchor)


@pytest.mark.asyncio
async def test_required_synchronous_snapshot_and_filter_callbacks_time_out():
    context, _, assembly = _bound_context(callback_timeout_s=0.01)

    def blocking_snapshot(_scope):
        time.sleep(0.05)

    assembly.register("source", blocking_snapshot, stable_order=0)
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    with pytest.raises(InstructionAssemblyError, match="snapshot callback.*timed out"):
        await _request(context, assembly, "sync-snapshot-timeout", anchor)

    context, coordinator, assembly = _bound_context(
        filters=(_FILTER_CONFIG,), callback_timeout_s=0.01
    )
    coordinator.register_capability(FILTER_CAPABILITY, _BlockingFilter())
    assembly.register(
        "source",
        lambda _scope: [{"key": "safe", "content": "safe", "placement": "before_human"}],
        stable_order=0,
    )
    anchor = await _add_input(context, assembly, "h2", "human", "task")
    with pytest.raises(InstructionAssemblyError, match="instruction filter.*timed out"):
        await _request(context, assembly, "sync-filter-timeout", anchor)


@pytest.mark.asyncio
async def test_required_blocking_async_snapshot_and_filter_callbacks_time_out():
    context, _, assembly = _bound_context(callback_timeout_s=0.01)

    async def blocking_snapshot(_scope):
        time.sleep(0.05)

    assembly.register("source", blocking_snapshot, stable_order=0)
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    with pytest.raises(InstructionAssemblyError, match="snapshot callback.*timed out"):
        await _request(context, assembly, "blocking-async-snapshot", anchor)

    context, coordinator, assembly = _bound_context(
        filters=(_FILTER_CONFIG,), callback_timeout_s=0.01
    )
    coordinator.register_capability(FILTER_CAPABILITY, _BlockingAsyncFilter())
    assembly.register(
        "source",
        lambda _scope: [{"key": "safe", "content": "safe", "placement": "before_human"}],
        stable_order=0,
    )
    anchor = await _add_input(context, assembly, "h2", "human", "task")
    with pytest.raises(InstructionAssemblyError, match="instruction filter.*timed out"):
        await _request(context, assembly, "blocking-async-filter", anchor)


@pytest.mark.asyncio
async def test_filter_configuration_requires_last_redaction_and_bound_receipt():
    with pytest.raises(InstructionAssemblyError, match="redaction.*last"):
        _bound_context(
            filters=(
                _FILTER_CONFIG,
                {"capability": "other.filter", "policy_id": "other-policy"},
            )
        )

    context, coordinator, assembly = _bound_context(filters=(_FILTER_CONFIG,))
    coordinator.register_capability(FILTER_CAPABILITY, _WrongReceiptFilter())
    assembly.register(
        "source",
        lambda _scope: [{"key": "safe", "content": "safe", "placement": "before_human"}],
        stable_order=0,
    )
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    with pytest.raises(InstructionAssemblyError, match="receipt"):
        await _request(context, assembly, "r1", anchor)


@pytest.mark.asyncio
async def test_retained_record_isolated_from_rejected_filter_nested_mutation():
    context, coordinator, assembly = _bound_context(filters=(_FILTER_CONFIG,))
    coordinator.register_capability(FILTER_CAPABILITY, _RetainedMutationFilter())
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    assembly.register("source", stable_order=0).publish(
        "fixed", "fixed beside h1", target=anchor, retain_history=True
    )

    with pytest.raises(InstructionAssemblyError, match="identity"):
        await _request(context, assembly, "r1", anchor)
    stored = next(
        message["metadata"]["amplifier:instruction"]
        for message in context.messages
        if message.get("content") == "fixed beside h1"
    )
    assert stored["placement"] == "before_human"
    assert stored["target"]["message_id"] == "h1"

    coordinator.register_capability(FILTER_CAPABILITY, _Filter())
    view = await _request(context, assembly, "r2", anchor)
    assert [message["content"] for message in view].index("fixed beside h1") + 1 == [
        message["content"] for message in view
    ].index("task")


@pytest.mark.asyncio
async def test_system_prompt_factory_is_versioned_and_frozen_with_prepared_requests():
    context, _, assembly = _bound_context()

    async def first_factory():
        return "first"

    version = context._instruction_mutation_version
    await context.set_system_prompt_factory(first_factory)
    assert context._instruction_mutation_version == version + 1
    assembly.register(
        "source",
        lambda _scope: [{"key": "safe", "content": "safe", "placement": "before_human"}],
        stable_order=0,
    )
    anchor = await _add_input(context, assembly, "h1", "human", "task")
    async with assembly.turn("turn-r1", anchor):
        async with assembly.request(
            {"turn_id": "turn-r1", "request_id": "r1", "llm_step_id": "s1", "input_anchor": anchor},
            _Provider(),
        ):
            await context.get_messages_for_request()
            with pytest.raises(InstructionAssemblyError, match="immutable"):
                await context.set_system_prompt_factory(first_factory)
