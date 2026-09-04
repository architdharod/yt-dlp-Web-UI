import type { Job, JobStatus } from "@/lib/types";

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

/** How many jobs are still working — the number on the Download tab badge. */
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
