import { randomUUID } from "@/lib/randomUUID";

interface CreatorAuthoringDraft {
  id: string;
  viewerId: string;
  reviewId: string;
  returnTo: string;
  brief: string;
  source: string;
  prompt: string;
}

// Only the opaque ID goes into navigation state. The brief and definition
// stay in this window's memory and are discarded on reload.
let draft: CreatorAuthoringDraft | undefined;

export function stageCreatorAuthoringDraft(input: Omit<CreatorAuthoringDraft, "id">): string {
  draft = { ...input, id: randomUUID() };
  return draft.id;
}

export function readCreatorAuthoringDraft(viewerId: string | null, id: unknown) {
  return draft?.viewerId === viewerId && draft.id === id ? draft : undefined;
}

export function readCreatorEditorDraft(viewerId: string | null, reviewId: string) {
  return draft?.viewerId === viewerId && draft.reviewId === reviewId ? draft : undefined;
}
