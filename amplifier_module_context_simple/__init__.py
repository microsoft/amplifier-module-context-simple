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

Real-Usage Token Meter (opt-in, default off -- see config `token_meter`):
  • The compaction trigger normally runs entirely off an uncalibrated
    len(str)//4 estimator (see _estimate_tokens) that is never reconciled
    against what the provider actually billed -- measured roughly 2x off
    in production sessions.
  • When hooks are available, this module always registers a listener on
    the canonical `llm:response` event and records the provider's own
    reported usage (see SimpleContextManager._on_llm_response) as an
    observability signal, exposed via `_last_token_meter_stats`, so the
    estimator-vs-real drift is visible even in the default mode.
  • Set `token_meter: "actual"` in config to additionally have the
    compaction trigger itself USE that real measurement once one has
    arrived this session (falling back to the estimator before then, or
    whenever hooks/events are unavailable). Default is `"estimate"`, which
    keeps behavior byte-identical to before this feature existed.
  • Set `token_meter: "hybrid"` to ANCHOR on the provider's own reported
    total from the last response and apply the heuristic ONLY to items
    appended since that anchor (openai/codex's shape), carrying the
    PROVENANCE of the resulting number -- `kind` in
    {'usage','estimated','none'} -- on every count
    (deepseek-harness's shape). Two guards ride with it:
      - CONSERVATISM: if the provider total is below what the heuristic
        priced for the same billed content, the anchor is rejected and
        the count is honestly marked kind='estimated'.
      - REFUSE TO GUESS: optional cache aggregates are reported only when
        EVERY usage event this session carried them; otherwise undefined.
    G-METER-PROVENANCE: in this mode no irreversible action (the
    compaction trigger) is taken on a count that is not kind='usage'.
    The single recorded escape -- no anchor has EVER arrived AND the
    count has reached 100% of budget, where refusing would guarantee a
    provider hard-failure -- fires, logs a warning, and is counted
    separately as a provenance OVERRIDE rather than a clean fire.
  • ALL THREE meters (estimate / actual / hybrid) are computed on every
    request in EVERY mode and reported via `_last_token_meter_stats` and
    the `context:token_meter` event, so their divergence is measurable
    without changing which one drives the trigger.
  • Ported from amplifier-module-context-handoff's proven `_on_llm_response`
    meter. See README "Real-usage token meter" for the full rationale.

Summary Compaction Strategy (opt-in, default off -- see config
`compaction_strategy`):
  • `compaction_strategy: "progressive"` (default) is this module's
    existing truncate/remove ladder, completely unchanged -- byte-identical
    to before this feature existed.
  • `compaction_strategy: "summary"` absorbs the oldest non-protected span
    into an LLM-generated rolling summary instead of truncating/removing
    it. The IDEAS (structured 5-section prompt, early-async-trigger
    design) are lifted from amplifier-bundle-context-managed's rolling
    summarizer; ALL plumbing is rebuilt on this module's own sticky/_seq
    machinery rather than that donor's index-based splice-and-swap -- see
    the "Summary compaction strategy" section in
    _select_summary_absorb_seqs/_snap_absorb_boundary/
    _swap_in_pending_summary below for why, and README "Summary
    compaction strategy" for the measured donor defects this avoids
    (a dropped tool-call/result pair, and a `role: "system"` summary tier
    that measurably busted the provider's system-prompt cache breakpoint).
  • The summary message is role="user" (never "system"), wrapped in a
    `<system-reminder source="context-summary">` envelope, and persists as
    stable history (not ephemeral, unlike the tail compaction notice).
  • MOTIVATED by retention (the progressive ladder is lossy; a summary
    keeps a lossy-but-real account of the absorbed span). It is NOT a
    cache-cost play: like the progressive ladder, this still shrinks what
    the model sees each turn, which under a grow-only cache is still a
    cold rebuild at the moment of absorption.
  • MEASURED (T0/T1 eval, n=3 vs n=5, S5-CRAC -- see README "Summary
    compaction strategy" for the full table): the mechanism is validated
    (zero tool-pair errors; agent system prompt byte-stable, 1 hash/run;
    append-only) but NO retention benefit is demonstrated -- 94.0 vs 94.4
    on a SATURATED metric (both arms 40/40 constraints, 20/20
    post-compaction, every run). Absence of evidence, not evidence of
    parity-by-design; a discriminating scenario does not exist yet.
  • KNOWN ISSUE, measured: +83% run cost and +84% compaction boundaries
    vs the progressive baseline, via a boundary-refire loop (absorbing a
    span shrinks the request below summary_trigger, so it refires
    sooner). The summarizer itself is only 8-11% of run cost. Not fixed
    here; see README for the candidate levers (cooldown / absolute floor
    / trigger hysteresis). OPT-IN, EXPERIMENTAL -- do not enable by
    default.
"""

# Amplifier module metadata
__amplifier_module_type__ = "context"

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amplifier_core import ModuleCoordinator

logger = logging.getLogger(__name__)

# token_meter config values. "estimate" (default) preserves pre-existing
# behavior exactly; "actual" lets a real llm:response measurement drive the
# compaction trigger once one has arrived this session. See module
# docstring "Real-Usage Token Meter" and
# SimpleContextManager._measure_working_tokens.
TOKEN_METER_ESTIMATE = "estimate"
TOKEN_METER_ACTUAL = "actual"
TOKEN_METER_HYBRID = "hybrid"
_VALID_TOKEN_METERS = (
    TOKEN_METER_ESTIMATE,
    TOKEN_METER_ACTUAL,
    TOKEN_METER_HYBRID,
)

# Provenance of a token count (`kind`), reported on EVERY count this module
# produces -- lifted from deepseek-harness's `baseline.kind`. "usage" means
# the number is anchored on the provider's own reported usage;
# "estimated" means it came from the len(str)//4 heuristic (in whole or in
# part, or the anchor was rejected by the conservatism guard); "none" means
# there was nothing to price. G-METER-PROVENANCE: in `hybrid` mode, no
# irreversible action may be taken on a count whose kind is not "usage".
METER_KIND_USAGE = "usage"
METER_KIND_ESTIMATED = "estimated"
METER_KIND_NONE = "none"

# compaction_strategy config values. "progressive" (default) preserves the
# existing truncate/remove ladder exactly (byte-identical -- see module
# docstring "Summary compaction strategy"). "summary" opts in to absorbing
# the oldest non-protected span into an LLM-generated rolling summary
# instead of truncating/removing it outright.
COMPACTION_STRATEGY_PROGRESSIVE = "progressive"
COMPACTION_STRATEGY_SUMMARY = "summary"
_VALID_COMPACTION_STRATEGIES = (
    COMPACTION_STRATEGY_PROGRESSIVE,
    COMPACTION_STRATEGY_SUMMARY,
)

# The summary message's envelope source tag and metadata type marker. The
# envelope is what makes foundation's is_real_user_message() classify this
# role="user" message as NOT a real user turn (see module docstring); the
# metadata type marker is how this module recognizes its own past summary
# messages (so they are never re-absorbed into a later summary).
_SUMMARY_ENVELOPE_SOURCE = "context-summary"
_SUMMARY_METADATA_TYPE = "context_summary"

# Default 5-section summarization prompt, lifted near-verbatim from
# amplifier-bundle-context-managed's modules/context-managed/__init__.py:71-97
# (the donor's structured summarization prompt -- see README "Summary
# compaction strategy" for full provenance). The donor's two
# `read_transcript` tool references are deliberately dropped: this module
# ships no transcript tool, and pointing an agent at a tool that does not
# exist would be actively misleading. File-overridable via
# `summarization_prompt_path`, mirroring the donor's own
# `summarization_prompt_path` config knob.
DEFAULT_SUMMARIZATION_PROMPT = """\
Produce a compact summary of the conversation so far. Use the following sections:

## User Requests & Decisions
List the key requests made by the user and any important decisions reached.

## Files Examined or Modified
List files that were read, analyzed, or modified during the conversation.

## Errors Encountered & Resolutions
Describe any errors, failures, or unexpected behavior encountered, and how they were resolved.

## Current Task State
Describe the current state of work -- what has been completed, what is in progress, and what remains.

## Key Technical Details
Note any important technical constraints, patterns, configurations, or implementation details
discovered during the conversation.

## Guidelines
- Be factual and concise. Do not speculate beyond what the conversation contains.
- Preserve numeric values, file paths, error messages, and command outputs exactly.
- Each section may be omitted if there is nothing to report for it.
"""


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
            - token_meter: "estimate" (default), "actual" or "hybrid".
              "estimate" is byte-identical to pre-existing behavior.
              "hybrid" anchors on the provider's own reported total and
              estimates only the un-billed tail, carrying provenance
              (`kind`) that gates irreversible actions -- see module
              docstring. "actual" drives the
              compaction trigger from real provider usage (input_tokens +
              cache_write_tokens, observed via the `llm:response` hook)
              once at least one response has been observed this session,
              falling back to the estimator before then. An unrecognized
              value falls back to "estimate" with a logged warning rather
              than crashing mount(). See module docstring.
            - compaction_strategy: "progressive" (default) or "summary". See
              module docstring "Summary compaction strategy". An
              unrecognized value falls back to "progressive" with a logged
              warning rather than crashing mount().
            - summary_trigger: Usage fraction (0.0-1.0) at which the summary
              strategy starts an async background summarization call, well
              ahead of compact_threshold so it has time to finish before
              tokens must actually be shed (default: 0.60). Only consulted
              when compaction_strategy == "summary". KNOWN ISSUE: because
              absorbing a span shrinks the request back below this
              fraction, an aggressive (low) trigger refires sooner and
              measurably multiplies compaction boundaries -- +84%
              boundaries / +83% run cost in the T0/T1 eval. Raising this
              is the cheapest lever; see README "Known issue: boundary
              refire".
            - summarization_model: Model identifier passed to the summarizer's
              ChatRequest (default: None, i.e. provider default).
            - summarization_prompt_path: Path to a file overriding
              DEFAULT_SUMMARIZATION_PROMPT (default: None).
            - summarization_timeout_s: Seconds to wait for the summarizer's
              provider.complete() call before treating it as a failure and
              falling back to progressive compaction for that pass
              (default: 30.0).

    Returns:
        Cleanup callable that unregisters the token-meter hook (if one was
        registered).
    """
    config = config or {}

    token_meter = config.get("token_meter", TOKEN_METER_ESTIMATE)
    if token_meter not in _VALID_TOKEN_METERS:
        logger.warning(
            f"context-simple: unknown token_meter {token_meter!r} (expected "
            f"one of {_VALID_TOKEN_METERS!r}); falling back to "
            f"{TOKEN_METER_ESTIMATE!r}"
        )
        token_meter = TOKEN_METER_ESTIMATE

    compaction_strategy = config.get(
        "compaction_strategy", COMPACTION_STRATEGY_PROGRESSIVE
    )
    if compaction_strategy not in _VALID_COMPACTION_STRATEGIES:
        logger.warning(
            f"context-simple: unknown compaction_strategy {compaction_strategy!r} "
            f"(expected one of {_VALID_COMPACTION_STRATEGIES!r}); falling back to "
            f"{COMPACTION_STRATEGY_PROGRESSIVE!r}"
        )
        compaction_strategy = COMPACTION_STRATEGY_PROGRESSIVE

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
        token_meter=token_meter,
        compaction_strategy=compaction_strategy,
        summary_trigger=config.get("summary_trigger", 0.60),
        summarization_model=config.get("summarization_model"),
        summarization_prompt_path=config.get("summarization_prompt_path"),
        summarization_timeout_s=config.get("summarization_timeout_s", 30.0),
        hooks=getattr(coordinator, "hooks", None),
    )

    # Always register the meter listener when hooks are available, regardless
    # of token_meter mode: recording is a no-op on trigger behavior unless
    # token_meter == "actual" consults it (see _measure_working_tokens), so
    # this keeps the default ("estimate") mode's behavior byte-identical
    # while still populating _last_token_meter_stats for observability.
    unregister: Callable[[], None] | None = None
    hooks = getattr(coordinator, "hooks", None)
    if hooks is not None:
        unregister = hooks.register(
            "llm:response",
            context._on_llm_response,
            priority=50,
            name="context-simple-meter",
        )

    await coordinator.mount("context", context)
    logger.info(f"Mounted SimpleContextManager (token_meter={token_meter!r})")

    async def cleanup() -> None:
        if unregister is not None:
            unregister()

    return cleanup


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
        token_meter: str = TOKEN_METER_ESTIMATE,
        compaction_strategy: str = COMPACTION_STRATEGY_PROGRESSIVE,
        summary_trigger: float = 0.60,
        summarization_model: str | None = None,
        summarization_prompt_path: str | None = None,
        summarization_timeout_s: float = 30.0,
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
            token_meter: "estimate" (default, byte-identical to pre-existing
                behavior), "hybrid" (provider-anchored total + heuristic
                for the un-billed tail, with provenance gating
                irreversible actions), or "actual" (compaction trigger uses real provider
                usage from the `llm:response` hook once observed this
                session -- see module docstring "Real-Usage Token Meter").
                An unrecognized value falls back to "estimate" with a
                logged warning rather than raising.
            compaction_strategy: "progressive" (default, byte-identical to
                pre-existing behavior) or "summary" -- see module docstring
                "Summary compaction strategy". An unrecognized value falls
                back to "progressive" with a logged warning rather than
                raising.
            summary_trigger: Usage fraction (0.0-1.0) at which the summary
                strategy kicks off an async background summarization call.
                Only consulted when compaction_strategy == "summary".
                KNOWN ISSUE (measured): a low trigger refires soon after
                each absorption shrinks the request, multiplying
                compaction boundaries (+84%) and run cost (+83%) -- see
                module docstring and README "Known issue: boundary
                refire".
            summarization_model: Model identifier for the summarizer's own
                ChatRequest. None uses the provider's default model.
            summarization_prompt_path: Path to a file overriding
                DEFAULT_SUMMARIZATION_PROMPT. None uses the built-in prompt.
            summarization_timeout_s: Seconds to wait for the summarizer's
                provider.complete() call before treating it as a failure.
            hooks: Optional hooks instance for emitting observability events
                and (always, when present) recording real usage for the
                token meter via `llm:response` -- see `_on_llm_response`.
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
        if token_meter not in _VALID_TOKEN_METERS:
            logger.warning(
                f"context-simple: unknown token_meter {token_meter!r} (expected "
                f"one of {_VALID_TOKEN_METERS!r}); falling back to "
                f"{TOKEN_METER_ESTIMATE!r}"
            )
            token_meter = TOKEN_METER_ESTIMATE
        self.token_meter = token_meter
        if compaction_strategy not in _VALID_COMPACTION_STRATEGIES:
            logger.warning(
                f"context-simple: unknown compaction_strategy {compaction_strategy!r} "
                f"(expected one of {_VALID_COMPACTION_STRATEGIES!r}); falling back to "
                f"{COMPACTION_STRATEGY_PROGRESSIVE!r}"
            )
            compaction_strategy = COMPACTION_STRATEGY_PROGRESSIVE
        self.compaction_strategy = compaction_strategy
        self.summary_trigger = summary_trigger
        self.summarization_model = summarization_model
        self.summarization_prompt_path = summarization_prompt_path
        self.summarization_timeout_s = summarization_timeout_s
        self._hooks = hooks
        self._last_compaction_stats: dict[str, Any] | None = None
        # --- Summary compaction strategy state (compaction_strategy == "summary") ---
        # Unused, and never touched, in the default "progressive" mode.
        self._cached_provider: Any = None
        self._is_summarizing: bool = False
        self._pending_summary: dict[str, Any] | None = None
        self._summarization_failures: int = 0
        self._summarization_task: "asyncio.Task[None] | None" = None
        self._summary_absorbed_count: int = 0
        # Real-usage token meter state (see _on_llm_response /
        # _measure_working_tokens). `_last_measured_prompt_tokens` holds the
        # most recent real usage observed via `llm:response`
        # (input_tokens + cache_write_tokens), or None before the first one
        # arrives this session. `_last_token_meter_stats` is the
        # always-populated observability surface (updated on every
        # get_messages_for_request() call, regardless of whether compaction
        # fires) so evals can read estimator-vs-real drift even in
        # "estimate" mode -- see README "Real-usage token meter".
        self._last_measured_prompt_tokens: int | None = None
        self._last_token_meter_stats: dict[str, Any] | None = None
        # --- Hybrid meter state (token_meter == "hybrid") ---
        # `_anchor_seq` is `_next_seq` frozen at the moment the anchor was
        # recorded: every message whose `_seq` is >= it was appended AFTER
        # the request the provider billed, so it is exactly the un-billed
        # tail the heuristic is still allowed to price (codex's shape).
        # `_anchor_estimate` is what the heuristic said about the view that
        # was actually SENT on that request -- the comparand for the
        # conservatism guard (deepseek's shape). `_last_sent_estimate` is
        # the running value that becomes `_anchor_estimate` when the next
        # llm:response arrives.
        self._anchor_seq: int | None = None
        self._anchor_estimate: int | None = None
        self._last_sent_estimate: int | None = None
        self._last_hybrid_tokens: int | None = None
        self._last_hybrid_kind: str = METER_KIND_NONE
        # Refuse-to-guess accounting for optional cache aggregates: they are
        # reported ONLY when every usage event seen this session carried
        # them, else undefined (None). Never partially summed.
        self._usage_events: int = 0
        self._usage_events_with_cache: int = 0
        self._usage_cache_read_total: int = 0
        self._usage_cache_write_total: int = 0
        # G-METER-PROVENANCE accounting (observability; see _should_compact).
        self._provenance_refusals: int = 0
        self._provenance_overrides: int = 0
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

        # Summary compaction strategy needs a provider handle to call the
        # summarizer -- cache the latest one seen (mirrors how the donor
        # module caches it, context-managed:365-367). No-op in the default
        # "progressive" mode.
        if self.compaction_strategy == COMPACTION_STRATEGY_SUMMARY and provider is not None:
            self._cached_provider = provider

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

        (
            token_count,
            meter_source,
            estimated_tokens,
            meter,
        ) = self._measure_working_tokens(working_messages)
        self._last_token_meter_stats = {
            "mode": self.token_meter,
            "source": meter_source,
            # Provenance of `used_tokens` -- present on 100% of counts, in
            # every mode (G-METER-PROVENANCE).
            "kind": meter["kind"],
            "used_tokens": token_count,
            # All three meters, computed simultaneously on every request,
            # regardless of which one is actually driving the trigger. This is
            # what makes the estimate-vs-hybrid-vs-actual divergence
            # measurable without changing behaviour (POC-05 G-METER-DELTA).
            "estimated_tokens": estimated_tokens,
            "measured_tokens": self._last_measured_prompt_tokens,
            "hybrid_tokens": meter["hybrid_tokens"],
            "hybrid_kind": meter["hybrid_kind"],
            "anchor_tokens": meter["anchor_tokens"],
            "anchor_estimate": meter["anchor_estimate"],
            "anchor_rejected": meter["anchor_rejected"],
            "tail_estimated_tokens": meter["tail_estimated_tokens"],
            "tail_messages": meter["tail_messages"],
            # Undefined (None) unless EVERY usage event this session reported
            # cache fields -- refuse to guess, never a partial sum.
            "cache_aggregates": self._cache_aggregates(),
            "provenance_refusals": self._provenance_refusals,
            "provenance_overrides": self._provenance_overrides,
            "budget": effective_budget,
            "ratio": (token_count / effective_budget) if effective_budget > 0 else None,
        }

        # Observability emit: lets an eval harness capture all three meters
        # per request without patching this module. Never raises, never
        # changes the view that is returned.
        if self._hooks is not None:
            try:
                await self._hooks.emit(
                    "context:token_meter", dict(self._last_token_meter_stats)
                )
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"Could not emit context:token_meter event: {e}")

        # Summary compaction strategy: trigger an async background
        # summarization call EARLY (well before compact_threshold, so it has
        # time to finish -- see module docstring "Summary compaction
        # strategy" and _maybe_trigger_summary_compaction). No-op in the
        # default "progressive" mode. Never raises, never blocks this turn.
        if self.compaction_strategy == COMPACTION_STRATEGY_SUMMARY:
            await self._maybe_trigger_summary_compaction(token_count, effective_budget)

        # Check if compaction needed (using effective budget with notice reserve deducted)
        if self._should_compact(token_count, effective_budget, meter["kind"]):
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
            return self._finalize_view(compacted)

        return self._finalize_view(working_messages)

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
        # Seqs were just restamped from 0, so any anchor split recorded
        # against the OLD numbering is meaningless -- and worse, would
        # silently classify restored history as an un-billed tail. Drop it;
        # the meter re-anchors on the next llm:response.
        self._reset_hybrid_meter_state()
        self._removed_seqs = set()
        self._truncated_seqs = set()
        self._stubbed_seqs = set()
        self._sticky_level = 0
        self._last_compaction_stats = None
        self._reset_summary_strategy_state()
        logger.info(f"Restored {len(messages)} messages to context")

    async def clear(self) -> None:
        """Clear all messages."""
        self.messages = []
        self._next_seq = 0
        self._removed_seqs = set()
        self._truncated_seqs = set()
        self._stubbed_seqs = set()
        self._sticky_level = 0
        self._last_compaction_stats = None
        self._last_measured_prompt_tokens = None
        self._last_token_meter_stats = None
        self._reset_hybrid_meter_state()
        self._reset_summary_strategy_state()
        logger.info("Context cleared")

    def _reset_summary_strategy_state(self) -> None:
        """Reset all `compaction_strategy == "summary"` state -- called from
        both set_messages() and clear() so a resumed/cleared session never
        carries stale in-flight summarization state across the reset. A
        no-op in the default "progressive" mode (the fields are simply
        never populated in the first place).

        Cancels any in-flight background summarization task rather than
        leaving it to run against a context that has just been reset out
        from under it.
        """
        if self._summarization_task is not None and not self._summarization_task.done():
            self._summarization_task.cancel()
        self._cached_provider = None
        self._is_summarizing = False
        self._pending_summary = None
        self._summarization_failures = 0
        self._summarization_task = None
        self._summary_absorbed_count = 0

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

    def _should_compact(
        self, token_count: int, budget: int, kind: str | None = None
    ) -> bool:
        """Check if context should be compacted.

        `kind` is the PROVENANCE of `token_count` (see METER_KIND_*). It is
        only consulted in `token_meter: "hybrid"` mode, where G-METER-PROVENANCE
        applies: firing compaction is an irreversible action (it destroys the
        provider's prompt cache for at least one request and permanently
        records sticky truncate/remove decisions), so it may NOT be taken on a
        number the provider never anchored.

        The one deliberate escape, recorded rather than hidden: if NO anchor
        has ever arrived this session AND the count has reached 100% of
        budget, refusing would guarantee a provider hard-failure on the very
        next request, which is strictly worse than acting on an estimate. That
        path fires, logs a warning, and is counted separately in
        `_provenance_overrides` so a gate reports it honestly instead of it
        looking like a clean pass. It cannot mask a guard-rejected anchor: it
        requires that no measurement exists at all.
        """
        usage = token_count / budget if budget > 0 else 0
        should = usage >= self.compact_threshold
        if (
            should
            and self.token_meter == TOKEN_METER_HYBRID
            and kind is not None
            and kind != METER_KIND_USAGE
        ):
            if self._last_measured_prompt_tokens is None and usage >= 1.0:
                self._provenance_overrides += 1
                logger.warning(
                    f"context-simple: token_meter='hybrid' firing compaction on an "
                    f"UNANCHORED count ({token_count:,} tokens = {usage:.1%} of "
                    f"budget) because no provider usage has been observed this "
                    f"session and the count has reached the hard ceiling; "
                    f"refusing would guarantee a context-overflow failure. "
                    f"Recorded as a provenance override, not a clean fire."
                )
                return True
            self._provenance_refusals += 1
            logger.info(
                f"context-simple: token_meter='hybrid' REFUSING to fire compaction "
                f"on kind={kind!r} ({token_count:,} tokens = {usage:.1%} of budget) "
                f"-- G-METER-PROVENANCE: no irreversible action on an un-anchored "
                f"number. Waiting for provider-reported usage."
            )
            return False
        if should:
            logger.info(
                f"Context at {usage:.1%} capacity ({token_count:,}/{budget:,} tokens), "
                f"threshold {self.compact_threshold:.0%} - compaction needed"
            )
        return should

    def _exceeds_threshold(self, estimated_tokens: int, budget: int) -> bool:
        """Whether usage is at/above compact_threshold -- the shared gate
        used both by the outer trigger (_should_compact, via
        _measure_working_tokens) and by _compact_ephemeral's internal
        escalation check.

        In token_meter="actual" mode with a real measurement available, the
        REAL measurement decides this, not `estimated_tokens` -- see module
        docstring "Real-Usage Token Meter" and _measure_working_tokens for
        the identical mode/fallback logic. In token_meter="hybrid" mode the
        hybrid number decides it, but ONLY when that number is anchored
        (kind == 'usage'); an estimated hybrid count falls through to the
        estimator branch rather than driving an escalation -- the same
        G-METER-PROVENANCE rule _should_compact applies to the outer trigger.
        Falls back to `estimated_tokens` in "estimate" mode, or whenever no
        real measurement has arrived yet.

        NOTE (unchanged from "actual" mode, and equally true here): only the
        GATE uses the anchored number. The SIZING of the reduction --
        target_tokens and every per-level termination check -- is still the
        estimator throughout, because a billed token count for a hypothetical
        smaller message set does not exist without another round-trip.
        """
        if budget <= 0:
            return False
        if (
            self.token_meter == TOKEN_METER_ACTUAL
            and self._last_measured_prompt_tokens is not None
        ):
            return (self._last_measured_prompt_tokens / budget) >= self.compact_threshold
        if (
            self.token_meter == TOKEN_METER_HYBRID
            and self._last_hybrid_tokens is not None
            and self._last_hybrid_kind == METER_KIND_USAGE
        ):
            return (self._last_hybrid_tokens / budget) >= self.compact_threshold
        return (estimated_tokens / budget) >= self.compact_threshold

    def _reset_hybrid_meter_state(self) -> None:
        """Drop every hybrid-meter reading. Called from clear() and from
        set_messages() (which restamps `_seq` from 0, invalidating any
        recorded anchor split). Observability-only in the default
        "estimate" mode -- these fields never drive that mode's trigger."""
        self._anchor_seq = None
        self._anchor_estimate = None
        self._last_sent_estimate = None
        self._last_hybrid_tokens = None
        self._last_hybrid_kind = METER_KIND_NONE
        self._usage_events = 0
        self._usage_events_with_cache = 0
        self._usage_cache_read_total = 0
        self._usage_cache_write_total = 0

    def _finalize_view(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip internal metadata and record the heuristic price of the view
        that is actually being SENT.

        That recorded number is the conservatism guard's comparand: when the
        next `llm:response` arrives, its provider total describes THIS view,
        so "is the provider total below what the heuristic would say for the
        same content?" is only a meaningful question against the estimate of
        the view that was sent -- not against the full, uncompacted history.
        """
        view = self._strip_internal_metadata(messages)
        self._last_sent_estimate = self._estimate_tokens(view)
        return view

    def _cache_aggregates(self) -> dict[str, int] | None:
        """Session cache aggregates, or None if UNDEFINED.

        Refuse-to-guess rule, lifted from deepseek-harness's
        `deriveTurnTokenUsage`: an optional aggregate is reported only when
        EVERY usage event observed this session reported the underlying
        fields. One event missing them makes the aggregate undefined -- a
        partial sum that silently under-reports is worse than no number.
        """
        if self._usage_events == 0:
            return None
        if self._usage_events_with_cache != self._usage_events:
            return None
        return {
            "events": self._usage_events,
            "cache_read_tokens": self._usage_cache_read_total,
            "cache_write_tokens": self._usage_cache_write_total,
        }

    def _hybrid_split(
        self, working_messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split `working_messages` into (billed prefix, un-billed tail) at
        the recorded anchor.

        A message belongs to the tail iff it carries a `_seq` >= `_anchor_seq`
        -- i.e. it was appended after the request the provider billed.
        Messages with no `_seq` at all (the factory-generated system prompt)
        are treated as PREFIX: they were part of the billed request, and
        pricing them into the tail on top of an anchor that already contains
        them would double-count the single largest block in the window.
        """
        if self._anchor_seq is None:
            return list(working_messages), []
        prefix: list[dict[str, Any]] = []
        tail: list[dict[str, Any]] = []
        for msg in working_messages:
            seq = self._extract_seq(msg)
            if seq is not None and seq >= self._anchor_seq:
                tail.append(msg)
            else:
                prefix.append(msg)
        return prefix, tail

    def _measure_hybrid(
        self, working_messages: list[dict[str, Any]], estimated_tokens: int
    ) -> dict[str, Any]:
        """Compute the hybrid (provider-anchored + provenance) token count.

            total = provider_reported_total_from_the_last_llm_response
                  + estimate(items appended since that response)

        This is codex's shape (never price the whole window by heuristic when
        the provider has already said what the window cost; heuristic only the
        un-billed tail) with deepseek's provenance and conservatism guard
        bolted on.

        Returns a dict carrying the number AND its provenance. Computed on
        every request in every mode, so the estimate-vs-hybrid-vs-actual
        divergence is observable without changing which meter drives the
        trigger.

        Guards:
          * CONSERVATISM -- if the provider's total is BELOW what the
            heuristic priced for the same sent content, the anchor is not
            trustworthy as a floor; reject it, report the (larger) full
            heuristic, and mark kind='estimated'. Never let a number that may
            under-state occupancy authorise an irreversible action.
          * REFUSE TO GUESS -- with no anchor at all this is honestly
            kind='estimated', not a hybrid number wearing a usage label.
        """
        if not working_messages:
            return {
                "hybrid_tokens": 0,
                "hybrid_kind": METER_KIND_NONE,
                "anchor_tokens": None,
                "anchor_estimate": None,
                "anchor_rejected": False,
                "tail_estimated_tokens": 0,
                "tail_messages": 0,
                "reason": "no_messages",
            }

        anchor = self._last_measured_prompt_tokens
        if anchor is None:
            return {
                "hybrid_tokens": estimated_tokens,
                "hybrid_kind": METER_KIND_ESTIMATED,
                "anchor_tokens": None,
                "anchor_estimate": None,
                "anchor_rejected": False,
                "tail_estimated_tokens": None,
                "tail_messages": None,
                "reason": "no_anchor",
            }

        _prefix, tail = self._hybrid_split(working_messages)
        tail_estimate = self._estimate_tokens(tail)

        if anchor <= 0:
            # A non-positive provider total is not a measurement of anything.
            # OBSERVED LIVE, not hypothetical: during this feature's own
            # divergence capture a provider returned HTTP 200 with an
            # all-zero usage block mid-run (input_tokens=0, output_tokens=0)
            # on a ~38k-token request. Trusting that as an anchor would have
            # asserted the context was EMPTY. The comparand guard below
            # catches it whenever a sent-estimate exists; this catches the
            # case where one does not.
            return {
                "hybrid_tokens": estimated_tokens,
                "hybrid_kind": METER_KIND_ESTIMATED,
                "anchor_tokens": anchor,
                "anchor_estimate": self._anchor_estimate,
                "anchor_rejected": True,
                "tail_estimated_tokens": tail_estimate,
                "tail_messages": len(tail),
                "reason": "anchor_non_positive",
            }

        if self._anchor_estimate is not None and anchor < self._anchor_estimate:
            # Conservatism guard fired: the provider total is smaller than the
            # heuristic price of the very content it billed, so it cannot be
            # trusted as a floor for this window.
            return {
                "hybrid_tokens": estimated_tokens,
                "hybrid_kind": METER_KIND_ESTIMATED,
                "anchor_tokens": anchor,
                "anchor_estimate": self._anchor_estimate,
                "anchor_rejected": True,
                "tail_estimated_tokens": tail_estimate,
                "tail_messages": len(tail),
                "reason": "anchor_below_heuristic",
            }

        return {
            "hybrid_tokens": anchor + tail_estimate,
            "hybrid_kind": METER_KIND_USAGE,
            "anchor_tokens": anchor,
            "anchor_estimate": self._anchor_estimate,
            "anchor_rejected": False,
            "tail_estimated_tokens": tail_estimate,
            "tail_messages": len(tail),
            "reason": None,
        }

    def _measure_working_tokens(
        self, working_messages: list[dict[str, Any]]
    ) -> tuple[int, str, int, dict[str, Any]]:
        """Return (token_count, source, estimated_tokens, meter) used to
        evaluate the compaction trigger this call.

        `estimated_tokens` is ALWAYS the len(str)//4 heuristic over
        `working_messages` (see _estimate_tokens), and `meter` ALWAYS carries
        the hybrid number too -- both computed unconditionally so all three
        meters (estimate / actual / hybrid) are observable per request via
        `_last_token_meter_stats` regardless of mode.

        `token_count`/`source` are what actually drives `_should_compact`:

        - token_meter == "estimate" (default): ALWAYS `estimated_tokens`,
          source "estimate" -- regardless of whether a real measurement is
          available. This is what keeps the default mode's behavior
          byte-identical to before this meter existed.
        - token_meter == "actual": the last real usage recorded from
          `llm:response` (input_tokens + cache_write_tokens -- see
          `_on_llm_response`), source "measured", if one has arrived this
          session; otherwise falls back to `estimated_tokens`, source
          "estimate".
        - token_meter == "hybrid": the provider-anchored total plus a
          heuristic price for the un-billed tail, source "hybrid" -- see
          `_measure_hybrid`. Its provenance (`kind`) rides along and gates
          irreversible actions in `_should_compact`.

        `meter["kind"]` is the provenance of the RETURNED `token_count` (not
        of the hybrid number, which is reported separately as `hybrid_kind`)
        -- so 100% of counts this module produces carry a provenance, in
        every mode.
        """
        estimated_tokens = self._estimate_tokens(working_messages)
        hybrid = self._measure_hybrid(working_messages, estimated_tokens)
        self._last_hybrid_tokens = hybrid["hybrid_tokens"]
        self._last_hybrid_kind = hybrid["hybrid_kind"]

        if (
            self.token_meter == TOKEN_METER_ACTUAL
            and self._last_measured_prompt_tokens is not None
        ):
            meter = {**hybrid, "kind": METER_KIND_USAGE}
            return (
                self._last_measured_prompt_tokens,
                "measured",
                estimated_tokens,
                meter,
            )

        if self.token_meter == TOKEN_METER_HYBRID:
            meter = {**hybrid, "kind": hybrid["hybrid_kind"]}
            return hybrid["hybrid_tokens"], "hybrid", estimated_tokens, meter

        kind = METER_KIND_NONE if not working_messages else METER_KIND_ESTIMATED
        meter = {**hybrid, "kind": kind}
        return estimated_tokens, "estimate", estimated_tokens, meter

    async def _on_llm_response(self, event: str, data: dict[str, Any]) -> Any:
        """Hook handler for the canonical `llm:response` event -- records the
        provider's OWN reported usage (ground truth) for the real-usage
        token meter (`token_meter: "actual"`).

        Ported from amplifier-module-context-handoff's `_on_llm_response`
        (see that module's README "Live demonstration" for the production
        incident that shaped this exact formula). Per PROVIDER_CONTRACT.md,
        `usage.input_tokens` is the provider's own GROSS total (fresh +
        cache_read combined) billed as "input" for the call that was just
        made. That figure alone UNDER-counts true context-window occupancy
        on any call that performs a first-time cache write: a large
        system/tool prompt written to cache for the first time is billed
        almost entirely as `cache_write_tokens`, disjoint from
        `input_tokens` -- a real session showed `input_tokens=2` and
        `cache_write_tokens=161,165` on its very first call. This meter
        therefore sums `input_tokens + cache_write_tokens`.
        `cache_read_tokens` is NOT added separately: it is already inside
        the gross `input_tokens` figure per the contract, and adding it
        again would double-count.

        Always records (regardless of `token_meter` config) so the
        estimator-vs-real drift is observable via `_last_token_meter_stats`
        even in "estimate" mode; only `token_meter: "actual"` ever uses this
        reading to DRIVE the compaction trigger -- see
        `_measure_working_tokens`.

        Never raises: any malformed/missing usage payload is logged at
        DEBUG and leaves the meter unchanged (falls back to the estimator
        wherever it's consulted), so a broken/unexpected event shape can
        never crash the agent loop or the request it was about to serve.
        """
        from amplifier_core.models import HookResult

        usage = (data or {}).get("usage") or {}
        input_tokens = usage.get("input_tokens")
        cache_write_tokens = usage.get("cache_write_tokens") or 0
        if isinstance(input_tokens, int | float):
            total = int(input_tokens) + int(cache_write_tokens)
            self._last_measured_prompt_tokens = total
            # Hybrid meter bookkeeping (no-op for the trigger unless
            # token_meter == "hybrid"; recorded always so the hybrid number
            # is observable in every mode -- see _measure_hybrid).
            self._anchor_seq = self._next_seq
            self._anchor_estimate = self._last_sent_estimate
            self._usage_events += 1
            cache_read = usage.get("cache_read_tokens")
            cache_write_reported = usage.get("cache_write_tokens")
            if isinstance(cache_read, int | float) and isinstance(
                cache_write_reported, int | float
            ):
                self._usage_events_with_cache += 1
                self._usage_cache_read_total += int(cache_read)
                self._usage_cache_write_total += int(cache_write_reported)
            logger.debug(
                f"context-simple: token_meter recorded real usage from "
                f"llm:response -- input_tokens={int(input_tokens):,} + "
                f"cache_write_tokens={int(cache_write_tokens):,} = {total:,} total"
            )
        else:
            logger.debug(
                "context-simple: llm:response event carried no "
                "usage.input_tokens; token_meter unchanged (still using "
                "estimator wherever token_meter='actual' has no measurement yet)"
            )
        return HookResult(action="continue")

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

        # Summary compaction strategy: if a background summarization call
        # has completed since the last escalation, absorb its span NOW --
        # before sticky decisions are (re-)applied, so the new removals and
        # the new summary message are both visible to this call's
        # _apply_sticky_decisions() below. No-op (did_summary_swap stays
        # False) in the default "progressive" mode, and a no-op whenever
        # compaction_strategy == "summary" but nothing is pending yet. See
        # module docstring "Summary compaction strategy".
        did_summary_swap = False
        if self.compaction_strategy == COMPACTION_STRATEGY_SUMMARY and (
            self._pending_summary is not None
        ):
            non_system_messages, did_summary_swap = await self._swap_in_pending_summary(
                non_system_messages
            )

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

        # Escalation GATE: in token_meter="actual" mode with a real
        # measurement available, the REAL measurement decides whether a new
        # escalation is needed -- consistent with the outer trigger in
        # get_messages_for_request() (_should_compact, driven by
        # _measure_working_tokens). This keeps "actual" mode meaningful: if
        # the estimator alone gated this too, a real measurement crossing
        # threshold could be silently ignored whenever the (up to ~2x off)
        # estimator disagreed -- exactly the gap this meter exists to close.
        #
        # KNOWN, ACCEPTED LIMITATION (documented, not hidden -- see README
        # "Real-usage token meter"): only the GATE (whether to escalate at
        # all) uses the real number. The amount of reduction below --
        # target_tokens, and every per-level termination check -- is still
        # computed from the estimator throughout, because a real, billed
        # token count for a *hypothetical* smaller message set does not
        # exist without another provider round-trip. If the real measurement
        # and the estimator disagree sharply, "actual" mode can still
        # converge on level 1 without having done much real reduction (the
        # estimator's own view of `working_messages` already looked small
        # enough) -- this module fires the escalation honestly, but the
        # sizing of that escalation is only as good as the estimator was
        # before this meter existed.
        needs_escalation = self._exceeds_threshold(current_tokens, budget)
        if not needs_escalation and not did_summary_swap:
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

        if not needs_escalation:
            # Summary swap alone (see above) already brought us back under
            # threshold this call -- no progressive level is needed. Still
            # route through _finalize_compaction_with_stats (rather than the
            # cheap early-return above) so _last_compaction_stats/hooks
            # observe that something DID change this call.
            # max_level_reached=0 records "no progressive level was needed".
            logger.info(
                f"Summary compaction alone reached target: {old_count} raw messages, "
                f"{old_tokens:,} raw tokens -> {len(working_messages)} messages, "
                f"{current_tokens:,} tokens"
            )
            return await self._finalize_compaction_with_stats(
                working_messages,
                system_messages,
                old_count,
                old_tokens,
                0,
                0,
                0,
                0,
                budget,
                target_tokens,
            )

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
                        # Sticky: record before `first_msg` var is superseded.
                        self._record_stubbed(first_msg)
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
                old_len = len(str(msg)) // 4
                messages[i] = self._truncate_tool_result(msg)
                new_len = len(str(messages[i])) // 4
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
        token_lens = [len(str(msg)) // 4 for msg in messages]
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
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 80:
                indices_to_stub.add(i)
                savings = (len(content) - 70) // 4  # Stub is ~70 chars
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
        if budget > 0 and final_tokens > budget:
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
        stats = {
            "before_tokens": old_tokens,
            "after_tokens": final_tokens,
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
        if self.compaction_strategy == COMPACTION_STRATEGY_SUMMARY:
            # Only surfaced in "summary" mode -- keeps the default
            # "progressive" mode's stats dict shape byte-identical to
            # before this feature existed.
            stats["messages_absorbed_by_summary"] = self._summary_absorbed_count
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

    # ------------------------------------------------------------------
    # Summary compaction strategy (`compaction_strategy: "summary"`)
    # ------------------------------------------------------------------
    #
    # Opt-in alternative to the progressive truncate/remove ladder above.
    # Lifts the IDEAS from amplifier-bundle-context-managed's rolling
    # summarizer -- the structured 5-section prompt, the early-async-trigger
    # design -- but rebuilds ALL plumbing on this module's own sticky/_seq
    # primitives instead of that donor module's index-based splice-and-swap:
    #
    #   - absorbed messages are recorded via _record_removed(), the SAME
    #     mechanism progressive Levels 3/5/7/8 already use, so
    #     _apply_sticky_decisions() replays the absorption byte-identically
    #     on every subsequent call. Candidates are keyed by each message's
    #     permanent `_seq`, never by list index/offset, so there is no
    #     "stale boundary" class of bug here at all -- contrast the donor's
    #     `offset_at_creation` drift-guard, which existed only because ITS
    #     design tracked absolute indices in the first place.
    #   - the summary itself is stamped with a fresh `_seq` exactly like
    #     add_message() would (see _make_summary_message), and is APPENDED
    #     to self.messages -- never spliced in -- so self.messages remains
    #     a strict, append-only log and this module's "compaction never
    #     modifies self.messages" invariant is never violated.
    #   - the summary is role="user" (never "system"), wrapped in a
    #     <system-reminder source="context-summary"> envelope, and is
    #     NOT marked ephemeral -- it is meant to persist as stable history,
    #     unlike the (ephemeral, tail-only) compaction notice above. A
    #     role="system" summary tier is what measurably busted the donor's
    #     own provider-level system-prompt cache breakpoint (see README
    #     "Summary compaction strategy" for the measured numbers); this fix
    #     is non-negotiable, not cosmetic.
    #   - the absorb boundary is snapped (_snap_absorb_boundary) so an
    #     assistant tool_calls message and every one of its tool results
    #     are absorbed together or not at all -- reusing the same
    #     tool_call_id identity fields _check_tool_pair_removable already
    #     keys on, not the donor's adjacent-index-only heuristic (the exact
    #     gap that let the donor split a live call/result pair in practice).

    def _get_summarization_prompt(self) -> str:
        """Return the summarization prompt.

        Reads from `summarization_prompt_path` if configured and the file
        exists, mirroring the donor's own file-override knob. Falls back to
        DEFAULT_SUMMARIZATION_PROMPT on any OSError, logging a warning --
        never raises.
        """
        if self.summarization_prompt_path:
            try:
                return Path(self.summarization_prompt_path).read_text()
            except OSError as e:
                logger.warning(
                    f"context-simple: could not read summarization_prompt_path "
                    f"{self.summarization_prompt_path!r}: {e}; falling back to "
                    "the built-in DEFAULT_SUMMARIZATION_PROMPT"
                )
        return DEFAULT_SUMMARIZATION_PROMPT

    def _format_messages_for_summarization(
        self, messages: list[dict[str, Any]]
    ) -> str:
        """Format messages into the plain-text transcript the summarizer
        reads. Each message becomes '[role]: content'; tool results are
        linked back to their call via '[tool_result for {tool_call_id}]:
        ...'; tool_calls are rendered as '[tool_call: name(args)]' with
        arguments truncated to 500 chars. Adapted from the donor's
        `_format_messages_for_summarization`.
        """
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text", "")
                        if text:
                            parts.append(text)
                    elif hasattr(block, "text"):
                        parts.append(block.text)
                content = "\n".join(parts)

            if role == "tool":
                tc_id = msg.get("tool_call_id", "")
                line = f"[tool_result for {tc_id}]: {content}"
            else:
                line = f"[{role}]: {content}"

            extra_lines: list[str] = []
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                if "function" in tc:
                    name = tc["function"].get("name", "unknown_tool")
                    raw_args = tc["function"].get("arguments", "{}")
                else:
                    name = tc.get("name") or tc.get("tool", "unknown_tool")
                    raw_args = tc.get("input") or tc.get("arguments") or {}
                if isinstance(raw_args, str):
                    arg_str = raw_args
                else:
                    arg_str = json.dumps(raw_args, separators=(",", ":"))
                if len(arg_str) > 500:
                    arg_str = arg_str[:500] + "..."
                extra_lines.append(f"  [tool_call: {name}({arg_str})]")

            lines.append("\n".join([line, *extra_lines]) if extra_lines else line)

        return "\n\n".join(lines)

    def _extract_text_from_response(self, response: Any) -> str:
        """Join every text-bearing content block in a ChatResponse. Blocks
        without a `.text` attribute (tool calls, thinking, etc.) are
        silently skipped."""
        parts = []
        for block in getattr(response, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "".join(parts)

    async def _maybe_trigger_summary_compaction(
        self, token_count: int, effective_budget: int
    ) -> None:
        """Start an async background summarization call if usage has
        crossed `summary_trigger` and nothing is already in flight/pending.

        Mirrors the donor's early-trigger design (default 0.60, well ahead
        of compact_threshold) so the LLM call has time to finish before
        tokens must actually be shed -- see _swap_in_pending_summary, which
        performs the actual absorption once this completes. Never raises,
        never blocks this turn: the provider call itself happens inside an
        asyncio.create_task, off the critical path.
        """
        if self._is_summarizing or self._pending_summary is not None:
            return
        if self._cached_provider is None:
            logger.debug(
                "context-simple: skipping summary trigger -- no cached "
                "provider yet (first get_messages_for_request() of the "
                "session hasn't run, or the caller never passes one)"
            )
            return
        if effective_budget <= 0:
            return

        usage_fraction = token_count / effective_budget
        if usage_fraction < self.summary_trigger:
            return

        target_tokens = int(effective_budget * self.target_usage)
        if token_count <= target_tokens:
            return
        excess_tokens = token_count - target_tokens

        seqs = self._select_summary_absorb_seqs(excess_tokens)
        if not seqs:
            logger.debug(
                "context-simple: summary trigger fired but nothing eligible "
                "to absorb yet (too little non-protected history)"
            )
            return

        self._is_summarizing = True
        self._summarization_task = asyncio.create_task(
            self._run_summary_compaction_task(seqs)
        )

    def _select_summary_absorb_seqs(self, excess_tokens: int) -> list[int] | None:
        """Select a prefix of the oldest, still-live, non-protected,
        non-system messages to summarize, sized to shed roughly
        `excess_tokens`, snapped so a tool_calls/tool-result pair is never
        split (_snap_absorb_boundary). Returns the ordered list of `_seq`
        ids to absorb, or None if nothing qualifies.

        Excludes: system messages (never compacted, handled separately),
        messages already absorbed/removed by a prior escalation, and this
        module's own past summary messages (never re-summarized -- each
        escalation produces its own standalone summary; see module
        docstring for why this PR does not implement tier merging).
        """
        live = [
            m
            for m in self.messages
            if m.get("role") != "system"
            and self._extract_seq(m) not in self._removed_seqs
            and (m.get("metadata") or {}).get("type") != _SUMMARY_METADATA_TYPE
        ]
        if not live:
            return None

        last_user_idx = None
        for i, m in enumerate(live):
            if m.get("role") == "user":
                last_user_idx = i

        protected_boundary = int(len(live) * (1 - self.protected_recent))
        if last_user_idx is not None:
            protected_boundary = min(protected_boundary, last_user_idx)
        if protected_boundary <= 0:
            return None

        accumulated = 0
        end_idx = 0
        for i in range(protected_boundary):
            accumulated += len(str(live[i])) // 4
            end_idx = i + 1
            if accumulated >= excess_tokens:
                break

        end_idx = self._snap_absorb_boundary(live, end_idx, protected_boundary)
        if end_idx <= 0:
            return None

        seqs = [self._extract_seq(m) for m in live[:end_idx]]
        return [s for s in seqs if s is not None] or None

    def _snap_absorb_boundary(
        self,
        live: list[dict[str, Any]],
        end_idx: int,
        protected_boundary: int,
    ) -> int:
        """Adjust `end_idx` (an exclusive boundary into `live`) so an
        assistant tool_calls message and every one of its tool results are
        absorbed together, or not absorbed at all -- and so the boundary
        never crosses into the protected tail (index >= protected_boundary).

        Reuses the same identity fields _check_tool_pair_removable keys on
        (`tool_calls[].id` / `tool_call_id`), applied to a single
        contiguous prefix boundary instead of scattered removal candidates.
        This is the fix for the donor's exact production failure: its
        `_snap_to_tool_pair_boundary` only checked whether the immediately
        NEXT message had role "tool" -- an adjacency heuristic that misses
        non-adjacent results and does no protected-boundary accounting at
        all, which is how it shipped dropping a `function_call` while
        keeping its `function_call_output` (InvalidRequestError, see
        README "Summary compaction strategy").
        """
        if end_idx <= 0:
            return 0

        id_map: dict[str, list[int]] = {}
        for idx, msg in enumerate(live):
            tcid = msg.get("tool_call_id")
            if tcid:
                id_map.setdefault(tcid, []).append(idx)

        def result_indices(assistant_msg: dict[str, Any]) -> list[int]:
            idxs: list[int] = []
            for tc in assistant_msg.get("tool_calls") or []:
                tc_id = tc.get("id") or tc.get("tool_call_id")
                if tc_id:
                    idxs.extend(id_map.get(tc_id, []))
            return idxs

        # Bounded fixed-point: each iteration either grows end_idx (capped
        # at protected_boundary) or shrinks it to exclude exactly one
        # unabsorbable call -- never both for the same call twice -- so
        # this always terminates within len(live) iterations.
        for _ in range(len(live) + 1):
            max_needed = end_idx
            overflow_call_idx: int | None = None
            for i in range(end_idx):
                msg = live[i]
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for k in result_indices(msg):
                        if k >= max_needed:
                            max_needed = k + 1
                        if k >= protected_boundary and overflow_call_idx is None:
                            overflow_call_idx = i
            if max_needed <= protected_boundary:
                return max_needed
            # Can't extend past the protected tail without splitting a
            # pair -- drop the first offending call (and, transitively,
            # everything after it in this round) rather than ever crossing
            # the boundary or absorbing a call without its result.
            end_idx = overflow_call_idx if overflow_call_idx is not None else 0
            if end_idx <= 0:
                return 0
        return 0  # defensive; unreachable given the termination argument above

    async def _run_summary_compaction_task(self, seqs: list[int]) -> None:
        """Background task: call the summarizer over the message span
        identified by `seqs` and stash the result in `_pending_summary` for
        the next get_messages_for_request()/_compact_ephemeral() call to
        swap in (_swap_in_pending_summary).

        Never raises: any failure (bad/absent provider, timeout, malformed
        response, empty summary) increments `_summarization_failures`,
        logs a warning, and leaves `_pending_summary` unset -- the next
        compaction pass that needs to shed tokens falls back to the
        progressive ladder for that pass, exactly as if
        compaction_strategy were "progressive". Always clears
        `_is_summarizing`/`_summarization_task` in a finally block so a
        failed round never permanently wedges future triggers.
        """
        try:
            seq_set = set(seqs)
            messages_to_summarize = [
                m for m in self.messages if self._extract_seq(m) in seq_set
            ]
            if not messages_to_summarize:
                return

            provider = self._cached_provider
            if provider is None:
                raise RuntimeError("no cached provider available for summary compaction")

            prompt = self._get_summarization_prompt()
            formatted = self._format_messages_for_summarization(messages_to_summarize)

            from amplifier_core import ChatRequest, Message

            request = ChatRequest(
                messages=[
                    Message(role="system", content=prompt),
                    Message(role="user", content=formatted),
                ],
                model=self.summarization_model,
            )

            if self._hooks is not None:
                try:
                    await self._hooks.emit(
                        "context:pre_summarize",
                        {"message_count": len(messages_to_summarize)},
                    )
                except Exception as e:
                    logger.warning(f"Could not emit context:pre_summarize: {e}")

            response = await asyncio.wait_for(
                provider.complete(request), timeout=self.summarization_timeout_s
            )
            summary_text = self._extract_text_from_response(response)
            if not summary_text.strip():
                raise ValueError("summarizer returned empty text")

            self._pending_summary = {"seqs": frozenset(seqs), "text": summary_text}
            self._summarization_failures = 0

            if self._hooks is not None:
                try:
                    await self._hooks.emit(
                        "context:post_summarize",
                        {"summary_length": len(summary_text)},
                    )
                except Exception as e:
                    logger.warning(f"Could not emit context:post_summarize: {e}")
        except Exception as e:
            self._summarization_failures += 1
            logger.warning(
                f"context-simple: summary compaction failed ({e!r}); falling "
                "back to progressive compaction for the next pass that needs "
                "to shed tokens"
            )
        finally:
            self._is_summarizing = False
            self._summarization_task = None

    def _make_summary_message(self, summary_text: str) -> dict[str, Any]:
        """Build the persisted summary message: role="user" (never
        "system" -- see module docstring), wrapped in a
        <system-reminder source="context-summary"> envelope so
        foundation's is_real_user_message() classifies it correctly, and
        stamped with a fresh `_seq` exactly like add_message() would.

        NOT marked metadata.ephemeral=True: unlike the tail compaction
        notice, this message is meant to persist as stable history.
        """
        content = (
            f'<system-reminder source="{_SUMMARY_ENVELOPE_SOURCE}">\n'
            f"{summary_text}\n"
            "</system-reminder>"
        )
        message: dict[str, Any] = {
            "role": "user",
            "content": content,
            "metadata": {
                "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "type": _SUMMARY_METADATA_TYPE,
                "_seq": self._next_seq,
            },
        }
        self._next_seq += 1
        return message

    async def _swap_in_pending_summary(
        self, non_system_messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], bool]:
        """Absorb a completed pending summary, if it is still valid.

        Validity is checked purely by `_seq` membership -- every captured
        seq must still be present in `non_system_messages` and not already
        removed by an intervening escalation. There is no index/offset
        arithmetic here, so there is no "stale boundary" bug to guard
        against beyond this membership check; if the whole span was
        already resolved (e.g. an emergency progressive escalation ran in
        between), the summary is discarded gracefully -- NOT counted as a
        failure -- and the caller falls back to progressive compaction for
        this pass, same as if none had been pending.

        Never hand-splices self.messages: absorbed messages are recorded
        via _record_removed() (the existing sticky-decision path), and the
        summary message is APPENDED to self.messages, never inserted at an
        arbitrary position.

        Returns (possibly-updated non_system_messages, did_swap).
        """
        pending = self._pending_summary
        self._pending_summary = None

        present_seqs = {self._extract_seq(m) for m in non_system_messages}
        absorb_seqs = {
            s
            for s in pending["seqs"]
            if s in present_seqs and s not in self._removed_seqs
        }
        if not absorb_seqs:
            logger.info(
                "context-simple: discarding stale pending summary -- its "
                "absorbed span was already resolved by an earlier escalation"
            )
            return non_system_messages, False

        for msg in non_system_messages:
            if self._extract_seq(msg) in absorb_seqs:
                self._record_removed(msg)

        summary_message = self._make_summary_message(pending["text"])
        self.messages.append(summary_message)
        self._summary_absorbed_count += len(absorb_seqs)

        logger.info(
            f"context-simple: summary compaction absorbed {len(absorb_seqs)} "
            "messages into 1 stable summary message"
        )
        return non_system_messages + [summary_message], True

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

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Rough token estimation (chars / 4)."""
        return sum(len(str(msg)) // 4 for msg in messages)
