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
- **Optional real-usage token meter** (`token_meter: "actual"` or `"hybrid"`, default off) drives the compaction trigger from real provider usage instead of the built-in estimator -- see [Real-usage token meter](#real-usage-token-meter-token_meter) below

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

### Hybrid mode (`token_meter: "hybrid"`)

`"actual"` replaces the estimator wholesale with the provider's last
reported total. That number is **retrospective by one call**: it describes
the request that was just answered, so everything appended since -- the new
user turn, the tool results from this turn's tool loop -- is invisible to it.

`"hybrid"` is the shape `openai/codex` uses, which has no "estimate mode" at
all:

```
total = provider_reported_total_from_the_last_llm_response   # kind='usage'
      + estimate(items appended since that response)         # the un-billed tail only
```

The heuristic is still used, but only for the small, recent slice the
provider has not priced yet -- never for the whole window, and never for the
system prompt and tool schemas, which are both the largest block in the
window and the block the provider has *definitely* already billed.

On top of that shape it carries two things lifted from
`deepseek-ai/deepseek-harness`:

**1. Provenance on every count (`kind`).** Every number this module produces
reports where it came from -- `'usage'`, `'estimated'`, or `'none'` -- in
`_last_token_meter_stats["kind"]` and on the `context:token_meter` event.
A consumer that acts *irreversibly* on a token count must branch on this
rather than silently accepting an estimate.

**2. A conservatism guard.** If the provider's total is *below* what the
heuristic priced for the very content it billed, the anchor is not a
trustworthy floor for this window: it is **rejected**, the (larger) full
heuristic is reported instead, and the count is honestly marked
`kind='estimated'`. The comparand is the estimate of the view that was
actually **sent** on that request, not the full uncompacted history -- those
are different numbers whenever compaction is active.

And a third, from the same source: **refuse to guess.** The optional cache
aggregates in the stats (`cache_aggregates`) are reported **only** when every
usage event observed this session carried the underlying fields. One event
missing them makes the aggregate `None` (undefined) rather than a partial sum
that silently under-reports.

#### G-METER-PROVENANCE

In `"hybrid"` mode, **no irreversible action is taken on a count whose kind
is not `'usage'`.** Firing compaction is irreversible in the ways that
matter: it destroys the provider's prompt cache for at least one request,
and it records sticky truncate/remove decisions that persist for the rest of
the session. So when the count is `kind='estimated'` -- before the first
response of the session, or when the conservatism guard rejected the anchor
-- the trigger **declines to fire** and waits for provider-anchored data.
Refusals are counted in `_last_token_meter_stats["provenance_refusals"]`.

There is exactly **one** escape, and it is recorded rather than hidden: if
**no** anchor has ever arrived this session *and* the count has reached 100%
of budget, refusing would guarantee a provider context-overflow failure on
the next request, which is strictly worse than acting on an estimate. That
path fires, logs a warning, and is counted separately in
`provenance_overrides` -- so a gate can report it honestly instead of it
looking like a clean anchored fire. It cannot mask a rejected anchor: it
requires that no measurement exists at all.

#### All three meters, every request

`estimated_tokens`, `measured_tokens` and `hybrid_tokens` are computed on
**every** request in **every** mode -- including the default -- and reported
via `_last_token_meter_stats` and the `context:token_meter` event, alongside
`anchor_tokens`, `anchor_rejected`, `tail_estimated_tokens` and
`tail_messages`. Only *which one drives the trigger* changes with the mode.
That is deliberate: it makes the estimate-vs-hybrid-vs-actual divergence
measurable on a real workload without changing behaviour to measure it.

#### Same sizing limitation as `"actual"`

Only the **gate** uses the anchored number. `target_tokens` and every
per-level termination check inside `_compact_ephemeral` are still the
estimator, for the same reason as in `"actual"` mode: a provider-billed
count for a hypothetical smaller message set does not exist without another
round trip.

### Future default flip (pending validation)

`token_meter` defaults to `"estimate"` specifically so it ships
with **zero behavior change**. Flipping the default to `"actual"` or
`"hybrid"` -- and
potentially raising `compact_threshold` closer to the real ceiling now that
it can be measured accurately -- is a follow-up, not part of this change. It
should happen only after running the module's own eval harness against
`"actual"` mode's stats (`_last_token_meter_stats`) to confirm the expected
reduction in compaction cadence (request count / wall time) holds up without
a corresponding quality regression.

## Summary compaction strategy (`compaction_strategy`)

> ### :warning: Opt-in, experimental. Do not enable by default.
>
> Ships behind `compaction_strategy: "progressive"` (default,
> byte-identical to this module's behavior before this feature existed).
>
> A T0/T1 evaluation has now run (n=3 vs. n=5 baselines, S5-CRAC
> scenario). Its two headline results, stated plainly:
>
> - **No retention benefit is demonstrated.** T1 scored 94.0 mean vs.
>   T0's 94.4 — statistically and practically indistinguishable, and on a
>   metric that is **saturated**: both arms score a perfect 40/40 planted
>   constraints and 20/20 post-compaction in *every* run, and have across
>   20+ historical runs. The scenario cannot discriminate retention. This
>   is *not* evidence that summaries retain worse — it is the absence of
>   evidence either way. A discriminating scenario does not exist yet.
> - **Measured +83% run cost** ($4.73 vs. $2.58) and **+84% compaction
>   boundaries** (39.7 vs. 21.6) on a compaction-heavy workload, via a
>   boundary-refire loop (see "Known issue" below). This **falsifies** the
>   pre-registered prediction that cache economics would land in T0's
>   band, in the worse direction.
>
> The mechanism itself is validated and correct (all four pre-registered
> gates pass, including the two the donor design failed catastrophically).
> This flag exists so the strategy can be studied further, not because it
> is known to be better. **Do not enable it by default anywhere.**

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

### What this was built to buy, and what the evaluation actually measured

**The motivation** was retention: the progressive ladder is lossy, so an
LLM summary that keeps a lossy-but-real account of an absorbed span
*should* retain more than dropping it outright. That was and remains the
reason to build this. It is **not** a cache-cost play — like the
progressive ladder, this strategy still shrinks what the model sees each
turn, which under a grow-only prompt cache is still a cold rebuild of the
shared prefix at the moment of absorption.

**What the T0/T1 evaluation measured** (n=3 T1 runs vs. n=5 reused T0
baselines, S5-CRAC scenario, capture root
`.amplifier/evaluation/treatment-validation/20260902-t0t1/`):

| Gate | Requirement | Result |
|---|---|---|
| **G1 retention** | T1 S5 ≥ T0 band (92–95) | **PASS — but vacuous.** T1 [95, 95, 92] vs. T0 mean 94.4. See note below. |
| **G2 tool-pairs** | zero `InvalidRequestError` | **PASS** — 0 across all runs (the donor design: 29/30 turns failed) |
| **G3 system prompt** | agent `instructions` byte-stable | **PASS** — 1 hash per run, all 3 runs (the `role: "user"` fix holds under a real provider round trip; the donor's agent prompt moved 44,516 → 1,113 → 45,310 within one run) |
| **G4 append-only** | no history re-minting | **PASS** — `ID_ONLY` divergences 0/0/0 |

> **G1 passed but proves nothing about retention.** `b_constraints` is
> 40/40 and `c_post_compaction` is 20/20 in **every run of both arms**,
> and has been across 20+ historical runs. The only moving part is the
> task score. S5-CRAC is at its ceiling: both strategies already hold
> every planted constraint perfectly, so a "≥ baseline" check against a
> saturated metric is a floor check, not a win. **No retention advantage
> is demonstrated by this evaluation.** Establishing one requires a
> scenario with headroom — one where the progressive baseline measurably
> *loses* constraints. That scenario does not exist yet (TBD).

Summaries do fire correctly and do carry the material: 63–95 requests per
run carried a summary, all `role: "user"` (zero `role: "system"`), up to
67 messages absorbed in a single round, and the emitted summaries restate
all five planted constraints verbatim. The mechanism works. What is
unproven is that the mechanism *helps*.

### Known issue: boundary refire roughly doubles compaction cost

Measured on the same evaluation, and **worse than the pre-registered
prediction** (which expected cache economics in T0's band):

| Metric | T0 (progressive) | T1 (summary) | Delta |
|---|---:|---:|---:|
| Cache waste | 29.0% | **53.9%** | **+24.9pp** (no overlap between arms) |
| Cache-read share | 0.714 | **0.537** | −0.177 |
| Run cost | $2.58 | **$4.73** | **+83%** |
| Compaction boundaries | 21.6 | **39.7** | **+84%** |

**Mechanism.** The summarizer itself is only 8–11% of run cost — it is
not the driver. The dominant cost is the near-doubling of compaction
boundaries, and every boundary is a guaranteed cold rebuild against a
grow-only cache. Absorbing a span *shrinks* the request, which pulls
usage back below `summary_trigger` (0.60) sooner, which fires another
absorb/compact cycle sooner: a refire loop. Under an aggressive
compaction config (the evaluation forced `max_tokens: 45000`) the early
trigger — deliberately set well below `compact_threshold` to give the
async call time to finish — drives the extra cycles.

**Not fixed in this change, tracked honestly.** The obvious levers, none
of which are implemented here: a post-absorb **cooldown** before the
trigger may refire; an **absolute floor** on absorbed-span size so small
absorptions cannot cycle; **hysteresis** on `summary_trigger` (arm at
0.60, disarm only after usage falls below some lower band). Tuning
`summary_trigger` upward is the cheapest first experiment. Any of these
should be validated against the same cost metrics before this flag is
enabled anywhere by default.

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
