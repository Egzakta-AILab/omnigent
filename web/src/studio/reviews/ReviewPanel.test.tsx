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

it.each(["single", "run", "coordinated"] as const)(
  "launches a project %s from the selected workspace",
  async (mode) => {
    const prUrl = "https://github.com/example/repo/pull/1";
    const baseSha = "1".repeat(40);
    const headSha = "2".repeat(40);
    const capturePath = "/v1/projects/project/studio/reviews";
    const launchPath = "/v1/studio/reviews/review/start";
    const profile = { id: "backend", label: "Backend", digest: "b".repeat(64) };
    const security = { id: "security", label: "Security", digest: "c".repeat(64) };
    const runsPath = "/v1/projects/project/studio/review-runs";
    const runPath = "/v1/studio/review-runs/run";
    const findingKey = "f".repeat(64);
    const reproductionPath = `${runPath}/findings/${findingKey}/reproductions`;
    let runWasStarted = false;
    let planWasApproved = false;
    let receiptAttached = false;
    let receiptAccessDenied = false;
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
    const second = {
      ...launched,
      id: "other",
      reviewer_session_id: "other",
      profile: { ...captured.profile, role: security },
    };
    const run = {
      id: "run",
      target: captured.target,
      reviewers: [launched, second],
      created_at: 0,
      patch_sha256: "e".repeat(64),
    };
    const plan = {
      coordinator_id: "coordinator",
      base_sha: baseSha,
      head_sha: headSha,
      decisions: [
        { review_id: "review", selected: true, reason: "Backend coverage is required." },
        { review_id: "other", selected: false, reason: "No security paths changed." },
      ],
    };
    const digest = "a".repeat(64);
    function visibleRun() {
      if (mode !== "coordinated") return run;
      return {
        ...run,
        coordinator: { ...launched, id: "coordinator", reviewer_session_id: "coordinator" },
        reviewers: [
          { ...(planWasApproved ? launched : captured), run_role: "required" },
          { ...second, reviewer_session_id: null, dispatch_status: "not_requested" },
        ],
        plan: planWasApproved ? plan : null,
      };
    }
    const finding = {
      title: "A retry writes twice",
      detail: "The second write is visible.",
      path: "src/backend.py",
      side: "head",
      line: 12,
      severity: "high",
    };
    const receipt = {
      observed_at: "2026-11-05T10:30:00Z",
      image_id: `sha256:${"7".repeat(64)}`,
      command: ["python", "check.py"],
      timeout_seconds: 60,
      base: {
        commit: baseSha,
        state: "exited",
        exit_code: 0,
        elapsed_ms: 200,
        output: "1 request",
      },
      head: {
        commit: headSha,
        state: "exited",
        exit_code: 1,
        elapsed_ms: 250,
        output: "3 requests",
      },
    };
    const evidence = { digest: "8".repeat(64), provenance: "uploaded", attached_at: 0, receipt };
    const unsafe = {
      ...evidence,
      digest: "9".repeat(64),
      receipt: { ...receipt, command: ["check", "\u001b[201~"] },
    };
    const posts: { path: string; body: unknown }[] = [];
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      const path = url.pathname;
      if (init?.method === "POST") {
        posts.push({ path, body: JSON.parse(String(init.body)) });
        if (path === capturePath) return Response.json({ review: captured });
        if (path === launchPath) return Response.json(launched);
        if (path === runsPath) return Response.json(visibleRun());
        if (path === reproductionPath) {
          receiptAttached = true;
          return Response.json(evidence);
        }
        if (path === `${runPath}/start`) {
          runWasStarted = true;
          if (JSON.parse(String(init.body)).proposal_digest === digest) planWasApproved = true;
          return Response.json({ run: visibleRun(), errors: {} });
        }
      }
      if (path === runsPath)
        return Response.json({ entries: runWasStarted ? [visibleRun()] : [], next_cursor: null });
      if (path === reproductionPath) {
        if (receiptAccessDenied)
          return Response.json({ detail: "Access changed" }, { status: 404 });
        return Response.json(receiptAttached ? [evidence, unsafe] : []);
      }
      if (path === `${runPath}/findings/${findingKey}/evidence`) return Response.json([]);
      if (path === `${runPath}/report`) {
        if (mode === "coordinated") {
          return Response.json(
            planWasApproved
              ? {
                  reviewers: [
                    {
                      review_id: "review",
                      report: { state: "ready", summary: "Backend report", findings: [finding] },
                    },
                  ],
                  findings: [{ finding, review_ids: ["review"] }],
                }
              : { proposal: { state: "ready", plan, digest }, reviewers: [], findings: [] },
          );
        }
        return Response.json({
          reviewers: [
            {
              review_id: "review",
              report: { state: "ready", summary: "Backend report", findings: [finding] },
            },
            {
              review_id: "other",
              report: { state: "ready", summary: "Security report", findings: [finding] },
            },
          ],
          findings: [{ finding, review_ids: ["review", "other"], finding_key: findingKey }],
        });
      }
      if (path === capturePath) {
        expect(url.searchParams.get("pr_url")).toBe(prUrl);
        return Response.json({ object: "list", data: [], next_cursor: null });
      }
      if (path === "/v1/projects/project/studio/review-profiles") {
        expect(url.searchParams.get("host_id")).toBe("selected-host");
        expect(url.searchParams.get("workspace")).toBe("/selected workspace");
        return Response.json({ profiles: [profile, security] });
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
    if (mode !== "single") {
      await user.click(screen.getByRole("button", { name: "Add reviewer to run" }));
      if (mode === "coordinated") {
        await user.click(screen.getByRole("button", { name: "Use as coordinator" }));
        await user.click(screen.getByRole("checkbox", { name: "Require Backend · Codex" }));
      }
      await user.click(screen.getByRole("combobox", { name: "Reviewer profile" }));
      await user.click(await screen.findByRole("option", { name: "Security" }));
      await user.click(screen.getByRole("button", { name: "Add reviewer to run" }));
      if (mode === "coordinated") {
        await user.click(screen.getByRole("button", { name: "Ask coordinator to plan" }));
        const approval = await screen.findByRole("button", {
          name: "Approve plan and start reviewers",
        });
        expect(screen.getByText("No security paths changed.")).toBeVisible();
        expect(screen.queryByText("Consolidated findings")).toBeNull();
        expect(posts[0].body).toMatchObject({
          coordinator: { profile_id: "backend" },
          required_reviewer_indices: [0],
        });
        expect(posts).toHaveLength(2);
        await user.click(approval);
        expect(await screen.findByText("1 / 1 reports ready")).toBeVisible();
        expect(screen.getByText("Skipped")).toBeVisible();
        expect(screen.getByText("Plan approved")).toBeVisible();
        expect(posts[2]).toEqual({ path: `${runPath}/start`, body: { proposal_digest: digest } });
        return;
      }
      await user.click(screen.getByRole("button", { name: "Run selected reviewers" }));
      expect(await screen.findByText("2 / 2 reports ready")).toBeVisible();
      expect(screen.getByText("Consolidated findings")).toBeVisible();
      expect(screen.getAllByText(finding.title)).toHaveLength(1);
      expect(posts.map((entry) => entry.path)).toEqual([runsPath, `${runPath}/start`]);
      expect(posts[0].body).toMatchObject({
        reviewers: [
          { profile_id: "backend", host_id: "selected-host", expected_head_sha: headSha },
          { profile_id: "security", host_id: "selected-host", expected_head_sha: headSha },
        ],
      });
      await user.click(screen.getByText("Reproductions"));
      const input = await screen.findByLabelText("Reproduction receipt");
      const file = new File([JSON.stringify(receipt)], "receipt.json", {
        type: "application/json",
      });
      Object.defineProperty(file, "text", { value: async () => JSON.stringify(receipt) });
      await user.upload(input, file);
      await user.click(screen.getByRole("button", { name: "Attach receipt" }));
      expect(await screen.findAllByText(/Uploaded receipt/)).toHaveLength(2);
      expect(posts[2]).toEqual({ path: reproductionPath, body: receipt });
      expect(screen.getAllByRole("button", { name: "Copy rerun command" })[1]).toBeDisabled();
      await user.click(screen.getAllByText(/Exit 1/)[0]);
      expect(screen.getAllByText("3 requests")[0]).toBeVisible();
      receiptAccessDenied = true;
      await client.invalidateQueries({ queryKey: ["studio-finding-reproductions"] });
      expect(await screen.findByRole("alert")).toHaveTextContent("Access changed");
      expect(screen.queryAllByText(/Uploaded receipt/)).toHaveLength(0);
      await user.click(screen.getAllByRole("button", { name: "Security · Codex" })[0]);
      expect(screen.getByText("Security report")).toBeVisible();
      expect(screen.getByRole("link", { name: "Open reviewer report" })).toHaveAttribute(
        "href",
        "/c/other?view=review",
      );
      expect(onReviewStarted).not.toHaveBeenCalled();
      expect(screen.getByText("Reproductions")).toBeVisible();
      return;
    }
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
  },
);
