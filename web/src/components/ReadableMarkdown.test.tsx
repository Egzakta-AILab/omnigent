import { render, screen, within } from "@testing-library/react";
import { expect, it } from "vitest";
import { ReadableMarkdown } from "./ReadableMarkdown";

it("formats Markdown and safe HTML without loading remote content", () => {
  const content = [
    "## Acceptance criteria",
    "",
    "- [x] Keep the previous verdict",
    "",
    "| Verdict | Evidence |",
    "| --- | --- |",
    "| Ready | `test_review` |",
    "",
    '<p>First line<br />Second line <a href="https://linear.app">Linear</a></p>',
    "",
    '<script>alert(1)</script><a href="javascript:alert(2)">Unsafe link</a>',
    "",
    "![Remote attachment](https://example.com/report.png)",
  ].join("\n");
  const { container } = render(<ReadableMarkdown>{content}</ReadableMarkdown>);
  expect(screen.getByRole("heading", { name: "Acceptance criteria" })).toBeVisible();
  expect(screen.getByRole("checkbox")).toBeChecked();
  expect(screen.getByRole("checkbox")).toBeDisabled();
  expect(within(screen.getByRole("table")).getByText("test_review")).toBeVisible();
  expect(container.querySelector("br")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Linear" })).toHaveAttribute(
    "rel",
    "noopener noreferrer",
  );
  expect(screen.queryByRole("link", { name: "Unsafe link" })).not.toBeInTheDocument();
  expect(screen.getByText("Remote attachment")).toBeVisible();
  expect(container.querySelector("img, script")).toBeNull();
});
