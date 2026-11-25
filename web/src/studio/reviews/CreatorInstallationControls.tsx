import { useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  changeCreatorInstallation,
  getCreatorInstallation,
  type CreatorPanelCapability,
  type CreatorPanelChange,
  type CreatorPanelPreview,
} from "./api";

const CAPABILITY_LABELS: Record<CreatorPanelCapability, string> = {
  "review.read": "Read review findings and check outcomes",
  "review.openProof": "Open receipts in the review",
  "review.attachProof": "Select a receipt in the agent question form",
};

export function useCreatorInstallation(
  runId: string | null,
  viewerId: string | null,
  onCommitted: () => void,
) {
  const queryClient = useQueryClient();
  const queryKey = ["creator-installation", viewerId, runId] as const;
  const installation = useQuery({
    queryKey,
    queryFn: ({ signal }) => getCreatorInstallation(runId!, signal),
    enabled: !!runId,
    retry: false,
    gcTime: 0,
    refetchInterval: 5000,
  });
  const change = useMutation({
    mutationFn: (action: CreatorPanelChange) => changeCreatorInstallation(runId!, action),
    onMutate: () => queryClient.cancelQueries({ queryKey }),
    onSuccess: async (next) => {
      await queryClient.cancelQueries({ queryKey });
      queryClient.setQueryData(queryKey, next);
      onCommitted();
    },
    onError: () => queryClient.invalidateQueries({ queryKey }),
  });
  return { installation, change };
}

function GrantAction({
  current,
  requested,
  busy,
  label,
  onConfirm,
}: {
  current: CreatorPanelCapability[];
  requested: CreatorPanelCapability[];
  busy: boolean;
  label: string;
  onConfirm: (approved?: CreatorPanelCapability[]) => void;
}) {
  const [approved, setApproved] = useState(false);
  const grantsId = useId();
  const changed =
    current.length !== requested.length || requested.some((grant) => !current.includes(grant));
  return (
    <div className="space-y-2">
      {changed && (
        <>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={approved}
              disabled={busy}
              aria-describedby={grantsId}
              onChange={(event) => setApproved(event.target.checked)}
            />
            Approve these capabilities
          </label>
          <ul id={grantsId} className="ml-6 list-disc text-muted-foreground">
            {requested.map((grant) => (
              <li key={grant}>{CAPABILITY_LABELS[grant]}</li>
            ))}
          </ul>
        </>
      )}
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={busy || (changed && !approved)}
        onClick={() => onConfirm(changed ? requested : undefined)}
      >
        {label}
      </Button>
    </div>
  );
}

export function CreatorInstallationControls({
  state,
  preview,
  onEdit,
}: {
  state: ReturnType<typeof useCreatorInstallation>;
  preview?: CreatorPanelPreview;
  onEdit: (definition: string) => void;
}) {
  const { installation, change } = state;
  const [versionChoice, setVersionChoice] = useState("");
  if (installation.isPending) return <p role="status">Loading project installation…</p>;
  if (installation.isError) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <p role="alert">Could not load the project installation.</p>
        <Button size="sm" variant="outline" onClick={() => void installation.refetch()}>
          Retry installation
        </Button>
      </div>
    );
  }
  const {
    revision,
    enabled,
    selected_version: selected,
    active_panel: active,
    history,
  } = installation.data;
  const current = history.find(({ version }) => version === selected);
  const target = history.find(({ version }) => String(version) === versionChoice);
  const installed = enabled && !!active;
  const grants = current?.capabilities ?? [];
  const previewGrants: CreatorPanelCapability[] = ["review.read", "review.openProof"];
  if (preview?.panel.proof_action === "open_and_discuss") previewGrants.push("review.attachProof");

  return (
    <section aria-label="Project panel installation" className="space-y-3 rounded-md border p-3">
      <h3 className="font-medium">Project panel installation</h3>
      <p className="text-muted-foreground">Applies to your account in this project.</p>
      {current && (
        <p role="status">
          Version {current.version} · {current.title ?? "Unavailable package"} ·{" "}
          {installed ? "Enabled" : enabled ? "Unavailable" : "Disabled"}
        </p>
      )}
      {enabled && !active && (
        <p role="alert">
          The selected version could not be loaded. Choose another version or disable it.
        </p>
      )}
      {change.error && <p role="alert">{change.error.message}</p>}
      {change.isSuccess && <p role="status">Installation updated.</p>}
      {preview && (!installed || preview.digest !== current?.digest) && (
        <GrantAction
          key={`install:${revision}:${preview.digest}`}
          current={grants}
          requested={previewGrants}
          busy={change.isPending}
          label={installed ? "Install update" : "Enable preview"}
          onConfirm={(approved) =>
            change.mutate({
              expected_revision: revision,
              panel: preview.panel,
              digest: preview.digest,
              approved_capabilities: approved,
            })
          }
        />
      )}
      {history.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {active && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => onEdit(JSON.stringify(active, null, 2))}
            >
              Edit installed definition
            </Button>
          )}
          {enabled && (
            <Button
              size="sm"
              variant="outline"
              disabled={change.isPending}
              onClick={() => change.mutate({ expected_revision: revision, version: null })}
            >
              Disable panel
            </Button>
          )}
          <label className="flex items-center gap-2">
            <span>Version history</span>
            <select
              className="min-w-0 rounded-md border bg-background p-1"
              value={versionChoice}
              onChange={(event) => setVersionChoice(event.target.value)}
            >
              <option value="">Choose a version</option>
              {history.map(({ version, title, digest, capabilities }) => (
                <option key={version} value={version} disabled={!capabilities}>
                  {version} · {title ?? "Unavailable"} · {digest?.slice(0, 8) ?? "Invalid digest"}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}
      {target?.capabilities && (!enabled || target.version !== selected) && (
        <GrantAction
          key={`activate:${revision}:${target.version}`}
          current={grants}
          requested={target.capabilities}
          busy={change.isPending}
          label={
            enabled && selected !== null && target.version < selected
              ? `Roll back to version ${target.version}`
              : `Enable version ${target.version}`
          }
          onConfirm={(approved) =>
            change.mutate({
              expected_revision: revision,
              version: target.version,
              approved_capabilities: approved,
            })
          }
        />
      )}
    </section>
  );
}
