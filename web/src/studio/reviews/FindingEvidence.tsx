import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  attachFindingEvidence,
  listFindingEvidence,
  readFindingEvidence,
  type FindingArtifact,
} from "./api";

const FILE_EXTENSIONS = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "video/mp4": "mp4",
  "video/webm": "webm",
  "application/json": "json",
} as const;

function EvidencePreview({
  entry,
  runId,
  findingKey,
  viewerId,
}: {
  entry: FindingArtifact;
  runId: string;
  findingKey: string;
  viewerId: string | null;
}) {
  const { digest, media_type: type } = entry;
  const { data, error, isPending } = useQuery({
    queryKey: ["studio-evidence-file", viewerId, runId, findingKey, digest],
    queryFn: ({ signal }) => readFindingEvidence(runId, findingKey, digest, signal),
    retry: false,
    gcTime: 0,
    staleTime: Infinity,
  });
  const [url, setUrl] = useState<string>();
  useEffect(() => {
    if (!data) return;
    const blobUrl = URL.createObjectURL(data);
    setUrl(blobUrl);
    return () => URL.revokeObjectURL(blobUrl);
  }, [data]);

  if (error) return <p role="alert">{error.message}</p>;
  if (isPending || !url) return <p role="status">Loading evidence…</p>;
  return (
    <div className="space-y-2">
      {type.startsWith("image/") && (
        <img alt="Screenshot attached to this finding" src={url} className="max-h-96 rounded" />
      )}
      {type.startsWith("video/") && (
        <video
          aria-label="Finding recording"
          src={url}
          controls
          className="max-h-96 max-w-full rounded"
        />
      )}
      <a
        className="underline"
        href={url}
        download={`finding-${digest.slice(0, 12)}.${FILE_EXTENSIONS[type]}`}
      >
        Download evidence
      </a>
    </div>
  );
}

function Measurements({ evidence }: { evidence: NonNullable<FindingArtifact["measurements"]> }) {
  return (
    <div className="space-y-2">
      <p className="whitespace-pre-wrap break-words">{evidence.method}</p>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead>
            <tr>
              <th scope="col">Metric</th>
              <th scope="col">Baseline</th>
              <th scope="col">Observed</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {evidence.metrics.map(({ label, unit, baseline, observed }) => (
              <tr key={label}>
                <th scope="row" className="break-words py-1 pr-2 font-medium">
                  {label}
                </th>
                <td className="py-1 pr-2">{baseline == null ? "—" : `${baseline} ${unit}`}</td>
                <td className="py-1">
                  {observed} {unit}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function FindingEvidence({
  runId,
  findingKey,
  patchDigest,
  viewerId,
  canAttach,
}: {
  runId: string;
  findingKey: string;
  patchDigest: string;
  viewerId: string | null;
  canAttach: boolean;
}) {
  const queryClient = useQueryClient();
  const queryKey = ["studio-finding-evidence", viewerId, runId, findingKey];
  const evidence = useQuery({
    queryKey,
    queryFn: ({ signal }) => listFindingEvidence(runId, findingKey, signal),
    retry: false,
    staleTime: 0,
    gcTime: 0,
  });
  const fileInput = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File>();
  const [opened, setOpened] = useState<string>();
  const upload = useMutation({
    mutationFn: (selected: File) => attachFindingEvidence(runId, findingKey, patchDigest, selected),
    onSuccess: async () => {
      setFile(undefined);
      if (fileInput.current) fileInput.current.value = "";
      await queryClient.invalidateQueries({ queryKey });
    },
  });
  return (
    <section aria-label="Finding evidence" className="mt-3 space-y-2 border-t pt-2">
      <h4 className="font-medium">Screenshots, recordings, and measurements</h4>
      {canAttach && (
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (file && !upload.isPending) upload.mutate(file);
          }}
        >
          <p className="w-full text-xs text-muted-foreground">
            Evidence is shared with people who can open this saved review.
          </p>
          <input
            ref={fileInput}
            type="file"
            className="max-w-full text-xs"
            aria-label="Finding evidence file"
            accept=".png,.jpg,.jpeg,.mp4,.webm,.json"
            disabled={upload.isPending}
            onChange={(event) => {
              setFile(event.currentTarget.files?.[0]);
              upload.reset();
            }}
          />
          <Button type="submit" variant="outline" size="sm" disabled={!file || upload.isPending}>
            {upload.isPending ? "Attaching…" : "Attach evidence"}
          </Button>
          {upload.error && (
            <p role="alert" className="w-full">
              {upload.error.message}
            </p>
          )}
        </form>
      )}
      {evidence.error && <p role="alert">{evidence.error.message}</p>}
      {evidence.data?.length === 0 && (
        <p className="text-xs text-muted-foreground">No evidence files attached.</p>
      )}
      {evidence.data?.map((entry) => {
        const label = entry.measurements
          ? "Measurements"
          : entry.media_type.startsWith("image/")
            ? "Screenshot"
            : "Recording";
        return (
          <article key={entry.digest} className="space-y-2 rounded-md border p-2">
            <p className="font-medium">{label}</p>
            <p className="break-all text-xs text-muted-foreground">
              Uploaded · head{" "}
              <code title={entry.source.head_sha}>{entry.source.head_sha.slice(0, 8)}</code>
              {" · "}
              <code title={entry.digest}>SHA-256 {entry.digest.slice(0, 12)}</code>
            </p>
            {entry.measurements && <Measurements evidence={entry.measurements} />}
            <Button
              variant="ghost"
              size="sm"
              aria-expanded={opened === entry.digest}
              onClick={() => {
                setOpened(opened === entry.digest ? undefined : entry.digest);
              }}
            >
              {opened === entry.digest ? "Hide evidence" : `Open ${label.toLowerCase()}`}
            </Button>
            {opened === entry.digest && (
              <EvidencePreview
                entry={entry}
                runId={runId}
                findingKey={findingKey}
                viewerId={viewerId}
              />
            )}
          </article>
        );
      })}
    </section>
  );
}
