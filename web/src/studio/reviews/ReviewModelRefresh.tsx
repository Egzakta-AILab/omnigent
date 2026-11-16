import { Button } from "@/components/ui/button";
import { useRefreshHostModelOptions } from "@/hooks/useHosts";

export function ReviewModelRefresh({
  hostId,
  harness,
  loading,
  loadError,
}: {
  hostId: string;
  harness: string;
  loading: boolean;
  loadError: boolean;
}) {
  const refresh = useRefreshHostModelOptions(hostId, harness);
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={loading || refresh.isPending}
        onClick={() => refresh.mutate()}
      >
        {refresh.isPending ? "Refreshing models…" : "Refresh models"}
      </Button>
      {refresh.isError ? (
        <span role="alert">Refresh failed. {refresh.error.message}</span>
      ) : loadError ? (
        <span role="status">Couldn’t load the host’s models.</span>
      ) : null}
    </div>
  );
}
