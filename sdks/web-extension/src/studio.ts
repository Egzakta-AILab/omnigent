/** Version 1 of the Studio extension contract. Methods use the caller's signed-in identity. */
export const STUDIO_METHOD_PERMISSIONS = {
  "studio.tasks.list": "studio.tasks",
  "studio.tasks.getOrCreate": "studio.tasks",
  "studio.tasks.updateAcceptance": "studio.tasks",
  "studio.tasks.linkPr": "studio.tasks",
  "studio.tasks.linkSession": "studio.tasks",
  "studio.tasks.createHandoff": "studio.tasks",
  "studio.tasks.syncLinearConfirmed": "studio.tasks",
  "studio.inbox.list": "studio.inbox",
  "studio.inbox.setAway": "studio.inbox",
  "studio.inbox.teamView": "studio.inbox",
  "studio.inbox.respond": "studio.inbox",
  "studio.fleet.inventory": "studio.fleet",
  "studio.fleet.claimLease": "studio.fleet",
  "studio.fleet.heartbeat": "studio.fleet",
  "studio.fleet.finishRound": "studio.fleet",
  "studio.fleet.adminSeats": "studio.fleet",
  "studio.reviews.listRuns": "studio.reviews",
  "studio.releases.getCandidate": "studio.releases",
  "studio.releases.recordScenario": "studio.releases",
  "studio.releases.signOff": "studio.releases",
  "studio.releases.list": "studio.releases",
} as const;

export type StudioMethod = keyof typeof STUDIO_METHOD_PERMISSIONS;
export type StudioPage = { limit?: number; after?: string | null };
export type StudioList<T> = { items: T[] };
export interface StudioPullRequest {
  repository: string;
  number: number;
  head_sha: string;
}
export interface StudioTask {
  task_id: string;
  linear_workspace: string;
  linear_issue_id: string;
  acceptance_check: string;
  pull_requests: StudioPullRequest[];
  session_ids: string[];
}
export interface StudioHandoffRequest {
  task_id: string;
  schema_version: "studio-handoff@1";
  request_id: string;
  recipient: string;
  question: string;
  evidence?: string[];
}
export type StudioDecision = "accept" | "not_me" | "away" | "reply";
export type StudioHandoff = StudioHandoffRequest & {
  sender: string;
  response: { decision: StudioDecision; message: string | null } | null;
};
export type StudioHarness =
  | "claude-native"
  | "codex-native"
  | "cursor-native"
  | "pi-native"
  | "opencode-native";
export interface StudioLease {
  idempotency_key: string;
  task_id: string;
  harness: StudioHarness;
  state: "active" | "unknown" | "passed" | "escalated";
  fix_round: number;
  lease_expires_at: number;
}
export interface StudioSeat {
  slot_id: string;
  harness: "claude-native" | "codex-native";
  enabled: boolean;
  weight: number;
  cooldown_until: number;
}
export interface StudioComment {
  task_id: string;
  request_id: string;
  body: string;
}
export interface StudioIssueChange {
  task_id: string;
  request_id: string;
  change: "status" | "assignee";
  value: string;
}
type PackagePayload = Record<string, unknown>;

export interface StudioReviewEntry {
  id: string;
  project_name: string;
  repository: string;
  pr_number: number;
  head_sha: string;
  created_at: number;
}
export type StudioReleaseEvidence = Pick<
  StudioTask,
  "task_id" | "pull_requests"
>;

type TaskRef = Pick<StudioTask, "task_id">;
type LeaseRef = Pick<StudioLease, "idempotency_key">;
type StudioRequest = <T>(method: StudioMethod, params: unknown) => Promise<T>;

export function createStudioClient(request: StudioRequest) {
  return {
    tasks: {
      list: (page: StudioPage = {}) =>
        request<StudioList<StudioTask>>("studio.tasks.list", page),
      getOrCreate: (
        key: Pick<StudioTask, "linear_workspace" | "linear_issue_id">,
      ) => request<StudioTask>("studio.tasks.getOrCreate", key),
      updateAcceptance: (params: TaskRef & { acceptance_check: string }) =>
        request<StudioTask>("studio.tasks.updateAcceptance", params),
      linkPr: (params: TaskRef & StudioPullRequest) =>
        request<StudioTask>("studio.tasks.linkPr", params),
      linkSession: (params: TaskRef & { session_id: string }) =>
        request<StudioTask>("studio.tasks.linkSession", params),
      createHandoff: (params: StudioHandoffRequest) =>
        request<StudioHandoff>("studio.tasks.createHandoff", params),
      syncLinearConfirmed: (params: StudioComment | StudioIssueChange) =>
        request<
          | { comment_id: string }
          | {
              linear_issue_id: string;
              change: "status" | "assignee";
              value: string;
            }
        >("studio.tasks.syncLinearConfirmed", params),
    },
    inbox: {
      setAway: (params: PackagePayload) =>
        request<PackagePayload>("studio.inbox.setAway", params),
      teamView: (params: PackagePayload = {}) =>
        request<PackagePayload>("studio.inbox.teamView", params),
      list: (page: StudioPage = {}) =>
        request<StudioList<StudioHandoff>>("studio.inbox.list", page),
      respond: (params: {
        request_id: string;
        decision: StudioDecision;
        message?: string | null;
      }) => request<StudioHandoff>("studio.inbox.respond", params),
    },
    fleet: {
      inventory: (page: StudioPage = {}) =>
        request<{ leases: StudioLease[]; seats: StudioSeat[] }>(
          "studio.fleet.inventory",
          page,
        ),
      claimLease: (params: LeaseRef & TaskRef & { harness: StudioHarness }) =>
        request<StudioLease>("studio.fleet.claimLease", params),
      heartbeat: (params: LeaseRef) =>
        request<StudioLease>("studio.fleet.heartbeat", params),
      finishRound: (params: LeaseRef & { round: number; passed: boolean }) =>
        request<StudioLease>("studio.fleet.finishRound", params),
      adminSeats: (
        params: Pick<StudioSeat, "slot_id" | "harness" | "enabled"> & {
          encrypted_profile_ref: string;
          weight?: number;
        },
      ) => request<StudioSeat>("studio.fleet.adminSeats", params),
    },
    reviews: {
      listRuns: (page: StudioPage = {}) =>
        request<StudioList<StudioReviewEntry> & { next_cursor: string | null }>(
          "studio.reviews.listRuns",
          page,
        ),
    },
    releases: {
      getCandidate: (params: PackagePayload & { candidate_id: string }) =>
        request<PackagePayload>("studio.releases.getCandidate", params),
      recordScenario: (params: PackagePayload & { candidate_id: string }) =>
        request<PackagePayload>("studio.releases.recordScenario", params),
      signOff: (params: PackagePayload & { candidate_id: string }) =>
        request<PackagePayload>("studio.releases.signOff", params),
      list: (page: StudioPage = {}) =>
        request<StudioList<StudioReleaseEvidence>>(
          "studio.releases.list",
          page,
        ),
    },
  };
}
