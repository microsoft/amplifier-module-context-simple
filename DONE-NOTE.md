<!--
DONE-NOTE.md is a SHARED, APPEND-ONLY file. Several parallel lanes each land a
note here, so an add/add conflict at merge time is expected and is resolved by
KEEPING BOTH notes, newest appended at the bottom, never by replacing one with
the other. Each note keeps its own `# DONE-NOTE - <item>` heading verbatim.
-->

# Lane notes index

| item | subject |
|---|---|
| `model_performance-x7p` | `protected_tool_results=0` protected ALL tool results (negative-slice bug) |
| `model_performance-x1r` | tool-result budget (token-denominated, head+tail, per-tool) + spill-to-disk |
| `model_performance-2o9` | `clear_at_least` — a worth-the-rebuild predicate in front of compaction (+ summary shrink guard) |

---

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

---

# DONE-NOTE — W3-3 / `model_performance-x1r`

**Fix the tool-result budget in context-simple** (250 chars head-only → token
budget, head+tail, per-tool), then spill the truncated middle to disk.

| | |
|---|---|
| Repo | `amplifier-module-context-simple` |
| Branch | `lane/x1r-tool-result-budget` (forked from `c6dfbba`, `main`) |
| Draft PR | microsoft/amplifier-module-context-simple#21 (DRAFT — do not merge) |
| Tests | **151 green** (103 pre-existing, unchanged + 48 new) |
| Spend | **$0.00** — no API calls, no DTU, no containers, no infrastructure created |
| Default behavior | **byte-identical**, proven by external stash-compare (below) |

---

## 1. Step 0 — the free measurement that decides how much this is worth

The spec's own instruction: *"If our tool-result share is 15%, phase B is not
worth building and this spec should stop at phase A."* The magnitude case rested
on a practitioner's worked example from somebody else's workload (5 tool results
= 81% of a context). So it got measured on ours first.

Script: `.amplifier/evaluation/treatment-validation/20260902-x1r/step0_tool_result_share.py`
Output: `.../20260902-x1r/step0-results.json`

Source: every `transcript.jsonl` under two existing capture roots. Counted in
**characters** — the unit this module's estimator actually works in
(`len(str(msg)) // 4`).

| | `20260902-t0t1` | `20260901-cadence` |
|---|---|---|
| sessions / tool results | 5 / 750 | 12 / 1,729 |
| **tool-result share of transcript chars** | **46.4%** | **47.3%** |
| p50 / p90 / p99 / max (chars) | 412 / 7,904 / 31,751 / 52,319 | 467 / 7,904 / 31,744 / 43,720 |
| over today's 250-char budget | 488 (65%) | 1,096 (63%) |
| chars discarded by today's budget | 1,524,013 (**91%** of tool-result content) | 3,082,300 (**90%**) |
| over 50,000 bytes | **1** | **0** |

**(knob moved: none — observation) · (model family: n/a, corpus measurement) ·
(confidence: measured, n=17 sessions / 2,479 tool results) · (evidence:
`.../20260902-x1r/step0-results.json`)**

Two conclusions:

1. **~47% ≫ 20%. Phase B is justified**, which is why this lane shipped spill and
   not just the budget fix.
2. **deepseek's shipped 50,000-byte spill threshold would fire once in 2,479
   results on our workload.** The mass is in the 400–32,000 char band. The
   mechanism is worth copying; that constant is not. This contradicts the
   reference implementation the spill design otherwise follows, and it is the
   single most transferable number in this note.

Caveats, stated: a transcript is the **full history**, not the compacted wire
view, so this is the share of what compaction *sees* — an upper bound on the wire
share in a run that compacts. Token figures are the module's own estimator, not
provider-billed tokens. `p90 = 7,904` is identical in both roots, which suggests a
common fixed-size output rather than a coincidence; not investigated.

---

## 2. What shipped

Five flags, **all inert by default**:

| flag | default | effect |
|---|---|---|
| `tool_result_budget_tokens` | `None` | Token-denominated budget. `None` = legacy `truncate_chars` path. |
| `tool_result_shape` | `"head"` | `"head_tail"` splits the budget and keeps both ends with `...[N chars omitted]...`. |
| `tool_result_budget_by_tool` | `{}` | Per-tool token budgets by tool name; beats the global budget. |
| `tool_result_exempt_tools` | `[]` | Never truncated (skill-type outputs). |
| `tool_result_spill_dir` | `None` | Full original written to a content-addressed file; pointer in the replacement text. |

Precedence per result: **per-tool → global → legacy `truncate_chars`**.

Roughly 300 LOC of module change against the spec's ~80 (phase A) + ~180
(phase B) estimate — the overage is comment density and per-knob validation, not
extra mechanism.

### Defaults rationale

- **`tool_result_budget_tokens` defaults to `None`, not `62`.** The spec says 62
  (= today's 250 chars). But `truncate_chars` is an existing shipped knob:
  someone running `truncate_chars: 500` today would have been silently reset to
  248 chars by a hard 62 default. `None` means "legacy path, whatever
  `truncate_chars` says", which is byte-identical for **every** existing
  configuration rather than only the default one. This is a deliberate deviation
  from the spec, in the safer direction.
- **Recommended values are documented, not shipped.** The README's starter config
  (2,000 tokens global, `head_tail`, per-tool map, `load_skill` exempt) is
  conservative relative to codex's 10,000. It is *not* a default, because nothing
  here has been evaluated against a model yet.
- **Per-tool numbers are transcribed, not measured.** They come from a
  specification that publishes zero measurements anywhere, rescaled to the size
  distribution in §1. The README says so at the point of use.
- **Tokens, not chars**, because chars/token constants are tokenizer-version
  specific and drift (published up to 1.35×, observed up to 1.47× on technical
  content) — a char budget silently changes meaning across a model version. The
  conversion constant is `4`, deliberately the *same* constant the module's own
  estimator uses, so budget and accounting cannot drift apart.
- **`head_tail` keeps the marker, not just the tail.** A model must never reason
  from a truncated result without knowing it is truncated.

### The one design constraint that shaped everything

`_apply_sticky_decisions` re-derives the replacement text for **every**
sticky-truncated message on **every** request. So the replacement must be a pure
function of content + config.

The consequence for spill: **the pointer is emitted whether or not the write
succeeded.** If the pointer tracked write success, one transient disk error would
change the bytes of an already-sent message, and under a grow-only prompt cache
every prefix mutation is a full cold rebuild. A dangling pointer is visible and
recoverable. A silently mutated prefix is neither. Write failures log a warning;
`tests/test_tool_result_budget.py::test_spill_write_failure_still_emits_stable_pointer`
pins it.

Spill paths are content-addressed (`tool-result-<seq>-<sha256[:16]>.txt`), so
writes are idempotent across repeated requests and across a resumed session.
Write-then-rename, so a reader never sees a half-written file.

---

## 3. Evidence

### 3.1 Byte-identity — the defaults-are-a-no-op claim

**Method (external oracle).** `.../20260902-x1r/byte_identity_harness.py` imports
whatever `amplifier_module_context_simple` is on `sys.path`, so neither side
defines its own baseline. 8 scenarios: light / heavy / aggressive pressure;
`truncate_chars` ∈ {10, 60, 250, 5000}; notice on and off; a view every third
turn **plus two consecutive views on turn 5** (the repeated call is what would
expose non-idempotent re-derivation). Canonical JSON of every returned message.
Only `metadata.timestamp` is normalized.

```
pre-change  (c6dfbba, 2,648 lines, flags absent)   sha256 76ee9d3f…3d6a467f
post-change (lane/x1r-tool-result-budget)          sha256 76ee9d3f…3d6a467f
3,398,618 bytes                                    IDENTICAL — PASS
```

**Non-vacuity:** the dump contains 76 `[truncated:` occurrences — the path is
really exercised. **Negative control:** the same harness with the treatment forced
on yields `4e59f380…` — a different hash, so the harness can see changes when
there are any.

Artifacts: `byte-identity-PRE.json`, `byte-identity-POST.json`,
`byte-identity-TREATMENT.json`, `.log` files alongside.

The unit suite pins the same claim from the opposite direction:
`test_default_config_replacement_text_is_byte_identical` asserts the exact legacy
string against an oracle **transcribed from the pre-change source**, not computed
by the new code, plus a second literal spelling so re-baselining the oracle alone
cannot silently pass.

### 3.2 Tail retention — mechanism demonstration, no model, no spend

`.../20260902-x1r/tail_retention_demo.py` → `tail-retention-demo.json`.
Four synthetic workloads (`pytest`, `grep`, `git log`, build output) whose answer
sits in the **last line** — the tool list enumerated *before* the run, as the gate
requires.

| arm | truncated results | tail present | rate |
|---|---|---|---|
| control (shipped defaults, 250 chars, head) | 27 | 0 | **0%** |
| **budget-neutral** (62 tok = 248 chars, `head_tail`) | 28 | 28 | **100%** |
| 4× budget (250 tok = 1,000 chars, `head_tail`) | 1 | 1 | 100% |

**(knob moved: `tool_result_shape`) · (model family: none — mechanical, no LLM) ·
(confidence: measured, n=4 workloads × 30 turns per arm) · (evidence:
`.../20260902-x1r/tail-retention-demo.json`)**

Row 2 is the clean A/B: **same bytes kept, different shape**, identical sticky
level per tool (4/2/3/3 in both arms). 0% → 100% tail retention at no budget cost.

**Honest negative in row 3, and it is load-bearing.** Raising the per-result
budget *without* raising `target_usage`/`max_tokens` **trades truncation for
removal**: a bigger budget sheds fewer tokens per truncation, so the ladder
escalates past the truncation rungs into message removal — strictly more lossy.
Three of four workloads went from 11–15 truncated results to **zero, removed
instead**. Anyone tuning this must raise budget and target together, or measure
what they actually got. This is the first thing the follow-up eval should
control for.

This is a mechanism demonstration, **not** the eval. No model was involved, so it
says nothing about whether an agent *uses* the tail.

### 3.3 Test suite

151 green. The 48 new tests cover: byte identity (literal + end-to-end + custom
`truncate_chars`); token budget arithmetic; head/tail split, exact omission
count, and the `content[-0:]`-returns-everything trap; per-tool resolution via
all three sources (harvested `tool_call_id`, `name` field, `metadata.tool_name`)
and both tool-call shapes (OpenAI `function`, Anthropic-ish); exemption including
"never counted as truncated"; spill write / idempotence / content-addressing /
**write-failure byte-stability** / lazy directory creation; **tool-pair atomicity
with every knob enabled**; `_seq` prefix stability and 5× request idempotence
with `head_tail` + spill on; `set_messages` map rebuild; `clear()` reset; config
validation fallbacks for all five flags; and `mount()` plumbing both ways.

Twelve of them carry an explicit **"test must actually truncate/compact/spill"**
assertion, because a compaction test that escalates to level 8 removes every tool
result and then passes vacuously. That is exactly what the first draft of two of
these tests did.

---

## 4. Discovered, filed, not fixed

**`model_performance-x7p`** (filed `discovered-from` this item):
`protected_tool_results=0` protects **all** tool results, not none —
`tool_result_indices[-0:]` is the whole list (`__init__.py:1407`, `:1510`,
`:1577`). Compaction then skips every truncation rung and escalates straight to
removal.

**(confidence: measured** — reproduced in a scratch harness: with
`protected_tool_results=0`, 40 turns × 2,000-char results reached sticky level 8
with **0** truncations; `protected_tool_results=1` on the identical workload
truncated normally.**)**

Not fixed here: the default is 5, so no shipped configuration hits it, and
changing it *is* a behavior change for anyone currently setting 0. ~3 lines plus a
regression test. The new tests avoid the trap and say why in a comment.

---

## 5. Deviations from the spec, and why

1. **Default is `None`, not `62`** — see §2. Protects existing `truncate_chars`
   overrides.
2. **Spill lives in `context-simple`, not a new `tool-result-spill` module.** The
   item's own task text asks for "spill-to-disk for the truncated middle with a
   stable pointer line", which is a compaction-time operation on the truncated
   middle — it shares the head/tail code and the `_seq` machinery. **This forfeits
   the spec's class-A property**: because the content is already in the
   conversation, spilling still shrinks the request, so it is still a boundary and
   still a cold rebuild under a grow-only cache. It buys **recoverability**, not
   free context. A post-execute hook that intercepts results *before* they enter
   the conversation is the class-A design and remains unbuilt. Recorded plainly
   rather than claimed.
3. **No cleanup sweep.** deepseek ships a 30-day startup sweep with symlink and
   ownership guards. Nothing here deletes anything, ever — deliberately, since a
   resumed or forked session may still hold an older pointer. The caller owns the
   directory lifecycle; README says so and recommends a session-scoped dir.
4. **No `read`-result exemption by default.** deepseek exempts reads to prevent
   `read → spill → read`. That loop cannot form here (spill happens at compaction
   time, not tool-execute time). `tool_result_exempt_tools` is the seam if this
   ever moves to a post-execute hook.
5. **No model-settable per-call budget.** codex lets the model raise its own limit
   to 262,144 per call. That needs a tool-schema surface this module does not own.
6. **No line-based cap.** The spec's "chars before lines" concern is satisfied
   structurally: the gate is a pure char count and no line cap exists in this
   path, so the "2 lines × 10MB" failure mode is impossible rather than merely
   unlikely. Zero LOC.
7. **No eval.** Explicitly out of scope for this lane ($0 authority). Spec below.

---

## 6. Follow-up eval spec (the item this lane does NOT do)

**Prerequisite that must be settled first:** the budget↔removal interaction in
§3.2. An arm that raises the budget while holding `target_usage` fixed is not
measuring "bigger budget", it is measuring "truncation replaced by removal". Fix
the design before spending.

**Arms** (n≥3 each, one variable at a time, per the program's own rule):

| arm | config |
|---|---|
| `control` | shipped defaults (byte-identical to today) |
| `shape_only` | `tool_result_budget_tokens: 62`, `tool_result_shape: "head_tail"` — **budget-neutral**, isolates shape |
| `budget_only` | `tool_result_budget_tokens: 2000`, shape `"head"`, `target_usage` raised to hold boundary count constant |
| `per_tool` | `budget_only` + `tool_result_budget_by_tool` + `tool_result_exempt_tools: ["load_skill"]` |
| `spill` | `per_tool` + `tool_result_spill_dir` |

**Pre-register before spending** (a gate written after seeing the result is not a
gate; check each for vacuity):

- **G-TRB-TAIL** — on a tool list enumerated **before** the run (pytest, grep,
  git log, build output), the tail is present in the model-visible content of
  100% of truncated results; control 0%. *Already demonstrated mechanically
  (§3.2); the eval's job is whether the agent* uses *it.*
- **G-TRB-RECALLS** — redundant re-calls of the same tool with identical
  arguments fall vs control. **The real quality signal**, and the one that can
  fail honestly.
- **G-TRB-COST** — cost does not rise. Watch the boundary count specifically:
  §3.2 says a larger budget pushes work from truncation into removal, and prior
  work established that boundary count, not the mechanism, is what moves cost
  (a comparable feature measured +84% boundaries → +83% cost).
- **G-SPILL-APPEND** — no request contains a tool result a previous request
  contained in longer form; `ID_ONLY = 0`.
- **G-SPILL-TOKENS** — tool-result input tokens fall by ≥ §1's predicted share,
  ±20%. §1 is measured *before* the treatment, so it cannot be fitted afterwards.
- **G-SPILL-BOUNDARIES** — boundary count does not rise.
- **G-SPILL-RECOVERY** — **record as VACUOUS on S5-CRAC** and defer. S5 never
  needs to recover anything (40/40 constraints, 20/20 post-compaction in every
  run of every arm across six probes). It needs the discriminating scenario from
  `model_performance-cb2`.
- **G-ANTH-GUARDRAIL** — required for any treatment lane: Anthropic re-warm ≤1
  request on raw wire fields, byte-stable system prompt, zero `ID_ONLY`.
- **S3 quality** — non-regression observation only. Six cells already sit at S3
  median 100 and dial-to-dial differences may be inside grader noise.

**Cheapest first experiment:** `shape_only` vs `control`. One variable, no budget
change, therefore no boundary-count confound, and §3.2 already says the mechanism
fires. If G-TRB-RECALLS does not move there, it will not move anywhere.

---

## 7. Infrastructure and spend

**$0.00.** No API calls, no DTU, no Gitea, no containers. Nothing registered in
the infra ledger because nothing was created; nothing to tear down. All evidence
is local CPU work over existing capture roots.

Artifacts written (outside the module repo, under the authorized capture path
`.amplifier/evaluation/treatment-validation/20260902-x1r/`):
`step0_tool_result_share.py`, `step0-results.json`, `byte_identity_harness.py`,
`byte-identity-{PRE,POST,TREATMENT}.json` + `.log`, `tail_retention_demo.py`,
`tail-retention-demo.json`.

No PII, no team-internal data, no individual attribution in any output. Spill
paths are the only new content class in a transcript; they are caller-configured
and contain no absolute path this module invents.


---

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
