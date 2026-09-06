import { useCallback, useMemo, useState } from "react";
import {
  RotateCw,
  Music,
  AlertCircle,
  ChevronDown,
  ChevronRight,
  Clock,
  Loader2,
  CheckCircle2,
  ListMusic,
  X,
  Tags,
  Ban,
  SkipForward,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useQueueQuery } from "@/hooks/useQueueQuery";
import { formatDuration } from "@/lib/format";
import { useQueueActions } from "@/hooks/useQueueActions";
import {
  CANCELLABLE_STATUSES,
  childCounts,
  isSkipped,
  sortJobs,
  type ChildCounts,
} from "@/lib/queue";
import type { Job, JobStatus } from "@/lib/types";

const STATUS_CONFIG: Record<
  JobStatus,
  {
    label: string;
    variant: "default" | "secondary" | "destructive" | "outline";
    icon: React.ReactNode;
  }
> = {
  queued: {
    label: "Queued",
    variant: "secondary",
    icon: <Clock className="size-3" />,
  },
  downloading: {
    label: "Downloading",
    variant: "default",
    icon: <Loader2 className="size-3 animate-spin" />,
  },
  converting: {
    label: "Converting",
    variant: "default",
    icon: <Loader2 className="size-3 animate-spin" />,
  },
  tagging: {
    label: "Tagging",
    variant: "default",
    icon: <Tags className="size-3" />,
  },
  done: {
    label: "Done",
    variant: "outline",
    icon: <CheckCircle2 className="size-3" />,
  },
  error: {
    label: "Error",
    variant: "destructive",
    icon: <AlertCircle className="size-3" />,
  },
  cancelled: {
    label: "Cancelled",
    variant: "outline",
    icon: <Ban className="size-3" />,
  },
};

function StatusBadge({ status }: { status: JobStatus }) {
  const config = STATUS_CONFIG[status];
  return (
    <Badge variant={config.variant} className="gap-1">
      {config.icon}
      {config.label}
    </Badge>
  );
}

function ProgressBar({ progress }: { progress: number }) {
  return (
    <div className="flex items-center gap-2">
      <div className="relative h-2 flex-1 overflow-hidden rounded-full bg-secondary">
        <div
          className="h-full rounded-full bg-primary transition-all duration-300"
          style={{ width: `${Math.min(Math.max(progress, 0), 100)}%` }}
        />
      </div>
      <span className="w-10 text-right text-xs tabular-nums text-muted-foreground">
        {Math.round(progress)}%
      </span>
    </div>
  );
}

/**
 * The "3 of 12" a job that counts whole items shows instead of a percent bar.
 *
 * An album tagging run knows how many tracks it has written but nothing about
 * how far through the current one it is, so a bar would be a guess. `queued`
 * shows "0 of 12" honestly rather than hiding the size of the run.
 */
function CountProgress({ done, total }: { done: number; total: number }) {
  return (
    <span className="text-xs tabular-nums text-muted-foreground">
      {`${done} of ${total}`}
    </span>
  );
}

/** What a tagging row calls itself, in place of a download's duration. */
const TAGGING_LABEL = "Updating metadata";

/**
 * One track's row: a standalone download, a manual tagging run, or a child of
 * a bulk parent, which is the same thing with the same actions and only sits
 * somewhere else on the page.
 */
function JobRow({
  job,
  cancelPending,
  onRetry,
  onCancel,
  onDismiss,
}: {
  job: Job;
  cancelPending: boolean;
  onRetry: (id: string) => void;
  onCancel: (id: string) => void;
  onDismiss: (id: string) => void;
}) {
  const tagging = job.kind === "tagging";
  // Progress is only meaningful while downloading; the converting phase
  // (ffmpeg encode) reports nothing, so it shows a spinning badge instead.
  // A tagging job has no bytes at all — it counts tracks, and only an album
  // run has more than one to count.
  const showProgress = !tagging && job.status === "downloading";
  const total = job.progress_total ?? null;
  const canCancel = CANCELLABLE_STATUSES.has(job.status);
  // The backend ends two kinds of download as an error without anything
  // having gone wrong: one whose target file already exists, and a Bandcamp
  // track whose seller has streaming turned off. Retrying either would fail
  // the same way, so both read as a neutral "Skipped" with the reason in
  // muted text and no Retry.
  const skipped = isSkipped(job);

  return (
    <div className="flex gap-3 rounded-lg border p-3">
      {/* Thumbnail */}
      <div className="flex size-16 shrink-0 items-center justify-center overflow-hidden rounded-md bg-muted sm:size-20">
        {job.thumbnail_url ? (
          <img
            src={job.thumbnail_url}
            alt={job.title ?? "Thumbnail"}
            className="size-full object-cover"
            loading="lazy"
          />
        ) : tagging ? (
          // A tagging run has no thumbnail to fetch: the file is already in the
          // library, so the row is identified by its icon and its label.
          <Tags className="size-6 text-muted-foreground" />
        ) : (
          <Music className="size-6 text-muted-foreground" />
        )}
      </div>

      {/* Content */}
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        {/* Title + Status row */}
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium">
              {job.title ?? "Loading metadata..."}
            </p>
            <p className="text-xs text-muted-foreground">
              {tagging ? TAGGING_LABEL : formatDuration(job.duration)}
              {job.artist && ` \u00B7 ${job.artist}`}
              {job.album && ` \u00B7 ${job.album}`}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {skipped ? (
              <Badge variant="outline" className="gap-1">
                <SkipForward className="size-3" />
                Skipped
              </Badge>
            ) : (
              <StatusBadge status={job.status} />
            )}
            {canCancel && (
              <Button
                variant="ghost"
                size="icon-xs"
                aria-label={tagging ? "Cancel metadata update" : "Cancel download"}
                title={tagging ? "Cancel metadata update" : "Cancel download"}
                disabled={cancelPending}
                onClick={() => onCancel(job.id)}
              >
                <X className="size-3" />
              </Button>
            )}
          </div>
        </div>

        {/* Progress bar, or the N of M an album tagging run counts instead. */}
        {showProgress && <ProgressBar progress={job.progress} />}
        {!showProgress && total !== null && (
          <CountProgress done={job.progress_done ?? 0} total={total} />
        )}

        {/*
          The note a job that finished anyway carries — "tags not fixed: ..."
          on a download, "partial: 9 of 12" on an album tagging run. Muted, not
          destructive: nothing failed. Most `done` rows leave the view before
          it can be read, but an errored row that carries one keeps it visible.
        */}
        {job.detail != null && job.detail !== "" && (
          <p className="truncate text-xs text-muted-foreground">{job.detail}</p>
        )}

        {/*
          The message shows whenever the job carries one, not just in the error
          state: a failed Cancel or Dismiss writes into job.error while the job
          is still queued or running, and that has to be visible. Retry and
          Dismiss stay gated on the error state, since that is the only one the
          backend accepts them in.
        */}
        {(job.error !== null || job.status === "error") && (
          <div className="flex items-center gap-2">
            <p
              className={`min-w-0 flex-1 truncate text-xs ${
                skipped ? "text-muted-foreground" : "text-destructive"
              }`}
            >
              {job.error ?? "An error occurred"}
            </p>
            {job.status === "error" && !skipped && (
              <Button
                variant="outline"
                size="xs"
                onClick={() => onRetry(job.id)}
                className="shrink-0"
              >
                <RotateCw className="size-3" data-icon="inline-start" />
                Retry
              </Button>
            )}
            {job.status === "error" && (
              <Button
                variant="ghost"
                size="xs"
                onClick={() => onDismiss(job.id)}
                className="shrink-0"
                title="Remove this job from the queue"
              >
                <X className="size-3" data-icon="inline-start" />
                Dismiss
              </Button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * The one-line summary of what became of a bulk parent's children —
 * "3 done · 1 failed · 2 skipped · 4 active".
 *
 * Zero counts are left out rather than shown as "0 failed": a collection where
 * nothing went wrong should not read as a list of things that did not happen.
 * Null when there is nothing to say at all, which is a parent whose children
 * have not been fetched yet.
 */
function countsSummary(counts: ChildCounts): string | null {
  const parts = [
    counts.done > 0 ? `${counts.done} done` : null,
    counts.failed > 0 ? `${counts.failed} failed` : null,
    counts.skipped > 0 ? `${counts.skipped} skipped` : null,
    counts.cancelled > 0 ? `${counts.cancelled} cancelled` : null,
    counts.active > 0 ? `${counts.active} active` : null,
  ].filter((part): part is string => part !== null);
  return parts.length === 0 ? null : parts.join(" \u00B7 ");
}

/**
 * A collection download: one row for the whole thing, expandable to the tracks
 * under it.
 *
 * Collapsed by default — a forty-track playlist would otherwise bury every
 * other job in the queue — and the parent carries the actions that make sense
 * over the whole collection: Cancel cascades to every child still running, and
 * Dismiss (on a parent the backend has derived to `error`) deletes the parent
 * and its children together. There is deliberately no Retry: the endpoint
 * answers 400 for one on a parent, because what failed is a particular track
 * and that child's own Retry is what re-queues it.
 */
function BulkJobRow({
  job,
  expanded,
  onToggle,
  cancelling,
  onRetry,
  onCancel,
  onDismiss,
}: {
  job: Job;
  expanded: boolean;
  onToggle: (id: string) => void;
  cancelling: ReadonlySet<string>;
  onRetry: (id: string) => void;
  onCancel: (id: string) => void;
  onDismiss: (id: string) => void;
}) {
  const children = job.children ?? [];
  const counts = childCounts(job);
  const summary = countsSummary(counts);
  const total = job.progress_total ?? children.length;
  const canCancel = CANCELLABLE_STATUSES.has(job.status);
  // A parent whose only errors are skips has not failed: every track is
  // accounted for and there is nothing to retry. The parent's status stays
  // `error` (that is what keeps Dismiss available and the reason on the child
  // rows), but it reads as the same neutral "Skipped" a single row gets.
  const allSkipped =
    job.status === "error" && counts.failed === 0 && counts.skipped > 0;

  return (
    <div className="rounded-lg border">
      <div className="flex gap-3 p-3">
        <Button
          variant="ghost"
          size="icon-xs"
          className="mt-0.5 shrink-0"
          aria-expanded={expanded}
          aria-label={expanded ? "Hide tracks" : "Show tracks"}
          title={expanded ? "Hide tracks" : "Show tracks"}
          onClick={() => onToggle(job.id)}
        >
          {expanded ? (
            <ChevronDown className="size-3" />
          ) : (
            <ChevronRight className="size-3" />
          )}
        </Button>

        <div className="flex size-10 shrink-0 items-center justify-center overflow-hidden rounded-md bg-muted">
          {job.thumbnail_url ? (
            <img
              src={job.thumbnail_url}
              alt={job.title ?? "Thumbnail"}
              className="size-full object-cover"
              loading="lazy"
            />
          ) : (
            <ListMusic className="size-5 text-muted-foreground" />
          )}
        </div>

        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 flex-1">
              {/* A collection whose title the source did not give is still
                  identified by the URL it was submitted from. */}
              <p className="truncate text-sm font-medium">
                {job.title ?? job.url}
              </p>
              {job.artist !== null && (
                <p className="truncate text-xs text-muted-foreground">
                  {job.artist}
                </p>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              {allSkipped ? (
                <Badge variant="outline" className="gap-1">
                  <SkipForward className="size-3" />
                  Skipped
                </Badge>
              ) : (
                <StatusBadge status={job.status} />
              )}
              {canCancel && (
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label="Cancel bulk download"
                  title="Cancel every track still to download"
                  disabled={cancelling.has(job.id)}
                  onClick={() => onCancel(job.id)}
                >
                  <X className="size-3" />
                </Button>
              )}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-x-2">
            <CountProgress done={job.progress_done ?? 0} total={total} />
            {summary !== null && (
              <span className="text-xs text-muted-foreground">{summary}</span>
            )}
          </div>

          {/* Same as a single row: a refused Cancel writes its message here,
              whatever state the parent is in. Dismiss is gated on `error`,
              which is the only state the endpoint accepts it in. */}
          {(job.error !== null || job.status === "error") && (
            <div className="flex items-center gap-2">
              <p
                className={`min-w-0 flex-1 truncate text-xs ${
                  allSkipped && job.error === null
                    ? "text-muted-foreground"
                    : "text-destructive"
                }`}
              >
                {/* Not naming the reason: a skip is a track already in the
                    library or one whose Bandcamp seller has streaming turned
                    off, and a parent can hold both. Each track row carries its
                    own reason, which is where it belongs. */}
                {job.error ??
                  (allSkipped
                    ? `${counts.skipped} ${
                        counts.skipped === 1 ? "track was" : "tracks were"
                      } skipped`
                    : "Some tracks could not be downloaded")}
              </p>
              {job.status === "error" && (
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={() => onDismiss(job.id)}
                  className="shrink-0"
                  title="Remove this download and its tracks from the queue"
                >
                  <X className="size-3" data-icon="inline-start" />
                  Dismiss
                </Button>
              )}
            </div>
          )}
        </div>
      </div>

      {expanded && children.length > 0 && (
        <div className="flex flex-col gap-2 border-t p-3 pl-10">
          {children.map((child) => (
            <JobRow
              key={child.id}
              job={child}
              cancelPending={cancelling.has(child.id)}
              onRetry={onRetry}
              onCancel={onCancel}
              onDismiss={onDismiss}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * The in-flight queue, read straight from the `["queue"]` query cache that the
 * SSE stream patches. Nothing is passed in: the rows and their actions are the
 * same data wherever the component is mounted.
 */
export function QueueDisplay() {
  const { data: jobs, error } = useQueueQuery();
  const { cancelling, retryJob, cancelJob, dismissJob } = useQueueActions();
  /**
   * The bulk parents whose tracks are showing, by id. Component state and not
   * the cache: which rows are open is this browser tab's business, and keying
   * by id means a parent that leaves the queue takes nothing with it.
   */
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());

  const toggleExpanded = useCallback((jobId: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (!next.delete(jobId)) next.add(jobId);
      return next;
    });
  }, []);

  const sorted = useMemo(() => sortJobs(jobs ?? []), [jobs]);
  const errorMessage =
    error === null
      ? null
      : error instanceof Error
        ? error.message
        : "Failed to load the queue";

  // The error only replaces the list when there is no list to show. useQuery
  // keeps the last data through a failed background refetch, so one 502 during
  // a backend restart must not blank out every in-flight row — the message goes
  // in the header instead and the rows stay put. With nothing to show, an
  // empty queue renders nothing at all and a failure renders the message,
  // whether the cache is unfetched or fetched-and-empty.
  if (sorted.length === 0) {
    if (errorMessage === null) return null;
    return (
      <Card className="shrink-0">
        <CardContent>
          <p className="text-sm text-destructive">{errorMessage}</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="flex min-h-0 flex-1 flex-col">
      <CardHeader className="shrink-0">
        <CardTitle>In flight</CardTitle>
        {errorMessage !== null && (
          <p className="text-xs text-destructive">{errorMessage}</p>
        )}
      </CardHeader>
      <CardContent className="min-h-0 flex-1 overflow-y-auto queue-scroll">
        <div className="flex flex-col gap-3">
          {sorted.map((job) =>
            job.kind === "bulk" ? (
              <BulkJobRow
                key={job.id}
                job={job}
                expanded={expanded.has(job.id)}
                onToggle={toggleExpanded}
                cancelling={cancelling}
                onRetry={retryJob}
                onCancel={cancelJob}
                onDismiss={dismissJob}
              />
            ) : (
              <JobRow
                key={job.id}
                job={job}
                cancelPending={cancelling.has(job.id)}
                onRetry={retryJob}
                onCancel={cancelJob}
                onDismiss={dismissJob}
              />
            ),
          )}
        </div>
      </CardContent>
    </Card>
  );
}
