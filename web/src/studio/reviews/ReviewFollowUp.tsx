import { useMutation } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Link } from "@/lib/routing";
import {
  advanceReviewFollowUp,
  type FollowUpPhase,
  type FollowUpTurn,
  type ReviewRun,
  type ReviewRunReport,
} from "./api";
import { reviewSettingsLabel } from "./ReviewResult";

function Turn({
  label,
  turn,
  sessionId,
}: {
  label: string;
  turn: FollowUpTurn;
  sessionId?: string | null;
}) {
  return (
    <div className="space-y-1 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-medium">
          {label}
          {turn.result === "blocked" && " · Blocked"}
        </p>
        {sessionId && (
          <Link className="underline" to={`/c/${encodeURIComponent(sessionId)}?view=chat`}>
            Transcript
          </Link>
        )}
      </div>
      {turn.detail && <p className="whitespace-pre-wrap break-words">{turn.detail}</p>}
      {turn.state === "pending" && <p role="status">Waiting for a reply…</p>}
      {turn.state === "unknown" && (
        <p role="status">Delivery is unconfirmed. Check the transcript.</p>
      )}
      {turn.state === "incomplete" && (
        <p role="status">The transcript has no complete reply for this question.</p>
      )}
    </div>
  );
}

export function ReviewFollowUp({
  run,
  report,
  current,
  refresh,
}: {
  run: ReviewRun;
  report: ReviewRunReport;
  current: boolean;
  refresh: () => Promise<unknown>;
}) {
  const advance = useMutation({
    mutationFn: (phase: FollowUpPhase) => advanceReviewFollowUp(run.id, phase),
    onSettled: refresh,
  });
  const proposal = report.synthesis?.follow_up;
  if (!proposal) return null;
  const member = run.reviewers.find(({ id }) => id === proposal.review_id);
  const finding = report.findings.find((entry) => entry.finding_key === proposal.finding_key);
  const specialist = report.follow_up?.specialist;
  const coordinator = report.follow_up?.coordinator;
  const phase = !specialist
    ? "ask"
    : specialist.state === "ready" && !coordinator
      ? "return"
      : null;
  return (
    <section aria-label="Orchestrator follow-up" className="space-y-3 rounded-md border p-3">
      <h5 className="font-medium">Orchestrator follow-up</h5>
      {member && <p className="text-xs text-muted-foreground">To {reviewSettingsLabel(member)}</p>}
      <p className="whitespace-pre-wrap break-words text-sm">{proposal.question}</p>
      {finding && (
        <p className="break-words text-xs text-muted-foreground">
          {finding.finding.path}:{finding.finding.line} · {finding.finding.title}
        </p>
      )}
      {specialist && (
        <Turn label="Specialist reply" turn={specialist} sessionId={member?.reviewer_session_id} />
      )}
      {coordinator && (
        <Turn
          label="Orchestrator assessment"
          turn={coordinator}
          sessionId={run.coordinator?.reviewer_session_id}
        />
      )}
      {phase && (
        <>
          <Button
            size="sm"
            variant="outline"
            disabled={!current || advance.isPending}
            onClick={() => advance.mutate(phase)}
          >
            {phase === "ask" ? "Send question to specialist" : "Return reply to Orchestrator"}
          </Button>
          {!current && (
            <p className="text-xs text-muted-foreground">This review is for an earlier revision.</p>
          )}
        </>
      )}
      {advance.error && (
        <p role="alert" className="text-sm text-destructive">
          {advance.error.message}
        </p>
      )}
    </section>
  );
}
