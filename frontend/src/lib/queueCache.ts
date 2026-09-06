import type { QueryClient } from "@tanstack/react-query";
import { queryKeys } from "@/lib/queryKeys";
import type { Job, Notice, NoticeAction, SSEEvent } from "@/lib/types";

/**
 * Statuses `GET /queue` does not return. A job that reaches one of them is
 * dropped from the cache straight away rather than waiting for a refetch, so
 * the in-flight view matches what the endpoint would answer right now.
 */
const TERMINAL_HIDDEN_STATUSES: ReadonlySet<Job["status"]> = new Set<
  Job["status"]
>(["done", "cancelled"]);

/**
 * Where a job sits in the cached list.
 *
 * `GET /queue` is two levels deep and no deeper: top-level rows, and the
 * `children` of a bulk parent. Every patch below therefore has to say which of
 * the two it found, because the rules differ — a top-level job that reaches
 * `done` leaves the cache, while a child that reaches `done` stays under its
 * parent, since the endpoint keeps listing it there.
 */
interface JobLocation {
  /** Index within the top-level list, or within the parent's `children`. */
  index: number;
  /** The parent's index in the top-level list, or null for a top-level job. */
  parentIndex: number | null;
}

/**
 * Find a job by id at either level.
 *
 * *parentId* is the `parent_id` an SSE event carries: given one, the search
 * goes straight to that parent and looks no further, which is what keeps a
 * child event off a scan of every parent's children. Without one — an action
 * taken from a row, which knows only the id it clicked — the top level is
 * tried first and the children are then scanned.
 */
function locateJob(
  jobs: readonly Job[],
  jobId: string,
  parentId?: string | null,
): JobLocation | null {
  const childIn = (parentIndex: number): JobLocation | null => {
    const index =
      jobs[parentIndex].children?.findIndex((c) => c.id === jobId) ?? -1;
    return index === -1 ? null : { index, parentIndex };
  };

  if (typeof parentId === "string") {
    const parentIndex = jobs.findIndex((j) => j.id === parentId);
    return parentIndex === -1 ? null : childIn(parentIndex);
  }

  const index = jobs.findIndex((j) => j.id === jobId);
  if (index !== -1) return { index, parentIndex: null };

  for (let i = 0; i < jobs.length; i += 1) {
    const found = childIn(i);
    if (found !== null) return found;
  }
  return null;
}

/** The job *location* points at. */
function jobAt(jobs: readonly Job[], location: JobLocation): Job {
  return location.parentIndex === null
    ? jobs[location.index]
    : jobs[location.parentIndex].children![location.index];
}

/**
 * Rewrite the job at *location* — or drop it, for a null *next* — leaving
 * every other job's identity intact so React re-renders only what changed.
 *
 * A patched child rebuilds its parent object, which is unavoidable: the parent
 * row shows counts over its children, so it has to change when one of them
 * does.
 */
function withJobAt(
  jobs: readonly Job[],
  location: JobLocation,
  next: Job | null,
): Job[] {
  if (location.parentIndex === null) {
    if (next === null) return jobs.filter((_, i) => i !== location.index);
    const out = [...jobs];
    out[location.index] = next;
    return out;
  }

  const parent = jobs[location.parentIndex];
  const children = parent.children!;
  const out = [...jobs];
  out[location.parentIndex] = {
    ...parent,
    children:
      next === null
        ? children.filter((_, i) => i !== location.index)
        : children.map((c, i) => (i === location.index ? next : c)),
  };
  return out;
}

/**
 * Apply *patch* to the job *jobId* wherever it sits. A patch answering null
 * removes the job; an id unknown at both levels leaves the list untouched.
 */
function patchJob(
  jobs: readonly Job[],
  jobId: string,
  parentId: string | null | undefined,
  patch: (job: Job) => Job | null,
): Job[] {
  const location = locateJob(jobs, jobId, parentId);
  if (location === null) return jobs as Job[];
  return withJobAt(jobs, location, patch(jobAt(jobs, location)));
}

/**
 * The id of the bulk parent holding *jobId*, or null when it is a top-level
 * job or is not cached at all.
 *
 * The dismiss action needs it: the backend may delete the parent along with
 * the last child that was not `done`, so a child dismiss has to be followed by
 * a refetch, while a top-level dismiss does not.
 */
export function parentIdOfCachedJob(
  queryClient: QueryClient,
  jobId: string,
): string | null {
  const jobs = readQueue(queryClient);
  if (jobs === undefined) return null;
  const location = locateJob(jobs, jobId);
  return location === null || location.parentIndex === null
    ? null
    : jobs[location.parentIndex].id;
}

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
/**
 * A parent without the children dropped since the fetch went out.
 *
 * A dismissed child is deleted server-side, but a `GET /queue` that left
 * before the dismiss still nests it under its parent — and the response is
 * written into the cache unconditionally, so without this the row would come
 * back and never leave again.
 */
function withoutDroppedChildren(job: Job, dropped: ReadonlySet<string>): Job {
  const children = job.children;
  if (children === undefined) return job;
  const kept = children.filter((child) => !dropped.has(child.id));
  return kept.length === children.length ? job : { ...job, children: kept };
}

export function reconcileQueueSnapshot(
  queryClient: QueryClient,
  jobs: Job[],
): Job[] {
  const dropped = droppedIds(queryClient);
  const added = addedIds(queryClient);

  const kept =
    dropped.size === 0
      ? jobs
      : jobs
          .filter((job) => !dropped.has(job.id))
          .map((job) => withoutDroppedChildren(job, dropped));
  if (added.size === 0) return kept;

  const cached = queryClient.getQueryData<Job[]>(queryKeys.queue) ?? [];
  const present = new Set(kept.map((job) => job.id));
  const missing = cached.filter(
    (job) => added.has(job.id) && !present.has(job.id),
  );
  return missing.length === 0 ? kept : [...kept, ...missing];
}

/**
 * Read a snapshot field that is a number or an explicit null.
 *
 * `undefined` (the field absent) leaves the job's current value alone, but an
 * explicit `null` is a value: a tagging job that could not count its tracks
 * sends `progress_total: null`, and treating that as "absent" would leave a
 * stale "3 of 12" on the row.
 */
function numberOrNull(
  value: unknown,
  current: number | null | undefined,
): number | null | undefined {
  if (typeof value === "number") return value;
  if (value === null) return null;
  return current;
}

/**
 * Merge the job snapshot fields carried by every SSE event into a job.
 *
 * The backend's `_emit_event` sends status, progress, title, thumbnail_url,
 * duration, artist and album on every event type, plus the N-of-M counters and
 * `skipped`, `detail` and `retry_at`, and `error` when it is set. `kind`,
 * `parent_id` and `path` are *not* merged — they never change over a job's life, so the cached
 * row keeps whatever `GET /queue` (or the creating response) gave it. Neither
 * are `children`: a parent's synthetic `status_change` carries its own derived
 * fields only, and the children it holds are patched by their own events.
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
  if (data.progress_done !== undefined) {
    merged.progress_done = numberOrNull(data.progress_done, job.progress_done);
  }
  if (data.progress_total !== undefined) {
    merged.progress_total = numberOrNull(
      data.progress_total,
      job.progress_total,
    );
  }
  if (typeof data.error === "string") {
    merged.error = data.error;
  }
  // Alongside the error, because it is the backend's verdict *on* that error:
  // a job cached while it was queued carries `skipped: false`, and the event
  // that ends it as a duplicate or a Bandcamp track with streaming off is the
  // only thing that says otherwise until the next `GET /queue`. Without this
  // the row would offer a Retry that cannot work.
  if (typeof data.skipped === "boolean") {
    merged.skipped = data.skipped;
  }
  // The note, and the instant a rate-limited job will try again. Both are
  // always present on the wire and null when there is nothing to say, because
  // both are things the backend takes *back*: a wait ends, and the row must
  // stop showing "retry 2 of 5 in 45 s". "There is no note" and "this event
  // does not mention the note" therefore have to be tellable apart, which is
  // what an explicit null does and an absent key cannot.
  if (data.detail !== undefined) {
    merged.detail = typeof data.detail === "string" ? data.detail : null;
  }
  if (data.retry_at !== undefined) {
    merged.retry_at = typeof data.retry_at === "string" ? data.retry_at : null;
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

/**
 * Replace a job with a fresher copy from an API response.
 *
 * The copy carries its own `parent_id`, so a retried child goes back under the
 * parent it came from rather than being appended to the top level.
 */
export function replaceJobInCache(queryClient: QueryClient, job: Job): void {
  writeQueue(queryClient, (jobs) =>
    patchJob(jobs, job.id, job.parent_id, () => job),
  );
}

/**
 * Drop a job from the in-flight view — a top-level row, or one child of a bulk
 * parent, which is what a dismissed child is: the backend deleted that row and
 * left the parent standing.
 */
export function removeJobFromCache(
  queryClient: QueryClient,
  jobId: string,
): void {
  recordDrop(queryClient, jobId);
  writeQueue(queryClient, (jobs) => patchJob(jobs, jobId, undefined, () => null));
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
    patchJob(jobs, jobId, undefined, (job) => ({ ...job, error: message })),
  );
}

/**
 * Read one element of a `notices` event's list, or null when it is malformed.
 *
 * The event data is `Record<string, unknown>` on the wire, so every field is
 * checked rather than cast: a malformed entry should be ignored, not rendered
 * as a banner with `undefined` in it.
 */
const NOTICE_SOURCES: ReadonlySet<string> = new Set([
  "navidrome",
  "lidarr",
  "youtube",
  "soundcloud",
  "bandcamp",
]);

/**
 * A path that is not a path: `//host/x` and `/\host\x` are protocol-relative
 * URLs that a browser resolves against another origin.
 */
const NOT_A_PATH = /^\/[/\\]/;

/** Read a notice's optional action, or null when it has none or is malformed. */
export function parseNoticeAction(value: unknown): NoticeAction | null {
  if (typeof value !== "object" || value === null) return null;
  const { label, method, path } = value as Record<string, unknown>;
  if (typeof label !== "string" || label.length === 0) return null;
  if (method !== "POST") return null;
  // An absolute path on this API and nothing else. The banner turns this
  // straight into a request against `VITE_API_BASE_URL`, so it is checked for
  // shape rather than compared against `location.origin` — that base URL is
  // allowed to be another origin, and often is in development.
  if (typeof path !== "string" || !path.startsWith("/")) return null;
  if (NOT_A_PATH.test(path)) return null;
  return { label, method, path };
}

/** Read one notice off the wire, or null when it is malformed. */
export function parseNotice(data: Record<string, unknown>): Notice | null {
  const {
    id,
    level,
    source,
    message,
    hold_until: holdUntil,
    reason,
    held_since: heldSince,
    action,
    created_at: createdAt,
  } = data;
  if (typeof id !== "string" || id.length === 0) return null;
  if (level !== "error" && level !== "warning") return null;
  if (typeof source !== "string" || !NOTICE_SOURCES.has(source)) return null;
  if (typeof message !== "string") return null;
  if (typeof createdAt !== "string") return null;
  return {
    id,
    level,
    source: source as Notice["source"],
    message,
    hold_until: typeof holdUntil === "string" ? holdUntil : null,
    reason: typeof reason === "string" ? reason : null,
    held_since: typeof heldSince === "string" ? heldSince : null,
    action: parseNoticeAction(action),
    created_at: createdAt,
  };
}

/** Read a whole list of notices off the wire, dropping the malformed ones. */
export function parseNotices(raw: unknown): Notice[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((entry) =>
      typeof entry === "object" && entry !== null
        ? parseNotice(entry as Record<string, unknown>)
        : null,
    )
    .filter((notice): notice is Notice => notice !== null);
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

  const notices = parseNotices(raw);

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
 *   - An event for a job the cache does not hold at *either* level — top row
 *     or child of a bulk parent — means the view is stale (submitted from
 *     another tab, or restored after a backend restart), so the queue query is
 *     invalidated; the refetch brings the whole parent back with the child in
 *     it. The snapshot in the event is deliberately not
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
  // Every job event carries the parent it belongs to (null when it has none),
  // which is what takes a child's event straight to the parent holding it.
  const parentId =
    typeof event.data.parent_id === "string" ? event.data.parent_id : undefined;
  if (
    jobId === null ||
    jobs === undefined ||
    locateJob(jobs, jobId, parentId) === null
  ) {
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
    const location = locateJob(current, jobId, parentId);
    if (location === null) return current;

    const job = mergeSnapshot(jobAt(current, location), event.data);

    if (event.event === "error") {
      // The error event is the verdict even if the snapshot lags behind it.
      job.status = "error";
    } else if (event.event === "status_change" && job.status !== "error") {
      // Clear the error when the job transitions away from the error state.
      job.error = null;
      // Back to `queued` is a retry: the note from the previous run ("tags not
      // fixed: no match") describes an attempt that is over, and no event
      // clears it otherwise, so a client that did not press Retry would keep
      // showing it for the whole new run.
      if (job.status === "queued") job.detail = null;
    }

    // A finished top-level row leaves, because `GET /queue` omits it. A
    // finished *child* stays: the endpoint nests every child of a bulk parent
    // whatever its status, and the parent's counts are read off them — drop
    // the done ones and "3 done · 1 failed" would come out as "1 failed".
    if (
      location.parentIndex === null &&
      TERMINAL_HIDDEN_STATUSES.has(job.status)
    ) {
      recordDrop(queryClient, jobId);
      return withJobAt(current, location, null);
    }

    return withJobAt(current, location, job);
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
