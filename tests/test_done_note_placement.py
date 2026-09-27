"""The repo-root ``DONE-NOTE.md`` guard, wired into this repo's own test suite.

Item ``model_performance-sqh``.  ``tools/check_done_note_placement.py`` is the
checker; this module makes it run under plain ``pytest`` (this repo has no CI
workflow and no ``run_tests.sh``, so pytest *is* the build), and pins its
fail-before / pass-after behaviour on scratch repos rather than asserting it.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / "tools" / "check_done_note_placement.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_done_note_placement", CHECKER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check = _load_checker().check


def _scratch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "scratch"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "test")
    (repo / "README.md").write_text("scratch\n")
    run("add", "-A")
    run("commit", "-qm", "init")
    return repo


def test_checker_has_no_env_bypass():
    """No environment variable may switch this guard off.

    Checked against the parsed AST, not a substring of the source -- the module
    *docstring* legitimately contains the words "environment-variable bypass".
    """
    import ast

    tree = ast.parse(CHECKER.read_text())
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
            offenders.append(node.attr)
        if isinstance(node, ast.Name) and node.id in {"environ", "getenv"}:
            offenders.append(node.id)
    assert not offenders, f"guard reads the environment: {offenders}"


def test_this_repo_is_clean():
    """The real repo: no root DONE-NOTE.md, every lane note single-author."""
    problems = check(REPO)
    assert problems == [], "\n".join(problems)


def test_this_repo_has_the_recovered_lane_notes():
    """Regression pin for the notes recovered from git history (item sqh)."""
    for lane in (
        "x7p-protected-tool-results-bug",
        "x1r-tool-result-budget",
        "2o9-clear-at-least",
        "7k2-summary-call-fork",
        "jnt-fork-prefix-capture",
        "rb1-rebase-conflicted-prs",
    ):
        note = REPO / "docs" / "lanes" / lane / "DONE-NOTE.md"
        assert note.is_file(), f"missing recovered note: {note}"
        assert note.read_text().lstrip().startswith("# DONE-NOTE"), lane


def test_fails_on_a_root_done_note(tmp_path):
    """FAIL-BEFORE: a root DONE-NOTE.md is caught."""
    repo = _scratch_repo(tmp_path)
    (repo / "DONE-NOTE.md").write_text("# DONE-NOTE - model_performance-aaa\n")
    problems = check(repo)
    assert problems, "a repo-root DONE-NOTE.md must be a violation"
    assert any("repo root" in p for p in problems), problems


def test_passes_once_the_note_moves_to_its_lane_dir(tmp_path):
    """PASS-AFTER: the same content at docs/lanes/<lane>/ is clean."""
    repo = _scratch_repo(tmp_path)
    d = repo / "docs" / "lanes" / "aaa-some-lane"
    d.mkdir(parents=True)
    (d / "DONE-NOTE.md").write_text("# DONE-NOTE - model_performance-aaa\n")
    assert check(repo) == []


def test_fails_on_two_lanes_concatenated_into_one_file(tmp_path):
    """The shape that made the original loss invisible: one file, two authors."""
    repo = _scratch_repo(tmp_path)
    d = repo / "docs" / "lanes" / "aaa-some-lane"
    d.mkdir(parents=True)
    (d / "DONE-NOTE.md").write_text(
        "# DONE-NOTE - model_performance-aaa\n\nbody\n\n---\n\n"
        "# DONE-NOTE - model_performance-bbb\n\nbody\n"
    )
    problems = check(repo)
    assert any("concatenated" in p for p in problems), problems


def test_fails_when_a_branch_adds_the_root_note(tmp_path):
    """A branch that re-introduces the shared root file is caught by diff."""
    repo = _scratch_repo(tmp_path)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    run("checkout", "-q", "-b", "lane/zzz")
    (repo / "DONE-NOTE.md").write_text("# DONE-NOTE - model_performance-zzz\n")
    run("add", "-A")
    run("commit", "-qm", "oops")
    problems = check(repo)
    assert any("adds" in p or "tracked" in p for p in problems), problems


def test_deleting_the_root_note_on_a_branch_is_allowed(tmp_path):
    """Deleting the shared file is the fix, not a violation."""
    repo = _scratch_repo(tmp_path)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    (repo / "DONE-NOTE.md").write_text("# DONE-NOTE - model_performance-aaa\n")
    run("add", "-A")
    run("commit", "-qm", "shared note")
    run("checkout", "-q", "-b", "lane/fix")
    run("rm", "-q", "DONE-NOTE.md")
    d = repo / "docs" / "lanes" / "aaa-some-lane"
    d.mkdir(parents=True)
    (d / "DONE-NOTE.md").write_text("# DONE-NOTE - model_performance-aaa\n")
    run("add", "-A")
    run("commit", "-qm", "re-home the note")
    assert check(repo) == []


def test_cli_entrypoint_returns_nonzero_on_violation(tmp_path):
    repo = _scratch_repo(tmp_path)
    (repo / "DONE-NOTE.md").write_text("# DONE-NOTE - model_performance-aaa\n")
    p = subprocess.run(
        ["python3", str(CHECKER), "--repo", str(repo), "--verbose"],
        capture_output=True,
        text=True,
    )
    assert p.returncode == 1, p.stdout + p.stderr


def test_cli_entrypoint_returns_zero_on_this_repo():
    p = subprocess.run(
        ["python3", str(CHECKER), "--verbose"], capture_output=True, text=True
    )
    assert p.returncode == 0, p.stdout + p.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
