#!/usr/bin/env python3
"""Recover every lane DONE-NOTE that was ever written into the repo-root
``DONE-NOTE.md`` of amplifier-module-context-simple, and split each one back
into its own ``docs/lanes/<lane>/DONE-NOTE.md``.

Method (item ``model_performance-sqh``; ported from ``model_performance-kez``'s
proven method in the evals repo, ``probes/kez-done-note-collision/AUDIT.md``):

1. Walk ``git log --all --full-history -- DONE-NOTE.md``.  ``--full-history``
   matters: plain ``git log`` prunes the losing side of a merge, which is what
   made this file look like an ordinary short-history file.
2. Resolve every one of those commits to a blob; de-duplicate.
3. In each blob, find the *authored* notes -- lines matching
   ``^# DONE-NOTE\\b.*model_performance-<id>``.  A bare mention of an item id in
   prose is NOT authorship; counting mentions is what produced the "8 lanes"
   estimate this lane was asked to verify.
4. Take, for each lane, the richest blob that contains its note, and slice the
   note out on exact line boundaries.
5. Prove the slice is lossless: re-concatenating index + separators + bodies
   must reproduce the original blob's object hash byte for byte.

Run from the repo root::

    python3 docs/lanes/sqh-context-simple-note-loss/recover_root_done_notes.py --report
    python3 docs/lanes/sqh-context-simple-note-loss/recover_root_done_notes.py --write
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HEADING_RE = re.compile(r"^# DONE-NOTE\b.*?model_performance-([a-z0-9]+)", re.IGNORECASE)

# Lane id -> lane directory name.  Every value here is a real branch name in
# this repo (``git for-each-ref refs/heads/lane refs/remotes/origin/lane``),
# never invented: the artifact-path/v1 rule says <lane> is the lane id string
# that the branch, the worktree and the marker path already use.
LANE_DIRS = {
    "x7p": "x7p-protected-tool-results-bug",
    "x1r": "x1r-tool-result-budget",
    "2o9": "2o9-clear-at-least",
    "7k2": "7k2-summary-call-fork",
    "jnt": "jnt-fork-prefix-capture",
    "rb1": "rb1-rebase-conflicted-prs",
}


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def hash_object(data: bytes) -> str:
    return subprocess.run(
        ["git", "hash-object", "--stdin"], input=data, capture_output=True, check=True
    ).stdout.decode().strip()


def distinct_root_blobs() -> list[tuple[str, str, str]]:
    """(blob, first_commit_seen, subject) for every distinct blob the root path held.

    Two passes, because neither alone is complete:

    * ``git log --all --full-history`` gives the *reachable* history and, with
      it, the commit each blob first appeared in.
    * A sweep of **every tree in the object store** then catches blobs that are
      no longer reachable from any ref -- a deleted worktree, an abandoned
      rebase, a mid-conflict snapshot.  Without this pass the enumeration
      silently under-counts, which is exactly the failure this item exists to
      stop.  Such blobs are labelled ``(unreachable object)``.
    """
    seen: dict[str, tuple[str, str]] = {}
    for c in git("log", "--all", "--full-history", "--format=%H", "--", "DONE-NOTE.md").split():
        try:
            blob = git("rev-parse", f"{c}:DONE-NOTE.md").strip()
        except subprocess.CalledProcessError:
            continue  # this commit deleted the path
        if blob not in seen:
            seen[blob] = (c, git("log", "-1", "--format=%h %s", c).strip())

    trees = [
        line.split()[0]
        for line in git("cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)").splitlines()
        if line.endswith(" tree")
    ]
    for t in trees:
        try:
            entries = git("ls-tree", t)
        except subprocess.CalledProcessError:
            continue
        names = {}
        for line in entries.splitlines():
            fields = line.split()
            if len(fields) >= 4:
                names[fields[3]] = (fields[1], fields[2])
        # only a REPO-ROOT tree counts: a per-lane directory also holds a file
        # called DONE-NOTE.md, and that one is correctly placed.
        if "DONE-NOTE.md" in names and "pyproject.toml" in names:
            kind, blob = names["DONE-NOTE.md"]
            if kind == "blob":
                seen.setdefault(blob, ("", "(unreachable object)"))
    return [(b, c, s) for b, (c, s) in seen.items()]


def authored_notes(text: str) -> list[tuple[int, str]]:
    """[(1-based heading line, lane id)] for the notes actually authored in *text*."""
    out = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = HEADING_RE.match(line)
        if m:
            out.append((i, m.group(1).lower()))
    return out


def split_blob(text: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Split a shared root file into (index_prefix, [(lane, separator, body), ...]).

    The separator above each note is found by scanning back from the heading to
    the nearest ``---`` rule and is captured **verbatim** rather than assumed to
    be a fixed number of lines -- the blank-line padding is not uniform across
    this file's history.  Bodies are sliced on exact line boundaries, so
    ``index_prefix + sum(separator + body)`` reproduces the input byte for byte.
    """
    lines = text.splitlines(keepends=True)
    heads = authored_notes(text)
    if not heads:
        return text, []

    def sep_start(heading_idx0: int) -> int:
        """0-based index of the ``---`` rule immediately above a heading."""
        for j in range(heading_idx0 - 1, -1, -1):
            if lines[j].rstrip("\n") == "---":
                return j
        return heading_idx0  # no rule found: empty separator

    starts0 = [h - 1 for h, _ in heads]
    seps0 = [sep_start(s) for s in starts0]
    index_prefix = "".join(lines[: seps0[0]])
    parts = []
    for idx, (start0, (_, lane)) in enumerate(zip(starts0, heads)):
        end0 = seps0[idx + 1] if idx + 1 < len(heads) else len(lines)
        separator = "".join(lines[seps0[idx] : start0])
        parts.append((lane, separator, "".join(lines[start0:end0])))
    return index_prefix, parts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write docs/lanes/<lane>/DONE-NOTE.md")
    ap.add_argument("--report", action="store_true", help="print the enumeration")
    args = ap.parse_args()

    blobs = distinct_root_blobs()
    rows = []
    for blob, commit, subject in blobs:
        text = git_bytes("cat-file", "-p", blob).decode()
        lanes = [lane for _, lane in authored_notes(text)]
        rows.append((blob, len(text.encode()), lanes, subject))
    rows.sort(key=lambda r: r[1])

    if args.report or not args.write:
        print(f"distinct blobs the root DONE-NOTE.md ever held: {len(rows)}")
        for blob, size, lanes, subject in rows:
            print(f"  {blob[:8]}  {size:>6}B  authors={lanes}  {subject}")
        every = sorted({l for _, _, lanes, _ in rows for l in lanes})
        print(f"distinct lane notes ever at the root path: {len(every)}  {every}")

    # richest blob per lane
    best: dict[str, tuple[str, str]] = {}
    for blob, size, lanes, _ in rows:  # ascending size -> last write wins
        text = git_bytes("cat-file", "-p", blob).decode()
        _, parts = split_blob(text)
        for lane, _sep, body in parts:
            best[lane] = (blob, body)

    # round-trip proof for each source blob we actually take content from
    proofs = []
    for blob in sorted({b for b, _ in best.values()}):
        text = git_bytes("cat-file", "-p", blob).decode()
        index_prefix, parts = split_blob(text)
        rebuilt = index_prefix + "".join(sep + body for _, sep, body in parts)
        ok = hash_object(rebuilt.encode()) == blob
        proofs.append((blob, ok))
        print(f"round-trip {blob[:8]}: {'OK (byte-identical)' if ok else 'MISMATCH'}")
        if not ok:
            return 1

    if args.write:
        root = Path(git("rev-parse", "--show-toplevel").strip())
        for lane, (blob, body) in sorted(best.items()):
            d = root / "docs" / "lanes" / LANE_DIRS[lane]
            d.mkdir(parents=True, exist_ok=True)
            (d / "DONE-NOTE.md").write_text(body)
            print(f"wrote docs/lanes/{LANE_DIRS[lane]}/DONE-NOTE.md  from {blob[:8]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
