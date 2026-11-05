import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import type * as IdentityModule from "@/lib/identity";
import { MemoryRouter } from "react-router-dom";
import { ProjectReviewPanel, ReviewPanel, useStudioReviewHistory } from "./ReviewPanel";

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

it("prefills profile defaults and drops model overrides when the harness changes", async () => {
  const profile = {
    id: "security",
    label: "Security",
    digest: "a".repeat(64),
    defaults: { harness: "codex-native", model: "codex-review-model", reasoning_effort: "high" },
  };
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
  await choose("Reviewer profile", "Security");
  expect(screen.queryByRole("combobox", { name: "Review skill" })).not.toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Reviewer" })).toHaveTextContent("Codex");
  await waitFor(() =>
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveTextContent("Codex review model"),
  );
  expect(screen.getByRole("combobox", { name: "Reasoning effort" })).toHaveTextContent("High");
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
  profile.defaults.model = "retired-model";
  await user.click(screen.getByRole("button", { name: "Reload profiles" }));
  await waitFor(() => expect(run).toBeDisabled());
  await choose("Reviewer profile", "Security");
  expect(screen.getByText(/Model retired-model is unavailable/)).toBeInTheDocument();
  expect(run).toBeDisabled();
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

it("captures a project review with the selected workspace and starts its reviewer directly", async () => {
  const prUrl = "https://github.com/example/repo/pull/1";
  const baseSha = "1".repeat(40);
  const headSha = "2".repeat(40);
  const capturePath = "/v1/projects/project/studio/reviews";
  const launchPath = "/v1/studio/reviews/review/start";
  const profile = { id: "backend", label: "Backend", digest: "b".repeat(64) };
  const captured = {
    id: "review",
    target: {
      pr_url: prUrl,
      base_sha: baseSha,
      head_sha: headSha,
      host_id: "selected-host",
      workspace: "/selected workspace",
      repository_path: "nested/repo",
    },
    profile: { harness: "codex-native", role: profile },
    dispatch_status: "not_requested",
  };
  const launched = { ...captured, dispatch_status: "succeeded", reviewer_session_id: "review" };
  const posts: { path: string; body: unknown }[] = [];
  vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    const path = url.pathname;
    if (init?.method === "POST") {
      posts.push({ path, body: JSON.parse(String(init.body)) });
      if (path === capturePath) return Response.json({ review: captured });
      if (path === launchPath) return Response.json(launched);
    }
    if (path === capturePath) {
      expect(url.searchParams.get("pr_url")).toBe(prUrl);
      return Response.json({ object: "list", data: [], next_cursor: null });
    }
    if (path === "/v1/projects/project/studio/review-profiles") {
      expect(url.searchParams.get("host_id")).toBe("selected-host");
      expect(url.searchParams.get("workspace")).toBe("/selected workspace");
      return Response.json({ profiles: [profile] });
    }
    if (path.endsWith("/codex-native/model-options")) return Response.json({ models: [] });
    if (path === "/v1/skills") return Response.json({ skills: [] });
    if (path === "/v1/studio/reviews/review") return Response.json(launched);
    if (path === "/v1/studio/reviews/review/report") {
      return Response.json({ state: "pending", summary: null, findings: [], message_id: null });
    }
    throw new Error(`Unexpected review request ${path}`);
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onReviewStarted = vi.fn();
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ProjectReviewPanel
          active
          projectId="project"
          hostId="selected-host"
          workspace="/selected workspace"
          repository="nested/repo"
          revision={{ prUrl, baseSha, headSha }}
          onReviewStarted={onReviewStarted}
        />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  const user = userEvent.setup();
  await user.click(await screen.findByRole("combobox", { name: "Reviewer profile" }));
  await user.click(await screen.findByRole("option", { name: "Backend" }));
  await user.click(screen.getByRole("combobox", { name: "Reviewer" }));
  await user.click(await screen.findByRole("option", { name: "Codex" }));
  await user.click(screen.getByRole("button", { name: "Run review" }));
  await waitFor(() => expect(onReviewStarted).toHaveBeenCalledWith(launched));
  expect(posts).toEqual([
    {
      path: capturePath,
      body: {
        pr_url: prUrl,
        expected_base_sha: baseSha,
        expected_head_sha: headSha,
        agent_id: "codex",
        harness: "codex-native",
        profile_id: "backend",
        profile_digest: "b".repeat(64),
        host_id: "selected-host",
        workspace: "/selected workspace",
        repository: "nested/repo",
      },
    },
    { path: launchPath, body: {} },
  ]);
  expect(await screen.findByRole("link", { name: "Open review report" })).toHaveAttribute(
    "href",
    "/c/review?view=review",
  );
});
