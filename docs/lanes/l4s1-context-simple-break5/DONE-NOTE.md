# l4s1 — Land context-simple break 5 (homeless across three lanes)

**Item:** `model_performance-l4s1` · **Outcome branch: A (RESOLVED)** · **Spend: $0.00 of $0.00**

Break 5 — *compaction must not drop the message carrying loaded-tool state* — is
**applied, re-measured at today's head, and shipped as a draft PR.** Every deliverable
is DONE. Nothing was NOT-POSSIBLE; the $0 authority was correct and sufficient
(the whole lane is `git`, `pytest` and `ruff` — no API calls, no DTU, no containers).

**The one thing that is NOT as previously reported: the patch did not apply cleanly.**
It applied *with fuzz* at today's head, so per the goal it was **ported by hand, not
forced**. The divergence is named in §3.

---

## 1. Deliverables

| # | Deliverable | State |
|---|---|---|
| 1 | Patch applied to current `context-simple` main, sourced from provider-openai main | **DONE** — ported (§3), not force-applied |
| 2 | FAIL-BEFORE re-run at today's head, counts from **this** run | **DONE** — §2, head `dd9b9c3` |
| 3 | Full suite + ruff clean | **DONE** — §2 |
| 4 | Explicit statement: does G13 become measurable? | **DONE** — §4 |
| 5 | DRAFT PR, not merged | **DONE** — see `DONE.json` `publication` block |
| 6 | DONE-NOTE.md at lane artifact root (never repo root) | **DONE** — this file |

---

## 2. The measurement, at today's head

**Head measured: `dd9b9c3f042fc70588f0afbd6a81f4ac20337ee7`** (`ci: add GitHub Actions
CI …`, PR #36). `v5co` measured against **`a2a098b`**; the baseline moved by 4 merged
PRs (#30, #32, #34, #36) touching `__init__.py` by 290 lines.

| step | `v5co` @ `a2a098b` | **this lane @ `dd9b9c3`** |
|---|---|---|
| patch applies | clean, no fuzz | **NOT clean — hunk 1 fuzz 2** → ported (§3) |
| FAIL-BEFORE (test, unpatched) | 1 failed, 2 passed | **1 failed, 2 passed** — identical |
| failure mode | `len([]) == 0`, carrier removed | **`assert 0 == 1`, `where 0 = len([])`** — identical |
| PASS-AFTER (test, patched) | 3 passed | **3 passed** |
| full suite, unpatched | 58 passed | **102 passed, 1 xfailed** |
| full suite, patched | 61 passed | **105 passed, 1 xfailed** |
| regressions | 0 | **0** |
| ruff | clean | **`uvx ruff@0.14.10 check .` → All checks passed** |

**The fail-before counts are unchanged from `v5co`'s (1 failed / 2 passed → 3 passed)
and so is the failure message.** The *suite* counts differ only because the suite grew
from 58 to 102 tests between the two heads; the delta is the same **+3**.

Raw logs: `evidence/fail-before.txt`, `evidence/pass-after.txt`.

**Byte-identity of default mode.** The protection fires only when a message's
`metadata` carries a key in `LOADED_TOOL_STATE_METADATA_KEYS`. With no such key
present, `loaded_tool_state_indices` is empty, `protected_indices` is untouched and no
branch changes — which is why all 102 pre-existing tests pass byte-for-byte unchanged
(stash-compare in `evidence/`: the same 102 passed / 1 xfailed on both sides).

**`ruff format --check` reports 5 files would be reformatted — this is pre-existing on
clean main and is not caused by this change.** The CI workflow itself documents this at
`.github/workflows/*.yml:19–29`: it deliberately runs `ruff check` only, because no repo
in this module family carries a `[tool.ruff]` section, so `format --check` would enforce
ruff's default line-length-88 and be red on clean main on day one. The 5 files it names
are exactly the 5 seen here, including `__init__.py` **before** this patch.

---

## 3. What diverged, and why the patch was ported rather than forced

`patch -p1 --dry-run` at `dd9b9c3`:

```
Hunk #1 succeeded at 56 with fuzz 2 (offset 27 lines).
Hunk #2 succeeded at 1456 (offset 254 lines).
```

- **Hunk 2 — clean.** Offset 254 is line drift only; zero fuzz, context matched exactly.
  Applied verbatim into `_remove_messages_with_protection`, immediately after the
  system-message protection loop, exactly as `v5co` wrote it.
- **Hunk 1 — NOT clean, fuzz 2.** `v5co` anchored the new module-level block on
  `logger = logging.getLogger(__name__)` followed directly by a blank line and
  `async def mount(`. Since `a2a098b`, PR #32/#34's **token-meter config block**
  (`TOKEN_METER_ESTIMATE` / `TOKEN_METER_ACTUAL` / `_VALID_TOKEN_METERS`, with its
  five-line comment) was inserted **between** those two anchors. `patch` resolved the
  mismatch by discarding 2 context lines — a silent placement decision.

  **Ported instead:** the block was placed by hand **after** the token-meter constants
  and immediately before `async def mount(`, which keeps all module-level constants
  together and leaves the token-meter block undisturbed. Semantically identical to
  `v5co`'s intent; the only difference is where in the constants region it sits.

Net diff: **+51 lines, one file** (`amplifier_module_context_simple/__init__.py`),
plus the 112-line test at `tests/test_loaded_tool_state_protection.py`.

Both patches are kept side by side for audit:
`evidence/break5-v5co-original.patch` (as shipped from provider-openai main) and
`evidence/break5-ported-at-dd9b9c3.patch` (what actually landed here).

---

## 4. Does G13 (compaction survival) become measurable once this lands?

**Yes — with this merged, G13 is measurable, and no other code prerequisite remains.
What remains is spend and a scoped caveat, not a blocker.**

The chain is now complete and was **verified across both repos in this lane, not
assumed**:

1. **Producer.** `amplifier-module-provider-openai` @ `702b361` (main) defines
   `METADATA_TOOL_SEARCH_ITEMS = "openai:tool_search_items"` (`_constants.py:19`) and
   writes it onto response metadata in two paths
   (`__init__.py:4752`, `_response_handling.py:590`).
2. **Consumer.** The same provider replays it **off each message's own `metadata`**
   while rebuilding the request (`__init__.py:3975`,
   `for _ts_item in metadata.get(METADATA_TOOL_SEARCH_ITEMS) or []`) — so the state
   genuinely lives on messages in the context manager's list.
3. **Protector.** This PR. The key string in
   `LOADED_TOOL_STATE_METADATA_KEYS` is **byte-identical** to the provider's constant,
   so the protection actually fires on real traffic and not only in the unit fixture.
   That equality was checked, because a near-miss here would have protected nothing
   while looking green.

**Two caveats a future eval must carry, both stated rather than discovered later:**

- **Retention is fixed; *ordering* is out of scope.** `TS:893`'s positional contract for
  an `additional_tools` input item is untouched, because this provider never persists
  that item into history — it rebuilds it at the input tail every request from
  session-scoped provider state. If a future implementation persists it into the message
  list, break 5 acquires a second half (ordering, not just retention) that this patch
  does **not** cover. (Carried forward verbatim from `v5co`'s scope-honesty section and
  re-confirmed against provider main.)
- **G13 still costs money to *run*.** This removes the code blocker. Actually grading
  compaction survival needs live OpenAI traffic with `tool_search.mode` on and enough
  context to force a boundary. That is a spend authority for a future lane, not a
  missing piece here.

**Consequence for `webu`'s record:** `webu` recorded G13 **UNINFORMATIVE with the
dependency named**. That call was correct at the time. Once this PR merges, the reason
it gave no longer holds, and a re-run of G13 should be expected to be *informative* —
pass or fail on its merits.

---

## 5. Spend

**$0.00 spent against a $0.00 authority** (`0 runs × 0 arms × $0 / 1.00 = $0.00`,
slack $0.00). No API calls, no DTU, no containers, no infrastructure registered and
therefore none to tear down. The arithmetic closed on first read: applying an existing
patch and running an existing suite costs nothing, and nothing in the deliverable list
required a purchase. Total wall time ≈ 6 minutes, all local.

---

## 6. Deviations from the goal

None substantive. One judgment call, recorded per procedure:

- **The patch was ported, not applied.** The goal explicitly forbids force-applying and
  requires the divergence be explained; §3 is that explanation. `patch` would have
  succeeded with fuzz, which is exactly the silent-placement case the instruction
  exists to prevent.

## 7. Files

| path | what |
|---|---|
| `DONE-NOTE.md` | this file |
| `evidence/fail-before.txt` | FAIL-BEFORE + unpatched full suite at `dd9b9c3` |
| `evidence/pass-after.txt` | PASS-AFTER + patched full suite + `ruff check` |
| `evidence/break5-v5co-original.patch` | the artifact as shipped on provider-openai main |
| `evidence/break5-ported-at-dd9b9c3.patch` | what actually landed here (+51 lines) |
| `evidence/BREAK5-PATCH-v5co.md` | `v5co`'s original write-up, kept for provenance |
