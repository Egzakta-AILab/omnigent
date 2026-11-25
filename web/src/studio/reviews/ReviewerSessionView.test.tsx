import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useSearchParams } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { copyText } from "@/lib/clipboard";
import { authenticatedFetch } from "@/lib/identity";
import type * as IdentityModule from "@/lib/identity";
import { ReviewerSessionView } from "./ReviewerSessionView";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn() }));
vi.mock("@/hooks/useViewerId", () => ({
  useViewerId: () => "owner",
  useIdentityReady: () => true,
}));
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: null }),
}));

function Content() {
  const [params] = useSearchParams();
  return (
    <ReviewerSessionView sessionId="review-session" requestedView={params.get("view")}>
      <p>Raw review transcript</p>
    </ReviewerSessionView>
  );
}

function renderSession(path = "/c/review-session") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        <Content />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

it.each([false, true])(
  "opens the parsed report and exposes actions when backed by a saved run (%s)",
  async (savedRun) => {
    const finding = {
      title: "Cross-account access",
      severity: "high",
      detail: "Check that the caller owns the record.",
      path: "src/auth.ts",
      line: 8,
      side: "head",
    };
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      if (String(input) === "/v1/sessions/review-session/studio/review") {
        return Response.json({
          id: "review",
          ...(savedRun ? { run_id: "run" } : {}),
          reviewer_session_id: "review-session",
          dispatch_status: "succeeded",
          target: {
            pr_url: "https://github.com/owner/repo/pull/2",
            pr_number: 2,
            repository: "owner/repo",
            base_sha: "1".repeat(40),
            head_sha: "2".repeat(40),
          },
          profile: { harness: "codex-native", role: { label: "Security" }, model: "review-model" },
        });
      }
      if (String(input) === "/v1/studio/reviews/review/report") {
        return Response.json({
          state: "ready",
          summary: "The authorization change has one regression.",
          findings: [finding],
        });
      }
      if (String(input) === "/v1/studio/review-runs/run/report?include_finding_keys=true") {
        return Response.json({
          reviewers: [{ review_id: "review", report: { state: "ready" } }],
          findings: [{ finding, review_ids: ["review"], finding_key: "verified-anchor" }],
        });
      }
      throw new Error(`Unexpected request ${String(input)}`);
    });
    renderSession();
    expect(await screen.findByRole("heading", { name: "Pull request review" })).toBeVisible();
    expect(screen.getByText("owner/repo #2")).toBeVisible();
    expect(screen.getByText("11111111 → 22222222")).toBeVisible();
    expect(screen.getByText("Security · Codex · review-model")).toBeVisible();
    expect(await screen.findByRole("heading", { name: "Findings (1)" })).toBeVisible();
    expect(screen.getByText("The authorization change has one regression.")).toBeVisible();
    expect(screen.getByText("Cross-account access")).toBeVisible();
    expect(screen.getByText("src/auth.ts:8 · Head")).toBeVisible();
    if (savedRun) {
      expect(await screen.findByRole("link", { name: "Open finding" })).toHaveAttribute(
        "href",
        "/reviews/run/discussion?finding=verified-anchor",
      );
      expect(screen.getByRole("link", { name: "Discuss review" })).toBeVisible();
    } else {
      expect(screen.getByRole("heading", { name: "Earlier review" })).toBeVisible();
      expect(screen.getByRole("link", { name: "Ask in original transcript" })).toHaveAttribute(
        "href",
        "/c/review-session?view=chat",
      );
      vi.mocked(copyText).mockResolvedValue(undefined);
      await userEvent.click(screen.getByRole("button", { name: "Copy follow-up prompt" }));
      expect(copyText).toHaveBeenCalledWith(expect.stringContaining("src/auth.ts:8 (head side)"));
      expect(screen.queryByRole("link", { name: "Open finding" })).not.toBeInTheDocument();
    }
    expect(screen.queryByText("Raw review transcript")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Open transcript" }));
    expect(screen.getByText("Raw review transcript")).toBeVisible();
    expect(screen.queryByText("Cross-account access")).not.toBeInTheDocument();
  },
);

it("renders normal sessions when the reviewer lookup has no matching record", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(new Response(null, { status: 404 }));
  renderSession();
  expect(await screen.findByText("Raw review transcript")).toBeVisible();
  expect(authenticatedFetch).toHaveBeenCalledTimes(1);
});

it("preserves an explicit report view when the report cannot be loaded", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(new Response(null, { status: 503 }));
  renderSession("/c/review-session?view=review");
  expect(await screen.findByRole("alert")).toHaveTextContent("Review report is unavailable.");
  expect(screen.getByRole("button", { name: "Retry report" })).toBeVisible();
  expect(screen.queryByText("Raw review transcript")).not.toBeInTheDocument();
});
