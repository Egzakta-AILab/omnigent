import { useState, type RefObject } from "react";
import { useMutation } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { randomUUID } from "@/lib/randomUUID";
import {
  createInlineDraft,
  discardInlineDraft,
  editInlineDraft,
  type DiscussionAnchor,
  type InlineDraft,
} from "./api";

export interface DraftAttempt {
  intent: string;
  id: string;
}

type DraftChange =
  | { type: "create"; id: string; body: string }
  | { type: "edit"; draft: InlineDraft; body: string }
  | { type: "discard"; draft: InlineDraft };

export function InlineDrafts({
  runId,
  anchor,
  drafts,
  suggestedBody,
  canEdit,
  attempt,
  refresh,
}: {
  runId: string;
  anchor: DiscussionAnchor;
  drafts: InlineDraft[];
  suggestedBody?: string;
  canEdit: boolean;
  attempt: RefObject<DraftAttempt | undefined>;
  refresh: () => Promise<void>;
}) {
  const [editor, setEditor] = useState<{ body: string; draft?: InlineDraft }>();
  const change = useMutation({
    mutationFn: async (action: DraftChange) => {
      if (action.type === "discard")
        return discardInlineDraft(runId, action.draft.id, action.draft.version);
      if (action.type === "edit")
        return editInlineDraft(runId, action.draft.id, action.draft.version, action.body);
      return createInlineDraft(runId, { id: action.id, anchor, body: action.body });
    },
    onSuccess: async (_, action) => {
      if (action.type === "create" && attempt.current?.id === action.id)
        attempt.current = undefined;
      setEditor(undefined);
      await refresh();
    },
  });
  if (!canEdit && !drafts.length) return null;
  return (
    <section aria-label="Private inline drafts" className="space-y-2 rounded-md border p-2">
      <p className="text-xs font-medium">Private inline drafts</p>
      {drafts.map((draft) => (
        <article key={draft.id} className="space-y-2 rounded-md border p-2 text-sm">
          <p className="break-all text-xs text-muted-foreground">
            {draft.anchor.path}:{draft.anchor.start}
            {draft.anchor.end !== draft.anchor.start && `–${draft.anchor.end}`}
            {" · "}
            {draft.anchor.side === "base" ? "Base" : "Head"}
          </p>
          <p className="whitespace-pre-wrap break-words">{draft.body}</p>
          {canEdit && (!draft.publication_state || draft.publication_state === "draft") && (
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={change.isPending || !!editor}
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
                disabled={change.isPending || !!editor}
                onClick={() => change.mutate({ type: "discard", draft })}
              >
                Discard draft
              </Button>
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
              if (!body || change.isPending) return;
              if (editor.draft) {
                change.mutate({ type: "edit", draft: editor.draft, body });
                return;
              }
              const intent = JSON.stringify([anchor, body]);
              if (attempt.current?.intent !== intent)
                attempt.current = { intent, id: randomUUID() };
              change.mutate({ type: "create", id: attempt.current.id, body });
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
              <Button type="submit" size="sm" disabled={!editor.body.trim() || change.isPending}>
                Save draft
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
            disabled={change.isPending}
            onClick={() => {
              change.reset();
              setEditor({ body: suggestedBody ?? "" });
            }}
          >
            Draft inline comment
          </Button>
        ))}
      {change.error && (
        <p className="text-sm" role="alert">
          {change.error.message}
        </p>
      )}
    </section>
  );
}
