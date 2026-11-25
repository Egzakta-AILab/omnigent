import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { useCanEdit } from "@/hooks/usePermissions";
import { useSession } from "@/hooks/useSession";
import { useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { authenticatedFetch } from "@/lib/identity";
import { parseHttpUrl } from "@/lib/openLinkInApp";
import { LEVEL_OWNER } from "@/lib/permissionsApi";
import { apiErrorFromResponse } from "@/lib/sessionsApi";
import { RepositorySettingsDialog } from "./RepositorySettingsDialog";

interface WorkspaceRepository {
  name: string;
  path: string;
  status: string | null;
  description: string | null;
  depends_on: string[];
  checkout: "ready" | "missing" | "invalid";
}

interface RepositoryCatalog {
  manifest: "repos.yaml" | null;
  configured?: boolean;
  repositories: WorkspaceRepository[];
}

interface BranchPullRequest {
  repository: string;
  branch: string | null;
  remote?: string | null;
  expected_remote?: string | null;
  reason?: "origin_mismatch";
  state: "found" | "no_pr" | "unavailable";
  pr: { url: string; title: string; number: number } | null;
}

async function workspaceRequest<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await authenticatedFetch(path, { signal });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as T;
}

interface PullRequestActions {
  hostOffline: boolean;
  linkPending: boolean;
  linkedUrls: readonly string[];
  onLink: (url: string) => void;
  onOpen: (url: string) => void;
}

function FindBranchPullRequest({
  endpoint,
  repository,
  viewer,
  hostOffline,
  linkPending,
  linkedUrls,
  onLink,
  onOpen,
}: PullRequestActions & {
  endpoint: string;
  repository: string;
  viewer: string;
}) {
  const lookup = useQuery({
    queryKey: ["workspace-repository-pr", viewer, endpoint, repository],
    queryFn: ({ signal }) =>
      workspaceRequest<BranchPullRequest>(
        `${endpoint}?${new URLSearchParams({ repository })}`,
        signal,
      ),
    enabled: false,
    retry: false,
    gcTime: 0,
  });
  const result =
    !lookup.isFetching && !lookup.isError && lookup.data?.repository === repository
      ? lookup.data
      : undefined;
  const pr = result?.state === "found" ? result.pr : null;
  const url = pr && parseHttpUrl(pr.url);
  const linked = url && linkedUrls.includes(url.href);

  return (
    <div className="space-y-2">
      <Button
        size="sm"
        variant="outline"
        disabled={hostOffline || lookup.isFetching || linkPending}
        onClick={() => void lookup.refetch()}
      >
        {lookup.isFetching ? "Finding branch PR…" : "Find branch PR"}
      </Button>
      {hostOffline && (
        <p className="text-muted-foreground">Connect the session’s host to find a branch PR.</p>
      )}
      {lookup.isError && (
        <p role="alert" className="text-destructive">
          Couldn’t find the branch PR. Try again or link it by URL.
        </p>
      )}
      {result?.state === "no_pr" && (
        <p role="status" className="wrap-break-word text-muted-foreground">
          {result.branch ? `No open PR for ${result.branch}.` : "No open PR for this branch."}
        </p>
      )}
      {result?.state === "unavailable" && (
        <div role="status" className="space-y-1 wrap-break-word text-muted-foreground">
          {result.reason === "origin_mismatch" ? (
            <>
              <p>
                This checkout’s origin differs from repos.yaml. Use the manifest URL as this
                checkout’s Git origin, then retry.
              </p>
              {result.expected_remote && (
                <p>
                  Expected repository is <code>{result.expected_remote}</code>.
                </p>
              )}
              {result.remote && (
                <p>
                  Current origin is <code>{result.remote}</code>.
                </p>
              )}
            </>
          ) : (
            <p>Branch PR lookup is unavailable. You can link a PR by URL.</p>
          )}
        </div>
      )}
      {pr && url?.protocol === "https:" && (
        <div className="space-y-2 border-t border-border pt-2">
          <div role="status" className="wrap-break-word">
            <p className="font-medium">
              #{pr.number} {pr.title}
            </p>
            {result?.branch && <p className="text-muted-foreground">{result.branch}</p>}
            {result?.remote && <p className="text-muted-foreground">{result.remote}</p>}
          </div>
          <Button
            size="sm"
            variant="outline"
            disabled={linkPending || (!linked && hostOffline)}
            onClick={() => (linked ? onOpen(url.href) : onLink(url.href))}
          >
            {linked ? "Open linked PR" : "Link this PR"}
          </Button>
        </div>
      )}
    </div>
  );
}

export function WorkspaceRepositories({
  conversationId,
  ...actions
}: PullRequestActions & { conversationId: string }) {
  const viewer = useViewerId();
  const canEdit = useCanEdit(conversationId);
  const { session } = useSession(conversationId);
  const info = useServerInfo();
  const canManage =
    info !== "loading" &&
    info.studio_workspace_settings &&
    !!session?.workspace &&
    (info.single_user || session.permissionLevel === LEVEL_OWNER);
  const base = `/v1/sessions/${encodeURIComponent(conversationId)}`;
  const endpoint = `${base}/resources/workspace-repositories`;
  const catalog = useQuery({
    queryKey: ["workspace-repositories", conversationId, viewer],
    queryFn: ({ signal }) => workspaceRequest<RepositoryCatalog>(endpoint, signal),
    enabled: viewer !== null && !actions.hostOffline,
    retry: false,
  });
  const [selected, setSelected] = useState<string>();
  const [managing, setManaging] = useState(false);
  if (
    viewer === null ||
    (!canManage && catalog.data?.manifest !== "repos.yaml" && !catalog.data?.configured)
  )
    return null;

  const repositories = catalog.isError ? [] : (catalog.data?.repositories ?? []);
  const repository =
    repositories.find((entry) => entry.name === selected) ??
    repositories.find((entry) => entry.checkout === "ready") ??
    repositories[0];

  return (
    <details className="max-h-[50%] shrink-0 overflow-y-auto border-b border-border">
      <summary className="cursor-pointer px-3 py-2 text-ui font-medium">
        Workspace repositories
      </summary>
      <div className="space-y-2 px-3 pb-3 text-ui">
        {catalog.data?.manifest && (
          <p className="text-muted-foreground">
            Includes checkouts from <code>repos.yaml</code>.
          </p>
        )}
        {canManage && (
          <>
            <Button
              size="sm"
              variant="outline"
              disabled={actions.hostOffline}
              onClick={() => setManaging(true)}
            >
              Manage repositories
            </Button>
            <RepositorySettingsDialog
              open={managing}
              onOpenChange={setManaging}
              endpoint={`${base}/studio/repositories`}
              workspace={session?.workspace ?? undefined}
            />
          </>
        )}
        {repository ? (
          <>
            <select
              aria-label="Workspace repository"
              className="block h-8 w-full rounded-md border border-input bg-background px-2 text-foreground"
              value={repository.name}
              onChange={(event) => setSelected(event.target.value)}
            >
              {repositories.map((entry) => (
                <option key={entry.name} value={entry.name}>
                  {entry.name}
                </option>
              ))}
            </select>
            <div className="flex items-baseline justify-between gap-2 text-muted-foreground">
              <p className="min-w-0 wrap-break-word font-mono">{repository.path}</p>
              {repository.checkout === "ready" && <span className="shrink-0">Ready</span>}
            </div>
            {repository.description && <p className="wrap-break-word">{repository.description}</p>}
            {repository.depends_on.length > 0 && (
              <p className="wrap-break-word text-muted-foreground">
                Uses {repository.depends_on.join(", ")}
              </p>
            )}
            {repository.checkout !== "ready" ? (
              <p className="text-muted-foreground">
                {repository.checkout === "missing"
                  ? "Checkout missing on this host."
                  : "Checkout is unavailable in this workspace."}
              </p>
            ) : (
              canEdit && (
                <FindBranchPullRequest
                  key={repository.name}
                  {...actions}
                  endpoint={endpoint}
                  repository={repository.name}
                  viewer={viewer}
                />
              )
            )}
          </>
        ) : (
          <p className="text-muted-foreground">
            {catalog.isError
              ? "Couldn’t load workspace repositories."
              : "No active repositories are configured."}
          </p>
        )}
      </div>
    </details>
  );
}
