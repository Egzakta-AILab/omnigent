import { useRef, useState } from "react";
import {
  type InfiniteData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { ExternalLinkIcon, Loader2Icon } from "lucide-react";
import {
  EFFORT_SELECT_NONE,
  MODEL_SELECT_DEFAULT,
  nativeEffortOptions,
} from "@/components/HarnessConfigControls";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHostModelOptions } from "@/hooks/useHosts";
import { sessionQueryOptions, useSession } from "@/hooks/useSession";
import { useSkills } from "@/hooks/useSkills";
import { useIdentityReady, useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { nativeCodingAgentForAgentName } from "@/lib/nativeCodingAgents";
import { canonicalNativeHarness } from "@/lib/nativeHarnessModes";
import { randomUUID } from "@/lib/randomUUID";
import { Link } from "@/lib/routing";
import { pullRequestFileUrl } from "@/shell/pullRequestReview";
import {
  captureReview,
  getReview,
  getReviewReport,
  listReviewerProfiles,
  listReviews,
  matchesReviewRevision,
  REVIEW_HARNESS_LABELS,
  startReview,
  type CaptureReview,
  type ReviewFinding,
  type ReviewHarness,
  type ReviewPage,
  type ReviewPass,
  type ReviewerProfile,
  type ReviewSource,
} from "./api";
import { ProfileEditorDialog } from "./ProfileEditorDialog";

const POLL_MS = 3_000;
const START_CONFIRMATION_MS = 120_000;
const FULL_SHA = /^[0-9a-f]{40}(?:[0-9a-f]{24})?$/;
const SKILL_NAME = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const CUSTOM_SKILL = "__custom_skill__";

export function useStudioReviewHistory(conversationId: string, enabled: boolean) {
  const identityReady = useIdentityReady();
  const viewerId = useViewerId();
  const key = ["studio-reviews", viewerId, conversationId] as const;
  const query = useInfiniteQuery({
    queryKey: key,
    queryFn: ({ pageParam, signal }) => listReviews(conversationId, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: enabled && identityReady,
    staleTime: 10_000,
    retry: false,
  });
  const authorized =
    !!query.data?.pages.length && query.data.pages.every((page) => !page.accessDenied);
  return { query, key, viewerId, authorized };
}

type CurrentRevision = Pick<ReviewSource, "prUrl" | "baseSha" | "headSha">;

type ReviewAction =
  | { kind: "start"; review: ReviewPass }
  | { kind: "capture"; input: CaptureReview; key: string; hostId: string; workspace: string };

const DISPATCH_LABELS: Record<ReviewPass["dispatch_status"], string> = {
  not_requested: "Prepared",
  dispatching: "Starting",
  running: "Review requested",
  succeeded: "Review requested",
  failed: "Start failed",
  cancelled: "Cancelled",
  unknown: "Start unconfirmed",
};

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

function reviewSettingsLabel(review: ReviewPass): string {
  const { role, skill, harness, model, reasoning_effort } = review.profile;
  return [role?.label, skill, REVIEW_HARNESS_LABELS[harness], model, reasoning_effort]
    .filter(Boolean)
    .join(" · ");
}

function ReviewSelect({
  label,
  value,
  onChange,
  options,
  disabled,
  placeholder,
  description,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string; title?: string | null }[];
  disabled: boolean;
  placeholder?: string;
  description?: string | null;
}) {
  return (
    <div className="min-w-0 space-y-1">
      <p className="text-xs text-muted-foreground">{label}</p>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger aria-label={label} disabled={disabled} className="w-full">
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value} title={option.title ?? undefined}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {description && (
        <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{description}</p>
      )}
    </div>
  );
}

function FindingCard({
  finding,
  review,
  current,
  headRepoUrl,
  onOpenSource,
}: {
  finding: ReviewFinding;
  review: ReviewPass;
  current: boolean;
  headRepoUrl?: string | null;
  onOpenSource: (source: ReviewSource) => void;
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
      <p className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">
        {finding.path}:{finding.line} · {finding.side === "base" ? "Base" : "Head"}
      </p>
      <p className="text-muted-foreground whitespace-pre-wrap [overflow-wrap:anywhere]">
        {finding.detail}
      </p>
      {(current || blobUrl) && (
        <div className="flex flex-wrap items-center gap-1">
          {current && (
            <Button variant="outline" size="sm" onClick={() => onOpenSource(source)}>
              Show in diff
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
    </li>
  );
}

function ReviewResult({
  review,
  active,
  viewerId,
  current,
  headRepoUrl,
  onOpenSource,
}: {
  review: ReviewPass;
  active: boolean;
  viewerId: string | null;
  current: boolean;
  headRepoUrl?: string | null;
  onOpenSource: (source: ReviewSource) => void;
}) {
  const launchAcknowledged =
    review.dispatch_status === "running" || review.dispatch_status === "succeeded";
  const { session: observedSession } = useSession(
    active && !launchAcknowledged ? review.reviewer_session_id : null,
  );
  const sessionKnown = launchAcknowledged || !!observedSession;
  const report = useQuery({
    queryKey: ["studio-review-report", viewerId, review.id],
    queryFn: ({ signal }) => getReviewReport(review.id, signal),
    enabled: active && sessionKnown && !!review.reviewer_session_id,
    retry: false,
    refetchInterval: (query) =>
      active && launchAcknowledged && query.state.data?.state === "pending" ? POLL_MS : false,
  });
  if (!review.reviewer_session_id || !sessionKnown) return null;
  const ready = report.data?.state === "ready" && !report.isError;
  const findings = new Map(
    report.data?.findings.map((finding) => [JSON.stringify(finding), finding]),
  );
  return (
    <section aria-label="Review findings" className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-1">
        <h3 className="font-medium">Findings</h3>
        <div className="flex flex-wrap items-center gap-1">
          <Button asChild variant="outline" size="sm">
            <Link to={`/c/${review.reviewer_session_id}`}>Open reviewer session</Link>
          </Button>
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
            <p className="text-muted-foreground">The reviewer reported no findings.</p>
          )}
        </>
      ) : report.data?.state === "incomplete" ? (
        <p role="status" className="text-muted-foreground">
          The reviewer has not produced a complete report. Open the reviewer session and ask the
          agent to {review.profile.skill ? "load the selected skill and " : ""}return a corrected
          report, then refresh here.
        </p>
      ) : (
        <p role="status" className="text-muted-foreground">
          Waiting for the reviewer’s report…
        </p>
      )}
    </section>
  );
}

export function ReviewPanel({
  conversationId,
  active,
  revision,
  headRepoUrl,
  history,
  onOpenSource,
}: {
  conversationId: string;
  active: boolean;
  revision: CurrentRevision;
  headRepoUrl?: string | null;
  history: ReturnType<typeof useStudioReviewHistory>;
  onOpenSource: (source: ReviewSource) => void;
}) {
  const queryClient = useQueryClient();
  const serverInfo = useServerInfo();
  const [editingProfiles, setEditingProfiles] = useState(false);
  const [agentId, setAgentId] = useState("");
  const [skill, setSkill] = useState("");
  const [profileChoice, setProfileChoice] = useState<{
    scope: string;
    profile: ReviewerProfile;
  } | null>(null);
  const [modelChoice, setModelChoice] = useState<{
    scope: string;
    model: string;
    effort: string;
  } | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const captureAttempt = useRef<{ intent: string; key: string; captured?: ReviewPass } | null>(
    null,
  );
  const launchInFlight = useRef(false);
  const { session } = useSession(active ? conversationId : null);
  const hostId = session?.hostId;
  const workspace = session?.workspace;
  const canEditProfiles =
    active &&
    history.authorized &&
    !!hostId &&
    !!workspace &&
    serverInfo !== "loading" &&
    serverInfo.studio_workspace_settings;
  const agents = useAvailableAgents({ enabled: active });
  const reviewers = (agents.data ?? []).flatMap((agent) => {
    const harness = canonicalNativeHarness(agent.harness ?? "");
    const wrapper = nativeCodingAgentForAgentName(agent.name);
    return agent.builtin === true &&
      wrapper?.harness === harness &&
      (harness === "codex-native" || harness === "claude-native")
      ? [{ ...agent, harness: harness as ReviewHarness }]
      : [];
  });
  const reviewer = reviewers.find((agent) => agent.id === agentId);
  const profileScope = JSON.stringify([history.viewerId, conversationId, hostId, workspace]);
  const profileCatalog = useQuery({
    queryKey: ["studio-review-profiles", history.viewerId, conversationId, hostId, workspace],
    queryFn: ({ signal }) => listReviewerProfiles(conversationId, signal),
    enabled: active && history.authorized && !!hostId && !!workspace,
    staleTime: 15_000,
    retry: false,
  });
  const profiles = profileCatalog.isSuccess ? profileCatalog.data : [];
  const selectedProfile = profileChoice?.scope === profileScope ? profileChoice.profile : null;
  const role = profiles.find(
    (profile) => profile.id === selectedProfile?.id && profile.digest === selectedProfile.digest,
  );
  const useCustomSkill = selectedProfile === null;
  function modelScopeFor(reviewerId: string | undefined) {
    return JSON.stringify([history.viewerId, conversationId, hostId, reviewerId]);
  }
  const modelScope = modelScopeFor(reviewer?.id);
  const modelCatalog = useHostModelOptions(
    hostId ?? null,
    reviewer?.harness ?? "",
    active && history.authorized && !!reviewer,
    { poll: false },
  );
  const models = modelCatalog.isSuccess ? modelCatalog.data : [];
  const { model, effort } =
    modelChoice?.scope === modelScope ? modelChoice : { model: "", effort: "" };
  const validModel = !model || models.some((option) => option.id === model);
  const efforts = validModel ? (nativeEffortOptions(reviewer?.harness, models, model) ?? []) : [];
  const validEffort = !effort || efforts.some((option) => option.value === effort);
  const catalog = useSkills({
    target:
      reviewer && hostId && workspace
        ? { hostId, path: workspace, harness: reviewer.harness, agentId: reviewer.id }
        : null,
    enabled: active && useCustomSkill,
  });
  const skills = catalog.skills.filter((entry) => SKILL_NAME.test(entry.name));
  const completeCatalog = catalog.skillsStatus === "ready" && catalog.skillsWarnings.length === 0;
  const reviewable = FULL_SHA.test(revision.baseSha) && FULL_SHA.test(revision.headSha);
  const validProfile = useCustomSkill
    ? completeCatalog && skills.some((entry) => entry.name === skill)
    : !!role;
  const records = (history.query.data?.pages ?? [])
    .flatMap((page) => page.data)
    .filter((review) => review.target.pr_url === revision.prUrl);
  const listed = records.find((review) => review.id === selectedId) ?? records[0];
  const detailKey = (id: string | undefined) => ["studio-review", history.viewerId, id] as const;
  const detail = useQuery({
    queryKey: detailKey(listed?.id),
    queryFn: ({ signal }) => getReview(listed!.id, signal),
    enabled: active && !!listed,
    retry: false,
    refetchInterval: (query) => {
      const review = query.state.data ?? listed;
      return active &&
        review?.dispatch_status === "dispatching" &&
        Date.now() - review.updated_at < START_CONFIRMATION_MS
        ? POLL_MS
        : false;
    },
  });
  const selected = detail.data ?? listed;
  const current = !!selected && matchesReviewRevision(selected, revision);
  const targetMatches =
    selected?.target.host_id === hostId && selected?.target.workspace === workspace;

  function remember(review: ReviewPass) {
    queryClient.setQueryData(detailKey(review.id), review);
    queryClient.setQueryData<InfiniteData<ReviewPage, string | null>>(history.key, (old) =>
      old && old.pages.every((page) => !page.accessDenied)
        ? {
            ...old,
            pages: old.pages.map((page, index) => {
              const data = page.data.filter((entry) => entry.id !== review.id);
              return { ...page, data: index === 0 ? [review, ...data] : data };
            }),
          }
        : old,
    );
    setSelectedId(review.id);
  }

  const action = useMutation({
    mutationFn: async (intent: ReviewAction) => {
      if (intent.kind === "start") return startReview(intent.review.id);
      const attempt = captureAttempt.current;
      const captured =
        attempt?.captured ?? (await captureReview(conversationId, intent.input, intent.key));
      if (attempt?.key === intent.key) attempt.captured = captured;
      remember(captured);
      if (
        captured.target.host_id !== intent.hostId ||
        captured.target.workspace !== intent.workspace
      ) {
        throw new Error(
          "The session’s execution target changed. Review the captured target before starting.",
        );
      }
      return startReview(captured.id);
    },
    onSuccess: (review) => {
      if (captureAttempt.current?.captured?.id === review.id) captureAttempt.current = null;
      remember(review);
    },
    onError: () => {
      // A lost reply may follow a successful start. Re-read the durable state.
      void queryClient.invalidateQueries({ queryKey: ["studio-review", history.viewerId] });
    },
    onSettled: () => {
      launchInFlight.current = false;
    },
    retry: false,
  });

  function applyProfileDefaults(profile: ReviewerProfile | undefined, reviewerId: string) {
    setModelChoice({
      scope: modelScopeFor(reviewerId),
      model: profile?.defaults?.model ?? "",
      effort: profile?.defaults?.reasoning_effort ?? "",
    });
  }

  function chooseProfile(id: string) {
    const profile = profiles.find((entry) => entry.id === id);
    const suggestedHarness = profile?.defaults?.harness;
    const nextReviewer = suggestedHarness
      ? reviewers.find((candidate) => candidate.harness === suggestedHarness)
      : reviewer;
    if (suggestedHarness) {
      setAgentId(nextReviewer?.id ?? "");
      setSkill("");
    }
    if (profile?.defaults || selectedProfile?.defaults) {
      if (nextReviewer) applyProfileDefaults(profile, nextReviewer.id);
      else setModelChoice(null);
    }
    setProfileChoice(profile ? { scope: profileScope, profile } : null);
    action.reset();
  }

  function runReview() {
    if (
      launchInFlight.current ||
      !reviewer ||
      !hostId ||
      !workspace ||
      !reviewable ||
      !validProfile ||
      !validModel ||
      !validEffort
    )
      return;
    const input: CaptureReview = {
      pr_url: revision.prUrl,
      expected_base_sha: revision.baseSha,
      expected_head_sha: revision.headSha,
      agent_id: reviewer.id,
      harness: reviewer.harness,
      ...(role ? { profile_id: role.id, profile_digest: role.digest } : { skill }),
      ...(model ? { model } : {}),
      ...(effort ? { reasoning_effort: effort } : {}),
    };
    const intent = JSON.stringify({ input, hostId, workspace });
    if (captureAttempt.current?.intent !== intent) {
      captureAttempt.current = { intent, key: randomUUID() };
    }
    launchInFlight.current = true;
    action.mutate({ kind: "capture", input, key: captureAttempt.current.key, hostId, workspace });
  }

  const pending = action.isPending;
  const canRun =
    reviewable &&
    !!hostId &&
    !!workspace &&
    !!reviewer &&
    validProfile &&
    validModel &&
    validEffort;
  const canStartCaptured =
    selected?.dispatch_status === "not_requested" && current && targetMatches;
  const canRetryRead =
    selected?.dispatch_status === "unknown" || selected?.dispatch_status === "dispatching";
  const unconfirmed =
    selected?.dispatch_status === "unknown" ||
    (selected?.dispatch_status === "dispatching" &&
      Math.max(detail.dataUpdatedAt, Date.now()) - selected.updated_at >= START_CONFIRMATION_MS);
  return (
    <div className="space-y-4 p-3">
      <section aria-label="Run a review" className="space-y-3">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="font-medium">Private review</h3>
            {canEditProfiles && (
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => setEditingProfiles(true)}
              >
                Manage profiles
              </Button>
            )}
          </div>
          <p className="text-muted-foreground">
            Run a workspace profile or a custom skill in a separate session. The reviewer uses its
            normal permissions in this workspace.
          </p>
        </div>
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
          <dt className="text-muted-foreground">Revision</dt>
          <dd className="min-w-0 font-mono" title={`${revision.baseSha} → ${revision.headSha}`}>
            {reviewable
              ? `${revision.baseSha.slice(0, 8)} → ${revision.headSha.slice(0, 8)}`
              : "Unavailable"}
          </dd>
          <dt className="text-muted-foreground">Host</dt>
          <dd className="font-mono [overflow-wrap:anywhere]">{hostId ?? "Unavailable"}</dd>
          <dt className="text-muted-foreground">Workspace</dt>
          <dd className="font-mono [overflow-wrap:anywhere]">{workspace ?? "Unavailable"}</dd>
        </dl>
        <div className="grid gap-2 min-[540px]:grid-cols-2">
          <ReviewSelect
            label="Reviewer"
            value={reviewer?.id ?? ""}
            onChange={(id) => {
              setAgentId(id);
              setSkill("");
              if (!reviewer && role?.defaults && !role.defaults.harness)
                applyProfileDefaults(role, id);
              else setModelChoice(null);
              action.reset();
            }}
            options={reviewers.map((agent) => ({ value: agent.id, label: agent.display_name }))}
            disabled={pending || reviewers.length === 0}
            placeholder={agents.isPending ? "Loading reviewers…" : "Choose reviewer"}
          />
          {reviewer && (
            <ReviewSelect
              label="Model"
              value={validModel ? model || MODEL_SELECT_DEFAULT : ""}
              onChange={(value) => {
                setModelChoice({
                  scope: modelScope,
                  model: value === MODEL_SELECT_DEFAULT ? "" : value,
                  effort: "",
                });
                action.reset();
              }}
              options={[
                { value: MODEL_SELECT_DEFAULT, label: "Default" },
                ...models.map((option) => ({
                  value: option.id,
                  label: option.displayName || option.id,
                })),
              ]}
              disabled={pending}
              placeholder="Choose model again"
              description={
                modelCatalog.isPending && hostId
                  ? "Loading models…"
                  : !validModel
                    ? `Model ${model} is unavailable on this host. Choose a model or Default.`
                    : undefined
              }
            />
          )}
          {(profiles.length > 0 || selectedProfile) && (
            <ReviewSelect
              label="Reviewer profile"
              value={useCustomSkill ? CUSTOM_SKILL : (role?.id ?? "")}
              onChange={chooseProfile}
              options={[
                { value: CUSTOM_SKILL, label: "Custom skill" },
                ...profiles.map((profile) => ({ value: profile.id, label: profile.label })),
              ]}
              disabled={pending}
              placeholder="Choose profile again"
              description={role?.description}
            />
          )}
          {(efforts.length > 0 || effort) && (
            <ReviewSelect
              label="Reasoning effort"
              value={validEffort ? effort || EFFORT_SELECT_NONE : ""}
              onChange={(value) => {
                setModelChoice({
                  scope: modelScope,
                  model,
                  effort: value === EFFORT_SELECT_NONE ? "" : value,
                });
                action.reset();
              }}
              options={[{ value: EFFORT_SELECT_NONE, label: "Default" }, ...efforts]}
              disabled={pending}
              placeholder="Choose effort again"
              description={
                !validEffort && validModel
                  ? `Effort ${effort} is unavailable for this model. Choose an effort or Default.`
                  : undefined
              }
            />
          )}
          {useCustomSkill && (
            <ReviewSelect
              label="Review skill"
              value={skills.some((entry) => entry.name === skill) ? skill : ""}
              onChange={(name) => {
                setSkill(name);
                action.reset();
              }}
              options={skills.map((entry) => ({
                value: entry.name,
                label: entry.display_name || entry.name,
                title: entry.description,
              }))}
              disabled={pending || !completeCatalog || skills.length === 0}
              placeholder={catalog.skillsStatus === "loading" ? "Loading skills…" : "Choose skill"}
            />
          )}
        </div>
        {!reviewer &&
          role?.defaults?.harness &&
          !reviewers.some((agent) => agent.harness === role.defaults?.harness) && (
            <p className="text-xs text-muted-foreground">
              {REVIEW_HARNESS_LABELS[role.defaults.harness]} is unavailable. Choose a reviewer for
              this run.
            </p>
          )}
        {reviewer && modelCatalog.isError && (
          <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
            <p>Couldn’t load the host’s models.</p>
            <Button variant="outline" size="sm" onClick={() => void modelCatalog.refetch()}>
              Reload models
            </Button>
          </div>
        )}
        {(profileCatalog.isError || (selectedProfile && action.isError)) && (
          <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
            {profileCatalog.isError && <p>{profileCatalog.error.message}</p>}
            <Button
              variant="outline"
              size="sm"
              disabled={pending || profileCatalog.isFetching}
              onClick={() => void profileCatalog.refetch()}
            >
              Reload profiles
            </Button>
          </div>
        )}
        {agents.isError && (
          <p role="alert" className="text-destructive">
            Couldn’t load reviewers.
          </p>
        )}
        {agents.isSuccess && reviewers.length === 0 && (
          <p className="text-muted-foreground">
            No built-in Codex or Claude Code reviewers are available.
          </p>
        )}
        {reviewer &&
          useCustomSkill &&
          (catalog.skillsStatus === "error" || catalog.skillsWarnings.length > 0) && (
            <div role="alert" className="flex flex-wrap items-center gap-2 text-destructive">
              <p>The host’s skill catalog is unavailable or incomplete.</p>
              <Button variant="outline" size="sm" onClick={() => void catalog.refetch()}>
                Reload skills
              </Button>
            </div>
          )}
        {reviewer && useCustomSkill && completeCatalog && skills.length === 0 && (
          <p className="text-muted-foreground">
            This reviewer has no supported skills in the selected workspace.
          </p>
        )}
        <Button onClick={runReview} disabled={pending || !canRun}>
          {pending && <Loader2Icon className="animate-spin" data-icon="inline-start" />}
          {pending ? "Requesting review…" : "Run review"}
        </Button>
        {action.isError && (
          <p role="alert" className="text-destructive">
            {action.error.message}
          </p>
        )}
      </section>
      {canEditProfiles && (
        <ProfileEditorDialog
          open={editingProfiles}
          onOpenChange={setEditingProfiles}
          endpoint={`/v1/sessions/${encodeURIComponent(conversationId)}/studio/review-profiles/editor`}
          hostId={hostId}
          workspace={workspace}
        />
      )}
      <section aria-label="Review history" className="space-y-3 border-t border-border pt-3">
        <h3 className="font-medium">Review history</h3>
        {history.query.isError && (
          <div className="flex flex-wrap items-center gap-2">
            <p role="alert" className="text-destructive">
              Couldn’t load review history.
            </p>
            <Button
              variant="outline"
              size="sm"
              disabled={history.query.isFetching}
              onClick={() => {
                if (history.query.isFetchNextPageError) void history.query.fetchNextPage();
                else void history.query.refetch();
              }}
            >
              Retry history
            </Button>
          </div>
        )}
        {records.length === 0 ? (
          <p className="text-muted-foreground">
            No reviews for this pull request in the loaded history.
          </p>
        ) : (
          <Select
            value={selected?.id ?? ""}
            onValueChange={(id) => {
              setSelectedId(id);
              action.reset();
            }}
          >
            <SelectTrigger
              aria-label="Select a captured review"
              className="w-full"
              disabled={pending}
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {records.map((review) => (
                <SelectItem key={review.id} value={review.id}>
                  {reviewSettingsLabel(review)} · {review.target.head_sha.slice(0, 8)} ·{" "}
                  {new Date(review.created_at).toLocaleString()}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        {history.query.hasNextPage && !history.query.isFetchNextPageError && (
          <Button
            variant="ghost"
            size="sm"
            disabled={history.query.isFetching}
            onClick={() => void history.query.fetchNextPage()}
          >
            Load older reviews
          </Button>
        )}
        {selected && (
          <>
            <div className="space-y-1">
              <div className="flex flex-wrap items-center gap-2" role="status">
                <Badge variant="secondary">
                  {unconfirmed ? "Start unconfirmed" : DISPATCH_LABELS[selected.dispatch_status]}
                </Badge>
                {!current && <Badge variant="outline">Earlier revision</Badge>}
              </div>
              <p className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">
                {selected.target.base_sha.slice(0, 8)} → {selected.target.head_sha.slice(0, 8)} ·{" "}
                {selected.target.workspace}
              </p>
              <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
                {reviewSettingsLabel(selected)}
              </p>
              {unconfirmed && (
                <p className="text-muted-foreground">
                  Launch is still unconfirmed. Refresh its status before requesting another run.
                </p>
              )}
              {detail.isError && (
                <p role="alert" className="text-destructive">
                  {detail.error.message}
                </p>
              )}
            </div>
            {canStartCaptured && (
              <Button
                variant="outline"
                size="sm"
                disabled={pending}
                onClick={() => {
                  if (launchInFlight.current) return;
                  launchInFlight.current = true;
                  action.mutate({ kind: "start", review: selected });
                }}
              >
                Start captured review
              </Button>
            )}
            {canRetryRead && (
              <Button
                variant="outline"
                size="sm"
                disabled={detail.isFetching}
                onClick={() => {
                  void detail.refetch();
                  if (selected.reviewer_session_id) {
                    void queryClient.invalidateQueries({
                      queryKey: sessionQueryOptions(selected.reviewer_session_id).queryKey,
                    });
                  }
                }}
              >
                Refresh start status
              </Button>
            )}
            <ReviewResult
              review={selected}
              active={active}
              viewerId={history.viewerId}
              current={current}
              headRepoUrl={headRepoUrl}
              onOpenSource={onOpenSource}
            />
          </>
        )}
      </section>
    </div>
  );
}
