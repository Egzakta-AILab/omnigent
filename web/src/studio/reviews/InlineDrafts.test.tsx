import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import {
  confirmInlinePublication,
  createInlineDraft,
  previewInlinePublication,
  type InlineDraft,
} from "./api";
import { InlineDrafts, type DraftAttempt } from "./InlineDrafts";

vi.mock("./api", () => ({
  createInlineDraft: vi.fn(),
  discardInlineDraft: vi.fn(),
  editInlineDraft: vi.fn(),
  previewInlinePublication: vi.fn(),
  confirmInlinePublication: vi.fn(),
  reconcileInlinePublication: vi.fn(),
}));

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

it("previews one line comment in place, then shows uncertain delivery after confirmation", async () => {
  const user = userEvent.setup();
  const anchor = { kind: "diff", path: "app.py", side: "head", start: 8, end: 8 } as const;
  const draft: InlineDraft = {
    id: "one",
    thread: anchor,
    anchor,
    body: "Could this path skip authorization?",
    version: 1,
    created_at: 0,
    updated_at: 0,
    publication_state: "draft",
  };
  const snapshot = {
    provider: "github",
    pr_url: "https://github.com/team/app/pull/1",
    base_sha: "a".repeat(40),
    head_sha: "b".repeat(40),
    digest: "digest",
    drafts: [draft],
  };
  let saved: InlineDraft[] = [];
  vi.mocked(createInlineDraft).mockImplementation(async () => {
    saved = [draft];
    return draft;
  });
  vi.mocked(previewInlinePublication).mockResolvedValue(snapshot);
  vi.mocked(confirmInlinePublication).mockImplementation(async () => {
    saved = [{ ...draft, publication_state: "unknown", publication_reference: "delivery" }];
    return { ...snapshot, drafts: saved };
  });

  function Fixture() {
    const [drafts, setDrafts] = useState<InlineDraft[]>([]);
    return (
      <InlineDrafts
        runId="run"
        anchor={anchor}
        drafts={drafts}
        suggestedBody={draft.body}
        canEdit
        attempt={useRef<DraftAttempt | undefined>(undefined)}
        refresh={async () => {
          setDrafts(saved);
        }}
        onReviewPublication={() => {}}
      />
    );
  }

  render(
    <QueryClientProvider client={new QueryClient()}>
      <Fixture />
    </QueryClientProvider>,
  );
  await user.click(screen.getByRole("button", { name: "Comment on PR" }));
  await user.click(screen.getByRole("button", { name: "Review and send" }));
  const send = await screen.findByRole("button", { name: "Send comment" });
  expect(previewInlinePublication).toHaveBeenCalledWith("run", [draft]);
  expect(confirmInlinePublication).not.toHaveBeenCalled();
  await user.click(send);
  expect(confirmInlinePublication).toHaveBeenCalledWith("run", snapshot);
  expect(await screen.findByText(/Delivery unconfirmed/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Check delivery" })).toBeVisible();
});
