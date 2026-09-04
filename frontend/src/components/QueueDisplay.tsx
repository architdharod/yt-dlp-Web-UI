import { useMemo } from "react";
import {
  RotateCw,
  Music,
  AlertCircle,
  Clock,
  Loader2,
  CheckCircle2,
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
import { CANCELLABLE_STATUSES, sortJobs } from "@/lib/queue";
import {
  ALREADY_IN_LIBRARY_PREFIX,
  type Job,
  type JobStatus,
} from "@/lib/types";

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

function JobItem({
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
  // Progress is only meaningful while downloading; the converting phase
  // (ffmpeg encode) reports nothing, so it shows a spinning badge instead.
  const showProgress = job.status === "downloading";
  const canCancel = CANCELLABLE_STATUSES.has(job.status);
  // The backend ends a download whose target file already exists as an error,
  // but nothing went wrong and retrying would fail the same way — so it reads
  // as a neutral "Skipped" with the reason in muted text and no Retry.
  const skipped = job.error?.startsWith(ALREADY_IN_LIBRARY_PREFIX) ?? false;

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
              {formatDuration(job.duration)}
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
                aria-label="Cancel download"
                title="Cancel download"
                disabled={cancelPending}
                onClick={() => onCancel(job.id)}
              >
                <X className="size-3" />
              </Button>
            )}
          </div>
        </div>

        {/* Progress bar */}
        {showProgress && <ProgressBar progress={job.progress} />}

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
 * The in-flight queue, read straight from the `["queue"]` query cache that the
 * SSE stream patches. Nothing is passed in: the rows and their actions are the
 * same data wherever the component is mounted.
 */
export function QueueDisplay() {
  const { data: jobs, error } = useQueueQuery();
  const { cancelling, retryJob, cancelJob, dismissJob } = useQueueActions();

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
          {sorted.map((job) => (
            <JobItem
              key={job.id}
              job={job}
              cancelPending={cancelling.has(job.id)}
              onRetry={retryJob}
              onCancel={cancelJob}
              onDismiss={dismissJob}
            />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
