import { useMemo } from "react";
import { RotateCw, Music, AlertCircle, Clock, Loader2, CheckCircle2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { Job, JobStatus } from "@/lib/types";

/** Sort jobs: active (downloading/converting) first, then newest-to-oldest by created_at. */
function sortJobs(jobs: Job[]): Job[] {
  return [...jobs].sort((a, b) => {
    const aActive = a.status === "downloading" || a.status === "converting";
    const bActive = b.status === "downloading" || b.status === "converting";

    // Active jobs always come first
    if (aActive && !bActive) return -1;
    if (!aActive && bActive) return 1;

    // Within the same group, sort newest first (descending by created_at)
    return b.created_at.localeCompare(a.created_at);
  });
}

interface QueueDisplayProps {
  jobs: Job[];
  onRetry: (jobId: string) => void;
}

/** Format seconds into MM:SS or HH:MM:SS. */
function formatDuration(seconds: number | null): string {
  if (seconds == null) return "--:--";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(sec).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

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

function JobItem({ job, onRetry }: { job: Job; onRetry: (id: string) => void }) {
  const showProgress = job.status === "downloading" || job.status === "converting";

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
          <StatusBadge status={job.status} />
        </div>

        {/* Progress bar */}
        {showProgress && <ProgressBar progress={job.progress} />}

        {/* Error message + retry */}
        {job.status === "error" && (
          <div className="flex items-center gap-2">
            <p className="min-w-0 flex-1 truncate text-xs text-destructive">
              {job.error ?? "An error occurred"}
            </p>
            <Button
              variant="outline"
              size="xs"
              onClick={() => onRetry(job.id)}
              className="shrink-0"
            >
              <RotateCw className="size-3" data-icon="inline-start" />
              Retry
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

export function QueueDisplay({ jobs, onRetry }: QueueDisplayProps) {
  const sorted = useMemo(() => sortJobs(jobs), [jobs]);

  if (sorted.length === 0) return null;

  return (
    <Card className="flex min-h-0 flex-1 flex-col">
      <CardHeader className="shrink-0">
        <CardTitle>Queue</CardTitle>
      </CardHeader>
      <CardContent className="min-h-0 flex-1 overflow-y-auto queue-scroll">
        <div className="flex flex-col gap-3">
          {sorted.map((job) => (
            <JobItem key={job.id} job={job} onRetry={onRetry} />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
