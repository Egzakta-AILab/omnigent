import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import { getCliServerUrl } from "@/lib/host";
import {
  approveHostedCheck,
  getReviewQuestion,
  type HostedCheckJob,
  type ProposedCheck,
  type ReviewDiscussion,
} from "./api";
import { hasTerminalControls, shellQuote } from "./sandboxCommand";

export function CheckProposal({
  check,
  proposalId,
  proposalDigest,
  initialJob,
  viewerId,
  findingKey,
  discussion,
  repository,
}: {
  check: ProposedCheck;
  proposalId: string;
  proposalDigest?: string | null;
  initialJob?: HostedCheckJob | null;
  viewerId: string | null;
  findingKey: string;
  discussion: ReviewDiscussion;
  repository: string;
}) {
  const [image, setImage] = useState(initialJob?.image_id ?? "");
  const [checkout, setCheckout] = useState(repository);
  const [status, setStatus] = useState("");
  const validImage = /^sha256:[0-9a-f]{64}$/.test(image.trim());
  const ready = validImage && !!checkout.trim() && !hasTerminalControls(checkout);
  const client = useQueryClient();
  const launch = useMutation({
    mutationFn: () =>
      approveHostedCheck(discussion.id, proposalId, proposalDigest ?? "", image.trim()),
  });
  const startingJob = launch.data ?? initialJob;
  const progress = useQuery({
    queryKey: ["studio-hosted-check", viewerId, discussion.id, proposalId],
    queryFn: async ({ signal }) =>
      (await getReviewQuestion(discussion.id, proposalId, signal)).check_job ?? null,
    enabled: startingJob?.state === "running",
    refetchInterval: (query) =>
      query.state.data?.state && query.state.data.state !== "running" ? false : 2000,
    retry: false,
    gcTime: 0,
  });
  const job = progress.data ?? startingJob;
  const receipt = job?.receipt_digest;
  useEffect(() => {
    if (!receipt) return;
    void client.invalidateQueries({
      queryKey: ["studio-finding-reproductions", viewerId, discussion.id, findingKey],
    });
    void client.invalidateQueries({
      queryKey: ["studio-finding-evidence", viewerId, discussion.id, findingKey],
    });
  }, [client, discussion.id, findingKey, receipt, viewerId]);
  async function copyCommand() {
    try {
      const server = getCliServerUrl();
      const mode = new URL(server).protocol === "http:" ? "local" : "server";
      const cli = "uv run --no-sync python scripts/verify_studio.py";
      const output = `.omnigent/studio/check-${proposalId.slice(0, 8)}-${Date.now()}.json`;
      const connection = ["--mode", mode, "--server", server];
      const run = [
        ...connection,
        "--run-id",
        discussion.id,
        "--pr",
        discussion.pr_url,
        "--proposal-id",
        proposalId,
        "--repository",
        checkout.trim(),
        "--image",
        image.trim(),
        "--timeout",
        String(check.timeout_seconds),
        ...check.paths.flatMap((path) => ["--include-path", path]),
        ...check.evidence_files.flatMap((name) => ["--capture-evidence", name]),
        "--output",
        output,
        "--",
        ...check.command,
      ];
      const attach = [...connection, "--finding", findingKey, "--receipt", output];
      if ([...run, ...attach].some(hasTerminalControls)) throw new Error("Unsafe terminal text");
      await copyText(
        `${cli} reproduce ${run.map(shellQuote).join(" ")} && ${cli} attach-proof ${attach.map(shellQuote).join(" ")}`,
      );
      setStatus("Command copied.");
    } catch {
      setStatus("Couldn’t copy the sandbox command.");
    }
  }

  return (
    <section aria-label="Sandbox check proposal" className="space-y-2 rounded-md border p-2">
      <h4 className="font-medium">Sandbox check proposal</h4>
      <p className="font-mono break-all text-xs">{check.command.map(shellQuote).join(" ")}</p>
      <p className="break-words text-xs text-muted-foreground">
        Git paths · {check.paths.join(", ")}
      </p>
      <p className="break-words text-xs text-muted-foreground">
        {check.timeout_seconds} seconds per revision
        {!!check.evidence_files.length && ` · Files: ${check.evidence_files.join(", ")}`}
      </p>
      <label className="grid gap-1 text-xs">
        Docker image ID
        <input
          className="rounded border bg-background p-1"
          value={image}
          placeholder="sha256:…"
          onChange={(event) => setImage(event.target.value)}
          disabled={launch.isPending}
        />
      </label>
      {proposalDigest && !job && (
        <>
          <p className="text-xs text-muted-foreground">
            The workspace host runs this command against both saved revisions in Docker and attaches
            the result.
          </p>
          <Button
            size="sm"
            disabled={!validImage || launch.isPending}
            onClick={() => launch.mutate()}
          >
            {launch.isPending ? "Starting check…" : "Run on workspace host"}
          </Button>
        </>
      )}
      {job && (
        <div className="space-y-1 text-xs" role="status">
          <p>
            {job.state === "running"
              ? "Sandbox check running…"
              : job.state === "attached"
                ? "Check results attached"
                : job.state === "partial"
                  ? "Check receipt attached with incomplete evidence"
                  : job.state === "failed"
                    ? "The workspace host could not run this check"
                    : "The workspace host did not confirm the check result"}
          </p>
          {job.receipt_digest && (
            <p>
              Receipt <code>{job.receipt_digest.slice(0, 12)}</code> · {job.files_attached} file
              links
            </p>
          )}
        </div>
      )}
      {(launch.error || progress.error) && (
        <p className="text-xs" role="alert">
          {(launch.error ?? progress.error)?.message}
        </p>
      )}
      <details className="space-y-2">
        <summary className="cursor-pointer text-xs">Run from terminal</summary>
        <label className="grid gap-1 text-xs">
          Repository checkout path
          <input
            className="rounded border bg-background p-1"
            value={checkout}
            onChange={(event) => setCheckout(event.target.value)}
          />
        </label>
        <Button
          size="sm"
          variant="outline"
          disabled={!ready || launch.isPending || job?.state === "running"}
          onClick={() => void copyCommand()}
        >
          Copy sandbox command
        </Button>
      </details>
      {status && (
        <p role="status" className="text-xs">
          {status}
        </p>
      )}
    </section>
  );
}
