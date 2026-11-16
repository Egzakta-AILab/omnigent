import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeftIcon,
  ArrowUpIcon,
  BotIcon,
  CheckIcon,
  ChevronRightIcon,
  FileIcon,
  FolderIcon,
  GitPullRequestIcon,
  EllipsisIcon,
  PanelRightCloseIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useHostFilesystem } from "@/hooks/useHostFilesystem";
import { useIdentityReady, useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { authenticatedFetch } from "@/lib/identity";
import { Link } from "@/lib/routing";
import { apiErrorFromResponse } from "@/lib/sessionsApi";
import { issueWorkspaceUrl } from "@/lib/studioIssue";
import { cn } from "@/lib/utils";
import { joinPath } from "@/shell/WorkspacePicker";
import { RepositorySettingsDialog, requestSettings } from "./RepositorySettingsDialog";
import type { ReviewPass, ReviewSource } from "./reviews/api";
import { ReviewDiff } from "./reviews/ReviewDiff";
import { ProjectReviewPanel } from "./reviews/ReviewPanel";
import { currentReviewRun, useProjectReviewRuns } from "./reviews/ReviewRunsPanel";

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

export function ProjectPullRequestDetails({
  projectId,
  target,
  repository,
  url,
  onBack,
  onAddPullRequest,
  onReviewStarted,
  initialView = "patch",
  previousProfile,
}: {
  projectId: string;
  target: Target;
  repository: string;
  url: string;
  onBack?: () => void;
  onAddPullRequest?: (context: ProjectPullRequestContext) => void;
  onReviewStarted?: (sessionId: string) => void;
  initialView?: "patch" | "review";
  previousProfile?: ReviewPass["profile"];
}) {
  const viewer = useViewerId();
  const [view, setView] = useState<string>(initialView);
  const info = useServerInfo();
  const reviewsEnabled = info !== "loading" && info.studio_reviews === true;
  const activeView = view === "review" && !reviewsEnabled ? "patch" : view;
  const [source, setSource] = useState<ReviewSource | null>(null);
  const params = { repository, pr_url: url };
  const endpoint = projectEndpoint(projectId, target, "pull-requests/preview", params);
  const preview = useQuery({
    queryKey: ["project-workspace-pr", viewer, endpoint],
    queryFn: ({ signal }) => readProjectResource<PullRequestPreview>(endpoint, signal),
    retry: false,
    refetchOnWindowFocus: false,
  });
  const pr = preview.data?.pr;
  const revision = pr ? { prUrl: pr.url, baseSha: pr.base_sha, headSha: pr.head_sha } : null;
  const reviewAvailable =
    reviewsEnabled && !!revision && preview.data?.review_available === true && !preview.isError;
  const reviewRuns = useProjectReviewRuns(
    viewer,
    projectId,
    revision?.prUrl ?? "",
    reviewAvailable,
  );
  const savedRuns =
    reviewAvailable && !reviewRuns.isError
      ? (reviewRuns.data?.pages.flatMap((page) => page.entries) ?? [])
      : [];
  const currentRun = revision ? currentReviewRun(savedRuns, revision) : undefined;
  const hasSavedReviews = savedRuns.some((run) => !run.target.candidate);
  const reviewStatus = currentRun
    ? "Saved review for this revision"
    : hasSavedReviews
      ? "Saved reviews for earlier revisions"
      : null;
  const currentSource =
    source &&
    pr &&
    source.prUrl === pr.url &&
    source.baseSha === pr.base_sha &&
    source.headSha === pr.head_sha
      ? source
      : null;
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
    enabled: !!pr && activeView === "patch",
    retry: false,
  });

  return (
    <div className="space-y-3 p-3">
      {onBack && (
        <Button variant="ghost" size="sm" onClick={onBack}>
          <ArrowLeftIcon /> Pull requests
        </Button>
      )}
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
            <div className="flex items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <Badge variant="secondary">#{pr.number}</Badge>
                <Badge variant="outline" className="capitalize">
                  {pr.is_draft ? "Draft" : pr.state.toLowerCase()}
                </Badge>
              </div>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    className="mobile-touch-target"
                    aria-label="Pull request actions"
                  >
                    <EllipsisIcon />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  {onAddPullRequest && (
                    <DropdownMenuItem onSelect={() => onAddPullRequest(pr)}>
                      Add PR to prompt
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuItem asChild>
                    <a href={pr.url} target="_blank" rel="noreferrer">
                      Open on GitHub
                    </a>
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    disabled={preview.isFetching}
                    onSelect={() => void preview.refetch()}
                  >
                    Refresh revision
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
            <h3 className="wrap-break-word text-base font-medium">{pr.title}</h3>
            <p className="wrap-break-word font-mono text-xs text-muted-foreground">
              {pr.head_ref} → {pr.base_ref} · {pr.head_sha.slice(0, 8)}
            </p>
            {reviewStatus && <p className="text-xs text-muted-foreground">{reviewStatus}</p>}
            {reviewAvailable && activeView !== "review" && (
              <Button size="sm" className="mobile-touch-target" onClick={() => setView("review")}>
                {currentRun ? "Open saved review" : "Review this PR"}
              </Button>
            )}
          </header>
          <Tabs value={activeView} onValueChange={setView}>
            <TabsList className="w-full justify-start">
              <TabsTrigger value="overview">Overview</TabsTrigger>
              <TabsTrigger value="patch">Diff</TabsTrigger>
              {reviewsEnabled && <TabsTrigger value="review">Review</TabsTrigger>}
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
                  <Button variant="outline" size="sm" onClick={() => void patch.refetch()}>
                    Retry diff
                  </Button>
                </div>
              )}
              {patch.data && !patch.isError && (
                <ReviewDiff
                  key={`${pr.base_sha}:${pr.head_sha}`}
                  patch={patch.data.patch}
                  truncated={patch.data.truncated}
                  source={currentSource}
                  onClearSource={() => setSource(null)}
                />
              )}
            </TabsContent>
            {reviewsEnabled && (
              <TabsContent value="review" className="pt-2">
                {reviewAvailable && revision ? (
                  <ProjectReviewPanel
                    projectId={projectId}
                    hostId={target.hostId}
                    workspace={preview.data.canonical_workspace}
                    repository={repository}
                    revision={revision}
                    hasCurrentReview={!!currentRun}
                    active={activeView === "review"}
                    previousProfile={previousProfile}
                    onOpenSource={(finding) => {
                      setSource(finding);
                      setView("patch");
                    }}
                    onReviewStarted={(review) => {
                      if (review.reviewer_session_id) onReviewStarted?.(review.reviewer_session_id);
                    }}
                  />
                ) : (
                  <WorkspaceMessage>
                    Mark this repository Active in Manage repositories to run a review.
                  </WorkspaceMessage>
                )}
              </TabsContent>
            )}
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
      <ProjectPullRequestDetails
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
          <details className="text-sm">
            <summary className="w-fit cursor-pointer text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring">
              Open by link
            </summary>
            <form
              className="mt-2 flex flex-wrap items-end gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                if (validUrl) setSelected(url.trim());
              }}
            >
              <label className="min-w-40 flex-1 space-y-1 text-xs">
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
          </details>
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
                  className="flex w-full items-center justify-between gap-2 rounded px-2 py-2.5 text-left text-sm hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
                  onClick={() => setSelected(pr.url)}
                >
                  <span className="min-w-0 space-y-1">
                    <span className="block font-medium [overflow-wrap:anywhere]">{pr.title}</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      #{pr.number} · {pr.is_draft && "Draft · "}
                      {pr.head_ref} → {pr.base_ref}
                    </span>
                  </span>
                  <ChevronRightIcon
                    className="size-4 shrink-0 text-muted-foreground"
                    aria-hidden="true"
                  />
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export function ProjectWorkspace({
  projectId,
  scope,
  target,
  loading,
  agents,
  agentId,
  onSelectAgent,
  onAddFile,
  onAddPullRequest,
  onReviewStarted,
  onClose,
  projectName,
  mobileOpen,
  onBackToChat,
}: {
  projectId: string | null;
  scope: string;
  target: Target | null;
  projectName: string;
  mobileOpen: boolean;
  onBackToChat: () => void;
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
  const [tab, setTab] = useState("files");
  return (
    <aside
      aria-label="Project workspace"
      className={cn(
        "flex min-h-0 min-w-0 flex-1 flex-col bg-background lg:flex-none lg:w-[clamp(340px,40%,620px)] lg:shrink-0 lg:border-l",
        !mobileOpen && "max-lg:hidden",
      )}
    >
      <header className="flex min-h-11 shrink-0 items-center justify-between gap-2 border-b px-3">
        <h2 className="truncate text-sm font-medium">
          <span className="lg:hidden">{projectName}</span>
          <span className="hidden lg:inline">Workspace</span>
        </h2>
        {projectId && (
          <Button asChild size="sm" variant="ghost" className="mobile-touch-target">
            <Link to={issueWorkspaceUrl(projectName)}>Issues</Link>
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          className="mobile-touch-target lg:hidden"
          onClick={onBackToChat}
        >
          <ArrowLeftIcon /> Back to chat
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          className="max-lg:hidden"
          aria-label="Collapse workspace"
          onClick={onClose}
        >
          <PanelRightCloseIcon />
        </Button>
      </header>
      <Tabs key={scope} value={tab} onValueChange={setTab} className="min-h-0 flex-1 gap-0">
        <TabsList className="grid w-full shrink-0 grid-cols-3 gap-1 rounded-none border-b px-2 py-1 group-data-horizontal/tabs:h-auto max-sm:[&_svg]:hidden">
          <TabsTrigger
            className="mobile-touch-target h-auto min-h-11 whitespace-normal lg:min-h-8"
            value="files"
          >
            <FolderIcon /> Files
          </TabsTrigger>
          <TabsTrigger
            className="mobile-touch-target h-auto min-h-11 whitespace-normal lg:min-h-8"
            value="pull-requests"
          >
            <GitPullRequestIcon /> Pull Requests
          </TabsTrigger>
          <TabsTrigger
            className="mobile-touch-target h-auto min-h-11 whitespace-normal lg:min-h-8"
            value="agents"
          >
            <BotIcon /> Agents
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
          <div className="space-y-3 p-3">
            <div className="space-y-1">
              <h3 className="text-sm font-medium">Session agent</h3>
              <p className="text-xs text-muted-foreground">
                Choose the agent for your next session.
              </p>
            </div>
            {agents.length ? (
              <ul className="space-y-1">
                {agents.map((agent) => (
                  <li key={agent.id}>
                    <Button
                      variant={agent.id === agentId ? "secondary" : "ghost"}
                      size="sm"
                      className="w-full justify-between"
                      aria-pressed={agent.id === agentId}
                      onClick={() => onSelectAgent(agent.id)}
                    >
                      <span className="min-w-0 truncate">{agent.name}</span>
                      {agent.id === agentId && <CheckIcon aria-hidden="true" />}
                    </Button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">No session agents available.</p>
            )}
          </div>
        </TabsContent>
      </Tabs>
    </aside>
  );
}
