import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeftIcon,
  ArrowUpIcon,
  BotIcon,
  FileIcon,
  FolderIcon,
  GitPullRequestIcon,
  ListChecksIcon,
  PanelRightCloseIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { useIdentityReady, useViewerId } from "@/hooks/useViewerId";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";
import { joinPath } from "@/shell/WorkspacePicker";
import { RepositorySettingsDialog, requestSettings } from "./RepositorySettingsDialog";
import { getReviewProfileEditor } from "./reviews/api";
import { ProfileEditorDialog } from "./reviews/ProfileEditorDialog";
import { ProjectReviewPanel } from "./reviews/ReviewPanel";

export interface ProjectPullRequestContext {
  number: number;
  url: string;
  base_sha: string;
  head_sha: string;
}

interface Target {
  hostId: string;
  workspace: string;
}

interface PullRequestLink {
  number: number;
  title: string;
  url: string;
  is_draft: boolean;
  base_ref: string;
  head_ref: string;
}

interface PullRequestPreview {
  pr: PullRequestLink & ProjectPullRequestContext & { state: string; body: string | null };
  files: { path: string; status: string; lines_added: number; lines_removed: number }[];
  files_truncated: boolean;
  review_available: boolean;
  canonical_workspace: string;
}

function projectEndpoint(projectId: string, target: Target, resource: string, extra = {}) {
  const params = new URLSearchParams({
    host_id: target.hostId,
    workspace: target.workspace,
    ...extra,
  });
  return `/v1/projects/${encodeURIComponent(projectId)}/studio/${resource}?${params}`;
}

async function readProjectResource<T>(endpoint: string, signal: AbortSignal): Promise<T> {
  const response = await authenticatedFetch(endpoint, { signal });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as T;
}

function WorkspaceMessage({ children }: { children: React.ReactNode }) {
  return <p className="px-3 py-2 text-sm text-muted-foreground">{children}</p>;
}

function WorkspaceFiles({
  projectId,
  target,
  onAddFile,
}: {
  projectId: string | null;
  target: Target;
  onAddFile: (path: string) => void;
}) {
  const [segments, setSegments] = useState<string[]>([]);
  const [file, setFile] = useState<string | null>(null);
  const directory = segments.join("/");
  const path = segments.reduce(joinPath, target.workspace);
  const listing = useHostFilesystem(target.hostId, path);
  const viewer = useViewerId();
  const previewEndpoint =
    projectId && file ? projectEndpoint(projectId, target, "files", { path: file }) : null;
  const preview = useQuery({
    queryKey: ["project-workspace-file", viewer, previewEndpoint],
    queryFn: ({ signal }) =>
      readProjectResource<{ content: string | null; truncated: boolean }>(previewEndpoint!, signal),
    enabled: previewEndpoint !== null,
    retry: false,
  });

  if (file !== null) {
    return (
      <div className="space-y-2 p-3">
        <Button variant="ghost" size="sm" onClick={() => setFile(null)}>
          <ArrowLeftIcon /> Files
        </Button>
        <p className="wrap-break-word font-mono text-xs">{file}</p>
        <Button
          variant="outline"
          size="sm"
          disabled={!!previewEndpoint && !preview.isSuccess}
          onClick={() => onAddFile(file)}
        >
          Add file to prompt
        </Button>
        {preview.isPending && previewEndpoint && <WorkspaceMessage>Loading file…</WorkspaceMessage>}
        {preview.error && (
          <p role="alert" className="text-sm">
            {preview.error.message}
          </p>
        )}
        {preview.data && (
          <>
            {preview.data.content === null ? (
              <WorkspaceMessage>Preview unavailable for this file type.</WorkspaceMessage>
            ) : (
              <pre className="overflow-x-auto rounded-md border bg-muted/40 p-3 text-xs">
                {preview.data.content}
              </pre>
            )}
            {preview.data.truncated && <WorkspaceMessage>Preview shortened.</WorkspaceMessage>}
          </>
        )}
      </div>
    );
  }

  const entries = listing.isPlaceholderData ? [] : (listing.data?.entries ?? []);
  return (
    <>
      <div className="flex items-center gap-1 border-b px-2 py-1">
        <Button
          size="icon-sm"
          variant="ghost"
          aria-label="Parent folder"
          disabled={!segments.length}
          onClick={() => setSegments((current) => current.slice(0, -1))}
        >
          <ArrowUpIcon />
        </Button>
        <p className="truncate font-mono text-xs text-muted-foreground" title={path}>
          {directory || target.workspace}
        </p>
      </div>
      {listing.error && (
        <p role="alert" className="p-3 text-sm">
          {listing.error.message}
        </p>
      )}
      {(listing.isPending || listing.isPlaceholderData) && (
        <WorkspaceMessage>Loading files…</WorkspaceMessage>
      )}
      {listing.isSuccess && !listing.isPlaceholderData && !entries.length && (
        <WorkspaceMessage>This folder is empty.</WorkspaceMessage>
      )}
      <ul className="p-1">
        {entries.map((entry) => {
          if (
            (entry.type !== "directory" && entry.type !== "file") ||
            !entry.name ||
            entry.name === "." ||
            entry.name === ".." ||
            /[\r\n/\\]/.test(entry.name)
          )
            return null;
          const isDirectory = entry.type === "directory";
          return (
            <li key={entry.path}>
              <button
                type="button"
                className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
                onClick={() =>
                  isDirectory
                    ? setSegments((current) => [...current, entry.name])
                    : setFile([...segments, entry.name].join("/"))
                }
              >
                {isDirectory ? (
                  <FolderIcon className="size-4 shrink-0 text-muted-foreground" />
                ) : (
                  <FileIcon className="size-4 shrink-0 text-muted-foreground" />
                )}
                <span className="truncate" title={entry.name}>
                  {entry.name}
                  {isDirectory && "/"}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {listing.data?.truncated && (
        <WorkspaceMessage>More entries exist in this folder.</WorkspaceMessage>
      )}
    </>
  );
}

function PullRequestDetails({
  projectId,
  target,
  repository,
  url,
  onBack,
  onAddPullRequest,
  onReviewStarted,
}: {
  projectId: string;
  target: Target;
  repository: string;
  url: string;
  onBack: () => void;
  onAddPullRequest: (context: ProjectPullRequestContext) => void;
  onReviewStarted: (sessionId: string) => void;
}) {
  const viewer = useViewerId();
  const [view, setView] = useState("overview");
  const params = { repository, pr_url: url };
  const endpoint = projectEndpoint(projectId, target, "pull-requests/preview", params);
  const preview = useQuery({
    queryKey: ["project-workspace-pr", viewer, endpoint],
    queryFn: ({ signal }) => readProjectResource<PullRequestPreview>(endpoint, signal),
    retry: false,
    refetchOnWindowFocus: false,
  });
  const pr = preview.data?.pr;
  const patch = useQuery({
    queryKey: ["project-workspace-patch", viewer, endpoint, pr?.base_sha, pr?.head_sha],
    queryFn: ({ signal }) =>
      readProjectResource<{ patch: string; truncated: boolean }>(
        projectEndpoint(projectId, target, "pull-requests/patch", {
          ...params,
          base_sha: pr!.base_sha,
          head_sha: pr!.head_sha,
        }),
        signal,
      ),
    enabled: !!pr && view === "patch",
    retry: false,
  });

  return (
    <div className="space-y-3 p-3">
      <Button variant="ghost" size="sm" onClick={onBack}>
        <ArrowLeftIcon /> Pull requests
      </Button>
      {preview.isPending && <WorkspaceMessage>Loading pull request…</WorkspaceMessage>}
      {preview.error && (
        <div className="space-y-2">
          <p role="alert" className="text-sm text-destructive">
            {preview.error.message}
          </p>
          <Button variant="outline" size="sm" onClick={() => void preview.refetch()}>
            Retry
          </Button>
        </div>
      )}
      {preview.data && pr && (
        <>
          <header className="space-y-2">
            <div className="flex flex-wrap gap-2 text-xs">
              <Badge variant="secondary">#{pr.number}</Badge>
              <Badge variant="outline" className="capitalize">
                {pr.is_draft ? "Draft" : pr.state.toLowerCase()}
              </Badge>
            </div>
            <h3 className="wrap-break-word text-base font-medium">{pr.title}</h3>
            <p className="wrap-break-word font-mono text-xs text-muted-foreground">
              {pr.head_ref} → {pr.base_ref} · {pr.head_sha.slice(0, 8)}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" size="sm" onClick={() => onAddPullRequest(pr)}>
                Add PR to prompt
              </Button>
              <Button asChild variant="ghost" size="sm">
                <a href={pr.url} target="_blank" rel="noreferrer">
                  Open on GitHub
                </a>
              </Button>
              <Button
                variant="ghost"
                size="sm"
                disabled={preview.isFetching}
                onClick={() => void preview.refetch()}
              >
                Refresh revision
              </Button>
            </div>
          </header>
          <Tabs value={view} onValueChange={setView}>
            <TabsList className="w-full justify-start">
              <TabsTrigger value="overview">Overview</TabsTrigger>
              <TabsTrigger value="patch">Diff</TabsTrigger>
              <TabsTrigger value="review">Review</TabsTrigger>
            </TabsList>
            <TabsContent value="overview" className="space-y-3 pt-2">
              {pr.body && (
                <p className="max-h-48 overflow-auto text-sm whitespace-pre-wrap [overflow-wrap:anywhere]">
                  {pr.body}
                </p>
              )}
              <h4 className="text-sm font-medium">Changed files</h4>
              <ul className="divide-y text-xs">
                {preview.data.files.map((file) => (
                  <li key={file.path} className="flex items-start justify-between gap-3 py-2">
                    <span className="min-w-0 font-mono [overflow-wrap:anywhere]">{file.path}</span>
                    <span className="shrink-0 text-muted-foreground">
                      +{file.lines_added} −{file.lines_removed}
                    </span>
                  </li>
                ))}
              </ul>
              {preview.data.files_truncated && (
                <WorkspaceMessage>File list shortened.</WorkspaceMessage>
              )}
              {!preview.data.files.length && (
                <WorkspaceMessage>No changed files to display.</WorkspaceMessage>
              )}
            </TabsContent>
            <TabsContent value="patch" className="pt-2">
              {patch.isPending && <WorkspaceMessage>Loading diff…</WorkspaceMessage>}
              {patch.error && (
                <div className="space-y-2">
                  <p role="alert" className="text-sm text-destructive">
                    {patch.error.message}
                  </p>
                  <Button variant="outline" size="sm" onClick={() => void preview.refetch()}>
                    Refresh revision
                  </Button>
                </div>
              )}
              {patch.data && (
                <>
                  <pre className="overflow-x-auto rounded-md border bg-muted/40 p-3 text-xs">
                    {patch.data.patch}
                  </pre>
                  {patch.data.truncated && (
                    <WorkspaceMessage>Diff preview shortened.</WorkspaceMessage>
                  )}
                </>
              )}
            </TabsContent>
            <TabsContent value="review" className="pt-2">
              {preview.data.review_available ? (
                <ProjectReviewPanel
                  projectId={projectId}
                  hostId={target.hostId}
                  workspace={preview.data.canonical_workspace}
                  repository={repository}
                  revision={{ prUrl: pr.url, baseSha: pr.base_sha, headSha: pr.head_sha }}
                  active={view === "review"}
                  onReviewStarted={(review) => {
                    if (review.reviewer_session_id) onReviewStarted(review.reviewer_session_id);
                  }}
                />
              ) : (
                <WorkspaceMessage>
                  Mark this repository Active in Manage repositories to run a review.
                </WorkspaceMessage>
              )}
            </TabsContent>
          </Tabs>
        </>
      )}
    </div>
  );
}

function WorkspacePullRequests({
  projectId,
  target,
  onAddPullRequest,
  onReviewStarted,
}: {
  projectId: string;
  target: Target;
  onAddPullRequest: (context: ProjectPullRequestContext) => void;
  onReviewStarted: (sessionId: string) => void;
}) {
  const viewer = useViewerId();
  const [repositoryChoice, setRepositoryChoice] = useState("");
  const [url, setUrl] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const repositoryEndpoint = projectEndpoint(projectId, target, "repositories");
  const catalog = useQuery({
    queryKey: ["studio-repository-settings", viewer, repositoryEndpoint],
    queryFn: ({ signal }) => requestSettings(repositoryEndpoint, { signal }),
    retry: false,
  });
  const repositories = (catalog.data?.repositories ?? []).filter(
    (repo) => repo.checkout === "ready" && repo.role !== "excluded",
  );
  const activeRepositories = repositories.filter((repo) => repo.role === "active");
  // Prefer a single active checkout; ask when there is more than one.
  const repository = repositories.some((repo) => repo.path === repositoryChoice)
    ? repositoryChoice
    : activeRepositories.length === 1
      ? activeRepositories[0].path
      : repositories.length === 1
        ? repositories[0].path
        : "";
  const endpoint = projectEndpoint(projectId, target, "pull-requests", { repository });
  const pullRequests = useQuery({
    queryKey: ["project-workspace-pull-requests", viewer, endpoint],
    queryFn: ({ signal }) =>
      readProjectResource<{ pull_requests: PullRequestLink[] }>(endpoint, signal),
    enabled: !!repository && !selected,
    retry: false,
    refetchOnWindowFocus: false,
  });
  const validUrl = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/[1-9]\d*\/?$/i.test(url.trim());
  if (selected && repository) {
    return (
      <PullRequestDetails
        key={`${repository}:${selected}`}
        projectId={projectId}
        target={target}
        repository={repository}
        url={selected}
        onBack={() => setSelected(null)}
        onAddPullRequest={onAddPullRequest}
        onReviewStarted={onReviewStarted}
      />
    );
  }
  return (
    <div className="space-y-3 p-3">
      <div className="flex items-end gap-2">
        <label className="min-w-0 flex-1 space-y-1 text-xs">
          <span>Repository</span>
          <select
            className="h-8 w-full rounded-md border bg-background px-2 text-sm"
            value={repository}
            disabled={!repositories.length}
            onChange={(event) => setRepositoryChoice(event.target.value)}
          >
            <option value="">{catalog.isPending ? "Loading…" : "Choose a repository"}</option>
            {repositories.map((repo) => (
              <option key={repo.path} value={repo.path}>
                {repo.path === "." ? "Workspace root" : repo.path}
              </option>
            ))}
          </select>
        </label>
        <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
          Manage repositories
        </Button>
      </div>
      {editing && (
        <RepositorySettingsDialog
          open
          onOpenChange={setEditing}
          endpoint={repositoryEndpoint}
          workspace={target.workspace}
        />
      )}
      {catalog.error && (
        <p role="alert" className="text-sm text-destructive">
          {catalog.error.message}
        </p>
      )}
      {catalog.isSuccess && !repositories.length && (
        <WorkspaceMessage>Scan the workspace to find repositories.</WorkspaceMessage>
      )}
      {repository && (
        <>
          <form
            className="flex items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (validUrl) setSelected(url.trim());
            }}
          >
            <label className="min-w-0 flex-1 space-y-1 text-xs">
              <span>GitHub pull request URL</span>
              <Input
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://github.com/…/pull/123"
              />
            </label>
            <Button type="submit" variant="outline" size="sm" disabled={!validUrl}>
              Open PR
            </Button>
          </form>
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-sm font-medium">Open pull requests</h3>
            <Button
              variant="ghost"
              size="sm"
              disabled={pullRequests.isFetching}
              onClick={() => void pullRequests.refetch()}
            >
              Refresh
            </Button>
          </div>
          {pullRequests.isPending && <WorkspaceMessage>Loading pull requests…</WorkspaceMessage>}
          {pullRequests.error && (
            <p role="alert" className="text-sm text-destructive">
              {pullRequests.error.message}
            </p>
          )}
          {pullRequests.isSuccess && !pullRequests.data.pull_requests.length && (
            <WorkspaceMessage>No open pull requests.</WorkspaceMessage>
          )}
          <ul className="divide-y">
            {(pullRequests.data?.pull_requests ?? []).map((pr) => (
              <li key={pr.number}>
                <button
                  type="button"
                  className="w-full space-y-1 rounded px-2 py-2 text-left text-sm hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
                  onClick={() => setSelected(pr.url)}
                >
                  <span className="block [overflow-wrap:anywhere]">{pr.title}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    #{pr.number} · {pr.is_draft && "Draft · "}
                    {pr.head_ref} → {pr.base_ref}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function WorkspaceProfiles({ projectId, target }: { projectId: string; target: Target }) {
  const [editing, setEditing] = useState(false);
  const viewer = useViewerId();
  const endpoint = projectEndpoint(projectId, target, "review-profiles/editor");
  const catalog = useQuery({
    queryKey: ["studio-review-profile-editor", viewer, endpoint, target.hostId, target.workspace],
    queryFn: ({ signal }) => getReviewProfileEditor(endpoint, signal),
    retry: false,
  });
  const profiles = (catalog.data?.profiles ?? []).filter((profile) => !profile.archived);
  return (
    <div className="space-y-3 p-3">
      <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
        Manage reviewer profiles
      </Button>
      {editing && (
        <ProfileEditorDialog
          open
          onOpenChange={setEditing}
          endpoint={endpoint}
          hostId={target.hostId}
          workspace={target.workspace}
        />
      )}
      {catalog.isPending && <WorkspaceMessage>Loading reviewer profiles…</WorkspaceMessage>}
      {catalog.error && (
        <p role="alert" className="text-sm text-destructive">
          {catalog.error.message}
        </p>
      )}
      {catalog.isSuccess && !profiles.length && (
        <WorkspaceMessage>No reviewer profiles in this workspace.</WorkspaceMessage>
      )}
      <ul className="divide-y">
        {profiles.map((profile) => (
          <li key={profile.id} className="space-y-1 py-2">
            <h3 className="text-sm font-medium">{profile.label}</h3>
            <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
              {profile.description}
            </p>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ProjectWorkspace({
  projectId,
  target,
  loading,
  agents,
  agentId,
  onSelectAgent,
  onAddFile,
  onAddPullRequest,
  onReviewStarted,
  onClose,
}: {
  projectId: string | null;
  target: Target | null;
  loading: boolean;
  agents: { id: string; name: string }[];
  agentId: string | null;
  onSelectAgent: (id: string) => void;
  onAddFile: (path: string) => void;
  onAddPullRequest: (context: ProjectPullRequestContext) => void;
  onReviewStarted: (sessionId: string) => void;
  onClose: () => void;
}) {
  const identityReady = useIdentityReady();
  return (
    <aside
      aria-label="Project workspace"
      className="flex min-h-0 min-w-0 flex-1 flex-col border-b bg-background lg:flex-none lg:w-[clamp(340px,40%,620px)] lg:shrink-0 lg:border-b-0 lg:border-l"
    >
      <header className="flex min-h-11 shrink-0 items-center justify-between gap-2 border-b px-3">
        <h2 className="text-sm font-medium">Workspace</h2>
        <Button variant="ghost" size="icon-sm" aria-label="Collapse workspace" onClick={onClose}>
          <PanelRightCloseIcon />
        </Button>
      </header>
      <Tabs defaultValue="files" className="min-h-0 flex-1 gap-0">
        <TabsList className="h-auto w-full shrink-0 flex-wrap justify-start rounded-none border-b px-2 py-1">
          <TabsTrigger value="files">
            <FolderIcon /> Files
          </TabsTrigger>
          <TabsTrigger value="pull-requests">
            <GitPullRequestIcon /> Pull Requests
          </TabsTrigger>
          <TabsTrigger value="agents">
            <BotIcon /> Agents
          </TabsTrigger>
          <TabsTrigger value="profiles">
            <ListChecksIcon /> Profiles
          </TabsTrigger>
        </TabsList>
        <TabsContent value="files" className="min-h-0 overflow-auto">
          {target && identityReady ? (
            <WorkspaceFiles projectId={projectId} target={target} onAddFile={onAddFile} />
          ) : (
            <WorkspaceMessage>
              {loading || !identityReady
                ? "Loading workspace…"
                : "Choose an online host and working directory in the composer."}
            </WorkspaceMessage>
          )}
        </TabsContent>
        <TabsContent value="pull-requests" className="min-h-0 overflow-auto">
          {target && projectId && identityReady ? (
            <WorkspacePullRequests
              projectId={projectId}
              target={target}
              onAddPullRequest={onAddPullRequest}
              onReviewStarted={onReviewStarted}
            />
          ) : (
            <WorkspaceMessage>
              {target && identityReady
                ? "Save this project to browse pull requests."
                : "Connect a workspace to browse pull requests."}
            </WorkspaceMessage>
          )}
        </TabsContent>
        <TabsContent value="agents" className="min-h-0 overflow-auto">
          {agents.length ? (
            <ul className="space-y-1 p-3">
              {agents.map((agent) => (
                <li key={agent.id}>
                  <Button
                    variant={agent.id === agentId ? "secondary" : "ghost"}
                    size="sm"
                    className="w-full justify-start"
                    aria-pressed={agent.id === agentId}
                    onClick={() => onSelectAgent(agent.id)}
                  >
                    <BotIcon /> {agent.name}
                  </Button>
                </li>
              ))}
            </ul>
          ) : (
            <WorkspaceMessage>Agent choices appear here when available.</WorkspaceMessage>
          )}
        </TabsContent>
        <TabsContent value="profiles" className="min-h-0 overflow-auto">
          {target && projectId && identityReady ? (
            <WorkspaceProfiles projectId={projectId} target={target} />
          ) : (
            <WorkspaceMessage>
              Open a saved project on a connected host to browse reviewer profiles.
            </WorkspaceMessage>
          )}
        </TabsContent>
      </Tabs>
    </aside>
  );
}
