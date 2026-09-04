# DONE-NOTE — `fix-513-standing-compaction-notice`

`context-simple: compaction notice degrades to a standing form between escalations (#513)`

Branch: `fix-513-standing-compaction-notice` · repo: `microsoft/amplifier-module-context-simple` ·
branched from `main` @ `f2dbde9`.

Bug: `_last_compaction_stats` is never cleared during normal operation, so once the first
compaction happens the notice gate at `get_messages_for_request` is true on every subsequent
request, and the FULL incident report is appended byte-identically forever, with no marker that
it is stale. Fix: track which stats object has already been announced
(`_notice_shown_for_stats`), compared by **identity**, and emit a short self-contained "standing"
notice instead of the full report whenever nothing new has been compacted since the last
announcement.

---

## 1. The identity-vs-level correction (spec §2.1), with probe output

The obvious-looking fix is to key "has this been announced" off
`stats["strategy_level"]` — compare the level in the new stats to the level last shown, only emit
the full notice again when the level *changes*. **This is wrong, and was caught before writing any
code**, because `strategy_level` is not a per-escalation value — it is `self._sticky_level`, a
monotonic high-water mark (`__init__.py`, sticky-state block near the constructor; assigned via
`max(...)` inside `_finalize_compaction_with_stats`). It climbs early in a session and then pins at
its ceiling while compaction keeps firing underneath it.

**Probe, run against the real ladder before implementing** (25-turn synthetic session, default
knobs): genuine escalations (a freshly-assigned stats object, i.e. `stats is not last_stats`)
occurred at calls **0, 5, 9, 14, 18, 23**. `strategy_level` at each of those six calls:

| call | 0 | 5 | 9 | 14 | 18 | 23 |
|---|---|---|---|---|---|---|
| `strategy_level` | 8 | 8 | 8 | 8 | 8 | 8 |
| `messages_removed` | 22 | 29 | 40 | 47 | 58 | 65 |

Level is `8` at every single one of the six real escalations, while `messages_removed` keeps
climbing. A level-keyed implementation would emit the full notice once (at call 0, when level
first reaches its production ceiling), and then **permanently suppress it for the rest of the
session** — hiding five subsequent real compactions that each dropped tens of additional messages.
That is worse than the bug being fixed: it doesn't just show a stale notice, it shows *no* notice
at all for the majority of real events.

**Fix used instead: object identity on the stats dict itself.**
`_last_compaction_stats` is assigned a fresh dict at exactly one call site
(`_finalize_compaction_with_stats`) and is never mutated afterward (checked: no `stats[...] = `
assignment anywhere after construction). So
`self._last_compaction_stats is not self._notice_shown_for_stats` is an exact, zero-cost "did a
real escalation just happen" signal, with no false negatives from the sticky ceiling and no
counter to maintain. This is also the same idiom the pre-existing test suite already used
(`stats is not last_stats`), so it isn't a new pattern for this codebase.

---

## 2. The `metadata.source` stability constraint (spec §2.2)

`tests/test_sticky_compaction_and_tail_notice.py`'s `_notices()` helper filters solely on
`(m.get("metadata") or {}).get("source") == "context-compaction"`. Giving the standing notice a
different `metadata["source"]` would make that helper return `[]` for it, and
`test_notice_returns_once_tool_results_arrive`'s `len(notices) == 1` assertion on a non-escalation
call would fail. More generally, any downstream consumer filtering the message stream on that
literal value would silently stop recognizing standing notices as compaction notices at all.

**Both notice kinds keep `metadata["source"] = "context-compaction"`, unconditionally.** The
fresh/standing distinction is carried two ways instead:

- a new `metadata["notice_kind"]` key (`"full"` or `"standing"`) — for telemetry / test assertions,
  never used as the model-facing signal;
- the `source=` attribute **inside** the XML-ish notice text itself
  (`source="context-compaction"` for the full notice, `source="context-compaction-standing"` for
  the standing one) — this is what the model actually reads, since it never sees `metadata`.

Verified: the four new tests assert `metadata["source"] == "context-compaction"` on *both* kinds,
and separately assert the differing `notice_kind` value and differing in-text `source=` attribute.
The full suite (`uv run pytest -q`) is green including the pre-existing
`test_notice_returns_once_tool_results_arrive`, unmodified.

---

## 3. Edge case 7 — subclass override contract

`_format_compaction_notice` is documented (and used in the wild) as a method subclasses may
override to customize the full notice's wording/format. This fix adds a sibling method,
`_format_standing_compaction_notice`, rather than folding standing-vs-full logic into
`_format_compaction_notice` itself.

**Consequence worth flagging, not fixed here:** a subclass that overrides only
`_format_compaction_notice` (the common case — customizing the full report) will silently inherit
the **base class's** standing-notice text for every repeat announcement, without the subclass
author necessarily realizing a second method now exists. This is a reasonable default (the
standing notice is generic and self-contained by design, §2.3 of the spec — it doesn't reference
anything from the full notice's format), but it is a contract change for existing subclasses: prior
to this fix there was only one method to override to control 100% of notice output; after this fix
there are two, and overriding only one no longer covers the whole surface. No existing subclass in
this repo's own test suite exercises this path, so nothing here breaks today — recorded so the next
person maintaining a subclass of `SimpleContextManager` knows to check both methods.

---

## 4. Other notes (not requested above, recorded for completeness)

- **No new reset site was needed beyond the two enumerated in the spec.** `_notice_shown_for_stats`
  is reset alongside `_last_compaction_stats` in both `set_messages` (session restore) and `clear`,
  so a resumed or cleared session re-announces its next compaction as full, from scratch.
- **The `tool_calls` skip guard composes correctly with the new field.**
  `_notice_shown_for_stats` is only updated *after* a notice is actually appended, so a full notice
  skipped by the unanswered-`tool_calls` guard is retried as full (not silently downgraded to
  standing) on the very next request. Covered by
  `test_standing_notice_respects_notice_config_gates`'s sibling assertion that a
  `min_level`-suppressed notice also leaves `_notice_shown_for_stats` at `None`.
- **Out of scope, per the spec:** this does not stop the notice from firing on every request
  forever — it makes every repeat cheap (short) and unambiguous (self-contained, distinctly
  tagged). Eliminating the notice firing altogether once acknowledged is a different, deliberately
  unaddressed change.

## 5. Verification

- `uv run pytest -q` — full suite green (101 passed, including the 4 new tests added to
  `tests/test_sticky_compaction_and_tail_notice.py`).
- `uv run ruff check .` — clean.
- `uv run ruff format --check` on the two touched files — the notice-kind dict literal was
  reformatted to match; one pre-existing, unrelated formatting diff in `__init__.py` (a
  `_should_compact` boolean expression untouched by this change, confirmed via `git stash`) was
  left alone, out of scope for this fix.
- `uv run pyright` on the two touched files — 0 errors, 0 warnings.
