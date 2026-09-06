"""DELIBERATELY FAILING test -- red-proof for the new CI workflow.

This file exists on a scratch branch only. It is never merged. Its whole
purpose is to make the workflow added in this branch report RED, so that the
green run on the real PR means something: a CI that has never been observed
red is decoration.
"""


def test_ci_workflow_can_report_red():
    assert 1 == 2, "deliberate failure -- proving CI reports RED, not a false green"
