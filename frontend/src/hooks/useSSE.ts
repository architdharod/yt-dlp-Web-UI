import { useCallback, useEffect, useRef, useState } from "react";
import {
  getQueue,
  connectQueueStream,
  retryJob,
  cancelJob,
  dismissJob,
} from "@/lib/api";
import type { Job, SSEEvent } from "@/lib/types";

/**
 * Merge the job snapshot fields carried by every SSE event into a job.
 * The backend sends status/progress/title/thumbnail_url/duration/artist/album
 * (and error when set) on every event type, so any event can refresh them.
 */
function mergeSnapshot(job: Job, data: Record<string, unknown>): Job {
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

/**
 * Custom hook that manages queue state with real-time SSE updates.
 *
 * On mount:
 *   1. Fetches the current queue state via GET /queue
 *   2. Opens an SSE connection to GET /queue/stream for incremental updates
 *
 * The local state is reconciled with GET /queue whenever an event arrives for
 * a job we do not know about (submitted from another tab, or a response we
 * never saw) and whenever the stream reconnects after a drop.
 *
 * Returns the current jobs list, the row action handlers, and connection
 * status.
 */
export function useSSE() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /**
   * Ids whose cancel request is in flight. The row's Cancel button is disabled
   * while its id is in here, so a double click cannot send two requests (the
   * second would answer 400 once the first one landed).
   */
  const [cancelling, setCancelling] = useState<Set<string>>(new Set());

  const closeRef = useRef<(() => void) | null>(null);
  const cancelledRef = useRef(false);
  /** Ids currently in state — read synchronously by the event handler. */
  const knownIdsRef = useRef<Set<string>>(new Set());
  /** True while a reconciliation refetch is in flight (coalesces concurrent ones). */
  const refetchingRef = useRef(false);
  /** True once the stream has errored, so the next onopen is a reconnect. */
  const hadErrorRef = useRef(false);

  /** Replace the jobs state, keeping the id index in sync. */
  const replaceJobs = useCallback((next: Job[]) => {
    knownIdsRef.current = new Set(next.map((j) => j.id));
    setJobs(next);
  }, []);

  /** Refetch the whole queue and replace local state. At most one in flight. */
  const resync = useCallback(async () => {
    if (refetchingRef.current) return;
    refetchingRef.current = true;
    try {
      const currentJobs = await getQueue();
      if (cancelledRef.current) return;
      replaceJobs(currentJobs);
      setError(null);
    } catch (err) {
      if (cancelledRef.current) return;
      setError(err instanceof Error ? err.message : "Failed to refresh queue");
    } finally {
      refetchingRef.current = false;
    }
  }, [replaceJobs]);

  /** Apply an SSE event to the local jobs state. */
  const handleEvent = useCallback(
    (event: SSEEvent) => {
      // An event for a job we have never seen means our view is stale.
      if (!knownIdsRef.current.has(event.job_id)) {
        void resync();
        return;
      }

      setJobs((prev) => {
        const idx = prev.findIndex((j) => j.id === event.job_id);
        if (idx === -1) return prev;

        const updated = [...prev];
        const job = mergeSnapshot(updated[idx], event.data);

        switch (event.event) {
          case "status_change":
            // Clear error when transitioning away from the error state
            if (job.status !== "error") {
              job.error = null;
            }
            // A running job only reaches "cancelled" once its thread has
            // actually stopped, which is the moment it leaves GET /queue —
            // so the row goes here rather than when the button was clicked.
            //
            // Note the asymmetry with "done": GET /queue omits both, but a
            // done row is deliberately kept in local state as completion
            // feedback and only disappears at the next resync or reload,
            // whereas a cancelled row goes immediately because the user just
            // asked for it to be gone. Phase 3 replaces this hand-rolled state
            // with a query-cache-backed in-flight view, which supersedes the
            // divergence.
            if (job.status === "cancelled") {
              knownIdsRef.current.delete(job.id);
              return prev.filter((j) => j.id !== event.job_id);
            }
            break;

          case "error":
            job.status = "error";
            break;
        }

        updated[idx] = job;
        return updated;
      });
    },
    [resync],
  );

  /** Add a newly created job to the local state (no-op if already present). */
  const addJob = useCallback((job: Job) => {
    setJobs((prev) => {
      if (prev.some((j) => j.id === job.id)) return prev;
      return [...prev, job];
    });
    knownIdsRef.current.add(job.id);
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

  /** Surface a failed row action as the job's own error text. */
  const showActionError = useCallback(
    (jobId: string, err: unknown, fallback: string) => {
      setJobs((prev) =>
        prev.map((j) =>
          j.id === jobId
            ? { ...j, error: err instanceof Error ? err.message : fallback }
            : j,
        ),
      );
    },
    [],
  );

  /** Cancel a queued or running job. */
  const handleCancel = useCallback(
    async (jobId: string) => {
      setCancelling((prev) => {
        const next = new Set(prev);
        next.add(jobId);
        return next;
      });
      try {
        const updatedJob = await cancelJob(jobId);
        // A queued job comes back already cancelled and can go now. A running
        // one is only signalled, so its row stays until the SSE status_change
        // says the thread has stopped — and the response is deliberately not
        // written into state: it is a snapshot from before the request, so it
        // would revert whatever the stream has delivered since (a job that has
        // reached "converting" would show "downloading" until it finished).
        if (updatedJob.status === "cancelled") {
          knownIdsRef.current.delete(jobId);
          setJobs((prev) => prev.filter((j) => j.id !== jobId));
        }
      } catch (err) {
        showActionError(jobId, err, "Cancel failed");
      } finally {
        setCancelling((prev) => {
          const next = new Set(prev);
          next.delete(jobId);
          return next;
        });
      }
    },
    [showActionError],
  );

  /** Dismiss an errored job — removed locally only once the backend agreed. */
  const handleDismiss = useCallback(
    async (jobId: string) => {
      try {
        await dismissJob(jobId);
        knownIdsRef.current.delete(jobId);
        setJobs((prev) => prev.filter((j) => j.id !== jobId));
      } catch (err) {
        showActionError(jobId, err, "Dismiss failed");
      }
    },
    [showActionError],
  );

  useEffect(() => {
    cancelledRef.current = false;

    async function init() {
      try {
        // 1. Fetch current queue state
        const currentJobs = await getQueue();
        if (cancelledRef.current) return;
        replaceJobs(currentJobs);
        setError(null);

        // 2. Open SSE connection for real-time updates
        const close = connectQueueStream(
          (event) => {
            if (!cancelledRef.current) handleEvent(event);
          },
          () => {
            if (cancelledRef.current) return;
            hadErrorRef.current = true;
            setConnected(false);
          },
          () => {
            if (cancelledRef.current) return;
            setConnected(true);
            setError(null);
            // Events emitted while the stream was down were lost.
            if (hadErrorRef.current) {
              hadErrorRef.current = false;
              void resync();
            }
          },
        );
        closeRef.current = close;
      } catch (err) {
        if (!cancelledRef.current) {
          setError(
            err instanceof Error ? err.message : "Failed to connect to queue",
          );
          setConnected(false);
        }
      }
    }

    void init();

    return () => {
      cancelledRef.current = true;
      closeRef.current?.();
      closeRef.current = null;
    };
  }, [handleEvent, replaceJobs, resync]);

  return {
    jobs,
    connected,
    error,
    cancelling,
    addJob,
    retryJob: handleRetry,
    cancelJob: handleCancel,
    dismissJob: handleDismiss,
  };
}
