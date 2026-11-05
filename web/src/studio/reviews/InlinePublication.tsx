import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  confirmInlinePublication,
  previewInlinePublication,
  type InlineDraft,
  type InlinePublicationPreview,
} from "./api";

const MAX_SELECTED = 8;

function anchorLabel({ path, start, end, side }: InlineDraft["anchor"]) {
  return `${path}:${start}${start === end ? "" : `–${end}`} · ${side === "base" ? "Base" : "Head"}`;
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
                    <p>Delivery unconfirmed. Check the pull request for this comment.</p>
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
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={pending} onClick={() => confirm.mutate(snapshot)}>
              {confirm.isPending
                ? "Publishing…"
                : `Publish ${snapshot.drafts.length} inline ${snapshot.drafts.length === 1 ? "comment" : "comments"}`}
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
