import { expect, it, vi } from "vitest";
import { peekStudioSelection, studioSessionLocation } from "./studioSession";

const mock = vi.hoisted(() => ({ projects: vi.fn(), catalog: vi.fn(), settings: vi.fn() }));
vi.mock("@/lib/projectsApi", () => ({ listProjects: mock.projects }));
vi.mock("@/hooks/useAvailableAgents", () => ({ fetchAgentCatalog: mock.catalog }));
vi.mock("@/studio/RepositorySettingsDialog", () => ({ requestSettings: mock.settings }));

const taskId = "12345678-1234-4567-8123-123456789abc";
const selection = {
  taskId,
  hostId: "host-a",
  repository: "org/app",
  branch: "work/one",
  harness: "codex-native",
};
it("rejects malformed task identities before looking up projects", async () => {
  await expect(
    studioSessionLocation(
      { ...selection, taskId: "-".repeat(36) },
      new AbortController().signal,
      "alice",
      "server-a",
    ),
  ).rejects.toThrow("Invalid Studio");
  expect(mock.projects).not.toHaveBeenCalled();
});
it("binds the checkout to its viewer, server and verified project", async () => {
  const { nativeCodingAgentForHarness } = await import("@/lib/nativeCodingAgents");
  mock.projects.mockResolvedValue([
    { id: "p", name: "Project", config: { host_id: "host-a", workspace: "/src" } },
  ]);
  mock.catalog.mockResolvedValue({
    agents: [
      { builtin: true, id: "a", name: nativeCodingAgentForHarness("codex-native")!.agentName },
    ],
  });
  mock.settings.mockResolvedValue({
    repositories: [
      { role: "active", checkout: "ready", origin: "git@github.com:Org/App.git", path: "app" },
    ],
  });
  const route = await studioSessionLocation(
    selection,
    new AbortController().signal,
    "alice",
    "server-a",
  );
  const params = new URLSearchParams(route.search);
  const token = params.get("studioStart");
  expect(token).toBeTruthy();
  expect(route.search).not.toContain("/src");
  expect(peekStudioSelection(token, "alice", "server-a")).toMatchObject({
    ...selection,
    workspace: "/src/app",
    agent: "a",
  });
  expect(peekStudioSelection(token, "bob", "server-a")).toBeNull();
  expect(peekStudioSelection(token, "alice", "server-b")).toBeNull();
  mock.settings.mockResolvedValue({
    repositories: [
      { role: "reference", checkout: "ready", origin: "git@github.com:Org/App.git", path: "other" },
    ],
  });
  await expect(
    studioSessionLocation(selection, new AbortController().signal, "alice", "server-a"),
  ).rejects.toThrow("active checkout");
});
