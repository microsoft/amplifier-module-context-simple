"""
Simple context manager module.

Implements an in-memory context manager with EPHEMERAL compaction:
  • Messages stored in memory (self.messages is the source of truth)
  • Compaction NEVER modifies self.messages
  • get_messages_for_request() returns compacted VIEW (new list)
  • get_messages() returns FULL history (for transcripts/session persistence)

This design ensures conversation history is never lost, even during compaction.
For persistent storage across sessions, use context-persistent instead.

Dynamic System Prompt Support:
  • set_system_prompt_factory() registers a callable that produces fresh system content
  • get_messages_for_request() calls the factory on EVERY request
  • Enables @mentions and bundle instructions to be re-processed each turn
  • Static system messages (via add_message) are still supported as fallback
"""

# Amplifier module metadata
__amplifier_module_type__ = "context"

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from amplifier_core import ModuleCoordinator

logger = logging.getLogger(__name__)


async def mount(coordinator: ModuleCoordinator, config: dict[str, Any] | None = None):
    """
    Mount the simple context manager.

    Args:
        coordinator: Module coordinator
        config: Optional configuration
            - max_tokens: Maximum context size (default: 200,000)
            - compact_threshold: Trigger compaction at this usage (default: 0.92)
            - target_usage: Compact down to this usage (default: 0.50)
            - protected_recent: Always protect last N% of messages (default: 0.30)
            - protected_tool_results: Always protect last N tool results (default: 5)
            - truncate_chars: Characters to keep when truncating tool results (default: 250)
            - compaction_notice_enabled: Enable compaction notice (default: True)
            - compaction_notice_token_reserve: Tokens to reserve for notice (default: 800)
            - compaction_notice_verbosity: Notice detail level - "minimal", "normal", "verbose" (default: "normal")
            - compaction_notice_min_level: Only show notice if compaction level >= this (default: 1)
            - output_reserve_fraction: Fraction of max_output_tokens to reserve for responses (default: 0.5)

    Returns:
        Optional cleanup function
    """
    config = config or {}
    context = SimpleContextManager(
        max_tokens=config.get("max_tokens", 200_000),
        compact_threshold=config.get("compact_threshold", 0.92),
        target_usage=config.get("target_usage", 0.50),
        protected_recent=config.get("protected_recent", 0.30),
        protected_tool_results=config.get("protected_tool_results", 5),
        truncate_chars=config.get("truncate_chars", 250),
        compaction_notice_enabled=config.get("compaction_notice_enabled", True),
        compaction_notice_token_reserve=config.get(
            "compaction_notice_token_reserve", 800
        ),
        compaction_notice_verbosity=config.get("compaction_notice_verbosity", "normal"),
        compaction_notice_min_level=config.get("compaction_notice_min_level", 1),
        output_reserve_fraction=config.get("output_reserve_fraction", 0.5),
        hooks=getattr(coordinator, "hooks", None),
    )
    await coordinator.mount("context", context)
    logger.info("Mounted SimpleContextManager")
    return


class SimpleContextManager:
    """
    In-memory context manager with EPHEMERAL compaction.

    Key Principle: self.messages is the source of truth and is NEVER modified
    by compaction. Compaction only returns a compacted VIEW for the current
    LLM request.

    Owns memory policy: orchestrators ask for messages via get_messages_for_request(),
    and this context manager decides how to fit them within limits. Compaction is
    handled internally and ephemerally - the original history is always preserved.

    Compaction Strategy (Progressive Interleaved):
    Triggered when usage >= compact_threshold (default 92%), target is target_usage (default 50%).

    Each level checks after every operation and stops as soon as target is reached:

    Level 1: Truncate oldest 25% of tool results
    Level 2: Truncate next 25% of tool results (now 50% truncated)
    Level 3: Remove oldest messages (use configured protected_recent)
    Level 4: Truncate next 25% of tool results (now 75% truncated)
    Level 5: Remove more messages (60% of configured protection)
    Level 6: Truncate remaining tool results (except last N)
    Level 7: Remove more messages (30% of configured protection - last resort)
    Level 8: Stub first user message + remove old stubs (extreme pressure)

    This interleaved approach ensures minimal data loss by:
    - Preferring truncation (preserves structure) over removal (loses context)
    - Progressively relaxing protection as pressure increases
    - Respecting configured protected_recent as baseline, only relaxing under pressure
    - Always protecting: system messages, last user message, last N tool results, tool pairs
    - First user message: stubbable at Level 8, but never fully removed
    """

    def __init__(
        self,
        max_tokens: int = 200_000,
        compact_threshold: float = 0.92,
        target_usage: float = 0.50,
        protected_recent: float = 0.30,
        protected_tool_results: int = 5,
        truncate_chars: int = 250,
        compaction_notice_enabled: bool = True,
        compaction_notice_token_reserve: int = 800,
        compaction_notice_verbosity: str = "normal",
        compaction_notice_min_level: int = 1,
        output_reserve_fraction: float = 0.5,
        hooks: Any = None,
    ):
        """
        Initialize the context manager.

        Args:
            max_tokens: Maximum context size in tokens
            compact_threshold: Trigger compaction at this usage ratio (0.0-1.0)
            target_usage: Compact down to this usage ratio (0.0-1.0)
            protected_recent: Always protect last N% of messages (0.0-1.0)
            protected_tool_results: Always protect last N tool results from truncation
            truncate_chars: Characters to keep when truncating tool results
            compaction_notice_enabled: Enable compaction notice injection
            compaction_notice_token_reserve: Tokens to reserve for notice
            compaction_notice_verbosity: Notice detail level ("minimal", "normal", "verbose")
            compaction_notice_min_level: Only show notice if compaction level >= this
            output_reserve_fraction: Fraction of max_output_tokens to reserve for
                responses (0.0-1.0, default: 0.5). Lower values give more context
                budget at the cost of less headroom for long responses.
            hooks: Optional hooks instance for emitting observability events
        """
        self.messages: list[dict[str, Any]] = []
        self.max_tokens = max_tokens
        self.compact_threshold = compact_threshold
        self.target_usage = target_usage
        self.protected_recent = protected_recent
        self.protected_tool_results = protected_tool_results
        self.truncate_chars = truncate_chars
        self.compaction_notice_enabled = compaction_notice_enabled
        self.compaction_notice_token_reserve = compaction_notice_token_reserve
        self.compaction_notice_verbosity = compaction_notice_verbosity
        self.compaction_notice_min_level = compaction_notice_min_level
        self.output_reserve_fraction = output_reserve_fraction
        self._hooks = hooks
        self._last_compaction_stats: dict[str, Any] | None = None
        self._system_prompt_factory: Callable[[], Awaitable[str]] | None = None

        # --- Sticky compaction decision state ---
        # Compaction decisions (remove / truncate / stub) are keyed by a
        # stable per-message sequence id (metadata["_seq"], assigned in
        # add_message()) rather than by list index, because indices shift
        # as history grows and as the ephemeral view is rebuilt every call.
        # Once a message's fate is decided, it is NEVER reconsidered -- this
        # is what keeps the returned view's shared prefix byte-stable across
        # calls where the underlying history only grew by a turn or two,
        # instead of the whole compaction decision being re-derived (and
        # potentially shifting) on every single get_messages_for_request()
        # call. See _apply_sticky_decisions() / _compact_ephemeral().
        self._next_seq: int = 0
        self._removed_seqs: set[int] = set()
        self._truncated_seqs: set[int] = set()
        self._stubbed_seqs: set[int] = set()
        # Cumulative highest progressive strategy level (1-8) ever reached.
        # Reported in compaction stats / notice so the LLM sees the total
        # accumulated effect, not just the most recent escalation step.
        self._sticky_level: int = 0
        # Log-once latch for an arithmetically unreachable target. Reset with
        # the rest of the sticky state so a genuinely new situation speaks up.
        self._infeasible_target_reported: bool = False
        # Runaway-compaction breaker. Counts CONSECUTIVE escalations that failed
        # to meaningfully reduce the post-compaction token count. Frequency alone
        # is the wrong signal -- see `_MAX_INEFFECTIVE_ESCALATIONS`.
        self._ineffective_escalations: int = 0
        self._last_after_tokens: int | None = None
        self._escalation_breaker_reported: bool = False

    async def add_message(self, message: dict[str, Any]) -> None:
        """Add a message to the context.

        Messages are always accepted. Compaction happens ephemerally when
        get_messages_for_request() is called before LLM requests.

        Tool results MUST be added even if over threshold, otherwise
        tool_use/tool_result pairing breaks.

        Timestamps are automatically added to message metadata for replay timing.
        Existing timestamps and metadata are preserved.
        """
        # Add timestamp in metadata if not already present (for replay timing)
        existing_meta = message.get("metadata") or {}
        if "timestamp" not in existing_meta:
            message = {
                **message,
                "metadata": {
                    **existing_meta,
                    "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
                },
            }

        # Assign a stable sequence id used as the identity key for sticky
        # compaction decisions (see _apply_sticky_decisions). Every message
        # gets exactly one, assigned once, never reused/reassigned.
        message = {
            **message,
            "metadata": {**(message.get("metadata") or {}), "_seq": self._next_seq},
        }
        self._next_seq += 1

        # Add message (no rejection - compaction happens ephemerally)
        self.messages.append(message)

        token_count = self._estimate_tokens(self.messages)
        usage = token_count / self.max_tokens
        logger.debug(
            f"Added message: {message.get('role', 'unknown')} - "
            f"{len(self.messages)} total messages, {token_count:,} tokens "
            f"({usage:.1%})"
        )

    async def set_system_prompt_factory(
        self, factory: Callable[[], Awaitable[str]]
    ) -> None:
        """Set a factory function that produces fresh system prompt content.

        The factory will be called on EVERY get_messages_for_request() call,
        enabling dynamic content like @mentions to be re-processed each turn.

        This is the preferred approach for bundle-based system prompts. When
        set, the factory takes precedence over any static system messages
        stored via add_message().

        Args:
            factory: Async callable that returns the system prompt string.
                     The factory should handle @mention resolution, file
                     loading, and instruction assembly.
        """
        self._system_prompt_factory = factory
        logger.info("System prompt factory registered - will refresh on each request")

    async def get_messages_for_request(
        self,
        token_budget: int | None = None,
        provider: Any | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get messages ready for an LLM request.

        If a system prompt factory is registered, it is called to produce fresh
        system content on EVERY request. This enables dynamic @mentions and
        bundle instructions to be re-processed each turn.

        Applies EPHEMERAL compaction if needed - returns a NEW list without
        modifying self.messages. The original history is always preserved.

        If compaction occurs and notice is enabled, a system-reminder is inserted
        at position 1 (after main system message) to inform the LLM about what
        was compacted.

        Args:
            token_budget: Optional explicit token limit (deprecated, prefer provider).
            provider: Optional provider instance for dynamic budget calculation.
                If provided, budget = context_window - max_output_tokens - safety_margin.

        Returns:
            Messages ready for LLM request, compacted if necessary.
        """
        budget = self._calculate_budget(token_budget, provider)

        # Reserve token budget for potential compaction notice (if enabled)
        effective_budget = budget
        if self.compaction_notice_enabled:
            effective_budget = budget - self.compaction_notice_token_reserve
            if effective_budget <= 0:
                # Misconfiguration guard: if the reserve consumes the entire budget
                # (or more), _should_compact's `budget > 0` check would silently
                # force usage to 0, disabling compaction entirely rather than
                # loudly failing. Fall back to the full budget instead of a
                # non-positive effective budget - a reserve that swallows the
                # whole context is not a valid state to compact against.
                logger.warning(
                    f"compaction_notice_token_reserve ({self.compaction_notice_token_reserve:,}) "
                    f">= budget ({budget:,}); ignoring reserve for this request to avoid "
                    f"silently disabling compaction (effective budget would be {effective_budget:,})"
                )
                effective_budget = budget
            else:
                logger.debug(
                    f"Reserved {self.compaction_notice_token_reserve} tokens for potential notice "
                    f"(effective budget: {effective_budget:,})"
                )

        # Determine working messages based on whether factory is set
        if self._system_prompt_factory:
            # Factory mode: get fresh system content, exclude stored system messages
            # BUT preserve hook-injected system messages (they have metadata.source = "hook")
            system_content = await self._system_prompt_factory()
            system_message = {"role": "system", "content": system_content}

            # Filter out static system messages but keep hook-injected ones
            # Hook injections have metadata.source = "hook" and should be preserved
            conversation_messages = [
                msg
                for msg in self.messages
                if msg.get("role") != "system"
                or (msg.get("metadata") or {}).get("source") == "hook"
            ]
            working_messages = [system_message] + conversation_messages
            logger.debug(
                f"System prompt factory produced {len(system_content):,} chars, "
                f"{len(conversation_messages)} conversation messages"
            )
        else:
            # Static mode: use messages as-is (may include stored system messages)
            working_messages = list(self.messages)

        token_count = self._estimate_tokens(working_messages)

        # Check if compaction needed (using effective budget with notice reserve deducted)
        if not self._should_compact(token_count, effective_budget):
            # No compaction needed at all: the strongest possible evidence that
            # pressure is relieved, so the runaway breaker re-arms here too.
            # Its bookkeeping otherwise lives inside `_compact_ephemeral`, which
            # by definition does not run on this path -- leaving a stale count
            # that could trip the breaker on an unrelated later burst.
            self._ineffective_escalations = 0
        if self._should_compact(token_count, effective_budget):
            # Compact EPHEMERALLY - returns new list, working_messages unchanged
            compacted = await self._compact_ephemeral(
                effective_budget, working_messages
            )
            logger.info(
                f"Ephemeral compaction: {len(working_messages)} -> {len(compacted)} messages for this request"
            )

            # Append compaction notice at the TAIL if enabled and level threshold met.
            #
            # CRITICAL (prompt cache stability): this notice must never be inserted
            # into the prefix. Two things make the tail the only safe placement:
            #
            # 1. role: previously this was "system", which -- for the Anthropic
            #    provider -- gets extracted OUT of the conversation entirely and
            #    merged into the single top-level system content block (see
            #    provider-anthropic's `_complete_chat_request`: `system_msgs = [m
            #    for m in request.messages if m.role == "system"]`, combined by
            #    `_format_system_with_cache`). That means a "system"-role notice
            #    inserted anywhere -- even at the tail -- would change the system
            #    block's text on every compaction, busting the SYSTEM cache
            #    breakpoint too, not just the conversation-region one. Using
            #    "user" keeps this message in the conversation region, where the
            #    provider's ephemeral-exclusion logic can see and skip it.
            # 2. metadata.ephemeral=True + tail position: the Anthropic provider's
            #    `_count_trailing_ephemeral_messages` walks backward from the end
            #    of the conversation and excludes trailing messages carrying
            #    `metadata.ephemeral=True` from cache-breakpoint placement. A
            #    "system"-role message is excluded from that walk entirely (it
            #    never reaches the conversation list), and content anywhere
            #    other than the tail is not "trailing" and would still corrupt
            #    the cached prefix. Tail + ephemeral=True + role != "system" is
            #    the only combination the existing provider fix recognizes.
            #
            # The notice content itself only changes when a NEW compaction
            # escalation actually occurs (see _compact_ephemeral's sticky decision
            # state) -- so on calls between escalations, this tail addition is
            # byte-identical, and everything before it (the real prefix) is
            # completely undisturbed either way.
            if self.compaction_notice_enabled and self._last_compaction_stats:
                level = self._last_compaction_stats.get("strategy_level", 0)
                # GUARD: never append into an unanswered tool_calls turn.
                #
                # Appending at the tail is what makes the notice cache-safe
                # (above), but the tail is not always a safe place to stand: if
                # the view ends with an assistant message carrying tool_calls,
                # its tool results have not been added yet, and a user-role
                # notice would land BETWEEN the tool call and its results.
                # Providers reject or mishandle that interleaving -- the same
                # tool_use/tool_result atomicity the compaction levels work hard
                # to preserve. (This is new exposure from the move to the tail;
                # the old index-1 insert could never land here.)
                #
                # Skip rather than reposition: placing it before the assistant
                # message would put it back INSIDE the prefix, re-introducing
                # exactly the cache-busting this fix exists to prevent. Skipping
                # costs nothing -- the notice is derived from sticky stats that
                # persist, so it reappears on the very next request once the
                # tool results have arrived and the tail is a safe place again.
                if compacted and compacted[-1].get("tool_calls"):
                    logger.debug(
                        "Skipping compaction notice this request: view ends with "
                        "an assistant message with unanswered tool_calls; the "
                        "notice would interleave between tool_use and tool_result. "
                        "It will be appended on the next request instead."
                    )
                elif level >= self.compaction_notice_min_level:
                    notice = self._format_compaction_notice()
                    if notice:
                        compacted.append(
                            {
                                "role": "user",
                                "content": notice,
                                "metadata": {
                                    "source": "context-compaction",
                                    "ephemeral": True,
                                },
                            }
                        )
                        logger.debug(
                            f"Appended compaction notice at tail (level {level}, "
                            f"verbosity: {self.compaction_notice_verbosity})"
                        )

            # Strip internal bookkeeping at the module boundary -- everything
            # above this point (sticky decisions, token accounting) still runs
            # on messages carrying `_seq`; only what leaves has it removed.
            return self._strip_internal_metadata(compacted)

        return self._strip_internal_metadata(working_messages)

    # Metadata keys that are internal bookkeeping only and must never cross
    # the module boundary into a provider-facing view. `_seq` is sticky
    # compaction identity (see _extract_seq): meaningless to a provider, and --
    # because _estimate_tokens stringifies the whole message dict -- it also
    # inflates the token estimate of every message carrying it.
    _INTERNAL_METADATA_KEYS = frozenset({"_seq"})

    def _strip_internal_metadata(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return a provider-facing view with internal-only metadata removed.

        CRITICAL: stored history must KEEP `_seq` -- the sticky decision store
        is keyed on it, so losing it would silently break stickiness (and with
        it, prefix stability). The returned view can share dict objects with
        `self.messages`: the no-compaction path returns stored dicts directly,
        and even the compacted path's `dict(msg)` shallow copies share the SAME
        nested metadata dict. So this NEVER mutates in place -- any message
        needing a strip is rebuilt as a new dict with a new metadata dict, and
        the stored original is left untouched.

        Messages with nothing to strip pass through by identity (no copy),
        which keeps this deterministic and byte-stable call over call.
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            meta = msg.get("metadata")
            if not isinstance(meta, dict) or self._INTERNAL_METADATA_KEYS.isdisjoint(
                meta
            ):
                result.append(msg)
                continue
            result.append(
                {
                    **msg,
                    "metadata": {
                        k: v
                        for k, v in meta.items()
                        if k not in self._INTERNAL_METADATA_KEYS
                    },
                }
            )
        return result

    async def get_messages(self) -> list[dict[str, Any]]:
        """
        Get ALL messages (full history, never compacted) for transcripts/debugging.

        This returns the complete, unmodified history - suitable for saving
        to transcript files for session persistence.
        """
        return list(self.messages)

    async def set_messages(self, messages: list[dict[str, Any]]) -> None:
        """Set messages from a saved transcript (for session resume).

        Sticky compaction decisions live only in this instance's memory (they
        are never persisted alongside the transcript), so a resumed session
        necessarily starts with a clean decision slate -- there is no stale
        state to reconcile against the incoming messages. Sequence ids are
        re-stamped deterministically (0..N-1, in transcript order) rather than
        trusting any `_seq` that might already be present in the incoming
        metadata, so identity stays internally consistent regardless of what
        produced the transcript.
        """
        restamped: list[dict[str, Any]] = []
        for i, msg in enumerate(messages):
            meta = dict(msg.get("metadata") or {})
            meta["_seq"] = i
            restamped.append({**msg, "metadata": meta})
        self.messages = restamped
        self._next_seq = len(restamped)
        self._removed_seqs = set()
        self._truncated_seqs = set()
        self._stubbed_seqs = set()
        self._sticky_level = 0
        self._infeasible_target_reported = False
        self._ineffective_escalations = 0
        self._last_after_tokens = None
        self._escalation_breaker_reported = False
        self._last_compaction_stats = None
        logger.info(f"Restored {len(messages)} messages to context")

    async def clear(self) -> None:
        """Clear all messages."""
        self.messages = []
        self._next_seq = 0
        self._removed_seqs = set()
        self._truncated_seqs = set()
        self._stubbed_seqs = set()
        self._sticky_level = 0
        self._infeasible_target_reported = False
        self._ineffective_escalations = 0
        self._last_after_tokens = None
        self._escalation_breaker_reported = False
        self._last_compaction_stats = None
        logger.info("Context cleared")

    async def should_compact(self) -> bool:
        """Check if context should be compacted.

        Note: This module uses ephemeral compaction during get_messages_for_request(),
        so this always returns False. The actual compaction check happens internally.
        This method exists to satisfy the ContextManager protocol.
        """
        return False

    async def compact(self) -> None:
        """Compact the context.

        Note: This module uses ephemeral compaction during get_messages_for_request(),
        so this is a no-op. Compaction happens automatically when getting messages.
        This method exists to satisfy the ContextManager protocol.
        """
        pass

    def _should_compact(self, token_count: int, budget: int) -> bool:
        """Check if context should be compacted."""
        usage = token_count / budget if budget > 0 else 0
        should = usage >= self.compact_threshold
        if should:
            logger.info(
                f"Context at {usage:.1%} capacity ({token_count:,}/{budget:,} tokens), "
                f"threshold {self.compact_threshold:.0%} - compaction needed"
            )
        return should

    # --- Sticky compaction decision helpers ---
    #
    # These give every compaction decision (remove / truncate / stub) a
    # permanent, stable identity keyed on metadata["_seq"] (assigned once per
    # message in add_message()) instead of list index. Indices shift on every
    # call as history grows and the ephemeral view is rebuilt from scratch;
    # seq ids do not. This is the mechanism that makes the returned view's
    # shared prefix byte-stable across calls where history only grew by a
    # turn or two, instead of re-deriving (and potentially shifting) the
    # entire compaction decision on every single get_messages_for_request()
    # call.

    @staticmethod
    def _extract_seq(msg: dict[str, Any]) -> int | None:
        """Return a message's stable sequence id, or None if it has none.

        Messages without a seq (e.g. a freshly-generated system prompt from
        set_system_prompt_factory(), which never goes through add_message())
        simply cannot participate in sticky tracking -- they are always
        system-role and therefore never candidates for compaction anyway, so
        this is a non-issue in practice, not a silent gap.
        """
        return (msg.get("metadata") or {}).get("_seq")

    def _record_removed(self, msg: dict[str, Any]) -> None:
        """Permanently record that a message has been removed by compaction."""
        seq = self._extract_seq(msg)
        if seq is not None:
            self._removed_seqs.add(seq)
            # A message can only be in one terminal state; removal supersedes
            # any earlier truncate/stub decision for the same seq.
            self._truncated_seqs.discard(seq)
            self._stubbed_seqs.discard(seq)

    def _record_truncated(self, msg: dict[str, Any]) -> None:
        """Permanently record that a tool result has been truncated."""
        seq = self._extract_seq(msg)
        if seq is not None:
            self._truncated_seqs.add(seq)

    def _record_stubbed(self, msg: dict[str, Any]) -> None:
        """Permanently record that a user message has been stubbed."""
        seq = self._extract_seq(msg)
        if seq is not None:
            self._stubbed_seqs.add(seq)

    def _apply_sticky_decisions(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Cheaply (O(n), no search) re-apply every previously-recorded
        compaction decision to a fresh copy of `messages`.

        Returns a NEW list -- `messages` (which may be `self.messages`
        itself, or a filtered view of it) is never mutated. Messages with no
        recorded decision are shallow-copied so downstream in-place mutation
        (there is none today, but this matches the discipline the rest of
        this module already follows) can never leak back into stored
        history.

        This is deterministic given the same input: the exact same messages
        are dropped/truncated/stubbed every time, in the same way, regardless
        of how many times this is called. Only messages with NO recorded
        decision (i.e. newly appended since the last escalation) pass
        through unchanged -- which is exactly the "only the tail grows"
        behavior prefix stability depends on.
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            seq = self._extract_seq(msg)
            if seq is not None and seq in self._removed_seqs:
                continue
            if seq is not None and seq in self._truncated_seqs:
                result.append(self._truncate_tool_result(msg))
            elif seq is not None and seq in self._stubbed_seqs:
                result.append(self._stub_user_message(msg))
            else:
                result.append(dict(msg))
        return result

    async def _compact_ephemeral(
        self, budget: int, source_messages: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """
        Compact the context EPHEMERALLY using progressive interleaved strategy.

        This returns a NEW list - the source messages are NEVER modified.

        CRITICAL: System messages are NEVER compacted. They are extracted at the start
        and re-inserted at the end, guaranteeing they are always preserved regardless
        of compaction pressure.

        Progressive levels (each checks after every operation, stops when at target):
        - Level 1: Truncate oldest 25% of tool results
        - Level 2: Truncate next 25% (now 50% truncated)
        - Level 3: Remove oldest messages (protect 50%)
        - Level 4: Truncate next 25% (now 75% truncated)
        - Level 5: Remove more messages (protect 30%)
        - Level 6: Truncate remaining (except last N)
        - Level 7: Remove more messages (protect 10%)

        Anthropic API requires that tool_use blocks in message N have matching tool_result
        blocks in message N+1. These pairs are treated as atomic units during compaction.

        Args:
            budget: Token budget for compaction target calculation.
            source_messages: Messages to compact. If None, uses self.messages.
                             This allows compacting factory-generated message lists.
        """
        messages_to_compact = (
            source_messages if source_messages is not None else self.messages
        )
        target_tokens = int(budget * self.target_usage)
        old_count = len(messages_to_compact)
        old_tokens = self._estimate_tokens(messages_to_compact)

        # === CRITICAL: Extract system messages FIRST - they are NEVER compacted ===
        # System messages contain the agent's identity and instructions. Losing them
        # causes the agent to lose its persona and capabilities mid-conversation.
        system_messages = [
            dict(msg) for msg in messages_to_compact if msg.get("role") == "system"
        ]
        non_system_messages = [
            msg for msg in messages_to_compact if msg.get("role") != "system"
        ]

        # UNITS CONVENTION (see the block comment below): every "are we under
        # target yet?" comparison in this method and its helpers is TOTAL vs
        # TOTAL. System messages are extracted from `working_messages` but are
        # still part of the request, so this fixed, un-reducible floor must be
        # added back to every token count derived from `working_messages`.
        # Computed once here -- `system_messages` is never modified during
        # compaction, so this value is constant for the whole call.
        system_tokens = self._estimate_tokens(system_messages)

        if system_messages:
            logger.debug(
                f"Preserving {len(system_messages)} system message(s) ({system_tokens:,} tokens) - "
                f"these are NEVER compacted"
            )

        # === STEP 1: cheaply apply decisions already made in a PRIOR escalation ===
        # No search, no candidate selection -- just filter out previously-removed
        # seqs and re-apply previously-recorded truncate/stub transforms. This is
        # the path taken on the vast majority of calls once at least one
        # escalation has happened: deterministic given the same input messages,
        # so it reproduces exactly what was returned last time for anything not
        # newly appended -- which is what keeps the shared prefix byte-stable.
        working_messages = self._apply_sticky_decisions(non_system_messages)

        # === UNITS CONVENTION: TOTAL vs TOTAL, everywhere in this path ===
        #
        # `target_tokens` above is derived from the TOTAL budget
        # (budget * target_usage), so EVERY "are we under threshold/target
        # yet?" comparison below -- the escalation trigger, each level's
        # termination check, and the checks inside the helpers this method
        # calls -- must put a TOTAL (system + conversation) token count on the
        # left-hand side. System tokens are a fixed, un-reducible floor: they
        # cannot be compacted away, but they DO consume budget.
        #
        # This is deliberately one convention applied uniformly rather than
        # two. The alternative (non-system vs `target - system_tokens`) is
        # numerically equivalent, but it would put a number in the logs and
        # stats that differs from the configured knob, and it yields a
        # negative target whenever the system prompt alone exceeds the
        # target. Total-vs-total keeps the compared number identical to the
        # reported number (`after_tokens` / `target_tokens` in the stats and
        # the compaction notice).
        #
        # Mixing the two is a real bug this code has had twice, in two
        # different places:
        #   1. the escalation TRIGGER below silently dropped system tokens,
        #      so with a large system prompt compaction never fired (see
        #      test_large_system_message_counts_toward_compaction_trigger);
        #   2. each level's TERMINATION check compared the NON-SYSTEM total
        #      (which is what the helpers naturally compute, since
        #      `working_messages` excludes system messages) against the
        #      TOTAL-budget target -- so compaction could declare victory at
        #      Level 1 after truncating one small tool result and never
        #      escalate again, silently turning the effective cap into
        #      `target_usage * budget + system_estimate` (see
        #      test_escalation_does_not_stall_at_level_1_with_large_system_message).
        # Hence: `system_tokens` is threaded into every helper that derives a
        # token count from `working_messages`.
        non_system_tokens = self._estimate_tokens(working_messages)
        current_tokens = system_tokens + non_system_tokens

        needs_escalation = (
            budget > 0 and (current_tokens / budget) >= self.compact_threshold
        )

        # FEASIBILITY PRE-CHECK. System messages are never compacted, so once
        # their share alone exceeds the target, the target is arithmetically
        # unreachable -- no escalation level can ever get there. Escalating
        # anyway does not fail; it deletes real conversation on every request to
        # chase a number that cannot come down, pinned at maximum level, while
        # `after_tokens` never moves.
        #
        # Measured on this module before the check existed, with a 12,153-token
        # system prompt against a 40,000 budget (target 10,000) and NO images:
        # level pinned at 8 from the fifth call, `after_tokens` stuck at ~14,776,
        # and `_removed_seqs` ratcheting 6 -> 12 -> 18 -> 30 -> 42 -> 54 of 58
        # messages ever added, the returned view sawtoothing 4 -> 8 -> 4 as
        # history regrew and was destroyed again. Silently: the existing
        # over-budget warning below is gated on `> budget`, and this state sits
        # at 37% of budget.
        #
        # Only skip while the view still fits the ACTUAL budget. Over budget,
        # a partial reduction beats none and we escalate as before.
        target_unreachable = system_tokens > target_tokens
        if needs_escalation and target_unreachable and current_tokens <= budget:
            if not self._infeasible_target_reported:
                logger.warning(
                    f"Compaction target is unreachable and further escalation would only "
                    f"destroy conversation: the system prompt alone is {system_tokens:,} "
                    f"tokens against a target of {target_tokens:,}. System messages are "
                    f"never compacted, so no level can reach the target. The view is "
                    f"{current_tokens:,} tokens, still within the {budget:,} budget, so it "
                    f"is being returned as-is. Reduce the system prompt or raise the budget."
                )
                self._infeasible_target_reported = True
            needs_escalation = False

        # RUNAWAY BREAKER.
        #
        # Every guard above tests whether THIS call's target is reachable. None
        # of them notices that the same answer has been reached over and over.
        # The incident ran 235 compactions across 266 model calls, every one at
        # maximum level, `after_tokens` never moving -- and the only signal was
        # an INFO line that fired 235 times and nobody saw.
        #
        # FREQUENCY IS THE WRONG SIGNAL, and that was the first design here.
        # A session genuinely over budget must keep compacting on every call;
        # freezing it there converts "destroying conversation" into "guaranteed
        # provider rejection", which is worse. The test that caught this is
        # `test_going_over_budget_still_escalates` -- under a frequency breaker
        # the view grew to 61,190 tokens against a 40,000 budget.
        #
        # The signal that actually separates "this workload needs compacting"
        # from "compaction is not working" is EFFECTIVENESS: in the incident
        # `after_tokens` never once dropped below 1,797,300 across all 235
        # passes. Zero improvement, 235 times.
        #
        # Freezing is PREFIX-SAFE by construction: the sticky decisions already
        # made are re-applied unchanged, which is strictly more stable than
        # re-deriving them. Same shape as the two guards above -- decide whether
        # to escalate, never what the view contains.
        if (
            needs_escalation
            and self._ineffective_escalations >= self._MAX_INEFFECTIVE_ESCALATIONS
        ):
            if not self._escalation_breaker_reported:
                logger.error(
                    f"Compaction has escalated {self._ineffective_escalations} "
                    f"times in a row without reducing the result (currently "
                    f"{current_tokens:,} tokens against a {budget:,} budget, "
                    f"target {target_tokens:,}, cumulative level "
                    f"{self._sticky_level}). Further escalation is deleting "
                    f"conversation without moving the number, so it is being "
                    f"stopped and the existing view returned. This is a "
                    f"configuration or estimation problem, not a workload "
                    f"problem: check the system prompt size, the token budget, "
                    f"and whether any single message is larger than the target."
                )
                self._escalation_breaker_reported = True
            needs_escalation = False

        if not needs_escalation:
            # Sticky state alone already keeps us under the threshold that
            # triggered compaction in the first place -- nothing NEW needs
            # deciding this call. Return the already-decided view unchanged;
            # _last_compaction_stats (and therefore the notice) is left as-is
            # from the last real escalation, so its content stays stable too.
            final_messages = system_messages + working_messages
            logger.debug(
                f"Sticky compaction view: {len(final_messages)} messages, "
                f"{current_tokens:,} tokens "
                f"(no new decisions this call; cumulative level so far: {self._sticky_level})"
            )
            return final_messages

        logger.info(
            f"Compacting context (new escalation): {old_count} raw messages, {old_tokens:,} raw tokens "
            f"-> {len(working_messages)} messages, {current_tokens:,} tokens after sticky state "
            f"(target: {target_tokens:,} tokens, {self.target_usage:.0%} of {budget:,})"
        )

        # Get all tool result indices for wave-based truncation
        tool_result_indices = [
            i for i, msg in enumerate(working_messages) if msg.get("role") == "tool"
        ]
        total_tools = len(tool_result_indices)

        # Always protect the last N tool results from truncation
        protected_tool_indices = set(
            tool_result_indices[-self.protected_tool_results :]
        )

        # Calculate wave boundaries (25% chunks)
        wave1_end = int(total_tools * 0.25)
        wave2_end = int(total_tools * 0.50)
        wave3_end = int(total_tools * 0.75)

        total_truncated = 0
        total_removed = 0
        total_stubbed = 0
        max_level_reached = 1

        # === LEVEL 1: Truncate oldest 25% of tool results ===
        truncated, current_tokens = self._truncate_tool_wave(
            working_messages,
            tool_result_indices[:wave1_end],
            protected_tool_indices,
            target_tokens,
            current_tokens,
            system_tokens,
        )
        total_truncated += truncated
        if current_tokens <= target_tokens:
            logger.info(f"Level 1: Truncated {truncated} tool results, reached target")
            return await self._finalize_compaction_with_stats(
                working_messages,
                system_messages,
                old_count,
                old_tokens,
                total_removed,
                total_truncated,
                total_stubbed,
                max_level_reached,
                budget,
                target_tokens,
            )

        # === LEVEL 2: Truncate next 25% (now 50% truncated) ===
        max_level_reached = 2
        truncated, current_tokens = self._truncate_tool_wave(
            working_messages,
            tool_result_indices[wave1_end:wave2_end],
            protected_tool_indices,
            target_tokens,
            current_tokens,
            system_tokens,
        )
        total_truncated += truncated
        if current_tokens <= target_tokens:
            logger.info(
                f"Level 2: Truncated {truncated} more tool results, reached target"
            )
            return await self._finalize_compaction_with_stats(
                working_messages,
                system_messages,
                old_count,
                old_tokens,
                total_removed,
                total_truncated,
                total_stubbed,
                max_level_reached,
                budget,
                target_tokens,
            )

        # === LEVEL 3: Remove oldest messages (use configured protection) ===
        max_level_reached = 3
        level3_protection = self.protected_recent  # Use configured value
        working_messages, removed, stubbed, current_tokens = (
            self._remove_messages_with_protection(
                working_messages,
                target_tokens,
                protected_recent=level3_protection,
                system_tokens=system_tokens,
                budget=budget,
            )
        )
        total_removed += removed
        total_stubbed += stubbed
        if current_tokens <= target_tokens:
            logger.info(
                f"Level 3: Removed {removed} messages, stubbed {stubbed} ({level3_protection:.0%} protected), reached target"
            )
            return await self._finalize_compaction_with_stats(
                working_messages,
                system_messages,
                old_count,
                old_tokens,
                total_removed,
                total_truncated,
                total_stubbed,
                max_level_reached,
                budget,
                target_tokens,
            )

        # === LEVEL 4: Truncate next 25% (now 75% truncated) ===
        max_level_reached = 4
        # Recalculate indices after removal
        tool_result_indices = [
            i for i, msg in enumerate(working_messages) if msg.get("role") == "tool"
        ]
        protected_tool_indices = set(
            tool_result_indices[-self.protected_tool_results :]
        )
        wave3_start = int(len(tool_result_indices) * 0.50)
        wave3_end = int(len(tool_result_indices) * 0.75)

        truncated, current_tokens = self._truncate_tool_wave(
            working_messages,
            tool_result_indices[wave3_start:wave3_end],
            protected_tool_indices,
            target_tokens,
            current_tokens,
            system_tokens,
        )
        total_truncated += truncated
        if current_tokens <= target_tokens:
            logger.info(
                f"Level 4: Truncated {truncated} more tool results, reached target"
            )
            return await self._finalize_compaction_with_stats(
                working_messages,
                system_messages,
                old_count,
                old_tokens,
                total_removed,
                total_truncated,
                total_stubbed,
                max_level_reached,
                budget,
                target_tokens,
            )

        # === LEVEL 5: Remove more messages (60% of configured protection) ===
        max_level_reached = 5
        level5_protection = self.protected_recent * 0.6
        working_messages, removed, stubbed, current_tokens = (
            self._remove_messages_with_protection(
                working_messages,
                target_tokens,
                protected_recent=level5_protection,
                system_tokens=system_tokens,
                budget=budget,
            )
        )
        total_removed += removed
        total_stubbed += stubbed
        if current_tokens <= target_tokens:
            logger.info(
                f"Level 5: Removed {removed} messages, stubbed {stubbed} ({level5_protection:.0%} protected), reached target"
            )
            return await self._finalize_compaction_with_stats(
                working_messages,
                system_messages,
                old_count,
                old_tokens,
                total_removed,
                total_truncated,
                total_stubbed,
                max_level_reached,
                budget,
                target_tokens,
            )

        # === LEVEL 6: Truncate remaining tool results (except last N) ===
        max_level_reached = 6
        tool_result_indices = [
            i for i, msg in enumerate(working_messages) if msg.get("role") == "tool"
        ]
        protected_tool_indices = set(
            tool_result_indices[-self.protected_tool_results :]
        )

        truncated, current_tokens = self._truncate_tool_wave(
            working_messages,
            tool_result_indices,
            protected_tool_indices,
            target_tokens,
            current_tokens,
            system_tokens,
        )
        total_truncated += truncated
        if current_tokens <= target_tokens:
            logger.info(
                f"Level 6: Truncated {truncated} remaining tool results, reached target"
            )
            return await self._finalize_compaction_with_stats(
                working_messages,
                system_messages,
                old_count,
                old_tokens,
                total_removed,
                total_truncated,
                total_stubbed,
                max_level_reached,
                budget,
                target_tokens,
            )

        # === LEVEL 7: Remove more messages (30% of configured protection - last resort) ===
        max_level_reached = 7
        level7_protection = self.protected_recent * 0.3
        working_messages, removed, stubbed, current_tokens = (
            self._remove_messages_with_protection(
                working_messages,
                target_tokens,
                protected_recent=level7_protection,
                system_tokens=system_tokens,
                budget=budget,
            )
        )
        total_removed += removed
        total_stubbed += stubbed

        logger.info(
            f"Level 7 complete ({level7_protection:.0%} protected): "
            f"Truncated {total_truncated} total, removed {total_removed} total, stubbed {total_stubbed} total. "
            f"Tokens: {old_tokens:,} → {current_tokens:,}"
        )

        # Check if we still need more space
        if current_tokens > target_tokens:
            # === LEVEL 8: Stub first user message + remove old stubs (extreme pressure) ===
            max_level_reached = 8

            # Find first user message and stub it if not already stubbed
            first_user_idx = None
            last_user_idx = None
            for i, msg in enumerate(working_messages):
                if msg.get("role") == "user":
                    if first_user_idx is None:
                        first_user_idx = i
                    last_user_idx = i

            # Stub first user message (previously protected) - but NEVER if it's also the last
            # The last user message is the current intent and must always be preserved
            if first_user_idx is not None and first_user_idx != last_user_idx:
                first_msg = working_messages[first_user_idx]
                if not first_msg.get("_stubbed"):
                    # Let `_stub_user_message` decide. An `isinstance(content,
                    # str)` guard here would re-impose the shape-based exemption
                    # that commit 56fecd6 removed FROM THE HELPER -- the helper
                    # handles block content, and a guard at the call site makes
                    # that branch unreachable from production.
                    before_tokens = self._estimate_message_tokens(first_msg)
                    stubbed_msg = self._stub_user_message(first_msg)
                    if stubbed_msg is not first_msg:
                        working_messages[first_user_idx] = stubbed_msg
                        # Sticky: record before `first_msg` var is superseded.
                        self._record_stubbed(first_msg)
                        total_stubbed += 1
                        # UNITS: measured, not derived from `len(content)`.
                        # `len()` on block content is a BLOCK COUNT, so the old
                        # `(len(content) - 70) // 4` was arithmetic on the wrong
                        # quantity the moment the shape stopped being a string --
                        # the same unit-mismatch class as ad7936a.
                        savings = before_tokens - self._estimate_message_tokens(
                            stubbed_msg
                        )
                        current_tokens -= savings
                        logger.info(
                            f"Level 8: Stubbed first user message (saved ~{savings} tokens)"
                        )

            # Remove old stubs if still over target (oldest first, outside protected zone)
            if current_tokens > target_tokens:
                protected_boundary = int(
                    len(working_messages) * (1 - level7_protection)
                )
                old_stub_indices = [
                    i
                    for i, msg in enumerate(working_messages)
                    if msg.get("_stubbed")
                    and i < protected_boundary  # Outside protected recent zone
                    and i != last_user_idx  # Never remove last user message
                ]

                stubs_removed = 0
                indices_to_remove = set()
                for i in old_stub_indices:  # Already sorted oldest-first
                    if current_tokens <= target_tokens:
                        break
                    indices_to_remove.add(i)
                    stubs_removed += 1
                    current_tokens -= 18  # Stub is ~70 chars = ~18 tokens

                if indices_to_remove:
                    # Sticky: record before filtering the list out from under them.
                    for i in indices_to_remove:
                        self._record_removed(working_messages[i])
                    working_messages = [
                        msg
                        for i, msg in enumerate(working_messages)
                        if i not in indices_to_remove
                    ]
                    total_removed += stubs_removed
                    logger.info(f"Level 8: Removed {stubs_removed} old user stubs")

            logger.info(
                f"Level 8 complete (extreme pressure): "
                f"Stubbed {total_stubbed} total, removed {total_removed} total. "
                f"Tokens: {old_tokens:,} → {current_tokens:,}"
            )

        return await self._finalize_compaction_with_stats(
            working_messages,
            system_messages,
            old_count,
            old_tokens,
            total_removed,
            total_truncated,
            total_stubbed,
            max_level_reached,
            budget,
            target_tokens,
        )

    def _truncate_tool_wave(
        self,
        messages: list[dict[str, Any]],
        indices: list[int],
        protected_indices: set[int],
        target_tokens: int,
        current_tokens: int,
        system_tokens: int,
    ) -> tuple[int, int]:
        """
        Truncate a wave of tool results, stopping when target is reached.

        Returns (truncated_count, new_token_count).

        UNITS: `target_tokens`, `current_tokens`, and the returned token count
        are all TOTAL (system + conversation) counts, matching the
        total-budget target computed in `_compact_ephemeral`. `messages` here
        is the NON-SYSTEM working list, so `system_tokens` -- the fixed,
        un-reducible system floor -- must be added to anything derived from
        it. Without that, this would return a non-system count that the
        caller then compares against a total-budget target, and truncating one
        small tool result could make compaction declare victory while total
        usage is still far over budget.

        Performance note: the original implementation called
        `self._estimate_tokens(messages)` (a full O(n) rescan of every message)
        after each individual truncation, making this O(n) per truncation
        candidate -> O(n^2) per wave. Since only ONE message changes per
        truncation, we instead compute the true total ONCE, lazily, the first
        time we actually mutate a message in this call, then maintain it via
        O(1) per-message deltas for every subsequent truncation. This produces
        numerically identical results to the original (which only ever
        recomputed `current_tokens` at the moment of an actual mutation) at
        O(n) total cost for the whole wave instead of O(n * truncated).
        """
        truncated = 0
        true_total: int | None = None
        for i in indices:
            if current_tokens <= target_tokens:
                break
            if i in protected_indices:
                continue
            if i >= len(messages):  # Index may be stale after removals
                continue
            msg = messages[i]
            if msg.get("role") != "tool":  # Verify it's still a tool message
                continue
            if not msg.get("_truncated"):
                if true_total is None:
                    # First mutation in this call: establish the true baseline
                    # once (matches what _estimate_tokens(messages) would have
                    # returned immediately before this mutation).
                    # UNITS: + system_tokens keeps this a TOTAL, so the
                    # `current_tokens <= target_tokens` check above stays
                    # total-vs-total after this first mutation replaces the
                    # caller-supplied (already total) seed value.
                    true_total = self._estimate_tokens(messages) + system_tokens
                # UNITS: content-aware on both sides, matching `true_total`.
                old_len = self._estimate_message_tokens(msg)
                messages[i] = self._truncate_tool_result(msg)
                new_len = self._estimate_message_tokens(messages[i])
                true_total += new_len - old_len
                truncated += 1
                current_tokens = true_total
                # Sticky: record this decision by the message's stable seq id
                # so it is never re-derived (or reversed) on a later call.
                self._record_truncated(msg)
        return truncated, current_tokens

    def _remove_messages_with_protection(
        self,
        messages: list[dict[str, Any]],
        target_tokens: int,
        protected_recent: float,
        system_tokens: int,
        budget: int = 0,
    ) -> tuple[list[dict[str, Any]], int, int, int]:
        """
        Remove oldest messages with specified protection level.

        User messages are NEVER removed - they may be stubbed if still over target.

        Returns (new_messages, removed_count, stubbed_count, new_token_count).

        UNITS: `target_tokens` and the returned token count are TOTAL (system
        + conversation) counts, matching the total-budget target computed in
        `_compact_ephemeral`. `messages` is the NON-SYSTEM working list, so
        `system_tokens` -- the fixed, un-reducible system floor -- is added to
        every count derived from it. Without that, this returns a non-system
        count the caller compares against a total-budget target, and removal
        stops early (or never starts) whenever the system prompt is a large
        fraction of the budget.
        """
        # Determine protected indices
        protected_indices = set()

        # Track user messages for stubbing (NEVER removal)
        user_message_indices = {
            i for i, msg in enumerate(messages) if msg.get("role") == "user"
        }

        # Find first and last user message indices (always fully protected from stubbing too)
        first_user_idx = None
        last_user_idx = None
        for i, msg in enumerate(messages):
            if msg.get("role") == "user":
                if first_user_idx is None:
                    first_user_idx = i
                last_user_idx = i

        # Always protect system messages
        for i, msg in enumerate(messages):
            if msg.get("role") == "system":
                protected_indices.add(i)

        # First user message is stubbable at extreme pressure (Level 8), but never fully removed
        # (It's excluded from removal_candidates via user_message_indices, but can be stubbed)
        # We don't add it to protected_indices so it can be stubbed at Level 8

        # Always protect the LAST user message (current context)
        if last_user_idx is not None:
            protected_indices.add(last_user_idx)

        # Protect last N% of messages (using the passed protection level)
        protected_boundary = int(len(messages) * (1 - protected_recent))
        for i in range(protected_boundary, len(messages)):
            protected_indices.add(i)

        # Removal candidates exclude ALL user messages (they can only be stubbed, not removed)
        removal_candidates = [
            i
            for i in range(len(messages))
            if i not in protected_indices and i not in user_message_indices
        ]

        # Precompute per-message token counts and a tool_call_id -> indices map
        # ONCE per call (i.e. once per compaction level), instead of rescanning
        # the full message list for every removal candidate. `messages` is not
        # mutated until the final `result` list is built below, so both of
        # these stay valid for the entire loop.
        #
        # Performance note: the original implementation recomputed
        # `self._estimate_tokens(messages)` (full O(n) rescan) on every
        # removal candidate even though `messages` never changes during this
        # loop -- that call always returned the same value. It also resummed
        # the ENTIRE (growing) `indices_to_remove` set from scratch every
        # iteration. Both were O(n) work repeated O(n) times -> O(n^2). Since
        # `messages` is constant here, the base total only needs computing
        # once, and the removed-token total only needs an O(1) delta per
        # newly-removed index.
        # UNITS: must match `base_tokens` below, which is content-aware. The
        # old `len(str(msg)) // 4` counted a base64 payload as prose, so
        # removing one image-bearing message credited ~100k tokens against a
        # baseline that had counted it at ~1.6k -- `current_tokens` went hard
        # negative, the loop exited on its first candidate, and compaction
        # silently UNDER-shot. Deltas and baseline must come from one estimator.
        token_lens = [self._estimate_message_tokens(msg) for msg in messages]
        tool_call_id_to_indices: dict[str, list[int]] = {}
        for idx, m in enumerate(messages):
            tcid = m.get("tool_call_id")
            if tcid:
                tool_call_id_to_indices.setdefault(tcid, []).append(idx)

        # Remove messages until under target, preserving tool pairs
        indices_to_remove: set[int] = set()
        removed_tokens_total = 0
        # UNITS: + system_tokens makes this a TOTAL, matching `target_tokens`.
        base_tokens = (
            self._estimate_tokens(messages) + system_tokens
        )  # computed once; messages is constant here
        current_tokens = base_tokens

        # STOP CONDITION FEASIBILITY.
        #
        # The loop below exits when `current_tokens <= target_tokens`. If
        # removing EVERY eligible candidate still cannot reach the target, that
        # condition never becomes true, so the loop runs to exhaustion and
        # deletes everything it is allowed to -- permanently, because
        # `_removed_seqs` is re-applied on every later rebuild -- for no gain.
        #
        # The target is unreachable whenever un-compactable content alone
        # exceeds it. System messages are one way (guarded earlier, before the
        # level ladder) but NOT the only one: the last user message and the last
        # `protected_tool_results` tool results are equally immune. One
        # `read_file` on a large file puts an enormous tool result inside that
        # protected window and arms this.
        #
        # Measured on this module before the clamp, with a 16-token system
        # prompt and no images -- so the earlier system-floor guard cannot fire:
        # a 64-message conversation collapsed to 6 messages on the FIRST call at
        # maximum level, 58 messages removed permanently, and by the second call
        # the view was 229 tokens against a 40,000 budget. The condition that
        # caused it is TRANSIENT -- the blob leaves the protected tool window
        # within a few turns and becomes truncatable -- but the removals are not.
        #
        # So: aim at the requirement that is actually achievable. `target_usage`
        # is a hysteresis preference; fitting the budget is the contract. If even
        # the budget is out of reach this changes nothing and the loop behaves
        # exactly as before.
        if budget > 0:
            achievable_floor = base_tokens - sum(
                token_lens[i] for i in removal_candidates
            )
            if achievable_floor > target_tokens:
                target_tokens = max(target_tokens, budget)

        for i in removal_candidates:
            if current_tokens <= target_tokens:
                break

            msg = messages[i]
            newly_removed: list[int] = []

            # Handle tool result - must remove with its tool_use pair
            if msg.get("role") == "tool":
                pair_removed, newly_removed = self._try_remove_tool_pair_from_result(
                    messages, i, protected_indices, tool_call_id_to_indices
                )
                if not pair_removed:
                    continue  # Can't remove this one, skip

            # Handle assistant with tool_calls - must remove with all its tool results
            elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                pair_removed, tool_result_indices = (
                    self._try_remove_tool_pair_from_assistant(
                        msg, protected_indices, tool_call_id_to_indices
                    )
                )
                if not pair_removed:
                    continue  # Can't remove this one, skip
                newly_removed = [i, *tool_result_indices]

            # Regular message - just mark for removal
            else:
                newly_removed = [i]

            # Update running totals in O(1) per newly-removed index instead of
            # resumming the whole (growing) indices_to_remove set every time.
            for idx in newly_removed:
                if idx not in indices_to_remove:
                    indices_to_remove.add(idx)
                    removed_tokens_total += token_lens[idx]

            current_tokens = base_tokens - removed_tokens_total

        # After removals, stub intermediate user messages if still over target
        # At normal levels (1-7), still protect first/last from stubbing
        # Level 8 will handle first user message stubbing separately
        stub_candidates = sorted(
            [
                i
                for i in user_message_indices
                if i not in protected_indices
                and i != first_user_idx  # Protected from stubbing at levels 1-7
                and i != last_user_idx  # Always protected (never stubbed)
                and not messages[i].get("_stubbed")  # Don't re-stub
            ]
        )

        indices_to_stub = set()
        for i in stub_candidates:
            if current_tokens <= target_tokens:
                break
            msg = messages[i]
            # Same reasoning as the Level 8 site above: the helper owns the
            # "is there anything worth stubbing here" decision for BOTH content
            # shapes, and savings are measured rather than derived from
            # `len(content)`, which is a block count for block content.
            stubbed_msg = self._stub_user_message(msg)
            if stubbed_msg is not msg:
                indices_to_stub.add(i)
                savings = self._estimate_message_tokens(
                    msg
                ) - self._estimate_message_tokens(stubbed_msg)
                current_tokens -= savings

        # Sticky: record these NEW decisions by stable seq id before building
        # the result, so they are never re-derived (or reversed) on a later
        # call. `indices_to_remove`/`indices_to_stub` here only ever contain
        # candidates that weren't already decided (removed messages are
        # already absent from `messages` by the time this runs; stub
        # candidates explicitly exclude already-`_stubbed` messages above).
        for i in indices_to_remove:
            self._record_removed(messages[i])
        for i in indices_to_stub:
            self._record_stubbed(messages[i])

        # Build result with stubs
        result = []
        for i, msg in enumerate(messages):
            if i in indices_to_remove:
                continue
            if i in indices_to_stub:
                result.append(self._stub_user_message(msg))
            else:
                result.append(msg)

        # UNITS: + system_tokens so the caller receives a TOTAL to compare
        # against its total-budget `target_tokens`.
        final_tokens = self._estimate_tokens(result) + system_tokens

        return result, len(indices_to_remove), len(indices_to_stub), final_tokens

    def _try_remove_tool_pair_from_result(
        self,
        messages: list[dict[str, Any]],
        result_idx: int,
        protected_indices: set[int],
        tool_call_id_to_indices: dict[str, list[int]],
    ) -> tuple[bool, list[int]]:
        """Try to remove a tool result and its paired assistant.

        Returns (success, newly_removed_indices). Does NOT mutate any shared
        state -- the caller is responsible for adding the returned indices to
        its removal set and accounting for their token cost.
        """
        # Find the assistant with tool_calls
        for j in range(result_idx - 1, -1, -1):
            check_msg = messages[j]
            if check_msg.get("role") == "assistant" and check_msg.get("tool_calls"):
                if j in protected_indices:
                    return False, []  # Can't remove protected assistant

                # Check if ALL tool_results for this assistant can be removed
                all_removable, tool_result_indices = self._check_tool_pair_removable(
                    check_msg, protected_indices, tool_call_id_to_indices
                )

                if all_removable:
                    return True, [j, *tool_result_indices]
                return False, []
            if check_msg.get("role") != "tool":
                break
        return False, []

    def _try_remove_tool_pair_from_assistant(
        self,
        assistant_msg: dict[str, Any],
        protected_indices: set[int],
        tool_call_id_to_indices: dict[str, list[int]],
    ) -> tuple[bool, list[int]]:
        """Try to remove an assistant with tool_calls and all its results.

        Returns (success, newly_removed_indices) -- the assistant's own index
        is not known to this helper, so the caller must include it separately
        if desired (see call site: it prepends the assistant's own index).
        """
        all_removable, tool_result_indices = self._check_tool_pair_removable(
            assistant_msg, protected_indices, tool_call_id_to_indices
        )

        if all_removable:
            return True, tool_result_indices
        return False, []

    def _check_tool_pair_removable(
        self,
        assistant_msg: dict[str, Any],
        protected_indices: set[int],
        tool_call_id_to_indices: dict[str, list[int]],
    ) -> tuple[bool, list[int]]:
        """Check if all tool results for an assistant can be removed. Returns (all_removable, result_indices).

        Performance note: uses a precomputed tool_call_id -> message-index map
        (built once per removal pass, see `_remove_messages_with_protection`)
        instead of scanning the full message list for every tool_call_id on
        every removal candidate.
        """
        all_removable = True
        tool_result_indices = []

        for tc in assistant_msg.get("tool_calls", []):
            tc_id = tc.get("id") or tc.get("tool_call_id")
            if tc_id:
                for k in tool_call_id_to_indices.get(tc_id, []):
                    if k in protected_indices:
                        all_removable = False
                    else:
                        tool_result_indices.append(k)

        return all_removable, tool_result_indices

    async def _finalize_compaction_with_stats(
        self,
        working_messages: list[dict[str, Any]],
        system_messages: list[dict[str, Any]],
        old_count: int,
        old_tokens: int,
        total_removed: int,
        total_truncated: int,
        total_stubbed: int,
        max_level_reached: int,
        budget: int,
        target_tokens: int,
    ) -> list[dict[str, Any]]:
        """Log final compaction state, store stats, emit event, and return the result.

        CRITICAL: This function prepends the preserved system messages to the compacted
        working messages, ensuring system messages are ALWAYS in the final result.
        """
        # === CRITICAL: Prepend system messages to final result ===
        # System messages were extracted before compaction and must be restored
        final_messages = system_messages + working_messages

        final_tokens = self._estimate_tokens(final_messages)
        system_count = len(system_messages)
        tool_use_count = sum(1 for m in final_messages if m.get("tool_calls"))
        tool_result_count = sum(1 for m in final_messages if m.get("role") == "tool")

        logger.info(
            f"Compaction complete: {old_count} → {len(final_messages)} messages, "
            f"{old_tokens:,} → {final_tokens:,} tokens "
            f"({system_count} system, {tool_use_count} tool_use, {tool_result_count} tool_result preserved)"
        )

        # === SANITY CHECK: Verify system messages are present ===
        result_system_count = sum(
            1 for m in final_messages if m.get("role") == "system"
        )
        if result_system_count != system_count:
            logger.error(
                f"CRITICAL: System message count mismatch! Expected {system_count}, got {result_system_count}. "
                f"This indicates a bug in compaction logic."
            )

        # === BUDGET GUARD: compaction finished, but the view is STILL over budget ===
        # Compaction can only shrink the CONVERSATION. System messages are an
        # un-reducible floor (see the units convention in _compact_ephemeral),
        # so once the system share alone exceeds the target, the target is
        # arithmetically unreachable -- no escalation level can ever get there.
        # That state previously ended here in total silence: an over-budget
        # view returned with no signal at all, which is the one failure mode
        # this module cannot afford, since it manages every session's memory.
        # Say it out loud, and name the system share as the floor so the
        # operator knows which knob actually moves (shrink the system prompt,
        # or raise the budget -- compacting harder will not help).
        system_tokens = self._estimate_tokens(system_messages)
        # Fires on the DESTRUCTIVE condition, not only when over budget. A view
        # can sit far under budget while compaction runs at maximum level and
        # deletes on every request, which is the state that previously ended here
        # in total silence -- the gate was `> budget` while the damage begins at
        # `> target_tokens`.
        if budget > 0 and (final_tokens > budget or system_tokens > target_tokens):
            if system_tokens > target_tokens:
                cause = (
                    f"the system prompt ALONE is {system_tokens:,} tokens, which already "
                    f"exceeds the compaction target of {target_tokens:,} tokens. System "
                    f"messages are never compacted, so this is an un-reducible floor: NO "
                    f"amount of conversation compaction can reach the target. Reduce the "
                    f"system prompt or raise the budget"
                )
            else:
                cause = (
                    f"the un-compactable system floor is {system_tokens:,} tokens against "
                    f"a target of {target_tokens:,}; the rest is protected content (last "
                    f"user message, last {self.protected_tool_results} tool results, "
                    f"tool_use/tool_result pairs) that compaction is not permitted to drop"
                )
            logger.warning(
                f"Compaction finished OVER BUDGET at level {max_level_reached}: "
                f"{final_tokens:,} tokens against a budget of {budget:,} "
                f"({final_tokens / budget:.0%} of budget, target {target_tokens:,}) -- "
                f"{cause}."
            )

        # Cumulative high-water mark across ALL escalations ever, not just
        # this one -- monotonic, never goes backward. This is what feeds the
        # compaction notice, so its content only changes when a genuinely
        # NEW escalation happens, keeping it stable across the (much more
        # common) calls where sticky state alone was already sufficient.
        self._sticky_level = max(self._sticky_level, max_level_reached)

        # Build and store stats for observability. Removed/truncated/stubbed
        # counts are read from the sticky decision store itself (cumulative
        # totals across every escalation this context manager has ever made),
        # not the per-call deltas passed in -- this reports the total
        # accumulated effect on the conversation, which is what the notice
        # and any observability consumer actually wants to know.
        # EFFECTIVENESS BOOKKEEPING for the runaway breaker at the escalation
        # gate. A pass that cannot move the result is the incident's signature:
        # `after_tokens` never once dropped below 1,797,300 across all 235
        # passes. Compare against the PREVIOUS result rather than this pass's
        # own before/after, because the pathology is that successive passes all
        # land on the same number.
        # A pass is INEFFECTIVE when it finishes still over the real budget.
        #
        # Two earlier definitions were wrong, and each was caught by
        # `test_going_over_budget_still_escalates` rather than by reasoning:
        #
        #   - "escalated N times in a row" punishes a session that is genuinely
        #     over budget and must compact on every call.
        #   - "did not reduce vs the previous pass" punishes compaction that is
        #     correctly HOLDING THE LINE while new turns arrive. A result that
        #     plateaus just under budget is compaction working, not failing.
        #
        # Landing over budget is the honest failure signal: the view being
        # returned will not fit, so the pass did not accomplish the one thing it
        # exists to do. In the incident every pass landed at ~1.84x the budget,
        # 235 times running.
        if budget > 0 and final_tokens > budget:
            self._ineffective_escalations += 1
        else:
            self._ineffective_escalations = 0
        self._last_after_tokens = final_tokens

        stats = {
            "before_tokens": old_tokens,
            "after_tokens": final_tokens,
            "ineffective_escalations": self._ineffective_escalations,
            "before_messages": old_count,
            "after_messages": len(final_messages),
            "messages_removed": len(self._removed_seqs),
            "messages_truncated": len(self._truncated_seqs),
            "user_messages_stubbed": len(self._stubbed_seqs),
            "system_messages_preserved": system_count,
            "strategy_level": self._sticky_level,
            "budget": budget,
            "target_tokens": target_tokens,
            "protected_recent": self.protected_recent,
            "protected_tool_results": self.protected_tool_results,
        }
        self._last_compaction_stats = stats

        # Emit event if hooks available
        if self._hooks is not None:
            try:
                await self._hooks.emit("context:compaction", stats)
            except Exception as e:
                logger.warning(f"Could not emit compaction event: {e}")

        return final_messages

    def _truncate_tool_result(self, msg: dict[str, Any]) -> dict[str, Any]:
        """
        Truncate a tool result message to reduce token count.

        Returns a NEW dict - does not modify the original.
        """
        content = msg.get("content", "")
        if not isinstance(content, str) or len(content) <= self.truncate_chars:
            return msg

        original_tokens = len(content) // 4
        return {
            **msg,
            "content": f"[truncated: ~{original_tokens:,} tokens - call tool again if needed] {content[: self.truncate_chars]}...",
            "_truncated": True,
            "_original_tokens": original_tokens,
        }

    _MAX_INEFFECTIVE_ESCALATIONS = 10
    """Consecutive INEFFECTIVE escalations before the runaway breaker trips.

    The incident ran **235 compactions across 266 model calls**, all pinned at
    maximum strategy, with `after_tokens` never once dropping below 1,797,300.
    Zero improvement, 235 times. Nothing anywhere counted, and the only signal
    was an INFO line that fired 235 times and nobody saw.

    Counting *frequency* was the obvious first design and it is wrong: a session
    that is genuinely over budget must keep compacting every call, and freezing
    it there converts "destroying conversation" into "guaranteed provider
    rejection". Ineffectiveness is the signal that actually separates "this
    workload needs compacting" from "compaction is not working".
    """

    def _stub_text(self, content: str) -> str:
        """The stub body: a 50-char preview of what was there."""
        preview = content[:50].replace("\n", " ").strip()
        if len(content) > 50:
            preview += "..."
        return f'[User message compacted - original: "{preview}"]'

    def _stub_user_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        """
        Create a stub for a user message to preserve thread while reducing tokens.

        Handles BOTH content shapes. A message whose content is a list of blocks
        used to be returned unchanged, because the guard was
        ``isinstance(content, str)`` -- so a multimodal message was structurally
        exempt from the only mechanism that could shrink it, while a small
        text-only message carrying the user's actual instructions could still be
        stubbed. Protection ran by TYPE rather than by cost.

        Non-text blocks are preserved rather than stubbed: they are counted at a
        flat cost by ``_estimate_content_tokens``, so removing one buys almost
        nothing and loses the attachment. Only the text is compacted.

        Returns a NEW dict - does not modify the original.
        """
        content = msg.get("content", "")

        if isinstance(content, str):
            if len(content) <= 80:
                return msg  # Too short to stub
            return {
                **msg,
                "content": self._stub_text(content),
                "_stubbed": True,
                "_original_length": len(content),
            }

        if isinstance(content, list):
            text_blocks = [
                block
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            joined = "".join(str(block.get("text", "")) for block in text_blocks)
            if len(joined) <= 80:
                return msg  # Nothing worth stubbing; attachments stay as they are
            preserved = [
                block
                for block in content
                if not (isinstance(block, dict) and block.get("type") == "text")
            ]
            return {
                **msg,
                "content": [
                    {"type": "text", "text": self._stub_text(joined)},
                    *preserved,
                ],
                "_stubbed": True,
                "_original_length": len(joined),
            }

        return msg

    def _format_compaction_notice(self) -> str:
        """
        Format compaction notice based on last compaction stats.

        This method can be overridden by subclasses to customize the notice
        format for different compaction strategies.

        Returns:
            Formatted notice string, or empty string if no stats available.
        """
        if not self._last_compaction_stats:
            return ""

        stats = self._last_compaction_stats
        level = stats.get("strategy_level", 0)

        # Handle different verbosity levels
        if self.compaction_notice_verbosity == "minimal":
            return (
                '<system-reminder source="context-compaction">\n'
                "Context has been compacted to fit within token budget. "
                "Some older messages and tool results may be truncated.\n"
                "</system-reminder>"
            )

        # Build level-specific affected items (for normal and verbose)
        affected = self._format_affected_items(level, stats)

        # Normal verbosity (default)
        old_count = stats.get("before_messages", 0)
        new_count = stats.get("after_messages", 0)
        removed = stats.get("messages_removed", 0)
        stubbed = stats.get("user_messages_stubbed", 0)
        truncated = stats.get("messages_truncated", 0)
        old_tokens = stats.get("before_tokens", 0)
        new_tokens = stats.get("after_tokens", 0)
        target_tokens = stats.get("target_tokens", 0)
        protected_recent = stats.get("protected_recent", 0.0)
        protected_tool_results = stats.get("protected_tool_results", 0)

        notice = f"""<system-reminder source="context-compaction">
Context has been compacted to fit within token budget.

Compaction summary:
- Strategy level: {level}/8
- Messages: {old_count} → {new_count} ({removed} removed, {stubbed} stubbed)
- Tool results: {truncated} truncated
- Tokens: {old_tokens:,} → {new_tokens:,} (target: {target_tokens:,})

What was preserved:
- All system messages (your instructions and identity)
- Last user message (current task)
- Recent messages ({protected_recent:.0%} of conversation)
- Last {protected_tool_results} tool results (full content)

What may be affected:
{affected}

Note: This compaction is ephemeral (affects only this request). Full history is preserved in session transcript.
</system-reminder>"""

        return notice

    def _format_affected_items(self, level: int, stats: dict[str, Any]) -> str:
        """
        Format affected items based on compaction level.

        Args:
            level: Compaction strategy level (1-8)
            stats: Compaction statistics dictionary

        Returns:
            Formatted string describing what may be affected at this level.
        """
        if level <= 2:
            return (
                '- Older tool results are truncated with "[truncated: ~N tokens]" prefix\n'
                "- You can re-run tools if you need the full output"
            )
        elif level <= 5:
            return (
                '- Older tool results are truncated with "[truncated: ~N tokens]" prefix\n'
                "- Older conversation messages have been removed\n"
                "- You can re-run tools if you need full output"
            )
        else:  # level 6-8
            n = stats.get("protected_tool_results", 5)
            return (
                f"- Most tool results are truncated (except last {n})\n"
                "- Significant conversation history has been removed\n"
                '- Some user messages may be stubbed as "[User message compacted...]"\n'
                "- If context is critical, consider asking user to clarify their current goal"
            )

    def _calculate_budget(self, token_budget: int | None, provider: Any | None) -> int:
        """Calculate effective token budget from provider or fallback to config.

        Priority:
        1. Explicit token_budget parameter (deprecated but supported)
        2. Provider model info (context_window - reserved_output - safety_margin)
        3. Provider defaults (legacy: some providers may put limits here)
        4. Configured max_tokens fallback

        Note: We reserve only 50% of max_output_tokens since most responses are
        much smaller than the maximum. This prevents over-conservative budgets
        that would trigger compaction too early.
        """
        # Explicit budget takes precedence (for backward compatibility)
        if token_budget is not None:
            logger.debug(f"Using explicit token_budget: {token_budget}")
            return token_budget

        safety_margin = 4096  # Buffer to avoid hitting hard limits
        output_reserve_fraction = self.output_reserve_fraction

        # Try provider-based dynamic budget
        if provider is not None:
            try:
                # First, try to get model info if provider exposes current model
                # Some providers have get_model_info() or similar
                if hasattr(provider, "get_model_info"):
                    model_info = provider.get_model_info()
                    if model_info:
                        context_window = getattr(model_info, "context_window", None)
                        max_output = getattr(model_info, "max_output_tokens", None)
                        if context_window and max_output:
                            reserved_output = int(max_output * output_reserve_fraction)
                            budget = context_window - reserved_output - safety_margin
                            logger.info(
                                f"Budget from provider model info: {budget:,} "
                                f"(context={context_window:,}, reserved_output={reserved_output:,} "
                                f"[{output_reserve_fraction:.0%} of {max_output:,}])"
                            )
                            return budget

                # Check provider info defaults (legacy approach)
                info = provider.get_info()
                defaults = info.defaults or {}
                context_window = defaults.get("context_window")
                max_output_tokens = defaults.get("max_output_tokens")

                if context_window and max_output_tokens:
                    reserved_output = int(max_output_tokens * output_reserve_fraction)
                    budget = context_window - reserved_output - safety_margin
                    logger.info(
                        f"Budget from provider defaults: {budget:,} "
                        f"(context={context_window:,}, reserved_output={reserved_output:,} "
                        f"[{output_reserve_fraction:.0%} of {max_output_tokens:,}])"
                    )
                    return budget
                else:
                    logger.debug(
                        f"Provider defaults missing context_window ({context_window}) "
                        f"or max_output_tokens ({max_output_tokens}), using fallback"
                    )
            except Exception as e:
                logger.debug(f"Could not get budget from provider: {e}")

        # Fall back to configured max_tokens
        logger.info(f"Using fallback max_tokens budget: {self.max_tokens:,}")
        return self.max_tokens

    # A non-text content block costs a flat approximation, never the length of
    # its payload. A base64 image measured as len(payload)/4 reads as hundreds
    # of thousands of tokens while actually costing ~1-2k, and because such a
    # message is structurally protected from shrinking, the compactor's target
    # becomes unreachable: it re-runs on every request, deleting real
    # conversation to chase a number that cannot come down. Any fixed value in
    # the low thousands is ~1000x closer to truth than the payload length.
    _NON_TEXT_BLOCK_TOKENS = 1600
    _NON_TEXT_BLOCK_TYPES = frozenset(
        {
            "image",
            "image_url",
            "input_image",
            "input_audio",
            "audio",
            "video",
            "document",
            "file",
        }
    )

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Rough token estimation, content-aware.

        Text is counted at chars/4. Non-text blocks are counted at a flat
        per-block cost rather than the size of their encoded payload -- see
        ``_NON_TEXT_BLOCK_TOKENS``.
        """
        return sum(self._estimate_message_tokens(msg) for msg in messages)

    def _estimate_message_tokens(self, msg: dict[str, Any]) -> int:
        """Estimate one message: envelope overhead plus content."""
        if not isinstance(msg, dict):
            return len(str(msg)) // 4
        envelope = {key: value for key, value in msg.items() if key != "content"}
        overhead = len(str(envelope)) // 4 if envelope else 0
        return overhead + self._estimate_content_tokens(msg.get("content"))

    def _estimate_content_tokens(self, content: Any) -> int:
        """Estimate a content value, descending into structured blocks."""
        if content is None:
            return 0
        if isinstance(content, str):
            return len(content) // 4
        if not isinstance(content, list):
            return len(str(content)) // 4

        total = 0
        for block in content:
            if not isinstance(block, dict):
                total += len(str(block)) // 4
                continue
            if block.get("type") in self._NON_TEXT_BLOCK_TYPES:
                total += self._NON_TEXT_BLOCK_TOKENS
                continue
            # A tool_result can carry blocks of its own, including images.
            nested = block.get("content")
            if isinstance(nested, (list, str)):
                envelope = {k: v for k, v in block.items() if k != "content"}
                total += len(str(envelope)) // 4 + self._estimate_content_tokens(nested)
                continue
            total += len(str(block)) // 4
        return total
