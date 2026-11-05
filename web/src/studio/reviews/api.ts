import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

export type ReviewHarness = "codex-native" | "claude-native";

export interface ReviewProfileDefaults {
  harness?: ReviewHarness | null;
  model?: string | null;
  reasoning_effort?: string | null;
}

export interface ReviewerProfile {
  id: string;
  label: string;
  description?: string | null;
  digest: string;
  defaults?: ReviewProfileDefaults | null;
}

export interface ReviewProfileDraft {
  id: string;
  label: string;
  description: string;
  instructions: string;
  archived: boolean;
  defaults?: ReviewProfileDefaults | null;
}

export interface ReviewProfileEditorEntry extends ReviewProfileDraft {
  source: string;
  managed: boolean;
}

interface ReviewProfileEditorCatalog {
  revision: string;
  profiles: ReviewProfileEditorEntry[];
}

export const REVIEW_PROFILE_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const REVISION = /^[0-9a-f]{64}$/;
export const REVIEW_HARNESS_LABELS: Record<ReviewHarness, string> = {
  "codex-native": "Codex",
  "claude-native": "Claude Code",
};

function hasValidDefaults(defaults: ReviewProfileDefaults | null | undefined): boolean {
  return (
    defaults == null ||
    (typeof defaults === "object" &&
      !Array.isArray(defaults) &&
      (defaults.harness == null ||
        defaults.harness === "codex-native" ||
        defaults.harness === "claude-native") &&
      (defaults.model == null || typeof defaults.model === "string") &&
      (defaults.reasoning_effort == null || typeof defaults.reasoning_effort === "string"))
  );
}

export interface CandidateReviewOrigin {
  run_id: string;
  finding_key: string;
  attachment_digest: string;
  finding_title: string;
}

export interface ReviewPass {
  id: string;
  run_id?: string | null;
  run_role?: "coordinator" | "candidate" | "required" | null;
  launch_error?: string | null;
  target: {
    pr_url: string;
    repository: string;
    repository_path?: string | null;
    pr_number: number;
    base_sha: string;
    head_sha: string;
    host_id: string;
    workspace: string;
    candidate?: CandidateReviewOrigin | null;
  };
  profile: {
    agent_id: string;
    harness: ReviewHarness;
    skill: string | null;
    model?: string | null;
    reasoning_effort?: string | null;
    role?: ReviewerProfile | null;
  };
  state: "prepared" | "running" | "completed" | "failed" | "cancelled" | "unknown";
  dispatch_status:
    "not_requested" | "dispatching" | "running" | "succeeded" | "failed" | "cancelled" | "unknown";
  reviewer_session_id: string | null;
  created_at: number;
  updated_at: number;
}

export interface ReviewFinding {
  title: string;
  detail: string;
  severity: "critical" | "high" | "medium" | "low";
  path: string;
  side: "base" | "head";
  line: number;
}

export interface ReviewReport {
  state: "pending" | "incomplete" | "ready";
  summary: string | null;
  findings: ReviewFinding[];
  message_id: string | null;
  citation_validation?: "not_checked" | "checked" | "unavailable";
  unverified_findings?: ReviewFinding[];
}

export interface ReviewSource {
  prUrl: string;
  baseSha: string;
  headSha: string;
  path: string;
  side: ReviewFinding["side"];
  line: number;
}

export interface CaptureReview {
  pr_url: string;
  expected_base_sha: string;
  expected_head_sha: string;
  agent_id: string;
  harness: ReviewHarness;
  skill?: string;
  model?: string;
  reasoning_effort?: string;
  profile_id?: string;
  profile_digest?: string;
}

export type ProjectReviewMember = CaptureReview & {
  host_id: string;
  workspace: string;
  repository: string;
};

export interface CaptureReviewRun {
  reviewers: ProjectReviewMember[];
  coordinator?: ProjectReviewMember;
  required_reviewer_indices?: number[];
}

interface ReviewPlan {
  coordinator_id: string;
  base_sha: string;
  head_sha: string;
  decisions: { review_id: string; selected: boolean; reason: string }[];
}

export interface ReviewRun {
  id: string;
  target: ReviewPass["target"];
  reviewers: ReviewPass[];
  created_at: number;
  patch_sha256?: string | null;
  coordinator?: ReviewPass | null;
  plan?: ReviewPlan | null;
}

export interface ReviewRunPage {
  entries: ReviewRun[];
  next_cursor: string | null;
}

export interface ReviewRunReport {
  reviewers: { review_id: string; report: ReviewReport }[];
  findings: { finding: ReviewFinding; review_ids: string[]; finding_key?: string | null }[];
  proposal?: {
    state: "pending" | "incomplete" | "ready";
    plan: ReviewPlan | null;
    digest: string | null;
    error: string | null;
  } | null;
}

export interface ReproductionSide {
  commit: string;
  state: "exited" | "timed_out" | "setup_error";
  exit_code: number | null;
  elapsed_ms: number;
  oom_killed: boolean;
  output: string;
  output_truncated: boolean;
  evidence?: { name: string; digest: string }[];
}

interface CheckReceiptSummary {
  observed_at: string;
  image_id: string;
  command?: string[];
  timeout_seconds: number;
}

export interface FixCheckSummary extends CheckReceiptSummary {
  before: ReproductionSide;
  after: ReproductionSide;
  outcome: "fail_to_pass" | "pass_to_pass" | "fail_to_fail" | "pass_to_fail" | "inconclusive";
  included_paths: string[];
  patch: string;
}

export interface AttachedReproduction {
  digest: string;
  attached_at: number;
  provenance: "uploaded";
  candidate_run_id?: string | null;
  receipt:
    | (CheckReceiptSummary & {
        base: ReproductionSide;
        head: ReproductionSide;
        included_paths?: string[];
        proposal_id?: string | null;
        requested_evidence?: string[];
      })
    | FixCheckSummary;
}

export type DiscussionAnchor =
  | { kind: "finding"; finding_key: string }
  | { kind: "diff"; path: string; side: "base" | "head"; start: number; end: number };

export interface ReviewDiscussionComment {
  id: string;
  anchor: DiscussionAnchor;
  body: string;
  author: string | null;
  created_at: number;
}

export interface InlineDraft {
  id: string;
  thread: DiscussionAnchor;
  anchor: Extract<DiscussionAnchor, { kind: "diff" }>;
  body: string;
  version: number;
  created_at: number;
  updated_at: number;
  publication_state?: "draft" | "sending" | "posted" | "unknown";
  provider_url?: string | null;
  publication_confirmed_by?: string | null;
  publication_reference?: string | null;
}

export interface InlinePublicationPreview {
  provider: string;
  pr_url: string;
  base_sha: string;
  head_sha: string;
  digest: string;
  drafts: InlineDraft[];
}

export type QuestionAction = "question" | "propose_fix" | "propose_check";

export interface HostedCheckJob {
  id: string;
  state: "running" | "attached" | "partial" | "failed" | "unconfirmed";
  image_id: string;
  started_at: number;
  receipt_digest?: string | null;
  files_attached: number;
}

export interface ProposedCheck {
  command: string[];
  paths: string[];
  timeout_seconds: number;
  evidence_files: string[];
}

export interface AskReviewQuestion extends Pick<ReviewDiscussionComment, "id" | "anchor" | "body"> {
  action?: QuestionAction;
  proof_digest?: string;
  agent_id?: string;
  reviewer_key?: string;
  host_id?: string;
  workspace?: string;
}

export interface QuestionCitation {
  path: string;
  side: "base" | "head";
  line: number;
  quote: string;
}

export interface ReviewQuestion extends Pick<ReviewDiscussionComment, "id" | "anchor" | "body"> {
  action?: QuestionAction;
  proof_digest?: string;
  session_id: string;
  harness: ReviewHarness;
  status: "dispatching" | "accepted" | "unknown";
  answer_state: "pending" | "incomplete" | "ready";
  answer: string | null;
  proposed_patch?: string | null;
  proposed_check?: ProposedCheck | null;
  proposed_check_digest?: string | null;
  check_job?: HostedCheckJob | null;
  citations?: QuestionCitation[] | null;
  published_at: number | null;
  updated_at: number;
}

export interface SharedReviewAnswer {
  id: string;
  anchor: DiscussionAnchor;
  question: string;
  answer: string;
  citations?: QuestionCitation[] | null;
  proof_digest?: string;
  harness: ReviewHarness;
  requested_by: string | null;
  published_at: number;
}

export interface ReviewDiscussion {
  id: string;
  project_name: string;
  repository: string;
  pr_url: string;
  pr_number: number;
  base_sha: string;
  head_sha: string;
  patch: string;
  candidate?: CandidateReviewOrigin | null;
  reports: {
    key: string;
    label: string;
    state: "pending" | "incomplete" | "ready";
    summary: string | null;
  }[];
  findings: { key: string; finding: ReviewFinding }[];
  comments: ReviewDiscussionComment[];
  inline_drafts?: InlineDraft[];
  publication_drafts?: InlineDraft[] | null;
  questions?: ReviewQuestion[];
  agent_answers?: SharedReviewAnswer[];
  can_comment: boolean;
  can_draft_inline?: boolean;
  can_publish_inline?: boolean;
  can_attach_evidence?: boolean;
  can_ask?: boolean;
  can_use_review_workspace?: boolean;
  creator_available?: boolean;
  check_repository?: string | null;
  review_workspace_host_id?: string | null;
  collaborators: string[] | null;
  publishers?: string[] | null;
  sharing_available: boolean;
}

function reviewRunPath(runId: string) {
  return `/v1/studio/review-runs/${encodeURIComponent(runId)}`;
}

export function getReviewDiscussion(runId: string, signal: AbortSignal) {
  return reviewRequest<ReviewDiscussion>(`${reviewRunPath(runId)}/discussion`, { signal });
}

export interface CreatorPanelDefinition {
  slot: "review.panel";
  contract_version: "1";
  title: string;
  description: string;
  group_by: "file" | "directory";
  checks: "failed" | "all";
  proof_action: "open" | "open_and_discuss";
}

export interface CreatorPanelPreview {
  run_id: string;
  base_sha: string;
  head_sha: string;
  digest: string;
  panel: CreatorPanelDefinition;
}

export type CreatorPanelCapability = "review.read" | "review.openProof" | "review.attachProof";

export interface CreatorPanelInstallation {
  revision: number;
  enabled: boolean;
  selected_version: number | null;
  active_panel: CreatorPanelDefinition | null;
  history: {
    version: number;
    digest: string | null;
    title: string | null;
    contract_version: string | null;
    capabilities: CreatorPanelCapability[] | null;
  }[];
}

export type CreatorPanelChange = {
  expected_revision: number;
  approved_capabilities?: CreatorPanelCapability[];
} & ({ panel: CreatorPanelDefinition; digest: string } | { version: number | null });

export function getCreatorInstallation(runId: string, signal: AbortSignal) {
  return reviewRequest<CreatorPanelInstallation>(`${reviewRunPath(runId)}/creator-installation`, {
    signal,
  });
}

export function changeCreatorInstallation(runId: string, change: CreatorPanelChange) {
  return reviewRequest<CreatorPanelInstallation>(`${reviewRunPath(runId)}/creator-installation`, {
    method: "panel" in change ? "PUT" : "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(change),
  });
}

export function getCreatorPanelContract(runId: string, signal: AbortSignal) {
  return reviewRequest<{
    definition_schema: Record<string, unknown>;
    template: CreatorPanelDefinition;
  }>(`${reviewRunPath(runId)}/creator-preview`, { signal });
}

export function buildCreatorPanelPreview(runId: string, definition: string) {
  return reviewRequest<CreatorPanelPreview>(`${reviewRunPath(runId)}/creator-preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: definition,
  });
}

interface SharedReviewPage {
  entries: {
    id: string;
    project_name: string;
    repository: string;
    pr_number: number;
    head_sha: string;
    created_at: number;
  }[];
  next_cursor: string | null;
}

export function listSharedReviews(afterId: string | null, signal: AbortSignal) {
  const query = afterId ? `?after_id=${encodeURIComponent(afterId)}` : "";
  return reviewRequest<SharedReviewPage>(`/v1/studio/shared-reviews${query}`, { signal });
}

export function postReviewDiscussionComment(
  runId: string,
  comment: Pick<ReviewDiscussionComment, "id" | "anchor" | "body">,
) {
  return postReview<ReviewDiscussionComment>(
    `${reviewRunPath(runId)}/discussion/comments`,
    comment.id,
    comment,
  );
}

function inlineDraftsPath(runId: string) {
  return `${reviewRunPath(runId)}/discussion/inline-drafts`;
}

export function createInlineDraft(
  runId: string,
  draft: Pick<ReviewDiscussionComment, "id" | "anchor" | "body">,
) {
  return postReview<InlineDraft>(inlineDraftsPath(runId), draft.id, draft);
}

export function editInlineDraft(runId: string, id: string, version: number, body: string) {
  return reviewRequest<InlineDraft>(`${inlineDraftsPath(runId)}/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ version, body }),
  });
}

export async function discardInlineDraft(runId: string, id: string, version: number) {
  const response = await authenticatedFetch(
    `${inlineDraftsPath(runId)}/${encodeURIComponent(id)}/discard`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version }),
    },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
}

export function previewInlinePublication(runId: string, drafts: InlineDraft[]) {
  return submitInlinePublication(runId, "preview", drafts);
}

export function reconcileInlinePublication(runId: string, draft: InlineDraft, providerUrl: string) {
  return postReview<InlineDraft>(
    `${inlineDraftsPath(runId)}/${encodeURIComponent(draft.id)}/reconcile`,
    draft.id,
    { version: draft.version, provider_url: providerUrl },
  );
}

export function confirmInlinePublication(runId: string, preview: InlinePublicationPreview) {
  return submitInlinePublication(runId, "confirm", preview.drafts, {
    digest: preview.digest,
    confirm: true,
  });
}

function submitInlinePublication(
  runId: string,
  action: "preview" | "confirm",
  drafts: InlineDraft[],
  confirmation?: { digest: string; confirm: true },
) {
  return reviewRequest<InlinePublicationPreview>(
    `${reviewRunPath(runId)}/discussion/inline-publication/${action}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        selected: drafts.map(({ id, version }) => ({ id, version })),
        ...confirmation,
      }),
    },
  );
}

function questionsPath(runId: string) {
  return `${reviewRunPath(runId)}/discussion/questions`;
}

export function askReviewQuestion(runId: string, body: AskReviewQuestion) {
  return postReview<ReviewQuestion>(questionsPath(runId), body.id, body);
}

export function getReviewQuestion(runId: string, questionId: string, signal: AbortSignal) {
  return reviewRequest<ReviewQuestion>(
    `${questionsPath(runId)}/${encodeURIComponent(questionId)}`,
    { signal },
  );
}

export function approveHostedCheck(
  runId: string,
  proposalId: string,
  proposalDigest: string,
  imageId: string,
) {
  return postReview<HostedCheckJob>(
    `${questionsPath(runId)}/${encodeURIComponent(proposalId)}/run-check`,
    proposalId,
    { proposal_digest: proposalDigest, image_id: imageId, confirm: true },
  );
}

export function shareReviewAnswer(runId: string, questionId: string) {
  return postReview<ReviewQuestion>(
    `${questionsPath(runId)}/${encodeURIComponent(questionId)}/share`,
    questionId,
    {},
  );
}

export async function changeReviewCollaborator(
  runId: string,
  userId: string,
  revoke: boolean,
  canPublish?: boolean,
) {
  const endpoint = `${reviewRunPath(runId)}/discussion/collaborators`;
  const response = await authenticatedFetch(
    revoke ? `${endpoint}?${new URLSearchParams({ user_id: userId })}` : endpoint,
    revoke
      ? { method: "DELETE" }
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_id: userId, can_publish: canPublish }),
        },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
}
export const EVIDENCE_TYPES = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  mp4: "video/mp4",
  webm: "video/webm",
  json: "application/json",
} as const;

type EvidenceMediaType = (typeof EVIDENCE_TYPES)[keyof typeof EVIDENCE_TYPES];

export interface FindingArtifact {
  digest: string;
  source: { base_sha: string; head_sha: string; patch_sha256: string };
  media_type: EvidenceMediaType;
  byte_size: number;
  attached_at: number;
  provenance: "uploaded";
  checks?: { receipt_digest: string; side: "base" | "head"; name: string; commit: string }[];
  measurements?: {
    method: string;
    metrics: { label: string; unit: string; baseline?: number | null; observed: number }[];
  } | null;
}

function findingEvidencePath(runId: string, findingKey: string) {
  return `${reviewRunPath(runId)}/findings/${encodeURIComponent(findingKey)}/evidence`;
}

export function listFindingEvidence(runId: string, findingKey: string, signal: AbortSignal) {
  return reviewRequest<FindingArtifact[]>(findingEvidencePath(runId, findingKey), { signal });
}

export function attachFindingEvidence(
  runId: string,
  findingKey: string,
  patchDigest: string,
  file: File,
) {
  const extension = file.name.split(".").pop()?.toLowerCase() as keyof typeof EVIDENCE_TYPES;
  const mediaType = Object.hasOwn(EVIDENCE_TYPES, extension) ? EVIDENCE_TYPES[extension] : null;
  if (!mediaType) throw new Error("Choose PNG, JPEG, MP4, WebM, or measurements JSON.");
  return reviewRequest<FindingArtifact>(
    `${findingEvidencePath(runId, findingKey)}?patch_sha256=${encodeURIComponent(patchDigest)}`,
    { method: "POST", headers: { "Content-Type": mediaType }, body: file },
  );
}

export async function readFindingEvidence(
  runId: string,
  findingKey: string,
  digest: string,
  signal: AbortSignal,
) {
  const response = await authenticatedFetch(
    `${findingEvidencePath(runId, findingKey)}/${encodeURIComponent(digest)}`,
    { signal },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.blob();
}

function reproductionPath(runId: string, findingKey: string) {
  return `${reviewRunPath(runId)}/findings/${encodeURIComponent(findingKey)}/reproductions`;
}

export function findingReproductionsKey(
  viewerId: string | null,
  runId: string,
  findingKey: string,
) {
  return ["studio-finding-reproductions", viewerId, runId, findingKey] as const;
}

export function listFindingReproductions(runId: string, findingKey: string, signal: AbortSignal) {
  return reviewRequest<AttachedReproduction[]>(reproductionPath(runId, findingKey), { signal });
}

export function reReviewCandidate(runId: string, findingKey: string, digest: string) {
  return postReview<{ run: ReviewRun; errors: Record<string, string> }>(
    `${reproductionPath(runId, findingKey)}/${encodeURIComponent(digest)}/re-review`,
    `studio-candidate-${digest}`,
    {},
  );
}

export async function attachFindingReproduction(
  runId: string,
  findingKey: string,
  file: File,
  patch?: File,
) {
  if (file.size + (patch?.size ?? 0) > 192 * 1024)
    throw new Error("Choose files that total at most 192 KiB.");
  let body = await file.text();
  let receipt: Record<string, unknown> | null;
  try {
    receipt = JSON.parse(body) as Record<string, unknown> | null;
  } catch {
    throw new Error("Choose a valid JSON check receipt.");
  }
  if (receipt?.schema_version === "omnigent.studio.fix-verification.v1") {
    if (!patch) throw new Error("Choose the Git patch used for this fix check.");
    body = JSON.stringify({ ...receipt, patch: await patch.text() });
    if (new TextEncoder().encode(body).length > 192 * 1024)
      throw new Error("The encoded receipt and patch exceed 192 KiB.");
  } else if (patch) {
    throw new Error("Choose a fix check receipt to attach a proposed patch.");
  }
  return reviewRequest<AttachedReproduction>(reproductionPath(runId, findingKey), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });
}

function projectReviewRunsPath(projectId: string) {
  return `/v1/projects/${encodeURIComponent(projectId)}/studio/review-runs`;
}

export function captureReviewRun(projectId: string, roster: CaptureReviewRun, key: string) {
  return postReview<ReviewRun>(projectReviewRunsPath(projectId), key, roster);
}

export function startReviewRun(runId: string, proposalDigest?: string) {
  return postReview<{ run: ReviewRun; errors: Record<string, string> }>(
    `${reviewRunPath(runId)}/start`,
    `studio-review-run-start-${runId}`,
    proposalDigest ? { proposal_digest: proposalDigest } : {},
  );
}

export async function listProjectReviewRuns(
  projectId: string,
  prUrl: string,
  afterId: string | null,
  signal: AbortSignal,
) {
  const params = new URLSearchParams({ pr_url: prUrl });
  if (afterId) params.set("after_id", afterId);
  const page = await reviewRequest<ReviewRunPage>(`${projectReviewRunsPath(projectId)}?${params}`, {
    signal,
  });
  if (!Array.isArray(page.entries) || (afterId && page.next_cursor === afterId)) {
    throw new Error("Invalid review run history");
  }
  return page;
}

export async function getCapturedReviewDiff(
  run: ReviewRun,
  source: ReviewSource | null,
  signal: AbortSignal,
) {
  const query = source
    ? `?${new URLSearchParams({ path: source.path, side: source.side, line: String(source.line) })}`
    : "";
  const evidence = await reviewRequest<{
    base_sha: string;
    head_sha: string;
    sha256: string;
    patch: string;
    source_line?: string | null;
  }>(`${reviewRunPath(run.id)}/patch${query}`, { signal });
  if (
    evidence.base_sha !== run.target.base_sha ||
    evidence.head_sha !== run.target.head_sha ||
    evidence.sha256 !== run.patch_sha256
  ) {
    throw new Error("The saved diff does not match this review run.");
  }
  return evidence;
}

export function getReviewRunReport(runId: string, signal: AbortSignal) {
  return reviewRequest<ReviewRunReport>(
    `${reviewRunPath(runId)}/report?include_finding_keys=true`,
    { signal },
  );
}

export interface ProjectReviewWorkspace {
  projectId: string;
  hostId: string;
  workspace: string;
}

export interface ReviewPage {
  object: "list";
  data: ReviewPass[];
  next_cursor: string | null;
  accessDenied?: true;
}

export type ReviewScope = { sessionId: string } | { projectId: string; prUrl: string };

function reviewListPath(scope: ReviewScope) {
  const origin =
    "sessionId" in scope
      ? `sessions/${encodeURIComponent(scope.sessionId)}`
      : `projects/${encodeURIComponent(scope.projectId)}`;
  return `/v1/${origin}/studio/reviews`;
}

function reviewPath(reviewId: string) {
  return `/v1/studio/reviews/${encodeURIComponent(reviewId)}`;
}

async function reviewRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(path, init);
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as T;
}

function postReview<T>(path: string, key: string, body: object): Promise<T> {
  return reviewRequest<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": key },
    body: JSON.stringify(body),
  });
}

export async function listReviews(
  scope: ReviewScope,
  after: string | null,
  signal: AbortSignal,
): Promise<ReviewPage> {
  const query = new URLSearchParams(after ? { after_id: after } : {});
  if ("projectId" in scope) query.set("pr_url", scope.prUrl);
  const response = await authenticatedFetch(`${reviewListPath(scope)}?${query}`, { signal });
  if ([401, 403, 404].includes(response.status)) {
    // Cache a denial so a subsequent transport failure cannot revive stale access.
    return { object: "list", data: [], next_cursor: null, accessDenied: true };
  }
  if (!response.ok) throw await apiErrorFromResponse(response);
  const page = (await response.json()) as ReviewPage;
  if (
    page.object !== "list" ||
    !Array.isArray(page.data) ||
    (after && page.next_cursor === after)
  ) {
    throw new Error("Invalid review list");
  }
  return page;
}

export function reviewProfilesPath(
  scope: { sessionId: string } | ProjectReviewWorkspace,
  editor = false,
) {
  const path = `studio/review-profiles${editor ? "/editor" : ""}`;
  if ("sessionId" in scope) return `/v1/sessions/${encodeURIComponent(scope.sessionId)}/${path}`;
  const query = new URLSearchParams({ host_id: scope.hostId, workspace: scope.workspace });
  return `/v1/projects/${encodeURIComponent(scope.projectId)}/${path}?${query}`;
}

export async function listReviewerProfiles(endpoint: string, signal: AbortSignal) {
  const response = await authenticatedFetch(endpoint, { signal });
  // A server from before the catalog was added can still run a custom skill.
  if (response.status === 404) return [];
  if (!response.ok) throw await apiErrorFromResponse(response);
  const body = (await response.json()) as { profiles?: ReviewerProfile[] } | null;
  if (
    !Array.isArray(body?.profiles) ||
    body.profiles.some(
      (profile) =>
        !profile ||
        typeof profile.id !== "string" ||
        !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$/.test(profile.id) ||
        typeof profile.label !== "string" ||
        !profile.label.trim() ||
        (profile.description != null && typeof profile.description !== "string") ||
        typeof profile.digest !== "string" ||
        !REVISION.test(profile.digest) ||
        !hasValidDefaults(profile.defaults),
    ) ||
    new Set(body.profiles.map((profile) => profile.id)).size !== body.profiles.length
  ) {
    throw new Error("Invalid reviewer profile catalog");
  }
  return body.profiles;
}

async function profileEditorRequest(endpoint: string, init: RequestInit) {
  const result = await reviewRequest<ReviewProfileEditorCatalog | null>(endpoint, init);
  if (
    typeof result?.revision !== "string" ||
    !result.revision ||
    result.revision.length > 256 ||
    !Array.isArray(result.profiles) ||
    result.profiles.some(
      (profile) =>
        !profile ||
        typeof profile.id !== "string" ||
        !REVIEW_PROFILE_ID.test(profile.id) ||
        typeof profile.label !== "string" ||
        typeof profile.description !== "string" ||
        typeof profile.instructions !== "string" ||
        typeof profile.source !== "string" ||
        typeof profile.managed !== "boolean" ||
        typeof profile.archived !== "boolean" ||
        !hasValidDefaults(profile.defaults),
    ) ||
    new Set(result.profiles.map((profile) => profile.id)).size !== result.profiles.length
  ) {
    throw new Error("Invalid reviewer profile editor response");
  }
  return result;
}

export function getReviewProfileEditor(endpoint: string, signal: AbortSignal) {
  return profileEditorRequest(endpoint, { signal });
}

export function saveReviewProfileEditor(
  endpoint: string,
  revision: string,
  profile: ReviewProfileDraft,
) {
  return profileEditorRequest(endpoint, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ revision, profile }),
  });
}

export async function captureReview(
  scope: ReviewScope,
  input: CaptureReview & { host_id?: string; workspace?: string; repository?: string },
  key: string,
) {
  const result = await postReview<{ review: ReviewPass }>(reviewListPath(scope), key, input);
  return result.review;
}

export async function getReviewForSession(sessionId: string, signal: AbortSignal) {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/studio/review`,
    { signal },
  );
  if ([401, 403, 404].includes(response.status)) return null;
  if (!response.ok) throw await apiErrorFromResponse(response);
  const review = (await response.json()) as ReviewPass;
  if (review.reviewer_session_id !== sessionId) throw new Error("Review session mismatch");
  return review;
}

export function getReview(reviewId: string, signal: AbortSignal) {
  return reviewRequest<ReviewPass>(reviewPath(reviewId), { signal });
}

export function startReview(reviewId: string) {
  return postReview<ReviewPass>(
    `${reviewPath(reviewId)}/start`,
    `studio-review-start-${reviewId}`,
    {},
  );
}

export function getReviewReport(reviewId: string, signal: AbortSignal) {
  return reviewRequest<ReviewReport>(`${reviewPath(reviewId)}/report`, { signal });
}

export function matchesReviewRevision(
  review: ReviewPass,
  source: Pick<ReviewSource, "prUrl" | "baseSha" | "headSha">,
) {
  return (
    review.target.pr_url === source.prUrl &&
    review.target.base_sha === source.baseSha &&
    review.target.head_sha === source.headSha
  );
}
