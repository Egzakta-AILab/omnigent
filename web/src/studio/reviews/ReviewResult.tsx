import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExternalLinkIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useSession } from "@/hooks/useSession";
import { Link } from "@/lib/routing";
import { pullRequestFileUrl } from "@/shell/pullRequestReview";
import {
  getReviewReport,
  REVIEW_HARNESS_LABELS,
  type ReviewFinding,
  type ReviewPass,
  type ReviewSource,
} from "./api";

const POLL_MS = 3_000;

export function uniqueReviewFindings(findings: ReviewFinding[] | undefined) {
  return new Map(findings?.map((finding) => [JSON.stringify(finding), finding]));
}

export function reviewSettingsLabel(review: ReviewPass): string {
  const { role, skill, harness, model, reasoning_effort } = review.profile;
  return [role?.label, skill, REVIEW_HARNESS_LABELS[harness], model, reasoning_effort]
    .filter(Boolean)
    .join(" · ");
}

function reviewSource(review: ReviewPass, finding: ReviewFinding): ReviewSource {
  return {
    prUrl: review.target.pr_url,
    baseSha: review.target.base_sha,
    headSha: review.target.head_sha,
    path: finding.path,
    side: finding.side,
    line: finding.line,
  };
}

export function FindingCard({
  finding,
  review,
  current,
  headRepoUrl,
  onOpenSource,
  onOpenCapturedSource,
  children,
}: {
  finding: ReviewFinding;
  review: ReviewPass;
  current: boolean;
  headRepoUrl?: string | null;
  onOpenSource?: (source: ReviewSource) => void;
  onOpenCapturedSource?: (source: ReviewSource) => void;
  children?: ReactNode;
}) {
  const source = reviewSource(review, finding);
  // GitHub's old diff side uses the merge base, which can differ from the PR base tip.
  const blobUrl =
    current && source.side === "head"
      ? pullRequestFileUrl({
          provider: "github",
          baseRepoUrl: null,
          headRepoUrl,
          baseSha: null,
          headSha: source.headSha,
          path: source.path,
          status: "modified",
        })
      : null;
  return (
    <li className="space-y-2 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-start gap-2">
        <Badge
          variant={
            finding.severity === "critical" || finding.severity === "high"
              ? "destructive"
              : "outline"
          }
          className="capitalize"
        >
          {finding.severity}
        </Badge>
        <h4 className="min-w-0 font-medium [overflow-wrap:anywhere]">{finding.title}</h4>
      </div>
      <p className="text-muted-foreground whitespace-pre-wrap [overflow-wrap:anywhere]">
        {finding.detail}
      </p>
      <p className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">
        {finding.path}:{finding.line} · {finding.side === "base" ? "Base" : "Head"}
      </p>
      {((current && onOpenSource) || onOpenCapturedSource || blobUrl) && (
        <div className="flex flex-wrap items-center gap-1">
          {current && onOpenSource && (
            <Button variant="outline" size="sm" onClick={() => onOpenSource(source)}>
              Show in diff
            </Button>
          )}
          {onOpenCapturedSource && (
            <Button variant="outline" size="sm" onClick={() => onOpenCapturedSource(source)}>
              Show in captured diff
            </Button>
          )}
          {blobUrl && (
            <Button asChild variant="ghost" size="sm">
              <a href={`${blobUrl}#L${finding.line}`} target="_blank" rel="noreferrer">
                Open source <ExternalLinkIcon data-icon="inline-end" />
              </a>
            </Button>
          )}
        </div>
      )}
      {children}
    </li>
  );
}

export function UnverifiedFindings({
  findings,
  review,
}: {
  findings?: ReviewFinding[];
  review: ReviewPass;
}) {
  if (!findings?.length) return null;
  return (
    <details className="space-y-2 rounded-md border border-border p-2">
      <summary className="text-sm">Unverified citations ({findings.length})</summary>
      <p className="text-xs text-muted-foreground">
        These findings cite lines outside the changes in the captured diff.
      </p>
      <ol className="space-y-2">
        {Array.from(uniqueReviewFindings(findings), ([findingKey, finding]) => (
          <FindingCard key={findingKey} finding={finding} review={review} current={false} />
        ))}
      </ol>
    </details>
  );
}

export function ReviewResult({
  review,
  active,
  viewerId,
  current,
  headRepoUrl,
  onOpenSource,
  standalone = false,
}: {
  review: ReviewPass;
  active: boolean;
  standalone?: boolean;
  viewerId: string | null;
  current: boolean;
  headRepoUrl?: string | null;
  onOpenSource?: (source: ReviewSource) => void;
}) {
  const launchAcknowledged =
    review.dispatch_status === "running" || review.dispatch_status === "succeeded";
  const { session: observedSession } = useSession(
    active && !standalone && !launchAcknowledged ? review.reviewer_session_id : null,
  );
  const sessionKnown = standalone || launchAcknowledged || !!observedSession;
  const report = useQuery({
    queryKey: ["studio-review-report", viewerId, review.id],
    queryFn: ({ signal }) => getReviewReport(review.id, signal),
    enabled: active && sessionKnown && !!review.reviewer_session_id,
    retry: false,
    refetchInterval: (query) => (active && query.state.data?.state === "pending" ? POLL_MS : false),
  });
  if (!review.reviewer_session_id || !sessionKnown) return null;
  const ready = report.data?.state === "ready" && !report.isError;
  const findings = uniqueReviewFindings(report.data?.findings);
  return (
    <section aria-label="Review findings" className={standalone ? "space-y-4" : "space-y-2"}>
      <div className="flex flex-wrap items-center justify-between gap-1">
        <h2 className="font-medium">{standalone ? "Report" : "Findings"}</h2>
        <div className="flex flex-wrap items-center gap-1">
          {!standalone && (
            <Button asChild variant="outline" size="sm">
              <Link to={`/c/${encodeURIComponent(review.reviewer_session_id)}?view=review`}>
                Open review report
              </Link>
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void report.refetch()}
            disabled={report.isFetching}
          >
            Refresh report
          </Button>
        </div>
      </div>
      {report.isError ? (
        <p role="alert" className="text-destructive">
          {report.error.message}
        </p>
      ) : ready ? (
        <>
          {report.data?.summary && (
            <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{report.data.summary}</p>
          )}
          {standalone && <h3 className="font-medium">Findings ({findings.size})</h3>}
          {report.data?.citation_validation === "unavailable" && (
            <p className="text-muted-foreground">This run has no saved diff for citation checks.</p>
          )}
          {findings.size ? (
            <ol className="space-y-2">
              {Array.from(findings, ([key, finding]) => (
                <FindingCard
                  key={key}
                  finding={finding}
                  review={review}
                  current={current}
                  headRepoUrl={headRepoUrl}
                  onOpenSource={onOpenSource}
                />
              ))}
            </ol>
          ) : (
            <p className="text-muted-foreground">
              {report.data?.unverified_findings?.length
                ? "None of the findings cite a changed line."
                : "The reviewer reported no findings."}
            </p>
          )}
          <UnverifiedFindings findings={report.data?.unverified_findings} review={review} />
        </>
      ) : report.data?.state === "incomplete" ? (
        <div className="space-y-2">
          <p role="status" className="text-muted-foreground">
            The reviewer has not produced a complete report. Ask the reviewer to{" "}
            {review.profile.skill ? "load the selected skill and " : ""}return a corrected report,
            then refresh here.
          </p>
          {!standalone && (
            <Button asChild variant="ghost" size="sm">
              <Link to={`/c/${encodeURIComponent(review.reviewer_session_id)}?view=chat`}>
                Open transcript
              </Link>
            </Button>
          )}
        </div>
      ) : (
        <p role="status" className="text-muted-foreground">
          Waiting for the reviewer’s report…
        </p>
      )}
    </section>
  );
}
