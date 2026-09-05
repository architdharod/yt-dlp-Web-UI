import type { QueryClient } from "@tanstack/react-query";
import { queryKeys } from "@/lib/queryKeys";
import type { Job, Notice, SSEEvent } from "@/lib/types";

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
 * *Additions* are protected by the mirror set below, for the same reason in
 * reverse: a `POST /download` answers while a `GET /queue` issued before the
 * job existed is still on the wire, and that older snapshot would write the
 * brand-new row back out of the cache. Nothing would put it back either — the
 * backend emits no event for a `queued` job until a slot frees, so the row
 * would simply be missing until the download starts. Only the *id* is kept, so
 * the re-append reads the current cached copy rather than replaying a frozen
 * one over fresher stream state.
 *
 * The two sets are mutually exclusive and the later record wins: an add
 * followed by a drop is a drop, a drop followed by an add is an add.
 *
 * Other in-place patches are not protected. The fresher job
 * `replaceJobInCache` writes from a retry response, or the advisory text
 * `setJobActionError` puts on a row, can still be overwritten by a snapshot
 * that was issued before the patch. That is accepted: the stream re-delivers
 * the job's true state on its next transition, and the action message is only
 * advisory. A "patched since this fetch went out" map was considered and
 * rejected, because replaying a frozen copy of the patch over the response
 * would clobber any fresher stream state applied in between.
 */
const droppedSinceFetch = new WeakMap<QueryClient, Set<string>>();

/** Ids added to the cache since the last `GET /queue` went out. See above. */
const addedSinceFetch = new WeakMap<QueryClient, Set<string>>();

/** The set held in *store* for *queryClient*, created on first use. */
function idsFor(
  store: WeakMap<QueryClient, Set<string>>,
  queryClient: QueryClient,
): Set<string> {
  let ids = store.get(queryClient);
  if (ids === undefined) {
    ids = new Set<string>();
    store.set(queryClient, ids);
  }
  return ids;
}

/** The drop set for *queryClient*, created on first use. */
function droppedIds(queryClient: QueryClient): Set<string> {
  return idsFor(droppedSinceFetch, queryClient);
}

/** The add set for *queryClient*, created on first use. */
function addedIds(queryClient: QueryClient): Set<string> {
  return idsFor(addedSinceFetch, queryClient);
}

/** Note that *jobId* left the cache, so a fetch already out cannot restore it. */
function recordDrop(queryClient: QueryClient, jobId: string): void {
  addedIds(queryClient).delete(jobId);
  droppedIds(queryClient).add(jobId);
}

/** Note that *jobId* joined the cache, so a fetch already out cannot erase it. */
function recordAdd(queryClient: QueryClient, jobId: string): void {
  droppedIds(queryClient).delete(jobId);
  addedIds(queryClient).add(jobId);
}

/** Called by the queue `queryFn` as it issues the request. */
export function beginQueueFetch(queryClient: QueryClient): void {
  droppedIds(queryClient).clear();
  addedIds(queryClient).clear();
}

/**
 * Reconcile a fetched queue with the cache changes that happened while it was
 * away: drop the ids removed since, and re-append the ids added since that the
 * response does not already list.
 *
 * The re-appended job is taken from the *current* cache, not from a copy saved
 * at add time, so a job the stream has advanced in the meantime keeps the
 * fresher state. One the stream has since removed is not in the cache at all
 * and so is not re-appended.
 */
export function reconcileQueueSnapshot(
  queryClient: QueryClient,
  jobs: Job[],
): Job[] {
  const dropped = droppedIds(queryClient);
  const added = addedIds(queryClient);

  const kept = dropped.size === 0 ? jobs : jobs.filter((job) => !dropped.has(job.id));
  if (added.size === 0) return kept;

  const cached = queryClient.getQueryData<Job[]>(queryKeys.queue) ?? [];
  const present = new Set(kept.map((job) => job.id));
  const missing = cached.filter(
    (job) => added.has(job.id) && !present.has(job.id),
  );
  return missing.length === 0 ? kept : [...kept, ...missing];
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
  recordAdd(queryClient, job.id);

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
 * Read one element of a `notices` event's list, or null when it is malformed.
 *
 * The event data is `Record<string, unknown>` on the wire, so every field is
 * checked rather than cast: a malformed entry should be ignored, not rendered
 * as a banner with `undefined` in it.
 */
function parseNotice(data: Record<string, unknown>): Notice | null {
  const { id, level, source, message, created_at: createdAt } = data;
  if (typeof id !== "string" || id.length === 0) return null;
  if (level !== "error" && level !== "warning") return null;
  if (source !== "navidrome" && source !== "lidarr") return null;
  if (typeof message !== "string") return null;
  if (typeof createdAt !== "string") return null;
  return { id, level, source, message, created_at: createdAt };
}

/**
 * Replace the cached notice list from a `notices` event.
 *
 * The event carries the *whole* open set, so this overwrites rather than
 * merges: a notice the backend has cleared is simply absent from the next
 * push, and that is the only thing that makes it leave the client. Elements
 * that do not parse are dropped, the rest still render.
 *
 * An unfetched cache is seeded rather than left alone: `GET /notices` may
 * still be in flight, and a banner the user should see must not wait for it.
 */
function applyNoticesEvent(
  queryClient: QueryClient,
  data: Record<string, unknown>,
): void {
  const raw = data.notices;
  if (!Array.isArray(raw)) return;

  const notices = raw
    .map((entry) =>
      typeof entry === "object" && entry !== null
        ? parseNotice(entry as Record<string, unknown>)
        : null,
    )
    .filter((notice): notice is Notice => notice !== null);

  // `useNotices` sets no `staleTime`, so a mount or refocus refetch may be out
  // right now; it would write its older answer over this push when it lands.
  // Cancelling is synchronous inside the call, so there is nothing to await:
  // the cancelled query settles at the value written just below and does not
  // refetch itself, while a later `invalidateQueries` still refetches normally.
  void queryClient.cancelQueries({ queryKey: queryKeys.notices });
  queryClient.setQueryData<Notice[]>(queryKeys.notices, notices);
}

/**
 * Apply one SSE event to the query cache.
 *
 * A plain function over a `QueryClient` rather than something inside the hook,
 * so the whole event-to-cache contract is testable without an EventSource.
 *
 *   - `library_changed` invalidates the library and the trash queries: a
 *     delete, a restore, or an empty-trash changes both, and the event does
 *     not say which of them happened.
 *   - `notices` replaces the notices query with the open set the event
 *     carries, so a service failure paints a banner — and a cleared one stops
 *     being shown — without a refetch.
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
    void queryClient.invalidateQueries({ queryKey: queryKeys.trash });
    return;
  }

  if (event.event === "notices") {
    applyNoticesEvent(queryClient, event.data);
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
  void queryClient.invalidateQueries(
    { queryKey: queryKeys.trash },
    { cancelRefetch: false },
  );
  // A notice raised while the stream was down was never delivered, and one the
  // backend has since cleared should stop being shown.
  void queryClient.invalidateQueries(
    { queryKey: queryKeys.notices },
    { cancelRefetch: false },
  );
}
