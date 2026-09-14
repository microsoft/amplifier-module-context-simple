"""Opt-in v1 assembly for module-owned system instructions.

This module deliberately contains no producer state. Producers register a
callback for their current snapshot and retain their own state; fixed records
are ordinary context messages so a host can checkpoint them through
``get_messages`` / ``restore_host_checkpoint``.
"""

from __future__ import annotations

import asyncio
import copy
import contextvars
import inspect
import math
import threading
import warnings
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator

from amplifier_core import HookResult, ToolResult


CAPABILITY = "context.instructions.v1"
FILTER_CAPABILITY = "context.instructions.filter.v1/redaction"
INSTRUCTION_METADATA = "amplifier:instruction"
INPUT_METADATA = "amplifier:input"
AUTHORITATIVE = "authoritative"
ADVISORY = "advisory"
_AUTHORITIES = frozenset({AUTHORITATIVE, ADVISORY})
_ACTIVE_CALLBACK_REQUEST: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "active_instruction_callback_request", default=None
)
_ACTIVE_RESPONSE_APPEND: contextvars.ContextVar["_ResponseAppendAllowance | None"] = (
    contextvars.ContextVar("active_instruction_response_append", default=None)
)


class InstructionAssemblyError(RuntimeError):
    """An active v1 request cannot safely be assembled."""


def _validate_authority(authority: Any, label: str = "instruction authority") -> str:
    if not isinstance(authority, str) or authority not in _AUTHORITIES:
        raise InstructionAssemblyError(f"{label} must be 'authoritative' or 'advisory'")
    return authority


def is_instruction_message(message: dict[str, Any]) -> bool:
    """Whether ``message`` is a v1 fixed record, including a terminal one."""
    return isinstance((message.get("metadata") or {}).get(INSTRUCTION_METADATA), dict)


def has_instruction_descriptor(message: dict[str, Any]) -> bool:
    """Whether a message carries any claimed v1 descriptor."""
    metadata = message.get("metadata")
    return isinstance(metadata, dict) and INSTRUCTION_METADATA in metadata


def has_input_descriptor(message: dict[str, Any]) -> bool:
    """Whether a message carries claimed v1 input provenance."""
    metadata = message.get("metadata")
    return isinstance(metadata, dict) and INPUT_METADATA in metadata


async def _await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _message_id(message: dict[str, Any]) -> str | None:
    metadata = message.get("metadata") or {}
    input_meta = metadata.get(INPUT_METADATA)
    if isinstance(input_meta, dict) and isinstance(input_meta.get("message_id"), str):
        return input_meta["message_id"]
    for key in ("message_id", "id"):
        if isinstance(message.get(key), str):
            return message[key]
        if isinstance(metadata.get(key), str):
            return metadata[key]
    return None


def _input_anchor(message: dict[str, Any]) -> dict[str, str] | None:
    if message.get("role") != "user":
        return None
    metadata = message.get("metadata") or {}
    anchor = metadata.get(INPUT_METADATA)
    if not isinstance(anchor, dict):
        return None
    if (
        type(anchor.get("version")) is not int
        or anchor.get("version") != 1
        or not isinstance(anchor.get("input_id"), str)
        or anchor.get("origin") not in {"human", "delegation", "synthetic"}
        or not isinstance(anchor.get("message_id"), str)
    ):
        return None
    return {
        "input_id": anchor["input_id"],
        "origin": anchor["origin"],
        "message_id": anchor["message_id"],
    }


def _fixed_descriptor(message: dict[str, Any]) -> dict[str, Any] | None:
    descriptor = (message.get("metadata") or {}).get(INSTRUCTION_METADATA)
    if (
        not isinstance(descriptor, dict)
        or isinstance(descriptor.get("version"), bool)
        or descriptor.get("version") != 1
    ):
        return None
    required = (
        "source",
        "key",
        "binding",
        "placement",
        "entry_id",
        "event_key",
        "session_id",
        "target",
        "order",
        "disposition",
    )
    if any(key not in descriptor for key in required):
        return None
    if descriptor["binding"] != "fixed":
        return None
    return descriptor


def _terminal(descriptor: dict[str, Any]) -> bool:
    return descriptor.get("disposition") in {"anchor_pruned", "retired"}


def _target_kind(target: Any) -> str:
    if isinstance(target, dict):
        if (
            set(target) == {"kind", "placement"}
            and target.get("kind") == "first_eligible_turn"
            and target.get("placement") in {"head", "before_human"}
        ):
            return "deferred"
        if target.get("kind") == "conversation_head":
            return "head"
        if {"input_id", "message_id", "origin"} <= set(target):
            return "before_human"
        if isinstance(target.get("after_message_id"), str):
            return "tail"
    raise InstructionAssemblyError("fixed instruction target is invalid or incomplete")


def _validate_snapshot_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise InstructionAssemblyError("instruction snapshot entries must be mappings")
    allowed = {"key", "content", "placement", "after", "authority"}
    if set(item) - allowed or not {"key", "content", "placement"} <= set(item):
        raise InstructionAssemblyError("instruction snapshot entry has an invalid shape")
    if not isinstance(item["key"], str) or not item["key"]:
        raise InstructionAssemblyError("instruction snapshot key must be a non-empty string")
    if not isinstance(item["content"], str):
        raise InstructionAssemblyError("instruction snapshot content must be text")
    if item["placement"] not in {"head", "before_human", "tail"}:
        raise InstructionAssemblyError("instruction snapshot placement is invalid")
    normalized = dict(item)
    normalized["authority"] = _validate_authority(
        normalized.get("authority", AUTHORITATIVE), "instruction snapshot authority"
    )
    return normalized


@dataclass
class _RegisteredSource:
    source_id: str
    callback: Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]]] | list[dict[str, Any]]] | None
    generation: int
    stable_order: int | None
    order: int


@dataclass(frozen=True)
class _RequiredFilter:
    capability: str
    policy_id: str | None
    legacy: bool


@dataclass
class _Prepared:
    state: str
    messages: list[dict[str, Any]] | None = None
    rendered_entries: list[str] | None = None
    task: asyncio.Task[None] | None = None
    acceptance_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    accepting_task: asyncio.Task[Any] | None = None
    response_message: dict[str, Any] | None = None
    response_seq: int | None = None


@dataclass
class _ResponseAppendAllowance:
    """One request-bound ordinary-ingress append, consumed before admission."""

    request_id: str
    response_message: dict[str, Any]
    accepting_task: asyncio.Task[Any] | None
    consumed: bool = False


class InstructionLease:
    """Source-bound public handle returned by :meth:`InstructionAssembly.register`."""

    def __init__(self, assembly: InstructionAssembly, source: _RegisteredSource) -> None:
        self._assembly = assembly
        self._source = source

    @property
    def route(self) -> str:
        return self._assembly.route

    def publish(
        self,
        event_key: str,
        instruction: str,
        *,
        target: dict[str, Any] | str,
        retain_history: bool = False,
        authority: str = AUTHORITATIVE,
    ) -> str:
        return self._assembly._publish(
            self._source,
            event_key,
            instruction,
            target=target,
            retain_history=retain_history,
            authority=authority,
        )

    def retire(self, event_key: str, reason: str) -> None:
        self._assembly._retire(self._source, event_key, reason)

    def close(self) -> None:
        self._assembly._close(self._source)

    def input_scope(self, origin: str, input_id: str):
        return self._assembly.input_scope(origin, input_id)

    def turn(self, turn_id: str, input_boundary: dict[str, Any] | None = None):
        return self._assembly.turn(turn_id, input_boundary)

    def request(self, request_scope: dict[str, Any], selected_provider: Any):
        return self._assembly.request(request_scope, selected_provider)

    async def accept_response(self, request_id: str, response_message: dict[str, Any]) -> None:
        await self._assembly.accept_response(request_id, response_message)


class InstructionAssembly:
    """Context-local registry, placement resolver, and request transaction."""

    # Consumers require this explicit counterpart to the provider capability
    # before sending descriptors that carry an authority field.
    instruction_layout_authority_v1 = True

    def __init__(
        self,
        context: Any,
        coordinator: Any,
        *,
        required_filters: list[dict[str, str] | str] | tuple[dict[str, str] | str, ...] = (),
        session_id: str = "context",
        callback_timeout_s: float = 5.0,
        callback_max_workers: int = 4,
    ) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise InstructionAssemblyError("instruction session_id must be a non-empty string")
        if (
            isinstance(callback_timeout_s, bool)
            or not isinstance(callback_timeout_s, (int, float))
            or not math.isfinite(callback_timeout_s)
            or callback_timeout_s <= 0
        ):
            raise InstructionAssemblyError("instruction callback timeout must be finite and greater than zero")
        if (
            isinstance(callback_max_workers, bool)
            or not isinstance(callback_max_workers, int)
            or callback_max_workers <= 0
        ):
            raise InstructionAssemblyError(
                "instruction callback max workers must be a positive integer"
            )
        self._context = context
        self._coordinator = coordinator
        self._required_filters = self._normalize_required_filters(required_filters)
        self._callback_timeout_s = float(callback_timeout_s)
        self._callback_workers = threading.BoundedSemaphore(callback_max_workers)
        self._session_id = session_id
        self._sources: dict[str, _RegisteredSource] = {}
        self._source_generation = 0
        self._fixed_order = 0
        self.rebase_fixed_order()
        self._pending: dict[str, dict[str, Any]] = {}
        self._prepared: dict[str, _Prepared] = {}
        self._current_input: dict[str, str] | None = None
        self._current_turn: dict[str, Any] | None = None
        self._current_request: dict[str, Any] | None = None
        self._route = "pending"
        self._preparing = False

    @property
    def route(self) -> str:
        """Read-only negotiated route: ``pending``, ``v1``, or ``legacy``."""
        return self._route

    @property
    def current_request(self) -> dict[str, Any] | None:
        return self._current_request

    @property
    def active(self) -> bool:
        return self._current_request is not None and self._route == "v1"

    @staticmethod
    def _normalize_required_filters(
        required_filters: list[dict[str, str] | str] | tuple[dict[str, str] | str, ...],
    ) -> tuple[_RequiredFilter, ...]:
        normalized: list[_RequiredFilter] = []
        for configured in required_filters:
            if isinstance(configured, str):
                if not configured:
                    raise InstructionAssemblyError(
                        "instruction filter capability must be a non-empty string"
                    )
                warnings.warn(
                    "string instruction_filters entries are deprecated; configure "
                    "{'capability', 'policy_id'} for a bound receipt",
                    DeprecationWarning,
                    stacklevel=3,
                )
                normalized.append(
                    _RequiredFilter(
                        capability=configured,
                        policy_id=None,
                        legacy=True,
                    )
                )
                continue
            if not isinstance(configured, dict) or set(configured) != {"capability", "policy_id"}:
                raise InstructionAssemblyError(
                    "required instruction filters must contain capability and policy_id"
                )
            capability = configured["capability"]
            policy_id = configured["policy_id"]
            if not isinstance(capability, str) or not capability:
                raise InstructionAssemblyError("instruction filter capability must be a non-empty string")
            if not isinstance(policy_id, str) or not policy_id:
                raise InstructionAssemblyError("instruction filter policy_id must be a non-empty string")
            if any(
                existing.capability == capability and not existing.legacy
                for existing in normalized
            ):
                raise InstructionAssemblyError(f"instruction filter {capability!r} is configured twice")
            normalized.append(
                _RequiredFilter(
                    capability=capability,
                    policy_id=policy_id,
                    legacy=False,
                )
            )
        redaction_indexes = [
            index
            for index, configured in enumerate(normalized)
            if configured.capability == FILTER_CAPABILITY
        ]
        if redaction_indexes and redaction_indexes != [len(normalized) - 1]:
            raise InstructionAssemblyError("required redaction filter must be configured last")
        return tuple(normalized)

    def rebase_fixed_order(self) -> None:
        """Set new fixed publications after every validated restored peer."""
        self._fixed_order = max(
            (
                descriptor["order"]
                for message in self._context.messages
                if (descriptor := _fixed_descriptor(message)) is not None
                and isinstance(descriptor.get("order"), int)
                and not isinstance(descriptor.get("order"), bool)
            ),
            default=0,
        )

    def validate_trusted_restore(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate and detach v1 records before the host restores them."""
        if not isinstance(messages, list):
            raise InstructionAssemblyError("restored messages must be a list")
        restored = copy.deepcopy(messages)
        entry_ids: set[str] = set()
        for message in restored:
            if not isinstance(message, dict) or not has_instruction_descriptor(message):
                continue
            self._validate_trusted_fixed_message(message, restored, entry_ids)
        return restored

    def _validate_trusted_fixed_message(
        self, message: dict[str, Any], messages: list[dict[str, Any]], entry_ids: set[str]
    ) -> None:
        descriptor = (message.get("metadata") or {}).get(INSTRUCTION_METADATA)
        if not isinstance(descriptor, dict):
            raise InstructionAssemblyError("trusted restored instruction descriptor must be a mapping")
        # Authority was added after v1 first shipped. Trusted historical
        # records intentionally retain their original meaning rather than
        # silently becoming advisory.
        descriptor.setdefault("authority", AUTHORITATIVE)
        expected = {
            "version",
            "source",
            "key",
            "binding",
            "placement",
            "authority",
            "entry_id",
            "event_key",
            "session_id",
            "target",
            "order",
            "disposition",
        }
        if descriptor.get("deferred_origin") is True:
            expected.add("deferred_origin")
        terminal = descriptor.get("disposition")
        if terminal == "retired":
            expected.add("retire_reason")
        if set(descriptor) != expected:
            raise InstructionAssemblyError("trusted restored instruction descriptor has an invalid closed shape")
        if message.get("role") != "system" or not isinstance(message.get("content"), str) or not message["content"]:
            raise InstructionAssemblyError("trusted restored instruction must be canonical non-empty system text")
        if (
            isinstance(descriptor.get("version"), bool)
            or descriptor.get("version") != 1
            or descriptor.get("binding") != "fixed"
            or descriptor.get("placement") not in {"head", "before_human", "tail"}
            or descriptor.get("disposition") not in {"pending", "delivered", "retired", "anchor_pruned"}
        ):
            raise InstructionAssemblyError("trusted restored instruction descriptor has invalid fixed fields")
        _validate_authority(descriptor.get("authority"), "trusted restored instruction authority")
        if "deferred_origin" in descriptor and descriptor["deferred_origin"] is not True:
            raise InstructionAssemblyError("trusted restored instruction deferred origin is invalid")
        for key in ("source", "key", "event_key", "entry_id", "session_id"):
            if not isinstance(descriptor.get(key), str) or not descriptor[key]:
                raise InstructionAssemblyError(f"trusted restored instruction {key} must be a non-empty string")
        if descriptor["session_id"] != self._session_id:
            raise InstructionAssemblyError("trusted restored instruction belongs to another logical session")
        if descriptor["event_key"] != descriptor["key"] or descriptor["entry_id"] != (
            f"{self._session_id}:{descriptor['source']}:{descriptor['key']}"
        ):
            raise InstructionAssemblyError("trusted restored instruction identity is invalid")
        if (
            isinstance(descriptor.get("order"), bool)
            or not isinstance(descriptor.get("order"), int)
            or descriptor["order"] <= 0
        ):
            raise InstructionAssemblyError("trusted restored instruction order is invalid")
        if descriptor["entry_id"] in entry_ids:
            raise InstructionAssemblyError("trusted restored instruction entry_id is duplicated")
        entry_ids.add(descriptor["entry_id"])
        if terminal == "retired" and (
            not isinstance(descriptor.get("retire_reason"), str) or not descriptor["retire_reason"]
        ):
            raise InstructionAssemblyError("trusted retired instruction requires a non-empty reason")

        target = descriptor["target"]
        kind = _target_kind(target)
        if kind == "deferred":
            if descriptor.get("deferred_origin") is not True:
                raise InstructionAssemblyError(
                    "trusted symbolic deferred instruction requires deferred_origin"
                )
            if descriptor["placement"] != target["placement"]:
                raise InstructionAssemblyError("trusted deferred instruction placement does not match target")
            if terminal in {"delivered", "anchor_pruned"}:
                raise InstructionAssemblyError(
                    "trusted unresolved deferred instruction cannot be delivered or anchor_pruned"
                )
            return
        if descriptor["placement"] != kind:
            raise InstructionAssemblyError("trusted instruction placement does not match target")
        if descriptor.get("deferred_origin") is True and kind not in {"head", "before_human"}:
            raise InstructionAssemblyError(
                "trusted deferred-origin instruction must resolve to head or before_human"
            )
        if terminal == "anchor_pruned" and kind == "head":
            raise InstructionAssemblyError(
                "trusted anchor_pruned instruction requires a concrete before_human or tail target"
            )
        if kind == "head":
            if set(target) != {"session_id", "kind"} or target["session_id"] != self._session_id:
                raise InstructionAssemblyError("trusted fixed head target is not this logical conversation")
            return
        if kind == "before_human":
            input_anchor = self._validate_input_anchor_shape(target)
        elif set(target) != {"after_message_id"}:
            raise InstructionAssemblyError("trusted fixed tail target has an invalid closed shape")
        if not _terminal(descriptor) and (
            (kind == "before_human" and not self._has_input_anchor(messages, input_anchor))
            or (kind == "tail" and not self._has_message(messages, target["after_message_id"]))
        ):
            raise InstructionAssemblyError("trusted fixed instruction target is not in restored context")

    def pending_anchor_message_ids(self) -> set[str]:
        """Anchors for pending fixed work that compaction must not discard."""
        protected: set[str] = set()
        for entry in self._all_fixed_entries():
            if entry.get("disposition") != "pending":
                continue
            target = entry.get("target")
            if isinstance(target, dict):
                for key in ("message_id", "after_message_id"):
                    if isinstance(target.get(key), str):
                        protected.add(target[key])
        return protected

    def register(
        self,
        source_id: str,
        snapshot_callback: Callable[
            [dict[str, Any]], Awaitable[list[dict[str, Any]]] | list[dict[str, Any]]
        ]
        | None = None,
        *,
        stable_order: int | None = None,
    ) -> InstructionLease:
        self._assert_not_preparing()
        if not isinstance(source_id, str) or not source_id:
            raise InstructionAssemblyError("source_id must be a non-empty string")
        if stable_order is not None and (
            isinstance(stable_order, bool)
            or not isinstance(stable_order, int)
            or stable_order < 0
        ):
            raise InstructionAssemblyError("instruction source stable_order must be a non-negative integer")
        if source_id in self._sources:
            raise InstructionAssemblyError(f"instruction source {source_id!r} is already active")
        if stable_order is not None and any(
            source.stable_order == stable_order for source in self._sources.values()
        ):
            raise InstructionAssemblyError(
                f"instruction source stable_order {stable_order!r} is already active"
            )
        self._source_generation += 1
        source = _RegisteredSource(
            source_id=source_id,
            callback=snapshot_callback,
            generation=self._source_generation,
            stable_order=stable_order,
            order=0,
        )
        self._sources[source_id] = source
        self._reorder_sources()
        return InstructionLease(self, source)

    def _reorder_sources(self) -> None:
        """Rank explicit source orders before omitted source IDs deterministically."""
        for order, source in enumerate(
            sorted(
                self._sources.values(),
                key=lambda source: (
                    source.stable_order is None,
                    source.stable_order if source.stable_order is not None else 0,
                    source.source_id,
                ),
            )
        ):
            source.order = order

    def _assert_live_source(self, source: _RegisteredSource) -> None:
        if self._sources.get(source.source_id) != source:
            raise InstructionAssemblyError("instruction lease is closed or replaced")

    def _assert_not_preparing(self) -> None:
        if _ACTIVE_CALLBACK_REQUEST.get() is not None or self._preparing:
            raise InstructionAssemblyError(
                "instruction callbacks cannot publish, retire, or mutate context while preparing"
            )
        if self._current_request is not None:
            prepared = self._prepared.get(self._current_request.get("request_id"))
            if prepared is not None and prepared.state in {"prepared", "accepting", "stored"}:
                raise InstructionAssemblyError(
                    "prepared instruction requests are immutable; open a new request_id"
                )

    def assert_context_mutation_allowed(self) -> None:
        """Reject a mutation that would invalidate an already-prepared request."""
        self._assert_not_preparing()

    def assert_generic_history_replacement_allowed(self) -> None:
        """Keep generic history imports from retargeting fixed v1 state."""
        if self.has_marked_fixed_state():
            raise InstructionAssemblyError(
                "generic history cannot replace pending or retained v1 fixed state; "
                "use restore_host_checkpoint or clear the context deliberately"
            )

    def has_marked_fixed_state(self) -> bool:
        """Whether fixed v1 state would be silently lost on the legacy route."""
        return bool(self._pending) or any(
            has_instruction_descriptor(message) for message in self._context.messages
        )

    def reset_fixed_state(self) -> None:
        """Discard request-only fixed state as part of an explicit context clear."""
        self._assert_not_preparing()
        self._pending.clear()

    def assert_message_ingress_allowed(self, message: dict[str, Any]) -> None:
        """Allow exactly the response append owned by the active acceptance."""
        allowance = _ACTIVE_RESPONSE_APPEND.get()
        request = self._current_request
        if allowance is not None:
            prepared = self._prepared.get(allowance.request_id)
            if (
                request is not None
                and request.get("request_id") == allowance.request_id
                and prepared is not None
                and prepared.state == "accepting"
                and allowance.accepting_task is asyncio.current_task()
                and not allowance.consumed
                and message == allowance.response_message
            ):
                allowance.consumed = True
                return
            raise InstructionAssemblyError(
                "response acceptance may append only its validated provider response"
            )
        self._assert_not_preparing()

    def _entry_id(self, source: _RegisteredSource, event_key: str) -> str:
        return f"{self._session_id}:{source.source_id}:{event_key}"

    def _publish(
        self,
        source: _RegisteredSource,
        event_key: str,
        instruction: str,
        *,
        target: dict[str, Any] | str,
        retain_history: bool,
        authority: str,
    ) -> str:
        self._assert_live_source(source)
        self._assert_not_preparing()
        if not isinstance(event_key, str) or not event_key:
            raise InstructionAssemblyError("event_key must be a non-empty string")
        if not isinstance(instruction, str):
            raise InstructionAssemblyError("fixed instruction content must be text")
        authority = _validate_authority(authority, "fixed instruction authority")
        target_kind = _target_kind(target)
        if target_kind != "deferred":
            self._validate_fixed_target(target)
            placement = target_kind
        else:
            placement = target["placement"]
        entry_id = self._entry_id(source, event_key)
        existing = self._find_entry(entry_id)
        if existing is not None:
            descriptor = _fixed_descriptor(existing)
            assert descriptor is not None
            same_deferred_request = (
                target_kind == "deferred"
                and descriptor.get("deferred_origin") is True
                and descriptor.get("placement") == placement
            )
            if (
                existing.get("content") != instruction
                or descriptor.get("authority", AUTHORITATIVE) != authority
                or (
                    descriptor.get("target") != target
                    and not same_deferred_request
                )
            ):
                raise InstructionAssemblyError(
                    f"fixed instruction {entry_id!r} was republished with different content or target "
                    "(or authority)"
                )
            return entry_id

        self._fixed_order += 1
        entry = {
            "entry_id": entry_id,
            "source": source.source_id,
            "key": event_key,
            "content": instruction,
            "target": copy.deepcopy(target),
            "placement": placement,
            "authority": authority,
            "deferred_origin": target_kind == "deferred",
            "retain_history": retain_history,
            "order": self._fixed_order,
            "disposition": "pending",
        }
        if retain_history:
            self._store_fixed(entry)
        else:
            self._pending[entry_id] = entry
        return entry_id

    def _find_entry(self, entry_id: str) -> dict[str, Any] | None:
        for message in self._context.messages:
            descriptor = _fixed_descriptor(message)
            if descriptor and descriptor.get("entry_id") == entry_id:
                return message
        pending = self._pending.get(entry_id)
        if pending is not None:
            return {
                "content": pending["content"],
                "metadata": {INSTRUCTION_METADATA: pending},
            }
        return None

    def _store_fixed(self, entry: dict[str, Any]) -> None:
        descriptor = {
            "version": 1,
            "source": entry["source"],
            "key": entry["key"],
            "binding": "fixed",
            "placement": entry["placement"],
            "authority": entry["authority"],
            "entry_id": entry["entry_id"],
            "event_key": entry["key"],
            "session_id": self._session_id,
            "target": entry["target"],
            "order": entry["order"],
            "disposition": entry["disposition"],
        }
        if entry.get("deferred_origin"):
            descriptor["deferred_origin"] = True
        self._context._add_fixed_instruction_message(
            {"role": "system", "content": entry["content"], "metadata": {INSTRUCTION_METADATA: descriptor}}
        )

    def _retire(self, source: _RegisteredSource, event_key: str, reason: str) -> None:
        self._assert_live_source(source)
        self._assert_not_preparing()
        if not isinstance(reason, str) or not reason:
            raise InstructionAssemblyError("retirement reason must be non-empty")
        entry_id = self._entry_id(source, event_key)
        if entry_id in self._pending:
            del self._pending[entry_id]
            return
        message = self._find_entry(entry_id)
        if message is None:
            raise InstructionAssemblyError(f"unknown fixed instruction {entry_id!r}")
        descriptor = _fixed_descriptor(message)
        if descriptor is None or descriptor.get("source") != source.source_id:
            raise InstructionAssemblyError("a source may retire only its own fixed instruction")
        if descriptor.get("disposition") in {"retired", "anchor_pruned"}:
            return
        descriptor["disposition"] = "retired"
        descriptor["retire_reason"] = reason

    def _close(self, source: _RegisteredSource) -> None:
        if self._sources.get(source.source_id) != source:
            return
        self._assert_not_preparing()
        del self._sources[source.source_id]
        # Close only abandons instance-local, request-only work.  Retained
        # entries remain ordinary checkpointable history records.
        for entry_id, entry in list(self._pending.items()):
            if entry["source"] == source.source_id and not entry["retain_history"]:
                del self._pending[entry_id]

    @contextmanager
    def input_scope(self, origin: str, input_id: str) -> Iterator[None]:
        if origin not in {"human", "delegation", "synthetic"} or not isinstance(input_id, str) or not input_id:
            raise InstructionAssemblyError("input scope requires a supported origin and non-empty input_id")
        if self._current_input is not None:
            raise InstructionAssemblyError("nested input scopes are not supported")
        self._current_input = {"version": 1, "input_id": input_id, "origin": origin, "message_id": input_id}
        try:
            yield
        finally:
            self._current_input = None

    @asynccontextmanager
    async def turn(self, turn_id: str, input_boundary: dict[str, Any] | None = None):
        if self._current_turn is not None:
            raise InstructionAssemblyError("overlapping outer turns are not supported")
        if not isinstance(turn_id, str) or not turn_id:
            raise InstructionAssemblyError("turn_id must be non-empty")
        self._current_turn = {"turn_id": turn_id, "input_boundary": input_boundary}
        try:
            yield
        finally:
            self._current_turn = None

    @asynccontextmanager
    async def request(self, request_scope: dict[str, Any], selected_provider: Any):
        if self._current_request is not None:
            raise InstructionAssemblyError("overlapping requests are not supported")
        request_id = request_scope.get("request_id") if isinstance(request_scope, dict) else None
        if not isinstance(request_id, str) or not request_id:
            raise InstructionAssemblyError("request scope requires a non-empty request_id")
        if self._current_turn and request_scope.get("turn_id") != self._current_turn["turn_id"]:
            raise InstructionAssemblyError("request scope does not belong to the active turn")
        supports_v1 = (
            getattr(selected_provider, "instruction_layout_version", None) == 1
            and getattr(selected_provider, "instruction_layout_authority_v1", None) is True
        )
        if not supports_v1 and self.has_marked_fixed_state():
            raise InstructionAssemblyError(
                "marked v1 instruction history requires a provider with "
                "instruction_layout_version == 1 and instruction_layout_authority_v1 is True"
            )
        self._route = "v1" if supports_v1 else "legacy"
        self._current_request = dict(request_scope)
        try:
            yield
        finally:
            prepared = self._prepared.get(request_id)
            if prepared is not None and prepared.state == "prepared":
                prepared.state = "abandoned"
                self._context._record_instruction_observation(
                    {"phase": "delivery", "request_id": request_id, "delivery": "abandoned"}
                )
            self._current_request = None
            self._route = "pending"

    def bind_input_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Attach explicit provenance at the input boundary, never by text matching."""
        if self._current_input is None:
            return message
        if message.get("role") != "user":
            return message
        metadata = dict(message.get("metadata") or {})
        existing = metadata.get(INPUT_METADATA)
        if existing is not None and existing != self._current_input:
            raise InstructionAssemblyError("input message provenance conflicts with active input scope")
        metadata[INPUT_METADATA] = dict(self._current_input)
        return {**message, "metadata": metadata}

    async def prepare(
        self,
        *,
        request_id: str,
        provider: Any,
        base_view: Callable[[int], Awaitable[tuple[list[dict[str, Any]], str | None]]],
        budget: int,
    ) -> list[dict[str, Any]]:
        """Memoize one immutable assembled view per request ID."""
        prepared = self._prepared.get(request_id)
        current_task = asyncio.current_task()
        if prepared:
            if prepared.state in {"prepared", "accepting", "stored"}:
                assert prepared.messages is not None
                return copy.deepcopy(prepared.messages)
            if prepared.state == "preparing":
                if prepared.task is current_task or _ACTIVE_CALLBACK_REQUEST.get() == request_id:
                    raise InstructionAssemblyError("recursive instruction request collection")
                assert prepared.task is not None
                await prepared.task
                return await self.prepare(
                    request_id=request_id, provider=provider, base_view=base_view, budget=budget
                )
            raise InstructionAssemblyError(f"instruction request {request_id!r} is {prepared.state}")

        prepared = _Prepared(state="preparing", task=current_task)
        self._prepared[request_id] = prepared
        self._preparing = True
        version = self._context._instruction_mutation_version
        try:
            records = await self._collect_records(request_id)
            # Deferred retained entries become ordinary context records during
            # their one-time binding.  That controlled mutation is part of
            # this transaction; subsequent external changes still invalidate
            # the request.
            version = self._context._instruction_mutation_version
            reservation = self._context._estimate_tokens(records)
            if self._context.compaction_notice_enabled:
                reservation += self._context.compaction_notice_token_reserve
            base_messages, notice = await base_view(reservation)
            if version != self._context._instruction_mutation_version:
                raise InstructionAssemblyError("context changed while instruction request was preparing")
            records = self._drop_pruned_fixed_records(records, base_messages)
            records = await self._filter_and_admit(records, request_id, "instructions")
            if notice:
                notice_record = self._record(
                    source="context-compaction",
                    key="notice",
                    content=notice,
                    placement="before_human",
                    authority=ADVISORY,
                    binding="live",
                    order=-1,
                )
                admitted_notice = await self._filter_and_admit(
                    [notice_record], request_id, "compaction_notice"
                )
                records.extend(admitted_notice)
            if version != self._context._instruction_mutation_version:
                raise InstructionAssemblyError("context changed while instruction request was preparing")
            messages, rendered = self._place(base_messages, records)
            for record in records:
                descriptor = record["metadata"][INSTRUCTION_METADATA]
                self._context._record_instruction_observation(
                    {
                        "phase": "admission",
                        "request_id": request_id,
                        "source": descriptor["source"],
                        "key": descriptor["key"],
                        "placement": descriptor["placement"],
                        "delivery": "pending",
                    }
                )
            if self._context._estimate_tokens(messages) > budget:
                raise InstructionAssemblyError(
                    f"assembled instructions cannot fit request budget ({self._context._estimate_tokens(messages)} > {budget})"
                )
            prepared.state = "prepared"
            prepared.messages = copy.deepcopy(messages)
            prepared.rendered_entries = rendered
            return copy.deepcopy(prepared.messages)
        except BaseException:
            prepared.state = "failed"
            raise
        finally:
            self._preparing = False

    async def _collect_records(self, request_id: str) -> list[dict[str, Any]]:
        scope = self._current_request
        if scope is None or scope.get("request_id") != request_id:
            raise InstructionAssemblyError("request scope is no longer active")
        records: list[dict[str, Any]] = []
        for source in sorted(self._sources.values(), key=lambda value: value.order):
            if source.callback is None:
                continue
            snapshot = await self._call_required(
                source.callback,
                f"snapshot callback for {source.source_id!r}",
                request_id,
                copy.deepcopy(scope),
            )
            if not isinstance(snapshot, list):
                raise InstructionAssemblyError(f"snapshot for {source.source_id!r} must return a list")
            keys: set[str] = set()
            for index, item in enumerate(snapshot):
                item = _validate_snapshot_item(item)
                if item["key"] in keys:
                    raise InstructionAssemblyError(
                        f"snapshot for {source.source_id!r} has duplicate key {item['key']!r}"
                    )
                keys.add(item["key"])
                records.append(
                    self._record(
                        source=source.source_id,
                        key=item["key"],
                        content=item["content"],
                        placement=item["placement"],
                        authority=item["authority"],
                        binding="live",
                        order=(source.order * 1_000_000) + index,
                        target=item.get("after"),
                    )
                )

        for entry in self._all_fixed_entries():
            if _terminal(entry):
                continue
            if _target_kind(entry["target"]) == "deferred":
                entry["target"] = self._resolve_deferred_target(scope, entry["placement"])
                if entry["retain_history"]:
                    self._update_stored_target(entry["entry_id"], entry["target"])
            records.append(
                self._record(
                    source=entry["source"],
                    key=entry["key"],
                    content=entry["content"],
                    placement=entry["placement"],
                    authority=entry.get("authority", AUTHORITATIVE),
                    binding="fixed",
                    order=entry["order"],
                    target=entry["target"],
                    entry_id=entry["entry_id"],
                    disposition=entry["disposition"],
                )
            )
        return records

    def _all_fixed_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for message in self._context.messages:
            descriptor = _fixed_descriptor(message)
            if descriptor is None:
                if is_instruction_message(message):
                    raise InstructionAssemblyError("trusted restored instruction descriptor has invalid shape")
                continue
            entries.append(
                {
                    **copy.deepcopy(descriptor),
                    "authority": descriptor.get("authority", AUTHORITATIVE),
                    "content": message.get("content"),
                    "retain_history": True,
                }
            )
        entries.extend(copy.deepcopy(list(self._pending.values())))
        return sorted(entries, key=lambda entry: (entry["order"], entry["entry_id"]))

    def _resolve_deferred_target(self, scope: dict[str, Any], placement: str) -> dict[str, Any]:
        if placement == "head":
            return {"session_id": self._session_id, "kind": "conversation_head"}
        anchor = scope.get("input_anchor")
        if not isinstance(anchor, dict):
            raise InstructionAssemblyError("first_eligible_turn has no explicit input anchor")
        self._validate_input_anchor(anchor, self._context.messages)
        return dict(anchor)

    def _update_stored_target(self, entry_id: str, target: dict[str, Any]) -> None:
        message = self._find_entry(entry_id)
        descriptor = _fixed_descriptor(message) if message else None
        if descriptor is None:
            raise InstructionAssemblyError("retained deferred entry disappeared before binding")
        if _terminal(descriptor):
            raise InstructionAssemblyError("terminal deferred instruction cannot be rebound")
        descriptor["target"] = copy.deepcopy(target)
        self._context._instruction_mutation_version += 1

    def _drop_pruned_fixed_records(
        self, records: list[dict[str, Any]], base: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Terminalize an absent fixed anchor before final-content admission."""
        renderable: list[dict[str, Any]] = []
        for record in records:
            descriptor = record["metadata"][INSTRUCTION_METADATA]
            if descriptor["binding"] != "fixed":
                renderable.append(record)
                continue
            target = descriptor["target"]
            kind = _target_kind(target)
            if kind == "head":
                renderable.append(record)
                continue
            if kind == "before_human":
                try:
                    self._validate_input_anchor(target, base)
                except InstructionAssemblyError:
                    self._anchor_pruned(descriptor["entry_id"])
                    continue
            elif not self._has_message(base, target.get("after_message_id")):
                self._anchor_pruned(descriptor["entry_id"])
                continue
            renderable.append(record)
        return renderable

    def _record(
        self,
        *,
        source: str,
        key: str,
        content: str,
        placement: str,
        authority: str,
        binding: str,
        order: int,
        target: dict[str, Any] | None = None,
        entry_id: str | None = None,
        disposition: str | None = None,
    ) -> dict[str, Any]:
        descriptor: dict[str, Any] = {
            "version": 1,
            "source": source,
            "key": key,
            "binding": binding,
            "placement": placement,
            "authority": _validate_authority(authority),
        }
        if binding == "fixed":
            descriptor.update(
                entry_id=entry_id,
                event_key=key,
                session_id=self._session_id,
                target=copy.deepcopy(target),
                order=order,
                disposition=disposition,
            )
        elif target is not None:
            descriptor["target"] = copy.deepcopy(target)
        return {"role": "system", "content": content, "metadata": {INSTRUCTION_METADATA: descriptor}}

    async def _call_required(
        self, callback: Callable[..., Any], label: str, request_id: str, *args: Any
    ) -> Any:
        loop = asyncio.get_running_loop()
        result: asyncio.Future[Any] = loop.create_future()

        def resolve(value: Any = None, error: BaseException | None = None) -> None:
            if result.done():
                return
            if error is None:
                result.set_result(value)
            else:
                result.set_exception(error)

        def schedule(value: Any = None, error: BaseException | None = None) -> None:
            if loop.is_closed():
                return
            try:
                if error is None:
                    loop.call_soon_threadsafe(resolve, value)
                else:
                    loop.call_soon_threadsafe(lambda error=error: resolve(error=error))
            except RuntimeError:
                # A timed-out callback can finish after a caller closes its
                # request loop. Its result is intentionally discarded.
                return

        def invoke() -> Any:
            token = _ACTIVE_CALLBACK_REQUEST.set(request_id)
            value: Any = None
            error: BaseException | None = None
            try:
                value = callback(*args)
                if inspect.isawaitable(value):
                    value = asyncio.run(_await(value))
            except BaseException as caught:
                error = caught
            finally:
                _ACTIVE_CALLBACK_REQUEST.reset(token)
                self._callback_workers.release()
            schedule(value, error)

        try:
            if not self._callback_workers.acquire(blocking=False):
                raise InstructionAssemblyError(
                    "instruction callback worker capacity is exhausted"
                )
            try:
                threading.Thread(target=invoke, daemon=True).start()
            except BaseException:
                self._callback_workers.release()
                raise
            return await asyncio.wait_for(result, timeout=self._callback_timeout_s)
        except TimeoutError as error:
            raise InstructionAssemblyError(
                f"{label} timed out after {self._callback_timeout_s:g}s"
            ) from error

    async def _filter_and_admit(
        self, records: list[dict[str, Any]], request_id: str, phase: str
    ) -> list[dict[str, Any]]:
        for configured in self._required_filters:
            capability = self._coordinator.get_capability(configured.capability)
            if capability is None or not callable(getattr(capability, "apply", None)):
                raise InstructionAssemblyError(
                    f"required instruction filter {configured.capability!r} is unavailable"
                )
            identity = copy.deepcopy(records)
            returned = await self._call_required(
                capability.apply,
                f"required instruction filter {configured.capability!r}",
                request_id,
                copy.deepcopy(identity),
                request_id,
                phase,
            )
            records = self._validate_filter_result(
                original=identity,
                returned=returned,
                request_id=request_id,
                phase=phase,
                capability=configured.capability,
                policy_id=configured.policy_id,
                legacy=configured.legacy,
            )

        admitted: list[dict[str, Any]] = []
        for record in records:
            content = ToolResult(success=True, output=record["content"]).get_serialized_output()
            if not content:
                raise InstructionAssemblyError("instruction content is empty after public normalization")
            result = HookResult(
                action="inject_context",
                context_injection=content,
                context_injection_role="system",
                ephemeral=True,
                suppress_output=True,
            )
            response = await _await(
                self._coordinator.process_hook_result(
                    result, event="context:instructions", hook_name=record["metadata"][INSTRUCTION_METADATA]["source"]
                )
            )
            if getattr(response, "action", None) == "deny":
                raise InstructionAssemblyError("core denied final instruction admission")
            admitted.append({**record, "content": content})
        return admitted

    @staticmethod
    def _validate_filter_result(
        *,
        original: list[dict[str, Any]],
        returned: Any,
        request_id: str,
        phase: str,
        capability: str,
        policy_id: str | None,
        legacy: bool,
    ) -> list[dict[str, Any]]:
        if not isinstance(returned, dict) or set(returned) != {"records", "receipt"}:
            raise InstructionAssemblyError("instruction filter returned an invalid closed shape")
        records, receipt = returned["records"], returned["receipt"]
        if not isinstance(records, list) or not isinstance(receipt, dict):
            raise InstructionAssemblyError("instruction filter returned invalid records or receipt")
        receipt_matches = (
            receipt.get("request_id") == request_id
            and receipt.get("phase") == phase
        )
        if legacy:
            receipt_matches = receipt_matches and isinstance(receipt.get("policy_id"), str)
        else:
            receipt_matches = receipt_matches and (
                set(receipt) == {"capability", "policy_id", "request_id", "phase"}
                and receipt.get("capability") == capability
                and receipt.get("policy_id") == policy_id
            )
        if not receipt_matches or len(records) != len(original):
            raise InstructionAssemblyError("instruction filter receipt does not match this request phase")
        validated: list[dict[str, Any]] = []
        for before, after in zip(original, records, strict=True):
            if not isinstance(after, dict) or set(after) != set(before):
                raise InstructionAssemblyError("instruction filter changed the record shape")
            if not isinstance(after.get("content"), str):
                raise InstructionAssemblyError("instruction filter returned non-text content")
            for key, value in before.items():
                if key != "content" and after.get(key) != value:
                    raise InstructionAssemblyError("instruction filter changed protected record identity")
            validated.append(copy.deepcopy(after))
        return validated

    def _place(
        self, base: list[dict[str, Any]], records: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        head: list[dict[str, Any]] = []
        before: dict[str, list[dict[str, Any]]] = {}
        after: dict[str, list[dict[str, Any]]] = {}
        rendered: list[str] = []
        latest_anchor = self._current_request.get("input_anchor") if self._current_request else None
        for record in records:
            descriptor = record["metadata"][INSTRUCTION_METADATA]
            placement = descriptor["placement"]
            target = descriptor.get("target")
            if descriptor["binding"] == "fixed":
                kind = _target_kind(target)
                if kind == "head":
                    head.append(record)
                else:
                    anchor_id = target.get("message_id") if kind == "before_human" else target.get("after_message_id")
                    if not self._has_message(base, anchor_id):
                        self._anchor_pruned(descriptor["entry_id"])
                        continue
                    if kind == "before_human":
                        self._validate_input_anchor(target, base)
                    (before if kind == "before_human" else after).setdefault(anchor_id, []).append(record)
                rendered.append(descriptor["entry_id"])
                continue
            if placement == "head":
                head.append(record)
            elif placement == "before_human":
                anchor = latest_anchor
                if not isinstance(anchor, dict) or anchor.get("origin") not in {"human", "delegation"}:
                    raise InstructionAssemblyError("live before_human instruction has no human/delegation input anchor")
                self._validate_input_anchor(anchor, base)
                before.setdefault(anchor["message_id"], []).append(record)
            else:
                anchor = target or (self._current_request or {}).get("tail_anchor")
                anchor_id = anchor.get("after_message_id") if isinstance(anchor, dict) else None
                if not isinstance(anchor_id, str) or not self._has_message(base, anchor_id):
                    raise InstructionAssemblyError("live tail instruction has no validated tail anchor")
                after.setdefault(anchor_id, []).append(record)

        # System records must lead with base system material.  The context's
        # legacy compactor preserves base system records at the front.
        prefix_end = 0
        while prefix_end < len(base) and base[prefix_end].get("role") == "system":
            prefix_end += 1
        result = list(base[:prefix_end]) + head
        for message in base[prefix_end:]:
            message_id = _message_id(message)
            if message_id and message_id in before:
                result.extend(before.pop(message_id))
            result.append(message)
            if message_id and message_id in after:
                result.extend(after.pop(message_id))
        if before or after:
            raise InstructionAssemblyError("instruction anchor was not found in prepared request")
        return result, rendered

    @staticmethod
    def _has_message(messages: list[dict[str, Any]], message_id: Any) -> bool:
        return isinstance(message_id, str) and any(_message_id(message) == message_id for message in messages)

    def _anchor_pruned(self, entry_id: str) -> None:
        pending = self._pending.get(entry_id)
        if pending is not None:
            # Request-only work cannot outlive a pruned causal anchor.
            del self._pending[entry_id]
            return
        message = self._find_entry(entry_id)
        if message is None:
            return
        descriptor = _fixed_descriptor(message)
        if descriptor and not _terminal(descriptor):
            descriptor["disposition"] = "anchor_pruned"

    @staticmethod
    def _validated_provider_response(response_message: Any) -> dict[str, Any]:
        if not isinstance(response_message, dict) or response_message.get("role") != "assistant":
            raise InstructionAssemblyError("response acceptance requires a canonical assistant message")
        metadata = response_message.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise InstructionAssemblyError("provider response metadata must be a mapping when present")
        if has_instruction_descriptor(response_message) or has_input_descriptor(response_message):
            raise InstructionAssemblyError(
                "provider response must not claim reserved instruction or input provenance"
            )
        return copy.deepcopy(response_message)

    def _response_was_appended(self, seq: int) -> bool:
        return any(
            (message.get("metadata") or {}).get("_seq") == seq
            for message in self._context.messages
        )

    def _stored_response_is_present(self, prepared: _Prepared) -> bool:
        if prepared.response_message is None or prepared.response_seq is None:
            return False
        expected = copy.deepcopy(prepared.response_message)
        expected_metadata = expected.pop("metadata", None) or {}
        for message in self._context.messages:
            metadata = message.get("metadata") or {}
            if metadata.get("_seq") != prepared.response_seq:
                continue
            actual = copy.deepcopy(message)
            actual_metadata = actual.pop("metadata", None) or {}
            return actual == expected and all(
                actual_metadata.get(key) == value for key, value in expected_metadata.items()
            )
        return False

    def _complete_response_acceptance(self, request_id: str, prepared: _Prepared) -> None:
        """Acknowledge rendered fixed entries after response ingress succeeds."""
        if not self._stored_response_is_present(prepared):
            raise InstructionAssemblyError(
                "stored provider response is no longer present; delivery remains unacknowledged"
            )
        # The observation validates before any delivery state changes.  It has no
        # asynchronous boundary, so a successful return means the following
        # in-memory updates cannot expose a half-acknowledged request.
        self._context._record_instruction_observation(
            {"phase": "delivery", "request_id": request_id, "delivery": "accepted"}
        )
        retained_changed = False
        for entry_id in prepared.rendered_entries or []:
            if entry_id in self._pending:
                del self._pending[entry_id]
                continue
            message = self._find_entry(entry_id)
            descriptor = _fixed_descriptor(message) if message else None
            if descriptor is not None and not _terminal(descriptor):
                descriptor["disposition"] = "delivered"
                retained_changed = True
        if retained_changed:
            self._context._instruction_mutation_version += 1
        prepared.state = "accepted"

    async def accept_response(self, request_id: str, response_message: dict[str, Any]) -> None:
        if self._current_request is None or self._current_request.get("request_id") != request_id:
            raise InstructionAssemblyError("response acceptance requires the matching active request")
        prepared = self._prepared.get(request_id)
        if prepared is None:
            raise InstructionAssemblyError("cannot accept an unprepared instruction request")
        canonical_response = self._validated_provider_response(response_message)
        current_task = asyncio.current_task()
        if prepared.accepting_task is current_task:
            raise InstructionAssemblyError("response acceptance cannot re-enter the same request")
        async with prepared.acceptance_lock:
            prepared.accepting_task = current_task
            try:
                if prepared.state == "prepared":
                    prepared.state = "accepting"
                    append_seq = self._context._next_seq
                    allowance = _ResponseAppendAllowance(
                        request_id, canonical_response, current_task
                    )
                    token = _ACTIVE_RESPONSE_APPEND.set(allowance)
                    try:
                        await self._context.add_message(copy.deepcopy(canonical_response))
                    except BaseException:
                        if self._response_was_appended(append_seq):
                            prepared.state = "stored"
                            prepared.response_message = canonical_response
                            prepared.response_seq = append_seq
                        else:
                            prepared.state = "prepared"
                        raise
                    finally:
                        _ACTIVE_RESPONSE_APPEND.reset(token)
                    if not allowance.consumed:
                        prepared.state = "prepared"
                        raise InstructionAssemblyError("response ingress did not consume its acceptance allowance")
                    prepared.state = "stored"
                    prepared.response_message = canonical_response
                    prepared.response_seq = append_seq
                elif prepared.state == "stored":
                    if prepared.response_message != canonical_response:
                        raise InstructionAssemblyError(
                            "a different response cannot replace the response already stored for this request"
                        )
                else:
                    raise InstructionAssemblyError(
                        f"instruction request {request_id!r} is {prepared.state}"
                    )
                self._complete_response_acceptance(request_id, prepared)
            finally:
                prepared.accepting_task = None

    def _validate_fixed_target(self, target: dict[str, Any] | str) -> None:
        kind = _target_kind(target)
        if kind == "head":
            if set(target) != {"session_id", "kind"} or target["session_id"] != self._session_id:
                raise InstructionAssemblyError("fixed head target is not this logical conversation")
            return
        if kind == "before_human":
            self._validate_input_anchor(target, self._context.messages)
            return
        if set(target) != {"after_message_id"} or not self._has_message(
            self._context.messages, target["after_message_id"]
        ):
            raise InstructionAssemblyError("fixed tail target does not name an admitted message")

    @staticmethod
    def _validate_input_anchor(anchor: Any, messages: list[dict[str, Any]]) -> None:
        anchor = InstructionAssembly._validate_input_anchor_shape(anchor)
        if not InstructionAssembly._has_input_anchor(messages, anchor):
            raise InstructionAssemblyError("instruction input anchor is not an admitted input record")

    @staticmethod
    def _validate_input_anchor_shape(anchor: Any) -> dict[str, str]:
        if not isinstance(anchor, dict):
            raise InstructionAssemblyError("input anchor has an invalid closed shape")
        keys = {"input_id", "message_id", "origin"}
        if set(anchor) == keys | {"version"}:
            if type(anchor.get("version")) is not int or anchor.get("version") != 1:
                raise InstructionAssemblyError("input anchor has an unsupported version")
            anchor = {key: anchor[key] for key in keys}
        elif set(anchor) != keys:
            raise InstructionAssemblyError("input anchor has an invalid closed shape")
        if (
            not all(isinstance(anchor[key], str) and anchor[key] for key in keys)
            or anchor["origin"] not in {"human", "delegation"}
        ):
            raise InstructionAssemblyError("instruction input anchor is not human/delegation provenance")
        return {key: anchor[key] for key in keys}

    @staticmethod
    def _has_input_anchor(messages: list[dict[str, Any]], anchor: dict[str, str]) -> bool:
        return any(_input_anchor(message) == anchor for message in messages)