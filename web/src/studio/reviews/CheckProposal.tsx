import { useState } from "react";
import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import { getCliServerUrl } from "@/lib/host";
import type { ProposedCheck, ReviewDiscussion } from "./api";
import { hasTerminalControls, shellQuote } from "./sandboxCommand";

export function CheckProposal({
  check,
  proposalId,
  findingKey,
  discussion,
  repository,
}: {
  check: ProposedCheck;
  proposalId: string;
  findingKey: string;
  discussion: ReviewDiscussion;
  repository: string;
}) {
  const [image, setImage] = useState("");
  const [checkout, setCheckout] = useState(repository);
  const [status, setStatus] = useState("");
  const validImage = /^sha256:[0-9a-f]{64}$/.test(image.trim());
  const ready = validImage && !!checkout.trim() && !hasTerminalControls(checkout);
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
      <p className="text-xs text-muted-foreground">
        Run the copied command from the Omnigent checkout on a machine with Docker. It checks both
        saved revisions and attaches the result to this finding.
      </p>
      <label className="grid gap-1 text-xs">
        Repository checkout path
        <input
          className="rounded border bg-background p-1"
          value={checkout}
          onChange={(event) => setCheckout(event.target.value)}
        />
      </label>
      <label className="grid gap-1 text-xs">
        Local Docker image ID
        <input
          className="rounded border bg-background p-1"
          value={image}
          placeholder="sha256:…"
          onChange={(event) => setImage(event.target.value)}
        />
      </label>
      <Button size="sm" variant="outline" disabled={!ready} onClick={() => void copyCommand()}>
        Copy sandbox command
      </Button>
      {status && (
        <p role="status" className="text-xs">
          {status}
        </p>
      )}
    </section>
  );
}
