# DONE-NOTE — W3-2 / `model_performance-7k2`

`context-simple: cache-safe forking of the summarization call (summary_call_mode: "fork")`

**Status: draft PR, mechanism shipped behind an off-by-default flag, nothing
measured.** Default is byte-identical to today. Lane spend: **$0.00** (the
item's authority was $0; no API calls, no DTU, no infrastructure created,
nothing to tear down).

## 1. Step 0 — the free measurement that could have cancelled this item

The item said: read the summarizer call; if it already prepends the parent's
assembled request, §2h conflict #8 resolves in favour of [P6]'s 2.4% table
figure and **there is nothing to build**.

It does not. `_run_summary_compaction_task` built, verbatim:

```python
request = ChatRequest(
    messages=[
        Message(role="system", content=prompt),      # the ~955-char prompt
        Message(role="user", content=formatted),     # the span, re-rendered as plain text
    ],
    model=self.summarization_model,
)
```

No parent history. No tools. Its own `role: "system"` prompt. This is the
standalone shape, so **the branch that cancels the item does not apply** and
the fix is real work. §2d's indirect evidence was right: G3 had to split out
the summarizer's own `instructions` population to get a clean hash precisely
*because* the summarizer sends its own separate system prompt.

**What this does NOT resolve:** the 8.3–10.9% (prose) vs 2.4% (same source's
own table) conflict about the summarizer's cost *share*. Being standalone
makes the cost avoidable; it does not say how much cost there is. That number
must come from a run's own per-call table, and the follow-up item requires it.

## 2. What shipped

`summary_call_mode: "standalone" | "fork"`, default `"standalone"`, consulted
only when `compaction_strategy == "summary"`.

- **`"standalone"`** — the pre-existing two-message call, unchanged.
  `"inline"` is accepted as an alias (the lane brief and the work item named
  the same default differently; both mean "today's behaviour"). Unknown values
  warn and fall back, matching every other enum in this module.
- **`"fork"`** — the same ask re-issued as a **pure append**:
  `[...the exact messages of the last request...] + [one user message: prompt + scope]`.
  Pure append is the one mutation measured as a HIT under grow-only
  (P4: identical-repeat 9,789 HIT, pure-append 9,789 HIT, truncation 0,
  middle-drop 0).

Three consequences, each load-bearing rather than incidental:

1. **The span is not re-sent.** It is already inside the prefix. Re-sending it
   would cost what standalone costs today *plus* the prefix — a regression
   wearing an optimization's clothes. This is the single decision that
   determines whether the feature is worth anything.
2. **The prompt moves into the appended `role: "user"` message.** A fork must
   not add a `role: "system"` message: providers hoist every system-role
   message into one top-level system block, so a per-summarization one would
   rewrite the cached system prefix — the exact failure already measured for
   the summary tier (cache_read 46,307 → 21,523) and the compaction notice.
3. **Scope must be stated.** Standalone scopes by construction (it can only
   see the span). A fork can see everything, so the appended message names the
   span: its message count plus a bounded (300-char) verbatim excerpt of its
   final message as the "summarize up to here" marker. **This is a real
   behavioural difference between the two modes**, not formatting — flagged as
   a residual risk in the follow-up item, not hand-waved.

New optional public seam: **`note_request_sent(messages=None, *, tools=None,
model=None)`** — see §3, which is the finding this lane most wants read.

Observability: `last_summary_call_stats` property (`mode_requested`,
`mode_used`, `reason`, `prefix_messages`, `fork_fallbacks`) and a `call_mode`
field on `context:pre_summarize` / `context:post_summarize`, so an eval arm
can count *real* forks without patching the module.

Size: **170 executable lines** across the new helpers (item estimated 50–70).
The overrun is entirely the refusal ladder (§3) and the staleness guard (§4);
the happy path is ~30 lines.

## 3. The finding: this cannot be done from inside the context module alone

**Tool specs are part of the cached prefix and this module is never handed
them.** Anthropic serializes `tools` *ahead of* the system block; OpenAI's
implicit cache matches forward from a cached entry. A summarizer request with
`tools=None` diverges from the parent at byte zero, so it hits nothing — and
because it now carries the whole conversation, it costs *more than the
standalone call it replaced*. That is not a missed win, it is a regression.

I looked for a way to obtain them without changing any caller and found none
that is contract-backed:

- `amplifier_core.interfaces` defines no tool-registry protocol; the
  coordinator exposes `mount_points`, not tool specs in send order.
- The `llm:request` event carries `model`/`message_count`/thinking flags, and
  the actual tool payload only under provider `raw: true` — where it is wire
  dicts passed through `redact_secrets()`, i.e. not reconstructable into
  `ToolSpec` byte-faithfully.
- The default orchestrator lives in the compiled Rust engine, so there is no
  seam to read it from.

So fork mode requires the caller to say what it sent. **Decision taken without
waiting** (per SCOPE-OUTS): add one optional public method rather than a
config knob that lets an operator *assert* alignment they cannot verify.
Passing `tools` **at all** — even `None`/`[]` for a genuinely tool-free
session — is what arms the fork; "not supplied" and "supplied as empty" are
deliberately distinguishable, because guessing between them is exactly how a
fork silently misaligns.

**Every misalignment refuses and falls back to standalone, loudly** — WARNING
naming the precondition (once per distinct reason, so a session that can never
fork costs a handful of log lines, not one per summarization), a session
counter, and the mode actually used reported on both hooks and the stats
property. The refusals: seam never called · `summarization_model` set (a
summarizer routed elsewhere reads none of the main line's cache — deepseek
states this explicitly) · no request recorded yet · prefix ends on an assistant
turn with unanswered `tool_calls` (appending there would interleave between
`tool_use` and `tool_result`) · span absent from the recorded prefix · the
forked request fails to build.

Falling back to *standalone* rather than skipping is deliberate: standalone is
correct and costs what it always did. Losing a summary over a cache
optimization would be strictly worse than not optimizing.

**Consequence for the eval, stated plainly:** the S5-CRAC harness must wire
`note_request_sent()` or the treatment arm silently degrades into the control
arm and every gate is vacuous. That is the first thing the follow-up item
(`model_performance-6da`) tells the runner to verify — on a 1-run smoke,
before any spend.

## 4. Provider asymmetry, and the staleness trap

**Anthropic vs OpenAI need different fork sources, and the seam covers both.**
Anthropic places its cache breakpoint on the last *stable* message, walking
back past ephemeral/injected content — which is exactly where this module's
own returned view ends. So `tools` alone is enough there. OpenAI's implicit
cache measured **MISS on strict truncation** (P4), i.e. it needs a strict
superset of a cached request; an orchestrator-injected tail this module never
sees would break that. So on OpenAI the caller must also pass `messages`.
`_capture_fork_prefix` prefers the caller's record and falls back to the
module's own view.

**The trap I built a guard for.** A caller that wires `note_request_sent()`
once (a startup helper, first turn only) would have turn 40's fork append to
turn 1's request — a guaranteed miss *and* a wasted cache write, wearing a
correct-looking API call. A `_view_serial`/`_sent_serial` pair means a
`messages` record is only used while it still describes the most recent
request served; a stale one is ignored (module view used instead), never
trusted. Tested.

## 5. Evidence — 282 tests green (250 before, 32 new)

`tests/test_summary_call_mode_fork.py`, written against the ways this goes
silently wrong rather than the way it is supposed to work:

| Group | What it pins |
|---|---|
| **A — the default must not move** | The standalone request is asserted against **independently rebuilt** expected content (2 messages, system prompt then formatted span, `model` from `summarization_model`), not against itself. Plus: the fork bookkeeping is never even *written* unless armed (`_last_request_view is None` after 5 requests in both default configs) — an always-on capture would be a silent per-request list allocation on the hot path. Plus: `note_request_sent()` is fully inert when fork is not configured. |
| **B — pure append** | `sha256(fork[:-1]) == sha256(parent_view)` (G-FORK-PREFIX in unit form); exactly one appended message; **no new system message**; tools and model pinned from the seam; the span is **not** re-sent (`formatted not in appended`, and the appended message is shorter than the formatted span); the caller's `messages` record wins verbatim when supplied, including an injected tail. |
| **C — the main line is not the summarizer's scratchpad** | No `_seq` consumed; history byte-identical before/after; `_removed_seqs` unmoved; **`_last_sent_estimate` unmoved** (building the fork through `_finalize_view` would silently rewrite the hybrid meter's conservatism comparand with the summarizer's own request — this is why `_build_fork_request` calls `_strip_internal_metadata` directly); and a forked session serves views **bit-for-bit identical** to an unforked control, same `_removed_seqs`. |
| **D — refusals** | All six refusal paths fall back to the 2-message standalone request with the reason recorded; warning fires; warn-once-per-reason while the counter still counts every one; a failed fork build still produces the summary; hooks report the mode actually used; the stale-record guard prefers the fresh view. |
| **E — tool pairs** | `_select_summary_absorb_seqs` returns **identical** spans in both modes on a tool-pair-heavy history; end-to-end, no tool result is ever served without its call. The call mode changes how the summarizer is *called*, never what is *selected*. |
| **F — reset/lifecycle** | `clear()` and `set_messages()` drop all fork alignment state; the fork prefix is snapshotted at **trigger** time, not at task-scheduling time (proved by mutating `_last_request_view` while the task is in flight); the single-summarization-in-flight guard still holds under `asyncio.gather` of three concurrent requests. |

`ruff check` clean. `ruff format` was **not** run: the repo is not
format-clean today (7 pre-existing files would be reformatted), so running it
would bury this change in unrelated noise.

## 6. What is NOT claimed

- **No cache win. No cost win. Nothing was measured.** G-FORK-PREFIX /
  G-FORK-CACHED / G-FORK-COST / G-FORK-NOBOUNDARY are the follow-up item
  (`model_performance-6da`, filed `discovered-from` this one, with the arm
  design, the blocking prerequisite, and the residual risks). $0 spent here by
  mandate.
- **Honest ceiling, unchanged from the item's own statement:** this reduces
  the separate summarizer charge only. It does **not** touch the boundary
  rebuild, which §2d measured as the dominant cost (+84% boundaries → +83% run
  cost). If the summarizer's real share is the table's 2.4% rather than the
  prose's 8.3–10.9%, a *perfect* fork is worth ~2% of run cost and the honest
  recommendation may be "mechanism proven, not worth enabling". The follow-up
  item is required to report that share from the run's own table and to say so
  if it lands there.
- **Retention parity is not established.** Fork mode scopes by instruction
  where standalone scopes by construction. The follow-up item reports S5 score
  and `b_constraints`/`c_post_compaction` per arm so a cost win bought with a
  retention loss cannot be reported as a win.
- **`reasoning_effort` / thinking configuration is not reproduced** by the
  fork — it is not observable from inside a context module. If a provider keys
  its cache on it, the fork misses despite byte-aligned messages.
  G-FORK-CACHED is the detector; this is disclosed, not designed around.
- **The compaction-buffer reserve (condition (a) in the item) is not
  implemented.** The appended instruction is ~1.2k chars, not the ~8,000-token
  reserve the item contemplated, and the fork never grows the *main* line's
  request. If the follow-up eval shows the fork's own request crowding the
  window, that reserve is the fix, and G-FORK-NOBOUNDARY is the gate that
  would catch it.

## 7. Deliverable ledger

| Deliverable | Status |
|---|---|
| DRAFT PR on origin, branch `lane/7k2-summary-call-fork`, default byte-identical, tests green | **DONE** |
| Prefix-stability test proving the fork does not touch the main line | **DONE** — Group C (no `_seq`, history byte-identical, `_last_sent_estimate` unmoved, forked-vs-control views identical) |
| Follow-up eval item filed via `work_file` with its arm design | **DONE** — `model_performance-6da` |
| DONE-NOTE.md in the PR body | **DONE** — this section |

No PII, no team-internal data, no individual attribution. No merges to main.
No files touched outside this module. No infrastructure created; nothing to
tear down.

