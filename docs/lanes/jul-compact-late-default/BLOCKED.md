# BLOCKED — `model_performance-jul` (W4-CL: ship compact-late as the context-simple DEFAULT)

**GOAL.md OUTCOME branch: C.** The outcome is unreachable **for a reason other than the
cap**. Item released via `work_release` while held, per Procedure step 5.

Lane: `jul-compact-late-default` · repo: `microsoft/amplifier-module-context-simple` ·
branch: `lane/jul-compact-late-default` · draft PR
[#32](https://github.com/microsoft/amplifier-module-context-simple/pull/32) · date: 2026-09-03

---

## What is unreachable

> "Ship compact-late (raised compaction trigger) as the context-simple **DEFAULT**"
> — and its acceptance criterion (a): *"the default trigger equals `cad-fewer`'s measured
> value with the old value reachable via config."*

There is no such default to change, and setting one to `cad-fewer`'s value would do the
opposite of what was measured.

## Why — the defect, and where it lives

**The defect is in the item's own premise**, which assumes `cad-fewer` overrode a shipped
context-simple default. It did not.

`cad-fewer`'s trigger came from `--max-tokens 70000` passed to the S5 harness. That value
only reaches the trigger because `scenarios/_harness/configure_cell.py:83-92` **patches this
module's source inside the container** to insert `budget = min(budget, self.max_tokens)`.
The patch does not exist in shipped source, and its own docstring states why it is needed:

> *"the loop always passes the provider, and `_calculate_budget` returns the model's
> context_window (200k, or 1M with enable_1m_context), so the configured max_tokens is dead
> and compaction never fires in a bounded run."*

Verified independently, each link, no run required:

| claim | evidence |
|---|---|
| `max_tokens` is consulted only as priority 4 | `amplifier_module_context_simple/__init__.py:1895-1961` |
| every orchestrator call site passes a provider | `loop-streaming/__init__.py:3215, 3329, 3453, 4017` |
| the shipped bundle sets `max_tokens: 300000` (inert) | `amplifier-foundation .../bundle.md:54-58` |
| `compact_threshold` has been 0.92 since the first commit | `git log -S`, this repo, full history |
| `cad-fewer` varied *only* the budget | `20260901-cadence/fewer_leg.sh:41` (target reverted to stock 0.50) |

**And adopting the value inverts the measured win.** `trigger = compact_threshold × budget`:

| provider window | shipped trigger | trigger if capped at 70,000 | direction |
|---|---:|---:|---|
| 200,000 | 150,791 | 64,400 | **2.34× earlier** |
| 1,000,000 | 857,351 | 64,400 | **13.3× earlier** |

Earlier trigger → **more** boundaries. The entire measured result (−29% requests, −14% wall)
came from having **fewer**. Stated positively: **the shipped default already compacts later
than `cad-fewer` did.** `[P4]`'s recommendation is "raise the trigger *where headroom
allows*", and the shipped default has none left — the budget is already the provider window
minus a 50%-of-max-output reserve and a 4,096-token margin.

## Why this is branch C and not A or B

- **Not A.** Branch A requires the deliverables to exist. Two of six do not.
- **Not B.** Branch B requires that *the spend authority could not fund the remaining work*.
  It could not fund the **guardrail** (see below), but that is a downstream deliverable. The
  **core** deliverable is unbuildable at **any** authority: $0 of $12 was spent and more
  money buys nothing. Filing this as cap-bound would be a false attribution to budget.
- **C, verbatim:** *"unreachable for a reason other than the cap."* The reason is a defect in
  the item's premise, established at $0.

An earlier revision of this lane's DONE-NOTE argued that none of the three branches fit and
proposed a fourth. **That was wrong**: GOAL.md states the three are exhaustive, and a lane
does not get to extend the taxonomy. The objection behind it was real but is a *consequence*
concern, not a classification one, and it is mitigated below rather than used to dodge the
branch.

## Mitigation for the re-queue hazard (the objection, handled inside branch C)

`work_release` returns the item to the **ready** queue, where the next lane can claim it,
read the same premise, and re-spend its authority rediscovering the dead knob. Two things
were done so that cannot happen silently:

1. **The item's own description was amended via `work_edit`** (attributed, non-destructive)
   to carry the falsifier, this file's path, and the PR link — so the finding travels with
   the item, not just with this lane's directory.
2. **The durable work is already merged-ready on the branch**, not stranded here: 10
   characterization tests pinning the trigger's real provenance, and a README section
   carrying the measurement. Before those tests, **no test in the suite exercised the
   provider-derived budget path at all** — all 87 used the no-provider fallback — so a PR
   "raising the trigger" via `max_tokens` would have gone green while doing nothing on the
   wire.

## What is NOT blocked — delivered on the branch, draft PR #32

| deliverable | state |
|---|---|
| Default trigger equals `cad-fewer`'s value | **BLOCKED — this file** |
| DTU guardrail, both providers | **NOT-POSSIBLE** — nothing to guard (no wire change), and the authority does not close: 4 arms × $2.65 / 0.667 valid = **$15.89** vs **$12**; even at perfect validity the $1.40 residue cannot buy the $2.13 minimum re-run. Both providers unrun for the same stated reason; neither silently dropped. |
| DTUs destroyed, ledger rows closed | **N/A** — none created; no `infra.tsv` row for this lane; `sweep` never run |
| CHANGELOG/README naming the measured numbers | **DONE** — README *"Where the compaction trigger comes from"*: 74 vs 104 requests, 485 vs 562 s, $2.65 vs $2.58, S5 95.0 vs 94.4; also corrects a pre-existing README error claiming compaction triggers at "92% of `max_tokens`" |
| Draft PR on origin, full suite green | **DONE** — PR #32, 97 passed (87 baseline + 10 new) |
| DONE-NOTE.md at the artifact root | **DONE** — `docs/lanes/jul-compact-late-default/DONE-NOTE.md`; repo-root `DONE-NOTE.md` untouched |

**Spend: $0.00 of $12.**

## What would unblock it

Not money, and not a prerequisite this lane can supply. An **owner decision** on one of:

1. **Re-spec the item** around `compact_threshold` (0.92 → higher) — the only shipped knob
   that expresses "compact later" independently of the provider. Unmeasured, and its ceiling
   is ~19% fewer boundaries against `cad-fewer`'s 56%, so it cannot reproduce the arm. Needs
   its own measurement and a guardrail funded at **≥ $15.89**.
2. **Decide whether `max_tokens` should become a real cap.** It would make the documented
   knob work and make an absolute trigger expressible without patching source — which is
   exactly what the cadence lane needed. It must default to *no cap* to stay a no-op, and a
   defaults-no-op feature is what `e9ac159` ("main carries wins only") reverts. An owner
   call, not a silent PR.
3. **Close the item as answered**, on the grounds that the shipped default already satisfies
   the intent (compact as late as headroom allows) and the finding is recorded in the module
   README where the next reader will hit it.

## Goal defects reported (not absorbed)

1. **The `$12` authority is stated as a bare figure**, contrary to GOAL.md's own AUTHORING
   RULE requiring `runs × arms × per-run / validity`. Supplied, it does not close: the
   guardrail needs **$15.89**, or **$18.02** with one spare re-run.
2. **GOAL.md's Task paragraph describes an unrelated item** (R0 `prompt_cache_mode` in
   `provider-openai`). The work item's own description was treated as authoritative per
   Procedure step 1; the `provider-openai` paragraph was not actioned and no file outside
   this repo was touched.
3. **The three-branch taxonomy has no state for "the deliverable is provably mis-specified
   and must not be re-queued."** This lane lands on C as required and mitigates the re-queue
   hazard by hand (above). Offered as a template improvement only — **not** claimed as this
   lane's terminal state.

Full reasoning, evidence and arithmetic: `DONE-NOTE.md` beside this file.
