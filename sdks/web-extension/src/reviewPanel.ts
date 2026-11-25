export type ReviewCapability = "review.read" | "review.openProof" | "review.attachProof";

export interface ReviewFindingSummary {
  key: string;
  path: string;
  line: number;
  title: string;
  severity: "critical" | "high" | "medium" | "low";
}

export interface ReviewProofTarget {
  findingKey: string;
  receiptDigest: string;
}

export interface ReviewCheckResult {
  commit: string;
  state: "exited" | "timed_out" | "setup_error";
  exitCode: number | null;
  elapsedMs: number;
  oomKilled: boolean;
}

export interface ReviewProofSummary {
  digest: string;
  kind: "reproduction" | "fix";
  observedAt: string;
  before: ReviewCheckResult;
  after: ReviewCheckResult;
}

export type ReviewPanelPage =
  | {
      kind: "findings";
      baseSha: string;
      headSha: string;
      canAsk: boolean;
      findings: ReviewFindingSummary[];
      nextOffset: number | null;
    }
  | {
      kind: "proofs";
      findingKey: string;
      proofs: ReviewProofSummary[];
      nextOffset: number | null;
    };

export interface ReviewPanelApi {
  read(options?: { findingKey?: string; offset?: number }): Promise<ReviewPanelPage>;
  openProof(target: ReviewProofTarget): Promise<void>;
  attachProof(target: ReviewProofTarget): Promise<void>;
}
