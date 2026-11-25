import { useState, type RefObject } from "react";
import { useMutation } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { randomUUID } from "@/lib/randomUUID";
import {
  createInlineDraft,
  confirmInlinePublication,
  discardInlineDraft,
  editInlineDraft,
  previewInlinePublication,
  type DiscussionAnchor,
  type InlineDraft,
} from "./api";
import { CheckDelivery } from "./InlinePublication";

export interface DraftAttempt {
  intent: string;
  id: string;
}

type DraftChange =
  | { type: "create"; id: string; body: string; preview?: boolean }
  | { type: "edit"; draft: InlineDraft; body: string; preview?: boolean }
  | { type: "discard"; draft: InlineDraft };

export function InlineDrafts({
  runId,
  anchor,
  drafts,
  suggestedBody,
  canEdit,
  attempt,
  refresh,
  onReviewPublication,
}: {
  runId: string;
  anchor: DiscussionAnchor;
  drafts: InlineDraft[];
  suggestedBody?: string;
  canEdit: boolean;
  attempt: RefObject<DraftAttempt | undefined>;
  refresh: () => Promise<void>;
  onReviewPublication?: () => void;
}) {
  const [editor, setEditor] = useState<{ body: string; draft?: InlineDraft }>();
  const preview = useMutation({
    mutationFn: (draft: InlineDraft) => previewInlinePublication(runId, [draft]),
  });
  const publish = useMutation({
    mutationFn: (snapshot: NonNullable<typeof preview.data>) =>
      confirmInlinePublication(runId, snapshot),
    onSettled: async () => {
      preview.reset();
      await refresh();
    },
  });
  const change = useMutation({
    mutationFn: async (action: DraftChange) => {
      if (action.type === "discard")
        return discardInlineDraft(runId, action.draft.id, action.draft.version);
      if (action.type === "edit")
        return editInlineDraft(runId, action.draft.id, action.draft.version, action.body);
      return createInlineDraft(runId, { id: action.id, anchor, body: action.body });
    },
    onSuccess: async (draft, action) => {
      if (action.type === "create" && attempt.current?.id === action.id)
        attempt.current = undefined;
      setEditor(undefined);
      await refresh();
      if (draft && action.type !== "discard" && action.preview) preview.mutate(draft);
    },
  });
  const pending = change.isPending || preview.isPending || publish.isPending;
  const snapshot = preview.data;
  const selected = snapshot?.drafts[0];
  const current =
    selected &&
    drafts.some(
      (draft) =>
        draft.id === selected.id &&
        draft.version === selected.version &&
        (draft.publication_state ?? "draft") === "draft",
    );
  if (!canEdit && !drafts.length) return null;
  return (
    <section aria-label="Your inline drafts" className="space-y-2 rounded-md border p-2">
      {drafts.length > 0 && <p className="text-xs font-medium">PR comments for this finding</p>}
      {drafts.map((draft) => (
        <article key={draft.id} className="space-y-2 rounded-md border p-2 text-sm">
          <p className="break-all text-xs text-muted-foreground">
            {draft.anchor.path}:{draft.anchor.start}
            {draft.anchor.end !== draft.anchor.start && `–${draft.anchor.end}`}
            {" · "}
            {draft.anchor.side === "base" ? "Base" : "Head"}
          </p>
          <p className="whitespace-pre-wrap break-words">{draft.body}</p>
          {draft.publication_state === "posted" && draft.provider_url && (
            <a
              className="text-xs underline"
              href={draft.provider_url}
              target="_blank"
              rel="noreferrer"
            >
              Open published comment
            </a>
          )}
          {draft.publication_state === "sending" && (
            <p role="status" className="text-xs">
              Sending comment…
            </p>
          )}
          {draft.publication_state === "unknown" && (
            <div className="space-y-2">
              <p role="status" className="text-xs">
                Delivery unconfirmed. Check the pull request for this comment.
              </p>
              {onReviewPublication && draft.publication_reference && (
                <CheckDelivery runId={runId} draft={draft} refresh={refresh} />
              )}
            </div>
          )}
          {canEdit && (!draft.publication_state || draft.publication_state === "draft") && (
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={pending || !!editor || !!snapshot}
                onClick={() => {
                  change.reset();
                  setEditor({ draft, body: draft.body });
                }}
              >
                Edit draft
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={pending || !!editor || !!snapshot}
                onClick={() => change.mutate({ type: "discard", draft })}
              >
                Discard draft
              </Button>
              {onReviewPublication && (
                <Button
                  size="sm"
                  disabled={pending || !!editor || !!snapshot}
                  onClick={() => preview.mutate(draft)}
                >
                  Review and send
                </Button>
              )}
            </div>
          )}
        </article>
      ))}
      {canEdit &&
        (editor ? (
          <form
            className="space-y-2"
            onSubmit={(event) => {
              event.preventDefault();
              const body = editor.body.trim();
              if (!body || pending) return;
              const submitter = (event.nativeEvent as SubmitEvent).submitter;
              const send = submitter?.getAttribute("value") === "preview";
              if (editor.draft) {
                change.mutate({ type: "edit", draft: editor.draft, body, preview: send });
                return;
              }
              const intent = JSON.stringify([anchor, body]);
              if (attempt.current?.intent !== intent)
                attempt.current = { intent, id: randomUUID() };
              change.mutate({ type: "create", id: attempt.current.id, body, preview: send });
            }}
          >
            <textarea
              aria-label="Inline comment draft"
              className="min-h-20 w-full rounded-md border bg-background p-2 text-sm"
              maxLength={2000}
              value={editor.body}
              disabled={change.isPending}
              onChange={(event) => setEditor({ ...editor, body: event.target.value })}
            />
            <div className="flex gap-2">
              {onReviewPublication && (
                <Button
                  type="submit"
                  size="sm"
                  value="preview"
                  disabled={!editor.body.trim() || pending}
                >
                  Review and send
                </Button>
              )}
              <Button
                type="submit"
                size="sm"
                variant={onReviewPublication ? "outline" : "default"}
                disabled={!editor.body.trim() || pending}
              >
                {onReviewPublication ? "Save for batch" : "Save draft"}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                disabled={change.isPending}
                onClick={() => {
                  change.reset();
                  setEditor(undefined);
                }}
              >
                Cancel
              </Button>
            </div>
          </form>
        ) : (
          <Button
            size="sm"
            variant="outline"
            disabled={pending || !!snapshot}
            onClick={() => {
              change.reset();
              setEditor({ body: suggestedBody ?? "" });
            }}
          >
            Comment on PR
          </Button>
        ))}
      {onReviewPublication &&
        drafts.some((draft) => (draft.publication_state ?? "draft") === "draft") && (
          <Button size="sm" variant="outline" onClick={onReviewPublication}>
            Review batch
          </Button>
        )}
      {onReviewPublication && snapshot && selected && (
        <div className="space-y-2 rounded-md border p-3" aria-label="Confirm PR comment">
          <a
            href={snapshot.pr_url}
            target="_blank"
            rel="noreferrer"
            className="break-all text-sm underline"
          >
            {snapshot.provider} pull request
          </a>
          <p className="break-all text-xs text-muted-foreground">
            {selected.anchor.path}:{selected.anchor.start}
            {selected.anchor.end !== selected.anchor.start ? `–${selected.anchor.end}` : ""}
            {` · ${selected.anchor.side} · ${snapshot.head_sha.slice(0, 8)}`}
          </p>
          <p className="whitespace-pre-wrap break-words text-sm">{selected.body}</p>
          {!current && (
            <p role="alert" className="text-sm">
              The draft changed. Open a new preview to send it.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              disabled={pending || !current}
              onClick={() => publish.mutate(snapshot)}
            >
              {publish.isPending ? "Sending…" : "Send comment"}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={publish.isPending}
              onClick={() => preview.reset()}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}
      {(publish.error || preview.error || change.error) && (
        <p className="text-sm" role="alert">
          {(publish.error || preview.error || change.error)?.message}
        </p>
      )}
    </section>
  );
}
