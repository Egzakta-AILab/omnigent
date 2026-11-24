import { fetchAgentCatalog } from "@/hooks/useAvailableAgents";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";
import { randomUUID } from "@/lib/randomUUID";
import { isValidSandboxBranch } from "@/lib/sandboxRepositories";
import { linearIssueKey } from "@/lib/studioIssue";
import { listProjects } from "@/lib/projectsApi";
import { requestSettings } from "@/studio/RepositorySettingsDialog";
import { ExtensionHostServiceError } from "./errors";

export interface StudioSelection {
  hostId: string;
  repository: string;
  branch: string;
  harness: string;
  taskId: string;
  project: string;
  workspace: string;
  agent: string;
}

// Only the host's most recent Work selection can open a task composer in this tab.
let pending:
  { token: string; viewer: string; server: string; selection: StudioSelection } | undefined;
export function peekStudioSelection(
  token: string | null,
  viewer: string | null,
  server: string | null,
) {
  return pending &&
    pending.token === token &&
    pending.viewer === viewer &&
    pending.server === server
    ? pending.selection
    : null;
}

function matchesOrigin(origin: string | null | undefined, repository: string) {
  if (!origin) return false;
  const remote = origin.toLowerCase();
  const slug = repository.toLowerCase();
  return [`https://github.com/${slug}`, `git@github.com:${slug}`].some(
    (candidate) => remote === candidate || remote === candidate + ".git",
  );
}

export async function studioSessionLocation(
  input: Record<string, unknown>,
  signal: AbortSignal,
  viewer: string | null,
  server: string | null,
) {
  const { hostId, repository, branch, harness, taskId } = input;
  if (
    !viewer ||
    !server ||
    typeof hostId !== "string" ||
    !hostId ||
    typeof repository !== "string" ||
    !/^[\w.-]+\/[\w.-]+$/.test(repository) ||
    typeof branch !== "string" ||
    !/^[\w][\w./-]{0,200}$/.test(branch) ||
    !isValidSandboxBranch(branch) ||
    branch.split("/").some((part) => !part || part.startsWith(".") || part.endsWith(".lock")) ||
    typeof harness !== "string" ||
    !["claude-native", "codex-native", "cursor-native", "pi-native", "opencode-native"].includes(
      harness,
    ) ||
    typeof taskId !== "string" ||
    !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(taskId)
  ) {
    throw new ExtensionHostServiceError("InvalidParams", "Invalid Studio session selection");
  }
  const [projects, catalog] = await Promise.all([listProjects(), fetchAgentCatalog()]);
  const agent = catalog.agents.find(
    (a) => a.builtin && a.name === nativeCodingAgentForHarness(harness)?.agentName,
  );
  const candidates = projects.filter(
    (p) =>
      (!input.projectId || p.id === input.projectId) &&
      p.config?.host_id === hostId &&
      p.config.workspace,
  );
  const matches = (
    await Promise.all(
      candidates.map(async (p) => {
        const workspace = p.config!.workspace!;
        const query = new URLSearchParams({ host_id: hostId, workspace });
        const settings = await requestSettings(
          `/v1/projects/${encodeURIComponent(p.id)}/studio/repositories?${query}`,
          { signal },
        );
        return settings.repositories
          .filter(
            (r) =>
              (r.path === "." ||
                r.path
                  .split("/")
                  .every(
                    (part) =>
                      !!part &&
                      part !== "." &&
                      part !== ".." &&
                      !/[\\:]/.test(part) &&
                      !part.includes("\0"),
                  )) &&
              r.checkout === "ready" &&
              r.role === "active" &&
              matchesOrigin(r.origin, repository),
          )
          .map((r) => ({
            project: p.name,
            workspace: r.path === "." ? workspace : `${workspace.replace(/\/$/, "")}/${r.path}`,
          }));
      }),
    )
  ).flat();
  signal.throwIfAborted();
  if (!agent || matches.length !== 1) {
    throw new ExtensionHostServiceError(
      "Unavailable",
      "Select a project with one active checkout of this repository on the chosen host",
    );
  }
  const target = matches[0];
  const token = randomUUID();
  pending = {
    token,
    viewer,
    server,
    selection: { hostId, repository, branch, harness, taskId, agent: agent.id, ...target },
  };
  const query = new URLSearchParams({ project: target.project, studioStart: token });
  const issue = typeof input.issue === "string" ? linearIssueKey(input.issue) : null;
  if (issue) query.set("issue", issue);
  if (typeof input.prompt === "string") query.set("prompt", input.prompt.slice(0, 1000));
  return { pathname: "/", search: `?${query}` };
}
