import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useLinearConfirmation } from "./useLinearConfirmation";

const preview = {
  linear_workspace: "workspace-A",
  linear_issue_id: "issue-B",
  body: "First line\n  Second line <i>as plain text</i>",
};

function Fixture({
  signal,
  resolved,
}: {
  signal: AbortSignal;
  resolved: (approved: boolean) => void;
}) {
  const { confirm, dialog } = useLinearConfirmation();
  return (
    <>
      <button type="button" onClick={() => void confirm(preview, signal).then(resolved)}>
        Review write
      </button>
      {dialog}
    </>
  );
}

describe("host Linear confirmation", () => {
  it("shows the returned destination and exact plain text before the user approves", async () => {
    const resolved = vi.fn();
    render(<Fixture signal={new AbortController().signal} resolved={resolved} />);
    fireEvent.click(screen.getByText("Review write"));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Workspace workspace-A · Issue issue-B");
    expect(dialog.querySelector("pre")?.textContent).toBe(preview.body);
    expect(dialog.querySelector("i")).toBeNull();
    expect(resolved).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Post comment" })));
    expect(resolved).toHaveBeenCalledExactlyOnceWith(true);
  });
  it("closes a cancelled iframe request without issuing approval", async () => {
    const controller = new AbortController();
    const resolved = vi.fn();
    render(<Fixture signal={controller.signal} resolved={resolved} />);
    fireEvent.click(screen.getByText("Review write"));
    await act(async () => controller.abort());
    expect(resolved).toHaveBeenCalledExactlyOnceWith(false);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

import { studioHostMethods } from "./studio";

it.each(["status", "assignee"] as const)(
  "confirms the exact %s change through the trusted host",
  async (change) => {
    const params = { task_id: "task", request_id: "unique", change, value: "new-id" };
    const canonical = { ...preview, change, value: "new-id", body: `Set ${change} to new-id` };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(Response.json(canonical))
      .mockResolvedValueOnce(Response.json(canonical))
      .mockResolvedValueOnce(Response.json({ confirmation_token: "server-ticket" }))
      .mockResolvedValueOnce(
        Response.json({ linear_issue_id: preview.linear_issue_id, change, value: "new-id" }),
      );
    const receipt = vi.fn();
    const signal = new AbortController().signal;
    function Host() {
      const { confirm, dialog } = useLinearConfirmation();
      const invoke = studioHostMethods("example.work", fetch, confirm)[
        "studio.tasks.syncLinearConfirmed"
      ];
      return (
        <>
          <button type="button" onClick={() => void invoke(params, signal).then(receipt, receipt)}>
            Request update
          </button>
          {dialog}
        </>
      );
    }
    render(<Host />);
    fireEvent.click(screen.getByText("Request update"));
    expect(await screen.findByText(canonical.body)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(fetch).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText("Request update"));
    const approve = await screen.findByRole("button", { name: "Apply change" });
    await act(async () => fireEvent.click(approve));
    expect(receipt).toHaveBeenLastCalledWith({
      linear_issue_id: preview.linear_issue_id,
      change,
      value: "new-id",
    });
    expect(JSON.parse(fetch.mock.calls[3][1].body)).toEqual({
      ...params,
      confirmation_token: "server-ticket",
    });
  },
);
