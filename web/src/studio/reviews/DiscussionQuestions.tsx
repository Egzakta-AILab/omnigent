import { useId, useState, type RefObject } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { nativeEffortOptions } from "@/components/HarnessConfigControls";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { triggerBrowserDownload } from "@/hooks/useFileContent";
import { useHostModelOptions, useHosts } from "@/hooks/useHosts";
import { reviewModelLabels } from "./reviewModelLabels";
import { ReviewModelRefresh } from "./ReviewModelRefresh";
import { nativeCodingAgentForAgentName } from "@/lib/nativeCodingAgents";
import { randomUUID } from "@/lib/randomUUID";
import { Link } from "@/lib/routing";
import { CheckProposal } from "./CheckProposal";
import {
  askReviewQuestion,
  findingReproductionsKey,
  getReviewQuestion,
  listFindingReproductions,
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
import type { ReviewProofTarget } from "./creatorReviewBridge";

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
  discussion,
  initial,
  viewerId,
  canShare,
  refresh,
}: {
  discussion: ReviewDiscussion;
  initial: ReviewQuestion;
  viewerId: string | null;
  canShare: boolean;
  refresh: () => Promise<void>;
}) {
  const runId = discussion.id;
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
  const isProposal = question.action === "propose_fix" || question.action === "propose_check";
  const proposalLabel = question.action === "propose_check" ? "check proposal" : "fix proposal";
  const proposedPatch = question.proposed_patch;
  if (question.published_at || share.isSuccess) return null;
  const pending =
    !result.isError &&
    question.answer_state === "pending" &&
    Date.now() - question.updated_at < 120_000;
  return (
    <article className="space-y-2 rounded-md border p-2 text-sm">
      <p className="text-xs font-medium">
        {isProposal ? `Private ${proposalLabel}` : "Private"} ·{" "}
        {REVIEW_HARNESS_LABELS[question.harness]}
      </p>
      <p className="whitespace-pre-wrap break-words">{question.body}</p>
      {question.proof_digest && (
        <p className="text-xs text-muted-foreground">
          Check receipt · {question.proof_digest.slice(0, 8)}
        </p>
      )}
      {question.answer ? (
        <>
          <p className="whitespace-pre-wrap break-words rounded-md bg-muted/60 p-2">
            {question.answer}
          </p>
          <AnswerCitations citations={question.citations} />
          {question.proposed_check &&
            question.anchor.kind === "finding" &&
            discussion.check_repository && (
              <CheckProposal
                check={question.proposed_check}
                proposalId={question.id}
                proposalDigest={question.proposed_check_digest}
                initialJob={question.check_job}
                viewerId={viewerId}
                findingKey={question.anchor.finding_key}
                discussion={discussion}
                repository={discussion.check_repository}
              />
            )}
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
                ? `Waiting for a ${proposalLabel}…`
                : "Waiting for an answer…"
              : isProposal
                ? `No ${proposalLabel} confirmed. Open the private session for details.`
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
  viewerId,
  action,
  draftKey,
  drafts,
  attempt,
  refresh,
  suggestedProof,
}: {
  discussion: ReviewDiscussion;
  anchor: DiscussionAnchor;
  viewerId: string | null;
  action: QuestionAction;
  draftKey: string;
  drafts: RefObject<Map<string, string>>;
  attempt: RefObject<QuestionAttempt | undefined>;
  refresh: () => Promise<void>;
  suggestedProof?: ReviewProofTarget;
}) {
  const [selection, setSelection] = useState("");
  const [proofSelection, setProofSelection] = useState<{
    suggestion: ReviewProofTarget | undefined;
    digest: string;
  }>();
  const findingKey = anchor.kind === "finding" ? anchor.finding_key : "";
  const proofs = useQuery({
    queryKey: findingReproductionsKey(viewerId, discussion.id, findingKey),
    queryFn: ({ signal }) => listFindingReproductions(discussion.id, findingKey, signal),
    enabled: !!findingKey,
    retry: false,
  });
  const requestedDigest =
    proofSelection && proofSelection.suggestion === suggestedProof
      ? proofSelection.digest
      : suggestedProof?.receiptDigest;
  const proofDigest =
    proofs.isSuccess && proofs.data.some(({ digest }) => digest === requestedDigest)
      ? requestedDigest
      : "";
  const isProposal = action !== "question";
  const [body, setBody] = useState(
    () =>
      drafts.current.get(draftKey) ??
      (action === "propose_check"
        ? "Propose a sandbox check that can reproduce this finding."
        : action === "propose_fix"
          ? "Propose a focused patch for this finding."
          : ""),
  );
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
  const { modelId, modelName, defaultModelLabel, defaultEffortLabel } = reviewModelLabels(
    models,
    model,
  );
  const validModel = !model || !!modelId;
  const efforts = validModel
    ? (nativeEffortOptions(nativeAgent?.harness, models, model) ?? [])
    : [];
  const validEffort = !effort || efforts.some((option) => option.value === effort);
  const ready =
    (reviewerKey || nativeAgent) &&
    !!body.trim() &&
    (!requestedDigest || !!proofDigest) &&
    validModel &&
    validEffort &&
    (reviewWorkspace || (!!hostId && !!workspace.trim()));
  const ask = useMutation({
    mutationFn: (input: AskReviewQuestion) => askReviewQuestion(discussion.id, input),
    onSuccess: async (_question, input) => {
      if (attempt.current?.id === input.id) attempt.current = undefined;
      if ((drafts.current.get(draftKey) ?? body).trim() === input.body) {
        drafts.current.set(draftKey, "");
        setBody("");
      }
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
          ...(proofDigest ? { proof_digest: proofDigest } : {}),
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
        {action === "propose_check"
          ? "The agent drafts a command from the captured diff in a private session. Review it before running the sandbox check."
          : isProposal
            ? "The agent drafts a patch from the captured diff in a private session using its native permissions. Review the patch before running a fix check."
            : "The agent receives the captured diff in a private session on the selected host, using its native permissions. Read its answer before sharing it."}
      </p>
      {(!!proofs.data?.length || !!requestedDigest) && (
        <label className="grid gap-1">
          <span>Include check receipt</span>
          <select
            className="w-full rounded-md border bg-background p-1"
            value={requestedDigest || ""}
            disabled={ask.isPending}
            onChange={(event) =>
              setProofSelection({ suggestion: suggestedProof, digest: event.target.value })
            }
          >
            <option value="">None</option>
            {requestedDigest && !proofDigest && (
              <option value={requestedDigest} disabled>
                {proofs.isPending ? "Loading receipt…" : "Receipt unavailable"}
              </option>
            )}
            {(proofs.isSuccess ? proofs.data : []).map(({ digest, receipt }) => (
              <option key={digest} value={digest}>
                {"base" in receipt ? "Reproduction" : "Fix comparison"} ·{" "}
                {new Date(receipt.observed_at).toLocaleString()} · {digest.slice(0, 8)}
              </option>
            ))}
          </select>
        </label>
      )}
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
              <option value="">{defaultModelLabel}</option>
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
            {modelId && modelId !== modelName && (
              <p className="break-all text-xs text-muted-foreground">{modelId}</p>
            )}
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
                <option value="">{defaultEffortLabel}</option>
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
          {modelHost && (
            <ReviewModelRefresh
              key={modelScope}
              hostId={modelHost}
              harness={nativeAgent.harness}
              loading={modelCatalog.isFetching}
              loadError={modelCatalog.isError}
            />
          )}
        </div>
      )}
      <textarea
        aria-label={
          action === "propose_check"
            ? "Check instructions"
            : isProposal
              ? "Fix instructions"
              : "Question for agent"
        }
        className="min-h-16 w-full rounded-md border bg-background p-2"
        placeholder={
          action === "propose_check"
            ? "What should the check prove?"
            : isProposal
              ? "What should the patch change?"
              : "What should the agent explain?"
        }
        maxLength={2000}
        value={body}
        disabled={ask.isPending}
        onChange={(event) => {
          drafts.current.set(draftKey, event.target.value);
          setBody(event.target.value);
        }}
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
  draftKey,
  drafts,
  questions,
  answers,
  viewerId,
  attempt,
  refresh,
  suggestedProof,
}: {
  discussion: ReviewDiscussion;
  anchor: DiscussionAnchor;
  draftKey: string;
  drafts: RefObject<Map<string, string>>;
  questions: ReviewQuestion[];
  answers: SharedReviewAnswer[];
  viewerId: string | null;
  attempt: RefObject<QuestionAttempt | undefined>;
  refresh: () => Promise<void>;
  suggestedProof?: ReviewProofTarget;
}) {
  const [composer, setComposer] = useState<{
    suggestion: ReviewProofTarget | undefined;
    action: QuestionAction | null;
  }>();
  const open =
    composer && composer.suggestion === suggestedProof
      ? composer.action
      : suggestedProof
        ? "question"
        : null;
  const setOpen = (action: QuestionAction | null) =>
    setComposer({ suggestion: suggestedProof, action });
  const privateSessions = new Map(
    questions
      .filter((question) => question.published_at)
      .map((question) => [question.id, question.session_id]),
  );
  return (
    <div className="space-y-2">
      {discussion.can_ask && (
        <>
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="outline"
              className="mobile-touch-target"
              aria-expanded={open === "question"}
              onClick={() => setOpen(open === "question" ? null : "question")}
            >
              Ask an agent
            </Button>
            {anchor.kind === "finding" && !discussion.candidate && (
              <Button
                size="sm"
                variant="outline"
                className="mobile-touch-target"
                aria-expanded={open === "propose_fix"}
                onClick={() => setOpen(open === "propose_fix" ? null : "propose_fix")}
              >
                Propose a fix
              </Button>
            )}
            {anchor.kind === "finding" && discussion.check_repository && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button size="sm" variant="ghost" className="mobile-touch-target">
                    More actions
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent>
                  <DropdownMenuItem
                    onSelect={() => setOpen(open === "propose_check" ? null : "propose_check")}
                  >
                    Draft sandbox check
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </div>
          {open && (
            <AskForm
              key={open}
              draftKey={JSON.stringify([draftKey, open])}
              drafts={drafts}
              action={open}
              discussion={discussion}
              anchor={anchor}
              viewerId={viewerId}
              attempt={attempt}
              refresh={refresh}
              suggestedProof={open === "propose_check" ? undefined : suggestedProof}
            />
          )}
        </>
      )}
      {answers.map((item) => {
        const sessionId = privateSessions.get(item.id);
        return (
          <article key={item.id} className="space-y-1 rounded-md border p-2 text-sm">
            <p className="text-xs font-medium">
              {REVIEW_HARNESS_LABELS[item.harness]} answer · Shared by{" "}
              {item.requested_by ?? "Local"}
            </p>
            <p className="whitespace-pre-wrap break-words text-muted-foreground">{item.question}</p>
            {item.proof_digest && (
              <p className="text-xs text-muted-foreground">
                Check receipt · {item.proof_digest.slice(0, 8)}
              </p>
            )}
            <p className="whitespace-pre-wrap break-words">{item.answer}</p>
            <AnswerCitations citations={item.citations} />
            {sessionId && (
              <Button asChild size="sm" variant="outline">
                <Link to={`/c/${encodeURIComponent(sessionId)}`}>Continue in private session</Link>
              </Button>
            )}
          </article>
        );
      })}
      {questions
        .filter((question) => !question.published_at)
        .map((question) => (
          <QuestionCard
            key={question.id}
            discussion={discussion}
            initial={question}
            viewerId={viewerId}
            canShare={discussion.can_comment}
            refresh={refresh}
          />
        ))}
    </div>
  );
}
