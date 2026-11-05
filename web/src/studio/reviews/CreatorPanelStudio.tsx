import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { triggerBrowserDownload } from "@/hooks/useFileContent";
import { copyText } from "@/lib/clipboard";
import { Link } from "@/lib/routing";
import { buildCreatorPanelPreview, getCreatorPanelContract, type ReviewDiscussion } from "./api";
import { CreatorPanelPreview, type ShowCreatorProof } from "./CreatorPanelPreview";

const MAX_PANEL_BYTES = 8 * 1024;
const DEFAULT_BRIEF =
  "Add a QA panel that groups failed checks by file. Clicking a failure opens its proof and includes the receipt in an agent question.";

export function CreatorPanelStudio({
  discussion,
  viewerId,
  onProof,
}: {
  discussion: ReviewDiscussion;
  viewerId: string | null;
  onProof: ShowCreatorProof;
}) {
  const [open, setOpen] = useState(false);
  const [brief, setBrief] = useState(DEFAULT_BRIEF);
  const [source, setSource] = useState("");
  const [status, setStatus] = useState("");
  const contract = useQuery({
    queryKey: ["studio-creator-contract", viewerId, discussion.id],
    queryFn: ({ signal }) => getCreatorPanelContract(discussion.id, signal),
    enabled: open,
    retry: false,
    gcTime: 0,
  });
  const build = useMutation({
    mutationFn: (definition: string) => buildCreatorPanelPreview(discussion.id, definition),
  });
  const preview = build.data;
  const current =
    contract.isSuccess &&
    preview &&
    build.variables === source &&
    preview.run_id === discussion.id &&
    preview.base_sha === discussion.base_sha &&
    preview.head_sha === discussion.head_sha;

  async function copyPrompt() {
    if (!contract.data) return;
    const prompt = [
      "You are creating an Omnigent Creator Mode review panel.",
      "Return exactly one JSON definition matching the installed schema, without a code fence.",
      "Use only the declared settings. The native host supplies review data and performs proof actions.",
      "This task needs no repository files, credentials, commands, or external services.",
      `Requested behavior: ${brief.trim()}`,
      `Installed schema:\n${JSON.stringify(contract.data.definition_schema, null, 2)}`,
      `Starting template:\n${JSON.stringify(contract.data.template, null, 2)}`,
      source.trim() ? `Current draft to revise:\n${source}` : "",
      build.error && build.variables === source ? `Build diagnostic:\n${build.error.message}` : "",
    ]
      .filter(Boolean)
      .join("\n\n");
    try {
      await copyText(prompt);
      setStatus("Authoring prompt copied. Paste it into your chosen agent session.");
    } catch {
      setStatus("Could not copy the prompt. Check clipboard access and try again.");
    }
  }

  function validate() {
    if (new TextEncoder().encode(source).length > MAX_PANEL_BYTES) {
      setStatus("Panel definitions must be at most 8 KiB.");
      return;
    }
    setStatus("");
    build.mutate(source);
  }

  return (
    <details className="rounded-md border" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="cursor-pointer px-3 py-2 text-sm font-medium">Creator Mode</summary>
      {open && (
        <div className="space-y-4 border-t p-3 text-sm">
          <p className="text-muted-foreground">
            Describe a review panel, generate a definition with your agent, and preview it against
            saved revision <code>{discussion.head_sha.slice(0, 8)}</code>.
          </p>
          {contract.isPending && <p role="status">Loading the installed contract…</p>}
          {contract.error && (
            <div className="flex flex-wrap items-center gap-2">
              <p role="alert">{contract.error.message}</p>
              <Button variant="outline" size="sm" onClick={() => void contract.refetch()}>
                Retry contract
              </Button>
            </div>
          )}
          {contract.isSuccess && (
            <>
              <label className="grid gap-1">
                <span className="font-medium">Describe the panel</span>
                <textarea
                  className="min-h-16 w-full rounded-md border bg-background p-2"
                  value={brief}
                  maxLength={2000}
                  onChange={(event) => setBrief(event.target.value)}
                />
              </label>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void copyPrompt()}
                  disabled={!brief.trim()}
                >
                  Copy authoring prompt
                </Button>
                <Link className="underline" to="/" target="_blank" rel="noreferrer">
                  Open agent composer
                </Link>
              </div>
              <p className="text-xs text-muted-foreground">
                Choose a local sandbox or a remote workspace for the authoring session. The prompt
                contains your brief, the schema, and any definition you entered here.
              </p>
              <details className="rounded-md border p-2">
                <summary className="cursor-pointer">Installed definition schema</summary>
                <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words text-xs">
                  {JSON.stringify(contract.data.definition_schema, null, 2)}
                </pre>
              </details>
              <form
                className="space-y-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (source.trim() && !build.isPending) validate();
                }}
              >
                <label className="grid gap-1">
                  <span className="font-medium">Panel definition (JSON)</span>
                  <textarea
                    spellCheck={false}
                    className="min-h-40 w-full rounded-md border bg-background p-2 font-mono text-xs"
                    placeholder="Paste the agent’s JSON definition."
                    value={source}
                    maxLength={MAX_PANEL_BYTES}
                    onChange={(event) => setSource(event.target.value)}
                  />
                </label>
                <div className="flex flex-wrap gap-2">
                  <Button type="submit" size="sm" disabled={!source.trim() || build.isPending}>
                    {build.isPending ? "Validating…" : "Build preview"}
                  </Button>
                  {!source && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => setSource(JSON.stringify(contract.data.template, null, 2))}
                    >
                      Load QA template
                    </Button>
                  )}
                  {current && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        triggerBrowserDownload(
                          new Blob([JSON.stringify(preview.panel, null, 2) + "\n"], {
                            type: "application/json",
                          }),
                          "review-panel.json",
                        )
                      }
                    >
                      Download definition
                    </Button>
                  )}
                </div>
              </form>
            </>
          )}
          {status && <p role="status">{status}</p>}
          {build.error && build.variables === source && (
            <p role="alert" className="break-words">
              {build.error.message}
            </p>
          )}
          {preview && !current && (
            <p role="status">Preview is out of date. Build again to apply the changes.</p>
          )}
          {current && (
            <CreatorPanelPreview
              key={preview.digest}
              panel={preview.panel}
              discussion={discussion}
              viewerId={viewerId}
              onProof={onProof}
            />
          )}
        </div>
      )}
    </details>
  );
}
