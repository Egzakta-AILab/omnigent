import { useInfiniteQuery } from "@tanstack/react-query";
import { GitPullRequestIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useViewerId } from "@/hooks/useViewerId";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { Link } from "@/lib/routing";
import { listSharedReviews } from "./api";

export function SharedReviewsInbox() {
  const viewerId = useViewerId();
  const info = useServerInfo();
  const enabled =
    viewerId !== null &&
    viewerId !== "local" &&
    info !== "loading" &&
    info.studio_reviews === true &&
    (info.sharing_mode === "on" || info.sharing_mode === "read_only");
  const reviews = useInfiniteQuery({
    queryKey: ["studio-shared-reviews", viewerId],
    enabled,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => listSharedReviews(pageParam, signal),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    refetchOnMount: "always",
  });
  const entries = reviews.data?.pages.flatMap((page) => page.entries) ?? [];
  if (!enabled || (!entries.length && !reviews.isError)) return null;

  return (
    <section aria-labelledby="shared-review-heading" className="mb-6 space-y-2">
      <h2 id="shared-review-heading" className="px-3 py-1.5 text-sm text-muted-foreground">
        Shared PR reviews
      </h2>
      {entries.map((review) => (
        <Link
          key={review.id}
          to={`/reviews/${review.id}/discussion`}
          className="flex items-center gap-3 rounded-lg border px-3 py-2.5 hover:bg-muted"
        >
          <GitPullRequestIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
          <div className="min-w-0">
            <div className="break-words text-ui font-medium">
              {review.repository} #{review.pr_number}
            </div>
            <div className="break-words text-sm text-muted-foreground">
              {review.project_name} · <code>{review.head_sha.slice(0, 8)}</code>
            </div>
          </div>
        </Link>
      ))}
      {reviews.isError ? (
        <div className="flex items-center gap-2 px-3 text-sm text-muted-foreground">
          <span role="status">Couldn’t load shared PR reviews.</span>
          <Button variant="ghost" size="sm" onClick={() => void reviews.refetch()}>
            Retry
          </Button>
        </div>
      ) : (
        reviews.hasNextPage && (
          <Button
            variant="outline"
            size="sm"
            disabled={reviews.isFetching}
            onClick={() => void reviews.fetchNextPage()}
          >
            {reviews.isFetchingNextPage ? "Loading…" : "Load more shared reviews"}
          </Button>
        )
      )}
    </section>
  );
}
