import { useMemo, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { authenticatedFetch, resolveIdentity } from "@/lib/identity";
import { getOmnigentServerIdentity } from "@/lib/host";
import { fetchPullRequestInfo, type PullRequest } from "@/hooks/usePullRequests";
import {
  fetchSessionPullRequests,
  sessionPullRequestsKey,
  storesSessionPullRequests,
  type StoredPullRequests,
} from "@/hooks/useSessionPullRequests";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { RunnerOfflineError } from "@/hooks/useWorkspaceChangedFiles";
import { fetchProjectSessionsPage } from "@/hooks/useConversations";
import { listProjects } from "@/lib/projectsApi";
import { isImplementationSession, issueWorkspaceUrl, linearIssueKey } from "@/lib/studioIssue";
import { useNavigate } from "@/lib/routing";
import type { ExtensionCatalogItem, ExtensionPullRequest } from "../types";
import { ExtensionHostServiceError } from "./errors";
import { studioHostMethods } from "./studio";
import { useLinearConfirmation } from "./useLinearConfirmation";
import { grantedHostMethods } from "./registry";
import {
  cachedProjectSummaries,
  createProjectSummary,
  listProjectSummaries,
  parseCreateProjectParams,
} from "./projects";
import { cachedSessionSummaries, listSessionPage, SessionReadLimiter } from "./sessions";
import {
  ExtensionStorageError,
  ExtensionStorageWriteLimiter,
  ExtensionUserStorage,
} from "./storage";

function objectParams(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ExtensionHostServiceError("InvalidParams", "Expected an object");
  }
  return value as Record<string, unknown>;
}

function throwIfAborted(signal: AbortSignal): void {
  if (signal.aborted) throw new DOMException("Host operation cancelled", "AbortError");
}

async function mapStorageErrors<T>(operation: () => Promise<T>): Promise<T> {
  try {
    return await operation();
  } catch (error) {
    if (error instanceof ExtensionStorageError) {
      throw new ExtensionHostServiceError(error.code, error.message);
    }
    throw error;
  }
}

async function storageFor(extensionId: string): Promise<ExtensionUserStorage> {
  const [userId, serverIdentity] = await Promise.all([
    resolveIdentity(),
    Promise.resolve(getOmnigentServerIdentity()),
  ]);
  if (!userId || !serverIdentity) {
    throw new ExtensionHostServiceError(
      "Unavailable",
      "Extension storage requires resolved user and server identities",
    );
  }
  return new ExtensionUserStorage(serverIdentity, userId, extensionId);
}

function pageSearch(value: unknown): string {
  if (value === undefined) return "";
  const params = objectParams(value);
  const search = new URLSearchParams();
  for (const key of Object.keys(params).sort()) {
    const item = params[key];
    if (typeof item !== "string" && typeof item !== "number" && typeof item !== "boolean") {
      throw new ExtensionHostServiceError("InvalidParams", `Page parameter ${key} is invalid`);
    }
    search.set(key, String(item));
  }
  const serialized = search.toString();
  return serialized ? `?${serialized}` : "";
}

export function useExtensionHostServices(extension: ExtensionCatalogItem) {
  const { confirm, dialog } = useLinearConfirmation();
  const navigate = useNavigate();
  const theme = useResolvedThemeMode();
  const queryClient = useQueryClient();
  const storedPullRequests = storesSessionPullRequests(useServerInfo());
  const writeLimiter = useMemo(() => new ExtensionStorageWriteLimiter(), []);
  const sessionListLimiter = useMemo(() => new SessionReadLimiter(), []);
  const pullRequestLimiter = useMemo(() => new SessionReadLimiter(), []);
  // External URLs the host has handed to this extension; the only ones it may open.
  const externalUrlsRef = useRef(new Set<string>());

  const methods = useMemo(() => {
    const implementations = {
      ...studioHostMethods(extension.id, authenticatedFetch, confirm),
      "navigation.openPage": (params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        const input = objectParams(params);
        const pageId = input.pageId;
        if (typeof pageId !== "string") {
          throw new ExtensionHostServiceError("InvalidParams", "pageId is required");
        }
        const page = extension.pages.find((item) => item.id === pageId);
        if (!page) {
          throw new ExtensionHostServiceError("PermissionDenied", "Page is not owned by extension");
        }
        navigate({
          pathname: `/extensions/${extension.id}/${page.route}`,
          search: pageSearch(input.params),
        });
        return null;
      },
      "navigation.openSession": (params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        const sessionId = objectParams(params).sessionId;
        if (typeof sessionId !== "string" || !sessionId || sessionId.length > 256) {
          throw new ExtensionHostServiceError("InvalidParams", "sessionId is invalid");
        }
        navigate(`/c/${encodeURIComponent(sessionId)}`);
        return null;
      },
      "navigation.openExternal": async (params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        const url = objectParams(params).url;
        if (typeof url !== "string" || !externalUrlsRef.current.has(url)) {
          throw new ExtensionHostServiceError(
            "PermissionDenied",
            "URL was not provided by the host",
          );
        }
        window.open(url, "_blank", "noopener,noreferrer");
        return null;
      },
      "navigation.openNewSession": async (params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        const { projectId, prompt, issue } = params === undefined ? {} : objectParams(params);
        if (prompt !== undefined && (typeof prompt !== "string" || prompt.length > 1000)) {
          throw new ExtensionHostServiceError("InvalidParams", "Initial prompt is invalid");
        }
        const promptQuery =
          typeof prompt === "string" && prompt.trim()
            ? `prompt=${encodeURIComponent(prompt.trim())}`
            : "";
        if (projectId === undefined || projectId === null) {
          navigate(promptQuery ? `/?${promptQuery}` : "/");
          return null;
        }
        if (typeof projectId !== "string" || !projectId || projectId.length > 256) {
          throw new ExtensionHostServiceError("InvalidParams", "projectId is invalid");
        }
        // The composer takes the project by name (`?project=`), so resolve it here.
        const project = (await listProjectSummaries()).find((item) => item.id === projectId);
        throwIfAborted(signal);
        if (!project) throw new ExtensionHostServiceError("InvalidParams", "Project not found");
        if (issue !== undefined) {
          const key = typeof issue === "string" ? linearIssueKey(issue) : null;
          if (!key || extension.id !== "egzakta.linear") {
            throw new ExtensionHostServiceError("InvalidParams", "Linear issue is invalid");
          }
          navigate(issueWorkspaceUrl(project.name, key));
          return null;
        }
        navigate({
          pathname: "/",
          search: `?project=${encodeURIComponent(project.name)}${promptQuery ? `&${promptQuery}` : ""}`,
        });
        return null;
      },
      "theme.getCurrent": (_params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        return { theme };
      },
      "theme.subscribe": (_params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        return { theme };
      },
      "storage.user.get": async (params: unknown, signal: AbortSignal) => {
        const storage = await storageFor(extension.id);
        return mapStorageErrors(() => storage.get(objectParams(params).key, signal));
      },
      "storage.user.set": async (params: unknown, signal: AbortSignal) => {
        const input = objectParams(params);
        return writeLimiter.run(signal, async () => {
          const storage = await storageFor(extension.id);
          await mapStorageErrors(() => storage.set(input.key, input.value, signal));
          return null;
        });
      },
      "storage.user.delete": async (params: unknown, signal: AbortSignal) =>
        writeLimiter.run(signal, async () => {
          const storage = await storageFor(extension.id);
          await mapStorageErrors(() => storage.delete(objectParams(params).key, signal));
          return null;
        }),
      "sessions.getCached": (params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        return cachedSessionSummaries(queryClient, params);
      },
      "sessions.listPage": (params: unknown, signal: AbortSignal) =>
        sessionListLimiter.run(signal, () => listSessionPage(params, signal)),
      "sessions.pullRequest": (params: unknown, signal: AbortSignal) =>
        pullRequestLimiter.run(signal, async (): Promise<ExtensionPullRequest | null> => {
          const sessionId = objectParams(params).sessionId;
          if (typeof sessionId !== "string" || !sessionId || sessionId.length > 256) {
            throw new ExtensionHostServiceError("InvalidParams", "sessionId is invalid");
          }
          let pr: Pick<PullRequest, "number" | "title" | "state" | "url"> | null | undefined;
          try {
            // Shares the GitHub panel's cache so the runner is asked at most every 30s per session.
            const info = await queryClient.fetchQuery({
              queryKey: ["github-info", sessionId],
              queryFn: () => fetchPullRequestInfo(sessionId),
              staleTime: 30_000,
            });
            pr = info.available ? info.pr : null;
          } catch (error) {
            // The host is offline; answer with the state the server last stored, if it does.
            if (!(error instanceof RunnerOfflineError) || !storedPullRequests) throw error;
            let stored: StoredPullRequests;
            try {
              stored = await queryClient.fetchQuery({
                queryKey: sessionPullRequestsKey(sessionId),
                queryFn: () => fetchSessionPullRequests(sessionId),
                staleTime: 30_000,
              });
            } catch (fallbackError) {
              throw Object.assign(error, { cause: fallbackError });
            }
            const [first] = stored.prs;
            // A linked PR answers once it has a state, imported or not; an empty list
            // means "no PR" only once the server has imported the session's PRs.
            if (first ? !first.state : !stored.synced) throw error;
            pr = first?.state
              ? {
                  number: first.number,
                  title: first.title ?? "",
                  state: first.state,
                  url: first.url,
                }
              : null;
          }
          if (!pr || !/^https:\/\//.test(pr.url)) return null;
          externalUrlsRef.current.add(pr.url);
          return { number: pr.number, title: pr.title.slice(0, 256), state: pr.state, url: pr.url };
        }),
      "projects.list": (_params: unknown, signal: AbortSignal) => {
        throwIfAborted(signal);
        return cachedProjectSummaries(queryClient) ?? listProjectSummaries();
      },
      "projects.create": async (params: unknown, signal: AbortSignal) => {
        const name = parseCreateProjectParams(params);
        const { templateProjectId, sourceKey, linearIssue } = objectParams(params);
        throwIfAborted(signal);
        const template =
          templateProjectId === undefined && sourceKey === undefined
            ? undefined
            : {
                template_project_id: templateProjectId,
                source_key: sourceKey,
                ...(extension.id === "egzakta.linear" ? { linear_issue: linearIssue } : {}),
              };
        const project = await createProjectSummary(name, template);
        // The sidebar caches its project list; refresh it so the new project shows there too.
        void queryClient.invalidateQueries({ queryKey: ["projects"] });
        return project;
      },
      ...(extension.id === "egzakta.linear"
        ? {
            "linear.findTask": async (params: unknown, signal: AbortSignal) => {
              const { sourceKey, issue } = objectParams(params);
              const key = typeof issue === "string" ? linearIssueKey(issue) : null;
              if (
                !key ||
                typeof sourceKey !== "string" ||
                !/^linear:[a-z0-9-]+:[a-z0-9-]+$/.test(sourceKey)
              ) {
                throw new ExtensionHostServiceError("InvalidParams", "Linear task is invalid");
              }
              throwIfAborted(signal);
              const project = (await listProjects()).find(
                (item) => item.config?.source_key === sourceKey,
              );
              throwIfAborted(signal);
              if (!project) return null;
              const task = {
                projectId: project.id,
                snapshotTemplateId: project.config?.linear_issue
                  ? undefined
                  : project.config?.template_project_id,
              };
              let after: string | undefined;
              do {
                // The cursor is only available after reading the preceding page.
                // eslint-disable-next-line no-await-in-loop
                const page = await fetchProjectSessionsPage(
                  project.name,
                  after,
                  20,
                  "updated_at",
                  false,
                  signal,
                );
                const session = page.data.find((item) => isImplementationSession(item.labels, key));
                if (session) return { ...task, sessionId: session.id };
                after = page.has_more ? (page.last_id ?? undefined) : undefined;
              } while (after);
              return { ...task, sessionId: null };
            },
            "linear.read": async (params: unknown, signal: AbortSignal) => {
              const { token, team, issue, after, search, assignee } = objectParams(params);
              throwIfAborted(signal);
              const response = await authenticatedFetch("/v1/extensions/egzakta.linear/read", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ token, team, issue, after, search, assignee }),
                signal,
              });
              if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new ExtensionHostServiceError(
                  "HostError",
                  typeof error.detail === "string" ? error.detail : "Linear request failed",
                );
              }
              return response.json();
            },
          }
        : {}),
    };
    return grantedHostMethods(extension, implementations);
  }, [
    extension,
    confirm,
    navigate,
    pullRequestLimiter,
    queryClient,
    sessionListLimiter,
    storedPullRequests,
    theme,
    writeLimiter,
  ]);
  const events = useMemo(() => ({ "theme.changed": { theme } }), [theme]);
  return { methods, events, dialog };
}
