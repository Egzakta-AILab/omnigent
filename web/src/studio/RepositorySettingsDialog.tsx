import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useViewerId } from "@/hooks/useViewerId";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

type Role = "active" | "reference" | "excluded";
interface Checkout {
  path: string;
  branch: string | null;
  origin?: string | null;
  expected_origin?: string | null;
  role: Role;
  declared: boolean;
  checkout: "ready" | "missing" | "invalid" | "unverified";
}

interface RepositorySettings {
  revision: string;
  depth: number;
  repositories: Checkout[];
  truncated: boolean;
}

async function requestSettings(endpoint: string, init?: RequestInit): Promise<RepositorySettings> {
  const response = await authenticatedFetch(endpoint, init);
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as RepositorySettings;
}

function RepositorySettingsForm({ endpoint }: { endpoint: string }) {
  const viewer = useViewerId();
  const client = useQueryClient();
  const key = ["studio-repository-settings", viewer, endpoint];
  const settings = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => requestSettings(endpoint, { signal }),
    retry: false,
    refetchOnWindowFocus: false,
  });
  const [draft, setDraft] = useState<RepositorySettings>();
  const [depthInput, setDepthInput] = useState<string>();
  const [pathInput, setPathInput] = useState("");
  const [saved, setSaved] = useState(false);
  const snapshot = draft ?? settings.data;
  const depth = depthInput ?? String(snapshot?.depth ?? 2);
  const canScan = /^[0-5]$/.test(depth);
  const scan = useMutation({
    mutationFn: async () => {
      const found = await requestSettings(`${endpoint}?depth=${depth}`);
      if (snapshot && found.revision !== snapshot.revision) {
        throw new Error("Repository settings changed. Reload before scanning again.");
      }
      const chosen = new Map(snapshot?.repositories.map((repo) => [repo.path, repo]));
      const repositories = found.repositories.map((repo) => {
        const prior = chosen.get(repo.path);
        chosen.delete(repo.path);
        return prior && !repo.declared ? { ...repo, role: prior.role } : repo;
      });
      const staged = [...chosen.values()].filter(
        (repo) => repo.role !== "reference" || repo.checkout === "unverified",
      );
      return { ...found, repositories: [...repositories, ...staged] };
    },
    onSuccess: (result) => {
      setDraft(result);
      setSaved(false);
    },
  });
  const save = useMutation({
    mutationFn: () =>
      requestSettings(endpoint, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          revision: snapshot!.revision,
          depth: Number(depth),
          repositories: snapshot!.repositories
            .filter((repo) => !repo.declared)
            .map(({ path, role, origin }) => ({ path, role, origin })),
        }),
      }),
    onSuccess: (result) => {
      client.setQueryData(key, result);
      void client.invalidateQueries({ queryKey: ["workspace-repositories"] });
      setDraft(undefined);
      setDepthInput(undefined);
      setSaved(true);
      scan.reset();
    },
  });
  const busy = settings.isFetching || scan.isPending || save.isPending;
  const error = save.error ?? scan.error ?? settings.error;
  const path = pathInput.trim();
  const assignmentCount = snapshot?.repositories.filter((repo) => !repo.declared).length ?? 0;

  function addPath() {
    if (!snapshot || !path || snapshot.repositories.some((repo) => repo.path === path)) return;
    setDraft({
      ...snapshot,
      repositories: [
        ...snapshot.repositories,
        { path, branch: null, role: "reference", declared: false, checkout: "unverified" },
      ],
    });
    setPathInput("");
    setSaved(false);
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-2">
        <label className="space-y-1 text-ui" htmlFor="studio-repo-depth">
          <span>Scan depth</span>
          <Input
            id="studio-repo-depth"
            type="number"
            min={0}
            max={5}
            step={1}
            value={depth}
            disabled={busy || !snapshot}
            onChange={(event) => {
              setDepthInput(event.target.value);
              setSaved(false);
            }}
            className="w-24"
          />
        </label>
        <Button
          variant="outline"
          disabled={busy || !snapshot || !canScan}
          onClick={() => scan.mutate()}
        >
          {scan.isPending ? "Scanning…" : "Scan repositories"}
        </Button>
        <p className="self-center text-ui text-muted-foreground">
          0 scans the workspace root. 1 includes its immediate subfolders.
        </p>
      </div>
      {settings.isPending && <p role="status">Loading repositories…</p>}
      {snapshot?.truncated && (
        <p role="status" className="text-ui text-muted-foreground">
          The scan reached its limit. Lower the depth or add a checkout by path.
        </p>
      )}
      {assignmentCount >= 64 && (
        <p role="status" className="text-ui text-muted-foreground">
          A workspace can save up to 64 checkout assignments. Remove an entry to make room.
        </p>
      )}
      {snapshot && (
        <>
          {snapshot.repositories.length ? (
            <ul className="divide-y divide-border rounded-md border border-border">
              {snapshot.repositories.map((repo) => (
                <li
                  key={repo.path}
                  className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-ui"
                >
                  <div className="min-w-0 space-y-1">
                    <p className="font-mono wrap-break-word">
                      {repo.path === "." ? ". (workspace root)" : repo.path}
                    </p>
                    <p className="wrap-break-word text-muted-foreground">
                      {[repo.branch, repo.origin, repo.checkout === "ready" ? null : repo.checkout]
                        .filter(Boolean)
                        .join(" · ") || "No origin"}
                    </p>
                    {repo.expected_origin && repo.expected_origin !== repo.origin && (
                      <p className="wrap-break-word text-destructive">
                        Origin changed. Expected {repo.expected_origin}.
                      </p>
                    )}
                  </div>
                  {repo.declared ? (
                    <Badge variant="outline">Active · repos.yaml</Badge>
                  ) : (
                    <div className="flex items-center gap-1">
                      <select
                        aria-label={`Role for ${repo.path}`}
                        value={repo.role}
                        disabled={busy}
                        className="h-8 rounded-md border border-input bg-background px-2 text-foreground"
                        onChange={(event) => {
                          const role = event.target.value as Role;
                          setDraft({
                            ...snapshot,
                            repositories: snapshot.repositories.map((entry) =>
                              entry.path === repo.path ? { ...entry, role } : entry,
                            ),
                          });
                          setSaved(false);
                        }}
                      >
                        <option value="active">Active</option>
                        <option value="reference">Reference</option>
                        <option value="excluded">Excluded</option>
                      </select>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        aria-label={`Remove ${repo.path}`}
                        onClick={() => {
                          setDraft({
                            ...snapshot,
                            repositories: snapshot.repositories.filter(
                              (entry) => entry.path !== repo.path,
                            ),
                          });
                          setSaved(false);
                        }}
                      >
                        Remove
                      </Button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-ui text-muted-foreground">
              No repositories configured. Scan the workspace to find checkouts.
            </p>
          )}
          <div className="flex flex-wrap items-end gap-2">
            <label htmlFor="studio-repo-path" className="min-w-48 grow space-y-1 text-ui">
              <span>Checkout path</span>
              <Input
                id="studio-repo-path"
                placeholder="e.g. references/upstream"
                value={pathInput}
                maxLength={512}
                disabled={busy}
                onChange={(event) => setPathInput(event.target.value)}
              />
            </label>
            <Button
              variant="outline"
              disabled={
                busy ||
                !path ||
                assignmentCount >= 64 ||
                snapshot.repositories.some((repo) => repo.path === path)
              }
              onClick={addPath}
            >
              Add path
            </Button>
          </div>
        </>
      )}
      {error && (
        <p role="alert" className="text-ui text-destructive">
          {error.message}
        </p>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div role="status" className="text-ui text-muted-foreground">
          {saved && "Repository settings saved."}
        </div>
        <div className="flex gap-2">
          {error && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => {
                void settings.refetch().then((result) => {
                  if (!result.isSuccess) return;
                  setDraft(undefined);
                  setDepthInput(undefined);
                  scan.reset();
                  save.reset();
                });
              }}
            >
              Reload settings
            </Button>
          )}
          <Button
            disabled={busy || !snapshot || !canScan || assignmentCount > 64}
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : "Save repositories"}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function RepositorySettingsDialog({
  open,
  onOpenChange,
  endpoint,
  workspace,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  endpoint: string;
  workspace?: string;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[85dvh] overflow-y-auto sm:max-w-3xl"
        onClick={(event) => event.stopPropagation()}
      >
        <DialogHeader>
          <DialogTitle>Workspace repositories</DialogTitle>
          <DialogDescription>
            Scan the working directory on the connected host. Active checkouts appear in the PR
            picker. References remain available for context.
          </DialogDescription>
          {workspace && (
            <p className="wrap-break-word font-mono text-xs text-muted-foreground">{workspace}</p>
          )}
        </DialogHeader>
        {open && <RepositorySettingsForm key={endpoint} endpoint={endpoint} />}
      </DialogContent>
    </Dialog>
  );
}
