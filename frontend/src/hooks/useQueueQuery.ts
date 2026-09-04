import {
  queryOptions,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { getQueue } from "@/lib/api";
import { countActiveJobs } from "@/lib/queue";
import { queryKeys } from "@/lib/queryKeys";
import { beginQueueFetch, reconcileQueueSnapshot } from "@/lib/queueCache";
import type { Job } from "@/lib/types";

/**
 * The one definition of the `["queue"]` query, shared by every hook that reads
 * it so they cannot drift apart — in particular so both go through the
 * reconciliation below rather than one of them writing a raw snapshot.
 *
 * The response is filtered through the ids dropped since the request went out:
 * a fetch that was already in flight when an SSE event finished a job still
 * lists that job, and TanStack writes a response into the cache
 * unconditionally, so without this the finished row would come back for good.
 * `beginQueueFetch` has to run here, inside the `queryFn` — an effect would
 * miss the refetches this exists for (window focus, tab mount, invalidation).
 */
export function queueQueryOptions(queryClient: QueryClient) {
  return queryOptions<Job[]>({
    queryKey: queryKeys.queue,
    queryFn: async () => {
      beginQueueFetch(queryClient);
      return reconcileQueueSnapshot(queryClient, await getQueue());
    },
  });
}

/**
 * The in-flight and errored jobs from `GET /queue`.
 *
 * There is no `refetchInterval`: the SSE stream patches this cache as events
 * arrive, and `refetchOnWindowFocus` (the default) covers a tab that was in
 * the background while the stream was closed.
 */
export function useQueueQuery() {
  const queryClient = useQueryClient();
  return useQuery(queueQueryOptions(queryClient));
}

/**
 * How many jobs are still working — the number on the Download tab badge.
 *
 * Reads the same query through `select`, so the badge re-renders only when the
 * count itself changes, not on every progress event.
 */
export function useActiveJobCount(): number {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    ...queueQueryOptions(queryClient),
    select: countActiveJobs,
  });
  return data ?? 0;
}
