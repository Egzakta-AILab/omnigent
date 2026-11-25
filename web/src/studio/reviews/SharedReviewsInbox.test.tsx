import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { listSharedReviews } from "./api";
import { SharedReviewsInbox } from "./SharedReviewsInbox";

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "coworker" }));
vi.mock("./api", () => ({ listSharedReviews: vi.fn() }));

const first = {
  id: "a".repeat(32),
  project_name: "KVARK",
  repository: "Egzakta-AILab/kvark",
  pr_number: 1461,
  head_sha: "b".repeat(40),
  created_at: 1,
};

function renderInbox(sharingMode: "on" | "off") {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <CapabilitiesProvider
        info={{ ...FALLBACK_SERVER_INFO, studio_reviews: true, sharing_mode: sharingMode }}
      >
        <MemoryRouter>
          <SharedReviewsInbox />
        </MemoryRouter>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

it("links to the saved review revision and can load an earlier share", async () => {
  const user = userEvent.setup();
  const second = { ...first, id: "c".repeat(32), head_sha: "d".repeat(40) };
  vi.mocked(listSharedReviews)
    .mockResolvedValueOnce({ entries: [first], next_cursor: first.id })
    .mockResolvedValueOnce({ entries: [second], next_cursor: null });
  renderInbox("on");

  expect(await screen.findByRole("link", { name: /#1461.*bbbbbbbb/ })).toHaveAttribute(
    "href",
    `/reviews/${first.id}/discussion`,
  );
  await user.click(screen.getByRole("button", { name: "Load more shared reviews" }));
  expect(await screen.findByRole("link", { name: /#1461.*dddddddd/ })).toHaveAttribute(
    "href",
    `/reviews/${second.id}/discussion`,
  );
  expect(listSharedReviews).toHaveBeenLastCalledWith(first.id, expect.any(AbortSignal));
});

it("does not fetch or expose shares when the server disables sharing", () => {
  renderInbox("off");
  expect(screen.queryByRole("region", { name: "Shared PR reviews" })).toBeNull();
  expect(listSharedReviews).not.toHaveBeenCalled();
});
