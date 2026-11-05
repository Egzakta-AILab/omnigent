import { useId, useState, type RefObject } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { nativeEffortOptions } from "@/components/HarnessConfigControls";
import { Button } from "@/components/ui/button";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { triggerBrowserDownload } from "@/hooks/useFileContent";
import { useHostModelOptions, useHosts } from "@/hooks/useHosts";
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
  type QuestionCitation,
  type QuestionAction,
  type ReviewDiscussion,
  type ReviewQuestion,
  type SharedReviewAnswer,
} from "./api";

export interface QuestionAttempt {
  intent: string;
  id: string;
}

function AnswerCitations({ citations }: { citations?: QuestionCitation[] | null }) {
  if (!citations?.length) {
    return (
      <p className="text-xs text-muted-foreground">
        {citations ? "No excerpts cited" : "Citation verification unavailable for this answer"}
      </p>
    );
  }
  return (
    <section aria-label="Matched diff excerpts" className="space-y-1">
      <p className="text-xs font-medium">Matched diff excerpts</p>
      <ul className="space-y-1">
        {citations.map(({ path, side, line, quote }, index) => (
          <li key={JSON.stringify([path, side, line, index])} className="rounded border p-2">
            <p className="break-all text-xs text-muted-foreground">
              {path}:{line} · {side === "base" ? "Base" : "Head"}
            </p>
            <code className="whitespace-pre-wrap break-all">{quote}</code>
          </li>
        ))}
      </ul>
    </section>
  );
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
  const isProposal = question.action === "propose_fix";
  const proposedPatch = question.proposed_patch;
  if (question.published_at || share.isSuccess) return null;
  const pending =
    !result.isError &&
    question.answer_state === "pending" &&
    Date.now() - question.updated_at < 120_000;
  return (
    <article className="space-y-2 rounded-md border p-2 text-sm">
      <p className="text-xs font-medium">
        {isProposal ? "Private fix proposal" : "Private"} ·{" "}
        {REVIEW_HARNESS_LABELS[question.harness]}
      </p>
      <p className="whitespace-pre-wrap break-words">{question.body}</p>
      {question.answer ? (
        <>
          <p className="whitespace-pre-wrap break-words rounded-md bg-muted/60 p-2">
            {question.answer}
          </p>
          <AnswerCitations citations={question.citations} />
          {proposedPatch && (
            <>
              <details className="rounded-md border p-2">
                <summary className="cursor-pointer">Preview proposed patch</summary>
                <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all text-xs">
                  {proposedPatch}
                </pre>
              </details>
              <p className="text-xs text-muted-foreground">
                Run an isolated fix check with this patch, then attach the receipt under
                Reproductions.
              </p>
            </>
          )}
        </>
      ) : result.isError ? (
        <p role="alert">{result.error.message}</p>
      ) : (
        <p className="text-muted-foreground" role="status">
          {question.status === "unknown"
            ? "Delivery unconfirmed. Check the private session."
            : pending
              ? isProposal
                ? "Waiting for a fix proposal…"
                : "Waiting for an answer…"
              : isProposal
                ? "No fix proposal confirmed. Open the private session for details."
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
            {isProposal ? "Check for proposal" : "Check for answer"}
          </Button>
        )}
        {question.answer && canShare && !isProposal && (
          <Button size="sm" onClick={() => share.mutate()} disabled={share.isPending}>
            Share answer
          </Button>
        )}
        {proposedPatch && (
          <Button
            size="sm"
            variant="outline"
            onClick={() =>
              triggerBrowserDownload(
                new Blob([proposedPatch], { type: "text/x-diff" }),
                `proposed-fix-${question.id.slice(0, 8)}.patch`,
              )
            }
          >
            Download patch
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
  action,
  attempt,
  refresh,
}: {
  discussion: ReviewDiscussion;
  anchor: DiscussionAnchor;
  action: QuestionAction;
  attempt: RefObject<QuestionAttempt | undefined>;
  refresh: () => Promise<void>;
}) {
  const [selection, setSelection] = useState("");
  const isProposal = action === "propose_fix";
  const [body, setBody] = useState(isProposal ? "Propose a focused patch for this finding." : "");
  const [reviewWorkspace, setReviewWorkspace] = useState(!!discussion.can_use_review_workspace);
  const [hostId, setHostId] = useState("");
  const [workspace, setWorkspace] = useState("");
  const modelInputId = useId();
  const effortInputId = useId();
  const [modelChoice, setModelChoice] = useState<{
    scope: string;
    model: string;
    effort: string;
  }>();
  const agents = useAvailableAgents();
  const hosts = useHosts({ enabled: !reviewWorkspace });
  const onlineHosts = (hosts.data ?? []).filter((host) => host.status === "online");
  const nativeAgents = (agents.data ?? []).flatMap((agent) => {
    const harness = nativeCodingAgentForAgentName(agent.name)?.harness;
    return agent.builtin && (harness === "codex-native" || harness === "claude-native")
      ? [{ ...agent, harness }]
      : [];
  });
  const nativeAgent = nativeAgents.find((agent) => agent.id === selection);
  const reviewerKey = selection.startsWith("reviewer:") ? selection.slice("reviewer:".length) : "";
  const modelHost = (reviewWorkspace ? discussion.review_workspace_host_id : hostId) || null;
  const modelScope = JSON.stringify([modelHost, nativeAgent?.id]);
  const modelCatalog = useHostModelOptions(modelHost, nativeAgent?.harness ?? "", !!nativeAgent, {
    poll: false,
  });
  const models = modelCatalog.data ?? [];
  const { model, effort } =
    modelChoice?.scope === modelScope ? modelChoice : { model: "", effort: "" };
  const validModel = !model || models.some((option) => option.id === model);
  const efforts = validModel
    ? (nativeEffortOptions(nativeAgent?.harness, models, model) ?? [])
    : [];
  const validEffort = !effort || efforts.some((option) => option.value === effort);
  const ready =
    (reviewerKey || nativeAgent) &&
    !!body.trim() &&
    validModel &&
    validEffort &&
    (reviewWorkspace || (!!hostId && !!workspace.trim()));
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
          ...(isProposal ? { action } : {}),
          ...(reviewerKey
            ? { reviewer_key: reviewerKey }
            : {
                agent_id: selection,
                ...(model ? { model } : {}),
                ...(effort ? { reasoning_effort: effort } : {}),
              }),
          ...(reviewWorkspace ? {} : { host_id: hostId, workspace: workspace.trim() }),
        };
        const intent = JSON.stringify(input);
        if (attempt.current?.intent !== intent) attempt.current = { intent, id: randomUUID() };
        ask.mutate({ ...input, id: attempt.current.id });
      }}
    >
      <p className="text-xs text-muted-foreground">
        {isProposal
          ? "The agent drafts a patch from the captured diff in a private session using its native permissions. Review the patch before running a fix check."
          : "The agent receives the captured diff in a private session on the selected host, using its native permissions. Read its answer before sharing it."}
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
          {nativeAgents.map((agent) => (
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
      {nativeAgent && modelHost && (
        <div className="grid gap-2 sm:grid-cols-2">
          <div className="grid gap-1">
            <label htmlFor={modelInputId}>Model</label>
            <select
              id={modelInputId}
              className="w-full rounded-md border bg-background p-1"
              value={model}
              disabled={ask.isPending}
              onChange={(event) =>
                setModelChoice({ scope: modelScope, model: event.target.value, effort: "" })
              }
            >
              <option value="">Agent default</option>
              {!validModel && (
                <option value={model} disabled>
                  {model} (unavailable)
                </option>
              )}
              {models.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.displayName || option.id}
                </option>
              ))}
            </select>
          </div>
          {(efforts.length > 0 || effort) && (
            <div className="grid gap-1">
              <label htmlFor={effortInputId}>Reasoning effort</label>
              <select
                id={effortInputId}
                className="w-full rounded-md border bg-background p-1"
                value={effort}
                disabled={ask.isPending}
                onChange={(event) =>
                  setModelChoice({ scope: modelScope, model, effort: event.target.value })
                }
              >
                <option value="">Default</option>
                {!validEffort && (
                  <option value={effort} disabled>
                    {effort} (unavailable)
                  </option>
                )}
                {efforts.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </div>
          )}
          {modelCatalog.isPending && <p role="status">Loading models…</p>}
          {modelCatalog.isError && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => void modelCatalog.refetch()}
            >
              Retry loading models
            </Button>
          )}
        </div>
      )}
      <textarea
        aria-label={isProposal ? "Fix instructions" : "Question for agent"}
        className="min-h-16 w-full rounded-md border bg-background p-2"
        placeholder={
          isProposal ? "What should the patch change?" : "What should the agent explain?"
        }
        maxLength={2000}
        value={body}
        disabled={ask.isPending}
        onChange={(event) => setBody(event.target.value)}
      />
      <Button type="submit" size="sm" disabled={!ready || ask.isPending}>
        {ask.isPending
          ? "Starting session…"
          : isProposal
            ? "Request private proposal"
            : "Ask privately"}
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
  const [open, setOpen] = useState<QuestionAction | null>(null);
  return (
    <div className="space-y-2">
      {answers.map((item) => (
        <article key={item.id} className="space-y-1 rounded-md border p-2 text-sm">
          <p className="text-xs font-medium">
            {REVIEW_HARNESS_LABELS[item.harness]} answer · Shared by {item.requested_by ?? "Local"}
          </p>
          <p className="whitespace-pre-wrap break-words text-muted-foreground">{item.question}</p>
          <p className="whitespace-pre-wrap break-words">{item.answer}</p>
          <AnswerCitations citations={item.citations} />
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
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="outline"
              aria-expanded={open === "question"}
              onClick={() => setOpen(open === "question" ? null : "question")}
            >
              Ask an agent
            </Button>
            {anchor.kind === "finding" && (
              <Button
                size="sm"
                variant="outline"
                aria-expanded={open === "propose_fix"}
                onClick={() => setOpen(open === "propose_fix" ? null : "propose_fix")}
              >
                Propose a fix
              </Button>
            )}
          </div>
          {open && (
            <AskForm
              key={open}
              action={open}
              discussion={discussion}
              anchor={anchor}
              attempt={attempt}
              refresh={refresh}
            />
          )}
        </>
      )}
    </div>
  );
}
