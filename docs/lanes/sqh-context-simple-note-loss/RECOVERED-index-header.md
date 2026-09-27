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
| `model_performance-7k2` | `summary_call_mode` — cache-safe forking of the summarization call |

---
