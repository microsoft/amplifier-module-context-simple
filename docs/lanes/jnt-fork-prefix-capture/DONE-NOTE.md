# DONE-NOTE - model_performance-jnt

**`_capture_fork_prefix()` appends to an array that was never sent (11 of 20
forks measured).**

**Verdict: CORRUPTING, not cosmetic.** It is not dead code and it produces a
record. It silently substitutes a message array that never went on the wire
for one that did, on a path whose entire purpose is byte-parity with the wire.
Fixed. **No 6da measured number is invalidated** — see §3.

## 1. The array, named, at file:line

Everything below is `amplifier_module_context_simple/__init__.py` at the
pre-fix commit `a877b36`.

| | |
|---|---|
| **the array** | **`self._last_request_view`** |
| created | **:1015** (`__init__`), written **:1778** inside `_finalize_view()` |
| what it holds | this module's own last RETURNED view, pre-strip |
| why it is "never sent" | it is written on **every view served**, and a view served is not a request sent |
| consumed by | **:4187** `_capture_fork_prefix()`, as the fallback source |
| flows to | `_maybe_trigger_summary_compaction` **:4040** → `_run_summary_compaction_task` → `_build_fork_request` **:4422** → the provider |

The selection at **:4187–4198** preferred the caller's recorded wire array
(`_sent_messages`, **:1017/:1210**) only while
`_sent_serial == _view_serial` (**:4189**), and substituted
`_last_request_view` whenever that equality failed (**:4187**, **:4192–4197**
— `logger.debug`, not warning).

**Why the equality fails in production.** `_view_serial` counts **views
served**, not **requests sent**, and the two are not 1:1. The real
orchestrator serves the view more than once per sent request:
`amplifier-module-loop-streaming/__init__.py` calls
`context.get_messages_for_request()` at **:3215** and then re-fetches at
**:3329** and **:3453** after persisting an ephemeral injection — up to three
views for one `ChatRequest`. The summary trigger is evaluated inside *every*
one of them (**:1364**, which runs *before* that call's `_finalize_view` at
**:1480/:1482**). So:

- trigger on the **first** view of a request → serials match → the wire array
  is used → **correct fork** (wire offset 0 or 1);
- trigger on a **re-fetched** view → serials differ by 1–2 → the wire array is
  discarded in favour of `_last_request_view`, which at that instant holds
  **the view the re-fetch just superseded** — built, thrown away, never sent.

That is the 9-vs-11 split 6da measured, and it reproduces exactly.

## 2. Consequence — unambiguous

**Corrupting to fork-mode behaviour, and self-concealing.**

1. **Guaranteed cache miss.** A superseded view is not a prefix any provider
   holds. Fork mode's only justification is appending onto a cached prefix; a
   fork that misses pays full price for the whole conversation, which is
   *strictly worse* than the standalone call it replaces.
2. **Silent.** `last_summary_call_stats["mode_used"]` reported `"fork"` for
   all 20 calls. The substitution was `logger.debug`. There was no field
   distinguishing the two sources — this is precisely why 6da had to
   reconstruct the distinction from the provider's request log.
3. **Backwards under uncertainty.** The check traded the array with *positive
   evidence* of having been sent (the caller said so) for one with *none*
   (fate unknown to this module), and did so exactly when uncertainty was
   highest.

Not affected: history, `_seq` allocation, span selection, tool-pair
integrity, the served view, or any default-mode behaviour. The blast radius
is fork mode's cache economics and the honesty of its self-report.

## 3. Does this invalidate any of 6da's measured numbers? **NO.**

Stated plainly for the manager, because the item asked for it loudly:

- **G-FORK-PREFIX (2/7/11 offset distribution, 45% aligned) — VALID, and is
  the direct measurement of this bug.** Scored from the wire, not from the
  module's self-report.
- **G-FORK-CACHED, G-FORK-NOBOUNDARY, the Anthropic guardrail, quality
  parity, and the −0.8% run-cost delta — VALID.** All are wire/usage-derived
  and none depend on `_capture_fork_prefix()` having chosen correctly.
- **The §7 correction (summarizer share ≈30%, not 2.4%/8.3–10.9%) — VALID
  and untouched.** Independent of the fork path.

**One caveat, in 6da's favour, not against it:** the −0.8% run-cost delta was
measured with only ~45% of forks byte-aligned. It is a **lower bound** on
what a correctly-aligned fork arm would deliver, not an upper bound. 6da's
"the mechanism works and the lever does not pay / DON'T-SHIP as-is" verdict
therefore **still stands as written**, but its cost figure is now known to
have been measured on a partially-broken treatment and should be **re-measured
before the DON'T-SHIP call is made final**. 6da itself flagged this
("one of them has a cheap fix worth a follow-up item"); this is that fix.

## 4. The fix (minimal)

`_capture_fork_prefix()` (**:4176**) no longer substitutes:

- a recorded wire array, when one exists, is **used** — it is the only source
  carrying positive evidence it was on the wire, so it is never traded for one
  that carries none. Extra views served since the send are **reported, not
  acted on**;
- the module's own view is used **only** when the caller has never supplied a
  message array (the documented explicit-breakpoint/Anthropic path, unchanged);
- staleness in the wire record is still caught, but by an **exact** check
  instead of a proxy: a record too old to contain the span fails
  `_prefix_contains_span` and **refuses LOUDLY** — standalone call, `WARNING`,
  `_summary_fork_fallbacks` incremented, named `reason`. "A fork that silently
  missed" is no longer reachable on this path.

Also added, because 6da needed it and could not get it: **`prefix_source`**
(`"wire_record"` / `"module_view"` / `None`) and **`prefix_views_since_send`**
on `last_summary_call_stats`. The next arm can separate the two populations
from the module's own report instead of reconstructing them from the wire.

**Config surface: unchanged. Default `summary_call_mode` remains
`"standalone"`.**

## 5. Tests

`286 passed, 1 skipped` (was 281 passed at `a877b36`). `ruff check`: clean.

New Group F in `tests/test_summary_call_mode_fork.py`:

| Test | Pins |
|---|---|
| `test_a_re_fetched_view_does_not_displace_the_recorded_wire_array` | THE regression: a superseding re-fetch must not displace the wire array |
| `test_the_module_view_is_never_substituted_when_a_wire_record_exists` | same defect from the other side: never-sent content cannot reach the fork |
| `test_prefix_source_names_the_module_view_path_honestly` | the module-view path is allowed but reported as what it is |
| `test_prefix_source_is_none_when_the_call_did_not_fork` | a refused fork claims no alignment |
| `test_tool_pair_integrity_and_seq_stability_survive_the_re_fetch_path` | no `_seq` consumed, history byte-identical, same span absorbed, served view identical to an unforked control |

**These three fail against the pre-fix selection logic and pass against the
fix** — verified by temporarily restoring the old branch and re-running; they
are load-bearing, not decoration.

One existing test changed: `test_a_stale_caller_message_record_is_ignored_not_trusted`
→ `test_a_stale_caller_message_record_refuses_loudly_not_silently`. It
asserted the substitution *as correct behaviour*; it now asserts the loud
refusal. The rewritten docstring records why the original resolution was
wrong, so the reversal is not silent.

Default-mode byte-identity re-verified by the pre-existing Group A/C tests,
strengthened with `assert context._fork_prefix_source is None` in
`test_default_mode_never_records_a_fork_prefix`.

## 6. Residual, disclosed

The **module-view path** (`note_request_sent(tools=...)` with no `messages`)
can still append to a superseded view — this module genuinely cannot know
whether its own view was sent. Not silently, now: `prefix_source ==
"module_view"` says so on every call. **A caller that wants byte-parity must
pass `messages`.** Closing this properly needs a caller-side confirmation
signal, which is an orchestrator change and out of this lane's scope.

Unchanged and still true: fork mode cannot fork the **first** summarization of
a CLI turn (each turn is a fresh `amplifier run --resume` process, and the
trigger is evaluated before any request is sent). 6da measured 12 of 24
refusals from this; this fix does not address it.

## 7. Deliverable ledger

| Deliverable | Status |
|---|---|
| DRAFT PR on origin, branch `lane/jnt-fork-prefix-capture`, tests green, default inline byte-identical | **DONE** |
| The array named at file:line with why it was never sent + cosmetic-vs-corrupting verdict | **DONE** — §1, §2 (**corrupting**) |
| Explicit statement of whether any of 6da's measured fork numbers are invalidated | **DONE** — §3 (**none invalidated**; −0.8% is a lower bound and warrants re-measurement) |
| DONE-NOTE.md in the PR body | **DONE** — this section |

**Spend: $0.00.** No API calls, no DTU, no containers, no infrastructure
created — the item was answerable from the code, the shipped tests, and 6da's
existing evidence files. Nothing to tear down; nothing registered in the infra
ledger. No PII or team-internal data. No merge to main. No files touched
outside this module.
