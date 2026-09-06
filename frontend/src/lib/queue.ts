import {
  ALREADY_IN_LIBRARY_PREFIX,
  type Job,
  type JobStatus,
} from "@/lib/types";

/** Statuses that keep a job at the top of the queue and count towards the badge. */
export const ACTIVE_STATUSES: ReadonlySet<JobStatus> = new Set<JobStatus>([
  "queued",
  "downloading",
  "converting",
  "tagging",
]);

/**
 * Statuses the backend accepts a cancel on. Anything else is terminal and the
 * endpoint answers 400, so offering the button there would only ever produce
 * an error the user cannot act on.
 */
export const CANCELLABLE_STATUSES: ReadonlySet<JobStatus> = ACTIVE_STATUSES;

/**
 * How many jobs are still working — the number on the Download tab badge.
 *
 * Top-level rows only. A bulk parent counts once however many children it is
 * running, because the badge counts the things the user submitted, and
 * `GET /queue` never lists a child at the top level anyway — a collection of
 * forty tracks must not read as forty downloads.
 */
export function countActiveJobs(jobs: readonly Job[] | undefined): number {
  if (!jobs) return 0;
  return jobs.reduce(
    (count, job) => (ACTIVE_STATUSES.has(job.status) ? count + 1 : count),
    0,
  );
}

/** Sort jobs: active first, then newest-to-oldest by created_at. */
export function sortJobs(jobs: readonly Job[]): Job[] {
  return [...jobs].sort((a, b) => {
    const aActive = ACTIVE_STATUSES.has(a.status);
    const bActive = ACTIVE_STATUSES.has(b.status);

    // Active jobs always come first
    if (aActive && !bActive) return -1;
    if (!aActive && bActive) return 1;

    // Within the same group, sort newest first (descending by created_at)
    return b.created_at.localeCompare(a.created_at);
  });
}

/**
 * Whether a job that ended as an error was in fact skipped.
 *
 * The backend has no "skipped" status: a download whose track is already under
 * the target artist, or whose Bandcamp seller has streaming turned off, ends
 * as an error carrying that reason. Nothing went wrong and retrying would fail
 * identically, so the queue reads it neutrally and offers no Retry.
 *
 * Which errors those are is the backend's answer (`Job.skipped`), not a list
 * of strings kept in step over here. The prefix check is only the fallback for
 * a backend too old to send the field — a rolling update, where the queue is
 * served by one version and the SSE stream by another.
 */
export function isSkipped(job: Job): boolean {
  if (job.skipped !== undefined) return job.skipped;
  return job.error?.startsWith(ALREADY_IN_LIBRARY_PREFIX) ?? false;
}

/** How a bulk parent's children are getting on, for its one-line summary. */
export interface ChildCounts {
  done: number;
  /** Errors that are real failures — the ones a Retry is offered for. */
  failed: number;
  /** Errors the backend marked as skips: nothing to retry. */
  skipped: number;
  cancelled: number;
  /** Queued, downloading, converting, or tagging: still to come. */
  active: number;
}

/**
 * Tally a bulk parent's children by outcome.
 *
 * The parent's own `progress_done`/`progress_total` say "3 of 12"; this is the
 * line under it that says what became of the other nine. Skips are split out
 * of the error count because a playlist where half the tracks were already
 * in the library has not half failed.
 *
 * A parent with no `children` (one built from an SSE snapshot before the
 * refetch has landed) tallies zeroes rather than throwing.
 */
export function childCounts(parent: Job): ChildCounts {
  const counts: ChildCounts = {
    done: 0,
    failed: 0,
    skipped: 0,
    cancelled: 0,
    active: 0,
  };

  for (const child of parent.children ?? []) {
    if (child.status === "done") counts.done += 1;
    else if (child.status === "cancelled") counts.cancelled += 1;
    else if (child.status === "error") {
      if (isSkipped(child)) counts.skipped += 1;
      else counts.failed += 1;
    } else if (ACTIVE_STATUSES.has(child.status)) counts.active += 1;
  }

  return counts;
}
