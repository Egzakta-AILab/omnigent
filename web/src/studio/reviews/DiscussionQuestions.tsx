import { useState, type RefObject } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import { nativeCodingAgentForAgentName } from "@/lib/nativeCodingAgents";
import { randomUUID } from "@/lib/randomUUID";
import { Link } from "@/lib/routing";
import {
  askReviewQuestion,
  getReviewQuestion,
  REVIEW_HARNESS_LABELS,
  shareReviewAnswer,
  type AskReviewQuestion,
  type DiscussionAnchor,
  type ReviewDiscussion,
  type ReviewQuestion,
  type SharedReviewAnswer,
} from "./api";

export interface QuestionAttempt {
  intent: string;
  id: string;
}

function QuestionCard({
  runId,
  initial,
  viewerId,
  canShare,
  refresh,
}: {
  runId: string;
  initial: ReviewQuestion;
  viewerId: string | null;
  canShare: boolean;
  refresh: () => Promise<void>;
}) {
  const result = useQuery({
    queryKey: ["studio-review-question", viewerId, runId, initial.id],
    queryFn: ({ signal }) => getReviewQuestion(runId, initial.id, signal),
    enabled: !initial.answer,
    retry: false,
    staleTime: 0,
    gcTime: 0,
    refetchInterval: (query) => {
      const value = query.state.data;
      return query.state.status !== "error" &&
        value?.answer_state === "pending" &&
        Date.now() - value.updated_at < 120_000
        ? 3000
        : false;
    },
  });
  const share = useMutation({
    mutationFn: () => shareReviewAnswer(runId, initial.id),
    onSuccess: async () => refresh(),
  });
  const question = initial.answer ? initial : (result.data ?? initial);
  if (question.published_at || share.isSuccess) return null;
  const pending =
    !result.isError &&
    question.answer_state === "pending" &&
    Date.now() - question.updated_at < 120_000;
  return (
    <article className="space-y-2 rounded-md border p-2 text-sm">
      <p className="text-xs font-medium">Private · {REVIEW_HARNESS_LABELS[question.harness]}</p>
      <p className="whitespace-pre-wrap break-words">{question.body}</p>
      {question.answer ? (
        <p className="whitespace-pre-wrap break-words rounded-md bg-muted/60 p-2">
          {question.answer}
        </p>
      ) : result.isError ? (
        <p role="alert">{result.error.message}</p>
      ) : (
        <p className="text-muted-foreground" role="status">
          {question.status === "unknown"
            ? "Delivery unconfirmed. Check the private session."
            : pending
              ? "Waiting for an answer…"
              : "No answer confirmed for the saved question."}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <Button asChild size="sm" variant="outline">
          <Link to={`/c/${encodeURIComponent(question.session_id)}`}>Open private session</Link>
        </Button>
        {!question.answer && !pending && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => void result.refetch()}
            disabled={result.isFetching}
          >
            Check for answer
          </Button>
        )}
        {question.answer && canShare && (
          <Button size="sm" onClick={() => share.mutate()} disabled={share.isPending}>
            Share answer
          </Button>
        )}
      </div>
      {share.error && <p role="alert">{share.error.message}</p>}
    </article>
  );
}

function AskForm({
  discussion,
  anchor,
  attempt,
  refresh,
}: {
  discussion: ReviewDiscussion;
  anchor: DiscussionAnchor;
  attempt: RefObject<QuestionAttempt | undefined>;
  refresh: () => Promise<void>;
}) {
  const [selection, setSelection] = useState("");
  const [body, setBody] = useState("");
  const [reviewWorkspace, setReviewWorkspace] = useState(!!discussion.can_use_review_workspace);
  const [hostId, setHostId] = useState("");
  const [workspace, setWorkspace] = useState("");
  const agents = useAvailableAgents();
  const hosts = useHosts({ enabled: !reviewWorkspace });
  const onlineHosts = (hosts.data ?? []).filter((host) => host.status === "online");
  const ready =
    !!selection && !!body.trim() && (reviewWorkspace || (!!hostId && !!workspace.trim()));
  const ask = useMutation({
    mutationFn: (input: AskReviewQuestion) => askReviewQuestion(discussion.id, input),
    onSuccess: async (_question, input) => {
      if (attempt.current?.id === input.id) attempt.current = undefined;
      setBody("");
      await refresh();
    },
  });
  return (
    <form
      className="space-y-2 rounded-md border p-2 text-sm"
      onSubmit={(event) => {
        event.preventDefault();
        if (!ready || ask.isPending) return;
        const input = {
          anchor,
          body: body.trim(),
          ...(selection.startsWith("reviewer:")
            ? { reviewer_key: selection.slice("reviewer:".length) }
            : { agent_id: selection }),
          ...(reviewWorkspace ? {} : { host_id: hostId, workspace: workspace.trim() }),
        };
        const intent = JSON.stringify(input);
        if (attempt.current?.intent !== intent) attempt.current = { intent, id: randomUUID() };
        ask.mutate({ ...input, id: attempt.current.id });
      }}
    >
      <p className="text-xs text-muted-foreground">
        The agent receives the captured diff in a private session on the selected host, using its
        native permissions. Read its answer before sharing it.
      </p>
      <label className="grid gap-1">
        <span>Answer with</span>
        <select
          className="w-full rounded-md border bg-background p-1"
          value={selection}
          onChange={(event) => setSelection(event.target.value)}
        >
          <option value="">Choose agent</option>
          {discussion.reports.map((report) => (
            <option key={report.key} value={`reviewer:${report.key}`}>
              Saved reviewer settings · {report.label}
            </option>
          ))}
          {(agents.data ?? [])
            .filter((agent) => {
              const native = nativeCodingAgentForAgentName(agent.name);
              return native?.harness === "codex-native" || native?.harness === "claude-native";
            })
            .map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agent.display_name ?? agent.name}
              </option>
            ))}
        </select>
      </label>
      {discussion.can_use_review_workspace && (
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={reviewWorkspace}
            onChange={(event) => setReviewWorkspace(event.target.checked)}
          />
          Use the saved review workspace
        </label>
      )}
      {!reviewWorkspace && (
        <div className="space-y-2">
          <label className="grid gap-1">
            <span>My connected host</span>
            <select
              className="w-full rounded-md border bg-background p-1"
              value={hostId}
              onChange={(event) => setHostId(event.target.value)}
            >
              <option value="">Choose host</option>
              {onlineHosts.map((host) => (
                <option key={host.host_id} value={host.host_id}>
                  {host.name}
                </option>
              ))}
            </select>
          </label>
          <label className="grid gap-1">
            <span>Working directory</span>
            <input
              className="rounded-md border bg-background p-1"
              placeholder="/path/to/checkout"
              value={workspace}
              maxLength={4096}
              onChange={(event) => setWorkspace(event.target.value)}
            />
          </label>
        </div>
      )}
      <textarea
        aria-label="Question for agent"
        className="min-h-16 w-full rounded-md border bg-background p-2"
        placeholder="What should the agent explain?"
        maxLength={2000}
        value={body}
        disabled={ask.isPending}
        onChange={(event) => setBody(event.target.value)}
      />
      <Button type="submit" size="sm" disabled={!ready || ask.isPending}>
        {ask.isPending ? "Starting session…" : "Ask privately"}
      </Button>
      {ask.error && <p role="alert">{ask.error.message}</p>}
    </form>
  );
}

export function DiscussionQuestions({
  discussion,
  anchor,
  questions,
  answers,
  viewerId,
  attempt,
  refresh,
}: {
  discussion: ReviewDiscussion;
  anchor: DiscussionAnchor;
  questions: ReviewQuestion[];
  answers: SharedReviewAnswer[];
  viewerId: string | null;
  attempt: RefObject<QuestionAttempt | undefined>;
  refresh: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="space-y-2">
      {answers.map((item) => (
        <article key={item.id} className="space-y-1 rounded-md border p-2 text-sm">
          <p className="text-xs font-medium">
            {REVIEW_HARNESS_LABELS[item.harness]} answer · Shared by {item.requested_by ?? "Local"}
          </p>
          <p className="whitespace-pre-wrap break-words text-muted-foreground">{item.question}</p>
          <p className="whitespace-pre-wrap break-words">{item.answer}</p>
        </article>
      ))}
      {questions
        .filter((question) => !question.published_at)
        .map((question) => (
          <QuestionCard
            key={question.id}
            runId={discussion.id}
            initial={question}
            viewerId={viewerId}
            canShare={discussion.can_comment}
            refresh={refresh}
          />
        ))}
      {discussion.can_ask && (
        <>
          <Button size="sm" variant="outline" aria-expanded={open} onClick={() => setOpen(!open)}>
            Ask an agent
          </Button>
          {open && (
            <AskForm discussion={discussion} anchor={anchor} attempt={attempt} refresh={refresh} />
          )}
        </>
      )}
    </div>
  );
}
