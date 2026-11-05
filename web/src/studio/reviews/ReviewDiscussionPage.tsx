import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useIdentityReady, useViewerId } from "@/hooks/useViewerId";
import { copyText } from "@/lib/clipboard";
import { randomUUID } from "@/lib/randomUUID";
import { Link, useParams, useSearchParams } from "@/lib/routing";
import {
  changeReviewCollaborator,
  getReviewDiscussion,
  postReviewDiscussionComment,
  type DiscussionAnchor,
  type ReviewDiscussion,
} from "./api";
import { FindingReproductions } from "./FindingReproductions";
import { InlineDrafts, type DraftAttempt } from "./InlineDrafts";
import { DiscussionQuestions, type QuestionAttempt } from "./DiscussionQuestions";
import { ReviewDiff } from "./ReviewDiff";

function anchorKey(anchor: DiscussionAnchor) {
  return JSON.stringify(
    anchor.kind === "finding"
      ? [anchor.kind, anchor.finding_key]
      : [anchor.kind, anchor.path, anchor.side, anchor.start, anchor.end],
  );
}

function rangeLabel(anchor: Extract<DiscussionAnchor, { kind: "diff" }>) {
  const lines = anchor.start === anchor.end ? anchor.start : `${anchor.start}–${anchor.end}`;
  return `${anchor.path}:${lines} · ${anchor.side === "base" ? "Base" : "Head"}`;
}

function Sharing({
  discussion,
  refresh,
}: {
  discussion: ReviewDiscussion;
  refresh: () => Promise<void>;
}) {
  const [account, setAccount] = useState("");
  const [status, setStatus] = useState("");
  const change = useMutation({
    mutationFn: ({ id, revoke }: { id: string; revoke: boolean }) =>
      changeReviewCollaborator(discussion.id, id, revoke),
    onSuccess: async (_, { revoke }) => {
      if (!revoke) setAccount("");
      setStatus(revoke ? "Access removed." : "Access granted. Share the review link.");
      await refresh();
    },
  });
  if (discussion.collaborators === null) return null;
  return (
    <details className="rounded-md border p-3">
      <summary className="cursor-pointer font-medium">Share this review</summary>
      <div className="mt-3 space-y-2 text-sm">
        <p className="text-muted-foreground">
          Collaborators can read this saved diff, findings, and reproduction receipts, and reply in
          Omnigent.
        </p>
        {discussion.sharing_available && (
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (account.trim() && !change.isPending)
                change.mutate({ id: account.trim(), revoke: false });
            }}
          >
            <label className="grid gap-1 text-xs">
              <span>Omnigent account ID</span>
              <input
                className="rounded-md border bg-background px-2 py-1 text-sm"
                value={account}
                maxLength={128}
                onChange={(event) => setAccount(event.target.value)}
              />
            </label>
            <Button type="submit" size="sm" disabled={!account.trim() || change.isPending}>
              Grant access
            </Button>
          </form>
        )}
        <ul className="divide-y divide-border">
          {discussion.collaborators.map((id) => (
            <li key={id} className="flex items-center justify-between gap-2 py-1">
              <span className="break-all">{id}</span>
              <Button
                variant="outline"
                size="sm"
                disabled={change.isPending}
                aria-label={`Revoke access for ${id}`}
                onClick={() => change.mutate({ id, revoke: true })}
              >
                Revoke access
              </Button>
            </li>
          ))}
        </ul>
        {change.error && <p role="alert">{change.error.message}</p>}
        {status && <p role="status">{status}</p>}
      </div>
    </details>
  );
}

function DiscussionContent({
  discussion,
  initialFinding,
  viewerId,
  refresh,
}: {
  discussion: ReviewDiscussion;
  initialFinding: string | null;
  viewerId: string | null;
  refresh: () => Promise<void>;
}) {
  const initial =
    discussion.findings.find(({ key }) => key === initialFinding) ?? discussion.findings[0];
  const [anchor, setAnchor] = useState<DiscussionAnchor | null>(
    initial ? { kind: "finding", finding_key: initial.key } : null,
  );
  const [body, setBody] = useState("");
  const [copyStatus, setCopyStatus] = useState("");
  const attempt = useRef<{ fingerprint: string; id: string } | undefined>(undefined);
  const questionAttempt = useRef<QuestionAttempt | undefined>(undefined);
  const draftAttempt = useRef<DraftAttempt | undefined>(undefined);
  const send = useMutation({
    mutationFn: (message: Parameters<typeof postReviewDiscussionComment>[1]) =>
      postReviewDiscussionComment(discussion.id, message),
    onSuccess: async () => {
      attempt.current = undefined;
      setBody("");
      await refresh();
    },
  });
  const selectedFinding =
    anchor?.kind === "finding"
      ? discussion.findings.find(({ key }) => key === anchor.finding_key)
      : undefined;
  const range = anchor?.kind === "diff" ? anchor : undefined;
  const selectedLine = range
    ? { path: range.path, side: range.side, line: range.start }
    : selectedFinding?.finding;
  const source = selectedLine
    ? {
        path: selectedLine.path,
        side: selectedLine.side,
        line: selectedLine.line,
        prUrl: discussion.pr_url,
        baseSha: discussion.base_sha,
        headSha: discussion.head_sha,
      }
    : null;
  const activeKey = anchor ? anchorKey(anchor) : "";
  const comments = discussion.comments.filter((item) => anchorKey(item.anchor) === activeKey);
  const ranges = new Map(
    [
      ...discussion.comments,
      ...(discussion.inline_drafts ?? []).map((draft) => ({ anchor: draft.thread })),
      ...(discussion.questions ?? []),
      ...(discussion.agent_answers ?? []),
    ].flatMap(({ anchor: threadAnchor }) =>
      threadAnchor.kind === "diff" ? [[anchorKey(threadAnchor), threadAnchor] as const] : [],
    ),
  );
  function select(next: DiscussionAnchor | null) {
    if (send.isPending) return;
    setAnchor(next);
    setBody("");
    send.reset();
  }

  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-6xl space-y-4 p-4 md:p-6">
        <header className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <p className="text-sm text-muted-foreground">{discussion.project_name}</p>
            <h1 className="text-xl font-semibold">
              {discussion.candidate ? "Candidate review" : "Review discussion"}
            </h1>
            <a
              className="text-sm underline"
              href={discussion.pr_url}
              target="_blank"
              rel="noreferrer"
            >
              {discussion.repository} #{discussion.pr_number}
            </a>
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              {discussion.base_sha.slice(0, 8)} → {discussion.head_sha.slice(0, 8)}
            </p>
            {discussion.candidate && (
              <Link
                className="text-sm underline"
                to={`/reviews/${encodeURIComponent(discussion.candidate.run_id)}/discussion?finding=${encodeURIComponent(discussion.candidate.finding_key)}`}
              >
                Original finding · {discussion.candidate.finding_title}
              </Link>
            )}
          </div>
          <div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                void copyText(window.location.href).then(
                  () => setCopyStatus("Review link copied."),
                  () => setCopyStatus("Couldn’t copy the review link."),
                );
              }}
            >
              Copy review link
            </Button>
            {copyStatus && (
              <p role="status" className="mt-1 text-xs">
                {copyStatus}
              </p>
            )}
          </div>
        </header>
        <Sharing discussion={discussion} refresh={refresh} />
        <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.8fr)]">
          <section className="min-w-0 space-y-3" aria-label="Review context">
            <details className="rounded-md border p-3">
              <summary className="cursor-pointer font-medium">Reviewer reports</summary>
              <ul className="mt-2 space-y-2 text-sm">
                {discussion.reports.map((report) => (
                  <li key={report.key} className="space-y-1">
                    <span className="font-medium">{report.label}</span>{" "}
                    <Badge variant="outline">{report.state}</Badge>
                    {report.summary && (
                      <p className="whitespace-pre-wrap break-words">{report.summary}</p>
                    )}
                  </li>
                ))}
              </ul>
            </details>
            <h2 className="font-medium">Captured diff</h2>
            {discussion.can_comment && (
              <p className="text-sm text-muted-foreground">
                Select a line number or drag across a range on one side to open a discussion.
              </p>
            )}
            <ReviewDiff
              patch={discussion.patch}
              truncated={false}
              source={source}
              discussionRange={range}
              onClearSource={() => select(null)}
              onSelectRange={discussion.can_comment ? select : undefined}
            />
          </section>
          <section className="min-w-0 space-y-3" aria-label="Internal discussion">
            <h2 className="font-medium">Findings and line threads</h2>
            <ul className="space-y-1">
              {discussion.findings.map(({ key, finding }) => (
                <li key={key}>
                  <button
                    type="button"
                    className="w-full break-words rounded-md border p-2 text-left text-sm aria-pressed:bg-muted"
                    aria-pressed={selectedFinding?.key === key}
                    onClick={() => select({ kind: "finding", finding_key: key })}
                  >
                    <Badge variant="outline">{finding.severity}</Badge> {finding.title}
                  </button>
                </li>
              ))}
              {[...ranges].map(([key, item]) => (
                <li key={key}>
                  <button
                    type="button"
                    className="w-full break-all rounded-md border p-2 text-left text-sm aria-pressed:bg-muted"
                    aria-pressed={activeKey === key}
                    onClick={() => select(item)}
                  >
                    {rangeLabel(item)}
                  </button>
                </li>
              ))}
            </ul>
            {anchor ? (
              <section className="space-y-3 rounded-md border p-3" aria-label="Selected thread">
                <h3 className="break-words font-medium">
                  {range ? rangeLabel(range) : selectedFinding?.finding.title}
                </h3>
                {selectedFinding && (
                  <>
                    <p className="whitespace-pre-wrap break-words text-sm">
                      {selectedFinding.finding.detail}
                    </p>
                    <FindingReproductions
                      key={selectedFinding.key}
                      run={discussion}
                      findingKey={selectedFinding.key}
                      viewerId={viewerId}
                      canAttach={discussion.can_attach_evidence}
                      canReReview={discussion.can_attach_evidence && !discussion.candidate}
                    />
                  </>
                )}
                {comments.length ? (
                  <ol className="divide-y divide-border">
                    {comments.map((comment) => (
                      <li key={comment.id} className="space-y-1 py-2 text-sm">
                        <p className="break-all text-xs text-muted-foreground">
                          {comment.author ?? "Local"} ·{" "}
                          {new Date(comment.created_at).toLocaleString()}
                        </p>
                        <p className="whitespace-pre-wrap break-words">{comment.body}</p>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p className="text-sm text-muted-foreground">No replies yet.</p>
                )}
                <InlineDrafts
                  key={`draft-${activeKey}`}
                  runId={discussion.id}
                  anchor={anchor}
                  drafts={(discussion.inline_drafts ?? []).filter(
                    (draft) => anchorKey(draft.thread) === activeKey,
                  )}
                  suggestedBody={selectedFinding?.finding.detail}
                  canEdit={discussion.can_draft_inline === true}
                  attempt={draftAttempt}
                  refresh={refresh}
                />
                <DiscussionQuestions
                  key={activeKey}
                  discussion={discussion}
                  anchor={anchor}
                  questions={(discussion.questions ?? []).filter(
                    (item) => anchorKey(item.anchor) === activeKey,
                  )}
                  answers={(discussion.agent_answers ?? []).filter(
                    (item) => anchorKey(item.anchor) === activeKey,
                  )}
                  viewerId={viewerId}
                  attempt={questionAttempt}
                  refresh={refresh}
                />
                {discussion.can_comment && (
                  <form
                    className="space-y-2"
                    onSubmit={(event) => {
                      event.preventDefault();
                      const text = body.trim();
                      if (!text || send.isPending) return;
                      const fingerprint = JSON.stringify([activeKey, text]);
                      if (attempt.current?.fingerprint !== fingerprint)
                        attempt.current = { fingerprint, id: randomUUID() };
                      send.mutate({ id: attempt.current.id, anchor, body: text });
                    }}
                  >
                    <textarea
                      className="min-h-20 w-full rounded-md border bg-background p-2 text-sm"
                      aria-label="Internal reply"
                      placeholder="Write an internal reply"
                      maxLength={2000}
                      value={body}
                      disabled={send.isPending}
                      onChange={(event) => setBody(event.target.value)}
                    />
                    <Button type="submit" size="sm" disabled={!body.trim() || send.isPending}>
                      {send.isPending ? "Posting…" : "Post internal reply"}
                    </Button>
                    {send.error && <p role="alert">{send.error.message}</p>}
                  </form>
                )}
              </section>
            ) : (
              <p className="text-sm text-muted-foreground">Choose a finding or a diff range.</p>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

export function ReviewDiscussionPage() {
  const { runId } = useParams();
  const [params] = useSearchParams();
  const queryClient = useQueryClient();
  const viewerId = useViewerId();
  const identityReady = useIdentityReady();
  const queryKey = ["studio-review-discussion", viewerId, runId] as const;
  const result = useQuery({
    queryKey,
    enabled: !!runId && identityReady,
    queryFn: ({ signal }) => getReviewDiscussion(runId!, signal),
    retry: false,
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: true,
    refetchInterval: (query) => (query.state.status === "error" ? false : 8000),
  });
  if (result.isError)
    return (
      <p className="p-4" role="alert">
        {result.error.message}
      </p>
    );
  if (!identityReady || !result.data || !result.isFetchedAfterMount)
    return (
      <p className="p-4" role="status">
        Loading review discussion…
      </p>
    );
  return (
    <DiscussionContent
      key={JSON.stringify([viewerId, runId, params.get("finding")])}
      discussion={result.data}
      initialFinding={params.get("finding")}
      viewerId={viewerId}
      refresh={() => queryClient.invalidateQueries({ queryKey })}
    />
  );
}
