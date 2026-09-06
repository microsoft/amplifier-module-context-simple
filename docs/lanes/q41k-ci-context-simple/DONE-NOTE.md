# DONE-NOTE — lane `q41k-ci-context-simple`

**Item:** `model_performance-q41k` (project `model_performance`)
**Outcome branch:** **A — RESOLVED.** Every deliverable is DONE. Nothing was
cap-bound, nothing was blocked.
**Date:** 2026-09-06
**Repo:** `microsoft/amplifier-module-context-simple`, branch
`lane/q41k-ci-context-simple`, draft PR
[#36](https://github.com/microsoft/amplifier-module-context-simple/pull/36)

---

## Result in one paragraph

`amplifier-module-context-simple` had **no `.github/workflows/` at all** — the
nine PRs this program merged there (#26–#34) were verified by lane-run suites
only, never by GitHub CI. This lane added `.github/workflows/ci.yml`
(`ruff check` + `pytest` on py3.11/3.12, ubuntu-latest, on push-to-main and
pull_request), modelled on `provider-openai`'s workflow, and **proved it can go
red before letting it go green**. The pre-existing suite is green in CI on clean
main — no fix of any kind was needed, so the PR touches no module code. One
finding is reported rather than papered over: `ruff format --check` fails on
clean main and is deliberately **not** wired in (see §4).

---

## 1. Deliverables

| # | Deliverable | State |
|---|---|---|
| 1 | `.github/workflows/ci.yml` on push-to-main + pull_request, shaped like the sibling module repo's, invoking the repo's own entry points | **DONE** — `f1de188` |
| 2 | RED run URL and GREEN run URL both quoted in the real PR body | **DONE** — both in [#36](https://github.com/microsoft/amplifier-module-context-simple/pull/36) |
| 3 | Workflow file only (+ tiny fix only if genuinely needed); STOP and report if clean main is red | **DONE** — **no fix was needed**; suite green in CI unmodified. PR also carries this lane's own `docs/lanes/q41k-ci-context-simple/` artifacts, as procedure 4 requires. No module code touched. |
| 4 | README badge only if the siblings have one | **DONE (correctly: none added)** — neither `provider-openai` nor `provider-anthropic` has a badge on main. Not introducing a convention this family does not use. |
| 5 | State what was adapted and why (test dir, extras, matrix, Makefile) | **DONE** — §3 below, and in the PR body |

---

## 2. The red-then-green evidence

### RED — run [34061819623](https://github.com/microsoft/amplifier-module-context-simple/actions/runs/34061819623)

Scratch branch `scratch/q41k-ci-red-proof` = this workflow + one deliberately
failing test (`assert 1 == 2`), opened as draft PR
[#35](https://github.com/microsoft/amplifier-module-context-simple/pull/35).

| Job | Conclusion | Detail |
|---|---|---|
| `ruff check` | success | |
| `pytest (py3.11)` | **failure** | `1 failed, 102 passed, 1 xfailed in 9.38s` |
| `pytest (py3.12)` | **failure** | `1 failed, 102 passed, 1 xfailed in 9.08s` |

Head sha `d05bca57dba840fce68458d2f676ae313d1b236f`. Run conclusion `failure`.

**Why the shape of this result is the actual proof, not just the red X:** the
102 real tests ran *alongside* the deliberate failure. That rules out the whole
failure class this gate exists to catch — a wrong path filter, a wrong test
dir, a selection that silently matches nothing, or a step whose exit code is
swallowed. Any of those would also have produced a plausible-looking green on
the real PR.

PR #35 is **closed**; branch `scratch/q41k-ci-red-proof` is **deleted from
origin** (`git ls-remote --heads origin 'scratch/*'` returns empty). The run
JSON and the failing-step log are preserved at `evidence/red-run.json` and
`evidence/red-run-failed-log.txt`.

### GREEN — run [34061907688](https://github.com/microsoft/amplifier-module-context-simple/actions/runs/34061907688)

Same workflow, real PR #36, head `f4662cfc961cc470530ade3c6fbefce647be224d`:
`ruff check` ✅, `pytest (py3.11)` ✅, `pytest (py3.12)` ✅ — run conclusion
`success`. Preserved at `evidence/green-run.json`. The PR body additionally
quotes the run for the PR's final head commit.

---

## 3. What was adapted, and why

Template: `microsoft/amplifier-module-provider-openai/.github/workflows/ci.yml`
(named in the item; green on main). `provider-anthropic`'s was read as the
second reference.

**Kept identical to the template:** trigger set (`push: branches: [main]` +
`pull_request`), `actions/checkout@v4`, `astral-sh/setup-uv@v5` with
`python-version` from the matrix, `uv sync --all-extras --dev`,
`uv run pytest -q`, `timeout-minutes: 10`, `fail-fast: false`, and the
single-OS `["3.11", "3.12"]` matrix.

| Question the goal asked | Answer, measured |
|---|---|
| **Test dir** | `tests/` — `pyproject` already declares `testpaths = ["tests"]`, `addopts = "--import-mode=importlib"`, `asyncio_mode = "strict"`. Bare `uv run pytest -q` inherits all of it; nothing re-specified in the workflow. |
| **Extras** | `uv sync --all-extras --dev` unchanged. Dev group = `amplifier-core` (git source, `branch = main`), `pytest>=9.0.3`, `pytest-asyncio>=0.23.0`. `uv.lock` is committed, so resolution is pinned. |
| **Matrix** | `ubuntu-latest` only, matching the `provider-openai` template. `provider-anthropic` runs ubuntu+macos+windows, but it ships OS-sensitive provider I/O; context-simple is pure in-memory message-list manipulation with **zero runtime dependencies** (`dependencies = []`). A 3-OS matrix here is 3× cost for no signal. This also avoids inventing a broader matrix than the template. |
| **Makefile target** | **There is no Makefile** in context-simple — nor in either sibling. So the repo's own entry point *is* `pytest` under its committed `pyproject` config, and that is what CI invokes. Nothing to prefer over it. |
| **API key placeholder** | **Not needed** — deviation from `provider-openai`, which exports a dummy `OPENAI_API_KEY` because its `mount()` refuses to construct without one. Measured here: `env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u GOOGLE_API_KEY -u AZURE_OPENAI_API_KEY uv run pytest -q` → `102 passed, 1 xfailed`. context-simple's structural/behavioral tests inherit from `amplifier-core` and mount no live provider. |
| **`-m "not live"` deselection** | **Not needed** — deviation from `provider-openai`. This repo registers no `live` marker and has no network-dependent test. Copying the deselection would have silently narrowed the selection, which is precisely the decorative-CI failure mode. |
| **README badge** | **Not added.** `gh api .../README.md?ref=main` on both siblings: no badge. |

Local baselines taken before writing the workflow: py3.12 `102 passed,
1 xfailed in 5.27s`; py3.11 `102 passed, 1 xfailed in 5.42s`.

---

## 4. Finding: `ruff format --check` is red on clean main — reported, not papered over

Measured on clean main with `uvx ruff@0.14.10`:

- `ruff check .` → **`All checks passed!`** → **wired into CI** as its own `lint` job.
- `ruff format --check .` → **`5 files would be reformatted, 10 files already formatted`**:
  - `amplifier_module_context_simple/__init__.py`
  - `tests/test_compaction_storm_irreducible_floor.py`
  - `tests/test_compaction_trigger_provenance.py`
  - `tests/test_token_meter.py`
  - `docs/lanes/fwut-compaction-storm/evidence/pr33_recompute.py`

**Not wired in, deliberately.** No repo in this module family carries a
`[tool.ruff]` section or a `ruff.toml` — checked across all 13
`amplifier-module-*` checkouts, plus `provider-openai`'s tree (it pins
`ruff>=0.14.10` in its dev group yet never invokes it in CI). So
`format --check` would enforce ruff's **default line-length-88** on code that
was never written to it, and CI would be **red on clean main on day one**.

The goal's rule is explicit and was followed: do not add `continue-on-error`,
do not narrow the selection to force green — state it. Adopting a shared ruff
format config is a **family-wide decision**, not something one CI lane should
impose on one repo by side effect. The measured numbers and this reasoning are
in the workflow file itself, so the next contributor need not rediscover them.

Reproducibility note: the lint step runs the version-pinned
`uvx ruff@0.14.10 check .`, so a developer reproduces CI's lint byte-for-byte
without adding a dev dependency, and CI/local cannot drift on a floating
version.

---

## 5. Spend ledger

**Authority: $0.00** — arithmetic as stated in the goal: `0 runs × 0 arms ×
$0 / 1.00 = $0.00`, slack $0.00. Workflow YAML plus GitHub-hosted CI minutes;
no API calls, no DTU, no containers.

| Category | Authorized | Spent | Note |
|---|---|---|---|
| LLM API | $0.00 | **$0.00** | no model calls made by this lane's work |
| DTU / containers | $0.00 | **$0.00** | none created |
| Infrastructure rows registered | — | **none** | nothing to tear down; `lane_teardown.sh` not run, `infra_ledger.sh` not touched |
| GitHub Actions minutes | in scope | 4 runs × 3 jobs, all <2 min | on `microsoft/*`, ubuntu-latest |

The arithmetic closes: the deliverable required no purchasable unit, so a $0
authority funds it completely. Residue: $0.00, and the smallest useful purchase
this lane could have needed was also $0.00. **No cap-bound deliverable exists**
— outcome branch **A**, not B.

---

## 6. Deviations, choices made without asking, and scope

1. **Two jobs, not one** (`lint` + `test`), where the template has only `test`.
   Rationale: the goal's task line asks for lint *and* tests; separating them
   makes a lint failure and a test failure distinguishable at a glance. The
   `test` job is byte-shaped like the template.
2. **`ruff check` included, `ruff format --check` excluded.** §4. This is the
   one place the goal's literal wording ("lint (`ruff check` + `ruff format
   --check`)") could not be satisfied in full without shipping a CI that is red
   on clean main — which the same goal forbids, twice. Chose the STOP-AND-REPORT
   rule over the wording, and recorded it here and in the PR body.
3. **The PR carries this lane's `docs/lanes/q41k-ci-context-simple/` artifacts**
   in addition to the workflow. Procedure 4 requires lane artifacts at the
   ARTIFACT ROOT, and the repo already carries `docs/lanes/fwut-.../` from an
   earlier lane. "Workflow file only" is honored where it matters: **no module
   code and no test code is modified**.
4. **The repo-root `DONE-NOTE.md` was not created, touched, or read into this
   work** (scope-out, item kez). This note lives at
   `docs/lanes/q41k-ci-context-simple/DONE-NOTE.md`.
5. **Nothing merged; nothing outside this repo edited.** PR #36 is a draft,
   marked ready once green. The merge is the manager's stage.

---

## 7. What remains open (for the next reader)

1. **Three sibling repos still have no CI.** The sweep that motivated this item
   found 4 workflow-less repos; this closes one quarter of that gap.
2. **The module family has no shared ruff/format convention.** Five files here
   are off ruff's default format, and there is no config anywhere in the family
   to say whether that is a defect or the intent. Worth one family-wide decision
   (adopt a `[tool.ruff]` with an agreed line-length, then turn
   `format --check` on everywhere at once), not thirteen local ones.
3. **`amplifier-core` is pulled from `branch = main`** in the dev group. `uv.lock`
   pins it today, so CI is reproducible — but a lock refresh can import an
   upstream break into this repo's CI without any change to this repo. Not
   acted on: out of scope for a CI-wiring lane, and no evidence it has bitten.
