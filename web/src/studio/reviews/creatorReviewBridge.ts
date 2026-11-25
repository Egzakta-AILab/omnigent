import { ExtensionHostServiceError } from "@/extensions/services/errors";
import { ApiError } from "@/lib/sessionsApi";
import type {
  ReviewCapability,
  ReviewCheckResult,
  ReviewPanelPage,
  ReviewProofTarget,
} from "../../../../sdks/web-extension/src/reviewPanel";
import { getReviewDiscussion, listFindingReproductions, type ReproductionSide } from "./api";

export type { ReviewProofTarget } from "../../../../sdks/web-extension/src/reviewPanel";

const PAGE_SIZE = 16;
const DIGEST = /^[a-f0-9]{64}$/;

type HostMethod = (params: unknown, signal: AbortSignal) => Promise<ReviewPanelPage | null>;

function invalidParams(): never {
  throw new ExtensionHostServiceError("InvalidParams", "Invalid review panel parameters");
}

function parameters(params: unknown, keys: string[]): Record<string, unknown> {
  if (
    !params ||
    typeof params !== "object" ||
    Array.isArray(params) ||
    Object.keys(params).some((key) => !keys.includes(key))
  ) {
    invalidParams();
  }
  return params as Record<string, unknown>;
}

function parseDigest(value: unknown): string {
  if (typeof value !== "string" || value.length !== 64 || !DIGEST.test(value)) invalidParams();
  return value;
}

function summarizeCheck(side: ReproductionSide): ReviewCheckResult {
  return {
    commit: side.commit,
    state: side.state,
    exitCode: side.exit_code,
    elapsedMs: side.elapsed_ms,
    oomKilled: side.oom_killed,
  };
}

async function authorizedRead<T>(request: Promise<T>, signal: AbortSignal): Promise<T> {
  try {
    const result = await request;
    signal.throwIfAborted();
    return result;
  } catch (error) {
    signal.throwIfAborted();
    if (error instanceof ApiError && [401, 403, 404].includes(error.status)) {
      throw new ExtensionHostServiceError("Unavailable", "Review data is unavailable");
    }
    throw new ExtensionHostServiceError("HostError", "Could not read review data");
  }
}

function notFound(): never {
  throw new ExtensionHostServiceError("Unavailable", "Review data is unavailable");
}

export function createReviewPanelMethods(
  runId: string,
  capabilities: readonly ReviewCapability[],
  actions: Record<"openProof" | "attachProof", (target: ReviewProofTarget) => boolean>,
): Record<string, HostMethod> {
  async function loadDiscussion(signal: AbortSignal) {
    signal.throwIfAborted();
    const discussion = await authorizedRead(getReviewDiscussion(runId, signal), signal);
    if (discussion.id !== runId) notFound();
    return discussion;
  }

  async function moveToProof(
    action: "openProof" | "attachProof",
    params: unknown,
    signal: AbortSignal,
  ) {
    const input = parameters(params, ["findingKey", "receiptDigest"]);
    const target = {
      findingKey: parseDigest(input.findingKey),
      receiptDigest: parseDigest(input.receiptDigest),
    };
    const discussion = await loadDiscussion(signal);
    if (
      !discussion.findings.some(({ key }) => key === target.findingKey) ||
      (action === "attachProof" && !discussion.can_ask)
    ) {
      notFound();
    }
    const receipts = await authorizedRead(
      listFindingReproductions(runId, target.findingKey, signal),
      signal,
    );
    if (!receipts.some(({ digest }) => digest === target.receiptDigest)) notFound();
    if (!actions[action](target)) {
      throw new ExtensionHostServiceError("Busy", "Finish the current discussion action first");
    }
    return null;
  }

  const methods: Record<ReviewCapability, HostMethod> = {
    "review.read": async (params, signal) => {
      const input = parameters(params, ["findingKey", "offset"]);
      const findingKey = input.findingKey === undefined ? undefined : parseDigest(input.findingKey);
      const offset = input.offset === undefined ? 0 : input.offset;
      if (typeof offset !== "number" || !Number.isSafeInteger(offset) || offset < 0) {
        invalidParams();
      }
      const discussion = await loadDiscussion(signal);
      const end = offset + PAGE_SIZE;
      if (findingKey === undefined) {
        return {
          kind: "findings",
          baseSha: discussion.base_sha,
          headSha: discussion.head_sha,
          canAsk: discussion.can_ask === true,
          findings: discussion.findings
            .slice(offset, end)
            .map(({ key, finding: { path, line, title, severity } }) => ({
              key,
              path,
              line,
              title,
              severity,
            })),
          nextOffset: end < discussion.findings.length ? end : null,
        };
      }
      if (!discussion.findings.some(({ key }) => key === findingKey)) notFound();
      const receipts = await authorizedRead(
        listFindingReproductions(runId, findingKey, signal),
        signal,
      );
      return {
        kind: "proofs",
        findingKey,
        proofs: receipts.slice(offset, end).map(({ digest, receipt }) => ({
          digest,
          kind: "base" in receipt ? "reproduction" : "fix",
          observedAt: receipt.observed_at,
          before: summarizeCheck("base" in receipt ? receipt.base : receipt.before),
          after: summarizeCheck("head" in receipt ? receipt.head : receipt.after),
        })),
        nextOffset: end < receipts.length ? end : null,
      };
    },
    "review.openProof": (params, signal) => moveToProof("openProof", params, signal),
    "review.attachProof": (params, signal) => moveToProof("attachProof", params, signal),
  };
  return Object.fromEntries(
    Object.entries(methods).filter(([method]) =>
      capabilities.some((granted) => granted === method),
    ),
  );
}
