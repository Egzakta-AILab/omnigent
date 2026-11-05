import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import { getCliServerUrl } from "@/lib/host";
import {
  attachFindingReproduction,
  listFindingReproductions,
  type AttachedReproduction,
  type ReproductionSide,
  type ReviewRun,
} from "./api";

function shellQuote(value: string) {
  return "'" + value.replaceAll("'", "'\"'\"'") + "'";
}

function hasTerminalControls(value: string) {
  return [...value].some((character) => {
    const code = character.charCodeAt(0);
    return (
      code < 32 ||
      (code >= 127 && code <= 159) ||
      (code >= 0x202a && code <= 0x202e) ||
      (code >= 0x2066 && code <= 0x2069)
    );
  });
}

function rerunCommand(
  run: Pick<ReviewRun, "id" | "target">,
  receipt: AttachedReproduction["receipt"],
) {
  if (!receipt.command) throw new Error("The command is unavailable.");
  const server = getCliServerUrl();
  const mode = new URL(server).protocol === "http:" ? "local" : "server";
  const relative = run.target.repository_path;
  const repository =
    relative && relative !== "."
      ? run.target.workspace.replace(/\/$/, "") + "/" + relative
      : run.target.workspace;
  const args = [
    "--mode",
    mode,
    "--server",
    server,
    "--run-id",
    run.id,
    "--pr",
    run.target.pr_url,
    "--repository",
    repository,
    "--image",
    receipt.image_id,
    "--timeout",
    String(receipt.timeout_seconds),
    "--output",
    `.omnigent/studio/reproduction-${Date.now()}.json`,
    "--",
    ...receipt.command,
  ];
  if (args.some(hasTerminalControls)) throw new Error("The command contains terminal controls.");
  return (
    "uv run --no-sync python scripts/verify_studio.py reproduce " + args.map(shellQuote).join(" ")
  );
}

function ReproductionOutcome({ label, side }: { label: string; side: ReproductionSide }) {
  return (
    <details className="rounded-md border border-border px-2 py-1">
      <summary className="cursor-pointer text-xs">
        {label} <code title={side.commit}>{side.commit.slice(0, 8)}</code>
        {" · "}
        {side.state === "timed_out"
          ? "Timed out"
          : side.state === "setup_error"
            ? "Setup failed"
            : `Exit ${side.exit_code}`}
        {" · "}
        {side.elapsed_ms.toLocaleString()} ms
        {side.oom_killed && " · Out of memory"}
      </summary>
      <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all text-xs">
        {side.output || "No captured output"}
      </pre>
      {side.output_truncated && (
        <p className="text-xs text-muted-foreground">Output preview truncated.</p>
      )}
    </details>
  );
}

function Reproductions({
  run,
  findingKey,
  viewerId,
}: {
  run: Pick<ReviewRun, "id"> & Partial<Pick<ReviewRun, "target">>;
  findingKey: string;
  viewerId: string | null;
}) {
  const queryClient = useQueryClient();
  const queryKey = ["studio-finding-reproductions", viewerId, run.id, findingKey] as const;
  const [file, setFile] = useState<File>();
  const [copyStatus, setCopyStatus] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const receipts = useQuery({
    queryKey,
    queryFn: ({ signal }) => listFindingReproductions(run.id, findingKey, signal),
    retry: false,
    staleTime: 0,
    gcTime: run.target ? undefined : 0,
  });
  const attach = useMutation({
    mutationFn: (selected: File) => attachFindingReproduction(run.id, findingKey, selected),
    onSuccess: async () => {
      setFile(undefined);
      if (fileInput.current) fileInput.current.value = "";
      await queryClient.invalidateQueries({ queryKey });
    },
  });

  async function copyRerun(receipt: AttachedReproduction["receipt"]) {
    if (!run.target) return;
    try {
      await copyText(rerunCommand({ id: run.id, target: run.target }, receipt));
      setCopyStatus("Rerun command copied.");
    } catch {
      setCopyStatus("Couldn’t copy the rerun command.");
    }
  }

  return (
    <section aria-label="Finding reproductions" className="mt-2 space-y-2 text-sm">
      {run.target && (
        <>
          <p className="text-xs text-muted-foreground">
            Attach a receipt from the Studio reproduce command for this saved review run.
          </p>
          <form
            className="flex flex-wrap items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (file && !attach.isPending) attach.mutate(file);
            }}
          >
            <input
              ref={fileInput}
              className="max-w-full text-xs"
              type="file"
              accept=".json,application/json"
              aria-label="Reproduction receipt"
              disabled={attach.isPending}
              onChange={(event) => {
                setFile(event.target.files?.[0]);
                attach.reset();
              }}
            />
            <Button type="submit" size="sm" variant="outline" disabled={!file || attach.isPending}>
              {attach.isPending ? "Attaching…" : "Attach receipt"}
            </Button>
          </form>
          {attach.error && <p role="alert">{attach.error.message}</p>}
        </>
      )}
      {receipts.isFetching && <p role="status">Loading reproductions…</p>}
      {receipts.error && <p role="alert">{receipts.error.message}</p>}
      {receipts.isSuccess && !receipts.isFetching && receipts.data.length === 0 && (
        <p className="text-xs text-muted-foreground">No reproduction receipts attached.</p>
      )}
      {receipts.isSuccess &&
        !receipts.isFetching &&
        receipts.data.map(({ digest, receipt }) => (
          <article key={digest} className="space-y-2 rounded-md border border-border p-2">
            <div className="flex flex-wrap items-center justify-between gap-1">
              <span className="text-xs font-medium">
                Uploaded receipt · {new Date(receipt.observed_at).toLocaleString()}
              </span>
              {run.target && receipt.command && (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={receipt.command.some(hasTerminalControls)}
                  onClick={() => void copyRerun(receipt)}
                >
                  Copy rerun command
                </Button>
              )}
            </div>
            <p className="break-all font-mono text-xs" title="Sandbox image ID">
              {receipt.image_id}
            </p>
            {run.target && receipt.command?.some(hasTerminalControls) && (
              <p className="text-xs text-muted-foreground">
                This command contains terminal control characters. Re-enter it manually.
              </p>
            )}
            {run.target && receipt.command && (
              <p className="whitespace-pre-wrap break-all font-mono text-xs">
                {receipt.command.map(shellQuote).join(" ")}
              </p>
            )}
            <ReproductionOutcome label="Base" side={receipt.base} />
            <ReproductionOutcome label="Head" side={receipt.head} />
          </article>
        ))}
      {copyStatus && <p role="status">{copyStatus}</p>}
    </section>
  );
}

export function FindingReproductions(props: Parameters<typeof Reproductions>[0]) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="rounded-md border border-border px-2 py-1"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer text-sm font-medium">Reproductions</summary>
      {open && <Reproductions {...props} />}
    </details>
  );
}
