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

## Tool-result budget, shape, and spill

**Every flag in this section defaults to a no-op.** With no configuration the
truncation path is byte-identical to what this module has always emitted: 250
characters, head-only, the same prefix string. See "Byte-identity evidence"
below for how that is proven rather than asserted.

### The defect this fixes

When the progressive ladder truncates a tool result, it has always kept
`content[:250]` — **250 characters, head only, ~62 estimator tokens**. The one
shipped reference implementation available to compare against (codex) keeps a
**~10,000-token** budget, **head + tail**, with an explicit truncation marker,
and lets the model raise it per call up to 262,144. We were roughly **160×
below** it, on the *only* axis where a direct comparison exists.

The head-only choice is the sharper half of the problem. For `pytest`, `grep`,
`git log` and build output **the answer is in the tail** — the failing
assertion, the matching line, the linker error. Keeping the head keeps the part
that matters least.

### What the workload actually looks like (measured)

From two existing capture roots — 17 sessions, 2,479 tool results, counted from
each session's own `transcript.jsonl` in **characters**, which is the unit this
module's estimator works in (`len(str(msg)) // 4`):

| | `20260902-t0t1` | `20260901-cadence` |
|---|---|---|
| sessions / tool results | 5 / 750 | 12 / 1,729 |
| tool-result share of all transcript chars | **46.4%** | **47.3%** |
| size p50 / p90 / p99 / max (chars) | 412 / 7,904 / 31,751 / 52,319 | 467 / 7,904 / 31,744 / 43,720 |
| results over today's 250-char budget | 488 (65%) | 1,096 (63%) |
| chars discarded by today's budget | 1,524,013 (**91%** of all tool-result content) | 3,082,300 (**90%**) |
| results over 50,000 bytes | **1** | **0** |

Two things follow, and the second one contradicts the reference
implementation this spill design otherwise copies:

1. Tool results are **~47% of the material compaction has to work with**, not a
   rounding error. (Caveat, stated: a transcript is the full history, not the
   compacted wire view, so this is the share of what compaction *sees* — an
   upper bound on the wire share in a run that compacts. Confidence: measured,
   char-denominated; token figures are the module's own estimator, not
   provider-billed tokens.)
2. deepseek's shipped **50,000-byte spill threshold would fire once in 2,479
   results** on this workload. The mass sits in the 400–32,000 char band. Do not
   copy that constant; copy the mechanism.

### The flags

| flag | default | effect |
|---|---|---|
| `tool_result_budget_tokens` | `None` | Token-denominated budget. `None` keeps the legacy `truncate_chars` char budget, byte-identical. |
| `tool_result_shape` | `"head"` | `"head_tail"` splits the budget in half and keeps both ends with an explicit `...[N chars omitted]...` marker. |
| `tool_result_budget_by_tool` | `{}` | Per-tool token budgets keyed by tool name. Takes precedence over the global budget. |
| `tool_result_exempt_tools` | `[]` | Tool results that are **never** truncated. |
| `tool_result_spill_dir` | `None` | Writes the full original result to a content-addressed file and points the model at it. `None` writes nothing, ever. |

Precedence for a single result: **per-tool budget → global token budget →
legacy `truncate_chars`**. Setting only `tool_result_budget_by_tool` therefore
leaves every *other* tool on the legacy path, byte-identical.

A starter configuration — conservative relative to codex's 10,000, and
deliberately **not** the shipped default:

```toml
[[contexts]]
module = "context-simple"
config = {
    tool_result_budget_tokens = 2000,          # ~8,000 chars, vs today's 250
    tool_result_shape = "head_tail",
    tool_result_budget_by_tool = { grep = 1000, read_file = 4000, bash = 2000 },
    tool_result_exempt_tools = ["load_skill"],  # never trim skill output
    tool_result_spill_dir = "/tmp/amplifier-spill",
}
```

These per-tool numbers are **starting points transcribed from a specification
that publishes no measurements**, rescaled to the size distribution above. They
are not measured. Say so wherever they get copied.

### Why tokens, not chars

Chars-per-token constants are tokenizer-version specific and drift (published
drift up to 1.35×, observed up to 1.47× on technical content, with the rate card
unchanged). A char budget therefore silently changes meaning across a model
version; a token budget does not. The conversion constant used here is `4` —
deliberately the same constant this module's own estimator uses, so a budget
expressed in tokens and the accounting the ladder runs on cannot drift apart.

**Chars before lines, always.** The gate is a pure character count and there is
no line-based cap anywhere in this path, so the "a file could have 2 lines that
are each 10MB" failure mode is structurally impossible here rather than merely
unlikely.

### Spill: the truncated middle stays reachable

With `tool_result_spill_dir` set, the **full original** result is written to
`<dir>/tool-result-<seq>-<sha256[:16]>.txt` and the replacement text becomes:

```
[truncated: ~12,431 tokens - read /tmp/amplifier-spill/tool-result-000042-a1b2….txt for the full result] <head>
...[48,920 chars omitted]...
<tail>
```

There is **no new retrieval tool**, deliberately: the model is pointed at the
ordinary file tools it already has. (The opposite failure is on record — a
README advertising `grep` over an archive where only `ls`/`view` existed, leaving
the model to guess which summary hid the fact it needed.)

Three properties worth knowing:

- **The pointer is a pure function of content + config, and is emitted whether
  or not the write succeeded.** This is not sloppiness, it is the constraint:
  `_apply_sticky_decisions` re-derives the replacement text for every
  sticky-truncated message on *every request*. A pointer that tracked write
  success would change the bytes of an already-sent message after a transient
  disk error — and under a grow-only prompt cache, every prefix mutation is a
  **full cold rebuild**. A dangling pointer is visible and recoverable; a
  silently mutated prefix is neither. Write failures log a warning.
- **Content-addressed, so writes are idempotent** across repeated requests and
  across a resumed or forked session.
- **Nothing is ever deleted** — not on `clear()`, not on compaction, not at
  session end. A resumed or forked session may still hold a pointer to an older
  file. **No cleanup sweep ships in this module**: the caller owns the
  directory's lifecycle, and a session-scoped directory is the recommended
  shape. (deepseek's reference implementation ships a 30-day startup sweep with
  symlink and ownership guards; porting it is deliberately left to whoever wires
  spill into a bundle default.)

### Shape change is nearly free; budget change is not

Mechanism demonstration, no model and no spend — 4 synthetic tool workloads
(`pytest`, `grep`, `git log`, build output) whose answer sits in the last line,
enumerated *before* the run:

| arm | truncated results | tail present | rate |
|---|---|---|---|
| control (shipped defaults: 250 chars, head) | 27 | 0 | **0%** |
| **budget-neutral** (62 tokens = 248 chars, `head_tail`) | 28 | 28 | **100%** |
| 4× budget (250 tokens = 1,000 chars, `head_tail`) | 1 | 1 | 100% |

The middle row is the clean A/B: **same bytes kept, different shape**, same
sticky level per tool (4/2/3/3 in both arms). Tail retention goes 0% → 100% at
no budget cost.

The third row is the honest caveat, and it is load-bearing for anyone tuning
this: **raising the per-result budget without also raising `target_usage` or
`max_tokens` trades truncation for removal.** A larger budget sheds fewer tokens
per truncation, so the ladder escalates past the truncation rungs into message
*removal* — which is strictly more lossy. Three of four workloads went from
11–15 truncated results to **zero**, having been removed instead. Raise the
budget and the target together, or measure what you actually got.

### Byte-identity evidence

Claim: with default configuration, output is byte-identical to before this
change. Method: an **external** harness (it imports whatever
`amplifier_module_context_simple` is on `sys.path`, so neither side defines its
own baseline) drives 8 scenarios — light/heavy/aggressive pressure, four
`truncate_chars` values from 10 to 5,000, notice on and off — takes a view every
third turn plus two consecutive views on turn 5, and dumps canonical JSON of
every returned message. Only `metadata.timestamp` is normalized; every truncated
result is compared character for character.

```
pre-change  (c6dfbba, 2,648 lines, no such flags)  sha256 76ee9d3f…a467f
post-change (this branch)                          sha256 76ee9d3f…a467f
3,398,618 bytes, identical                         PASS
```

Non-vacuity: the dump contains 76 `[truncated:` occurrences, so the harness
really exercises the path. Negative control: the same harness with the treatment
forced on produces a **different** hash, so it can see changes when there are
any.

The unit suite pins the same claim from the other direction —
`tests/test_tool_result_budget.py` asserts the exact legacy replacement string
literally, against an oracle transcribed from the pre-change source rather than
computed by the new code.

### Not built here

- **Model-settable per-call budgets.** codex lets the model raise its own
  truncation limit per call up to 262,144 tokens. That needs a tool-schema
  surface this module does not own.
- **Count-based spill triggers inside the search tools** (`glob` at >100
  results, `grep` at >250 matches). Size is not the only signal, but those
  triggers belong in the tools, not in the context manager.
- **`read`-result exemption.** deepseek exempts read results specifically to
  prevent a `read → spill → read again` loop. Here spill happens *at compaction
  time* on content already in the conversation, not at tool-execute time before
  it enters, so that loop cannot form — but if this ever moves to a
  post-execute hook, the exemption becomes necessary. `tool_result_exempt_tools`
  is the seam for it.
- **A cleanup sweep.** See above.

## Last-user replay (`replay_last_user_on_compaction`)

**Opt-in. Default `false`. Default is byte-identical to the behavior
before this feature existed. NOT MEASURED — see "Status" below.**

```toml
config = { replay_last_user_on_compaction = true }
```

### What it does

The highest-value tier of retained context is the **user's own
verbatims**. A compaction boundary can leave the most recent user
instruction sitting far from the attention-strongest tail position,
behind whatever tool results the ladder chose to keep.

When this flag is on **and a compaction boundary actually occurs**, the
module appends a copy of the most recent real user message as the last
item before the dynamic tail (the compaction notice), wrapped in a
`<system-reminder source="context-replay">` envelope that states
explicitly that it is a repeat and not a new request:

```
[... compacted conversation ...]
[replay ]  user, ephemeral: <system-reminder source="context-replay"> … </system-reminder>
[notice ]  user, ephemeral: <system-reminder source="context-compaction"> … </system-reminder>
```

### Why it is shaped this way

- **Append-only.** Nothing before the append point moves. No `_seq` is
  consumed, no sticky decision is touched, `self.messages` is never
  modified, and the message is ephemeral (view-only). Append is the
  measured cache-**HIT** shape; a shrink or a reorder is a cold rebuild.
- **Both tail items are `ephemeral: true`,** so the Anthropic provider's
  trailing-ephemeral walk-back skips them and the cache breakpoint lands
  in the same place it did before.
- **Tool-pair integrity is untouched.** The replay applies the same
  unanswered-`tool_calls` tail guard the compaction notice uses, and
  skips rather than interleaving between a `tool_use` and its
  `tool_result`.
- **Once per boundary, not once per request.** The boundary identity is
  `(sticky progressive level, summary-absorbed count)`; a request that
  merely re-applies an existing sticky decision does not re-emit.

### When it deliberately does nothing

| Condition | Why |
|---|---|
| No compaction this request | No boundary, nothing was buried |
| This boundary's replay already went out | Once per boundary |
| View ends on unanswered `tool_calls` | Tool-pair atomicity; retries next request |
| The last real user message is already the tail | A copy would be a pure duplicate |
| The message is a Level-8 stub | A stub is not the user's words |
| No real user message, or no text in it | Nothing to repeat |
| The copy would exceed the budget | Compaction just shed tokens to reach it |

### `_is_real_user_message` is vendored, not imported

This module declares **no runtime dependencies**, so the predicate is
implemented locally rather than imported from `amplifier-foundation` — a
soft import would make behavior depend on whether an undeclared package
happens to be installed.

It is also deliberately **stronger** than foundation's. Measured against
`amplifier-foundation` 1.0.0 (`session/messages.py`): foundation rejects
only content beginning with the **bare** `<system-reminder>` tag, so an
**attributed** envelope — including this module's own
`<system-reminder source="context-summary">` summary message — passes
foundation's check as a real user turn. Replaying a synthetic envelope as
if it were the user's words is exactly what this feature must never do,
so the local predicate rejects any `<system-reminder` opener regardless
of attributes. The asymmetry is pinned by a test.

### Status

**Mechanism only. No quality evidence.** The retention scenario that
could discriminate does not exist yet, and the scenario used for probes
1–6 is saturated (40/40 constraints, 20/20 post-compaction in every arm
of every run), so it cannot show a retention difference in either
direction. Do not enable by default, and do not claim a quality benefit,
until a discriminating eval has run.

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
