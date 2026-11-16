import { ReadableMarkdown } from "@/components/ReadableMarkdown";
import { useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { EllipsisIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Link } from "@/lib/routing";
import { FindingReproductions } from "./FindingReproductions";
import {
  getCapturedReviewDiff,
  getReviewRun,
  getReviewRunReport,
  listProjectReviewRuns,
  requestReviewSynthesis,
  startReview,
  startReviewRun,
  type ReviewPass,
  type ReviewRun,
  type ReviewRunReport,
  type ReviewSource,
} from "./api";
import {
  FindingCard,
  reviewSettingsLabel,
  uniqueReviewFindings,
  UnverifiedFindings,
} from "./ReviewResult";

import { ReviewDiff } from "./ReviewDiff";
import { ReviewFollowUp } from "./ReviewFollowUp";

type Revision = Pick<ReviewSource, "prUrl" | "baseSha" | "headSha">;

export function reviewRunsKey(viewerId: string | null, projectId: string, prUrl: string) {
  return ["studio-review-runs", viewerId, projectId, prUrl] as const;
}

export function useProjectReviewRuns(
  viewerId: string | null,
  projectId: string,
  prUrl: string,
  enabled = true,
) {
  return useInfiniteQuery({
    queryKey: reviewRunsKey(viewerId, projectId, prUrl),
    queryFn: ({ pageParam, signal }) => listProjectReviewRuns(projectId, prUrl, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled,
    retry: false,
    staleTime: 10_000,
    refetchInterval: (query) =>
      query.state.data?.pages.some((page) =>
        page.entries.some(
          (run) =>
            run.coordinator?.dispatch_status === "dispatching" ||
            run.reviewers.some((member) => member.dispatch_status === "dispatching"),
        ),
      )
        ? 4_000
        : false,
  });
}

export function currentReviewRun(runs: ReviewRun[], revision: Revision) {
  return runs.find(
    (run) =>
      !run.target.candidate &&
      run.target.base_sha === revision.baseSha &&
      run.target.head_sha === revision.headSha,
  );
}

function isPendingRun(run: ReviewRun | undefined, report: ReviewRunReport | undefined) {
  const coordinator = run?.coordinator;
  if (coordinator && !run?.plan) {
    return (
      coordinator.dispatch_status === "dispatching" ||
      (["running", "succeeded"].includes(coordinator.dispatch_status) &&
        (!report?.proposal || report.proposal.state === "pending"))
    );
  }
  return run?.reviewers.some(
    (member) =>
      member.dispatch_status === "dispatching" ||
      (["running", "succeeded"].includes(member.dispatch_status) &&
        report?.reviewers.find((entry) => entry.review_id === member.id)?.report.state ===
          "pending"),
  );
}

function memberStatus(member: ReviewPass, report: ReviewRunReport | undefined) {
  const state = report?.reviewers.find((entry) => entry.review_id === member.id)?.report.state;
  if (state === "ready") return "Report ready";
  if (state === "incomplete") return "Report incomplete";
  if (member.dispatch_status === "not_requested")
    return member.launch_error ? "Blocked" : "Prepared";
  if (member.dispatch_status === "failed") return "Launch failed";
  if (member.dispatch_status === "unknown") return "Launch unconfirmed";
  if (member.dispatch_status === "cancelled") return "Cancelled";
  return "Waiting for report";
}

export function ReviewRunsPanel({
  projectId,
  revision,
  viewerId,
  selectedRunId,
  onSelectRun,
  onOpenSource,
  launchErrors,
  launchPending,
  fixedRun = false,
}: {
  projectId: string;
  revision: Revision;
  viewerId: string | null;
  selectedRunId: string;
  onSelectRun: (id: string) => void;
  onOpenSource?: (source: ReviewSource) => void;
  launchErrors?: { run: ReviewRun; errors: Record<string, string> };
  launchPending: boolean;
  fixedRun?: boolean;
}) {
  const queryClient = useQueryClient();
  const [selectedMember, setSelectedMember] = useState("");
  const [captured, setCaptured] = useState<{ runId: string; source: ReviewSource | null }>();
  const key = reviewRunsKey(viewerId, projectId, revision.prUrl);
  const runs = useProjectReviewRuns(viewerId, projectId, revision.prUrl, !fixedRun);
  const fixed = useQuery({
    queryKey: ["studio-review-run", viewerId, selectedRunId],
    queryFn: ({ signal }) => getReviewRun(selectedRunId, signal),
    enabled: fixedRun && !!selectedRunId,
    retry: false,
    refetchInterval: (query) =>
      query.state.data?.coordinator?.dispatch_status === "dispatching" ||
      query.state.data?.reviewers.some((entry) => entry.dispatch_status === "dispatching")
        ? 4_000
        : false,
  });
  const entries = (runs.data?.pages ?? []).flatMap((page) => page.entries);
  const run = fixedRun
    ? fixed.data
    : (entries.find((entry) => entry.id === selectedRunId) ??
      currentReviewRun(entries, revision) ??
      entries.find((entry) => !entry.target.candidate) ??
      entries[0]);
  const reloadRun = () => (fixedRun ? fixed.refetch() : runs.refetch());
  const capturedOpen = !!run?.patch_sha256 && captured?.runId === run.id && !runs.isRefetchError;
  const capturedSource = capturedOpen ? (captured?.source ?? null) : null;
  const savedDiff = useQuery({
    queryKey: ["studio-review-run-patch", viewerId, run?.id, run?.patch_sha256, capturedSource],
    queryFn: ({ signal }) => getCapturedReviewDiff(run!, capturedSource, signal),
    enabled: capturedOpen,
    retry: false,
    placeholderData: (old, query) =>
      query?.queryKey[1] === viewerId &&
      query?.queryKey[2] === run?.id &&
      query?.queryKey[3] === run?.patch_sha256
        ? old
        : undefined,
  });
  const report = useQuery({
    queryKey: ["studio-review-run-report", viewerId, run?.id],
    queryFn: ({ signal }) => getReviewRunReport(run!.id, signal),
    enabled: !!run && !runs.isRefetchError,
    retry: false,
    refetchInterval: (query) =>
      isPendingRun(run, query.state.data) ||
      query.state.data?.synthesis?.state === "pending" ||
      query.state.data?.follow_up?.specialist?.state === "pending" ||
      query.state.data?.follow_up?.coordinator?.state === "pending"
        ? 4_000
        : false,
  });
  const start = useMutation({
    mutationFn: async ({
      runId,
      memberId,
      proposalDigest,
    }: {
      runId: string;
      memberId?: string;
      proposalDigest?: string;
    }) => {
      if (memberId) {
        await startReview(memberId);
        return {};
      }
      return (await startReviewRun(runId, proposalDigest)).errors;
    },
    onSettled: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: key }),
        ...(fixedRun ? [fixed.refetch()] : []),
        report.refetch(),
      ]);
    },
  });

  const synthesisAction = useMutation({
    mutationFn: requestReviewSynthesis,
    onSettled: () => report.refetch(),
  });

  if (fixedRun && fixed.isPending) return <p role="status">Loading review run…</p>;
  if ((fixedRun && fixed.isError) || (runs.isError && !runs.isFetchNextPageError)) {
    return (
      <section className="space-y-2 border-t border-border pt-3">
        <h3 className="font-medium">Review runs</h3>
        <p role="alert" className="text-muted-foreground">
          Couldn’t load review runs.
        </p>
        <Button variant="outline" size="sm" onClick={() => void reloadRun()}>
          Retry
        </Button>
      </section>
    );
  }
  if (!run) return null;

  const current =
    run.target.base_sha === revision.baseSha && run.target.head_sha === revision.headSha;
  const ready =
    report.data?.reviewers.filter((entry) => entry.report.state === "ready").length ?? 0;
  const coordinator = run.coordinator;
  const awaitingPlan = !!coordinator && !run.plan;
  const proposal = !report.isError && awaitingPlan ? report.data?.proposal : undefined;
  const decisions = new Map(
    (run.plan ?? proposal?.plan)?.decisions.map((decision) => [decision.review_id, decision]),
  );
  const selected = run.reviewers.filter(
    (entry) => !coordinator || (!!run.plan && decisions.get(entry.id)?.selected),
  );
  const synthesis = report.data?.synthesis;
  const canSynthesize =
    !!run.plan &&
    ready > 0 &&
    selected.every(
      (entry) =>
        ["failed", "cancelled"].includes(entry.dispatch_status) ||
        report.data?.reviewers.some(
          (result) => result.review_id === entry.id && result.report.state !== "pending",
        ),
    );
  const planningStatus =
    proposal?.state === "ready"
      ? "Plan ready for approval"
      : coordinator?.dispatch_status === "not_requested"
        ? "Coordinator queued"
        : coordinator?.dispatch_status === "failed" ||
            coordinator?.dispatch_status === "cancelled" ||
            !!proposal?.error
          ? "Planning stopped"
          : "Coordinator planning";
  const member = run.reviewers.find((entry) => entry.id === selectedMember);
  const memberReport = report.data?.reviewers.find(
    (entry) => entry.review_id === member?.id,
  )?.report;
  const findingKeys = new Map(
    report.data?.findings.map(({ finding, finding_key }) => [JSON.stringify(finding), finding_key]),
  );
  const unverifiedCount =
    report.data?.reviewers.reduce(
      (total, entry) => total + (entry.report.unverified_findings?.length ?? 0),
      0,
    ) ?? 0;
  const openCapturedSource = run.patch_sha256
    ? (source: ReviewSource) => setCaptured({ runId: run.id, source })
    : undefined;
  const canStart = current && selected.some((entry) => entry.dispatch_status === "not_requested");
  const errors =
    start.variables?.runId === run.id
      ? start.data
      : launchErrors?.run.id === run.id
        ? launchErrors.errors
        : undefined;

  function reproductions(findingKey: string | null | undefined) {
    if (!findingKey || !run?.patch_sha256) return null;
    return (
      <>
        <Button asChild size="sm" variant="outline">
          <Link
            to={`/reviews/${encodeURIComponent(run.id)}/discussion?finding=${encodeURIComponent(findingKey)}`}
          >
            Discuss finding
          </Link>
        </Button>
        <FindingReproductions
          key={JSON.stringify([viewerId, run.id, findingKey])}
          run={run}
          findingKey={findingKey}
          viewerId={viewerId}
        />
      </>
    );
  }

  return (
    <section aria-label="Review runs" className="space-y-3 border-t border-border pt-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-medium">Review runs</h3>
        <div className="flex items-center gap-1">
          <Badge variant="secondary" role="status">
            {awaitingPlan ? planningStatus : `${ready} / ${selected.length} reports ready`}
          </Badge>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Review run actions"
                className="mobile-touch-target"
              >
                <EllipsisIcon />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => void Promise.all([reloadRun(), report.refetch()])}>
                Refresh run
              </DropdownMenuItem>
              {run.patch_sha256 && (
                <DropdownMenuItem
                  onSelect={() =>
                    setCaptured(capturedOpen ? undefined : { runId: run.id, source: null })
                  }
                >
                  {capturedOpen ? "Close captured diff" : "Open captured diff"}
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
      {!fixedRun && (
        <label className="block space-y-1 text-xs">
          <span>Review run</span>
          <select
            className="h-8 w-full rounded-md border bg-background px-2 text-sm"
            value={run.id}
            onChange={(event) => {
              onSelectRun(event.target.value);
              setSelectedMember("");
              setCaptured(undefined);
              start.reset();
            }}
          >
            {entries.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.target.head_sha.slice(0, 8)} · {entry.reviewers.length}{" "}
                {entry.coordinator ? "candidates" : "reviewers"} ·{" "}
                {entry.target.candidate && "Candidate snapshot · "}
                {new Date(entry.created_at).toLocaleString()}
              </option>
            ))}
          </select>
        </label>
      )}
      {runs.hasNextPage && (
        <Button
          size="sm"
          variant="outline"
          disabled={runs.isFetching}
          onClick={() => void runs.fetchNextPage()}
        >
          {runs.isFetchNextPageError ? "Retry loading earlier runs" : "Load earlier runs"}
        </Button>
      )}
      <p className="font-mono text-xs text-muted-foreground">
        {run.target.base_sha.slice(0, 8)} → {run.target.head_sha.slice(0, 8)}
        {run.target.candidate ? " · Candidate snapshot" : !current && " · Outdated"}
      </p>
      <details className="text-xs text-muted-foreground">
        <summary className="w-fit cursor-pointer">Run environment</summary>
        <p className="mt-1 wrap-break-word">
          {run.target.workspace} · Host {run.target.host_id}
        </p>
      </details>
      {coordinator && run.plan && (
        <section aria-label="Orchestrator synthesis" className="space-y-2 rounded-md border p-3">
          <h4 className="font-medium">Orchestrator synthesis</h4>
          {synthesis?.summary && (
            <ReadableMarkdown className="text-sm">{synthesis.summary}</ReadableMarkdown>
          )}
          {!!synthesis?.conflicts.length && (
            <ul className="space-y-2 text-sm">
              {synthesis.conflicts.map((conflict) => (
                <li key={JSON.stringify(conflict.finding_keys)} className="rounded-md border p-2">
                  <p className="font-medium">Reviewer disagreement</p>
                  <p>{conflict.detail}</p>
                  <ul className="mt-1 text-xs text-muted-foreground">
                    {conflict.finding_keys.map((findingKey) => {
                      const source = report.data?.findings.find(
                        (entry) => entry.finding_key === findingKey,
                      );
                      return source ? (
                        <li key={findingKey}>
                          {source.finding.path}:{source.finding.line} · {source.finding.title}
                        </li>
                      ) : null;
                    })}
                  </ul>
                </li>
              ))}
            </ul>
          )}
          {synthesis?.state === "pending" && (
            <p role="status">The Orchestrator is reading the specialist reports…</p>
          )}
          {synthesis?.state === "unknown" && (
            <p role="status">Delivery is unconfirmed. Check the Orchestrator transcript.</p>
          )}
          {synthesis?.state === "incomplete" && (
            <p role="status">
              The Orchestrator did not return a complete synthesis. The specialist reports are
              below.
            </p>
          )}
          {!synthesis && (
            <>
              <p className="text-sm text-muted-foreground">
                {canSynthesize
                  ? "Send the saved specialist reports to the Orchestrator."
                  : "Synthesis is available when the reviewers finish and at least one report is ready."}
              </p>
              <Button
                size="sm"
                className="mobile-touch-target"
                disabled={!canSynthesize || synthesisAction.isPending}
                onClick={() => synthesisAction.mutate(run.id)}
              >
                {synthesisAction.isPending
                  ? "Requesting synthesis…"
                  : "Consolidate with Orchestrator"}
              </Button>
            </>
          )}
          {synthesisAction.error && (
            <p role="alert" className="text-destructive">
              {synthesisAction.error.message}
            </p>
          )}
          {report.data && (
            <ReviewFollowUp
              key={run.id}
              run={run}
              report={report.data}
              current={current}
              refresh={report.refetch}
            />
          )}
        </section>
      )}
      {coordinator && (
        <section aria-label="Coordinator" className="space-y-2 rounded-md border p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h4 className="font-medium">Coordinator</h4>
              <p className="text-sm text-muted-foreground">{reviewSettingsLabel(coordinator)}</p>
            </div>
            {coordinator.reviewer_session_id && !fixedRun && (
              <Button asChild size="sm" variant="outline">
                <Link to={`/c/${encodeURIComponent(coordinator.reviewer_session_id)}?view=review`}>
                  Open Orchestrator
                </Link>
              </Button>
            )}
          </div>
          {run.plan ? (
            <p className="text-sm">Plan approved</p>
          ) : (
            <p className="text-sm" role="status">
              {proposal?.state === "ready"
                ? "Review the proposed roster below. Reviewer sessions start after approval."
                : proposal?.error ||
                  coordinator.launch_error ||
                  errors?.[coordinator.id] ||
                  (["dispatching", "running", "succeeded"].includes(coordinator.dispatch_status)
                    ? "Waiting for the coordinator’s plan…"
                    : memberStatus(coordinator, undefined))}
            </p>
          )}
          {current && awaitingPlan && coordinator.dispatch_status === "not_requested" && (
            <Button
              size="sm"
              variant="outline"
              disabled={start.isPending || launchPending}
              onClick={() => start.mutate({ runId: run.id })}
            >
              Start coordinator
            </Button>
          )}
        </section>
      )}
      {!awaitingPlan && selected.length > 0 && (
        <section aria-label="Specialist reports" className="space-y-3">
          <h4 className="font-medium">Specialist reports</h4>
          {selected.map((entry) => {
            const result = report.data?.reviewers.find(
              (item) => item.review_id === entry.id,
            )?.report;
            return (
              <article key={entry.id} className="space-y-2 rounded-md border p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h5 className="font-medium">{reviewSettingsLabel(entry)}</h5>
                  <Badge variant="outline">{memberStatus(entry, report.data)}</Badge>
                </div>
                {result?.summary && (
                  <ReadableMarkdown className="text-sm">{result.summary}</ReadableMarkdown>
                )}
                <div className="flex flex-wrap gap-2">
                  {entry.reviewer_session_id && (
                    <Button asChild size="sm" variant="ghost">
                      <Link to={`/c/${encodeURIComponent(entry.reviewer_session_id)}?view=chat`}>
                        Transcript
                      </Link>
                    </Button>
                  )}
                  {result &&
                    (result.findings.length > 0 || !!result.unverified_findings?.length) && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setSelectedMember(entry.id)}
                      >
                        View findings ({result.findings.length})
                      </Button>
                    )}
                </div>
              </article>
            );
          })}
        </section>
      )}
      <details open={awaitingPlan}>
        <summary className="cursor-pointer text-sm text-muted-foreground">
          Reviewer selection and reasons
        </summary>
        <ul className="divide-y divide-border">
          {run.reviewers.map((entry) => {
            const decision = decisions.get(entry.id);
            const hasReportView = !coordinator || (!!run.plan && decision?.selected);
            return (
              <li key={entry.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                {hasReportView ? (
                  <button
                    type="button"
                    className="min-w-0 text-left text-sm [overflow-wrap:anywhere] hover:underline"
                    aria-pressed={member?.id === entry.id}
                    onClick={() => setSelectedMember(entry.id)}
                  >
                    {reviewSettingsLabel(entry)}
                  </button>
                ) : (
                  <span className="min-w-0 text-sm [overflow-wrap:anywhere]">
                    {reviewSettingsLabel(entry)}
                  </span>
                )}
                <div className="flex gap-1">
                  {entry.run_role === "required" && <Badge variant="secondary">Required</Badge>}
                  <Badge variant="outline">
                    {hasReportView
                      ? memberStatus(entry, report.data)
                      : decision
                        ? decision.selected
                          ? "Selected"
                          : "Skipped"
                        : "Candidate"}
                  </Badge>
                </div>
                {decision && (
                  <p className="basis-full break-words text-xs text-muted-foreground">
                    {decision.reason}
                  </p>
                )}
                {decision?.assignment && (
                  <div className="basis-full space-y-1 break-words text-sm">
                    <p>
                      <span className="font-medium">Assignment</span>{" "}
                      {decision.assignment.objective}
                    </p>
                    {decision.assignment.acceptance_criterion && (
                      <p>
                        <span className="font-medium">Acceptance criterion</span>{" "}
                        {decision.assignment.acceptance_criterion}
                      </p>
                    )}
                    <p>
                      <span className="font-medium">Expected evidence</span>{" "}
                      {decision.assignment.expected_evidence}
                    </p>
                  </div>
                )}
                {(entry.launch_error || errors?.[entry.id]) && (
                  <p className="basis-full text-xs text-destructive">
                    {entry.launch_error || errors?.[entry.id]}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      </details>
      <div className="flex flex-wrap gap-2">
        {run.patch_sha256 && (
          <Button asChild size="sm" variant="outline" className="mobile-touch-target">
            <Link to={`/reviews/${encodeURIComponent(run.id)}/discussion`}>Discuss review</Link>
          </Button>
        )}
        {current && proposal?.digest && proposal.state === "ready" && (
          <Button
            size="sm"
            disabled={start.isPending || launchPending}
            onClick={() => start.mutate({ runId: run.id, proposalDigest: proposal.digest! })}
          >
            Approve plan and start reviewers
          </Button>
        )}
        {canStart && (
          <Button
            size="sm"
            disabled={start.isPending || launchPending}
            onClick={() => start.mutate({ runId: run.id })}
          >
            Start pending reviewers
          </Button>
        )}
      </div>
      {capturedOpen && (
        <section aria-label="Captured diff" className="space-y-2 rounded-md border p-2">
          <h4 className="font-medium">Captured diff</h4>
          <p className="break-all font-mono text-xs text-muted-foreground">
            SHA-256 {run.patch_sha256}
          </p>
          {savedDiff.isPending && <p role="status">Loading captured diff…</p>}
          {savedDiff.error && <p role="alert">{savedDiff.error.message}</p>}
          {savedDiff.data !== undefined && !savedDiff.isError && (
            <ReviewDiff
              key={run.id}
              patch={savedDiff.data.patch}
              truncated={false}
              source={capturedSource}
              sourceLine={savedDiff.isPlaceholderData ? null : savedDiff.data.source_line}
              onClearSource={() => setCaptured({ runId: run.id, source: null })}
            />
          )}
        </section>
      )}
      {start.error && (
        <p role="alert" className="text-destructive">
          {start.error.message}
        </p>
      )}
      {report.error && (
        <p role="alert" className="text-destructive">
          {report.error.message}
        </p>
      )}
      {report.isPending && (
        <p role="status" className="text-muted-foreground">
          Loading reports…
        </p>
      )}
      {report.data && !report.isError && !awaitingPlan && (
        <section className="space-y-2" aria-label={member ? "Reviewer report" : "Consolidation"}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h4 className="font-medium">
              {member ? reviewSettingsLabel(member) : "Consolidated findings"}
            </h4>
            {member && (
              <Button size="sm" variant="ghost" onClick={() => setSelectedMember("")}>
                Consolidated
              </Button>
            )}
          </div>
          {member ? (
            <>
              <div className="flex flex-wrap gap-2">
                {member.reviewer_session_id && (
                  <Button asChild size="sm" variant="outline">
                    <Link to={`/c/${encodeURIComponent(member.reviewer_session_id)}?view=review`}>
                      Open reviewer report
                    </Link>
                  </Button>
                )}
                {canStart &&
                  selected.includes(member) &&
                  member.dispatch_status === "not_requested" && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={start.isPending || launchPending}
                      onClick={() => start.mutate({ runId: run.id, memberId: member.id })}
                    >
                      Start this reviewer
                    </Button>
                  )}
              </div>
              <ol className="space-y-2">
                {Array.from(
                  uniqueReviewFindings(memberReport?.findings),
                  ([findingKey, finding]) => (
                    <FindingCard
                      key={findingKey}
                      finding={finding}
                      review={member}
                      current={current}
                      onOpenSource={run.patch_sha256 ? undefined : onOpenSource}
                      onOpenCapturedSource={openCapturedSource}
                    >
                      {reproductions(findingKeys.get(findingKey))}
                    </FindingCard>
                  ),
                )}
              </ol>
              <UnverifiedFindings findings={memberReport?.unverified_findings} review={member} />
            </>
          ) : (
            <>
              {ready < selected.length && (
                <p className="text-muted-foreground">
                  Partial coverage. Only available reports are included.
                </p>
              )}
              {ready > 0 && !run.patch_sha256 && (
                <p className="text-muted-foreground">
                  This run has no saved diff for citation checks.
                </p>
              )}
              {unverifiedCount > 0 && (
                <p className="text-muted-foreground">
                  {unverifiedCount} unverified {unverifiedCount === 1 ? "citation" : "citations"}.
                  Open the individual reports to inspect them.
                </p>
              )}
              {report.data.findings.length === 0 && ready > 0 && unverifiedCount === 0 && (
                <p className="text-muted-foreground">The available reports contain no findings.</p>
              )}
              <ol className="space-y-3">
                {report.data.findings.map(({ finding, review_ids, finding_key }) => {
                  const origin = run.reviewers.find((entry) => entry.id === review_ids[0]);
                  if (!origin) return null;
                  return (
                    <li key={JSON.stringify(finding)} className="space-y-1">
                      <ol>
                        <FindingCard
                          finding={finding}
                          review={origin}
                          current={current}
                          onOpenSource={run.patch_sha256 ? undefined : onOpenSource}
                          onOpenCapturedSource={openCapturedSource}
                        >
                          {reproductions(finding_key)}
                        </FindingCard>
                      </ol>
                      <div className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
                        <span>Reported by</span>
                        {run.reviewers
                          .filter((entry) => review_ids.includes(entry.id))
                          .map((entry) => (
                            <Button
                              key={entry.id}
                              size="sm"
                              variant="ghost"
                              onClick={() => setSelectedMember(entry.id)}
                            >
                              {reviewSettingsLabel(entry)}
                            </Button>
                          ))}
                      </div>
                    </li>
                  );
                })}
              </ol>
            </>
          )}
        </section>
      )}
    </section>
  );
}
