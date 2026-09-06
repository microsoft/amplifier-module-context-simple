# Lane `fwut-compaction-storm` — DONE-NOTE

**Item:** `model_performance-fwut` — "Compaction storm: a session with message_count=4 recorded 969 compaction events"
**Repo:** `microsoft/amplifier-module-context-simple`, branch `lane/fwut-compaction-storm`
**Run date:** 2026-09-05 · **Outcome: A — RESOLVED**, all deliverables DONE, none NOT-POSSIBLE.
**Spend: $0.00 against a $0.00 authority.** No API calls, no containers, no DTUs, no evals, no
infrastructure registered. Every number below came from read-only Cypher/blob queries against the
team-shared context-intelligence graph (free) and `grep` over already-captured local files.

---

## 1. THE ANSWER — mechanism **(a)**, in its general form

> **(a)** the token estimate for a tiny message set exceeding budget — a single huge tool result or
> system prompt?

Yes, with the source named precisely: **a single structurally-protected message that compaction is
forbidden to touch is, on its own, at or above `compact_threshold × budget`. That makes
`_exceeds_threshold` permanently true, so the module re-runs the entire escalation ladder to level 8
on every single request, achieves zero further reduction, and emits a `context:compaction` event and
a compaction notice each time.**

**(b), (c) and (d) are ruled out by the payloads**, not by argument — see §4.

### The per-event payload evidence

All 969 `context:compaction` events for
`0000000000000000-445ac89c107c4f52_anchors-amp-dev-architect` (claude-sonnet-5, samueljklee,
2026-09-04T23:23:42Z → 2026-09-05T02:50:05Z), pulled whole and aggregated locally
(`evidence/445ac89c-compaction-events.tsv`):

| field | value |
|---|---|
| events | **969** across **988 `llm:request`s** (98.1%) |
| `strategy_level` | **8 on 969/969** — the maximum, on the very first event |
| `budget` | **131,104 on 969/969** (never varies) |
| `target_tokens` | **65,552 on 969/969** |
| `after_tokens` **> target** | **969/969 (100%)** |
| `after_tokens` **> budget** | **968/969 (99.9%)** |
| `after_tokens` min / mean / max | 130,627 / **142,003** / 188,265 |
| `after_messages` min / max | 2 / 12 |
| `before_messages` min / max | 3 / **5,174** (monotonically increasing, never resets) |
| `before_tokens` max | 13,019,053 |
| `user_messages_stubbed` | **0 on 969/969** |
| `messages_truncated` (total, all events) | 114 |

Read the first event on its own and the whole thing is already visible:

```
before_messages=3  after_messages=2
before_tokens=131,321  after_tokens=130,627
budget=131,104  target_tokens=65,552  strategy_level=8  messages_removed=1
```

Three messages. 131,321 estimated tokens. The ladder runs to its last level, removes one message,
frees **694 tokens**, and stops 65,075 tokens above its own target. The second event is worse — it
finishes at 144,627 against a 131,104 budget, i.e. **compaction completed and the request was still
over budget**, which is where it stayed for the remaining 967 events.

### What the irreducible floor actually is

Fetched the raw request body (`ci-blob://…__llm_request__…__raw`, 580,815 bytes) and measured only
the per-message sizes — never loaded the payload into context:

```
model claude-sonnet-5   max_tokens 128000   system 25,446 chars   28 tools / 42,942 chars
messages: 4
  [0] role=user       content=list[text:516,153]  516,184 chars   est ~124,338 tok
  [1] role=assistant  content=list[thinking, tool_use×4]  2,663 chars   est ~665 tok
  [2] role=user       content=list[tool_result×4]         3,058 chars   est ~722 tok
  [3] role=user       content=str (the compaction notice)   917 chars   est ~225 tok
```

**Message [0] is 516,184 chars ≈ 124,338 estimated tokens — 94.8% of the entire 131,104 budget, and
190% of the 65,552 target.** It is the delegation payload of a forked sub-agent
(`session:fork`, `agent_name: anchors-amp-dev:architect`, parent `7b31dcdc-…` — Samuel's own
investigation session): inherited `<context_file …>` blocks followed by the actual instruction.

The arithmetic closes exactly:

```
est(message[0]) 124,338  +  est(system) 6,289  =  130,627  ==  after_tokens of event 1
```

And it never changes: **md5 of message[0] is `76fcb100389c8abf91d25178fb6f91b0` at the first request
and byte-identical at the last one, 988 requests and 3h26m later.**

### Why nothing can reduce it — three protection rules, all firing

| rule | where | effect here |
|---|---|---|
| system messages are NEVER compacted | `_compact_ephemeral` extracts them up front and re-prepends them | fixed floor |
| user messages are never *removed*, only stubbed | `_remove_messages_with_protection`: "Removal candidates exclude ALL user messages" | msg[0] cannot be dropped |
| Level 8 stubs the first user message **"but NEVER if it's also the last"** — `first_user_idx != last_user_idx` | `__init__.py` Level 8 | a sub-agent has exactly **one** user message, so first == last → **skipped** |
| Level 8's stub also requires `isinstance(content, str)` (both inline and inside `_stub_user_message`) | `__init__.py` | msg[0]'s content is a **list of blocks** → **skipped** even if it were not the last |

Two independent guards, either one sufficient. The observable consequence is
`user_messages_stubbed == 0` on all 969 events: **the one lever that could have shrunk the floor
never fired once.**

`_finalize_compaction_with_stats` already *diagnoses* this — it logs a warning naming the
un-reducible floor — but only when the result is over **budget**, and it does not stop the loop.

---

## 2. The `187→8 / 192→7 / 196→6` sequence — **both halves**

The hunt read this as a cycle ("compact down to ~7, context regrows to ~190, compact again"). **It is
not a cycle, and the "regrowth" never happened.** Both halves, from the event stream:

**Why it compacts to ~7 messages.** The ladder runs to level 8 every request and strips everything it
is permitted to strip. What survives is only the protected residue: the system message(s), the sole
(irreducible) user message, and the protected-recent tail — the last assistant turn plus up to
`protected_tool_results` (5) tool results. That is 5–9 messages. Across all 969 events
`after_messages` never leaves the 2–12 band.

**Why ~190 messages are there again on the next request.** *They never left.* Compaction in this
module is **ephemeral by construction** — `_compact_ephemeral`'s own docstring: *"This returns a NEW
list - the source messages are NEVER modified"*, and the notice the model reads says *"This
compaction is ephemeral (affects only this request). Full history is preserved in session
transcript."* `self.messages` is only ever appended to. The per-request compacted view is rebuilt
from scratch, from the full stored history, every single time.

So `before_messages` is monotone across the whole session: **3, 6, 11, 17, 24, 29, 33, 37, 42, 46,
50, 54 … 5,108, 5,113, 5,123, 5,156, 5,159, 5,162, 5,170, 5,174.** It never once decreases in 969
events. `187 → 192 → 196` is simply **+5 then +4** — one assistant message plus its tool results,
the normal growth of one agent turn. The three notices differ because a *fresh* compaction genuinely
ran each turn, exactly as the hunt observed; what did not happen is any regrowth, because nothing had
shrunk.

The correct one-line shape is therefore: **monotone history growth × per-request ephemeral
compaction against a floor it can never get under.**

---

## 3. This is not one session — **74.6% of the corpus, concentrated in forked sub-agents**

Server-side aggregate over the hunt's exact population (all sessions with ≥3 `context:compaction`
events since 2026-08-01 — 431 sessions, reproducing the hunt's count exactly;
`evidence/corpus-431-sessions-level8.tsv`):

- **33,541 compaction events, of which 25,307 (75.5%) are at `strategy_level 8`.**
- Defining a **storm session** as ≥90% of its events at max level: **94 of 431 sessions (21.8%),
  carrying 25,023 compactions = 74.6% of every compaction in the corpus.**

Split by session kind (`0000000000000000-…_<agent>` = forked sub-agent; bare UUID = root session):

| | root sessions | forked sub-agents |
|---|---:|---:|
| sessions | 160 | 271 |
| compaction events | 6,650 | 26,891 |
| at `strategy_level 8` | 196 (**2.9%**) | 25,111 (**93.4%**) |
| storm sessions | 6 (3.8%) | 88 (**32.5%**) |
| compactions inside storm sessions | 27 (0.4%) | 24,996 (**93.0%**) |

**Compaction is healthy in root sessions and pathological in forked sub-agent sessions — a 32×
difference in max-level rate.** It spans at least 14 distinct agent types across five bundles:

```
10,761 compactions / 33 sessions  foundation-git-ops
 4,505 /  2  anchors-amp-dev-architect       2,654 /  1  superpowers-spec-reviewer
 2,495 / 12  foundation-explorer             1,527 /  4  amplifier-amplifier-expert
   648 /  4  foundation-post-task-cleanup      615 /  1  browser-tester-browser-operator
   498 /  3  foundation-bug-hunter             447 /  1  python-dev-code-intel
   302 /  4  superpowers-implementer           154 /  4  foundation-modular-builder
   148 /  2  foundation-file-ops                96 /  1  superpowers-code-quality-reviewer
```

The single session named in the item (969 events) is **3.9% of the phenomenon**. The largest is
`ad49ee295c254ce5_anchors-amp-dev-architect` at **3,536** events, 100% at level 8.

### The floor comes in two flavours — verified on both arms' outliers

The opus-5 outlier the hunt names as supplying **83% of all opus-5 duplicates**,
`0000000000000000-877774a55cd1430c_foundation-explorer`, is the **same defect**
(`evidence/877774a5-compaction-events.tsv`):

```
2,165 events · strategy_level 8 on 2,165/2,165 · after_tokens > target on 2,165/2,165
user_messages_stubbed 0 on all · budget 131,104 / target 65,552
after_messages max 6 · before_messages 7 → 5,131 (monotone)
```

But its raw request shows a different floor: **`system` is 402,949 chars ≈ 100,737 estimated
tokens** (154% of target) while its user message is a trivial 2,123 chars. Also a `session:fork`
(`foundation:explorer`, claude-opus-5).

So: **sonnet's floor is the sole user message; opus's floor is the system prompt.** Same class-(a)
defect, same terminal behaviour, two entry points — which is why the fail-before test file covers
both shapes.

---

## 4. Why not (b), (c), or (d)

**(b) — budget derived from a provider reporting a tiny/zero window: RULED OUT, arithmetically.**
`budget = 131,104` on every one of the 969 events. That reconciles exactly to a *full, healthy*
provider report through `_calculate_budget` + the notice reserve:

```
context_window 200,000
  − int(max_output_tokens 128,000 × output_reserve_fraction 0.5) = 64,000
  − safety_margin 4,096
  = 131,904
  − compaction_notice_token_reserve 800
  = 131,104   ✓ exact
```

and `target_tokens = 131,104 × target_usage 0.50 = 65,552` ✓. Nothing tiny, nothing zero. The hunt's
own *healthy* comparison session (`cdab6c369ccf4476_anchors-amp-dev-builder`) carries the identical
`target: 65,552` and compacted **442,307 → 64,887** successfully. Same budget, working fine.
*(Side observation, not this bug: reserving 50% of a 128,000 output cap spends 64,000 tokens — 32% of
the window — before any conversation exists. Worth a separate look; it is not what storms here.)*

**(c) — sticky level pinned high with `needs_escalation` re-firing: RULED OUT as the cause, though
the symptom is real.** `strategy_level` is 8 on the *first* event, when there were only 3 messages —
it did not climb and stick, it started at the top because the floor was already above budget on
request one. Stickiness is downstream: `_sticky_level` is a monotone high-water mark, and
`needs_escalation = _exceeds_threshold(current_tokens, budget)` stays true because the *floor* stays
above threshold. Fix the floor and the stickiness is harmless; freeze the level and the storm
continues. Cause vs. consequence.

**(d) — `ContextCompactionEvent` over-emitting: RULED OUT.** The 969 events are 969 *distinct*
compactions: `before_messages` is strictly increasing across them (3 → 5,174), `before_tokens` grows
131,321 → 13,019,053, and `after_tokens` varies event to event. Duplicates would repeat. There are
969 events against 988 `llm:request`s — under, not over.

**On the emitter's two-directional unreliability** (the tension the item flags): both hunt findings
survive, and they are now explained by the *same* mechanism rather than being in tension.
`_finalize_compaction_with_stats` emits **exactly once per genuine escalation**. The undercount in
Part 3 finding 1 (`ea8afed3…`: notice stats prove a compaction ran, graph recorded 7) is the
*intended* path working — `_compact_ephemeral` returns early via the "sticky state alone is
sufficient — nothing NEW to decide" branch without emitting, because the residue *did* fall back
under threshold. The storm is that same branch being **unreachable**: with an irreducible floor,
`_exceeds_threshold` never goes false, so every call is scored as a new escalation and emits. **One
gate explains both the undercount and the storm.** The event count remains a lower bound on real
compaction *applications*, and an exact count of *escalations*.

---

## 5. Our own captures — **zero** at the item's threshold, three at the sharper one

Scanned **3,117** `events.jsonl` / `root_events.jsonl` files under
`/home/bkrabach/dev/openai-evals-team-ci/.amplifier/evaluation/` (28 GB) with `grep` only — no file
parsed whole, nothing large loaded into context (`evidence/scan_own_captures.sh`).

- 101 files (60 distinct sessions) contain any compaction; **736 compaction events total.**
- **Sessions with a compactions / message_count ratio > 5: `0`.** Max ratio observed **1.48**; the
  most compactions in any one of our sessions is **31**. For scale, the team-shared session is
  969 / 4 ≈ **242**.

**So on the item's stated metric the answer is plainly zero, and the blast radius is bounded: the
runaway storm does not occur in our evaluation corpus.**

Applying the sharper mechanism test rather than the ratio (`strategy_level == 8` **and**
`after_tokens > target_tokens`, ≥90% of ≥3 events) finds **3 distinct sessions, 66 events** — and
they are not a surprise: **all three are the `cad-deep` arm of `20260901-cadence`**, already
documented in `ai-notes/00-what-we-know.md` §2(b) as *"degenerate and disclosed as such: 6,750-token
target is below the ~32k system floor, so compaction escalated to max level every request"*.

That is the same defect. We reproduced it ourselves in a controlled arm two months ago, diagnosed it
correctly, and filed it as a knob-misconfiguration artifact of `target_usage 0.15` rather than as a
general class of failure. **`cad-deep` is the storm induced from the target side; the sub-agent
sessions are the storm induced from the floor side.** Worth recording in §2(b) that its stated cause
generalises.

---

## 6. Does this change a PR #33 conclusion? **Yes — and it moves the verdict, in the direction the PR author wants**

The hunt's primary criterion missed by 0.0015 (sonnet 0.2451 ÷ opus 0.1226 = **1.9985×** against a
2.0× bar) and returned INCONCLUSIVE. Its duplicate-rate metric is 76% driven by the sonnet storm
session and 83% by the opus one — **and both are now proven to be the same non-PR#33 pathology**,
by the same criterion, from event payloads rather than from the outcome metric.

Sensitivity, on the hunt's own published figures (`evidence/pr33_recompute.py`):

| cut | sonnet | opus | ratio | pre-registered verdict |
|---|---:|---:|---:|---|
| as published (no exclusion) | 0.2451 | 0.1226 | **2.00×** | INCONCLUSIVE |
| exclude **sonnet** storm only | 0.0652 | 0.1226 | 0.53× | NOT-REPRODUCED |
| exclude **opus** storm only | 0.2451 | 0.0240 | 10.22× | REPRODUCED |
| **exclude BOTH storms** (the only symmetric cut) | **0.0652** | **0.0240** | **2.72×** | **REPRODUCED** |

**The new number is 2.72×.** The hunt's own independent leave-top-3-sessions-out variant landed at
2.61× — mutual corroboration from a different exclusion rule.

Three things must travel with that number:

1. **The exclusion criterion is mechanical and arm-symmetric**, defined from `strategy_level` /
   `after_tokens` in the event payload, not from duplicate counts. It was fixed before the ratio was
   computed. Both arms' outliers meet it at 100%.
2. **It is nonetheless a post-hoc reanalysis.** The pre-registration governs the hunt's published
   verdict; this does not overturn it by fiat. It is evidence for `model_performance-81qj` to weigh.
3. **Direction matters enormously** — dropping only sonnet's storm gives 0.53×, below the
   NOT-REPRODUCED floor. Any reanalysis that removes one arm's outlier without applying the identical
   rule to the other is not a reanalysis.

The arithmetic is derived from the hunt's published, 4-dp-rounded rates, so it carries their rounding
(implied dup totals 2,895 sonnet / 2,730 opus reproduce the hunt's stated 76% / 83% shares as 75.6% /
82.3%). Re-deriving from raw turns would tighten it; it would not move the verdict boundary, which is
0.72 clear of the bar.

**And the deeper point for 81qj:** PR #33's *premise* is untouched and remains correct — a stale,
byte-identical, recency-marker-free notice is real and byte-verified. What changes is that the
loudest evidence cited *for* it was never an instance of it, and the corpus it was measured on has
**74.6% of its compaction events** generated by a different defect that PR #33 does not fix and that
this lane's tests now pin.

---

## 7. Deliverables

| deliverable | state |
|---|---|
| Mechanism named as exactly one of (a)–(d), payload evidence quoted | **DONE** — (a); §1, §4 |
| Explain `187→8 / 192→7 / 196→6`, both halves | **DONE** — §2 |
| Fail-before unit test against context-simple main → DRAFT PR | **DONE** — `tests/test_compaction_storm_irreducible_floor.py`; §8 |
| Scan our own captures for ratio > 5, report the count | **DONE** — **0**; §5 |
| State whether this changes a PR #33 conclusion, name the new number | **DONE** — yes, **2.72×**; §6 |
| Full suite green, pasted in PR body | **DONE** — §8 |
| DRAFT PR on origin, branch `lane/fwut-compaction-storm` | **DONE** — see `DONE.json` `publication` |
| This DONE-NOTE at the lane artifact root | **DONE** |

## 8. The test, and the suite

`tests/test_compaction_storm_irreducible_floor.py` — **test-only, no runtime behaviour changed.**

- 5 characterisation tests that **pass on main** and pin today's behaviour: the sole-user-message
  floor (sonnet shape), the system-prompt floor (opus shape), the `isinstance(content, str)` guard
  in isolation, the storm itself (10 requests → 10 compaction events, `after_tokens` pinned at the
  floor), and the notice being re-attached every request.
- 1 `xfail(strict=True)` carrying the **fail-before assertion** — the storm should not re-escalate on
  every request. Verified failing on main for the right reason:

```
AssertionError: compaction should recognise an irreducible floor and stop re-escalating;
                got 10 escalations for 10 requests
assert 10 <= 2
  where 10 = len([{'after_messages': 3, 'after_tokens': 9756, 'before_messages': 25, ...},
                  {'after_messages': 3, 'after_tokens': 9757, 'before_messages': 35, ...}, ...])
```

`after_tokens` pinned at 9,756/9,757 against a 10,000 budget (97.6%) while `before_messages` grows
25 → 35 — a faithful 13×-smaller replica of production's 130,627/131,104 (99.6%) with
`before_messages` 3 → 5,174.

`strict=True` is deliberate: it stays green while the defect exists and turns into a hard failure the
moment someone fixes it, which is the signal to delete the marker.

Suite, this branch:

```
$ uv run pytest -q
........................................................................ [ 69%]
...............................                                          [100%]
102 passed, 1 xfailed in 5.34s
```

Baseline (`git stash`, i.e. `f2dbde9` untouched): `97 passed in 5.11s`. Net **+5 passing, +1 strict
xfail, 0 regressions, 0 runtime lines changed.**

### Why no runtime fix is proposed here

The program's merge policy is wins-only, and the honest fix is a **behaviour** change to compaction
(recognise an irreducible floor; stop re-escalating; stop re-attaching a notice that promises a
reduction that did not happen). Its value is real but its risk is non-zero, and demonstrating a win
needs a measured run. **This lane's authority is $0.00** — correctly sized for a forensic task, and
it did fund the whole diagnosis — so proposing an unmeasured default change would be exactly the
"unproven default-off feature" that `e9ac159` already reverted once. The evidence, the reproduction
and the fail-before assertion are shipped; the fix decision, and the run that would price it, belong
to the owner.

**A priced ask, if the owner wants the fix landed with evidence:** the fix itself is unit-testable at
$0 (this file's xfail flips). Confirming it does not regress compaction quality wants one S5 A/B —
`2 runs × 2 arms × $2.58/run ÷ 0.667 valid = $15.48` at the cadence-probe rate in
`00-what-we-know.md` §2(b). That is a separate item, not this one.

## 9. Deviations, limits, honesty

- **The `graph_query` / `blob_read` tools are not configured in this session** (same as the hunt's).
  All graph access went through `_dashboard/_cq.sh` / `_blob.sh` against the team-shared
  `POST /cypher` and `GET /blobs/{sid}/{key}` with an Entra bearer. Free; no LLM calls.
- **§6's arithmetic is a sensitivity analysis on the hunt's published rounded rates**, not a
  re-derivation from raw turns. Stated as such, with the rounding reconciled (§6).
- **§6 is post-hoc.** The exclusion rule is mechanical, pre-specified and symmetric, but it was
  applied after the hunt's pre-registration. It is input to `81qj`, not a replacement verdict.
- The turn counts for the two storm sessions (972 / 2,163) are the hunt's; I did not re-derive
  whether all of them fall in the "later notice" cell. Compaction begins at turn ~1 in both, so the
  error is ≤2 turns per session and cannot move a 2.72× ratio.
- The first capture-scan pass had a shell bug (`grep -c` printing `0` while exiting 1 broke the
  zero-skip test). It was discarded and re-run clean from zero; the committed script is the corrected
  one and the reported 3,117-file count is from the clean run.
- **Scope kept.** No change to the compaction notice text and no touch to PR #33's subject
  (`drbf`'s territory). No file in the evals repo was modified (`81qj` is live there) — the
  cross-findings in §5 and §6 are reported here for the manager to route. Nothing outside this
  worktree was written. No merge. No infrastructure created, so nothing to tear down.
- **No PII.** Session ids and agent names only; no user file paths or personal data reproduced.

## 10. Evidence

| file | what |
|---|---|
| `evidence/445ac89c-compaction-events.tsv` | all 969 events, per-event scalars |
| `evidence/877774a5-compaction-events.tsv` | all 2,165 events of the opus-5 twin |
| `evidence/corpus-431-sessions-level8.tsv` | the hunt's full 431-session population, total vs level-8 counts |
| `evidence/own-captures-compaction-summary.tsv` | our captures, per session: events / over-target / level-8 / storm |
| `evidence/pr33_recompute.py` | §6's table, runnable, inputs cited inline |
| `evidence/scan_own_captures.sh` | the grep-only capture scan |
