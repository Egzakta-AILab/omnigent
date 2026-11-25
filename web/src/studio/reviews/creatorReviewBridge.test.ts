import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/sessionsApi";
import { getReviewDiscussion, listFindingReproductions, type ReviewDiscussion } from "./api";
import { createReviewPanelMethods } from "./creatorReviewBridge";

vi.mock("./api", () => ({
  getReviewDiscussion: vi.fn(),
  listFindingReproductions: vi.fn(),
}));

const findingKey = "a".repeat(64);
const receiptDigest = "b".repeat(64);
const baseSha = "1".repeat(40);
const headSha = "2".repeat(40);
const discussion: ReviewDiscussion = {
  id: "run",
  project_name: "Project",
  repository: "example/project",
  pr_url: "https://example.test/pull/1",
  pr_number: 1,
  base_sha: baseSha,
  head_sha: headSha,
  patch: "private patch",
  reports: [],
  findings: [
    {
      key: findingKey,
      finding: {
        path: "src/check.ts",
        line: 8,
        side: "head",
        title: "Missing validation",
        severity: "high",
        detail: "private detail",
      },
    },
  ],
  comments: [],
  can_ask: true,
  can_comment: true,
  collaborators: null,
  sharing_available: false,
};
const side = {
  commit: baseSha,
  state: "exited" as const,
  exit_code: 0,
  elapsed_ms: 100,
  oom_killed: false,
  output: "private command output",
  output_truncated: false,
};
const actions = { openProof: vi.fn(() => true), attachProof: vi.fn(() => true) };
const signal = new AbortController().signal;
const target = { findingKey, receiptDigest };

beforeEach(() => {
  vi.resetAllMocks();
  actions.openProof.mockReturnValue(true);
  actions.attachProof.mockReturnValue(true);
  vi.mocked(getReviewDiscussion).mockResolvedValue(discussion);
  vi.mocked(listFindingReproductions).mockResolvedValue([
    {
      digest: receiptDigest,
      attached_at: 1,
      provenance: "uploaded",
      receipt: {
        image_id: "private image",
        observed_at: "2026-11-05T13:00:00Z",
        timeout_seconds: 30,
        command: ["private command"],
        base: side,
        head: { ...side, commit: headSha, exit_code: 1 },
      },
    },
  ]);
});

describe("review panel bridge", () => {
  it("grants only declared methods and projects only the current run's safe fields", async () => {
    const methods = createReviewPanelMethods("run", ["review.read"], actions);
    expect(Object.keys(methods)).toEqual(["review.read"]);
    const result = await methods["review.read"]({}, signal);
    expect(result).toEqual({
      kind: "findings",
      baseSha,
      headSha,
      canAsk: true,
      findings: [
        {
          key: findingKey,
          path: "src/check.ts",
          line: 8,
          title: "Missing validation",
          severity: "high",
        },
      ],
      nextOffset: null,
    });
    const receipts = await methods["review.read"]({ findingKey }, signal);
    expect(receipts).toMatchObject({
      kind: "proofs",
      findingKey,
      proofs: [{ digest: receiptDigest, after: { commit: headSha, exitCode: 1 } }],
    });
    expect(JSON.stringify(receipts)).not.toContain("private");
    expect(getReviewDiscussion).toHaveBeenCalledWith("run", signal);
    expect(listFindingReproductions).toHaveBeenCalledWith("run", findingKey, signal);
  });

  it("checks membership and authorization before routing a proof or selecting it for a question", async () => {
    const methods = createReviewPanelMethods(
      "run",
      ["review.openProof", "review.attachProof"],
      actions,
    );
    await expect(methods["review.openProof"](target, signal)).resolves.toBeNull();
    expect(actions.openProof).toHaveBeenCalledWith(target);
    await expect(methods["review.attachProof"](target, signal)).resolves.toBeNull();
    expect(actions.attachProof).toHaveBeenCalledWith(target);
    await expect(
      methods["review.attachProof"]({ ...target, receiptDigest: "f".repeat(64) }, signal),
    ).rejects.toMatchObject({ code: "Unavailable" });
    vi.mocked(getReviewDiscussion).mockResolvedValueOnce({ ...discussion, can_ask: false });
    await expect(methods["review.attachProof"](target, signal)).rejects.toMatchObject({
      code: "Unavailable",
    });
    expect(actions.attachProof).toHaveBeenCalledTimes(1);
  });

  it("refuses cross-run parameters, foreign findings and revoked access", async () => {
    const methods = createReviewPanelMethods("run", ["review.read", "review.openProof"], actions);
    await expect(methods["review.read"]({ runId: "different" }, signal)).rejects.toMatchObject({
      code: "InvalidParams",
    });
    expect(getReviewDiscussion).not.toHaveBeenCalled();
    await expect(
      methods["review.openProof"]({ ...target, findingKey: "f".repeat(64) }, signal),
    ).rejects.toMatchObject({ code: "Unavailable" });
    expect(listFindingReproductions).not.toHaveBeenCalled();
    vi.mocked(getReviewDiscussion).mockRejectedValueOnce(new ApiError("private error", 403, null));
    await expect(methods["review.openProof"](target, signal)).rejects.toMatchObject({
      code: "Unavailable",
      message: "Review data is unavailable",
    });
    expect(actions.openProof).not.toHaveBeenCalled();
  });
});
