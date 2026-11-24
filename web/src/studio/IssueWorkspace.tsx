import { SessionTaskThread, TaskThreadDisclosure, useStudioThreadPage } from "./SessionTaskThread";
import { ReadableMarkdown } from "@/components/ReadableMarkdown";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { type Conversation, useProjects, useProjectSessions } from "@/hooks/useConversations";
import { useIdentityReady, useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { authenticatedFetch } from "@/lib/identity";
import { getOmnigentServerIdentity } from "@/lib/host";
import type { StudioTask } from "../../../sdks/web-extension/src/studio";
import { getProject } from "@/lib/projectsApi";
import { Link, useNavigate, useSearchParams } from "@/lib/routing";
import { getSession, updateSession } from "@/lib/sessionsApi";
import {
  isImplementationSession,
  issueWorkspaceUrl,
  linearIssueKey,
  STUDIO_ISSUE_LABEL,
} from "@/lib/studioIssue";
import { getReviewForSession, getReviewRun } from "./reviews/api";

function sessionReviewUrl(sessionId: string): string {
  return `/c/${encodeURIComponent(sessionId)}?reviewPr=1`;
}

export function SessionTaskBrief({
  projectId,
  issue,
  sessionId,
}: {
  projectId: string;
  issue: string;
  sessionId: string;
}) {
  const viewerId = useViewerId();
  const project = useQuery({
    queryKey: ["studio-task-project", viewerId, projectId],
    queryFn: () => getProject(projectId),
  });
  const task = project.data?.config?.linear_issue;
  return (
    <details className="shrink-0 border-b px-3 py-2 text-sm">
      <summary className="cursor-pointer font-medium">Task · {issue}</summary>
      <div className="mt-2 max-h-52 space-y-2 overflow-y-auto">
        {task?.identifier === issue && (
          <>
            <p className="font-medium">{task.title}</p>
            {task.description && (
              <ReadableMarkdown className="text-sm">{task.description}</ReadableMarkdown>
            )}
            {linearIssueKey(task.url) === issue && (
              <a className="underline" href={task.url} target="_blank" rel="noreferrer">
                Open in Linear
              </a>
            )}
          </>
        )}
        <Link className="block underline" to={sessionReviewUrl(sessionId)}>
          Review PR
        </Link>
        {project.data && (
          <Link className="block underline" to={issueWorkspaceUrl(project.data.name, issue)}>
            Open task and sessions
          </Link>
        )}
        {project.isError && <p role="alert">Could not load the task project.</p>}
      </div>
    </details>
  );
}

export function SharedSessionTaskBrief({
  sessionId,
  fallback,
  threadOnly = false,
}: {
  sessionId: string;
  fallback: React.ReactNode;
  threadOnly?: boolean;
}) {
  const thread = useStudioThreadPage();
  const viewer = useViewerId();
  const server = getOmnigentServerIdentity();
  const query = useQuery({
    queryKey: ["studio-shared-session-tasks", server, viewer, sessionId],
    enabled: !!viewer && !!server,
    retry: false,
    queryFn: async ({ signal }): Promise<StudioTask[]> => {
      const response = await authenticatedFetch(
        "/v1/extensions/egzakta.work/studio/v1/tasks/forSession",
        {
          method: "POST",
          signal,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId }),
        },
      );
      if (!response.ok) throw new Error("Could not load the shared task.");
      return (await response.json()).items;
    },
  });
  if (query.isError)
    return (
      <>
        {fallback}
        <p role="alert" className="px-3 py-2 text-sm">
          Could not load the shared task.
        </p>
      </>
    );
  if (threadOnly && query.isPending && query.fetchStatus === "fetching")
    return <p role="status">Loading shared tasks…</p>;
  if (!query.data?.length) return fallback;
  if (threadOnly)
    return (
      thread &&
      query.data.map((task) => (
        <section key={task.task_id}>
          <p>{task.acceptance_check || task.linear_issue_id}</p>
          <SessionTaskThread {...thread} sessionId={sessionId} taskId={task.task_id} />
        </section>
      ))
    );
  return (
    <>
      {query.data.map((task) => (
        <details key={task.task_id} className="shrink-0 border-b px-3 py-2 text-sm">
          <summary className="cursor-pointer font-medium">Task · {task.linear_issue_id}</summary>
          <div className="mt-2 max-h-52 space-y-2 overflow-y-auto">
            {task.acceptance_check && <p>Acceptance check · {task.acceptance_check}</p>}
            {task.pull_requests.map((pr) => (
              <a
                key={pr.repository + "#" + pr.number}
                className="block underline"
                href={
                  "https://github.com/" +
                  pr.repository +
                  "/pull/" +
                  pr.number +
                  "/commits/" +
                  pr.head_sha
                }
                target="_blank"
                rel="noreferrer"
              >
                {pr.repository} #{pr.number} · {pr.head_sha}
              </a>
            ))}
            <Link className="block underline" to="/extensions/egzakta.work/board">
              Open Work
            </Link>
            <Link className="block underline" to={sessionReviewUrl(sessionId)}>
              Review PR
            </Link>
          </div>
          {thread && (
            <TaskThreadDisclosure {...thread} sessionId={sessionId} taskId={task.task_id} />
          )}
        </details>
      ))}
    </>
  );
}

function IssueSession({ session }: { session: Conversation }) {
  const viewerId = useViewerId();
  const info = useServerInfo();
  const isReview =
    info !== "loading" &&
    info.studio_reviews === true &&
    session.labels["omnigent.studio.review"] === "true";
  const review = useQuery({
    queryKey: ["studio-review-session", viewerId, session.id],
    queryFn: ({ signal }) => getReviewForSession(session.id, signal),
    enabled: isReview,
  });
  const pass = review.data;
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 py-2">
      <Link className="min-w-0 break-words underline" to={`/c/${encodeURIComponent(session.id)}`}>
        {session.title || "Untitled session"}
      </Link>
      <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
        <span>
          {session.status === "running" ? "Running" : session.status === "failed" ? "Failed" : ""}
        </span>
        {isReview && pass?.run_id && (
          <Link className="underline" to={`/reviews/${encodeURIComponent(pass.run_id)}/discussion`}>
            {pass.target.repository} #{pass.target.pr_number} review
          </Link>
        )}
      </div>
    </li>
  );
}

export function IssueWorkspace() {
  const identityReady = useIdentityReady();
  const viewerId = useViewerId();
  const info = useServerInfo();
  const reviewsEnabled = info !== "loading" && info.studio_reviews === true;
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const projectName = params.get("project") ?? "";
  const issue = linearIssueKey(params.get("issue") ?? "");
  const reviewId = params.get("review") ?? "";
  const [input, setInput] = useState(issue ?? "");
  const [sessionId, setSessionId] = useState("");
  const queryClient = useQueryClient();
  const projects = useProjects({ enabled: identityReady });
  const project = projects.data?.find(({ name }) => name === projectName);
  const sessions = useProjectSessions(projectName, !!project && !!issue);
  const projectDetails = useQuery({
    queryKey: ["studio-task-project", viewerId, project?.id],
    queryFn: () => getProject(project!.id!),
    enabled: !!project?.id,
  });
  const snapshot = projectDetails.data?.config?.linear_issue;
  const task = snapshot?.identifier === issue ? snapshot : undefined;
  const review = useQuery({
    queryKey: ["studio-issue-review", viewerId, reviewId],
    queryFn: ({ signal }) => getReviewRun(reviewId, signal),
    enabled: reviewsEnabled && !!project?.id && /^[0-9a-f]{32}$/.test(reviewId),
  });
  const linkedRun =
    reviewsEnabled && project?.id && review.data?.target.source_project_id === project.id
      ? review.data
      : undefined;
  const reviewSession = [linkedRun?.coordinator, ...(linkedRun?.reviewers ?? [])].find(
    (member) => member?.reviewer_session_id,
  )?.reviewer_session_id;
  const attach = useMutation({
    mutationFn: async (id: string) => {
      if (!project?.id || !issue) throw new Error("Choose a project and issue first");
      const session = await getSession(id);
      if (session.projectId !== project.id) throw new Error("Choose a session in this project");
      const existing = session.labels?.[STUDIO_ISSUE_LABEL];
      if (existing && existing !== issue) throw new Error(`This session is linked to ${existing}`);
      await updateSession(id, { labels: { [STUDIO_ISSUE_LABEL]: issue } });
    },
    onSuccess: async () => {
      setSessionId("");
      await queryClient.invalidateQueries({ queryKey: ["project-sessions", projectName] });
    },
  });
  const loaded = sessions.data?.pages.flatMap((page) => page.data) ?? [];
  const related = loaded
    .filter((session) => session.labels[STUDIO_ISSUE_LABEL] === issue)
    .sort((left, right) => right.updated_at - left.updated_at);
  const latest = issue
    ? related.find((session) => isImplementationSession(session.labels, issue))
    : undefined;
  const { hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage } = sessions;
  useEffect(() => {
    if (issue && !latest && hasNextPage && !isFetchingNextPage && !isFetchNextPageError) {
      void fetchNextPage();
    }
  }, [issue, latest, hasNextPage, isFetchingNextPage, isFetchNextPageError, fetchNextPage]);
  const available = loaded.filter((session) => !session.labels[STUDIO_ISSUE_LABEL]);
  const composer = new URLSearchParams({
    project: projectName,
    issue: issue ?? "",
    taskWorktree: "1",
    prompt: `Read Linear issue ${issue ?? ""} using the workspace’s configured tools. Summarize its acceptance criteria and proposed next step.`,
  });

  return (
    <div className="min-w-0 flex-1 overflow-y-auto px-4 pb-8 pt-14 md:p-6">
      <main className="mx-auto max-w-2xl space-y-5">
        <header className="space-y-1">
          <Link
            className="text-sm text-muted-foreground underline"
            to={`/?${new URLSearchParams({ project: projectName })}`}
          >
            {projectName || "Workspace"}
          </Link>
          <h1 className="text-xl font-semibold">
            {task ? `${task.identifier} · ${task.title}` : (issue ?? "Work on an issue")}
          </h1>
        </header>
        {!identityReady || projects.isPending ? (
          <p role="status">Loading workspace…</p>
        ) : projects.isError ? (
          <p role="alert">Could not load workspaces.</p>
        ) : !project?.id ? (
          <p role="alert">Open this view from a saved workspace.</p>
        ) : (
          <>
            <form
              className="space-y-2"
              onSubmit={(event) => {
                event.preventDefault();
                const next = linearIssueKey(input);
                if (!next) return;
                attach.reset();
                setSessionId("");
                navigate(issueWorkspaceUrl(projectName, next, reviewId));
              }}
            >
              <label htmlFor="studio-issue" className="text-sm font-medium">
                Linear issue
              </label>
              <p className="text-sm text-muted-foreground">
                Use issue IDs from this project’s Linear workspace.
              </p>
              <div className="flex gap-2">
                <Input
                  id="studio-issue"
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  placeholder="Issue ID or Linear URL"
                />
                <Button
                  type="submit"
                  variant="outline"
                  disabled={!linearIssueKey(input) || attach.isPending}
                >
                  Open issue
                </Button>
              </div>
            </form>
            {issue && (
              <>
                <section aria-label="Linear task" className="space-y-2">
                  {task ? (
                    <>
                      {linearIssueKey(task.url) === issue && (
                        <a className="underline" href={task.url} target="_blank" rel="noreferrer">
                          Open in Linear
                        </a>
                      )}
                      {task.description && (
                        <ReadableMarkdown className="text-sm">{task.description}</ReadableMarkdown>
                      )}
                    </>
                  ) : (
                    !projectDetails.isPending && (
                      <p className="text-sm text-muted-foreground">
                        Task details are unavailable.{" "}
                        <Link className="underline" to="/extensions/egzakta.linear/board">
                          Open Linear
                        </Link>{" "}
                        to reconnect and view the issue.
                      </p>
                    )
                  )}
                  {!sessions.isPending && !sessions.isError && (latest || !hasNextPage) && (
                    <Button asChild>
                      {latest ? (
                        <Link to={`/c/${encodeURIComponent(latest.id)}`}>Resume session</Link>
                      ) : (
                        <Link to={`/?${composer}`}>Start session</Link>
                      )}
                    </Button>
                  )}
                  {latest && (
                    <div className="inline-flex flex-wrap gap-2 pl-2">
                      <Button asChild variant="outline">
                        <Link to={sessionReviewUrl(latest.id)}>Review PR</Link>
                      </Button>
                      <Button asChild variant="ghost">
                        <Link to={`/?${composer}`}>Start another session</Link>
                      </Button>
                    </div>
                  )}
                </section>
                {linkedRun && reviewSession && !related.some(({ id }) => id === reviewSession) && (
                  <section className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3">
                    <p className="text-sm">
                      {linkedRun.target.repository} #{linkedRun.target.pr_number}
                    </p>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={attach.isPending}
                      onClick={() => attach.mutate(reviewSession)}
                    >
                      Link PR review to {issue}
                    </Button>
                  </section>
                )}
                <section className="space-y-2" aria-label="Issue sessions">
                  <h2 className="font-medium">Sessions and reviews</h2>
                  {sessions.isPending && <p role="status">Loading sessions…</p>}
                  {sessions.isError && <p role="alert">Could not load project sessions.</p>}
                  {!sessions.isPending && !sessions.isError && !related.length && (
                    <p className="text-sm text-muted-foreground">
                      No linked sessions in the loaded history.
                    </p>
                  )}
                  <ul className="divide-y">
                    {related.map((session) => (
                      <IssueSession key={session.id} session={session} />
                    ))}
                  </ul>
                  {available.length > 0 && (
                    <form
                      className="flex flex-wrap items-center gap-2"
                      onSubmit={(event) => {
                        event.preventDefault();
                        if (sessionId) attach.mutate(sessionId);
                      }}
                    >
                      <select
                        aria-label="Existing project session"
                        className="min-w-0 max-w-full rounded-md border bg-background p-2 text-sm"
                        value={sessionId}
                        onChange={(event) => setSessionId(event.target.value)}
                      >
                        <option value="">Choose an existing session</option>
                        {available.map((session) => (
                          <option key={session.id} value={session.id}>
                            {session.title || "Untitled session"}
                          </option>
                        ))}
                      </select>
                      <Button
                        type="submit"
                        size="sm"
                        variant="outline"
                        disabled={!sessionId || attach.isPending}
                      >
                        Link session
                      </Button>
                    </form>
                  )}
                  {sessions.hasNextPage && (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={sessions.isFetchingNextPage}
                      onClick={() => void sessions.fetchNextPage()}
                    >
                      Load older sessions
                    </Button>
                  )}
                </section>
                {attach.error && <p role="alert">{attach.error.message}</p>}
              </>
            )}
          </>
        )}
      </main>
    </div>
  );
}
