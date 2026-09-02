# DONE-NOTE — model_performance-x7p

`context-simple: protected_tool_results=0 protects ALL tool results (negative-slice bug)`

**Spend: $0.00.** No API calls, no eval runs, no DTU, no infrastructure created or
registered. Everything below is local `pytest` / `grep` / `python` on this host.
The lane's spend authority was $0 and none of it was used.

---

## 1. The defect, verified in the code that is actually on `origin/main`

`HEAD` == `origin/main` == `c6dfbba43a8efb166b07f5255329bb9ed573576a` (fetched and
compared; `git diff HEAD origin/main` empty).

**The line numbers in the tracker item had moved.** The item cites
`__init__.py:1407`, `:1510`, `:1577`. On `origin/main` today the three sites are:

| site | line on `origin/main` (c6dfbba) | item said |
|---|---|---|
| levels 1+2 protected set | `__init__.py:1243-1245` | 1407 |
| level 4 protected set (recomputed after removal) | `__init__.py:1346-1348` | 1510 |
| level 6 protected set (recomputed again) | `__init__.py:1413-1415` | 1577 |

All three were byte-identical:

```python
protected_tool_indices = set(
    tool_result_indices[-self.protected_tool_results :]
)
```

A `grep` for the negative-slice shape (`\[-self\.`) over the whole module returns
exactly those three lines — there is no fourth site. **DONE: verified before
changing anything.**

Python's `list[-0:]` is `list[0:]`, i.e. the whole list. So at exactly one value —
`protected_tool_results=0`, the value that *reads* as "protect nothing" — the
protected set becomes every tool result, all four truncation rungs (levels 1, 2, 4,
6) become no-ops, and compaction escalates straight to message **removal**, which is
strictly more lossy than the truncation it skipped.

**Reproduced by execution, not by reading** (workload: 8 tool pairs × 800 chars,
3,000-token budget, `target_usage=0.60`; identical except for the knob):

| `protected_tool_results` | tool results truncated | escalation level reached | messages removed |
|---|---|---|---|
| **0 (pre-fix)** | **0** | **3 (removal)** | **6** |
| 1 | 4 | 2 (truncation only) | 0 |
| 5 (default) | 4 | 2 (truncation only) | 0 |

---

## 2. The fix

The three call sites now route through one helper,
`SimpleContextManager._protected_tool_indices()`, which returns an empty set when
the knob is `<= 0` and otherwise takes the same `[-N:]` slice as before.

One place to be correct instead of three, and the docstring states *why* the guard
is load-bearing rather than defensive — so the next person to "simplify" it back
into an inline slice has to read the reason first.

A **negative** value is folded into the same branch (protect nothing). Handed to the
slice, `-1` would silently mean "protect the last 1" — the opposite of what a
negative reads like. This is a decision made in-lane, recorded here: negative is
treated as 0, not as an error, because the constructor validates nothing else either
and raising here would be a new failure mode in a bug-fix PR.

Two docstring lines (`__init__.py:168`, `:344`) now state that 0 protects none.

**Diff: 3 call sites collapsed to a call, 1 helper (+22 lines incl. docstring), 2
docstring lines. No other behavior touched.**

---

## 3. DELIVERABLE — regression test, fail-before / pass-after

**DONE.** `tests/test_protected_tool_results.py`, 11 tests.

Fail-before was proved by side-by-side, not by assertion: the pre-change module was
extracted with `git show HEAD:amplifier_module_context_simple/__init__.py` into a
scratch tree, the *new* test file copied in beside it, and pytest run from that tree
so the pre-change module shadowed the installed one (`module file: .../before.local/...`,
`has _protected_tool_indices: False`).

**Verbatim, against the PRE-CHANGE module:**

```
=========================== short test summary info ============================
FAILED tests/test_protected_tool_results.py::test_zero_protects_zero_tool_results
FAILED tests/test_protected_tool_results.py::test_protected_tool_indices[0-expected0]
FAILED tests/test_protected_tool_results.py::test_protected_tool_indices[1-expected1]
FAILED tests/test_protected_tool_results.py::test_protected_tool_indices[2-expected2]
FAILED tests/test_protected_tool_results.py::test_protected_tool_indices[5-expected3]
FAILED tests/test_protected_tool_results.py::test_protected_tool_indices[99-expected4]
FAILED tests/test_protected_tool_results.py::test_protected_tool_indices[-1-expected5]
FAILED tests/test_protected_tool_results.py::test_protected_tool_indices_empty_input
FAILED tests/test_protected_tool_results.py::test_protected_tool_indices_uses_real_positions_not_ordinals
9 failed, 2 passed in 0.03s
```

And the load-bearing one on its own — this is the bug, not a missing-attribute error:

```
_____________________ test_zero_protects_zero_tool_results _____________________
E       AssertionError: With protected_tool_results=0 the protected set must be EMPTY,
        so the oldest 50% of tool results (waves 1+2) are truncated. Pre-fix this was []
        because [-0:] protected everything.
        Got: {'level': 3, 'messages_removed': 6, 'messages_truncated': 0, 'truncated_tool_ids': []}
E       assert [] == ['t0', 't1', 't2', 't3']
E         Right contains 4 more items, first extra item: 't0'
tests/test_protected_tool_results.py:111: AssertionError
```

**Honest reading of that 9-failed/2-passed split** — the three numbers are not the
same kind of evidence:

* **1 failure is the bug itself.** `test_zero_protects_zero_tool_results` fails on
  the pre-change module with a real behavioral assertion and passes after. This is
  the fail-before/pass-after the item asked for.
* **8 failures are `AttributeError`** — the helper does not exist pre-change, so
  those unit tests cannot run there. They pin the new surface; they are *not*
  independent evidence of the bug, and are not counted as such.
* **2 PASSES pre-change are the point of the other half.**
  `test_n_protects_exactly_the_last_n_boundary` and
  `test_protecting_every_tool_result_still_works` pass on BOTH modules — that is the
  non-regression pin: `N > 0` behavior was never broken and is not changed.

**After the fix: 11/11 pass. Full suite: 114 passed** (103 pre-existing, all green,
plus these 11).

### How the "exactly the last N" half is pinned behaviorally

With 8 tool results, truncation waves 1+2 cover indices 0–3; the 5th-from-last tool
result *is* index 3. So the protected boundary is observable as a flip:

* `N=4` → protects indices 4–7 → index 3 truncatable → target reached by truncation
  alone (level 2, **0** messages removed).
* `N=5` → protects indices 3–7 → index 3 withheld → truncation can no longer reach
  target → **escalates to removal** (level 3, messages removed > 0).

An off-by-one in either direction moves that flip to a different `N`, so this pins
the boundary rather than merely "some protection happens".

---

## 4. DELIVERABLE — byte-identity check for every OTHER configuration value

**DONE.** 57-cell matrix, run against the pre-change and fixed modules, sha256 over
the JSON-serialised returned message list **plus** the compaction stats dict.

**A near-miss worth recording.** The first run reported *all 57 cells changed* —
which would have been a false alarm. `metadata["timestamp"]` is wall-clock, so a
same-module self-test showed 57/57 differing against **itself**. Timestamps are now
stripped before hashing and the self-test passes (57/57 identical across two runs of
one module) before the cross-module comparison is believed. Recorded because a
byte-identity check that has not been shown to be deterministic proves nothing.

**Result: 57 cells — 45 SAME, 12 DIFF. Every differing cell is
`protected_tool_results=0`. No cell with any other value differs.**

```
DIFF  39ce6394c3b36f55  0f6cf15629233adb  ptr=0
DIFF  3caaf8aec9c78835  79d013d40970b989  ptr=0,target_usage=0.3
DIFF  0e1fa5c985f8d9d9  46fc7faf46ffb28e  ptr=0,target_usage=0.5
DIFF  820772fc1b641d28  48af88140cd3e087  ptr=0,target_usage=0.7
DIFF  901280b3d5b135aa  3c54ebeeb60e7e29  ptr=0,protected_recent=0.3
DIFF  f9bc605a31c38740  c66fa9c67e939faf  ptr=0,protected_recent=0.9
DIFF  39ce6394c3b36f55  f4bf0b76df325aa1  ptr=0,truncate_chars=20
DIFF  39ce6394c3b36f55  4163e069f3725810  ptr=0,truncate_chars=200
DIFF  39ce6394c3b36f55  64279bdf89fd8799  ptr=0,truncate_chars=2000
DIFF  808f75bd2667c7f0  d5b9635680ecd3f0  ptr=0,max_tokens=1500
DIFF  4fe1c5e0d244a27e  31b4f79ee390ca82  ptr=0,max_tokens=8000
DIFF  8206d63ba77bb183  9b5fae2f7489edc4  ptr=0,notice=on

--- any DIFF cell whose ptr is NOT 0? ---
(none - every differing cell is protected_tool_results=0)

SAME  9f33469f30bcb500  9f33469f30bcb500  ptr=5
SAME  9f33469f30bcb500  9f33469f30bcb500  DEFAULT(no ptr arg)
```

Cells covered: `protected_tool_results` 0–16 swept; then `ptr ∈ {0,1,5}` crossed with
`target_usage ∈ {0.3,0.5,0.7}`, `protected_recent ∈ {0.1,0.3,0.9}`,
`truncate_chars ∈ {20,200,2000}`, `max_tokens ∈ {1500,8000,40000}`, and the
compaction notice on; plus default construction with the argument not passed at all.

**Corroborating detail the matrix exposed for free:** pre-fix, `ptr=0` with
`truncate_chars` of 20, 200 and 2000 all produced the *identical* hash
`39ce6394c3b36f55` — the same hash as the `ptr=0` baseline. `truncate_chars` had
literally no effect, because truncation never fired at all. Post-fix all three
differ. That is the bug visible from a second, independent direction.

Two `ptr=0` cells did **not** change (`protected_recent=0.1`, `max_tokens=40000`) —
stated rather than hidden: at `max_tokens=40000` the workload never crosses the
compaction threshold, so there is nothing for either module to do.

---

## 5. DELIVERABLE — does this contaminate any measurement the program has banked?

# NO — with the evidence.

Four independent lines, all negative:

**(a) The only path to a non-default value is a bundle `config:` block.**
`mount()` reads `config.get("protected_tool_results", 5)` (`__init__.py:238`).
Every bundle in these roots that mounts `context-simple` does so with **no `config:`
block at all**:

```
bundles/minimal.yaml:11:    module: context-simple
bundles/minimal.yaml:12:    source: git+https://github.com/microsoft/amplifier-module-context-simple@main
bundles/minimal.yaml:13:
bundles/minimal.yaml:14: tools:            <- next key; no config:
```

Checked in `amplifier-foundation-pr325`, `amplifier-foundation-ephemeral-docs`, and
the resolved cache bundle the runs actually loaded
(`~/.amplifier/cache/amplifier-foundation-c909465861f9d6ce/bundles/minimal.yaml`).
`~/.amplifier/settings.yaml` contains no `context-simple` config and no
`protected_tool_results` key.

**(b) Full-tree literal search of the 29 GB evals repo** for all five spellings
(`"protected_tool_results": 0`, `":0`, `: 0`, `=0`, ` = 0`) → **2 hits, both prose**,
both in `20260902-x1r/DONE-NOTE.md` *describing this very bug*. Zero configs.

**(c) Same search over `hw-model-performance` (356 MB)** → hits are the x7p goal
file, x1r's DONE-NOTE, x1r's `lane.log`, and `watchdog.log`. All prose about the bug.
Zero configs.

**(d) The decisive one — what the runs themselves emitted.** Compaction emits the
live value in its stats (`"protected_tool_results": self.protected_tool_results`,
`__init__.py:1987`). Tallying every occurrence across the entire
`.amplifier/evaluation` capture root:

```
    518 "protected_tool_results": 5
    467 "protected_tool_results":5
     24 "protected_tool_results": 1
      4 "protected_tool_results": 2
```

**1,013 records. 985 at the default 5. Zero at 0. Zero negative.** (An explicit
search for `0` or any negative in that field returns nothing.)

The 24 ones and 4 twos are **not** run records — every file carrying them is a test
fixture or harness: x1r's own `byte_identity_harness.py` / `byte-identity-{PRE,POST,
TREATMENT}.json`, and two vendored copies of
`tests/test_sticky_compaction_and_tail_notice.py` inside captured session caches.
90 distinct files carry the default 5.

**Conclusion: no banked measurement in this program ran with
`protected_tool_results=0`.** The default is 5 and nothing overrode it, so every
banked compaction figure — the 25.6–33.0% waste band, the cadence arms, T0/T1,
three-knob — is unaffected by this defect. Nothing needs re-running on account of it.

**One honest caveat on scope:** this searched the two program roots
(`openai-evals-team-ci`, `hw-model-performance`) exhaustively, plus
`~/.amplifier/settings.yaml` and the resolved cache bundles. It did **not**
exhaustively scan the 653 GB `~/.amplifier/projects` session archive — that is
personal session history, not banked program measurement, and line (a) covers it
anyway: with no config block anywhere, no session could have received a non-default
value.

**Related, and NOT the bug — flagged so nobody trips over it:**
`__init__.py:2083` reads `stats.get("protected_tool_results", 0)`, a `0` default in
the *notice-rendering* path. It is a display fallback for a key the stats dict always
carries, not a configuration path, so it cannot produce this defect. Left alone
deliberately; changing it would be scope creep in a bug-fix PR. (Note the sibling at
`:2131` defaults the same lookup to `5` — the two disagree. Cosmetic, not
behavioral.)

---

## 6. Deliverable ledger

| Deliverable | Status |
|---|---|
| Bug verified in current `origin/main` code, with corrected `file:line` | **DONE** — `__init__.py:1243/1346/1413` (item's 1407/1510/1577 had moved) |
| Draft PR on origin, branch `lane/x7p-protected-tool-results-bug` | **DONE** |
| Fix | **DONE** — single `_protected_tool_indices()` helper, 3 call sites |
| Regression test failing before / passing after | **DONE** — 11 tests; fail-before proved side-by-side against `git show HEAD:` |
| Fail-before evidence recorded verbatim in the PR body | **DONE** — §3 |
| Byte-identity: every other config value unchanged | **DONE** — 57 cells, only the 12 `ptr=0` cells differ |
| Contamination answer | **DONE** — **NO**, four independent lines of evidence, §5 |
| Full suite green | **DONE** — 114 passed (103 pre-existing + 11 new) |
| DONE-NOTE.md in PR body | **DONE** — this document |
| $0 spend | **DONE** — $0.00, no API/DTU spend, no infrastructure created |

**Not merged. Not pushed to main.** Branch pushed and PR opened as **draft** only.

## 7. What remains open

Nothing blocking. Two observations handed on rather than acted on:

1. `stats.get("protected_tool_results", 0)` at `:2083` vs `stats.get(..., 5)` at
   `:2131` — the two fallbacks disagree. Cosmetic (the key is always present);
   deliberately out of scope here.
2. `SimpleContextManager` validates none of its numeric knobs. `protected_tool_results`
   is now well-defined at 0 and negative, but e.g. `target_usage=5.0` or
   `truncate_chars=-1` remain undefined. Worth one item if anyone wants the
   constructor to fail loud; not filed, because it is a design call, not a defect.
