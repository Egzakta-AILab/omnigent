import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLinkIcon, FileCheck2Icon, MessageSquareIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useMarkConversationSeen } from "@/hooks/useUnseenConversations";
import { useIdentityReady, useViewerId } from "@/hooks/useViewerId";
import { Link } from "@/lib/routing";
import { getReviewForSession, type ReviewPass } from "./api";
import { ReviewResult, reviewSettingsLabel } from "./ReviewResult";

const STATUS: Record<ReviewPass["dispatch_status"], string> = {
  not_requested: "Prepared",
  dispatching: "Starting review",
  running: "Review requested",
  succeeded: "Review requested",
  failed: "Launch failed",
  cancelled: "Cancelled",
  unknown: "Launch unconfirmed",
};

function ReviewerReport({
  review,
  viewerId,
  lastActivityAt,
}: {
  review: ReviewPass;
  viewerId: string | null;
  lastActivityAt?: number;
}) {
  const { target, reviewer_session_id: sessionId } = review;
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
        <ReviewResult review={review} viewerId={viewerId} active current={false} standalone />
      </article>
    </div>
  );
}

export function ReviewerSessionView({
  sessionId,
  requestedView,
  lastActivityAt,
  children,
}: {
  sessionId: string;
  requestedView: string | null;
  lastActivityAt?: number;
  children: ReactNode;
}) {
  const identityReady = useIdentityReady();
  const viewerId = useViewerId();
  const transcript = requestedView === "chat" || requestedView === "terminal";
  const enabled = !transcript && identityReady;
  const lookup = useQuery({
    queryKey: ["studio-review-session", viewerId, sessionId],
    queryFn: ({ signal }) => getReviewForSession(sessionId, signal),
    enabled,
    staleTime: 15_000,
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.dispatch_status === "dispatching" ? 3_000 : false,
  });

  if (transcript) return children;
  if (!identityReady || lookup.isPending) {
    return (
      <p role="status" className="pt-16 text-center text-sm text-muted-foreground">
        Loading session…
      </p>
    );
  }
  if (lookup.data && !lookup.isError)
    return (
      <ReviewerReport review={lookup.data} viewerId={viewerId} lastActivityAt={lastActivityAt} />
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
