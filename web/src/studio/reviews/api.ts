import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

export type ReviewHarness = "codex-native" | "claude-native";

export interface ReviewerProfile {
  id: string;
  label: string;
  description?: string | null;
  digest: string;
}

export interface ReviewPass {
  id: string;
  target: {
    pr_url: string;
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

export interface ReviewPage {
  object: "list";
  data: ReviewPass[];
  next_cursor: string | null;
  accessDenied?: true;
}

function reviewListPath(sessionId: string) {
  return `/v1/sessions/${encodeURIComponent(sessionId)}/studio/reviews`;
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
  sessionId: string,
  after: string | null,
  signal: AbortSignal,
): Promise<ReviewPage> {
  const query = new URLSearchParams(after ? { after_id: after } : {});
  const response = await authenticatedFetch(`${reviewListPath(sessionId)}?${query}`, { signal });
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

export async function listReviewerProfiles(sessionId: string, signal: AbortSignal) {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/studio/review-profiles`,
    { signal },
  );
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
        !/^[0-9a-f]{64}$/.test(profile.digest),
    ) ||
    new Set(body.profiles.map((profile) => profile.id)).size !== body.profiles.length
  ) {
    throw new Error("Invalid reviewer profile catalog");
  }
  return body.profiles;
}

export async function captureReview(sessionId: string, input: CaptureReview, key: string) {
  const result = await postReview<{ review: ReviewPass }>(reviewListPath(sessionId), key, input);
  return result.review;
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
