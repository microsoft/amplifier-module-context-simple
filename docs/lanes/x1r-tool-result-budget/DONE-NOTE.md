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


