import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ReviewDiff } from "./ReviewDiff";

vi.mock("@pierre/diffs/react", () => ({ FileDiff: () => null }));

it("shows the exact saved line when a renamed Git-quoted path cannot be located by the viewer", () => {
  const patch =
    'diff --git "a/caf\\303\\251.py" "b/new name.py"\n' +
    'rename from "caf\\303\\251.py"\nrename to new name.py\n' +
    '--- "a/caf\\303\\251.py"\n+++ b/new name.py\t\n' +
    "@@ -10,2 +24,2 @@\n unchanged\n-removed\n+added\n";
  render(
    <ReviewDiff
      patch={patch}
      truncated={false}
      source={{
        prUrl: "https://github.com/example/repo/pull/1",
        baseSha: "1".repeat(40),
        headSha: "2".repeat(40),
        path: "café.py",
        side: "base",
        line: 11,
      }}
      sourceLine="removed"
      onClearSource={() => {}}
    />,
  );
  expect(screen.getByRole("status")).toHaveTextContent("Review finding · café.py:11");
  expect(screen.getByText("−removed")).toBeVisible();
});
