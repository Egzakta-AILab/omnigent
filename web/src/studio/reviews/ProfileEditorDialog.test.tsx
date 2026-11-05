import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import type * as IdentityModule from "@/lib/identity";
import { ProfileEditorDialog } from "./ProfileEditorDialog";
import type { ReviewProfileDraft } from "./api";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "owner" }));

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

it("duplicates a linked profile and preserves the draft until a changed catalog is reconciled", async () => {
  const endpoint = "/v1/projects/project/studio/review-profiles/editor";
  const linked = {
    id: "security",
    label: "Security",
    description: "Inspect trust boundaries",
    instructions: "Check **authorization**.",
    source: "review/security.md",
    managed: false,
    archived: false,
  };
  let revision = "workspace:one";
  const writes: { revision: string; profile: ReviewProfileDraft }[] = [];
  const profiles = [linked];
  vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
    expect(String(input)).toBe(endpoint);
    if (init?.method === "PUT") {
      const change = JSON.parse(String(init.body)) as (typeof writes)[number];
      writes.push(change);
      if (change.revision !== revision) {
        return Response.json({ detail: "The profile catalog changed" }, { status: 409 });
      }
      revision = "workspace:three";
      const saved = {
        ...change.profile,
        source: ".omnigent-review-profiles.json",
        managed: true,
      };
      const index = profiles.findIndex((profile) => profile.id === saved.id);
      if (index < 0) profiles.push(saved);
      else profiles[index] = saved;
    }
    return Response.json({ revision, profiles });
  });

  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={cache}>
      <ProfileEditorDialog open onOpenChange={() => {}} endpoint={endpoint} />
    </QueryClientProvider>,
  );
  const user = userEvent.setup();
  expect(await screen.findByRole("textbox", { name: "Instructions" })).toHaveAttribute("readonly");
  await user.click(screen.getByRole("button", { name: "Preview" }));
  expect(screen.getByText("authorization", { selector: "strong" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Duplicate" }));
  const id = screen.getByRole("textbox", { name: /Profile ID/ });
  await user.type(id, "security");
  expect(screen.getByRole("button", { name: "Save profile" })).toBeDisabled();
  await user.clear(id);
  await user.type(id, "security-copy");
  const instructions = screen.getByRole("textbox", { name: "Instructions" });
  await user.type(instructions, " Verify tenants.");
  revision = "workspace:two";
  await user.click(screen.getByRole("button", { name: "Save profile" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("The profile catalog changed");
  expect(instructions).toHaveValue("Check **authorization**. Verify tenants.");
  await user.click(screen.getByRole("button", { name: "Reload profiles" }));
  await user.click(await screen.findByRole("button", { name: "Copy draft to new profile" }));
  await user.type(screen.getByRole("textbox", { name: /Profile ID/ }), "team-security");
  await user.click(screen.getByRole("button", { name: "Save profile" }));
  await waitFor(() => expect(screen.getByText("Profile saved")).toBeInTheDocument());
  expect(writes[1]).toEqual({
    revision: "workspace:two",
    profile: {
      id: "team-security",
      label: "Security copy copy",
      description: "Inspect trust boundaries",
      instructions: "Check **authorization**. Verify tenants.",
      archived: false,
    },
  });
  expect(profiles[0]).toEqual(linked);
  await user.click(screen.getByRole("button", { name: "Archive" }));
  expect(writes).toHaveLength(2);
  await user.click(screen.getByRole("button", { name: "Save profile" }));
  await waitFor(() => expect(writes[2]?.profile.archived).toBe(true));
  await screen.findByText("Archived", { exact: true });
  await user.click(screen.getByRole("button", { name: "Restore" }));
  await user.click(screen.getByRole("button", { name: "Save profile" }));
  await waitFor(() => expect(writes[3]?.profile.archived).toBe(false));
  view.unmount();
  cache.clear();
});
