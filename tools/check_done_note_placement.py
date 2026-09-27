#!/usr/bin/env python3
"""Fail the build if a lane note is written to the repo-root ``DONE-NOTE.md``.

Why this exists
---------------
Several parallel lanes each appended their DONE-NOTE to ONE shared repo-root
``DONE-NOTE.md``.  Two lanes writing the same path is not a git conflict -- it
is git working correctly -- so the collision was structurally unable to raise an
alarm.  Then PR #30 (``revert: unproven default-off features per merge policy``)
reverted the features and took the shared note file with them, deleting four
lanes' notes from ``main`` in one commit (``e9ac159``).  A fifth lane's note
(``rb1``) never reached ``main`` at all: it was silently overwritten out of the
lineage before the revert ever happened.

The evals repo added the same guard after item ``model_performance-kez``.  That
guard was repo-local, which is exactly why this recurred here unseen.  This is
the port (item ``model_performance-sqh``).

What it checks
--------------
1. **No repo-root ``DONE-NOTE.md``** -- present on disk, or tracked in git.
2. **No branch adds or modifies one** -- diffed against the merge-base with
   ``origin/main``.  *Deleting* the root file is allowed; that is the fix.
3. **No two lanes' notes concatenated into one file** -- more than one
   ``# DONE-NOTE ... model_performance-<id>`` heading in a single file is the
   shape that made the original loss invisible.
4. **Lane notes live at ``docs/lanes/<lane>/DONE-NOTE.md``** -- the
   ``artifact-path/v1`` root resolved for this repo (item
   ``model_performance-6x4``: no ``probes/``, no ``ai_working/``, so the R3
   fallback applies).

There is deliberately **no environment-variable bypass**.

Usage::

    python3 tools/check_done_note_placement.py [--verbose] [--repo PATH]

Exit code 0 = clean, 1 = violation.  Also runs inside the pytest suite as
``tests/test_done_note_placement.py``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT_NOTE = "DONE-NOTE.md"
LANE_NOTE_DIR = "docs/lanes"
NOTE_HEADING_RE = re.compile(
    r"^# DONE-NOTE\b.*?model_performance-([a-z0-9]+)", re.IGNORECASE | re.MULTILINE
)


def _git(repo: Path, *args: str) -> tuple[int, str]:
    p = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    return p.returncode, p.stdout.strip()


def check(repo: Path, verbose: bool = False) -> list[str]:
    """Return a list of human-readable violations (empty == clean)."""
    problems: list[str] = []

    def say(msg: str) -> None:
        if verbose:
            print(f"  {msg}")

    # 1. root note present on disk / tracked
    if (repo / ROOT_NOTE).exists():
        problems.append(
            f"{ROOT_NOTE} exists at the repo root. Lane notes belong at "
            f"{LANE_NOTE_DIR}/<lane>/DONE-NOTE.md -- one file per lane, so a "
            f"revert of one lane's code cannot delete another lane's note."
        )
    rc, out = _git(repo, "ls-files", "--error-unmatch", ROOT_NOTE)
    if rc == 0 and out:
        problems.append(f"{ROOT_NOTE} is tracked in git at the repo root.")
    say(f"root {ROOT_NOTE}: {'PRESENT' if (repo / ROOT_NOTE).exists() else 'absent'}")

    # 2. branch adds or modifies the root note (deleting it is fine)
    base = None
    for ref in ("origin/main", "main"):
        rc, out = _git(repo, "merge-base", "HEAD", ref)
        if rc == 0 and out:
            base = out
            break
    if base:
        rc, out = _git(repo, "diff", "--name-status", f"{base}..HEAD", "--", ROOT_NOTE)
        for line in out.splitlines():
            status = line.split("\t", 1)[0]
            if status.startswith(("A", "M")):
                problems.append(
                    f"this branch {'adds' if status.startswith('A') else 'modifies'} "
                    f"the repo-root {ROOT_NOTE} (vs {base[:8]}). Write to "
                    f"{LANE_NOTE_DIR}/<lane>/DONE-NOTE.md instead."
                )
        say(f"branch diff vs {base[:8]} for {ROOT_NOTE}: {out or 'no change'}")
    else:
        say("no merge-base with origin/main or main -- skipped the branch-diff check")

    # 3 + 4. every tracked DONE-NOTE.md: single-author, correct location
    rc, out = _git(repo, "ls-files", "*DONE-NOTE.md")
    tracked = [p for p in out.splitlines() if p]
    # also consider untracked-but-present files under docs/lanes, so a lane
    # catches itself before it commits
    for p in sorted((repo / LANE_NOTE_DIR).glob("*/DONE-NOTE.md")):
        rel = p.relative_to(repo).as_posix()
        if rel not in tracked:
            tracked.append(rel)

    for rel in sorted(tracked):
        f = repo / rel
        if not f.exists():
            continue
        text = f.read_text(errors="replace")
        authors = NOTE_HEADING_RE.findall(text)
        if len(authors) > 1:
            problems.append(
                f"{rel} contains {len(authors)} lanes' notes concatenated into one "
                f"file ({', '.join(authors)}). Split them: one lane per file."
            )
        parts = rel.split("/")
        is_lane_note = (
            len(parts) == 4
            and parts[0] == "docs"
            and parts[1] == "lanes"
            and parts[3] == ROOT_NOTE
        )
        if not is_lane_note and rel != ROOT_NOTE:
            problems.append(
                f"{rel} is a DONE-NOTE outside {LANE_NOTE_DIR}/<lane>/ "
                f"(artifact-path/v1 for this repo)."
            )
        say(f"{rel}: authors={authors or ['(none)']}")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=None, help="repo root (default: this file's repo)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo) if args.repo else Path(__file__).resolve().parent.parent
    if args.verbose:
        print(f"check_done_note_placement: {repo}")
    problems = check(repo, verbose=args.verbose)
    if problems:
        print("FAIL -- DONE-NOTE placement:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("OK -- no repo-root DONE-NOTE.md; every lane note is single-author and in place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
