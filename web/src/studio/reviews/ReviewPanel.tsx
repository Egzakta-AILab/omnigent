import { useId, useRef, useState } from "react";
import {
  type InfiniteData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Loader2Icon } from "lucide-react";
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
import { ReviewModelRefresh } from "./ReviewModelRefresh";
import { sessionQueryOptions, useSession } from "@/hooks/useSession";
import { useSkills } from "@/hooks/useSkills";
import { useIdentityReady, useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { nativeCodingAgentForAgentName } from "@/lib/nativeCodingAgents";
import { canonicalNativeHarness } from "@/lib/nativeHarnessModes";
import { randomUUID } from "@/lib/randomUUID";
import {
  captureReview,
  captureReviewRun,
  getReview,
  listReviewerProfiles,
  listReviews,
  matchesReviewRevision,
  REVIEW_HARNESS_LABELS,
  reviewProfilesPath,
  type ProjectReviewMember,
  type ProjectReviewWorkspace,
  startReview,
  startReviewRun,
  type CaptureReview,
  type CaptureReviewRun,
  type ReviewHarness,
  type ReviewPage,
  type ReviewPass,
  type ReviewRun,
  type ReviewRunPage,
  type ReviewScope,
  type ReviewerProfile,
  type ReviewSource,
} from "./api";
import { ProfileEditorDialog } from "./ProfileEditorDialog";
import { reviewModelLabels } from "./reviewModelLabels";
import { ReviewResult, reviewSettingsLabel } from "./ReviewResult";

import { ReviewRunsPanel, reviewRunsKey } from "./ReviewRunsPanel";

const POLL_MS = 3_000;
const START_CONFIRMATION_MS = 120_000;
const FULL_SHA = /^[0-9a-f]{40}(?:[0-9a-f]{24})?$/;
const SKILL_NAME = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const CUSTOM_SKILL = "__custom_skill__";
const ORCHESTRATOR_PROFILE = "orchestrator";
const MAX_RUN_REVIEWERS = 16;

export function useStudioReviewHistory(conversationId: string, enabled: boolean) {
  return useReviewHistory({ sessionId: conversationId }, enabled);
}

function useReviewHistory(scope: ReviewScope, enabled: boolean) {
  const identityReady = useIdentityReady();
  const viewerId = useViewerId();
  const key = ["studio-reviews", viewerId, scope] as const;
  const query = useInfiniteQuery({
    queryKey: key,
    queryFn: ({ pageParam, signal }) => listReviews(scope, pageParam, signal),
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
  options: { value: string; label: string; title?: string | null; disabled?: boolean }[];
  disabled: boolean;
  placeholder?: string;
  description?: string | null;
}) {
  return (
    <div className="min-w-0 space-y-1">
      <p className="text-xs text-muted-foreground">{label}</p>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger
          aria-label={label}
          disabled={disabled}
          className="mobile-touch-target w-full"
        >
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem
              key={option.value}
              value={option.value}
              disabled={option.disabled}
              title={option.title ?? undefined}
            >
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

interface ReviewPanelProps {
  active: boolean;
  singleReviewerOnly?: boolean;
  revision: CurrentRevision;
  headRepoUrl?: string | null;
  onOpenSource?: (source: ReviewSource) => void;
}

type ReviewOrigin = { sessionId: string } | (ProjectReviewWorkspace & { repository: string });

export function ReviewPanel({
  conversationId,
  history,
  ...props
}: ReviewPanelProps & {
  conversationId: string;
  history: ReturnType<typeof useStudioReviewHistory>;
}) {
  return <ReviewForm {...props} origin={{ sessionId: conversationId }} history={history} />;
}

export function ProjectReviewPanel({
  projectId,
  hostId,
  workspace,
  repository,
  onReviewStarted,
  ...props
}: ReviewPanelProps &
  ProjectReviewWorkspace & {
    repository: string;
    hasCurrentReview?: boolean;
    previousProfile?: ReviewPass["profile"];
    onReviewStarted?: (review: ReviewPass) => void;
  }) {
  const history = useReviewHistory({ projectId, prUrl: props.revision.prUrl }, props.active);
  return (
    <ReviewForm
      key={JSON.stringify([projectId, hostId, workspace, repository, props.revision.prUrl])}
      {...props}
      origin={{ projectId, hostId, workspace, repository }}
      history={history}
      onReviewStarted={onReviewStarted}
    />
  );
}

function ReviewForm({
  origin,
  active,
  singleReviewerOnly,
  revision,
  headRepoUrl,
  history,
  onOpenSource,
  onReviewStarted,
  previousProfile,
  hasCurrentReview = false,
}: ReviewPanelProps & {
  origin: ReviewOrigin;
  hasCurrentReview?: boolean;
  previousProfile?: ReviewPass["profile"];
  history: ReturnType<typeof useReviewHistory>;
  onReviewStarted?: (review: ReviewPass) => void;
}) {
  const queryClient = useQueryClient();
  const serverInfo = useServerInfo();
  const [setupOpened, setSetupOpened] = useState(false);
  const setupId = useId();
  const showSetup = !hasCurrentReview || !!previousProfile || setupOpened;
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
  const projectOrigin = "projectId" in origin ? origin : null;
  const { session } = useSession(active && "sessionId" in origin ? origin.sessionId : null);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [manualScope, setManualScope] = useState<string | null>(null);
  const [draftRun, setDraftRun] = useState<{ scope: string; members: ProjectReviewMember[] }>();
  const runAttempt = useRef<{ intent: string; key: string } | undefined>(undefined);
  const runInFlight = useRef(false);
  const hostId = projectOrigin?.hostId ?? session?.hostId;
  const workspace = projectOrigin?.workspace ?? session?.workspace;
  const scope =
    "sessionId" in origin ? origin : { projectId: origin.projectId, prUrl: revision.prUrl };
  const runScope = JSON.stringify([history.viewerId, projectOrigin, revision]);
  const orchestrated = !!projectOrigin && !singleReviewerOnly && manualScope !== runScope;
  const members = draftRun?.scope === runScope ? draftRun.members : [];
  const runKey = reviewRunsKey(history.viewerId, projectOrigin?.projectId ?? "", revision.prUrl);
  const runAction = useMutation({
    mutationFn: async ({
      projectId,
      roster,
      key,
    }: {
      projectId: string;
      roster: CaptureReviewRun;
      key: string;
    }) => {
      const captured = await captureReviewRun(projectId, roster, key);
      rememberRun(captured);
      const target = roster.reviewers[0];
      if (
        captured.target.host_id !== target.host_id ||
        captured.target.workspace !== target.workspace ||
        captured.target.repository_path !== target.repository ||
        captured.target.base_sha !== target.expected_base_sha ||
        captured.target.head_sha !== target.expected_head_sha
      )
        throw new Error(
          "The captured review run did not match the selected checkout and revision.",
        );
      const started = await startReviewRun(captured.id);
      rememberRun(started.run);
      return started;
    },
    onSuccess: () => {
      if (!singleReviewerOnly) runAttempt.current = undefined;
      setDraftRun((current) => (current?.scope === runScope ? undefined : current));
    },
    onSettled: () => {
      runInFlight.current = false;
      void queryClient.invalidateQueries({ queryKey: runKey });
    },
  });

  function rememberRun(run: ReviewRun) {
    queryClient.setQueryData<InfiniteData<ReviewRunPage, string | null>>(runKey, (old) => ({
      pages: old?.pages.length
        ? old.pages.map((page, index) => ({
            ...page,
            entries: [
              ...(index === 0 ? [run] : []),
              ...page.entries.filter((entry) => entry.id !== run.id),
            ],
          }))
        : [{ entries: [run], next_cursor: null }],
      pageParams: old?.pageParams ?? [null],
    }));
    setSelectedRunId(run.id);
    void queryClient.invalidateQueries({
      queryKey: ["studio-review-run-report", history.viewerId, run.id],
    });
  }

  function requestRun(roster: CaptureReviewRun, openReviewer = false) {
    if (!projectOrigin || !history.authorized || runInFlight.current) return;
    const intent = JSON.stringify(roster);
    if (runAttempt.current?.intent !== intent) runAttempt.current = { intent, key: randomUUID() };
    runInFlight.current = true;
    setSetupOpened(true);
    runAction.mutate(
      { projectId: projectOrigin.projectId, roster, key: runAttempt.current.key },
      {
        onSuccess: ({ run }) => {
          const review = run.reviewers[0];
          if (
            openReviewer &&
            review?.reviewer_session_id &&
            (review.dispatch_status === "running" || review.dispatch_status === "succeeded")
          ) {
            onReviewStarted?.(review);
          }
        },
      },
    );
  }

  const profileEndpoint = reviewProfilesPath(origin);
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
  const previousReviewer =
    previousProfile && (!projectOrigin || previousProfile.role?.id !== ORCHESTRATOR_PROFILE)
      ? reviewers.find(
          (agent) =>
            agent.id === previousProfile.agent_id && agent.harness === previousProfile.harness,
        )
      : undefined;
  const profileScope = JSON.stringify([history.viewerId, profileEndpoint, hostId, workspace]);
  const profileCatalog = useQuery({
    queryKey: ["studio-review-profiles", history.viewerId, profileEndpoint, hostId, workspace],
    queryFn: ({ signal }) => listReviewerProfiles(profileEndpoint, signal),
    enabled: active && history.authorized && !!hostId && !!workspace,
    staleTime: 15_000,
    retry: false,
  });
  const profiles = profileCatalog.isSuccess ? profileCatalog.data : [];
  const reviewerProfiles = projectOrigin
    ? profiles.filter((profile) => profile.id !== ORCHESTRATOR_PROFILE)
    : profiles;
  const coordinatorProfile = profiles.find((profile) => profile.id === ORCHESTRATOR_PROFILE);
  const defaultTaskProfile =
    singleReviewerOnly && reviewerProfiles.length === 1 ? reviewerProfiles[0] : null;
  const selectedProfile =
    profileChoice?.scope === profileScope ? profileChoice.profile : defaultTaskProfile;
  const role = profiles.find(
    (profile) => profile.id === selectedProfile?.id && profile.digest === selectedProfile.digest,
  );
  const reviewer = agentId
    ? reviewers.find((agent) => agent.id === agentId)
    : singleReviewerOnly && role
      ? (reviewers.find((agent) => agent.harness === (role.defaults?.harness ?? "codex-native")) ??
        (!role.defaults?.harness ? reviewers[0] : undefined))
      : undefined;
  const useCustomSkill = !singleReviewerOnly && selectedProfile === null;
  function modelScopeFor(reviewerId: string | undefined) {
    return JSON.stringify([history.viewerId, profileEndpoint, hostId, reviewerId]);
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
    modelChoice?.scope === modelScope
      ? modelChoice
      : {
          model: singleReviewerOnly ? (role?.defaults?.model ?? "") : "",
          effort: singleReviewerOnly ? (role?.defaults?.reasoning_effort ?? "") : "",
        };
  const { modelId, modelName, modelSummary, defaultModelLabel, defaultEffort, defaultEffortLabel } =
    reviewModelLabels(models, model);
  const validModel = !model || !!modelId;
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

  function profileMember(profile: ReviewerProfile): ProjectReviewMember | null {
    if (!projectOrigin || !hostId || !workspace) return null;
    const defaults = profile.defaults;
    const agent = defaults?.harness
      ? reviewers.find((candidate) => candidate.harness === defaults.harness)
      : (reviewers.find((candidate) => candidate.harness === "codex-native") ?? reviewers[0]);
    if (!agent) return null;
    return {
      pr_url: revision.prUrl,
      expected_base_sha: revision.baseSha,
      expected_head_sha: revision.headSha,
      host_id: hostId,
      workspace,
      repository: projectOrigin.repository,
      agent_id: agent.id,
      harness: agent.harness,
      profile_id: profile.id,
      profile_digest: profile.digest,
      ...(defaults?.model ? { model: defaults.model } : {}),
      ...(defaults?.reasoning_effort ? { reasoning_effort: defaults.reasoning_effort } : {}),
    };
  }

  const coordinator = coordinatorProfile ? profileMember(coordinatorProfile) : null;
  const candidates = reviewerProfiles.flatMap((profile) => {
    const member = profileMember(profile);
    return member ? [member] : [];
  });
  const orchestrationIssue =
    profileCatalog.isPending || agents.isPending
      ? "Loading workspace reviewer profiles…"
      : profileCatalog.isError || agents.isError
        ? "Couldn’t load the workspace profiles or available agents."
        : !coordinatorProfile
          ? "This workspace needs an orchestrator profile before it can choose reviewers."
          : !coordinator
            ? "The Orchestrator’s agent is unavailable on this host."
            : !candidates.length
              ? "Add a specialist profile with an available agent."
              : candidates.length > MAX_RUN_REVIEWERS
                ? `Orchestrator supports up to ${MAX_RUN_REVIEWERS} eligible reviewer profiles per run.`
                : null;
  const validProfile = useCustomSkill
    ? completeCatalog && skills.some((entry) => entry.name === skill)
    : !!role;
  const records = (history.query.data?.pages ?? [])
    .flatMap((page) => page.data)
    .filter((review) => !review.run_id && review.target.pr_url === revision.prUrl);
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
    selected?.target.host_id === hostId &&
    selected?.target.workspace === workspace &&
    (!projectOrigin || selected?.target.repository_path === projectOrigin.repository);

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
      const captured = attempt?.captured ?? (await captureReview(scope, intent.input, intent.key));
      if (attempt?.key === intent.key) attempt.captured = captured;
      remember(captured);
      if (
        captured.target.host_id !== intent.hostId ||
        captured.target.workspace !== intent.workspace
      ) {
        throw new Error(
          "The workspace’s execution target changed. Review the captured target before starting.",
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

  function requestReview(intent: ReviewAction) {
    action.mutate(intent, {
      onSuccess: (review) => {
        if (
          review.reviewer_session_id &&
          (review.dispatch_status === "running" || review.dispatch_status === "succeeded")
        ) {
          onReviewStarted?.(review);
        }
      },
    });
  }

  function reviewInput(): CaptureReview | null {
    if (!reviewer || !reviewable || !validProfile || !validModel || !validEffort) return null;
    return {
      pr_url: revision.prUrl,
      expected_base_sha: revision.baseSha,
      expected_head_sha: revision.headSha,
      agent_id: reviewer.id,
      harness: reviewer.harness,
      ...(role ? { profile_id: role.id, profile_digest: role.digest } : { skill }),
      ...(model ? { model } : {}),
      ...(effort ? { reasoning_effort: effort } : {}),
    };
  }

  function addToRun() {
    const input = reviewInput();
    if (!input || !projectOrigin || !hostId || !workspace) return;
    const config = { ...input, host_id: hostId, workspace, repository: projectOrigin.repository };
    if (
      members.length >= MAX_RUN_REVIEWERS ||
      members.some((member) => JSON.stringify(member) === JSON.stringify(config))
    )
      return;
    setDraftRun({ scope: runScope, members: [...members, config] });
    runAction.reset();
  }

  function draftLabel(config: ProjectReviewMember) {
    const name =
      profiles.find((profile) => profile.id === config.profile_id)?.label ??
      config.profile_id ??
      config.skill;
    return [name, REVIEW_HARNESS_LABELS[config.harness], config.model].filter(Boolean).join(" · ");
  }

  function runReview() {
    const input = reviewInput();
    if (launchInFlight.current || !input || !hostId || !workspace) return;
    if (projectOrigin) {
      requestRun(
        {
          reviewers: [
            { ...input, host_id: hostId, workspace, repository: projectOrigin.repository },
          ],
        },
        true,
      );
      return;
    }
    const intent = JSON.stringify({ input, hostId, workspace });
    if (captureAttempt.current?.intent !== intent) {
      captureAttempt.current = { intent, key: randomUUID() };
    }
    launchInFlight.current = true;
    requestReview({ kind: "capture", input, key: captureAttempt.current.key, hostId, workspace });
  }

  const pending = action.isPending;
  const canRun =
    history.authorized &&
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
  const reviewRuns = projectOrigin && history.authorized && active && (
    <ReviewRunsPanel
      key="review-runs"
      projectId={projectOrigin.projectId}
      revision={revision}
      viewerId={history.viewerId}
      selectedRunId={selectedRunId}
      onSelectRun={setSelectedRunId}
      onOpenSource={onOpenSource}
      launchErrors={runAction.data}
      launchPending={runAction.isPending}
    />
  );
  return (
    <div className="@container/review space-y-5 p-3">
      {hasCurrentReview && reviewRuns}
      {hasCurrentReview && !previousProfile && (
        <Button
          variant="outline"
          size="sm"
          aria-expanded={showSetup}
          aria-controls={setupId}
          disabled={pending || runAction.isPending}
          onClick={() => {
            if (!showSetup) setManualScope(null);
            setSetupOpened(!showSetup);
          }}
        >
          {showSetup ? "Hide new review setup" : "New review"}
        </Button>
      )}
      <section
        id={setupId}
        aria-label="Run a review"
        hidden={!showSetup}
        onFocusCapture={projectOrigin ? () => setSetupOpened(true) : undefined}
        className="space-y-3"
      >
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
          {projectOrigin && !singleReviewerOnly && (
            <div
              role="group"
              aria-label="Review type"
              className="flex w-fit gap-1 rounded-lg bg-muted/60 p-0.5"
            >
              <Button
                variant={orchestrated ? "secondary" : "ghost"}
                size="sm"
                className="mobile-touch-target"
                aria-pressed={orchestrated}
                disabled={runAction.isPending}
                onClick={() => setManualScope(null)}
              >
                Orchestrator
              </Button>
              <Button
                variant={orchestrated ? "ghost" : "secondary"}
                size="sm"
                className="mobile-touch-target"
                aria-pressed={!orchestrated}
                disabled={runAction.isPending}
                onClick={() => setManualScope(runScope)}
              >
                Manual
              </Button>
            </div>
          )}
          <p className="pt-1 text-sm text-muted-foreground">
            {orchestrated
              ? "The Orchestrator investigates the PR and its linked task, then recommends specialists."
              : "Choose which agents investigate this pull request."}
          </p>
        </div>
        {orchestrated && (
          <div className="space-y-2">
            {orchestrationIssue ? (
              <div className="space-y-2">
                <p role="status" className="text-sm text-muted-foreground">
                  {orchestrationIssue}
                </p>
                {(profileCatalog.isError || agents.isError) && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      if (profileCatalog.isError) void profileCatalog.refetch();
                      if (agents.isError) void agents.refetch();
                    }}
                  >
                    Retry loading profiles and agents
                  </Button>
                )}
              </div>
            ) : (
              <details className="text-sm text-muted-foreground">
                <summary className="cursor-pointer">
                  {candidates.length} available{" "}
                  {candidates.length === 1 ? "specialist" : "specialists"}
                </summary>
                <ul className="mt-2 space-y-1">
                  {candidates.map((candidate) => (
                    <li key={candidate.profile_id}>{draftLabel(candidate)}</li>
                  ))}
                </ul>
              </details>
            )}
            <Button
              className="mobile-touch-target"
              disabled={
                !!orchestrationIssue || !reviewable || !history.authorized || runAction.isPending
              }
              onClick={() => {
                if (coordinator) requestRun({ coordinator, reviewers: candidates });
              }}
            >
              {runAction.isPending ? "Starting orchestrator…" : "Run orchestrated review"}
            </Button>
          </div>
        )}
        {previousReviewer && previousProfile && (
          <Button
            variant="outline"
            size="sm"
            disabled={pending || runAction.isPending}
            onClick={() => {
              setManualScope(runScope);
              setAgentId(previousReviewer.id);
              setSkill(previousProfile.skill ?? "");
              setProfileChoice(
                previousProfile.role
                  ? { scope: profileScope, profile: previousProfile.role }
                  : null,
              );
              setModelChoice({
                scope: modelScopeFor(previousReviewer.id),
                model: previousProfile.model ?? "",
                effort: previousProfile.reasoning_effort ?? "",
              });
              action.reset();
            }}
          >
            Use previous reviewer settings
          </Button>
        )}
        <p className="flex flex-wrap items-baseline gap-x-2 text-xs text-muted-foreground">
          <span>Revision</span>
          <code className="min-w-0 font-mono" title={`${revision.baseSha} → ${revision.headSha}`}>
            {reviewable
              ? `${revision.baseSha.slice(0, 8)} → ${revision.headSha.slice(0, 8)}`
              : "Unavailable"}
          </code>
        </p>
        {!orchestrated && (
          <div className="space-y-3">
            <div className="grid gap-3 @min-[440px]/review:grid-cols-2">
              {(reviewerProfiles.length > 0 || selectedProfile) && (
                <ReviewSelect
                  label="Reviewer profile"
                  value={useCustomSkill ? CUSTOM_SKILL : (role?.id ?? "")}
                  onChange={chooseProfile}
                  options={[
                    ...(singleReviewerOnly ? [] : [{ value: CUSTOM_SKILL, label: "Custom skill" }]),
                    ...reviewerProfiles.map((profile) => ({
                      value: profile.id,
                      label: profile.label,
                    })),
                  ]}
                  disabled={pending}
                  placeholder="Choose a profile"
                  description={role?.description}
                />
              )}
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
                disabled={pending || !reviewers.length}
                placeholder={agents.isPending ? "Loading reviewers…" : "Choose reviewer"}
              />
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
                  placeholder={
                    catalog.skillsStatus === "loading" ? "Loading skills…" : "Choose skill"
                  }
                />
              )}
            </div>
            {!reviewer &&
              role?.defaults?.harness &&
              !reviewers.some((agent) => agent.harness === role.defaults?.harness) && (
                <p className="text-xs text-muted-foreground">
                  {REVIEW_HARNESS_LABELS[role.defaults.harness]} is unavailable. Choose a reviewer
                  for this run.
                </p>
              )}
            <details className="border-y border-border">
              <summary className="cursor-pointer py-2.5 text-sm focus-visible:outline-2 focus-visible:outline-ring">
                Model and workspace settings
                {reviewer && (
                  <span className="ml-2 text-xs text-muted-foreground">
                    {!validModel || !validEffort
                      ? "Selection needs attention"
                      : [modelSummary, effort || defaultEffort].filter(Boolean).join(" · ")}
                  </span>
                )}
              </summary>
              <div className="space-y-3 pb-3">
                <div className="grid gap-3 @min-[440px]/review:grid-cols-2">
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
                        { value: MODEL_SELECT_DEFAULT, label: defaultModelLabel },
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
                            ? `Model ${model} is unavailable on this host. Choose another model.`
                            : modelId && modelId !== modelName
                              ? `Model ID: ${modelId}`
                              : undefined
                      }
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
                      options={[
                        { value: EFFORT_SELECT_NONE, label: defaultEffortLabel },
                        ...efforts,
                      ]}
                      disabled={pending}
                      placeholder="Choose effort again"
                      description={
                        !validEffort && validModel
                          ? `Effort ${effort} is unavailable for this model. Choose another effort.`
                          : undefined
                      }
                    />
                  )}
                </div>
                {reviewer && hostId && (
                  <ReviewModelRefresh
                    key={modelScope}
                    hostId={hostId}
                    harness={reviewer.harness}
                    loading={modelCatalog.isFetching}
                    loadError={modelCatalog.isError}
                  />
                )}
                <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
                  <dt className="text-muted-foreground">Host</dt>
                  <dd className="font-mono [overflow-wrap:anywhere]">{hostId ?? "Unavailable"}</dd>
                  <dt className="text-muted-foreground">Workspace</dt>
                  <dd className="font-mono [overflow-wrap:anywhere]">
                    {workspace ?? "Unavailable"}
                  </dd>
                </dl>
              </div>
            </details>
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
            {projectOrigin && members.length > 0 && (
              <ul aria-label="Selected reviewers" className="space-y-1">
                {members.map((member, index) => (
                  <li
                    key={JSON.stringify(member)}
                    className="flex flex-wrap items-center justify-between gap-2 text-sm"
                  >
                    <span className="[overflow-wrap:anywhere]">{draftLabel(member)}</span>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={runAction.isPending}
                      onClick={() =>
                        setDraftRun({
                          scope: runScope,
                          members: members.filter((_, position) => index !== position),
                        })
                      }
                    >
                      Remove
                    </Button>
                  </li>
                ))}
              </ul>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                className="mobile-touch-target"
                onClick={() => {
                  if (members.length) requestRun({ reviewers: members });
                  else runReview();
                }}
                disabled={
                  pending || runAction.isPending || (members.length ? !history.authorized : !canRun)
                }
              >
                {(pending || runAction.isPending) && (
                  <Loader2Icon className="animate-spin" data-icon="inline-start" />
                )}
                {pending || runAction.isPending
                  ? "Requesting review…"
                  : members.length
                    ? "Run selected reviewers"
                    : singleReviewerOnly
                      ? "Review current revision"
                      : "Run review"}
              </Button>
              {projectOrigin && !singleReviewerOnly && (
                <Button
                  variant="outline"
                  className="mobile-touch-target"
                  onClick={addToRun}
                  disabled={
                    !canRun || pending || runAction.isPending || members.length >= MAX_RUN_REVIEWERS
                  }
                >
                  Add reviewer
                </Button>
              )}
            </div>
          </div>
        )}
        {runAction.error && (
          <p role="alert" className="text-destructive">
            {runAction.error.message}
          </p>
        )}
        {action.isError && (
          <p role="alert" className="text-destructive">
            {action.error.message}
          </p>
        )}
      </section>
      {!hasCurrentReview && reviewRuns}
      {canEditProfiles && (
        <ProfileEditorDialog
          open={editingProfiles}
          onOpenChange={setEditingProfiles}
          endpoint={reviewProfilesPath(origin, true)}
          hostId={hostId}
          workspace={workspace}
        />
      )}
      {(!projectOrigin ||
        records.length > 0 ||
        history.query.isError ||
        history.query.hasNextPage) && (
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
                  {!current && <Badge variant="outline">Outdated</Badge>}
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
                    requestReview({ kind: "start", review: selected });
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
      )}
    </div>
  );
}
