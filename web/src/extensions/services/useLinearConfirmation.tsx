import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogFooter,
  DialogHeader,
} from "@/components/ui/dialog";
import { ExtensionHostServiceError } from "./errors";
import type { ConfirmLinear, LinearPreview } from "./studio";

export function useLinearConfirmation() {
  const [preview, setPreview] = useState<LinearPreview | null>(null);
  const pending = useRef<((approved: boolean) => void) | null>(null);
  const confirm = useCallback<ConfirmLinear>((next, signal) => {
    signal.throwIfAborted();
    if (pending.current)
      throw new ExtensionHostServiceError("Busy", "Another Linear update is awaiting confirmation");
    return new Promise((resolve) => {
      const cancel = () => finish(false);
      function finish(approved: boolean) {
        signal.removeEventListener("abort", cancel);
        pending.current = null;
        setPreview(null);
        resolve(approved && !signal.aborted);
      }
      pending.current = finish;
      signal.addEventListener("abort", cancel, { once: true });
      setPreview(next);
    });
  }, []);
  useEffect(() => () => pending.current?.(false), []);

  const dialog = preview && (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) pending.current?.(false);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {preview.change ? "Apply this Linear change?" : "Post this comment to Linear?"}
          </DialogTitle>
          <DialogDescription>
            Workspace {preview.linear_workspace} · Issue {preview.linear_issue_id}
          </DialogDescription>
        </DialogHeader>
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words text-sm">
          {preview.body}
        </pre>
        <DialogFooter>
          <Button variant="outline" onClick={() => pending.current?.(false)}>
            Cancel
          </Button>
          <Button onClick={() => pending.current?.(true)}>
            {preview.change ? "Apply change" : "Post comment"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
  return { confirm, dialog };
}
