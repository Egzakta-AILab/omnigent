import { useEffect, useRef, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  findingReproductionsKey,
  listFindingReproductions,
  type CreatorPanelDefinition,
  type ReproductionSide,
  type ReviewDiscussion,
} from "./api";
import { createReviewPanelMethods, type ReviewProofTarget } from "./creatorReviewBridge";

export type ShowCreatorProof = (
  target: ReviewProofTarget,
  attach: boolean,
  alsoOpen?: boolean,
) => boolean;

const FINDINGS_PER_PAGE = 8;

function checkStatus(side: ReproductionSide): string {
  if (side.oom_killed) return "Out of memory";
  if (side.state === "timed_out") return "Timed out";
  if (side.state === "setup_error" || side.exit_code === null) return "Unavailable";
  return side.exit_code === 0 ? "Passed" : `Failed · exit ${side.exit_code}`;
}

function groupPath(path: string, group: CreatorPanelDefinition["group_by"]) {
  if (group === "file") return path;
  return path.slice(0, Math.max(0, path.lastIndexOf("/"))) || "Repository root";
}

export function CreatorPanelPreview({
  panel,
  discussion,
  viewerId,
  onProof,
  label = "Generated panel preview",
  checkCurrent,
}: {
  panel: CreatorPanelDefinition;
  discussion: ReviewDiscussion;
  viewerId: string | null;
  onProof: ShowCreatorProof;
  label?: string;
  checkCurrent?: (signal: AbortSignal) => Promise<void>;
}) {
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const active = useRef<AbortController | null>(null);
  const showProof = useRef(onProof);
  useEffect(() => {
    showProof.current = onProof;
  }, [onProof]);
  useEffect(() => () => active.current?.abort(), []);
  const lastPage = Math.max(0, Math.ceil(discussion.findings.length / FINDINGS_PER_PAGE) - 1);
  const currentPage = Math.min(page, lastPage);
  const start = currentPage * FINDINGS_PER_PAGE;
  const findings = discussion.findings.slice(start, start + FINDINGS_PER_PAGE);
  const results = useQueries({
    queries: findings.map(({ key }) => ({
      queryKey: findingReproductionsKey(viewerId, discussion.id, key),
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        listFindingReproductions(discussion.id, key, signal),
      retry: false,
      staleTime: 0,
      gcTime: 0,
    })),
  });
  const rows = findings.flatMap(({ key, finding }, index) =>
    (results[index].data ?? []).flatMap(({ digest, receipt }) => {
      if (!results[index].isSuccess) return [];
      const before = checkStatus("base" in receipt ? receipt.base : receipt.before);
      const after = checkStatus("head" in receipt ? receipt.head : receipt.after);
      return panel.checks === "failed" && before === "Passed" && after === "Passed"
        ? []
        : [{ key, finding, digest, before, after, observedAt: receipt.observed_at }];
    }),
  );
  const groups = new Map<string, typeof rows>();
  for (const row of rows) {
    const path = groupPath(row.finding.path, panel.group_by);
    const group = groups.get(path) ?? [];
    group.push(row);
    groups.set(path, group);
  }
  const loading = results.some((result) => result.isPending);
  const unavailable = results.some((result) => result.isError);
  const attach = panel.proof_action === "open_and_discuss" && discussion.can_ask === true;
  const capability = attach ? "review.attachProof" : "review.openProof";
  async function activate(target: ReviewProofTarget) {
    if (active.current) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setActionError("");
    try {
      await checkCurrent?.(controller.signal);
      controller.signal.throwIfAborted();
      const methods = createReviewPanelMethods(discussion.id, [capability], {
        openProof: (proof) => showProof.current(proof, false),
        attachProof: (proof) => showProof.current(proof, true, true),
      });
      await methods[capability](target, controller.signal);
    } catch (error) {
      if (!controller.signal.aborted)
        setActionError(error instanceof Error ? error.message : "Could not open the proof");
    } finally {
      active.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return (
    <section aria-label={label} className="min-w-0 space-y-3 rounded-md border p-3">
      <div>
        <h3 className="break-words font-semibold">{panel.title}</h3>
        {panel.description && (
          <p className="break-words text-sm text-muted-foreground">{panel.description}</p>
        )}
      </div>
      {loading && <p role="status">Loading check receipts…</p>}
      {unavailable && (
        <div className="flex flex-wrap items-center gap-2">
          <p role="alert">Some check receipts are unavailable.</p>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              for (const result of results) if (result.isError) void result.refetch();
            }}
          >
            Retry receipts
          </Button>
        </div>
      )}
      {!loading && !unavailable && groups.size === 0 && (
        <p className="text-sm text-muted-foreground">
          {findings.length === 0
            ? "This review has no findings."
            : panel.checks === "failed"
              ? "No failed checks on this page."
              : "No check receipts on this page."}
        </p>
      )}
      {[...groups].map(([path, checks]) => (
        <div key={path} className="space-y-1">
          <h4 className="break-all font-mono text-xs text-muted-foreground">{path}</h4>
          <ul className="divide-y rounded-md border">
            {checks.map(({ key, finding, digest, before, after, observedAt }) => (
              <li
                key={`${key}:${digest}`}
                className="flex flex-wrap items-center gap-2 p-2 text-sm"
              >
                <div className="min-w-0 flex-1">
                  <p className="break-words">{finding.title}</p>
                  <p className="break-words text-xs text-muted-foreground">
                    {before} → {after} · {new Date(observedAt).toLocaleString()} ·{" "}
                    {digest.slice(0, 8)}
                  </p>
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={busy}
                  onClick={() => void activate({ findingKey: key, receiptDigest: digest })}
                >
                  {attach ? "Open and discuss" : "Open proof"}
                </Button>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {actionError && <p role="alert">{actionError}</p>}
      {discussion.findings.length > FINDINGS_PER_PAGE && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Button
            variant="outline"
            size="sm"
            disabled={currentPage === 0 || busy}
            onClick={() => setPage(currentPage - 1)}
          >
            Previous findings
          </Button>
          <span>
            {start + 1}–{start + findings.length} of {discussion.findings.length} findings
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={start + findings.length >= discussion.findings.length || busy}
            onClick={() => setPage(currentPage + 1)}
          >
            Next findings
          </Button>
        </div>
      )}
    </section>
  );
}
