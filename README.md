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

Compaction triggers when token usage reaches the configured threshold (default: 92% of the **effective budget** -- which is derived from the provider, *not* from `max_tokens`; see [Where the compaction trigger comes from](#where-the-compaction-trigger-comes-from)):

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

## Where the compaction trigger comes from

The trigger is one multiplication:

```
trigger = compact_threshold * effective_budget
```

`effective_budget` comes from `_calculate_budget()`, in this priority order:

1. an explicit `token_budget=` argument (deprecated, rarely used);
2. `provider.get_model_info()` -> `context_window - 0.5 * max_output_tokens - 4096`;
3. `provider.get_info().defaults` -> the same formula;
4. **only if none of the above yields a window**: the configured `max_tokens`.

### `max_tokens` is a fallback, not a cap

Orchestrators call `get_messages_for_request(provider=provider)`, so in
practice branch 2 or 3 always answers and **branch 4 is never reached**.
The `max_tokens` value in your bundle config (the shipped foundation bundle
sets `max_tokens: 300000`) therefore has **no effect on when compaction
fires**. Lowering it to compact sooner, or raising it to compact later, is a
no-op on the wire.

This is a real trap, not a theoretical one. The cadence probe that produced
the numbers below could not move the trigger with config at all: its harness
had to patch this module's source in-container to add
`budget = min(budget, self.max_tokens)` before either of its arms would
compact in a bounded run.

`tests/test_compaction_trigger_provenance.py` pins this behavior in both
directions -- same history and same config compacts with no provider and does
*not* compact with one -- so the trap fails a test rather than a measurement
run.

**To move the trigger, move `compact_threshold`.** It is the only shipped knob
that expresses "compact later" independently of the provider, and the old
value stays reachable:

```yaml
context:
  module: context-simple
  config:
    compact_threshold: 0.80   # compact earlier than the 0.92 default
```

### What the cadence measurement does and does not say

Measured on the S5-CRAC scenario (`gpt-5.6-terra`, n=2 vs n=5 reused
baselines; capture root
`.amplifier/evaluation/treatment-validation/20260901-cadence/`,
`PROBE4-VERDICT.md`), raising the compaction trigger budget from 45,000 to
70,000 tokens produced:

| arm | boundaries | requests | wall (s) | cost ($) | S5 score |
|---|---:|---:|---:|---:|---:|
| `cad-today` (trigger 45k, n=5) | 21.6 | 104 | 562 | 2.58 | 94.4 |
| `cad-fewer` (trigger 70k, n=2) | 9.5 | **74** | **485** | 2.65 | 95.0 |

**-29% requests, -14% wall, at equal cost and equal quality** -- and the only
arm in that matrix where input-item caching measurably occurred (20/72 and
11/77 requests). Buy the latency and request-count win; **do not promise a
cost win** ($2.65 vs $2.58 is nil, in the wrong direction).

Two limits on that result, both from its own source:

- Both values are **scenario forcing knobs**. 45,000 exists to make a bounded
  10-turn run compact at all. Neither is a production default, and neither is
  a value this module has ever shipped.
- **Production already compacts later than `cad-fewer` did.** With a
  200,000-token window the shipped trigger is `0.92 * 163,904 = 150,791`
  tokens; `cad-fewer`'s was `0.92 * 70,000 = 64,400`. Adopting 70,000 as a
  budget cap would move the trigger **earlier** for every provider whose
  window exceeds it -- more boundaries, inverting the measured win. The
  parametrized test at the bottom of
  `tests/test_compaction_trigger_provenance.py` asserts exactly this.
  (Those trigger figures are `compact_threshold * budget`;
  `get_messages_for_request` also subtracts the 800-token compaction-notice
  reserve first, moving each down by 736 tokens. No ratio changes.)

The general finding still holds and is the one to carry forward: **fewer
compaction boundaries buys latency and request count, not money, and costs no
measurable quality** (post-compaction retention was 20/20 in every run of
every arm, `b_constraints` 40/40 throughout -- on a scenario whose 5 crisp
constraints may be a ceiling effect).

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
