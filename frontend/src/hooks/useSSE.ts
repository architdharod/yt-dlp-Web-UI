import { useCallback, useEffect, useRef, useState } from "react";
import { getQueue, connectQueueStream, retryJob } from "@/lib/api";
import type { Job, SSEEvent } from "@/lib/types";

/**
 * Custom hook that manages queue state with real-time SSE updates.
 *
 * On mount:
 *   1. Fetches the current queue state via GET /queue
 *   2. Opens an SSE connection to GET /queue/stream for incremental updates
 *
 * Returns the current jobs list, a retry handler, and connection status.
 */
export function useSSE() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<(() => void) | null>(null);

  /** Apply an SSE event to the local jobs state. */
  const handleEvent = useCallback((event: SSEEvent) => {
    setJobs((prev) => {
      const idx = prev.findIndex((j) => j.id === event.job_id);
      if (idx === -1) return prev;

      const updated = [...prev];
      const job = { ...updated[idx] };

      switch (event.event) {
        case "status_change":
          if (typeof event.data.status === "string") {
            job.status = event.data.status as Job["status"];
          }
          if (typeof event.data.progress === "number") {
            job.progress = event.data.progress;
          }
          // Clear error when transitioning away from error state
          if (job.status !== "error") {
            job.error = null;
          }
          break;

        case "progress":
          if (typeof event.data.progress === "number") {
            job.progress = event.data.progress;
          }
          if (typeof event.data.status === "string") {
            job.status = event.data.status as Job["status"];
          }
          break;

        case "error":
          job.status = "error";
          if (typeof event.data.error === "string") {
            job.error = event.data.error;
          }
          if (typeof event.data.progress === "number") {
            job.progress = event.data.progress;
          }
          break;

        case "metadata":
          if (typeof event.data.title === "string") {
            job.title = event.data.title;
          }
          if (typeof event.data.thumbnail_url === "string") {
            job.thumbnail_url = event.data.thumbnail_url;
          }
          if (typeof event.data.duration === "number") {
            job.duration = event.data.duration;
          }
          break;
      }

      updated[idx] = job;
      return updated;
    });
  }, []);

  /** Add a newly created job to the local state. */
  const addJob = useCallback((job: Job) => {
    setJobs((prev) => [...prev, job]);
  }, []);

  /** Retry a failed job via the API and update local state. */
  const handleRetry = useCallback(async (jobId: string) => {
    try {
      const updatedJob = await retryJob(jobId);
      setJobs((prev) =>
        prev.map((j) => (j.id === updatedJob.id ? updatedJob : j)),
      );
    } catch (err) {
      // The retry API call itself failed — surface that in the job's error
      setJobs((prev) =>
        prev.map((j) =>
          j.id === jobId
            ? {
                ...j,
                error: err instanceof Error ? err.message : "Retry failed",
              }
            : j,
        ),
      );
    }
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function init() {
      try {
        // 1. Fetch current queue state
        const currentJobs = await getQueue();
        if (cancelled) return;
        setJobs(currentJobs);
        setError(null);

        // 2. Open SSE connection for real-time updates
        const close = connectQueueStream(
          (event) => {
            if (!cancelled) handleEvent(event);
          },
          () => {
            if (!cancelled) {
              setConnected(false);
            }
          },
        );
        closeRef.current = close;
        if (!cancelled) setConnected(true);
      } catch (err) {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : "Failed to connect to queue",
          );
          setConnected(false);
        }
      }
    }

    init();

    return () => {
      cancelled = true;
      closeRef.current?.();
      closeRef.current = null;
    };
  }, [handleEvent]);

  return { jobs, connected, error, addJob, retryJob: handleRetry };
}
