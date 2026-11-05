import { useId, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { nativeEffortOptions } from "@/components/HarnessConfigControls";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useHostModelOptions } from "@/hooks/useHosts";
import { useViewerId } from "@/hooks/useViewerId";
import { ApiError } from "@/lib/sessionsApi";
import { cn } from "@/lib/utils";
import {
  getReviewProfileEditor,
  REVIEW_HARNESS_LABELS,
  REVIEW_PROFILE_ID,
  saveReviewProfileEditor,
  type ReviewHarness,
  type ReviewProfileDraft,
  type ReviewProfileEditorEntry,
} from "./api";

const MAX_INSTRUCTION_BYTES = 8 * 1024;
const SELECT_CLASS =
  "h-8 w-full min-w-0 rounded-lg border border-input bg-background px-2 text-ui focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-50";

interface EditorContext {
  endpoint: string;
  hostId?: string;
  workspace?: string;
  onClose: () => void;
}

function editableCopy(profile: ReviewProfileDraft): ReviewProfileDraft {
  const { id, label, description, instructions, archived, defaults } = profile;
  return { id, label, description, instructions, archived, defaults };
}

function newProfile(): ReviewProfileDraft {
  return { id: "", label: "", description: "", instructions: "", archived: false };
}

function ProfileDefaults({
  profile,
  hostId,
  readOnly,
  update,
}: {
  profile: ReviewProfileDraft;
  hostId?: string;
  readOnly: boolean;
  update: (changes: Partial<ReviewProfileDraft>) => void;
}) {
  const { harness, model, reasoning_effort: effort } = profile.defaults ?? {};
  const modelCatalog = useHostModelOptions(hostId ?? null, harness ?? "", !!harness, {
    poll: false,
  });
  const models = modelCatalog.data ?? [];
  const efforts = nativeEffortOptions(harness, models, model) ?? [];
  const hasSavedModel = !!model && !models.some((item) => item.id === model);
  const hasSavedEffort = !!effort && !efforts.some((item) => item.value === effort);
  return (
    <fieldset className="space-y-2 rounded-lg border border-border px-3 pb-3" disabled={readOnly}>
      <legend className="px-1 text-xs font-medium text-muted-foreground">Run defaults</legend>
      <div className="grid gap-2 sm:grid-cols-3">
        <label className="min-w-0 space-y-1 text-xs">
          <span>Harness</span>
          <select
            className={SELECT_CLASS}
            value={harness ?? ""}
            onChange={(event) => {
              const value = event.target.value as ReviewHarness | "";
              update({ defaults: value ? { harness: value } : null });
            }}
          >
            <option value="">Choose per run</option>
            {Object.entries(REVIEW_HARNESS_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="min-w-0 space-y-1 text-xs">
          <span>Model</span>
          <select
            className={SELECT_CLASS}
            value={model ?? ""}
            disabled={!harness && !model}
            onChange={(event) =>
              update({ defaults: { harness, model: event.target.value || null } })
            }
          >
            <option value="">Harness default</option>
            {hasSavedModel && <option value={model}>{model} (saved)</option>}
            {models.map((item) => (
              <option key={item.id} value={item.id}>
                {item.displayName || item.id}
              </option>
            ))}
          </select>
        </label>
        <label className="min-w-0 space-y-1 text-xs">
          <span>Reasoning effort</span>
          <select
            className={SELECT_CLASS}
            value={effort ?? ""}
            disabled={!harness && !effort}
            onChange={(event) =>
              update({ defaults: { harness, model, reasoning_effort: event.target.value || null } })
            }
          >
            <option value="">Harness default</option>
            {hasSavedEffort && <option value={effort}>{effort} (saved)</option>}
            {efforts.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {harness && modelCatalog.isError && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>Couldn’t load models for this host.</span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => void modelCatalog.refetch()}
          >
            Reload models
          </Button>
        </div>
      )}
    </fieldset>
  );
}

function ProfileEditor({ endpoint, hostId, workspace, onClose }: EditorContext) {
  const viewer = useViewerId();
  const queryClient = useQueryClient();
  const formId = useId();
  const key = ["studio-review-profile-editor", viewer, endpoint, hostId, workspace] as const;
  const catalog = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => getReviewProfileEditor(endpoint, signal),
    retry: false,
    refetchOnWindowFocus: false,
  });
  const [selectedId, setSelectedId] = useState<string | undefined>();
  const [draft, setDraft] = useState<{
    profile: ReviewProfileDraft;
    revision: string;
    isNew: boolean;
  } | null>(null);
  const [preview, setPreview] = useState(false);
  const [saved, setSaved] = useState(false);
  const profiles = catalog.data?.profiles ?? [];
  const selected = profiles.find((profile) => profile.id === selectedId) ?? profiles[0];
  const profile = draft?.profile ?? selected;
  const readOnly = !draft?.isNew && !selected?.managed;
  const dirty = draft !== null;
  const stale = !!draft && !!catalog.data && draft.revision !== catalog.data.revision;

  const save = useMutation({
    mutationFn: (change: { revision: string; profile: ReviewProfileDraft }) =>
      saveReviewProfileEditor(endpoint, change.revision, change.profile),
    onSuccess: (data, change) => {
      queryClient.setQueryData(key, data);
      void queryClient.invalidateQueries({ queryKey: ["studio-review-profiles"] });
      setSelectedId(change.profile.id);
      setDraft(null);
      setSaved(true);
    },
    retry: false,
  });

  function begin(nextProfile: ReviewProfileDraft, isNew: boolean) {
    if (!catalog.data || catalog.isError) return;
    setDraft({ profile: nextProfile, revision: catalog.data.revision, isNew });
    setPreview(false);
    setSaved(false);
    save.reset();
  }

  function update(changes: Partial<ReviewProfileDraft>) {
    if (draft) setDraft({ ...draft, profile: { ...draft.profile, ...changes } });
    else if (selected?.managed) begin({ ...editableCopy(selected), ...changes }, false);
  }

  function duplicate(source: ReviewProfileDraft) {
    begin(
      {
        ...editableCopy(source),
        id: "",
        label: `${source.label} copy`.slice(0, 128),
        archived: false,
      },
      true,
    );
  }

  function choose(entry: ReviewProfileEditorEntry) {
    setSelectedId(entry.id);
    setPreview(false);
    setSaved(false);
    save.reset();
  }

  function discard() {
    setDraft(null);
    setSaved(false);
    save.reset();
  }

  const conflict = save.error instanceof ApiError && save.error.status === 409;
  const instructionsSize = profile ? new TextEncoder().encode(profile.instructions).length : 0;
  const duplicateId = !!draft?.isNew && profiles.some((entry) => entry.id === draft.profile.id);
  const invalidId = !!draft?.isNew && !REVIEW_PROFILE_ID.test(draft.profile.id);
  const canSave =
    !!draft &&
    !!profile?.label.trim() &&
    !!profile.instructions.trim() &&
    instructionsSize <= MAX_INSTRUCTION_BYTES &&
    !invalidId &&
    !duplicateId &&
    !stale &&
    !conflict &&
    !catalog.isError &&
    !save.isPending;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (draft && canSave) save.mutate(draft);
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !dirty && !save.isPending) onClose();
      }}
    >
      <DialogContent
        className="flex max-h-[88dvh] flex-col gap-0 p-0 sm:max-w-[58rem]"
        showCloseButton={false}
      >
        <DialogHeader className="shrink-0 border-b border-border px-5 py-4">
          <DialogTitle>Reviewer profiles</DialogTitle>
          <DialogDescription className="[overflow-wrap:anywhere]">
            {workspace ? `Workspace: ${workspace}` : "Profiles saved on the workspace host."}
          </DialogDescription>
        </DialogHeader>
        {catalog.isPending ? (
          <p className="p-5 text-sm text-muted-foreground" role="status">
            Loading profiles…
          </p>
        ) : catalog.isError && !catalog.data ? (
          <div className="space-y-2 p-5">
            <p role="alert" className="text-sm text-destructive">
              {catalog.error.message}
            </p>
            <Button type="button" variant="outline" onClick={() => void catalog.refetch()}>
              Reload profiles
            </Button>
          </div>
        ) : (
          <div className="grid min-h-0 flex-1 overflow-y-auto sm:grid-cols-[15rem_minmax(0,1fr)] sm:overflow-hidden">
            <aside className="space-y-3 border-b border-border bg-muted/30 p-3 sm:overflow-y-auto sm:border-r sm:border-b-0">
              <Button
                type="button"
                variant="outline"
                className="w-full"
                disabled={dirty || catalog.isError}
                onClick={() => begin(newProfile(), true)}
              >
                New profile
              </Button>
              <ul
                className="flex gap-1 overflow-x-auto sm:flex-col"
                aria-label="Workspace profiles"
              >
                {profiles.map((entry) => (
                  <li key={entry.id} className="min-w-0 shrink-0 sm:shrink">
                    <button
                      type="button"
                      className={cn(
                        "flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm hover:bg-accent disabled:opacity-70",
                        selected?.id === entry.id &&
                          !draft?.isNew &&
                          "bg-accent font-medium text-accent-foreground",
                      )}
                      aria-current={selected?.id === entry.id && !draft?.isNew ? "true" : undefined}
                      disabled={dirty}
                      onClick={() => choose(entry)}
                    >
                      <span className="min-w-0 truncate" title={entry.label}>
                        {entry.label}
                      </span>
                      {entry.archived && <Badge variant="outline">Archived</Badge>}
                    </button>
                  </li>
                ))}
              </ul>
            </aside>
            <div className="min-w-0 p-4 sm:overflow-y-auto">
              {profile ? (
                <form className="space-y-4" id={formId} onSubmit={submit}>
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <h3 className="font-medium [overflow-wrap:anywhere]">
                        {draft?.isNew ? "New profile" : profile.label}
                      </h3>
                      <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
                        {draft?.isNew ? "Saved to this workspace" : selected?.source}
                        {readOnly && " · Linked source"}
                      </p>
                    </div>
                    {!draft?.isNew && (
                      <div className="flex flex-wrap gap-1">
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={dirty || catalog.isError}
                          onClick={() => duplicate(profile)}
                        >
                          Duplicate
                        </Button>
                        {!readOnly && (
                          <Button
                            type="button"
                            size="sm"
                            variant="ghost"
                            disabled={save.isPending}
                            onClick={() => update({ archived: !profile.archived })}
                          >
                            {profile.archived ? "Restore" : "Archive"}
                          </Button>
                        )}
                      </div>
                    )}
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <label className="min-w-0 space-y-1 text-xs">
                      <span>Name</span>
                      <Input
                        value={profile.label}
                        maxLength={128}
                        required
                        readOnly={readOnly || save.isPending}
                        onChange={(event) => update({ label: event.target.value })}
                      />
                    </label>
                    <label className="min-w-0 space-y-1 text-xs">
                      <span>Profile ID</span>
                      <Input
                        value={profile.id}
                        maxLength={64}
                        required
                        readOnly={!draft?.isNew || save.isPending}
                        aria-invalid={!!profile.id && (invalidId || duplicateId)}
                        onChange={(event) => update({ id: event.target.value })}
                      />
                      {draft?.isNew && (
                        <p
                          className={cn(
                            "text-xs",
                            duplicateId ? "text-destructive" : "text-muted-foreground",
                          )}
                        >
                          {duplicateId
                            ? "A profile already uses this ID."
                            : "Lowercase letters, digits, hyphens, and underscores."}
                        </p>
                      )}
                    </label>
                  </div>
                  <label className="block space-y-1 text-xs">
                    <span>Description</span>
                    <Input
                      value={profile.description}
                      maxLength={512}
                      readOnly={readOnly || save.isPending}
                      onChange={(event) => update({ description: event.target.value })}
                    />
                  </label>
                  <ProfileDefaults
                    profile={profile}
                    hostId={hostId}
                    readOnly={readOnly || save.isPending}
                    update={update}
                  />
                  <div className="space-y-2">
                    <div className="flex items-center justify-between gap-2">
                      <label htmlFor={`${formId}-instructions`} className="text-xs font-medium">
                        Instructions
                      </label>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        onClick={() => setPreview(!preview)}
                      >
                        {preview ? "Markdown source" : "Preview"}
                      </Button>
                    </div>
                    {preview ? (
                      <div
                        className="min-h-40 space-y-3 rounded-lg border border-input p-3 text-sm [overflow-wrap:anywhere] [&_ul]:list-disc [&_ol]:list-decimal [&_li]:ml-4 [&_pre]:overflow-x-auto [&_a]:underline"
                        aria-label="Instructions preview"
                      >
                        {profile.instructions.trim() ? (
                          <ReactMarkdown
                            remarkPlugins={[remarkGfm]}
                            skipHtml
                            disallowedElements={["img"]}
                          >
                            {profile.instructions}
                          </ReactMarkdown>
                        ) : (
                          <p className="text-muted-foreground">No instructions to preview.</p>
                        )}
                      </div>
                    ) : (
                      <Textarea
                        id={`${formId}-instructions`}
                        value={profile.instructions}
                        className="min-h-40 font-mono"
                        readOnly={readOnly || save.isPending}
                        aria-invalid={instructionsSize > MAX_INSTRUCTION_BYTES}
                        onChange={(event) => update({ instructions: event.target.value })}
                      />
                    )}
                    {instructionsSize > MAX_INSTRUCTION_BYTES && (
                      <p className="text-xs text-destructive">
                        Instructions must fit within 8 KiB.
                      </p>
                    )}
                  </div>
                  {save.isError && (
                    <p role="alert" className="text-sm text-destructive">
                      {save.error.message}
                    </p>
                  )}
                  {(stale || conflict || catalog.isError) && (
                    <div className="space-y-2 rounded-md border border-border p-2 text-sm text-muted-foreground">
                      <p>
                        {catalog.isError
                          ? catalog.error.message
                          : "The workspace catalog changed. Your draft is still here."}
                      </p>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={catalog.isFetching}
                          onClick={() => void catalog.refetch()}
                        >
                          Reload profiles
                        </Button>
                        {stale && draft && !catalog.isError && (
                          <Button
                            type="button"
                            size="sm"
                            variant="outline"
                            onClick={() => duplicate(draft.profile)}
                          >
                            Copy draft to new profile
                          </Button>
                        )}
                      </div>
                    </div>
                  )}
                </form>
              ) : (
                <p className="text-sm text-muted-foreground">
                  Create a profile with instructions for how to review changes in this workspace.
                </p>
              )}
            </div>
          </div>
        )}
        <DialogFooter className="mx-0 mb-0 shrink-0 items-center gap-2 border-t border-border px-5 py-3 sm:justify-between">
          <p className="mr-auto text-xs text-muted-foreground" role="status">
            {saved ? "Profile saved" : dirty ? "Unsaved changes" : ""}
          </p>
          <div className="flex flex-wrap items-center justify-end gap-2">
            {dirty ? (
              <Button type="button" variant="ghost" disabled={save.isPending} onClick={discard}>
                Discard changes
              </Button>
            ) : (
              <Button type="button" variant="outline" onClick={onClose}>
                Close
              </Button>
            )}
            {!readOnly && profile && (
              <Button type="submit" form={formId} disabled={!canSave}>
                {save.isPending ? "Saving…" : "Save profile"}
              </Button>
            )}
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ProfileEditorDialog({
  open,
  onOpenChange,
  endpoint,
  hostId,
  workspace,
}: Omit<EditorContext, "onClose"> & { open: boolean; onOpenChange: (open: boolean) => void }) {
  return open ? (
    <ProfileEditor
      key={JSON.stringify([endpoint, hostId, workspace])}
      endpoint={endpoint}
      hostId={hostId}
      workspace={workspace}
      onClose={() => onOpenChange(false)}
    />
  ) : null;
}
