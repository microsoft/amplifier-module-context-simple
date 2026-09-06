# DONE-NOTE — `model_performance-sqh`

**Subject:** `kez` recurs in `amplifier-module-context-simple` — lane DONE-NOTEs
lost from the shared repo-root `DONE-NOTE.md`. Enumerate, recover, re-home,
guard.

**Spend: $0.** No API call, no DTU, no infrastructure created, nothing to tear
down. The whole item is a git-history read plus a checker; the spend authority
was $0 and none was used.

## Headline

**The item's count was wrong, and the real damage has a second half nobody knew
about.** The item said *8 lanes' notes were deleted from `origin/main` by the
`#30` revert*. Measured by authorship rather than by prose mentions:

* the repo-root path ever held **11 distinct blobs** (10 reachable + 1 that no
  ref reaches) carrying **6 distinct lane notes**;
* the `#30` revert deleted **4** of them from `main` (`x1r`, `2o9`, `7k2`,
  `jnt`) — not 8;
* a **5th** (`rb1`) was never on `main` at all. It was silently overwritten out
  of the lineage *before* the revert, and survives only on
  `origin/lane/rb1-rebase-conflicted-prs`. **This is the worse failure** — a
  revert is at least visible in the log; this one produced no event at all;
* **0 unrecoverable.** All six notes are re-homed, with a byte-identical
  round-trip proof.

The four names in the item that are not in my list (`6da`, `cb2`, `wxs`, `q69`)
are not victims: `6da`/`cb2`/`wxs` are **evals-repo** lanes already handled by
`kez` and never wrote a note here, and `q69` (like `l8`) committed no DONE-NOTE
anywhere in this repo's object store. Reporting them as lost would have been a
fabricated loss.

Full evidence: [`AUDIT.md`](AUDIT.md).

## Deliverable ledger

| # | deliverable | state |
|---|---|---|
| 1 | Per-blob enumeration from history, with the lane each belonged to, and a **verified** count | **DONE** — `AUDIT.md` §"The eleven blobs"; reproducible with `recover_root_done_notes.py --report`. Verified, and the item's "8" corrected to 6 notes / 4 revert-deleted. |
| 2 | DRAFT PR, **purely additive** (notes only, no reverted feature code), re-homing every recoverable note | **DONE** — see "Purely additive" below. |
| 3 | Repo-root `DONE-NOTE.md` guard, with a test, adapted to this repo's test setup | **DONE** — `tools/check_done_note_placement.py` + `tests/test_done_note_placement.py`, 10 tests, run by plain `pytest`. |
| 4 | Any unrecoverable note named explicitly | **DONE** — **none**, stated as a positive claim with its bound (`AUDIT.md` §Unrecoverable). |
| 5 | This DONE-NOTE, in the PR body | **DONE** — reproduced in the PR body verbatim. |

## The method, and the two ways it under-counts

Ported from `kez`'s proven method (evals repo,
`probes/kez-done-note-collision/AUDIT.md`), plus one addition this repo forced:

1. `git log --all --full-history -- DONE-NOTE.md` → **20** commits. Plain
   `git log origin/main -- DONE-NOTE.md` shows only **6**: history
   simplification prunes the losing side of a merge, and `rb1` is exactly what
   that prunes.
2. **Reachability is not enough.** The `--all` walk yields 10 blobs; sweeping
   every *repo-root tree in the object store* yields an **11th** (`231979c1`)
   that no ref reaches — a dangling snapshot of the `q69` worktree, frozen
   mid-conflict, still carrying `<<<<<<<` markers. It turned out to hold no
   unique note content (the complete set of lines it has that the richest blob
   lacks is four conflict markers), **but the only way to know that was to find
   it and read it.** An enumeration from reachable history alone would have
   silently missed it and still looked complete.
3. Authorship ≠ mention. Only `# DONE-NOTE … model_performance-<id>` headings
   count. Grepping item ids over the prose is what produced "8 lanes"; it counts
   every lane another lane's note happens to cite.

## Purely additive — what this PR does and does not do

This repo is under a **wins-only** merge policy, and `#30` reverted these
features deliberately. So, explicitly:

* **No reverted feature code is re-introduced.** The diff touches
  `docs/lanes/**`, `tools/check_done_note_placement.py` and
  `tests/test_done_note_placement.py`. `amplifier_module_context_simple/` is
  **untouched**; the notes describe code that is *not* being restored.
* **The one deletion is the shared root note file itself** —
  `DONE-NOTE.md` → `docs/lanes/x7p-protected-tool-results-bug/DONE-NOTE.md`,
  which git records as a rename. Its content is preserved in full: `x7p`'s note
  moves to `x7p`'s directory, and the index/header block that belongs to no
  lane is kept verbatim in `RECOVERED-index-header.md`. Nothing is dropped.
  Deleting it is required by the item's acceptance criteria (`git ls-tree -r
  origin/main | grep -i DONE-NOTE` must not list a root file) and is the change
  that removes the collision surface altogether.

## Evidence

**Round-trip proof the split is lossless.** The splitter captures each `---`
separator verbatim and reassembles index + separators + bodies; the result must
hash back to the original git object:

```
$ python3 docs/lanes/sqh-context-simple-note-loss/recover_root_done_notes.py --report
distinct blobs the root DONE-NOTE.md ever held: 11
distinct lane notes ever at the root path: 6  ['2o9', '7k2', 'jnt', 'rb1', 'x1r', 'x7p']
round-trip be96dd2b: OK (byte-identical)
round-trip c790066d: OK (byte-identical)
```

**Test suite: 87 → 97 passing, 0 failing.**

```
$ uv run pytest -q
97 passed in 5.15s
```

The 10 new tests are the guard. Fail-before / pass-after is **proven on scratch
repos inside the test module**, not asserted: a root `DONE-NOTE.md` fails, the
same content at `docs/lanes/<lane>/` passes, two lanes concatenated into one
file fails, a branch that *adds* the root file fails, and a branch that
*deletes* it passes (deleting is the fix, not a violation).

**The guard on this working tree:**

```
$ python3 tools/check_done_note_placement.py --verbose
  root DONE-NOTE.md: absent
  branch diff vs e9ac159a for DONE-NOTE.md: D  DONE-NOTE.md
  docs/lanes/2o9-clear-at-least/DONE-NOTE.md: authors=['2o9']
  … one author per file, six files …
OK -- no repo-root DONE-NOTE.md; every lane note is single-author and in place.
```

## Decisions taken without waiting (per SCOPE-OUTS)

1. **Artifact root.** `GOAL.md` named `probes/sqh-context-simple-note-loss/` and
   described this worktree as "the evals repo". It is not — it is a checkout of
   `microsoft/amplifier-module-context-simple` with a live `origin`. I used
   `artifact-path/v1` (item `6x4`) resolved against *this* repo — R1 does not
   apply (no top-level `probes/`), so R3 gives **`docs/lanes/<lane>/`**, which is
   also what the acceptance criteria name and what every other goal file for
   this repo states. Creating a `probes/` tree here would have been precisely the
   per-lane improvisation `6x4` measured and rejected.
2. **Lane directory naming.** `<lane>` is the full lane id (`2o9-clear-at-least`,
   not `2o9`), taken from the real branch names in this repo and from
   `manifest.tsv` — not invented.
3. **Which revision of each note.** For each lane, the **richest** blob
   containing its note, so every note is its final revision rather than an
   earlier draft.
4. **`pmt` left alone.** `origin/lane/pmt-fork-span-predicate` carries
   `probes/pmt-fork-span-predicate/DONE-NOTE.md` — a per-lane note, but under a
   `probes/` directory no other lane here uses. The new guard **will flag that
   path if that branch merges**. That is the guard working as designed and it is
   called out in the PR body so it is not a surprise; `pmt`'s note is not at risk
   and moving it belongs to that lane's PR, not this one.
5. **Publication.** `GOAL.md`'s publication block assumed an eval lane with no
   remote. This repo has one and the item asks for a draft PR, so publication is
   `required: true` and the marker carries values read back from the remote.

## What is NOT claimed

* Not claimed: that the guard would have prevented the loss retroactively. It is
  the **last** line of defence. Removing the shared path (this PR) and fixing the
  instruction (`6x4`) are the first two, and they mean the guard should never
  have anything to catch.
* Not claimed: that nothing was lost outside git. A lane that wrote a note and
  never committed it is outside git's reach and outside this audit. Of the ten
  lanes that targeted this repo, `q69` and `l8` committed no note — an absence of
  evidence, reported as such, not counted as damage.
* Not claimed: that the reverted features should return. This PR takes no
  position on the wins-only revert and restores no feature code.

## What remains open

1. **The generalisable one.** `kez` added this guard to the evals repo only;
   that is exactly why it recurred here unseen. Nine other repos these lanes
   write to still have no such check. Porting it repo-by-repo (this item is one)
   does not scale — a shared, installable check would.
2. `rb1`'s note is now on `main` for the first time, but **`rb1`'s branch still
   carries the root file** (blob `be96dd2b`). If that branch merges after this
   PR, the guard fails it — correctly, and it will need a trivial rebase that
   drops the root file. Same for `2o9`, `7k2`, `jnt`, `pmt`, `l8`, `x1r`.
3. Nothing checks that a *newly landed* lane actually wrote a note at all;
   `q69` and `l8` landed none and no one noticed until this audit.
