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
        self._hooks = hooks
        self._last_compaction_stats: dict[str, Any] | None = None
        self._system_prompt_factory: Callable[[], Awaitable[str]] | None = None

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
        existing_meta = message.get("metadata", {})
        if "timestamp" not in existing_meta:
            message = {
                **message,
                "metadata": {
                    **existing_meta,
                    "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
                },
            }

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
                or msg.get("metadata", {}).get("source") == "hook"
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
        if self._should_compact(token_count, effective_budget):
            # Compact EPHEMERALLY - returns new list, working_messages unchanged
            compacted = await self._compact_ephemeral(
                effective_budget, working_messages
            )
            logger.info(
                f"Ephemeral compaction: {len(working_messages)} -> {len(compacted)} messages for this request"
            )

            # Insert compaction notice if enabled and level threshold met
            if self.compaction_notice_enabled and self._last_compaction_stats:
                level = self._last_compaction_stats.get("strategy_level", 0)
                if level >= self.compaction_notice_min_level:
                    notice = self._format_compaction_notice()
                    if notice:
                        # Insert at position 1 (after main system message at position 0)
                        compacted.insert(
                            1,
                            {
                                "role": "system",
                                "content": notice,
                                "metadata": {
                                    "source": "context-compaction",
                                    "ephemeral": True,
                                },
                            },
                        )
                        logger.debug(
                            f"Inserted compaction notice at position 1 (level {level}, "
                            f"verbosity: {self.compaction_notice_verbosity})"
                        )

            return compacted

        return working_messages

    async def get_messages(self) -> list[dict[str, Any]]:
        """
        Get ALL messages (full history, never compacted) for transcripts/debugging.

        This returns the complete, unmodified history - suitable for saving
        to transcript files for session persistence.
        """
        return list(self.messages)

    async def set_messages(self, messages: list[dict[str, Any]]) -> None:
        """Set messages from a saved transcript (for session resume)."""
        self.messages = list(messages)
        logger.info(f"Restored {len(messages)} messages to context")

    async def clear(self) -> None:
        """Clear all messages."""
        self.messages = []
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

        logger.info(
            f"Compacting context: {len(messages_to_compact)} messages, {old_tokens:,} tokens "
            f"(target: {target_tokens:,} tokens, {self.target_usage:.0%} of {budget:,})"
        )

        # === CRITICAL: Extract system messages FIRST - they are NEVER compacted ===
        # System messages contain the agent's identity and instructions. Losing them
        # causes the agent to lose its persona and capabilities mid-conversation.
        system_messages = [
            dict(msg) for msg in messages_to_compact if msg.get("role") == "system"
        ]
        non_system_messages = [
            msg for msg in messages_to_compact if msg.get("role") != "system"
        ]

        if system_messages:
            system_tokens = self._estimate_tokens(system_messages)
            logger.info(
                f"Preserving {len(system_messages)} system message(s) ({system_tokens:,} tokens) - "
                f"these are NEVER compacted"
            )

        # Work on non-system messages only - system messages bypass all compaction
        working_messages = [dict(msg) for msg in non_system_messages]
        current_tokens = old_tokens

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
                working_messages, target_tokens, protected_recent=level3_protection
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
                working_messages, target_tokens, protected_recent=level5_protection
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
                working_messages, target_tokens, protected_recent=level7_protection
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
                    content = first_msg.get("content", "")
                    if isinstance(content, str) and len(content) > 80:
                        working_messages[first_user_idx] = self._stub_user_message(
                            first_msg
                        )
                        total_stubbed += 1
                        savings = (len(content) - 70) // 4
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
    ) -> tuple[int, int]:
        """
        Truncate a wave of tool results, stopping when target is reached.

        Returns (truncated_count, new_token_count).
        """
        truncated = 0
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
                messages[i] = self._truncate_tool_result(msg)
                truncated += 1
                current_tokens = self._estimate_tokens(messages)
        return truncated, current_tokens

    def _remove_messages_with_protection(
        self,
        messages: list[dict[str, Any]],
        target_tokens: int,
        protected_recent: float,
    ) -> tuple[list[dict[str, Any]], int, int, int]:
        """
        Remove oldest messages with specified protection level.

        User messages are NEVER removed - they may be stubbed if still over target.

        Returns (new_messages, removed_count, stubbed_count, new_token_count).
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

        # Remove messages until under target, preserving tool pairs
        indices_to_remove = set()
        current_tokens = self._estimate_tokens(messages)

        for i in removal_candidates:
            if current_tokens <= target_tokens:
                break

            msg = messages[i]

            # Handle tool result - must remove with its tool_use pair
            if msg.get("role") == "tool":
                pair_removed = self._try_remove_tool_pair_from_result(
                    messages, i, protected_indices, indices_to_remove
                )
                if not pair_removed:
                    continue  # Can't remove this one, skip

            # Handle assistant with tool_calls - must remove with all its tool results
            elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                pair_removed = self._try_remove_tool_pair_from_assistant(
                    messages, i, msg, protected_indices, indices_to_remove
                )
                if not pair_removed:
                    continue  # Can't remove this one, skip

            # Regular message - just mark for removal
            else:
                indices_to_remove.add(i)

            # Update token estimate after each removal decision
            removed_tokens = sum(
                len(str(messages[idx])) // 4 for idx in indices_to_remove
            )
            current_tokens = self._estimate_tokens(messages) - removed_tokens

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
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 80:
                indices_to_stub.add(i)
                savings = (len(content) - 70) // 4  # Stub is ~70 chars
                current_tokens -= savings

        # Build result with stubs
        result = []
        for i, msg in enumerate(messages):
            if i in indices_to_remove:
                continue
            if i in indices_to_stub:
                result.append(self._stub_user_message(msg))
            else:
                result.append(msg)

        final_tokens = self._estimate_tokens(result)

        return result, len(indices_to_remove), len(indices_to_stub), final_tokens

    def _try_remove_tool_pair_from_result(
        self,
        messages: list[dict[str, Any]],
        result_idx: int,
        protected_indices: set[int],
        indices_to_remove: set[int],
    ) -> bool:
        """Try to remove a tool result and its paired assistant. Returns True if successful."""
        # Find the assistant with tool_calls
        for j in range(result_idx - 1, -1, -1):
            check_msg = messages[j]
            if check_msg.get("role") == "assistant" and check_msg.get("tool_calls"):
                if j in protected_indices:
                    return False  # Can't remove protected assistant

                # Check if ALL tool_results for this assistant can be removed
                all_removable, tool_result_indices = self._check_tool_pair_removable(
                    messages, check_msg, protected_indices
                )

                if all_removable:
                    indices_to_remove.add(j)
                    for k in tool_result_indices:
                        indices_to_remove.add(k)
                    return True
                return False
            if check_msg.get("role") != "tool":
                break
        return False

    def _try_remove_tool_pair_from_assistant(
        self,
        messages: list[dict[str, Any]],
        assistant_idx: int,
        assistant_msg: dict[str, Any],
        protected_indices: set[int],
        indices_to_remove: set[int],
    ) -> bool:
        """Try to remove an assistant with tool_calls and all its results. Returns True if successful."""
        all_removable, tool_result_indices = self._check_tool_pair_removable(
            messages, assistant_msg, protected_indices
        )

        if all_removable:
            indices_to_remove.add(assistant_idx)
            for k in tool_result_indices:
                indices_to_remove.add(k)
            return True
        return False

    def _check_tool_pair_removable(
        self,
        messages: list[dict[str, Any]],
        assistant_msg: dict[str, Any],
        protected_indices: set[int],
    ) -> tuple[bool, list[int]]:
        """Check if all tool results for an assistant can be removed. Returns (all_removable, result_indices)."""
        all_removable = True
        tool_result_indices = []

        for tc in assistant_msg.get("tool_calls", []):
            tc_id = tc.get("id") or tc.get("tool_call_id")
            if tc_id:
                for k, m in enumerate(messages):
                    if m.get("tool_call_id") == tc_id:
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

        # Build and store stats for observability
        stats = {
            "before_tokens": old_tokens,
            "after_tokens": final_tokens,
            "before_messages": old_count,
            "after_messages": len(final_messages),
            "messages_removed": total_removed,
            "messages_truncated": total_truncated,
            "user_messages_stubbed": total_stubbed,
            "system_messages_preserved": system_count,
            "strategy_level": max_level_reached,
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

    def _stub_user_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        """
        Create a stub for a user message to preserve thread while reducing tokens.

        Returns a NEW dict - does not modify the original.
        """
        content = msg.get("content", "")
        if not isinstance(content, str) or len(content) <= 80:
            return msg  # Too short to stub

        # Take first 50 chars, clean up for display
        preview = content[:50].replace("\n", " ").strip()
        if len(content) > 50:
            preview += "..."

        return {
            **msg,
            "content": f'[User message compacted - original: "{preview}"]',
            "_stubbed": True,
            "_original_length": len(content),
        }

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

        safety_margin = 1000  # Buffer to avoid hitting hard limits
        output_reserve_fraction = (
            0.5  # Reserve 50% of max output (most responses are smaller)
        )

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

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Rough token estimation (chars / 4)."""
        return sum(len(str(msg)) // 4 for msg in messages)
