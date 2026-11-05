import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import type * as IdentityModule from "@/lib/identity";
import { ReviewPanel, useStudioReviewHistory } from "./ReviewPanel";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/hooks/useViewerId", () => ({
  useViewerId: () => "owner",
  useIdentityReady: () => true,
}));
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: { hostId: "host", workspace: "/workspace" } }),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: [
      {
        id: "codex",
        name: "codex-native-ui",
        display_name: "Codex",
        harness: "codex-native",
        builtin: true,
      },
      {
        id: "claude",
        name: "claude-native-ui",
        display_name: "Claude Code",
        harness: "claude-native",
        builtin: true,
      },
    ],
    isSuccess: true,
  }),
}));

function Panel() {
  const history = useStudioReviewHistory("session", true);
  return (
    <ReviewPanel
      conversationId="session"
      active
      revision={{
        prUrl: "https://github.com/example/repo/pull/1",
        baseSha: "1".repeat(40),
        headSha: "2".repeat(40),
      }}
      history={history}
      onOpenSource={() => {}}
    />
  );
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

it("refreshes a changed profile and drops model overrides when the harness changes", async () => {
  const profile = { id: "security", label: "Security", digest: "a".repeat(64) };
  const captures: unknown[] = [];
  vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
    const path = String(input).split("?")[0];
    if (path.endsWith("/review-profiles")) return Response.json({ profiles: [profile] });
    if (path.endsWith("/codex-native/model-options")) {
      return Response.json({
        models: [
          {
            id: "codex-review-model",
            displayName: "Codex review model",
            supportedReasoningEfforts: [{ reasoningEffort: "high" }],
          },
        ],
      });
    }
    if (path.endsWith("/claude-native/model-options")) return Response.json({ models: [] });
    if (path === "/v1/skills") return Response.json({ skills: [] });
    if (path.endsWith("/studio/reviews")) {
      if (init?.method !== "POST") {
        return Response.json({ object: "list", data: [], next_cursor: null });
      }
      captures.push(JSON.parse(String(init.body)));
      return Response.json({ detail: "Profile changed" }, { status: 409 });
    }
    throw new Error(`Unexpected review request ${path}`);
  });
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={cache}>
      <Panel />
    </QueryClientProvider>,
  );
  const user = userEvent.setup();
  const choose = async (field: string, option: string) => {
    await user.click(await screen.findByRole("combobox", { name: field }));
    await user.click(await screen.findByRole("option", { name: option }));
  };
  const run = screen.getByRole("button", { name: "Run review" });
  await choose("Reviewer", "Codex");
  await choose("Reviewer profile", "Security");
  expect(screen.queryByRole("combobox", { name: "Review skill" })).not.toBeInTheDocument();
  await choose("Model", "Codex review model");
  await choose("Reasoning effort", "High");
  await user.click(run);
  expect(await screen.findByRole("alert")).toHaveTextContent("Profile changed");
  expect(captures[0]).toMatchObject({
    profile_id: "security",
    profile_digest: "a".repeat(64),
    harness: "codex-native",
    model: "codex-review-model",
    reasoning_effort: "high",
  });
  expect(captures[0]).not.toHaveProperty("skill");

  profile.digest = "b".repeat(64);
  await user.click(screen.getByRole("button", { name: "Reload profiles" }));
  await waitFor(() => expect(run).toBeDisabled());
  await choose("Reviewer profile", "Security");
  await choose("Reviewer", "Claude Code");
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Default");
  expect(screen.getByRole("combobox", { name: "Reasoning effort" })).toHaveTextContent("Default");
  await user.click(run);
  await waitFor(() => expect(captures).toHaveLength(2));
  expect(captures[1]).toMatchObject({
    profile_id: "security",
    profile_digest: "b".repeat(64),
    harness: "claude-native",
  });
  expect(captures[1]).not.toHaveProperty("model");
  expect(captures[1]).not.toHaveProperty("reasoning_effort");
  view.unmount();
  cache.clear();
});
