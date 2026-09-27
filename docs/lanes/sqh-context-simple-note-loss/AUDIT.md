# AUDIT — repo-root `DONE-NOTE.md` loss in `amplifier-module-context-simple`

**Item:** `model_performance-sqh`. **Method:** git only, no lane self-report
trusted — the same method item `model_performance-kez` proved in the evals repo
(`probes/kez-done-note-collision/AUDIT.md`), re-run here.
**Repo state audited:** `origin/main` = `e9ac159`, fetched 2026-09-02.

## Headline

**The count in the item is wrong, and the real damage has a second, worse half.**

The item was filed as *"8 lanes' DONE-NOTEs deleted from origin/main by the
revert"*, listing `2o9, 6da, 7k2, cb2, jnt, q69, wxs, x1r`. That list came from
grepping `model_performance-[a-z0-9]+` over the file, which counts **mentions in
prose**, not authorship. Measured by `# DONE-NOTE … model_performance-<id>`
headings — the only thing that marks a note as *written by* a lane:

| | count |
|---|---|
| Distinct blobs the root path ever held (all refs + unreachable objects) | **11** |
| Distinct lane notes ever written at the root path | **6** |
| Still on `origin/main` today | **1** (`x7p`) |
| **Deleted from `main` by the `e9ac159` revert** | **4** (`x1r`, `2o9`, `7k2`, `jnt`) |
| **Never reached `main` at all** (silently overwritten out of the lineage) | **1** (`rb1`) |
| **UNRECOVERABLE** | **0** |

`6da`, `cb2` and `wxs` are **evals-repo** lanes; they never wrote a note in this
repo and are accounted for in `kez`'s audit there. `q69` and `l8` targeted this
repo but wrote **no DONE-NOTE anywhere in its object store**. Naming them as
victims here would have been a fabricated loss.

The `rb1` half is the part the item did not know about and is the more dangerous
one: a revert is at least *visible in the log*. `rb1`'s note was lost with no
event at all — see below.

## Method (reproduce)

```bash
git fetch origin
# 1. every commit that ever touched the root path, across ALL refs
git log --all --full-history --oneline -- DONE-NOTE.md        # 20 commits
# 2. blobs + AUTHORED notes per blob, including UNREACHABLE objects
python3 docs/lanes/sqh-context-simple-note-loss/recover_root_done_notes.py --report
# 3. cross-check the object store directly
git fsck --lost-found
git rev-list --objects --all | grep -i done-note
```

Two things make the naive walk under-count, and both bit here:

* `--full-history` matters: plain `git log origin/main -- DONE-NOTE.md` shows
  **6** commits, the `--all --full-history` walk shows **20**, because history
  simplification prunes the losing side of a merge. That pruning is exactly what
  hides `rb1`.
* **Reachability matters too.** The `--all` walk finds 10 blobs; a sweep of every
  *repo-root tree in the object store* finds an **11th** (`231979c1`) that no ref
  reaches. The recovery script therefore does both passes. Enumerating from
  reachable history alone would have silently missed it.

## The eleven blobs

Ascending by size. "authors" = `# DONE-NOTE … model_performance-<id>` headings
actually present in that blob.

| blob | bytes | authors | first commit seen |
|---|---|---|---|
| `ecc7cccb` | 14,989 | `x7p` | `d5ded0c` / restored by `e9ac159` — **== `origin/main` today** |
| `52052886` | 17,073 | `x1r` | `1f59edc` (x1r's standalone note, pre-merge) |
| `d5ec4350` | 17,158 | `x1r` | `afce8ff` |
| `5bfde271` | 32,657 | `x7p`, `x1r` | `fca37bd` |
| `231979c1` | 32,269 | `x7p`, `x1r` | **unreachable object** — see below |
| `4b822d72` | 32,742 | `x7p`, `x1r` | `49e2799` (PR #21) |
| `be96dd2b` | 42,979 | `x7p`, `x1r`, **`rb1`** | `56270a1` — **never an ancestor of `main`** |
| `0268ee62` | 48,022 | `x7p`, `x1r`, `2o9` | `2c42faa` |
| `412d6122` | 48,173 | `x7p`, `x1r`, `2o9` | `f851d12` (PR #26) |
| `0bd7671a` | 61,868 | `x7p`, `x1r`, `2o9`, `7k2` | `a877b36` (PR #27) |
| `c790066d` | 71,165 | `x7p`, `x1r`, `2o9`, `7k2`, `jnt` | `5cdbc62` (PR #28) — richest |

`231979c1` is reachable from no ref; it survives only as a dangling tree
(`c17df59c`, a snapshot of the `q69` lane's worktree — it carries
`tests/test_token_meter_hybrid.py`). It is a **mid-conflict snapshot**: it still
contains git's own markers. The complete set of lines it holds that
`c790066d` does not:

```
$ git diff c790066d 231979c1 | grep '^+' | grep -v '^+++'
+<<<<<<< HEAD
+||||||| parent of 1f59edc (docs: README section + DONE-NOTE for the tool-result budget and spill)
+=======
+>>>>>>> 1f59edc (docs: README section + DONE-NOTE for the tool-result budget and spill)
```

Four conflict markers and **no note content whatsoever**, so nothing is
recoverable from it — but the only way to know that was to find it and read it.
Note also that it is the add/add conflict the shared file's own header comment
predicted, frozen mid-resolution.

Every mainline transition is a **pure append** (`git diff --numstat`:
`343/0`, `267/0`, `217/0`, `174/0` — zero deletions), so `c790066d` is a strict
superset of every other mainline revision. `be96dd2b` is the one blob that is
**not** on that chain.

## What happened, in two distinct failures

**1. The revert (visible).** `e9ac159`, *"revert: unproven default-off features
per merge policy (wins only) (#30)"*, restored the root file to `ecc7cccb` —
`x7p`'s note alone. Four lanes' notes (`x1r`, `2o9`, `7k2`, `jnt`) left `main`
as collateral of a *code* revert, because the notes lived in a file the feature
PRs also touched. `git ls-tree -r origin/main | grep -i done-note` returns only
that root file, so those four exist nowhere else on `main`.

**2. The silent overwrite (invisible).** `rb1` committed its note at `56270a1`
on top of a tree whose root file held `x7p` + `x1r` (`4b822d72`). Meanwhile the
`2o9` lane branched from the *same* base and appended its own note, producing
`0268ee62`. `2o9`'s line landed; `rb1`'s did not. Because both are ordinary
writes to the same path from a common ancestor, git raised **nothing**:

```
$ git merge-base --is-ancestor 56270a1 origin/main; echo $?
1                       # rb1's note has never been on main
$ git for-each-ref --contains 56270a1 --format='%(refname)'
refs/heads/lane/rb1-rebase-conflicted-prs
refs/remotes/origin/lane/rb1-rebase-conflicted-prs
```

`rb1`'s work (rebasing and landing PRs #21 and #24) *is* on `main`. Only its
record of that work was dropped. This is `kez`'s exact shape, one repo over.

## Recovery — provenance and proof

| recovered file | source blob | slice |
|---|---|---|
| `docs/lanes/x7p-protected-tool-results-bug/DONE-NOTE.md` | `c790066d` | lines 19–323 |
| `docs/lanes/x1r-tool-result-budget/DONE-NOTE.md` | `c790066d` | lines 326–651 |
| `docs/lanes/2o9-clear-at-least/DONE-NOTE.md` | `c790066d` | lines 654–916 |
| `docs/lanes/7k2-summary-call-fork/DONE-NOTE.md` | `c790066d` | lines 919–1132 |
| `docs/lanes/jnt-fork-prefix-capture/DONE-NOTE.md` | `c790066d` | lines 1135–1305 |
| `docs/lanes/rb1-rebase-conflicted-prs/DONE-NOTE.md` | `be96dd2b` | lines 652–844 |

For each lane the **richest** blob containing its note is used, so every note is
its final revision, not an earlier draft.

**Round-trip proof that the split is lossless.** The splitter captures each
`---` separator verbatim and reassembles index + separators + bodies; the result
must hash to the original object:

```
$ python3 docs/lanes/sqh-context-simple-note-loss/recover_root_done_notes.py --report
round-trip be96dd2b: OK (byte-identical)
round-trip c790066d: OK (byte-identical)
```

Not one byte was dropped, edited or reflowed. The index table and the file's
header comment — the only part of the shared file that belongs to no single lane
— are preserved verbatim in `RECOVERED-index-header.md` beside this audit, and
restated as a live index in `docs/lanes/README.md`.

**Cross-check against the standalone revisions.** `x7p`'s and `x1r`'s notes also
existed as standalone root blobs before the sharing began. The recovered files
differ from those blobs by **trailing blank lines only** (`x7p`: +1 line,
`x1r`: +2), which is the padding the shared file inserted above each `---`:

```
$ diff <(git cat-file -p ecc7cccb) docs/lanes/x7p-protected-tool-results-bug/DONE-NOTE.md
304a305
> 
```

## Unrecoverable

**None.** Stated as a positive claim with its bound: every revision the root path
ever held is one of the ten blobs above; all ten are reachable from a ref in this
repo; every authored note in every one of them is now filed under the lane that
wrote it. Two independent completeness checks back this:

* `git log --all --full-history -- DONE-NOTE.md` → 20 commits → 10 reachable blobs.
* A sweep of **every repo-root tree in the object store**
  (`git cat-file --batch-all-objects`) → those 10 plus the unreachable
  `231979c1` = **11**, and no twelfth. `git fsck --lost-found` independently
  reports 3 dangling commits and 6 dangling trees; the only root-note blobs they
  carry are `ecc7cccb`, `4b822d72` and `231979c1`, all accounted for. No
  orphaned lane note exists in the object store.

**Bound:** this covers what git can see in *this* repo. A lane that wrote a note
and never committed it is outside git's reach and outside this audit. Of the ten
lanes that targeted this repo (`manifest.tsv`), `q69` and `l8` committed no
DONE-NOTE anywhere; that is an absence of evidence, not a recovered loss, and it
is reported as such rather than counted as damage.

## One adjacent finding, not fixed here

`origin/lane/pmt-fork-span-predicate` carries
`probes/pmt-fork-span-predicate/DONE-NOTE.md` (blob `37b8fc5e`) — a per-lane
note, but under a `probes/` directory that exists in no other lane of this repo.
`artifact-path/v1` (item `6x4`) resolves this repo to `docs/lanes/<lane>/`, so
`tools/check_done_note_placement.py` will flag that path if that branch merges.
That is the guard working as designed; it is called out in the PR body so it is
not a surprise. `pmt`'s note is on its own branch and is **not** at risk — it is
outside the root-file collision this item covers, and moving it belongs to that
lane's PR, not this one.

## Why it stayed silent, and what changed

The root file was **structurally unable to raise an alarm**: two lanes writing
the same path is not a conflict, and a revert of a code PR that also touched that
file is an ordinary, correct revert. Three changes, in the order that matters:

1. **The shared path is gone.** Root `DONE-NOTE.md` is deleted in this PR. The
   failure mode has no surface left to occur on.
2. **The instruction already points elsewhere.** `artifact-path/v1` names
   `docs/lanes/<lane>/` for this repo, so new lanes are told a valid path — the
   ambiguity that produced six root writes is closed.
3. **A check now watches, *in this repo*.**
   `tools/check_done_note_placement.py`, run by `tests/test_done_note_placement.py`
   under plain `pytest` (this repo has no CI workflow and no `run_tests.sh`, so
   pytest is the build). It fails on a root `DONE-NOTE.md` present, tracked, or
   added/modified on a branch, and on two lanes' notes concatenated into one
   file. It has no environment-variable bypass, and deleting the root file is
   explicitly allowed. Fail-before / pass-after is **proven on scratch repos** in
   the test module, not asserted.

Change 3 is the one that would have caught this on day one — and the reason it
did not is that `kez`'s guard was added only to the evals repo. That is the
generalisable lesson: **a guard that lives in one repo does not protect the nine
others the same lanes write to.**
