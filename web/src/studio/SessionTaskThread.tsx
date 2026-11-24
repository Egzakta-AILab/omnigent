import { useMemo, useState } from "react";
import { useExtensions, useRefreshExtensions } from "@/extensions/ExtensionProvider";
import { ExtensionViewHost } from "@/extensions/ExtensionViewHost";
import { useExtensionHostServices } from "@/extensions/services/useExtensionHostServices";
import type { ExtensionCatalogItem, ExtensionPage } from "@/extensions/types";

export async function studioThreadContext(taskId: string, sessionId: string) {
  const bytes = new TextEncoder().encode(JSON.stringify(["studio-thread@1", taskId, sessionId]));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return {
    task_id: taskId,
    thread_id: Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join(""),
  };
}

export function useStudioThreadPage() {
  const extension = useExtensions().find(
    (e) =>
      e.id === "egzakta.comms" &&
      e.distribution === "egzakta-comms" &&
      e.status === "enabled" &&
      e.permissions.includes("studio.inbox") &&
      e.permissions.includes("navigation"),
  );
  const page = extension?.pages.find(
    (p) => p.id === "egzakta.comms.thread" && p.view === "task-thread",
  );
  return extension && page ? { extension, page } : null;
}

// Mounted only for an active session/task association returned by the ACL-checked host lookup.
export function SessionTaskThread({
  extension,
  page,
  taskId,
  sessionId,
}: {
  extension: ExtensionCatalogItem;
  page: ExtensionPage;
  taskId: string;
  sessionId: string;
}) {
  const { methods, events, dialog } = useExtensionHostServices(extension);
  const refresh = useRefreshExtensions();
  const scoped = useMemo(
    () => ({
      ...methods,
      "navigation.currentStudioTask": () => studioThreadContext(taskId, sessionId),
    }),
    [methods, taskId, sessionId],
  );
  return (
    <>
      <ExtensionViewHost
        key={taskId + sessionId}
        extension={extension}
        page={page}
        methods={scoped}
        events={events}
        refresh={refresh}
        embedded
      />
      {dialog}
    </>
  );
}

export function TaskThreadDisclosure(props: React.ComponentProps<typeof SessionTaskThread>) {
  const [visible, setVisible] = useState(false);
  return (
    <details className="my-2" onToggle={(e) => setVisible(e.currentTarget.open)}>
      <summary>Teammate conversation</summary>
      {visible && <SessionTaskThread {...props} />}
    </details>
  );
}
