import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { triggerBrowserDownload } from "@/hooks/useFileContent";
import {
  confirmInlinePublication,
  previewInlinePublication,
  reconcileInlinePublication,
  type InlineDraft,
  type InlinePublicationPreview,
} from "./api";

const MAX_SELECTED = 8;

function anchorLabel({ path, start, end, side }: InlineDraft["anchor"]) {
  return `${path}:${start}${start === end ? "" : `–${end}`} · ${side === "base" ? "Base" : "Head"}`;
}

function CheckDelivery({
  runId,
  draft,
  refresh,
}: {
  runId: string;
  draft: InlineDraft;
  refresh: () => Promise<void>;
}) {
  const [url, setUrl] = useState("");
  const check = useMutation({
    mutationFn: () => reconcileInlinePublication(runId, draft, url.trim()),
    onSuccess: refresh,
  });
  return (
    <form
      className="space-y-1"
      onSubmit={(event) => {
        event.preventDefault();
        if (url.trim() && !check.isPending) check.mutate();
      }}
    >
      <label className="grid gap-1 text-xs">
        GitHub inline comment URL
        <input
          className="rounded-md border bg-background p-1"
          type="url"
          value={url}
          maxLength={2048}
          onChange={(event) => setUrl(event.target.value)}
        />
      </label>
      <Button size="sm" variant="outline" disabled={!url.trim() || check.isPending}>
        {check.isPending ? "Checking…" : "Check delivery"}
      </Button>
      {check.error && <p role="alert">{check.error.message}</p>}
    </form>
  );
}

export function InlinePublication({
  runId,
  drafts,
  refresh,
}: {
  runId: string;
  drafts: InlineDraft[];
  refresh: () => Promise<void>;
}) {
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const selected = drafts.filter(
    (draft) =>
      (!draft.publication_state || draft.publication_state === "draft") &&
      selectedIds.includes(draft.id),
  );
  const preview = useMutation({
    mutationFn: () => previewInlinePublication(runId, selected),
  });
  const confirm = useMutation({
    mutationFn: (result: InlinePublicationPreview) => confirmInlinePublication(runId, result),
    onSuccess: async () => {
      await refresh();
      setSelectedIds([]);
      preview.reset();
    },
    onError: refresh,
  });
  const pending = preview.isPending || confirm.isPending;
  const snapshot = preview.data;
  const error = confirm.error ?? preview.error;
  const stale = snapshot?.drafts.some(
    (draft) => !selected.some(({ id, version }) => id === draft.id && version === draft.version),
  );

  return (
    <section aria-label="Publish inline drafts" className="space-y-3 rounded-md border p-3">
      <div>
        <h2 className="font-medium">Publish inline drafts</h2>
        <p className="text-sm text-muted-foreground">
          Select up to {MAX_SELECTED} drafts. Each is posted as a separate line comment using the
          review workspace’s configured account.
        </p>
      </div>
      <ul className="divide-y text-sm">
        {drafts.map((draft) => {
          const state = draft.publication_state ?? "draft";
          const checked = selectedIds.includes(draft.id);
          return (
            <li key={draft.id} className="flex min-w-0 items-start gap-2 py-2">
              {state === "draft" ? (
                <label className="flex min-w-0 flex-1 items-start gap-2">
                  <input
                    className="mt-1"
                    type="checkbox"
                    checked={checked}
                    disabled={pending || (!checked && selected.length >= MAX_SELECTED)}
                    onChange={(event) => {
                      const next = event.currentTarget.checked;
                      preview.reset();
                      confirm.reset();
                      setSelectedIds(
                        next
                          ? [...selectedIds, draft.id]
                          : selectedIds.filter((id) => id !== draft.id),
                      );
                    }}
                  />
                  <span className="min-w-0">
                    <span className="block break-all text-xs text-muted-foreground">
                      {anchorLabel(draft.anchor)}
                    </span>
                    <span className="line-clamp-2 break-words">{draft.body}</span>
                  </span>
                </label>
              ) : (
                <div className="min-w-0">
                  <p className="break-all text-xs text-muted-foreground">
                    {anchorLabel(draft.anchor)}
                  </p>
                  {state === "posted" && draft.provider_url ? (
                    <a
                      className="underline"
                      href={draft.provider_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Open published comment
                    </a>
                  ) : (
                    <>
                      <p>Delivery unconfirmed. Check the pull request for this comment.</p>
                      {draft.publication_reference && (
                        <CheckDelivery runId={runId} draft={draft} refresh={refresh} />
                      )}
                    </>
                  )}
                  {draft.publication_confirmed_by && (
                    <p className="break-words text-xs text-muted-foreground">
                      Confirmed by {draft.publication_confirmed_by}
                    </p>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {snapshot && !stale ? (
        <div className="space-y-3 rounded-md border p-3">
          <div>
            <h3 className="font-medium">Publication preview</h3>
            <a
              className="break-all text-sm underline"
              href={snapshot.pr_url}
              target="_blank"
              rel="noreferrer"
            >
              {snapshot.pr_url}
            </a>
            <p
              className="break-all font-mono text-xs text-muted-foreground"
              title={`${snapshot.base_sha} → ${snapshot.head_sha}`}
            >
              {snapshot.base_sha.slice(0, 12)} → {snapshot.head_sha.slice(0, 12)}
            </p>
          </div>
          <ol className="space-y-2">
            {snapshot.drafts.map((draft) => (
              <li key={draft.id} className="rounded-md border p-2 text-sm">
                <p className="break-all text-xs text-muted-foreground">
                  {anchorLabel(draft.anchor)}
                </p>
                <p className="whitespace-pre-wrap break-words">{draft.body}</p>
              </li>
            ))}
          </ol>
          <p className="text-xs text-muted-foreground">
            Studio adds a delivery reference in the comment source so you can check it if
            confirmation is interrupted.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={pending} onClick={() => confirm.mutate(snapshot)}>
              {confirm.isPending
                ? "Publishing…"
                : `Publish ${snapshot.drafts.length} inline ${snapshot.drafts.length === 1 ? "comment" : "comments"}`}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={pending}
              onClick={() =>
                triggerBrowserDownload(
                  new Blob([JSON.stringify(snapshot, null, 2)], { type: "application/json" }),
                  `publication-preview-${snapshot.digest.slice(0, 8)}.json`,
                )
              }
            >
              Download preview
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={pending}
              onClick={() => {
                preview.reset();
                confirm.reset();
              }}
            >
              Back to selection
            </Button>
          </div>
        </div>
      ) : (
        <Button
          size="sm"
          variant="outline"
          disabled={pending || !selected.length}
          onClick={() => {
            confirm.reset();
            preview.mutate();
          }}
        >
          {preview.isPending ? "Checking pull request…" : "Preview selected"}
        </Button>
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error.message}
        </p>
      )}
    </section>
  );
}
