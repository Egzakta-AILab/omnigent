import { useCallback, useMemo, useRef, useState } from "react";
import { parsePatchFiles } from "@pierre/diffs";
import { FileDiff } from "@pierre/diffs/react";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { PULL_REQUEST_DIFF_THEME, reviewSelection } from "@/lib/pullRequestReviewThreads";
import { findDiffRow, findingLine } from "@/shell/pullRequestReview";
import type { DiscussionAnchor, ReviewSource } from "./api";

export function ReviewDiff({
  patch,
  truncated,
  source,
  sourceLine,
  onClearSource,
  discussionRange,
  onSelectRange,
}: {
  patch: string;
  truncated: boolean;
  source: ReviewSource | null;
  sourceLine?: string | null;
  onClearSource: () => void;
  discussionRange?: Extract<DiscussionAnchor, { kind: "diff" }>;
  onSelectRange?: (range: Extract<DiscussionAnchor, { kind: "diff" }> | null) => void;
}) {
  const themeMode = useResolvedThemeMode();
  const [selectedPath, setSelectedPath] = useState("");
  const files = useMemo(() => {
    try {
      return parsePatchFiles(patch).flatMap((item) => item.files);
    } catch {
      return [];
    }
  }, [patch]);
  const anchoredFile = source
    ? files.find(
        (file) =>
          (source.side === "base" ? (file.prevName ?? file.name) : file.name) === source.path,
      )
    : undefined;
  const selected = anchoredFile ?? files.find((file) => file.name === selectedPath) ?? files[0];
  const selectedRange =
    discussionRange &&
    selected &&
    (discussionRange.side === "base" ? (selected.prevName ?? selected.name) : selected.name) ===
      discussionRange.path
      ? {
          start: discussionRange.start,
          end: discussionRange.end,
          side: discussionRange.side === "base" ? ("deletions" as const) : ("additions" as const),
        }
      : null;
  const anchor = useMemo(
    () => (anchoredFile && source ? findingLine(anchoredFile, source, "unified") : null),
    [anchoredFile, source],
  );
  const diffContainer = useRef<HTMLDivElement>(null);
  const scrolledSource = useRef<ReviewSource | null>(null);
  const revealSource = useCallback(() => {
    if (!source || !anchor || !diffContainer.current || scrolledSource.current === source) return;
    const row = findDiffRow(anchor, diffContainer.current);
    if (!row) return;
    scrolledSource.current = source;
    row.scrollIntoView({ block: "nearest" });
  }, [anchor, source]);

  return (
    <div className="space-y-2">
      {source && (
        <p role="status" className="wrap-break-word text-xs text-muted-foreground">
          {anchor || sourceLine != null
            ? `${discussionRange ? "Discussion range" : "Review finding"} · ${source.path}:${source.line}`
            : "The finding’s cited line is unavailable in this diff preview."}
        </p>
      )}
      {source && !anchor && sourceLine != null && (
        <div className="space-y-1 rounded-md border p-2">
          <p className="text-xs text-muted-foreground">Exact line from the captured patch</p>
          <pre className="overflow-x-auto font-mono text-xs">
            {source.side === "base" ? "−" : "+"}
            {sourceLine}
          </pre>
        </div>
      )}
      {selected ? (
        <>
          <label className="block space-y-1 text-xs">
            <span>Changed file</span>
            <select
              className="h-8 w-full rounded-md border bg-background px-2 text-sm"
              value={selected.name}
              onChange={(event) => {
                onClearSource();
                setSelectedPath(event.target.value);
              }}
            >
              {files.map((file) => (
                <option key={file.name} value={file.name}>
                  {file.name}
                </option>
              ))}
            </select>
          </label>
          <div ref={diffContainer} className="overflow-x-auto rounded-md border">
            <FileDiff
              fileDiff={selected}
              options={{
                diffStyle: "unified",
                theme: PULL_REQUEST_DIFF_THEME,
                themeType: themeMode,
                enableLineSelection: !!onSelectRange,
                onLineSelectionEnd: onSelectRange
                  ? (range) => {
                      const lines = range && reviewSelection(range, selected);
                      if (!lines) return onSelectRange(null);
                      onSelectRange({
                        kind: "diff",
                        path:
                          lines.side === "LEFT"
                            ? (selected.prevName ?? selected.name)
                            : selected.name,
                        side: lines.side === "LEFT" ? "base" : "head",
                        start: lines.start_line ?? lines.line,
                        end: lines.line,
                      });
                    }
                  : undefined,
                onPostRender: revealSource,
              }}
              selectedLines={
                selectedRange ??
                (anchor && !onSelectRange
                  ? { start: anchor.line, end: anchor.line, side: anchor.side }
                  : null)
              }
              disableWorkerPool
            />
          </div>
        </>
      ) : patch ? (
        <pre className="overflow-x-auto rounded-md border bg-muted/40 p-3 text-xs">{patch}</pre>
      ) : (
        <p className="text-muted-foreground">No text changes to display.</p>
      )}
      {truncated && <p className="text-muted-foreground">Diff preview shortened.</p>}
    </div>
  );
}
