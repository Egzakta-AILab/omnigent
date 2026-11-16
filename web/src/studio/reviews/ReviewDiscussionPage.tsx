import { ReadableMarkdown } from "@/components/ReadableMarkdown";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useIdentityReady, useViewerId } from "@/hooks/useViewerId";
import { copyText } from "@/lib/clipboard";
import { randomUUID } from "@/lib/randomUUID";
import { Link, useParams, useSearchParams } from "@/lib/routing";
import { issueWorkspaceUrl } from "@/lib/studioIssue";
import {
  changeReviewCollaborator,
  getReviewDiscussion,
  postReviewDiscussionComment,
  type DiscussionAnchor,
  type ReviewDiscussion,
} from "./api";
import { FindingReproductions } from "./FindingReproductions";
import { InlineDrafts, type DraftAttempt } from "./InlineDrafts";
import { InlinePublication } from "./InlinePublication";
import { DiscussionQuestions, type QuestionAttempt } from "./DiscussionQuestions";
import { ReviewDiff } from "./ReviewDiff";
import { CreatorReviewPanels } from "./CreatorReviewPanels";
import { InstalledCreatorReviewPanel } from "./CreatorPanelPreview";
import type { ReviewProofTarget } from "./creatorReviewBridge";

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
    mutationFn: ({
      id,
      revoke,
      canPublish,
    }: {
      id: string;
      revoke: boolean;
      canPublish?: boolean;
    }) => changeReviewCollaborator(discussion.id, id, revoke, canPublish),
    onSuccess: async (_, { revoke, canPublish }) => {
      if (!revoke && canPublish === undefined) setAccount("");
      setStatus(
        revoke
          ? "Access removed."
          : canPublish === true
            ? "Publishing access granted."
            : canPublish === false
              ? "Publishing access removed."
              : "Access granted. Share the review link.",
      );
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
          Omnigent. Publishers can also confirm inline comments using the review workspace’s
          configured account.
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
          {discussion.collaborators.map((id) => {
            const publisher = discussion.publishers?.includes(id) ?? false;
            return (
              <li key={id} className="flex flex-wrap items-center justify-between gap-2 py-1">
                <span className="flex-1 break-all">
                  {id}
                  {publisher ? " · Publisher" : ""}
                </span>
                {discussion.sharing_available && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={change.isPending}
                    onClick={() => change.mutate({ id, revoke: false, canPublish: !publisher })}
                  >
                    {publisher ? "Remove publishing access" : "Allow publishing"}
                  </Button>
                )}
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
            );
          })}
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
  const [replies, setReplies] = useState(() => new Map<string, string>());
  const [copyStatus, setCopyStatus] = useState("");
  const [focusedProof, setFocusedProof] = useState<ReviewProofTarget>();
  const [suggestedProof, setSuggestedProof] = useState<ReviewProofTarget>();
  const replyAttempts = useRef(new Map<string, { text: string; id: string }>());
  const questionAttempt = useRef<QuestionAttempt | undefined>(undefined);
  const draftAttempt = useRef<DraftAttempt | undefined>(undefined);
  const isMobile = useIsMobileViewport();
  const [revealCitedLine, setRevealCitedLine] = useState(false);
  const diffHeading = useRef<HTMLHeadingElement>(null);
  const questionDrafts = useRef(new Map<string, string>());
  const threadRef = useRef<HTMLElement>(null);
  const publicationRef = useRef<HTMLElement>(null);
  const openedFromFinding = initialFinding !== null && initial?.key === initialFinding;
  useEffect(() => {
    if (openedFromFinding) threadRef.current?.focus();
  }, [openedFromFinding]);
  const send = useMutation({
    mutationFn: (message: Parameters<typeof postReviewDiscussionComment>[1]) =>
      postReviewDiscussionComment(discussion.id, message),
    onSuccess: async (_, message) => {
      replyAttempts.current.delete(anchorKey(message.anchor));
      setReplies((current) => {
        const next = new Map(current);
        next.delete(anchorKey(message.anchor));
        return next;
      });
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
  const body = replies.get(activeKey) ?? "";
  const comments = discussion.comments.filter((item) => anchorKey(item.anchor) === activeKey);
  const publicationDrafts = discussion.publication_drafts ?? discussion.inline_drafts ?? [];
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
    setFocusedProof(undefined);
    setSuggestedProof(undefined);
    setAnchor(next);
    setRevealCitedLine(false);
    send.reset();
  }

  function showPanelProof(target: ReviewProofTarget, attach: boolean, alsoOpen = false) {
    if (send.isPending) return false;
    if (target.findingKey !== selectedFinding?.key) {
      select({ kind: "finding", finding_key: target.findingKey });
    }
    if (attach) setSuggestedProof(target);
    if (!attach || alsoOpen) setFocusedProof(target);
    return true;
  }

  return (
    <div className="min-w-0 flex-1 overflow-y-auto max-md:scroll-pt-14">
      <div className="mx-auto max-w-6xl space-y-4 p-4 pt-12 md:p-6">
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
            <Button asChild size="sm" variant="ghost">
              <Link to={issueWorkspaceUrl(discussion.project_name, undefined, discussion.id)}>
                Link to issue
              </Link>
            </Button>
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
              className="mobile-touch-target"
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
        <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.8fr)]">
          <section
            className="min-w-0 space-y-3 lg:col-start-2 lg:row-start-1"
            aria-label="Internal discussion"
          >
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
              <section
                ref={threadRef}
                tabIndex={-1}
                className="space-y-3 rounded-md border p-3"
                aria-label="Selected thread"
              >
                <h3 className="break-words font-medium">
                  {range ? rangeLabel(range) : selectedFinding?.finding.title}
                </h3>
                {selectedFinding && (
                  <ReadableMarkdown className="text-sm">
                    {selectedFinding.finding.detail}
                  </ReadableMarkdown>
                )}
                {source && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="mobile-touch-target md:hidden"
                    onClick={() => {
                      diffHeading.current?.focus();
                      diffHeading.current?.scrollIntoView({ block: "start" });
                      setRevealCitedLine(true);
                    }}
                  >
                    View cited line
                  </Button>
                )}
                <DiscussionQuestions
                  key={activeKey}
                  draftKey={activeKey}
                  drafts={questionDrafts}
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
                  suggestedProof={suggestedProof}
                />
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
                  onReviewPublication={
                    discussion.can_publish_inline
                      ? () => publicationRef.current?.focus()
                      : undefined
                  }
                />
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
                {discussion.can_comment && (
                  <form
                    className="space-y-2"
                    onSubmit={(event) => {
                      event.preventDefault();
                      const text = body.trim();
                      if (!text || send.isPending) return;
                      const attempt = replyAttempts.current.get(activeKey);
                      const id = attempt?.text === text ? attempt.id : randomUUID();
                      replyAttempts.current.set(activeKey, { text, id });
                      send.mutate({ id, anchor, body: text });
                    }}
                  >
                    <textarea
                      className="min-h-20 w-full rounded-md border bg-background p-2 text-sm"
                      aria-label="Internal reply"
                      placeholder="Write an internal reply"
                      maxLength={2000}
                      value={body}
                      disabled={send.isPending}
                      onChange={(event) =>
                        setReplies(new Map(replies).set(activeKey, event.target.value))
                      }
                    />
                    <Button type="submit" size="sm" disabled={!body.trim() || send.isPending}>
                      {send.isPending ? "Posting…" : "Post internal reply"}
                    </Button>
                    {send.error && <p role="alert">{send.error.message}</p>}
                  </form>
                )}
                {selectedFinding && (
                  <FindingReproductions
                    key={selectedFinding.key}
                    run={discussion}
                    findingKey={selectedFinding.key}
                    viewerId={viewerId}
                    canAttach={discussion.can_attach_evidence}
                    canReReview={discussion.can_attach_evidence && !discussion.candidate}
                    focusProof={focusedProof}
                  />
                )}
              </section>
            ) : (
              <p className="text-sm text-muted-foreground">Choose a finding or a diff range.</p>
            )}
          </section>
          <section
            className="min-w-0 space-y-3 lg:col-start-1 lg:row-start-1"
            aria-label="Review context"
          >
            <details className="rounded-md border p-3">
              <summary className="cursor-pointer font-medium">Reviewer reports</summary>
              <ul className="mt-2 space-y-2 text-sm">
                {discussion.reports.map((report) => (
                  <li key={report.key} className="space-y-1">
                    <span className="font-medium">{report.label}</span>{" "}
                    <Badge variant="outline">{report.state}</Badge>
                    {report.summary && (
                      <ReadableMarkdown className="text-sm">{report.summary}</ReadableMarkdown>
                    )}
                  </li>
                ))}
              </ul>
            </details>
            <div className="sticky top-14 z-10 flex items-center justify-between gap-2 bg-background md:static">
              <h2 ref={diffHeading} tabIndex={-1} className="font-medium">
                Captured diff
              </h2>
              {anchor && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="mobile-touch-target md:hidden"
                  onClick={() => {
                    setRevealCitedLine(false);
                    threadRef.current?.focus();
                    threadRef.current?.scrollIntoView({ block: "start" });
                  }}
                >
                  Back to thread
                </Button>
              )}
            </div>
            {discussion.can_comment && (
              <p className="text-sm text-muted-foreground">
                Select a line number or drag across a range on one side to open a discussion.
              </p>
            )}
            <ReviewDiff
              patch={discussion.patch}
              truncated={false}
              source={source}
              revealCitedLine={!isMobile || revealCitedLine}
              discussionRange={range}
              onClearSource={() => select(null)}
              onSelectRange={discussion.can_comment ? select : undefined}
            />
          </section>
        </div>
        <Sharing discussion={discussion} refresh={refresh} />
        <CreatorReviewPanels runId={discussion.id} onProof={showPanelProof} />
        {discussion.creator_available && (
          <InstalledCreatorReviewPanel
            discussion={discussion}
            viewerId={viewerId}
            onProof={showPanelProof}
          />
        )}
        {discussion.can_publish_inline && publicationDrafts.length > 0 && (
          <InlinePublication
            sectionRef={publicationRef}
            runId={discussion.id}
            drafts={publicationDrafts}
            refresh={refresh}
          />
        )}
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
