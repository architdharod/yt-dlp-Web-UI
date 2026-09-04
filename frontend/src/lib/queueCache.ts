import type { QueryClient } from "@tanstack/react-query";
import { queryKeys } from "@/lib/queryKeys";
import type { Job, SSEEvent } from "@/lib/types";

/**
 * Statuses `GET /queue` does not return. A job that reaches one of them is
 * dropped from the cache straight away rather than waiting for a refetch, so
 * the in-flight view matches what the endpoint would answer right now.
 */
const TERMINAL_HIDDEN_STATUSES: ReadonlySet<Job["status"]> = new Set<
  Job["status"]
>(["done", "cancelled"]);

/**
 * Ids removed from the `["queue"]` cache since the last `GET /queue` went out,
 * kept per `QueryClient` so tests (and any second client) stay independent.
 *
 * Why this exists: a fetch that left before a job was dropped still lists that
 * job, and `Query#fetch` writes its response into the cache unconditionally —
 * so a `done`/`cancelled` job removed by an SSE event while a refetch was in
 * flight would come straight back, permanently, since no later event mentions
 * it. Filtering the response through this set keeps the drop.
 *
 * Clearing on fetch *start* is the correct boundary: anything dropped before
 * the request went out is already absent from the answer the server is about
 * to give, so forgetting it is safe — and necessary, or a job the server
 * legitimately lists again (a retry re-queues a job that had reached `done`)
 * would be filtered out forever. The set therefore only grows between two
 * fetches, which bounds it at the number of jobs that can finish in one
 * request round-trip.
 *
 * Only *removals* are protected this way. An in-place patch — the fresher job
 * `replaceJobInCache` writes from a retry response, or the advisory text
 * `setJobActionError` puts on a row — can still be overwritten by a snapshot
 * that was issued before the patch. That is accepted: the stream re-delivers
 * the job's true state on its next transition, and the action message is only
 * advisory. A "patched since this fetch went out" map was considered and
 * rejected, because replaying a frozen copy of the patch over the response
 * would clobber any fresher stream state applied in between.
 */
const droppedSinceFetch = new WeakMap<QueryClient, Set<string>>();

/** The drop set for *queryClient*, created on first use. */
function droppedIds(queryClient: QueryClient): Set<string> {
  let dropped = droppedSinceFetch.get(queryClient);
  if (dropped === undefined) {
    dropped = new Set<string>();
    droppedSinceFetch.set(queryClient, dropped);
  }
  return dropped;
}

/** Note that *jobId* left the cache, so a fetch already out cannot restore it. */
function recordDrop(queryClient: QueryClient, jobId: string): void {
  droppedIds(queryClient).add(jobId);
}

/** Called by the queue `queryFn` as it issues the request. */
export function beginQueueFetch(queryClient: QueryClient): void {
  droppedIds(queryClient).clear();
}

/** Filter a fetched queue through the drops that happened while it was away. */
export function reconcileQueueSnapshot(
  queryClient: QueryClient,
  jobs: Job[],
): Job[] {
  const dropped = droppedIds(queryClient);
  if (dropped.size === 0) return jobs;
  return jobs.filter((job) => !dropped.has(job.id));
}

/**
 * Merge the job snapshot fields carried by every SSE event into a job.
 * The backend sends status/progress/title/thumbnail_url/duration/artist/album
 * (and error when set) on every event type, so any event can refresh them.
 */
export function mergeSnapshot(job: Job, data: Record<string, unknown>): Job {
  const merged = { ...job };

  if (typeof data.status === "string") {
    merged.status = data.status as Job["status"];
  }
  if (typeof data.progress === "number") {
    merged.progress = data.progress;
  }
  if (typeof data.title === "string") {
    merged.title = data.title;
  }
  if (typeof data.thumbnail_url === "string") {
    merged.thumbnail_url = data.thumbnail_url;
  }
  if (typeof data.duration === "number") {
    merged.duration = data.duration;
  }
  if (typeof data.artist === "string") {
    merged.artist = data.artist;
  }
  if (typeof data.album === "string") {
    merged.album = data.album;
  }
  if (typeof data.error === "string") {
    merged.error = data.error;
  }

  return merged;
}

/** Read the cached queue, or undefined when nothing has been fetched yet. */
function readQueue(queryClient: QueryClient): Job[] | undefined {
  return queryClient.getQueryData<Job[]>(queryKeys.queue);
}

/** Rewrite the cached queue, leaving an unfetched cache alone. */
function writeQueue(
  queryClient: QueryClient,
  update: (jobs: Job[]) => Job[],
): void {
  queryClient.setQueryData<Job[]>(queryKeys.queue, (jobs) =>
    jobs === undefined ? jobs : update(jobs),
  );
}

/** Add a newly created job to the cache; a job already there is left alone. */
export function addJobToCache(queryClient: QueryClient, job: Job): void {
  if (readQueue(queryClient) === undefined) {
    // Nothing has been fetched yet — the initial GET /queue failed, or is still
    // out. Seeding the cache with just this job keeps the row the user asked
    // for visible, and the invalidation fills in everything else.
    queryClient.setQueryData<Job[]>(queryKeys.queue, [job]);
    void queryClient.invalidateQueries({ queryKey: queryKeys.queue });
    return;
  }

  writeQueue(queryClient, (jobs) =>
    jobs.some((j) => j.id === job.id) ? jobs : [...jobs, job],
  );
}

/** Replace a job with a fresher copy from an API response. */
export function replaceJobInCache(queryClient: QueryClient, job: Job): void {
  writeQueue(queryClient, (jobs) =>
    jobs.map((j) => (j.id === job.id ? job : j)),
  );
}

/** Drop a job from the in-flight view. */
export function removeJobFromCache(
  queryClient: QueryClient,
  jobId: string,
): void {
  recordDrop(queryClient, jobId);
  writeQueue(queryClient, (jobs) => jobs.filter((j) => j.id !== jobId));
}

/**
 * Surface a failed row action as the job's own error text.
 *
 * A Cancel or Dismiss that the backend refused has nowhere else to go: the job
 * itself is fine, so the message rides along on the row the user clicked.
 */
export function setJobActionError(
  queryClient: QueryClient,
  jobId: string,
  message: string,
): void {
  writeQueue(queryClient, (jobs) =>
    jobs.map((j) => (j.id === jobId ? { ...j, error: message } : j)),
  );
}

/**
 * Apply one SSE event to the query cache.
 *
 * A plain function over a `QueryClient` rather than something inside the hook,
 * so the whole event-to-cache contract is testable without an EventSource.
 *
 *   - `library_changed` invalidates the library query and touches nothing else.
 *   - An event for a job the cache does not hold means the view is stale
 *     (submitted from another tab, or restored after a backend restart), so the
 *     queue query is invalidated. The snapshot in the event is deliberately not
 *     used to build a job: it carries only the user-visible fields, not `url`
 *     or `created_at`.
 *   - Otherwise the snapshot is merged into the job, and a job that has reached
 *     `done` or `cancelled` leaves the cache, because `GET /queue` omits both.
 */
export function applyQueueEvent(
  queryClient: QueryClient,
  event: SSEEvent,
): void {
  if (event.event === "library_changed") {
    void queryClient.invalidateQueries({ queryKey: queryKeys.library });
    return;
  }

  const jobs = readQueue(queryClient);
  const jobId = event.job_id;
  if (jobId === null || jobs === undefined || !jobs.some((j) => j.id === jobId)) {
    // `cancelRefetch` lives on the *second* argument (InvalidateOptions), not
    // on the filters, and it defaults to true: with data already in the cache
    // an invalidation would cancel the in-flight GET /queue and start it over.
    // The backend emits a progress event per whole percent to every SSE client,
    // so a tab that does not yet know the job would restart the refetch on
    // every tick and, once the ticks outpace the round trip, never see it land.
    // Progress events therefore join the fetch already out. Every other event
    // still cancels: they are rare, and one may be a job's last word — a
    // terminal `error` for an unknown job must not join a fetch that left
    // before the job existed server-side, because nothing would re-invalidate
    // afterwards. Progress cannot be skipped altogether either: the SSE client
    // queue drops its oldest event when full, so a throttled background tab may
    // lose the `status_change` and have progress as its only signal.
    void queryClient.invalidateQueries(
      { queryKey: queryKeys.queue },
      { cancelRefetch: event.event !== "progress" },
    );
    return;
  }

  writeQueue(queryClient, (current) => {
    const idx = current.findIndex((j) => j.id === jobId);
    if (idx === -1) return current;

    const job = mergeSnapshot(current[idx], event.data);

    if (event.event === "error") {
      // The error event is the verdict even if the snapshot lags behind it.
      job.status = "error";
    } else if (event.event === "status_change" && job.status !== "error") {
      // Clear the error when the job transitions away from the error state.
      job.error = null;
    }

    if (TERMINAL_HIDDEN_STATUSES.has(job.status)) {
      recordDrop(queryClient, jobId);
      return current.filter((j) => j.id !== jobId);
    }

    const next = [...current];
    next[idx] = job;
    return next;
  });
}

/**
 * Refetch everything after the stream came back up: events emitted while it
 * was down were lost, and a backend restart may have restored jobs we never
 * heard about.
 *
 * `cancelRefetch: false` on both: all this needs is a fresh snapshot, and the
 * burst of progress events from restored jobs that follows a reconnect must not
 * keep cancelling and restarting the request that would deliver it.
 */
export function resyncAfterReconnect(queryClient: QueryClient): void {
  void queryClient.invalidateQueries(
    { queryKey: queryKeys.queue },
    { cancelRefetch: false },
  );
  void queryClient.invalidateQueries(
    { queryKey: queryKeys.library },
    { cancelRefetch: false },
  );
}
