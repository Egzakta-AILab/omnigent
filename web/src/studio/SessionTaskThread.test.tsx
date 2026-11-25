import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { FileMentionMenu } from "@/components/FileMentionMenu";
import { studioThreadContext } from "./SessionTaskThread";

describe("Studio human thread context", () => {
  it("names a stable per-session thread without exposing the session ID", async () => {
    const first = await studioThreadContext("task", "private-a");
    expect(Object.keys(first).sort()).toEqual(["task_id", "thread_id"]);
    expect(first.thread_id).toMatch(/^[a-f0-9]{64}$/);
    expect(await studioThreadContext("task", "private-a")).toEqual(first);
    expect((await studioThreadContext("task", "private-b")).thread_id).not.toBe(first.thread_id);
  });
});

it("opens the teammate picker when no session or file matches", () => {
  const choose = vi.fn();
  render(
    <FileMentionMenu
      currentDir=""
      activeIndex={-1}
      entries={[]}
      loading={false}
      onOpenDir={vi.fn()}
      onAttach={vi.fn()}
      onMentionPerson={choose}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Mention a teammate…" }));
  expect(choose).toHaveBeenCalledOnce();
});
