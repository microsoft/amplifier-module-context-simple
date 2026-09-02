# Amplifier Simple Context Manager Module

Basic message list context manager for conversation state.

## Prerequisites

- **Python 3.11+**
- **[UV](https://github.com/astral-sh/uv)** - Fast Python package manager

### Installing UV

```bash
# macOS/Linux/WSL
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Purpose

Provides straightforward in-memory conversation context management. This is the reference implementation and default context manager.

## Contract

**Module Type:** Context
**Mount Point:** `contexts`
**Entry Point:** `amplifier_module_context_simple:mount`

## Behavior

- In-memory message list
- No persistence across sessions
- Automatic compaction when approaching token limit (keeps system messages + last 10 messages)
- **Preserves tool pairs as atomic units** during compaction (data integrity guarantee)
- **Optional real-usage token meter** (`token_meter: "actual"`, default off) drives the compaction trigger from real provider usage instead of the built-in estimator -- see [Real-usage token meter](#real-usage-token-meter-token_meter) below

## Configuration

```toml
[[contexts]]
module = "context-simple"
name = "simple"
config = {
    max_messages = 100  # Optional limit
}
```

## Usage

```python
# In amplifier configuration
[session]
context = "context-simple"
```

Perfect for:

- Development and testing
- Short conversations
- Stateless applications

Not suitable for:

- Cross-session persistence
- Custom compaction strategies

## Compaction Strategy

The SimpleContextManager uses **ephemeral compaction** - `get_messages_for_request()` returns a compacted VIEW without modifying the internal message history. The full history is always preserved in memory.

Compaction triggers when token usage reaches the configured threshold (default: 92% of max_tokens):

### Protected Messages (Never Removed)

- **System messages**: All system messages are always preserved
- **First user message**: The original task/request is always protected (prevents losing context about what was originally asked)
- **Last user message**: The most recent user input is always preserved
- **Recent messages**: Last N% of messages (configurable via `protected_recent`)
- **Tool pairs**: Tool_use and tool_result messages are treated as atomic units

### Compaction Phases

1. **Phase 1 - Tool Result Truncation**: Older tool results are truncated to reduce token usage
2. **Phase 2 - Message Removal**: Older non-protected messages are removed if still over budget

### Tool Pair Preservation

Anthropic API requires that every tool_use in message N has a matching tool_result in message N+1. The context manager preserves these pairs as atomic units during compaction to maintain conversation state integrity and prevent API errors.

**Critical implementation detail**: When an assistant message has multiple tool_calls, there are multiple consecutive tool_result messages after it. The compaction logic walks backwards through these tool results to find the originating assistant message, ensuring the entire tool group is preserved as an atomic unit. This prevents orphaned tool results that would cause API validation errors.

## Real-usage token meter (`token_meter`)

### The problem

The compaction trigger described above runs entirely off `_estimate_tokens()`
-- `len(str(msg)) // 4` over the Python `repr()` of each message. This
estimator is **never reconciled against what the provider actually billed**
anywhere in this module. In production sessions it has been measured
roughly **2x off** from real provider usage. Because the trigger and the
whole progressive-compaction sizing logic are built on this number, running
compaction any closer to the real ceiling than the current conservative
default (92%) is unsafe on an estimator that inaccurate -- you would risk
provider-side context-length rejections with no warning.

A companion module, [amplifier-module-context-handoff](https://github.com/microsoft/amplifier-module-context-handoff),
solved this for its own (non-compacting) reserve trigger by registering a
listener on the canonical `llm:response` event and reading the provider's
own reported usage instead of guessing. This module ports that same
`_on_llm_response` meter, adapted to context-simple's compaction trigger.

### What it does

- When hooks are available, this module **always** registers a listener on
  `llm:response` and records the provider's own reported usage for the most
  recent request: `input_tokens + cache_write_tokens`. Per the provider
  contract, `input_tokens` is the GROSS total (fresh + cache_read combined)
  billed as input; `cache_write_tokens` is billed disjointly (a first-time
  cache write of a large system/tool prompt can be billed almost entirely as
  `cache_write_tokens` with `input_tokens` near zero), so it must be added
  separately or true context-window occupancy would be undercounted by
  orders of magnitude. `cache_read_tokens` is **not** added again -- it is
  already inside the gross `input_tokens` figure.
- This recording happens **regardless of `token_meter` mode** -- it is a
  cheap, side-effect-free observability signal, exposed via
  `context._last_token_meter_stats` (populated on every
  `get_messages_for_request()` call, not only when compaction fires) so the
  estimator-vs-real drift is visible even in the default mode.
- Set **`token_meter: "actual"`** in config to additionally have the
  compaction trigger -- and `_compact_ephemeral`'s internal escalation gate
  -- use that real measurement once at least one `llm:response` has been
  observed this session. Before the first response (or whenever hooks/events
  are unavailable), "actual" mode falls back to the same estimator
  `"estimate"` mode always uses.
- Default is **`token_meter: "estimate"`**, which is byte-identical to this
  module's behavior before this meter existed -- verified by running the
  full pre-existing test suite unchanged. An unrecognized `token_meter`
  value logs a warning and falls back to `"estimate"` rather than raising.

### Known, accepted limitation

Only the **escalation gate** (whether to compact at all, and whether a
sticky escalation needs to advance) uses the real measurement in `"actual"`
mode. The *amount* of reduction -- `target_tokens` and every per-level
termination check inside `_compact_ephemeral` -- is still computed from the
estimator throughout, because a real, provider-billed token count for a
*hypothetical smaller* message set does not exist without another round
trip to the provider. If the real measurement and the estimator disagree
sharply, `"actual"` mode can still converge at level 1 without having done
much real reduction (the estimator's own view already looked small enough).
This module fires the escalation honestly in that case, but the *sizing* of
that escalation is only as good as the estimator was before this meter
existed. This mirrors context-handoff's own documented limitation that its
measurement is retrospective (one-call lag): the meter describes the
request that was *just* answered, not the one currently being assembled.

### Future default flip (pending validation)

`token_meter` defaults to `"estimate"` in this PR specifically so it ships
with **zero behavior change**. Flipping the default to `"actual"` -- and
potentially raising `compact_threshold` closer to the real ceiling now that
it can be measured accurately -- is a follow-up, not part of this change. It
should happen only after running the module's own eval harness against
`"actual"` mode's stats (`_last_token_meter_stats`) to confirm the expected
reduction in compaction cadence (request count / wall time) holds up without
a corresponding quality regression.

## Summary compaction strategy (`compaction_strategy`)

> **Status: opt-in, gated on a T0/T1 DTU eval.** Ships behind
> `compaction_strategy: "progressive"` (default, byte-identical to this
> module's behavior before this feature existed). Do not flip the default
> until the eval clears.

### The idea, and where it comes from

The progressive ladder above is lossy: once a message is truncated or
removed, that content is gone. `compaction_strategy: "summary"` absorbs the
oldest non-protected span into an LLM-generated rolling summary instead --
retaining *meaning* at the cost of exact wording, rather than losing the
span outright.

The IDEAS here -- a structured 5-section summarization prompt, and an
async trigger that fires **early** (well before the hard compaction
threshold, so the LLM call has time to finish off the critical path) --
are lifted from `amplifier-bundle-context-managed`'s `modules/context-managed/`
rolling summarizer (see that repo's `__init__.py:71-97` for the prompt this
one is adapted from). **All plumbing is rebuilt from scratch** on this
module's own sticky/`_seq` machinery, because a live evaluation of that
donor module *as shipped* found two showstoppers its own 5,890 LOC of tests
never caught:

1. **It drops a tool call while keeping its result.** Its
   `_snap_to_tool_pair_boundary` only checked whether the *immediately
   next* message had role `"tool"` -- an adjacency heuristic with no
   protected-boundary accounting. In a real multi-turn session this
   produced `InvalidRequestError: No tool call found for function call
   output` on 29 of 30 turns.
2. **Its summary tiers are `role: "system"`.** The Anthropic provider
   hoists every system-role message into the single top-level system
   block, so each summary swap rewrote that block and busted the
   *system*-prompt cache breakpoint -- not just the conversation-region
   one. Measured on a live run: 7 distinct `instructions` hashes across 23
   requests (lengths swinging 44,516 -> 1,113 -> 45,310 tokens), vs. **1**
   stable hash for `context-simple`'s own control.

See `.amplifier/evaluation/treatment-validation/20260901-t4-ctxmanaged/PROBE5-VERDICT.md`
for the full write-up. Neither defect is inherited here:

- **Tool-pair atomicity**: the absorb boundary is snapped by
  `_snap_absorb_boundary`, which reuses the *same* `tool_calls[].id` /
  `tool_call_id` identity fields `_check_tool_pair_removable` (above) keys
  on -- not an adjacency guess. An assistant `tool_calls` message and every
  one of its results are absorbed together, or the whole group is excluded
  and left for the next round. Never split.
- **Cache-safe role**: the summary message is `role: "user"`, wrapped in a
  `<system-reminder source="context-summary">` envelope (so foundation's
  `is_real_user_message()` classifies it correctly) -- **never**
  `role: "system"`. Unlike the tail compaction notice above, it is **not**
  marked `metadata.ephemeral` -- it is meant to persist as stable history.

### How it's wired into this module's own primitives

- **Absorption is sticky, not a splice.** Absorbed messages are recorded
  via the *existing* `_record_removed()` path -- the exact mechanism
  progressive Levels 3/5/7/8 already use -- so `_apply_sticky_decisions()`
  replays the absorption byte-identically on every subsequent call.
  Candidates are keyed by each message's permanent `_seq`, never by list
  index, so there is no "stale boundary" class of bug at all (contrast the
  donor's `offset_at_creation` drift-guard, which existed only because its
  own design tracked absolute indices in the first place).
- **`self.messages` is still never modified by compaction.** The summary
  message is stamped with a fresh `_seq` exactly like `add_message()`
  would and *appended* to `self.messages` -- never spliced in at an
  earlier position. This keeps this module's core invariant intact, at the
  cost of the summary landing wherever `self.messages`' tail happens to be
  at swap time (not necessarily immediately after the span it covers) --
  an explicit, disclosed trade-off in exchange for never reordering a
  shared, cacheable prefix.
- **Async + fallback.** `summary_trigger` (default `0.60`, an absolute
  usage fraction of budget) fires an `asyncio.create_task` off the
  critical path, mirroring the donor's early-trigger idea (optionally
  driven by the real-usage token meter above when `token_meter: "actual"`).
  If the hard compaction threshold is reached and no summary has finished
  yet (in flight, failed, timed out, or no provider was ever passed), that
  pass falls back to the progressive ladder -- a turn is never blocked and
  never fails on a summarizer error.
- **No tier merging in this PR.** Each absorption round produces its own
  standalone summary message; a message already carrying
  `metadata.type == "context_summary"` is never re-selected for a later
  round. `context-managed`'s tier-merging (`_merge_oldest_tiers`) is real
  and useful but out of scope here -- see the design mandate for why this
  PR keeps scope tight (no custom resume logic, no transcript persistence,
  no tool-transcript tool).

### Configuration

```toml
[[contexts]]
module = "context-simple"
config = {
    compaction_strategy = "summary",   # default: "progressive"
    summary_trigger = 0.60,            # usage fraction that starts the async summarizer
    summarization_model = "...",       # optional; None uses the provider default
    summarization_prompt_path = "...", # optional file override for the 5-section prompt
    summarization_timeout_s = 30.0,    # provider.complete() timeout before falling back
}
```

### What this is -- and isn't -- expected to fix

**Retention/quality play, not a cache-cost play.** Like the progressive
ladder, this strategy still *shrinks* what the model sees each turn (via
sticky removal) -- under a grow-only prompt cache, that is still a cold
rebuild of the shared prefix at the moment of absorption, exactly like
today's compaction. Summarization does not, by itself, reduce cache waste.
What changes is *what survives*: a retained, if lossy, account of the
absorbed span instead of nothing. If constraint retention doesn't measurably
improve over the progressive baseline in the gating eval, this is a flag to
turn off, not a design to keep by default.

## Dependencies

- `amplifier-core>=1.0.0`

## Contributing

> [!NOTE]
> This project is not currently accepting external contributions, but we're actively working toward opening this up. We value community input and look forward to collaborating in the future. For now, feel free to fork and experiment!

Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
