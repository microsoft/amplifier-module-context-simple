# DONE-NOTE — `model_performance-pmt`

**Subject:** context-simple — gate `summary_call_mode` fork behind a span-size
predicate (fork only when the span is large relative to the prefix).

**Branch:** `lane/pmt-fork-span-predicate` · **Base:** `main@a877b36` (the PR #27
merge commit lane 6da measured) · **Spend: $0.00.** No API calls, no DTU, no
eval runs, no infrastructure created. 6da's measurement already exists and is
cited by file throughout.

---

## 1. What shipped

Three additions, all default no-op:

| knob | default | meaning |
|---|---|---|
| `summary_fork_min_span_ratio` | `None` | minimum `span_tokens / prefix_tokens` at which a fork is worth issuing. `None` or `0` = predicate not evaluated at all |
| `summary_call_mode: "auto"` | — | `"fork"` with the predicate on at the measured break-even default |
| `DEFAULT_FORK_MIN_SPAN_RATIO` | `0.22` | the break-even, derived below |

`summary_call_mode` is now `standalone` (a.k.a. `inline`) | `fork` | `auto`,
matching the item's requested vocabulary. **Two defaults, not one, are held
still:** `standalone` is byte-identical as before, *and* plain `fork` without
an explicit ratio still forks unconditionally, exactly as PR #27 shipped it. A
predicate that quietly switched itself on for existing `fork` users would be a
behaviour change wearing an opt-in's clothes.

Observability: `last_summary_call_stats` gains `fork_declines`
(session-cumulative) and `span_measure` (`span_tokens`, `prefix_tokens`,
`span_ratio`, `min_span_ratio`), both alongside the pre-existing
`fork_fallbacks`.

---

## 2. DELIVERABLES

| deliverable | status |
|---|---|
| DRAFT PR on origin, branch `lane/pmt-fork-span-predicate`, default byte-identical, tests green | **DONE** — see §6 |
| threshold justified from 6da's measured span-cost table, cited by file | **DONE** — §3 |
| statement of whether the 45.5% fork-rate cap applies | **DONE** — §5. It applies, and strictly |
| DONE-NOTE.md under this lane's own dir, reproduced in the PR body, $0 spend | **DONE** — this file |

---

## 3. The threshold: derived, not chosen

**Source: `.amplifier/evaluation/probes/6da-summary-fork/FINDINGS.md` §6
("WHY G-FORK-COST FAILED — the two effects cancel, exactly"), in the
`openai-evals-team-ci` repo.**

6da's two measured per-token rates:

```
forked call:     $0.00077 per 1,000 own-prompt tokens
standalone call: $0.00350 per 1,000 own-prompt tokens
```

The cost model those rates imply is asymmetric, and that asymmetry is the whole
finding:

- a **standalone** call's own prompt *is the span* (its ~955-char system prompt
  plus a fresh rendering of the span). Its cost scales with **S**, the span.
- a **forked** call's own prompt is *the whole prefix* — which already contains
  the span; the appended instruction is a rounding error. Its cost scales with
  **P**, the prefix.

So the fork is cheaper exactly when

```
P × 0.00077  <  S × 0.00350        ⟺        S / P  >  0.22
```

**0.22 is that ratio and nothing else.** It is a *break-even*, not a margin:
the point where the two calls cost the same. Raising it buys margin, lowering it
forks speculatively — both are one config value away, and neither is a number I
invented.

### Two independent cross-checks against the same table

**(a) The median cancellation.** 6da's median span is 5,129 tok and its median
fork prompt is 26,620 tok → ratio **0.193**, just *below* break-even. That is
exactly why 6da measured the two arms cancelling "exactly" at the median and
recorded a −0.8% (noise) total run cost. A break-even sitting a hair above the
observed median ratio is what that observation predicts.

**(b) The bucket table.** 6da's span buckets, and what the predicate says about
each:

| population | 6da mean cost | implied mean span | ratio vs median prefix | predicate says | correct? |
|---|---|---|---|---|---|
| standalone, span ≤15k (n=40) | $0.0151 | ~4,300 tok | ~0.16 | **decline** → standalone | ✅ $0.0151 < $0.0265 |
| standalone, span >30k (n=17) | $0.1410 | ~40,000 tok | ≥0.67 | **fork** → flat ~$0.027 | ✅ 5.3× win |
| forked, any span (n=20) | $0.0265 (flat) | — | — | — | — |

Implied mean spans are `mean cost ÷ $0.00350 per 1k` — the standalone rate
from the same table. Both measured buckets are classified the way the money
went. This is pinned as a test
(`test_the_default_classifies_6das_own_measured_populations_correctly`), so a
future edit to the constant has to argue with the measurement.

### Why a ratio and not an absolute token threshold

The item offered either. A ratio is the correct primitive and an absolute
threshold is only correct at one prefix size: fork cost scales with **P**,
which grows all session, while standalone cost scales with **S**. The
break-even is a ratio of two *rates*, so the predicate must be a ratio. At
6da's median prefix the equivalent absolute threshold is ~5,900 span tokens; by
late session the same ratio is a much larger number. This also matches the
item's own acceptance criteria wording ("a configurable fraction of the
recorded prefix tokens").

---

## 4. A decline is not a fallback

The predicate is evaluated **strictly after** every existing alignment
precondition, never before. That ordering is load-bearing in both directions:

- no span size can buy a *misaligned* fork (a misaligned fork is wrong at any
  size — it pays for the whole conversation as fresh input);
- a misalignment is never misreported as an economic decision.

Consequently the two counters answer different questions and are kept apart:

| counter | meaning | what a nonzero value tells you |
|---|---|---|
| `fork_fallbacks` | a fork was **wanted and could not be done** | wiring/alignment defect — the silent-unforking signal 6da relied on |
| `fork_declines` | a fork was **possible and judged more expensive** | the predicate working |

Summing them into one number would make a healthy predicate look exactly like a
broken fork. Declines log at `INFO` (once per kind, because every decline
message carries its own token counts and message-level dedupe would dedupe
nothing); refusals keep their existing `WARNING`.

---

## 5. Does the 45.5% fork-rate cap apply? **Yes — and strictly.**

6da found 20 of 44 treatment-arm summarizer calls actually forked (45.5%), the
other 24 refused in two families — 12 × `note_request_sent() has never been
called`, 8 × `the span … is not present in the recorded prefix`. The first is
structural on a CLI workload: every turn is a fresh `amplifier run --resume`
process, and the summary trigger fires inside the first
`get_messages_for_request()`, strictly before that process has sent anything.

**The predicate is subject to that same cap, and cannot relieve it.** Because
it runs *after* the refusal checks, it can only ever decline forks that were
already possible. It lowers the realized fork rate; it can never raise it. The
45.5% is an upper bound on what the predicate has any say over.

**And the cap bites precisely where the predicate would have helped most.**
6da's own numbers: the treatment arm's fallbacks (n=17) cost **$0.08719 mean**
against **$0.02652** for forked calls — the refusals land on the large spans
that accumulate while a fork is impossible. Those are exactly the
high-ratio calls the predicate is designed to route *to* a fork, and they never
reach it.

So, stated plainly for the PR: **on a CLI workload this predicate's realized win
is bounded by the ≤45.5% forkable subset, and the expensive tail is
disproportionately outside that subset. On a long-lived in-process session
(where `note_request_sent()` fires before the first trigger) the cap does not
apply and the predicate captures the full tail.** Lifting the cap is a change to
the seam — arming `note_request_sent()` earlier, or persisting the sent-tools
fact across a resume — not to this predicate, and it would multiply this
predicate's value rather than substitute for it.

---

## 6. Tests

New file: `tests/test_summary_fork_span_predicate.py` — **31 tests**, four
groups, written against how this can silently go wrong rather than how it is
supposed to work:

- **A — the default must not move, in *both* fork modes.** `standalone` never
  evaluates the predicate or writes a predicate attribute; `fork` without an
  explicit ratio records no measurement and forks as before; `auto` resolves to
  the default; an explicit ratio overrides `auto`; bad values disable loudly,
  `0` disables silently; `mount()` threads both knobs.
- **B — the predicate fires above the threshold and not below.** The boundary is
  pinned from **both sides against the fixture's own realized ratio**
  (`_measure_realized_ratio()`, 0.498 in this fixture), separated by a single
  epsilon — a predicate tested only at 0.000001 and 99.0 would pass while being
  an order of magnitude wrong. Plus the 6da-population replay from §3(b), the
  `>=` inclusive-boundary semantics, a zero-token prefix (no divide-by-zero, no
  opinion), and ratios >1.0 as a legal "never fork" setting.
- **C — a decline is not a fallback.** Neither counter contaminates the other,
  in either direction; a misalignment is still a fallback *with the predicate
  armed at a ratio nothing could fail*, and records no `span_measure` (so the
  distribution an eval arm plots is not contaminated with calls the threshold
  never governed); declines log `INFO`, never `WARNING`, once per kind.
- **D — nothing else moves.** A declined fork sends the standalone request
  **byte-for-byte** (digest-compared against a control manager that was never in
  fork mode), still produces the summary, consumes no `_seq`, touches no
  history, selects the same span with tool-pairs intact, and leaves the next
  served view byte-identical.

**Full suite: 312 passed, 1 skipped** (baseline before this change: 281 passed,
1 skipped). `ruff check`: clean.

### The tests were mutation-checked, not just run

A suite that passes first try proves nothing until you make it fail on purpose.
Four mutations, each reverted after:

| mutation | caught by |
|---|---|
| predicate always allows (`if ratio >= threshold` → `if True`) | **10 tests** |
| `auto` no longer resolves to the default | 4 tests |
| a decline increments `fork_fallbacks` instead of `fork_declines` | 3 tests |
| `DEFAULT_FORK_MIN_SPAN_RATIO` changed 0.22 → 0.9 | 2 tests |

The ordering guarantee (alignment before economics) is covered by
`test_a_misalignment_is_still_a_fallback_even_with_the_predicate_on`, which
asserts `span_measure is None` on a misaligned call — moving the predicate
earlier populates it and fails the test.

### One honesty note on the suite

`tests/test_compaction_performance.py::test_compaction_scales_sub_quadratically`
failed **once**, during a run executed concurrently with a mutation pass on the
same machine. It is a wall-clock ratio assertion (`large/small < 8`) and is
load-sensitive by construction. It passed 3/3 in isolation and 3/3 in
back-to-back full-suite runs on an unloaded machine. Pre-existing flake under
load, unrelated to this change — recorded rather than quietly re-run.

---

## 7. What is NOT claimed

- **No new measurement.** This lane spent $0 and ran no eval. The predicate's
  *economic* justification is entirely 6da's measurement; what is proven *here*
  is structural — default byte-identity in both modes, correct classification of
  6da's own two measured populations, and the counter/ordering separation.
- **No claimed win on the item's A/B acceptance criterion.** The item's second
  criterion ("summarizer cost share falls measurably vs plain fork mode AND
  total run cost does not regress, both terms signed") requires an n≥3/arm
  S5-CRAC run that this lane's $0 authority does not fund. That criterion is
  **NOT-POSSIBLE at this budget** and is left open, deliberately and on the
  record, rather than substituted with an arithmetic counterfactual dressed up
  as a result.
- **What the A/B would need to be valid**, so it is cheap to commission: two
  arms differing *only* in `summary_fork_min_span_ratio` (off vs 0.22),
  per-summarizer-call attribution taken from the module's own
  `ChatResponse.usage` — **never positional `llm:request`/`llm:response`
  pairing**, which is the defect that produced the wrong 2.4%/8.3–10.9% figures
  — and `b_constraints` / `c_post_compaction` reported per arm so a cost win
  bought with a retention loss cannot be reported as a win. The new
  `span_measure` field exists precisely so that arm can plot the realized
  span:prefix distribution and re-derive its own threshold from its own data
  instead of trusting this module's default.

## 8. Open / recommended next

1. **Commission the A/B above** (the only thing standing between this and a
   measured verdict).
2. **Lift the 45.5% cap** — persist the sent-tools fact across a `--resume`, or
   arm `note_request_sent()` before the first `get_messages_for_request()`.
   Independent of this predicate and worth more *because* of it: 6da measured
   the capped calls at $0.0872 mean, the most expensive population in the run.
3. `00-what-we-know.md` §2h conflict #8 still records 2.4% vs 8.3–10.9%. 6da
   filed `PROPOSED-CORRECTIONS.md` beside its FINDINGS; the real figure is
   ~30%. Not this lane's file to edit (lane rule 2), noted so it is not lost.
