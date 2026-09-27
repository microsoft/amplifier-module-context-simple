# Lane notes

One directory per lane, one `DONE-NOTE.md` per directory. This is the
`artifact-path/v1` root resolved for this repo (item `model_performance-6x4`,
rule R3: no top-level `probes/`, no `ai_working/`, no wave-prefix convention, so
the documented fallback `docs/lanes/<lane>/` applies).

**There is no repo-root `DONE-NOTE.md`, and there must never be one again.**
`tools/check_done_note_placement.py` fails the build on it, and runs as part of
`pytest` via `tests/test_done_note_placement.py`.

## Why

Six lanes appended their notes into one shared repo-root `DONE-NOTE.md`. Two
lanes writing the same path is not a git conflict — it is git working correctly
— so nothing could raise an alarm. Two things then happened, both silently:

* PR #30, `revert: unproven default-off features per merge policy (wins only)`
  (`e9ac159`), reverted the feature code **and the shared note file with it**,
  deleting four lanes' notes from `main` in a single commit.
* One lane's note (`rb1`) never reached `main` at all — it was overwritten out
  of the lineage before the revert, and survives only on
  `origin/lane/rb1-rebase-conflicted-prs`.

Every note was recovered from git history and re-homed here by item
`model_performance-sqh`; the enumeration, provenance and round-trip proof are in
[`sqh-context-simple-note-loss/AUDIT.md`](sqh-context-simple-note-loss/AUDIT.md).

## Index

| lane | item | subject |
|---|---|---|
| [`x7p-protected-tool-results-bug`](x7p-protected-tool-results-bug/DONE-NOTE.md) | `model_performance-x7p` | `protected_tool_results=0` protected ALL tool results (negative-slice bug) |
| [`x1r-tool-result-budget`](x1r-tool-result-budget/DONE-NOTE.md) | `model_performance-x1r` | tool-result budget (token-denominated, head+tail, per-tool) + spill-to-disk |
| [`2o9-clear-at-least`](2o9-clear-at-least/DONE-NOTE.md) | `model_performance-2o9` | `clear_at_least` — a worth-the-rebuild predicate in front of compaction (+ summary shrink guard) |
| [`7k2-summary-call-fork`](7k2-summary-call-fork/DONE-NOTE.md) | `model_performance-7k2` | `summary_call_mode` — cache-safe forking of the summarization call |
| [`jnt-fork-prefix-capture`](jnt-fork-prefix-capture/DONE-NOTE.md) | `model_performance-jnt` | `_capture_fork_prefix` forked onto an array that was never sent |
| [`rb1-rebase-conflicted-prs`](rb1-rebase-conflicted-prs/DONE-NOTE.md) | `model_performance-rb1` | merge-queue repair: rebased and landed the conflicted lane PRs (#21, #24) |
| [`sqh-context-simple-note-loss`](sqh-context-simple-note-loss/DONE-NOTE.md) | `model_performance-sqh` | this recovery: enumeration, re-homing, and the guard |

The first four rows' wording is carried over verbatim from the index table of
the shared file's last revision (`5cdbc62`), so nothing that file said is lost.
