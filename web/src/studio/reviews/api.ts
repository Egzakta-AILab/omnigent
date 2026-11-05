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

export interface ReviewPass {
  id: string;
  target: {
    pr_url: string;
    repository: string;
    repository_path?: string | null;
    pr_number: number;
    base_sha: string;
    head_sha: string;
    host_id: string;
    workspace: string;
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
