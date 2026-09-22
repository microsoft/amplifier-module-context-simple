# DONE-NOTE — W3-4 / `model_performance-2o9`

`clear_at_least` — a worth-the-rebuild predicate in front of compaction
(context-simple), plus deepseek's summary shrink guard in the same PR.

**Spend: $0.00.** No API calls, no eval runs, no DTU, no containers, no
infrastructure created or registered (and therefore none to tear down).
Everything below is local `pytest` / `python` on this host. The lane's spend
authority was $0 and none of it was used.

---

## 1. Deliverables

| deliverable | status |
|---|---|
| DRAFT PR on origin, branch `lane/2o9-clear-at-least`, default byte-identical, full suite green | **DONE** — [microsoft/amplifier-module-context-simple#26](https://github.com/microsoft/amplifier-module-context-simple/pull/26) (draft, rebased on `3972070`) |
| Tests for block / allow / default byte-identity / prefix stability | **DONE** — 41 new tests, 249 passed + 1 skipped total |
| Follow-up eval item filed with a Given/When/Then gate | **DONE** — `model_performance-wxs`, filed `discovered-from` this item |
| DONE-NOTE.md in the PR body | **DONE** — this note |

---

## 2. The one design question the item asked me to answer honestly

The spec (`POC-SPECS/04-clear-at-least-predicate.md`) assumes a plan/apply
split:

```
plan  = build_compaction_plan(messages, target)
freed = tokens(messages) - tokens(apply(plan, messages))
```

**That split does not exist in this module.** `_compact_ephemeral` has no plan
object: it mutates an ephemeral copy level by level (levels 1–8), threading a
running `current_tokens` through every rung, and returns through
`_finalize_compaction_with_stats` from eight different call sites. There is no
point at which a projection is available *before* the work is done.

The GOAL says: *"if the projection is not available before the decision, say so
and implement the cheapest honest alternative, documenting the choice."* So,
stated plainly:

**Chosen: judge the ladder's actual result, not a projection of it.** The
predicate runs at `_finalize_compaction_with_stats` — the single terminal choke
point every level returns through — and *before* that method writes any durable
effect (sticky level, stats, `context:compaction` event). If the reclaim is
below the floor, the escalation is rolled back and the baseline view is
returned.

Three things make this the honest choice rather than a workaround:

1. **It is exact, not estimated.** The ladder runs on an ephemeral copy that
   never touches `self.messages` (`_truncate_tool_wave` does
   `messages[i] = self._truncate_tool_result(msg)` — a *new* dict;
   `_remove_messages_with_protection` returns a *new* list). So running it is a
   simulation whose only durable output is three sticky-decision sets, which are
   snapshot-and-restored. The number the predicate acts on is what the
   compaction *did*, not what it might do.
2. **No second estimator was introduced.** Both sides of the comparison go
   through the same `_estimate_tokens` the rest of the ladder runs on — which is
   exactly what the item required ("use the existing projection the ladder
   already computes; do not add a second estimator").
3. **The cost of a refusal is CPU, not tokens.** A refused boundary costs one
   wasted ladder pass over an in-memory list. The thing being avoided is a full
   cold prompt-cache rebuild. The asymmetry is not close.

**`freed` is the MARGINAL reclaim**, not the distance from raw history:

```
freed = tokens(view this call would have returned had it decided nothing new)
      - tokens(view the ladder actually produced)
```

This is deliberate and load-bearing. What breaks the provider's cache is *this
view differing from the last one*, not its distance from raw history. Measured
against raw history, an already-compacted session would report a large stale
"freed" on every single call and the predicate would wave through boundaries
that free nothing — the exact failure it exists to prevent.
`test_freed_is_marginal_not_measured_against_raw_history` pins this.

---

## 3. Decisions taken without waiting (per SCOPE-OUTS), and why

| # | decision | reasoning |
|---|---|---|
| 1 | **Fail-loud = `raise RuntimeError`**, not log-and-compact-anyway | The POC's own Risks §1 says the gate is *"written so the failure mode fails the run rather than degrading quietly"*, and G-CAL-NOSKIPHANG requires the run to FAIL. A silent override would let an eval pass while the predicate had stopped applying — the one outcome the gate exists to catch. Local precedent (`_provenance_overrides`) points the other way, but that escape exists to avoid a *guaranteed* provider hard-failure; here the operator has a knob to move, and the error names it. Unreachable while the predicate is disabled. |
| 2 | **The summary swap is NOT gated by the predicate** | By the time `_swap_in_pending_summary` returns it has already appended to `self.messages` and consumed a `_seq` — irreversible. Rather than build a fragile rollback for `self.messages`, that path is governed by the shrink guard (below), which checks *before* mutating. The predicate governs the progressive ladder, which is where the boundary-refire cost actually lives. |
| 3 | **Fraction form added** (`0 < v < 1` = fraction of budget) | The GOAL says "at least N tokens (or a fraction of the window)"; the POC spec said `int` only. A hardcoded token floor silently means something different on a 200k window than a 45k one. `1.0` is read as **absolute (1 token)**, not "100% of budget" — a floor of one whole budget can never be met, so the fraction reading would turn a plausible config into a guaranteed fail-loud. |
| 4 | **`compact_max_consecutive_skips <= 0` clamps UP to 1** | Read literally, 0 means "tolerate unlimited refusals" — precisely the silent hang the cap exists to prevent. |
| 5 | **A malformed value disables the predicate with a warning** | Consistent with how this module already handles `token_meter` / `compaction_strategy` / `tool_result_shape`. Never take a session down on a config typo. |
| 6 | Config names `compact_clear_at_least` / `compact_max_consecutive_skips` | Namespaced with the existing `compact_threshold`; the spec's bare `max_consecutive_skips` would have read as unrelated to compaction. |
| 7 | **No knob for the shrink guard** | There is no defensible reason to want a summary that makes the context bigger. |
| 8 | Predicate state does **not** survive `clear()` / `set_messages()` | A skip streak accumulated against one message set says nothing about a different one; inheriting it would fail loud on the first refusal after a resume. |

---

## 4. An interaction bug this lane introduced and fixed inside itself

The branch was cut from `f47c894`, but `origin/main` had advanced two commits
(`49e2799` tool-result budget, `3972070` last-user replay). **Rebased onto
`origin/main` (3972070)**; three conflicts, all additive, resolved by keeping
both sides.

The replay feature then created a real interaction:
`replay_last_user_on_compaction` fires once per compaction *boundary*, where a
boundary is `(_sticky_level, _summary_absorbed_count)`. **A refusal leaves both
unchanged by design** — the verdict runs before `_sticky_level` is bumped — so
on the first-ever refusal that identity still differs from the initial `None`
and would have looked like a fresh boundary. A verbatim user replay would have
been appended after a compaction that did not happen: misleading to the model,
and spending the tokens the refusal exists to save.

Fixed with an explicit call-scoped flag (`_clear_at_least_last_refused`) rather
than by changing the replay's own boundary logic, so the fix **cannot alter
main's behaviour** — the flag is always `False` while the predicate is
disabled. Pinned by `test_a_refusal_is_not_a_boundary_for_the_last_user_replay`
and its positive counterpart.

This is worth naming as evidence for the parallel-lane process: the bug did not
exist when this lane started and would not have been found by either lane
alone.

---

## 5. Evidence

### 5.1 Test suite

```
249 passed, 1 skipped in 5.57s
```

**41 new tests** in `tests/test_clear_at_least_predicate.py`; the 208 tests
already on `origin/main` are unmodified and green. The new tests are written
adversarially against the predicate's *own* failure mode (starvation), not just
its happy path:

- **Blocks** — an unsatisfiable floor refuses the boundary; the returned view is
  the untouched history; `context:compaction` is **not** emitted;
  `context:compaction-skipped` carries freed/required/level/skips.
- **Allows** — a floor of 1 token produces a view **byte-identical** to running
  with the predicate off (`test_allowed_boundary_is_identical_to_the_disabled_path`).
- **Default byte-identity** — `None`, `0`, and a negative value produce
  identical views *and* identical event streams; `_clear_at_least_pending` stays
  `None`, proving the guarded path is never *entered*, not merely that it agreed.
- **Rollback** — all three sticky sets, `_sticky_level`, and
  `_last_compaction_stats` are unchanged after a refusal; no compaction notice
  appears.
- **Prefix / `_seq` stability** — two consecutive refused calls with one turn of
  growth in between share a byte-identical prefix (exact comparison, no
  normalisation), and `_seq` identity is untouched.
- **Tool-pair integrity** — every `tool_calls` id in a refused view is answered
  by a `tool_call_id`.
- **Starvation** — no-op calls never count as skips; the cap raises with a
  message naming freed/required/protected set; state is rolled back *before* the
  raise; the streak resets on an accepted compaction and on clear/resume.
- **Floor resolution** — 12 parametrised cases covering None/0/negative/int/
  fraction/1.0-boundary/zero-budget/malformed.
- **Shrink guard** — larger summary refused (history untouched, span not
  recorded as removed, **not** counted as a summarizer failure); *equal-sized*
  summary refused (the `>=` boundary exercised directly by searching for a
  text that prices exactly at the span's estimate); genuinely smaller summary
  still swaps.

### 5.2 Default byte-identity vs `origin/main` (the honest stash-compare)

Captures:
`/home/bkrabach/dev/openai-evals-team-ci/.amplifier/evaluation/treatment-validation/20260902-2o9-clear-at-least/`
(`byte_identity_check.py`, `baseline_origin_main__init__.py`,
`treatment__init__.py`, `byte_identity_result.txt`,
`negctl_predicate_on_by_default__init__.py`, `negative_control_result.txt`).

Both module versions are loaded **in one process** and driven through identical
scenarios with identical inputs. Non-determinism is *eliminated*, not normalised
away: message timestamps are frozen to a fixed string after history is built, so
the two runs differ in nothing but the module source. Compared per call: the
returned view, every emitted hook event (name + payload), all three sticky
decision sets, `_sticky_level`, and `_last_compaction_stats`.

**11 of 11 scenarios byte-identical:**

```
IDENTICAL  default                       IDENTICAL  tool_result_head_tail
IDENTICAL  token_meter_actual            IDENTICAL  replay_last_user
IDENTICAL  token_meter_hybrid            IDENTICAL  notice_disabled
IDENTICAL  summary_strategy_no_provider  IDENTICAL  protected_tool_results_zero
IDENTICAL  tool_result_budget            IDENTICAL  larger_budget
                                         IDENTICAL  system_prompt_factory
ALL SCENARIOS BYTE-IDENTICAL
```

The scenario set deliberately covers **every other opt-in feature**, not just
the default path: "default byte-identical" must also mean "does not perturb a
feature that was already opt-in".

**The check is not vacuous.** A negative control that changes exactly one line —
`DEFAULT_CLEAR_AT_LEAST = None` → `20_000` — diverges immediately on the same
harness, and does so by raising the fail-loud error, which doubles as a live
demonstration of its content:

```
RuntimeError: context-simple: compact_clear_at_least=20000 could not be
satisfied 3 consecutive times (cap: 3). The compaction ladder reached level 8
and freed only 7,698 tokens against a required floor of 20,000. Protected set
holding the floor: protected_tool_results=1 (of 30 tool results in the view),
protected_recent=20% of 124 messages. Baseline view is 7,852 tokens against a
budget of 1,200. Lower compact_clear_at_least, lower
protected_recent/protected_tool_results, or raise the budget -- compacting
harder will not help.
```

(That fixture's budget is 1,200 tokens, so a 20k floor is unsatisfiable by
construction — i.e. exactly the misconfiguration the fail-loud path exists for,
and the message names the right knobs.)

---

## 6. What is NOT claimed

**No workload measurement exists. No performance claim rides into source, the
PR body, or the README** (§5 rule 6).

- The mechanism is implemented and unit-tested. Its effect on boundary count,
  cost, waste, or latency on a real workload is **unmeasured**.
- The eval is filed as **`model_performance-wxs`** with gates pre-registered
  *before* any spend: `G-CAL-BOUNDARIES` (monotonic dose-response across
  `{null, 10k, 20k, 40k}`, n≥3 — a flat response falsifies the mechanism),
  `G-CAL-NOSKIPHANG`, `G-CAL-LATENCY` (the pre-registered **win**),
  `G-CAL-COST` and `G-CAL-WASTE` (non-regression only), `G-CAL-SKIPCOUNT` (an
  instrument check that catches a run where every floor was trivially
  satisfiable), and the Anthropic guardrail from raw wire fields.
- **A cost reduction is deliberately NOT pre-registered as a win.** §2b is
  explicit that fewer boundaries "buys latency, not money" (`cad-fewer`: −29%
  requests, −14% wall, *same* cost, same quality), and the underlying fit
  (`waste ≈ 36.3 − 0.357 × boundaries`, r = −0.586, 12 points) is suggestive,
  not established.
- **A retention gate is rejected as vacuous** here as everywhere: `b_constraints`
  40/40 and `c_post_compaction` 20/20 in every run of every arm across probes
  1–6. Deferred to `model_performance-cb2`.
- **The 20,000 starting value is not pinned.** It derives from an 18,458-token
  head computed through a 4.59 chars/token constant that is tokenizer- and
  model-version-specific (DESIGN-SPACE.md C-4). The eval item requires
  re-deriving the head *in tokens* from provider usage first. The README says so
  too.
- **Do not run this on the bare estimator.** The default `token_meter:
  "estimate"` is `len(str)//4`, never reconciled against provider usage and ~2×
  off in production sessions; a predicate acting on a 2× off number refuses the
  wrong boundaries. `model_performance-q69` landed the provider-anchored hybrid
  meter with provenance precisely so this predicate has an anchored number.
  README and the eval item both state `token_meter: "hybrid"` as the operating
  condition.

**Known limitation, disclosed:** a refused call pays one wasted ladder pass
(CPU over an in-memory list, no tokens). Also, like the rest of this module's
shared mutable state, the predicate's call-scoped state assumes
`get_messages_for_request` is not re-entered concurrently — a pre-existing
property of the module, not new exposure, but now with one more field.

No PII, no team-internal data, no individual attribution in any output. No
merges to main; no files touched outside this module.

