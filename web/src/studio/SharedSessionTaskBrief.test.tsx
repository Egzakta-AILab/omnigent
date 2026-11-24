import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import type * as Identity from "@/lib/identity";
import { SharedSessionTaskBrief } from "./IssueWorkspace";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), viewer: vi.fn() }));
vi.mock("@/lib/identity", async (original) => ({
  ...(await original<typeof Identity>()),
  authenticatedFetch: mocks.fetch,
}));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: mocks.viewer }));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

it("shows only tasks associated with the currently readable session, preserving its legacy issue brief", async () => {
  const client = new QueryClient();
  const sessionId = "4a454af4d5a54829bb6acd53369a9956";
  const sha = "8".repeat(40);
  mocks.viewer.mockReturnValue("alice");
  mocks.fetch
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          items: [
            {
              task_id: "task",
              linear_issue_id: "issue",
              acceptance_check: "Share the pinned review",
              pull_requests: [{ repository: "Org/Repo", number: 4, head_sha: sha }],
              session_ids: [sessionId],
            },
          ],
        }),
      ),
    )
    .mockResolvedValueOnce(new Response(null, { status: 404 }));
  const component = () => (
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <SharedSessionTaskBrief sessionId={sessionId} fallback={<span>Legacy issue brief</span>} />
      </QueryClientProvider>
    </MemoryRouter>
  );
  const page = render(component());
  const summary = await screen.findByText("Task · issue");
  fireEvent.click(summary);
  expect(screen.getByText("Acceptance check · Share the pinned review")).toBeVisible();
  expect(screen.getByRole("link", { name: /Org\/Repo #4/ })).toHaveAttribute(
    "href",
    "https://github.com/Org/Repo/pull/4/commits/" + sha,
  );
  expect(mocks.fetch.mock.calls[0][0]).toBe(
    "/v1/extensions/egzakta.work/studio/v1/tasks/forSession",
  );
  expect(JSON.parse(mocks.fetch.mock.calls[0][1].body)).toEqual({ session_id: sessionId });
  mocks.viewer.mockReturnValue("bob");
  page.rerender(component());
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the shared task.");
  expect(screen.getByText("Legacy issue brief")).toBeVisible();
  expect(screen.queryByText(/Share the pinned review/)).toBeNull();
  client.clear();
});
