# DONE-NOTE — `model_performance-jul` (W4-CL: ship compact-late as the context-simple default)

Lane: `jul-compact-late-default` · repo: `microsoft/amplifier-module-context-simple` ·
branch: `lane/jul-compact-late-default` · date: 2026-09-03

**Terminal outcome: GOAL.md branch C — BLOCKED, item released.** `BLOCKED.md` sits beside
this file and is committed. The outcome ("ship compact-late as the context-simple DEFAULT")
is unreachable **for a reason other than the cap**: `cad-fewer` overrode no shipped default,
and adopting its value would invert the measured win. Established at **$0 of $12**.

The work itself is not blocked and is not stranded — draft PR
[#32](https://github.com/microsoft/amplifier-module-context-simple/pull/32) carries the
measurement's provenance and the tests that stop the next lane walking into the same trap.

---

## 0. Terminal-state classification — and a correction of record

**This lane got its terminal state wrong twice before landing on C. Both errors are recorded
here rather than quietly rewritten.**

| revision | claimed state | what was wrong |
|---|---|---|
| 1st (`570502a`) | "branch A (RESOLVED)" | Branch A requires *resolved **AND** the deliverables exist*. Two of six did not. The marker annotated its way around a conjunction instead of failing it. |
| 2nd (`d32d280`) | "none of the three fits; propose a branch D" | Worse, not better. GOAL.md states the three branches **are exhaustive**. A lane does not get to extend the taxonomy of the contract it is working under. An undefined terminal state fails the goal condition outright. |
| 3rd (this one) | **C — BLOCKED, released** | Correct. See below. |

### Why C, tested against all three

- **Not A.** Two of six deliverables do not exist. False by its own conjunction.
- **Not B.** B requires that *the spend authority could not fund the remaining work*. It
  could not fund the **guardrail** — but that is downstream. The **core** deliverable is
  unbuildable at **any** authority: $0 of $12 spent, and more money buys nothing. Filing it
  as cap-bound would be a false attribution to budget, which is exactly the failure mode
  GOAL.md warns about in the other direction.
- **C, verbatim.** *"The outcome is unreachable for a reason other than the cap."* It is —
  the reason is a defect in the item's premise, which assumed `cad-fewer` overrode a shipped
  default. Its enumerated examples ("a missing prerequisite ... a defect in another
  component") follow an em-dash and illustrate that clause; they do not narrow it.

### The objection that produced revision 2, and how it is handled inside C

C's remedy is `work_release`, which returns the item to the **ready** queue — where the next
lane can claim it, read the same premise, and re-spend its authority rediscovering the dead
knob. That hazard is real. It was wrong to treat it as grounds for refusing the branch: it is
a **consequence** concern, not a **classification** one. Handled inside C instead:

1. **`work_edit` amended the item's own description** (attributed, non-destructive — the
   acceptance criteria were left untouched, they are the owner's) to carry the falsifier,
   `BLOCKED.md`'s path, and the PR link. The finding now travels with the item, not just with
   this lane's directory, which the next claimant never sees.
2. **The durable work is on the branch and merge-ready**, not held hostage to the
   classification.

### Cost of the correction, disclosed

`work_reopen` cleared `closed_at` (was `2026-09-03T08:16:45Z`), so this item re-lands on the
correction date and every throughput roll-up moves by one. That cost was accepted to reach a
**defined** terminal state; GOAL.md's anti-churn rule ("choose the terminal state ONCE",
citing lane 1ru) governs re-deciding between two *valid* states on unchanged evidence, which
is not what happened here — revisions 1 and 2 were not valid states at all.

A template improvement is still worth making — the taxonomy has no state for "provably
mis-specified, must not be re-queued" — but that is a **recommendation to the owner**, filed
in `BLOCKED.md` §"Goal defects", and explicitly **not** this lane's terminal state.

---

## 1. Deviation from GOAL.md, declared up front

`GOAL.md`'s **Task** paragraph describes an unrelated item (R0 `prompt_cache_mode` in
`provider-openai`, plus a `reasoning.context` gate fix). Its title, OUTCOME, and
DELIVERABLES describe compact-late in `context-simple`, this worktree is a checkout of
`context-simple`, and Procedure step 1 states that the work item's own description and
acceptance criteria are authoritative. **I followed the work item.** The `provider-openai`
paragraph was not actioned and no file outside this repo was touched.

---

## 2. What was measured, and what it actually says

Source: `[P4]` `.amplifier/evaluation/treatment-validation/20260901-cadence/PROBE4-VERDICT.md`
(arm table, lines 58–75; policy recommendation, lines 165–174).

| arm | boundaries | requests | wall (s) | cost ($) | S5 | post-C retention |
|---|---:|---:|---:|---:|---:|---:|
| `cad-today` (trigger 45k, n=5) | 21.6 | 104 | 562 | 2.58 | 94.4 | 20/20 |
| `cad-fewer` (trigger 70k, n=2) | 9.5 | **74** | **485** | 2.65 | 95.0 | 20/20 |

−29% requests, −14% wall, cost nil-and-slightly-negative, quality equal.
(knob moved: compaction trigger budget · family: gpt-5.6-terra · confidence: **measured**,
n=2 vs n=5 reused baselines · evidence: capture root above.)

### How `cad-fewer` actually set the trigger — the thing the item told me to check first

`fewer_leg.sh:41` passes `--max-tokens 70000` to `s5-crac/scripted_driver.py`, which
forwards it to `scenarios/_harness/configure_cell.py`. That script does **two** things
(`configure_cell.py:83–92`, verbatim in its own docstring):

1. rewrites `context.config.max_tokens` in the cached foundation `bundle.md`; **and**
2. **patches this module's source in-container** to insert
   `budget = min(budget, self.max_tokens)` into `_calculate_budget`.

Its stated reason for (2): *"Without (2), the loop always passes the provider, and
`_calculate_budget` returns the model's context_window (200k, or 1M with
enable_1m_context), so the configured max_tokens is dead and compaction never fires in a
bounded run."*

I verified each link independently rather than taking the docstring's word:

- `amplifier_module_context_simple/__init__.py:1895–1961` — `_calculate_budget` consults
  `max_tokens` **only** as priority 4, after an explicit `token_budget`,
  `provider.get_model_info()`, and `provider.get_info().defaults`.
- `amplifier-module-loop-streaming/__init__.py:3215, 3329, 3453, 4017` — every call site is
  `get_messages_for_request(provider=provider)`. Never `token_budget=`.
- `amplifier-foundation .../bundle.md:54–58` — the shipped bundle configures
  `context-simple` with `max_tokens: 300000`.
- `git log -S` on this repo — `compact_threshold` has been **0.92 since the repository's
  first commit** and was never 0.8. `cad-fewer` did not override it; `fewer_leg.sh` explicitly
  reverted `target_usage` to stock 0.50 so the arm varied *only* the budget.

(confidence: **measured** — code read plus git archaeology, no run required.)

**Conclusion: `max_tokens` is a dead knob in production, and there is no shipped module
default equal to 45,000 or 70,000.** `cad-fewer` overrode a harness forcing value through a
container-only source patch.

---

## 3. Why "make cad-fewer's value the default" must not be done

`trigger = compact_threshold × effective_budget`. Making 70,000 the default means capping
every session's budget at 70,000, i.e. a trigger of `0.92 × 70,000 = 64,400` tokens for
everyone.

| provider window | shipped budget | shipped trigger | trigger if capped at 70k | direction |
|---|---:|---:|---:|---|
| 200,000 (max_out 64k) | 163,904 | 150,791 | 64,400 | **2.34× earlier** |
| 1,000,000 (max_out 128k) | 931,904 | 857,351 | 64,400 | **13.3× earlier** |

An earlier trigger means **more** boundaries — and the entire measured win came from having
**fewer** of them. The literal deliverable inverts its own evidence.

(Figures above are `compact_threshold × budget`. `get_messages_for_request` additionally
subtracts the 800-token compaction-notice reserve before applying the threshold, which moves
each number down by 736 tokens — e.g. 150,791 → 150,056, the value the empirical check in §4
reports. It changes no ratio and no conclusion; both forms appear in this note because §4
quotes a live run and §3 quotes the formula.)

Stated the other way round, which is the more useful form: **the shipped default already
compacts later than `cad-fewer` did** (150,791 or 857,351 tokens vs `cad-fewer`'s 64,400).
`[P4]`'s policy recommendation is "raise the compaction trigger *where headroom allows*" —
and in the shipped default there is no headroom left to give: the budget is already the
provider's window minus a 50%-of-max-output reserve and a 4,096-token safety margin.

The one shipped knob that expresses "compact later" independently of the provider is
`compact_threshold`. Its full remaining range (0.92 → 1.00, i.e. zero headroom for the
response) buys at most ~19% fewer boundaries, against `cad-fewer`'s 56%. It cannot reproduce
the arm, and no measurement of it exists. (confidence: **inferred** — arithmetic on the
shipped formula, from measured constants.)

---

## 4. What shipped instead — commit `259bb0d`, zero behavior change

Under the repo's stated merge policy (`e9ac159`: *"main carries wins only"*, which reverted
seven unproven default-off / defaults-no-op features), a new opt-in knob would be reverted
on arrival and an unmeasured default flip is disallowed by lane rule 6. So this PR changes
no behavior at all. It ships the two things that are defensible on evidence already in hand:

**`tests/test_compaction_trigger_provenance.py` — 10 tests.** Before this file, **no test
in the suite exercised the provider-derived budget path**: all 87 existing tests construct
the manager with `max_tokens` and no provider, i.e. exclusively the fallback branch. The
trap was invisible to the suite — a PR "raising the trigger" via `max_tokens` would have
gone green while doing nothing on the wire, which is precisely the failure mode the item
warned about. The tests pin:

- the provider budget wins over `max_tokens` on both provider paths;
- `max_tokens` is reached only when no provider reports a window;
- **the trap end-to-end**: identical history and config does *not* compact with a provider
  present and *does* compact with it absent — the second half being the deliberate
  non-vacuity control (lane rule 5), since a test asserting "nothing happened" is worthless
  without one. Measured fill: 51,070 estimated tokens, against a 45k-fallback trigger of
  40,664 and a 200k-provider trigger of 150,056 — comfortably discriminating on both sides;
- `compact_threshold == 0.92` pinned, with its override exercised in both directions
  (0.80 and 0.95), so the "old value reachable via config" property is under test;
- the parametrized arithmetic from §3: a 70,000 cap moves the trigger earlier, not later.

**README — `Where the compaction trigger comes from`.** The priority order, the dead-knob
warning with the harness evidence, `compact_threshold` as the real lever with a config
example, and the measured cadence table with both limits its own source states. Also
corrects a pre-existing factual error in the README: it claimed compaction triggers at
"92% of max_tokens", which is exactly the wrong belief this lane spent its effort refuting.

Full suite: **97 passed** (87 baseline + 10 new), `uv run pytest -q`.

---

## 5. Spend, with arithmetic

**Total spend: $0.00** against a $12 authority. No API run, no DTU, no container, no ledger
row created (`infra.tsv` contains no `jul` row — verified), therefore nothing to tear down.
`lane_teardown.sh` was not invoked because this lane owns no rows; `infra_ledger.sh sweep`
was never run.

### The guardrail was priced before spending, as the goal requires — and it does not close

The deliverable is "anchors + anchors-amp-dev, openai AND anthropic roots" = 2 bundles ×
2 providers = **4 arms**, minimum one run each.

```
per-run price, quoted for THIS run count from the S5 arm table [P4]:
  $2.13 – $3.47 per run, mean $2.65 (n=12)

best case, perfect validity:
  4 runs × $2.65            = $10.60      → residue $1.40
observed-validity case (0.667, the rate this program's own runs exhibit):
  4 runs × $2.65 / 0.667    = $15.89      → over the $12 authority by $3.89
```

Even the best case fails: the $1.40 residue cannot buy a single re-run of a failed arm
(minimum useful purchase **$2.13**), so any one capture failure — and the cadence lane
recorded capture failures — leaves the deliverable unfinishable with money still nominally
"remaining". **Smallest useful purchase the residue could not buy: one S5 arm re-run at
$2.13.**

This is also, per the goal's own AUTHORING RULE, a reportable defect in the goal: the $12
authority is stated as a bare figure with no `runs × arms × per-run / validity` arithmetic,
and when the arithmetic is supplied it does not close. The authority that *would* close it
is **$15.89** (4 arms, one run each, at the observed 0.667 validity rate); with one spare
re-run budgeted, **$18.02**.

**But the cap is not why the guardrail did not run.** It did not run because **there is no
behavior change to guard.** A guardrail exists to catch an Anthropic cache regression caused
by a raised trigger changing when the prefix is rebuilt; this PR does not change when the
prefix is rebuilt, or anything else on the wire. Spending $10–16 to measure a no-op would
have been spend without a gate. If the owner ratifies a real default change, the guardrail
must run then, at an authority of at least $15.89.

---

## 6. Deliverables — disposition

| # | deliverable | state | reason |
|---|---|---|---|
| 1 | Default trigger equals `cad-fewer`'s measured value, old value via config, tests for both | **NOT-POSSIBLE (as specified)** | `cad-fewer`'s value is a harness forcing knob applied through a container-only source patch; no shipped default equals it; adopting 70,000 moves the trigger **2.3–13.3× earlier**, inverting the measured win (§3). The *property* the change was meant to buy — compact as late as headroom allows — is **already true of the shipped default**. The `compact_threshold` half of the deliverable ("old value reachable via config, tests covering both") **is** delivered and under test. $0 spent. |
| 2 | DTU guardrail, both providers, anchors + anchors-amp-dev at pinned SHAs | **NOT-POSSIBLE** | Two independent reasons, either sufficient: (i) no wire behavior changed, so there is nothing to guard; (ii) the authority does not close — $15.89 needed at observed validity vs $12 available, and even at perfect validity the $1.40 residue cannot buy the $2.13 minimum re-run (§5). Not run for reason (i); reason (ii) is recorded because the goal asks for the arithmetic on first read. Neither provider was silently dropped: **both** are unrun, for the same stated reason. |
| 3 | DTUs destroyed after the run, ledger rows closed | **N/A — none created** | No DTU launched, no `infra.tsv` row for this lane. `sweep` never run. |
| 4 | CHANGELOG/README line naming the measured numbers (74 vs 104 requests, 485 vs 562 s, $2.65 vs $2.58, S5 95.0 vs 94.4) | **DONE** | README §*Where the compaction trigger comes from*, full arm table plus the two limits from the source. Provenance is now readable without the tracker. |
| 5 | DRAFT PR on origin, full module suite green | **DONE** | 97 passed. See `publication` block in `DONE.json` for the read-back branch/PR values. |
| 6 | DONE-NOTE.md in the PR body | **DONE** | This file, at the lane artifact root. The repo-root `DONE-NOTE.md` was **not** created or modified. |

---

## 7. What the owner should decide next

0. **The item is released and back in the ready queue** (branch C). It should not be
   re-claimed as written — its premise is falsified. Re-spec it, or close it as answered; see
   `BLOCKED.md` §"What would unblock it" for the three options. Separately, worth considering
   a fourth branch for the goal template ("provably mis-specified, do not re-queue"); two
   lanes have now hit that gap, and this one had to mitigate it by hand.
1. **Is "compact late" still wanted as a default?** If yes, the only shipped lever is
   `compact_threshold` (0.92 → higher), it is unmeasured, and its ceiling is ~19% fewer
   boundaries. That is a *new measurement*, not this item — and it needs a real guardrail at
   ≥ $15.89.
2. **Should `max_tokens` become a real cap?** It would make the documented knob work and make
   an absolute trigger expressible without patching source — which is what the cadence lane
   needed. It must default to *no cap* to stay a no-op, and a defaults-no-op feature is
   exactly what `e9ac159` reverts. So: worth a decision, not worth a silent PR.
3. **`[P4]`'s own recommendation should be re-read as scoped.** "Raise the compaction trigger
   where headroom allows" is true, and in the shipped default there is no headroom — the
   budget is already the provider window minus reserve and margin. Any future lane quoting
   that recommendation as a shippable default change should be pointed at §3 above first.

## 8. Honest gaps

1. The harm arithmetic in §3 uses `max_output_tokens` of 64,000 / 128,000 for the two window
   sizes. Those are illustrative, not read off a live provider response. The *direction* of
   the claim does not depend on them: for **any** provider whose derived budget exceeds
   70,000, a 70,000 cap moves the trigger earlier. Only the exact ratio would move.
2. The cadence result itself is n=2 for `cad-fewer` against n=5 reused baselines, on one
   scenario whose 20/20 post-compaction retention across every arm may be a ceiling effect —
   its source says so, and nothing here strengthens it.
3. No run of any kind was performed by this lane. Every claim above is code-read, git-read, or
   arithmetic on measurements taken by `[P4]`. Nothing is labelled "measured" that this lane
   did not verify at the file it cites.
