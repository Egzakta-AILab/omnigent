import { expect, it } from "vitest";
import {
  readCreatorAuthoringDraft,
  readCreatorEditorDraft,
  stageCreatorAuthoringDraft,
} from "./creatorAuthoringDraft";

it("keeps the authoring handoff scoped to its viewer, review, and navigation", () => {
  const input = {
    viewerId: "reviewer",
    reviewId: "review-1",
    returnTo: "/reviews/review-1/discussion",
    brief: "Group failures by directory",
    source: "{}",
    prompt: "Create a QA panel",
  };
  const id = stageCreatorAuthoringDraft(input);
  expect(readCreatorAuthoringDraft("reviewer", id)).toEqual({ ...input, id });
  expect(readCreatorAuthoringDraft(null, id)).toBeUndefined();
  expect(readCreatorAuthoringDraft("another-viewer", id)).toBeUndefined();
  expect(readCreatorEditorDraft("reviewer", "review-1")?.source).toBe("{}");
  expect(readCreatorEditorDraft("reviewer", "another-review")).toBeUndefined();
  expect(readCreatorEditorDraft("another-viewer", "review-1")).toBeUndefined();

  stageCreatorAuthoringDraft({ ...input, reviewId: "review-2" });
  expect(readCreatorAuthoringDraft("reviewer", id)).toBeUndefined();
  expect(readCreatorEditorDraft("reviewer", "review-1")).toBeUndefined();
});
