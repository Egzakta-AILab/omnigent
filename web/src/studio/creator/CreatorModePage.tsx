import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useIdentityReady, useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { triggerBrowserDownload } from "@/hooks/useFileContent";
import { copyText } from "@/lib/clipboard";
import { readCreatorEditorDraft, stageCreatorAuthoringDraft } from "@/lib/creatorAuthoringDraft";
import { Link, useLocation, useNavigate, useSearchParams } from "@/lib/routing";
import {
  buildCreatorPanelPreview,
  getReviewDiscussion,
  getCreatorPanelContract,
} from "../reviews/api";
import {
  CreatorInstallationControls,
  useCreatorInstallation,
} from "../reviews/CreatorInstallationControls";
import { CreatorPanelPreview } from "../reviews/CreatorPanelPreview";

const MAX_PANEL_BYTES = 8 * 1024;
const DEFAULT_BRIEF =
  "Add a QA panel that groups failed checks by file. Clicking a failure opens its proof and includes the receipt in an agent question.";

const EDITOR_ID = "review-panel";
const REVIEW_ID = /^(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$/i;

export function CreatorModePage() {
  const viewerId = useViewerId();
  const identityReady = useIdentityReady();
  const info = useServerInfo();
  if (!identityReady || info === "loading") return <p className="p-6">Loading Creator Mode…</p>;
  if (!info.studio_creator) return <p className="p-6">Creator Mode is unavailable.</p>;
  return <CreatorEditor key={viewerId} viewerId={viewerId} />;
}

function CreatorEditor({ viewerId }: { viewerId: string | null }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const reviewId = params.get("review");
  const [previewInput, setPreviewInput] = useState(reviewId ?? "");
  const [inputError, setInputError] = useState("");
  const [restored] = useState(() => readCreatorEditorDraft(viewerId, EDITOR_ID));
  const [brief, setBrief] = useState(restored?.brief ?? DEFAULT_BRIEF);
  const [source, setSource] = useState(restored?.source ?? "");
  const [status, setStatus] = useState("");
  const contract = useQuery({
    queryKey: ["studio-creator-contract", viewerId],
    queryFn: ({ signal }) => getCreatorPanelContract(signal),
    retry: false,
  });
  const previewReview = useQuery({
    queryKey: ["studio-creator-preview-review", viewerId, reviewId],
    queryFn: ({ signal }) => getReviewDiscussion(reviewId!, signal),
    enabled: !!reviewId && REVIEW_ID.test(reviewId),
    retry: false,
    gcTime: 0,
  });
  const discussion =
    previewReview.isSuccess && previewReview.data.creator_available ? previewReview.data : null;
  const build = useMutation({
    mutationFn: (definition: string) => buildCreatorPanelPreview(discussion!.id, definition),
  });
  const installationState = useCreatorInstallation(discussion?.id ?? null, viewerId, build.reset);
  const preview = build.data;
  const current =
    contract.isSuccess &&
    discussion &&
    preview &&
    build.variables === source &&
    preview.run_id === discussion.id &&
    preview.base_sha === discussion.base_sha &&
    preview.head_sha === discussion.head_sha;

  function selectPreview() {
    const value = previewInput.trim();
    const pathId = value.match(/\/reviews\/([^/?#]+)\/discussion(?:[?#].*)?$/)?.[1];
    const id = pathId ?? value;
    if (!REVIEW_ID.test(id)) {
      setInputError("Paste a saved review’s discussion link or run ID.");
      return;
    }
    setInputError("");
    build.reset();
    setParams({ review: id });
  }

  function authoringPrompt() {
    if (!contract.data || !brief.trim()) return;
    return [
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
  }

  async function copyPrompt() {
    const prompt = authoringPrompt();
    if (!prompt) return;
    try {
      await copyText(prompt);
      setStatus("Authoring prompt copied. Paste it into your chosen agent session.");
    } catch {
      setStatus("Could not copy the prompt. Check clipboard access and try again.");
    }
  }

  function openAuthoringDraft() {
    const prompt = authoringPrompt();
    if (!viewerId || !prompt) return;
    const creatorAuthoringId = stageCreatorAuthoringDraft({
      viewerId,
      editorId: EDITOR_ID,
      returnTo: location.pathname + location.search + location.hash,
      brief,
      source,
      prompt,
    });
    navigate("/", { state: { creatorAuthoringId } });
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
    <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-8 pt-16 sm:px-7">
      <main className="mx-auto max-w-3xl space-y-5 text-sm">
        <header className="space-y-2">
          <h1 className="text-xl font-semibold">Creator Mode</h1>
          <p className="text-muted-foreground">Build extensions for your Omnigent workspace.</p>
        </header>
        <section
          aria-labelledby="creator-panel-heading"
          className="space-y-4 rounded-md border p-4"
        >
          <h2 id="creator-panel-heading" className="font-medium">
            Review panel
          </h2>
          <p className="text-muted-foreground">
            Describe a panel and draft its definition with an agent. Choose a saved review when
            you’re ready to preview with real data or install it in that project.
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
                  onClick={openAuthoringDraft}
                  disabled={!viewerId || !brief.trim()}
                >
                  Open authoring draft
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void copyPrompt()}
                  disabled={!brief.trim()}
                >
                  Copy authoring prompt
                </Button>
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
                  if (discussion && source.trim() && !build.isPending) validate();
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
                  <Button
                    type="submit"
                    size="sm"
                    disabled={!discussion || !source.trim() || build.isPending}
                  >
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
          <form
            className="space-y-2"
            onSubmit={(event) => {
              event.preventDefault();
              selectPreview();
            }}
          >
            <label className="grid gap-1">
              <span className="font-medium">Saved review for preview</span>
              <Input
                placeholder="Paste a review discussion link"
                value={previewInput}
                onChange={(event) => setPreviewInput(event.target.value)}
              />
            </label>
            <Button type="submit" size="sm" variant="outline" disabled={!previewInput.trim()}>
              Use saved review
            </Button>
            {inputError && <p role="alert">{inputError}</p>}
            {reviewId && previewReview.isFetching && <p role="status">Loading saved review…</p>}
            {previewReview.isError && <p role="alert">{previewReview.error.message}</p>}
            {previewReview.isSuccess && !discussion && (
              <p role="alert">This review is unavailable for Creator Mode previews.</p>
            )}
            {discussion && (
              <p className="text-muted-foreground">
                Previewing {discussion.repository} #{discussion.pr_number} ·{" "}
                <code>{discussion.head_sha.slice(0, 8)}</code>{" "}
                <Link
                  className="underline"
                  to={`/reviews/${encodeURIComponent(discussion.id)}/discussion`}
                >
                  Open review
                </Link>
              </p>
            )}
          </form>
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
            />
          )}
          {discussion && (
            <CreatorInstallationControls
              key={discussion.id}
              state={installationState}
              preview={current ? preview : undefined}
              onEdit={setSource}
            />
          )}
        </section>
      </main>
    </div>
  );
}
