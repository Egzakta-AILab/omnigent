"""Citation qualification uses changed rows in the captured Git diff."""

import pytest

from omnigent.studio.reviews.evidence import changed_anchors
from omnigent.studio.reviews.report import ReviewFinding, ReviewReport, qualify_citations


def test_old_and_new_paths_and_line_numbers_survive_a_rename():
    patch = (
        'diff --git "a/caf\\303\\251.py" "b/new name.py"\n'
        'rename from "caf\\303\\251.py"\n'
        "rename to new name.py\n"
        '--- "a/caf\\303\\251.py"\n'
        "+++ b/new name.py\t\n"
        "@@ -10,2 +24,3 @@ def handler():\n"
        " unchanged\n"
        "-old\n"
        "+first\n"
        "+second\n"
        "\\ No newline at end of file\n"
    )
    assert changed_anchors(patch) == {
        ("café.py", "base", 11),
        ("new name.py", "head", 25),
        ("new name.py", "head", 26),
    }


def test_context_binary_and_zero_count_sides_are_not_citable():
    patch = (
        "diff --git a/new b/new\n--- /dev/null\n+++ b/new\n@@ -0,0 +1 @@\n+added\n"
        "diff --git a/old b/old\n--- a/old\n+++ /dev/null\n@@ -2 +0,0 @@\n-removed\n"
        "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
    )
    assert changed_anchors(patch) == {("new", "head", 1), ("old", "base", 2)}


@pytest.mark.parametrize(
    "body", ["+added\n", "-removed\n+added\n+extra\n", "diff --git a/other b/other\n"]
)
def test_malformed_or_truncated_hunks_are_refused(body):
    with pytest.raises(ValueError):
        changed_anchors("diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -2 +2 @@\n" + body)


def test_rejected_citations_remain_visible_in_the_original_report():
    finding = ReviewFinding(
        title="Observation",
        detail="Check this behavior.",
        severity="medium",
        path="src/api.py",
        side="head",
        line=2,
    )
    wrong_side = finding.model_copy(update={"side": "base"})
    original = ReviewReport(state="ready", findings=[finding, wrong_side])
    result = qualify_citations(original, frozenset({("src/api.py", "head", 2)}))
    assert result.citation_validation == "checked"
    assert result.findings == [finding]
    assert result.unverified_findings == [wrong_side]
    assert qualify_citations(original, None).citation_validation == "unavailable"
    assert original.findings == [finding, wrong_side]
