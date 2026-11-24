/** Version 1 of the Studio extension contract. Methods use the caller's signed-in identity. */
export const STUDIO_METHOD_PERMISSIONS = {
  "studio.tasks.list": "studio.tasks",
  "studio.tasks.forSession": "studio.tasks",
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
  "studio.fleet.dispatchFreeAgent": "studio.fleet",
  "studio.fleet.wakeWorkspace": "studio.fleet",
  "studio.fleet.stopWorkspace": "studio.fleet",
  "studio.fleet.allocatePreview": "studio.fleet",
  "studio.fleet.claimLease": "studio.fleet",
  "studio.fleet.heartbeat": "studio.fleet",
  "studio.fleet.finishRound": "studio.fleet",
  "studio.fleet.adminSeats": "studio.fleet",
  "studio.reviews.listQueue": "studio.reviews",
  "studio.reviews.getWalkthrough": "studio.reviews",
  "studio.reviews.askFrozen": "studio.reviews",
  "studio.reviews.dispute": "studio.reviews",
  "studio.reviews.dismiss": "studio.reviews",
  "studio.reviews.listRuns": "studio.reviews",
  "studio.releases.getCandidate": "studio.releases",
  "studio.releases.recordScenario": "studio.releases",
  "studio.releases.createIssueConfirmed": "studio.releases",
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
  native_bound: boolean;
  last_result: {
    source: "native" | "manual";
    round: number;
    status: "verified" | "check_failed" | "unverified" | "env_blocked";
    commit_sha: string | null;
    base_sha: string | null;
    output_digest: string | null;
    ci_url: string | null;
  } | null;
  idempotency_key: string;
  task_id: string;
  harness: StudioHarness;
  state: "active" | "unknown" | "passed" | "escalated";
  fix_round: number;
  lease_expires_at: number;
}
export type StudioSeatRoutingMode = "subscription" | "fallback";
export interface StudioFleetCapacity {
  bot_review_held: number;
  other_held: number;
  bot_review_limit: 1;
  other_limit: 3;
}
export interface StudioFleetInventory {
  leases: StudioLease[];
  seats: StudioSeat[];
  hosts: StudioHost[];
  previews: StudioPreview[];
  can_admin_seats: boolean;
  fallback_order: ("api" | "on_prem")[];
  capacity: StudioFleetCapacity | null;
}
export interface StudioSeat {
  routing_mode: StudioSeatRoutingMode;
  usage_7d?: number;
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
export interface StudioPreview {
  repository:string; pr_number:number; requested_sha:string; checked_out_sha:string; url:string; template_version:string; workspace_id:string; build_id:string;
}
export interface StudioHost {
  host_id:string; name:string; status:"online"|"offline"; configured_harnesses:Record<string,unknown>; managed:boolean;
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
export interface StudioReviewQueueItem {
  review_id: string;
  repository: string;
  pull_number: number;
  base_sha: string;
  head_sha: string;
  mode: "delta" | "full";
  state: string;
  lane: "auto" | "human_review" | "vibe_check";
  protected_paths: string[];
  selected_lenses: string[];
}
export interface StudioCitation {
  path: string;
  line: number;
  side: "base" | "head";
  url: string;
}
export type StudioEvidenceStatus =
  "verified" | "check_failed" | "env_blocked" | "unverified";
export type StudioReviewAction = "dispute" | "dismiss";
export interface StudioReviewDecision {
  review_id: string;
  proof_id: string;
  lens: string;
  path: string;
  line: number;
  reason: string;
}
export interface StudioWalkthrough extends StudioReviewQueueItem {
  walkthrough: (StudioCitation & {
    text: string;
    layer: "summary" | "component" | "detail";
    services_path: string | null;
    services_line: number | null;
    services_url?: string;
  })[];
  proofs: (StudioCitation & {
    id: string;
    lens: string;
    text: string;
    severity: "blocks_merge" | "should_fix" | "note";
    status: StudioEvidenceStatus;
    command: string;
    observed: string;
    allowed_actions: StudioReviewAction[];
  })[];
  dispositions: (Omit<StudioReviewDecision, "review_id"> & {
    actor: string;
    action: StudioReviewAction;
  })[];
  evidence_status: StudioEvidenceStatus;
  services_revision: string | null;
  can_decide: boolean;
  can_ask: boolean;
  shadow: { raw: string[]; verified: string[]; final: string[] } | null;
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
      forSession: (params: { session_id: string }) =>
        request<StudioList<StudioTask>>("studio.tasks.forSession", params),
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
      inventory: (page: StudioPage & { task_id?: string } = {}) =>
        request<StudioFleetInventory>("studio.fleet.inventory", page),
      dispatchFreeAgent: (params:LeaseRef & TaskRef & {harness:StudioHarness}) => request<StudioLease>("studio.fleet.dispatchFreeAgent", params),
      wakeWorkspace: (params:{host_id:string}) => request<{host_id:string;running:boolean|null}>("studio.fleet.wakeWorkspace",params),
      stopWorkspace: (params:{host_id:string}) => request<{host_id:string;running:boolean|null}>("studio.fleet.stopWorkspace",params),
      allocatePreview: (params:TaskRef & StudioPullRequest) => request<{workspace_id:string;template_version:string;requested_sha:string}>("studio.fleet.allocatePreview",params),
      claimLease: (params: LeaseRef & TaskRef & { harness: StudioHarness }) =>
        request<StudioLease>("studio.fleet.claimLease", params),
      heartbeat: (params: LeaseRef) =>
        request<StudioLease>("studio.fleet.heartbeat", params),
      finishRound: (params: LeaseRef & { round: number; passed: boolean }) =>
        request<StudioLease>("studio.fleet.finishRound", params),
      adminSeats: (
        params: Pick<StudioSeat, "slot_id" | "harness" | "enabled"> & {
          encrypted_profile_ref?: string;
          routing_mode?: StudioSeatRoutingMode;
          weight?: number;
        },
      ) => request<StudioSeat>("studio.fleet.adminSeats", params),
    },
    reviews: {
      listQueue: (params: StudioPage = {}) =>
        request<
          StudioList<StudioReviewQueueItem> & { next_cursor: string | null }
        >("studio.reviews.listQueue", params),
      getWalkthrough: (params: { review_id: string }) =>
        request<StudioWalkthrough>("studio.reviews.getWalkthrough", params),
      askFrozen: (params: { review_id: string; question: string }) =>
        request<{
          answer: string;
          citations: StudioCitation[];
          head_sha: string;
        }>("studio.reviews.askFrozen", params),
      dispute: (params: StudioReviewDecision) =>
        request<StudioWalkthrough>("studio.reviews.dispute", params),
      dismiss: (params: StudioReviewDecision) =>
        request<StudioWalkthrough>("studio.reviews.dismiss", params),
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
      createIssueConfirmed: (params: {
        candidate_id: string;
        scenario_id: string;
        head_sha: string;
        request_id: string;
      }) =>
        request<PackagePayload>("studio.releases.createIssueConfirmed", params),
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
