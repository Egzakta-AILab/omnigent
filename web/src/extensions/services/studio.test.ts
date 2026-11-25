import { describe, expect, it, vi } from "vitest";
import { studioHostMethods } from "./studio";

const comment = { task_id: "task", request_id: "action", body: "Check the preview." };
const preview = { linear_workspace: "workspace-1", linear_issue_id: "issue-1", body: comment.body };
const endpoint = "/v1/extensions/acme.work/studio/v1/";
const signal = new AbortController().signal;

describe("Studio v1 extension transport", () => {
  it("binds the extension identity, scope and method to the host catalog", async () => {
    const fetch = vi.fn().mockResolvedValue(Response.json({ items: [] }));
    const methods = studioHostMethods("acme.work", fetch, vi.fn());
    const parameters = { limit: 10 };
    await expect(methods["studio.tasks.list"](parameters, signal)).resolves.toEqual({ items: [] });
    expect(fetch).toHaveBeenCalledWith(endpoint + "tasks/list", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parameters),
      signal,
    });
    expect(Object.keys(methods)).not.toContain("studio.tasks.confirmLinear");
  });

  it("requires host confirmation for the exact preview before writing to Linear", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(Response.json(preview))
      .mockResolvedValueOnce(Response.json({ confirmation_token: "server-only" }))
      .mockResolvedValueOnce(Response.json({ comment_id: "comment-1" }));
    const approve = vi.fn().mockResolvedValue(true);
    const methods = studioHostMethods("acme.work", fetch, approve);
    await expect(
      methods["studio.tasks.syncLinearConfirmed"](
        { ...comment, confirmation_token: "forged", token: "forged", confirmed: true },
        signal,
      ),
    ).resolves.toEqual({ comment_id: "comment-1" });
    expect(approve).toHaveBeenCalledWith(preview, signal);
    expect(fetch.mock.calls.map(([url]) => url)).toEqual(
      ["previewLinear", "confirmLinear", "syncLinearConfirmed"].map(
        (op) => endpoint + "tasks/" + op,
      ),
    );
    expect(fetch.mock.calls.map(([, init]) => JSON.parse(init.body))).toEqual([
      comment,
      comment,
      { ...comment, confirmation_token: "server-only" },
    ]);
  });

  it("cancelled confirmation cannot write or mint a token", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(Response.json(preview));
    const methods = studioHostMethods("acme.work", fetch, vi.fn().mockResolvedValue(false));
    await expect(
      methods["studio.tasks.syncLinearConfirmed"](comment, signal),
    ).rejects.toMatchObject({ code: "Cancelled" });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
