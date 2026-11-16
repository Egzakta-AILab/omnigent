import { expect, it } from "vitest";
import {
  readCreatorAuthoringDraft,
  readCreatorEditorDraft,
  stageCreatorAuthoringDraft,
} from "./creatorAuthoringDraft";

it("keeps the authoring handoff scoped to its viewer, editor, and navigation", () => {
  const input = {
    viewerId: "reviewer",
    editorId: "review-panel",
    returnTo: "/creator",
    brief: "Group failures by directory",
    source: "{}",
    prompt: "Create a QA panel",
  };
  const id = stageCreatorAuthoringDraft(input);
  expect(readCreatorAuthoringDraft("reviewer", id)).toEqual({ ...input, id });
  expect(readCreatorAuthoringDraft(null, id)).toBeUndefined();
  expect(readCreatorAuthoringDraft("another-viewer", id)).toBeUndefined();
  expect(readCreatorEditorDraft("reviewer", "review-panel")?.source).toBe("{}");
  expect(readCreatorEditorDraft("reviewer", "another-editor")).toBeUndefined();
  expect(readCreatorEditorDraft("another-viewer", "review-panel")).toBeUndefined();

  stageCreatorAuthoringDraft({ ...input, editorId: "another-editor" });
  expect(readCreatorAuthoringDraft("reviewer", id)).toBeUndefined();
  expect(readCreatorEditorDraft("reviewer", "review-panel")).toBeUndefined();
});
