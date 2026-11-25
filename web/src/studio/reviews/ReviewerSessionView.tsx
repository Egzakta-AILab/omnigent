import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLinkIcon, FileCheck2Icon, MessageSquareIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useMarkConversationSeen } from "@/hooks/useUnseenConversations";
import { useIdentityReady, useViewerId } from "@/hooks/useViewerId";
import { copyText } from "@/lib/clipboard";
import { Link } from "@/lib/routing";
import {
  getReviewForSession,
  getReviewRunReport,
  type ReviewFinding,
  type ReviewPass,
} from "./api";
import { ReviewResult, reviewSettingsLabel } from "./ReviewResult";
import { ReviewRunsPanel } from "./ReviewRunsPanel";

const STATUS: Record<ReviewPass["dispatch_status"], string> = {
  not_requested: "Prepared",
  dispatching: "Starting review",
  running: "Review requested",
  succeeded: "Review requested",
  failed: "Launch failed",
  cancelled: "Cancelled",
  unknown: "Launch unconfirmed",
};

function HistoricalFindingActions({
  review,
  finding,
  sessionId,
}: {
  review: ReviewPass;
  finding: ReviewFinding;
  sessionId: string;
}) {
  const [copyStatus, setCopyStatus] = useState("");
  const { target } = review;
  const prompt = [
    `Follow up on this finding from ${target.repository} PR #${target.pr_number}.`,
    `Reviewed revision: ${target.base_sha} → ${target.head_sha}.`,
    `${finding.path}:${finding.line} (${finding.side} side) · ${finding.title}`,
    finding.detail,
    "Explain what the reviewed diff supports and what would need a check to confirm.",
  ].join("\n\n");
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button asChild size="sm" variant="outline">
        <Link to={`/c/${encodeURIComponent(sessionId)}?view=chat`}>Ask in original transcript</Link>
      </Button>
      <Button
        size="sm"
        variant="ghost"
        onClick={() =>
          void copyText(prompt).then(
            () => setCopyStatus("Copied. Paste into the original transcript."),
            () => setCopyStatus("Couldn’t copy the prompt."),
          )
        }
      >
        Copy follow-up prompt
      </Button>
      {copyStatus && <span role="status">{copyStatus}</span>}
    </div>
  );
}

function ReviewerReport({
  review,
  viewerId,
  lastActivityAt,
  onOpenPullRequest,
}: {
  review: ReviewPass;
  viewerId: string | null;
  lastActivityAt?: number;
  onOpenPullRequest?: () => void;
}) {
  const { target, reviewer_session_id: sessionId, run_id: runId } = review;
  const consolidation = useQuery({
    queryKey: ["studio-review-run-report", viewerId, runId],
    queryFn: ({ signal }) => getReviewRunReport(runId!, signal),
    enabled: !!runId && review.run_role !== "coordinator",
    retry: false,
    refetchInterval: ({ state }) =>
      state.data?.reviewers.some(
        (entry) => entry.review_id === review.id && entry.report.state === "pending",
      )
        ? 3_000
        : false,
  });
  const savedDiffUnavailable = consolidation.data?.reviewers.some(
    (entry) => entry.review_id === review.id && entry.report.citation_validation === "unavailable",
  );
  const discussionPath =
    runId && !savedDiffUnavailable ? `/reviews/${encodeURIComponent(runId)}/discussion` : null;
  const findings = consolidation.isError ? [] : (consolidation.data?.findings ?? []);
  const findingKeys = new Map(
    findings
      .filter(({ review_ids }) => review_ids.includes(review.id))
      .map(({ finding, finding_key }) => [JSON.stringify(finding), finding_key]),
  );
  useMarkConversationSeen(sessionId ?? undefined, lastActivityAt);
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-8 pt-16 sm:px-7">
      <article className="mx-auto max-w-3xl space-y-5 text-sm">
        <header className="space-y-3 rounded-xl border border-border bg-card p-4 sm:p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0 space-y-1">
              <p className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
                <FileCheck2Icon className="size-4 shrink-0" aria-hidden="true" />
                Private review
              </p>
              <h1 className="text-lg font-semibold sm:text-xl">Pull request review</h1>
              <p className="text-muted-foreground [overflow-wrap:anywhere]">
                {target.repository} #{target.pr_number}
              </p>
            </div>
            <div className="flex flex-wrap gap-1">
              {sessionId && (
                <Button asChild variant="outline" size="sm">
                  <Link to={`/c/${encodeURIComponent(sessionId)}?view=chat`}>
                    <MessageSquareIcon data-icon="inline-start" />
                    Open transcript
                  </Link>
                </Button>
              )}
              {onOpenPullRequest && (
                <Button variant="outline" size="sm" onClick={onOpenPullRequest}>
                  Open PR in Workspace
                </Button>
              )}
              <Button asChild variant="ghost" size="sm">
                <a href={target.pr_url} target="_blank" rel="noreferrer">
                  Open pull request <ExternalLinkIcon data-icon="inline-end" />
                </a>
              </Button>
            </div>
          </div>
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 border-t border-border pt-3">
            <dt className="text-muted-foreground">Reviewer</dt>
            <dd className="[overflow-wrap:anywhere]">{reviewSettingsLabel(review)}</dd>
            <dt className="text-muted-foreground">Revision</dt>
            <dd className="font-mono" title={`${target.base_sha} → ${target.head_sha}`}>
              {target.base_sha.slice(0, 8)} → {target.head_sha.slice(0, 8)}
            </dd>
            <dt className="text-muted-foreground">Evidence</dt>
            <dd>Captured pull request patch</dd>
            <dt className="text-muted-foreground">Launch</dt>
            <dd>
              <Badge variant="secondary">{STATUS[review.dispatch_status]}</Badge>
            </dd>
          </dl>
        </header>
        {discussionPath ? (
          <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
            <Button asChild variant="outline" size="sm">
              <Link to={discussionPath}>Discuss review</Link>
            </Button>
            <p>Open the captured diff, reviewer reports, checks, and inline comment drafts.</p>
          </div>
        ) : (
          <section className="space-y-2 rounded-md border border-border p-3">
            <h2 className="font-medium">Earlier review</h2>
            <p className="text-muted-foreground">
              This report has no saved diff for a finding thread. Ask about its finding in the
              original transcript. To draft an inline comment in Studio, review the current PR
              revision first.
            </p>
            {onOpenPullRequest && target.source_project_id && (
              <Button size="sm" variant="outline" onClick={onOpenPullRequest}>
                Set up review of current PR
              </Button>
            )}
          </section>
        )}
        <ReviewResult
          review={review}
          viewerId={viewerId}
          active
          current={false}
          standalone
          renderFindingActions={(finding) => {
            const key = findingKeys.get(JSON.stringify(finding));
            return discussionPath && key ? (
              <Button asChild variant="outline" size="sm">
                <Link to={`${discussionPath}?finding=${encodeURIComponent(key)}`}>
                  Open finding
                </Link>
              </Button>
            ) : !discussionPath && sessionId ? (
              <HistoricalFindingActions review={review} finding={finding} sessionId={sessionId} />
            ) : null;
          }}
        />
      </article>
    </div>
  );
}

export function useReviewerSessionLookup(sessionId: string, enabled: boolean) {
  const viewerId = useViewerId();
  const identityReady = useIdentityReady();
  return useQuery({
    queryKey: ["studio-review-session", viewerId, sessionId],
    queryFn: ({ signal }) => getReviewForSession(sessionId, signal),
    enabled: enabled && identityReady,
    staleTime: 15_000,
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.dispatch_status === "dispatching" ? 3_000 : false,
  });
}

export function ReviewerSessionView({
  sessionId,
  requestedView,
  lastActivityAt,
  onOpenPullRequest,
  children,
}: {
  sessionId: string;
  requestedView: string | null;
  lastActivityAt?: number;
  onOpenPullRequest?: () => void;
  children: ReactNode;
}) {
  const identityReady = useIdentityReady();
  const viewerId = useViewerId();
  const transcript = requestedView === "chat" || requestedView === "terminal";
  const lookup = useReviewerSessionLookup(sessionId, !transcript);

  if (transcript) return children;
  if (!identityReady || lookup.isPending) {
    return (
      <p role="status" className="pt-16 text-center text-sm text-muted-foreground">
        Loading session…
      </p>
    );
  }
  if (lookup.data?.run_role === "coordinator" && !lookup.isError) {
    const { target, run_id: runId } = lookup.data;
    if (runId && target.source_project_id) {
      return (
        <section
          aria-label="Orchestrated review"
          className="min-h-0 flex-1 overflow-y-auto px-4 pb-8 pt-16 sm:px-7"
        >
          <div className="mx-auto max-w-3xl space-y-4">
            <header className="space-y-2">
              <h1 className="text-lg font-semibold">Orchestrator · PR #{target.pr_number}</h1>
              <a
                className="text-sm underline"
                href={target.pr_url}
                target="_blank"
                rel="noreferrer"
              >
                {target.repository} on GitHub
              </a>
              <div className="flex flex-wrap gap-2">
                <Button asChild size="sm" variant="outline">
                  <Link to={`/c/${encodeURIComponent(sessionId)}?view=chat`}>Open transcript</Link>
                </Button>
                {onOpenPullRequest && (
                  <Button size="sm" variant="outline" onClick={onOpenPullRequest}>
                    Open PR in Workspace
                  </Button>
                )}
              </div>
            </header>
            <ReviewRunsPanel
              projectId={target.source_project_id}
              revision={{
                prUrl: target.pr_url,
                baseSha: target.base_sha,
                headSha: target.head_sha,
              }}
              viewerId={viewerId}
              selectedRunId={runId}
              onSelectRun={() => {}}
              fixedRun
              launchPending={false}
            />
          </div>
        </section>
      );
    }
  }
  if (lookup.data && !lookup.isError)
    return (
      <ReviewerReport
        review={lookup.data}
        viewerId={viewerId}
        lastActivityAt={lastActivityAt}
        onOpenPullRequest={onOpenPullRequest}
      />
    );
  if (requestedView === "review") {
    return (
      <div className="flex flex-wrap items-center justify-center gap-2 px-4 pt-16 text-sm">
        <p role="alert" className="text-muted-foreground">
          Review report is unavailable.
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void lookup.refetch()}
          disabled={lookup.isFetching}
        >
          Retry report
        </Button>
        <Button asChild variant="ghost" size="sm">
          <Link to={`/c/${encodeURIComponent(sessionId)}?view=chat`}>Open transcript</Link>
        </Button>
      </div>
    );
  }
  return children;
}
