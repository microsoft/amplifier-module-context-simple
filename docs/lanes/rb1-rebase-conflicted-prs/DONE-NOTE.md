# DONE-NOTE — model_performance-rb1

`Merge-queue repair: rebase and land the conflicted lane PRs (unblocks 57p)`

**Spend: $0.00.** No API calls, no eval runs, no DTU, no containers, no
infrastructure created or registered (nothing to tear down). Everything below is
local `git` / `uv run pytest` / `gh` on this host. The lane's spend authority was
$0 and none of it was used.

| | |
|---|---|
| starting `origin/main` | `f47c894` (`token_meter "hybrid"`, on top of `d5ded0c` #23 and `c6dfbba` #20) |
| final `origin/main` | `3972070` |
| PRs landed | **#21 → `49e2799`**, **#24 → `3972070`** (both `--squash --admin`) |
| PRs deliberately untouched | routing-matrix **#49** (see §4) |
| suite, start → end | **139 passed → 208 passed, 1 skipped** |

---

## 1. PR #21 — tool-result budget + spill — **DONE, merged `49e2799`**

Branch `lane/x1r-tool-result-budget`, 3 commits, rebased `c6dfbba` → `f47c894`.

**What actually conflicted — two things, not one.** The lane brief predicted a
single `DONE-NOTE.md` add/add. There was also a **real code conflict**, and it is
recorded here rather than smoothed over:

| # | file | site | ours (`origin/main`) | theirs (#21) |
|---|---|---|---|---|
| 1 | `amplifier_module_context_simple/__init__.py` | `clear()` | `self._reset_hybrid_meter_state()` (from #22 `f47c894`) | `self._tool_name_by_call_id = {}` + `self._spilled_paths = set()` |
| 2 | `DONE-NOTE.md` | whole file | x7p note (from #23 `d5ded0c`) | x1r note |

`README.md` auto-merged; no third-party hunk.

**How each was resolved.**

1. **`clear()`** — the diff3 base section was **empty**, i.e. both sides *added*
   different lines at the same insertion point; neither modified the other's
   code. Resolved by keeping **both**, ours first. Semantically both are
   required: `clear()` must reset the hybrid meter anchor *and* drop the
   per-tool name map, and neither reset can substitute for the other.
2. **`DONE-NOTE.md`** — resolved by **keeping both notes verbatim**, not by
   picking a winner. The file was given the structure it now has: an HTML-comment
   convention header declaring it **shared and append-only**, an index table, then
   each lane's note under its own original `# DONE-NOTE — <item>` heading, oldest
   first. This is why the same conflict is cheaper for the next lane: the
   documented resolution is "append, never replace".

**Suite:** `187 passed` on the rebased branch (baseline `f47c894` = `139 passed`;
#21 contributes 48 tests). Green before the push, green on `origin/main` after.

## 2. PR #24 — `replay_last_user_on_compaction` — **DONE, merged `3972070`**

Branch `lane/l8-replay-last-user`, 1 commit, rebased `c6dfbba` → `49e2799`
(i.e. **after** #21 landed, as instructed).

**8 conflict hunks — 7 in `__init__.py`, 1 in `README.md`.** Every one of them was
additive-vs-additive at a shared insertion point (module docstring feature
section; `mount()` config docstring; `mount()` kwarg passthrough; `__init__`
signature; `__init__` Args docstring; `__init__` state initialisation; `clear()`
reset; and in `README.md`, two new sections anchored before `## Dependencies`).

Rather than eyeball eight hunks, the resolution was **mechanical and checked**:
a script walked the diff3 markers and, for each hunk, *asserted the base section
was empty* before emitting ours-then-theirs. Any hunk where the two sides had
actually touched the same pre-existing lines would have been reported and handled
by hand. **None were** — so the rebase is a pure re-application of #24's work on
top of #21 + #22 + #23, with nothing of theirs displaced.

**Proof the rebase changed nothing about the feature**, rather than an assertion:

```
git diff --stat origin/main..pr24
 README.md                                   |  83 ++++
 amplifier_module_context_simple/__init__.py | 277 +++++++++++
 tests/test_replay_last_user.py              | 702 ++++++++++++++++++++++++++++
 3 files changed, 1062 insertions(+)          <- 0 deletions
```

1,062 insertions and **zero deletions** against the new `origin/main` — identical
to the pre-rebase insertion count. Nothing already on `main` was modified or
removed to make room for the replay feature.

**Both features' defaults verified to coexist, post-resolution:**

| knob | default on merged `main` |
|---|---|
| `replay_last_user_on_compaction` | `False` (signature `= False`, and `config.get(..., False)`) |
| `token_meter` | `TOKEN_METER_ESTIMATE` (unchanged by #24 — 0 deletions) |
| `tool_result_budget_tokens` | `None` (#21's no-op default, unchanged) |
| `compaction_strategy` | `COMPACTION_STRATEGY_PROGRESSIVE` |

**Suite:** `208 passed, 1 skipped` on the rebased branch and on `origin/main`
after merge (`49e2799` = `187 passed`; #24 contributes 22 tests, one of which
skips — see §3).

## 3. DEVIATION — the "1 failing test" on #24 was **1 SKIP, never a failure**

The lane brief said #24's branch was "124/125 — find and fix the one failure
before merging, or REFUSE and say why." Measured on the branch **as it was, before
any rebase** (`89ada64`, base `c6dfbba`):

```
124 passed, 1 skipped in 5.00s
SKIPPED [1] tests/test_replay_last_user.py:682: amplifier-foundation cannot be
a dependency here (it requires amplifier-core>=1.0.10; this repo pins core
<1.0.10). The frozen reference in
test_predicate_is_strictly_stronger_than_foundation_1_0_0 covers the same
contract unconditionally.
```

**There was no failing test to fix, at any point.** The skip is a deliberate,
environment-conditional guard with its reason stated in the skip message, and the
same contract is covered **unconditionally** by a companion test that runs against
a frozen reference. "Fixing" it would mean adding `amplifier-foundation` as a
dependency, which this repo **cannot** take: foundation requires
`amplifier-core>=1.0.10` and this repo pins core `<1.0.10`. That is a dependency
change, not a test fix, and it is outside this lane's scope.

Recorded as a deviation rather than silently satisfied: the count `124/125` in the
brief was read as pass/fail when it was pass/skip. Nothing was changed to make the
number look different.

## 4. routing-matrix #49 — **DEFERRED, untouched (as instructed)**

A live lane owns that repo. This lane issued **no** command against it: not
cloned, not fetched, not checked out, no `gh` call. Nothing to hand over beyond
"still open, still owned elsewhere".

## 5. Is `model_performance-57p` unblocked? — **Partly. Be precise about which blocker.**

Filing nothing, per the brief; stating it here instead.

**Unblocked:** the *merge-queue* blocker is gone. The treatment (`#24`,
`replay_last_user_on_compaction`) is now **on `origin/main` at `3972070`**, still
**default-off**, alongside #21/#22/#23. The eval no longer needs a draft branch,
and no longer has to be run against a tree that conflicts with main.

**Still blocked, and this lane did not change it:** 57p's own description blocks
it on **S7 existing and being demonstrated to discriminate** — "the T0 progressive
baseline MEASURABLY loses the most recent user instruction". That is a scenario
prerequisite ([00-what-we-know §4.1]: S5-CRAC is saturated at 40/40 constraints
and 20/20 post-compaction in every arm of probes 1–6). Running G1 against a
saturated scenario reproduces the PROBE6 error. **Merging #24 did not create S7.**

**Two things the eval lane must re-do because `main` moved (confidence: measured):**

1. **The recorded T0 hash is stale.** 57p pins T0 as "flag off, verified
   byte-identical to `origin/main`, sha256
   `c985bbb95ec8aea0b74b058cc4ad109fbee73c822d6739a2df6ec547286789f9`". That hash
   was taken against the *old* `origin/main`. Four PRs (#20, #23, #22, #21) have
   landed since. T0 is still "the flag off", and the flag is still default-off, but
   **the hash no longer identifies the same tree** — re-baseline it against
   `3972070` rather than trusting the recorded value.
2. **One-variable discipline now has more knobs to hold still.** #21 added five
   tool-result knobs (`tool_result_budget_tokens`, `tool_result_shape`,
   `tool_result_budget_by_tool`, `tool_result_exempt_tools`,
   `tool_result_spill_dir`), all default no-op. The T0/T1 arms must leave **all**
   of them, plus `token_meter` and `compaction_strategy`, at their defaults — the
   arms differ only in `replay_last_user_on_compaction`.

## 6. Suite counts, every measurement point

| point | tree | result |
|---|---|---|
| baseline, before this lane | `origin/main` `f47c894` | **139 passed** |
| #24's branch, pre-rebase (as the brief found it) | `89ada64` (base `c6dfbba`) | **124 passed, 1 skipped** |
| #21 after rebase + conflict resolution | `c92dcb8` | **187 passed** |
| `origin/main` after #21 merged | `49e2799` | **187 passed** |
| #24 after rebase + conflict resolution | `de2a943` | **208 passed, 1 skipped** |
| `origin/main` after #24 merged (final) | `3972070` | **208 passed, 1 skipped** |

Command at every point: `uv run pytest -q` (with `-rs` where the skip is quoted).

## 7. Choices made without waiting for a human

Per the lane rule that no human decision is waited on — each choice, and why:

- **`DONE-NOTE.md` conflict → keep both, append-only, add a convention header.**
  The alternative (one lane's note wins) destroys a deliverable another lane was
  paid for. The header exists so the *next* add/add is resolved the same way
  without re-deciding.
- **#24's skip → recorded, not "fixed".** Making it pass requires a dependency
  this repo pins against. Recorded honestly instead (§3).
- **Merged with `--admin`.** Self-approval is blocked by the ruleset; `--admin`
  squash-merge is the sanctioned path already used for the six merges that landed
  earlier today. Both PRs were taken out of draft first (`gh pr ready`) — a draft
  cannot merge even with `--admin`.
- **Remote branch deletion:** `--delete-branch` deleted both remote branches;
  the *local* delete failed on both ("used by worktree"), which is cosmetic and
  left alone rather than tearing down another lane's worktree.

No PII, no team-internal data, no individual attribution.
