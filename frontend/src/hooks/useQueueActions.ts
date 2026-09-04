import { useCallback, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { cancelJob, dismissJob, retryJob } from "@/lib/api";
import {
  addJobToCache,
  removeJobFromCache,
  replaceJobInCache,
  setJobActionError,
} from "@/lib/queueCache";
import type { Job } from "@/lib/types";

/** Message shown on the row when an action failed for no stated reason. */
function messageOf(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

/**
 * The row actions of the in-flight queue, as mutations that patch the queue
 * cache directly. Nothing here invalidates: the backend answers with the job
 * it just changed, and anything it has not decided yet arrives on the stream.
 */
export function useQueueActions() {
  const queryClient = useQueryClient();
  /**
   * Ids whose cancel request is in flight. The row's Cancel button is disabled
   * while its id is in here, so a double click cannot send two requests (the
   * second would answer 400 once the first one landed).
   */
  const [cancelling, setCancelling] = useState<ReadonlySet<string>>(new Set());

  const retry = useMutation({
    mutationFn: retryJob,
    onSuccess: (job) => replaceJobInCache(queryClient, job),
    onError: (err, jobId) =>
      setJobActionError(queryClient, jobId, messageOf(err, "Retry failed")),
  });

  const cancel = useMutation({
    mutationFn: cancelJob,
    onMutate: (jobId) => {
      setCancelling((prev) => new Set(prev).add(jobId));
    },
    onSuccess: (job) => {
      // A queued job comes back already cancelled and can go now. A running
      // one is only signalled, so its row stays until the SSE status_change
      // says the thread has stopped — and the response is deliberately not
      // written into the cache: it is a snapshot from before the request, so
      // it would revert whatever the stream has delivered since (a job that
      // has reached "converting" would show "downloading" until it finished).
      if (job.status === "cancelled") {
        removeJobFromCache(queryClient, job.id);
      }
    },
    onError: (err, jobId) =>
      setJobActionError(queryClient, jobId, messageOf(err, "Cancel failed")),
    onSettled: (_data, _err, jobId) => {
      setCancelling((prev) => {
        const next = new Set(prev);
        next.delete(jobId);
        return next;
      });
    },
  });

  const dismiss = useMutation({
    mutationFn: dismissJob,
    // The backend deletes the row outright, so it only goes once it agreed.
    onSuccess: (_data, jobId) => removeJobFromCache(queryClient, jobId),
    onError: (err, jobId) =>
      setJobActionError(queryClient, jobId, messageOf(err, "Dismiss failed")),
  });

  const addJob = useCallback(
    (job: Job) => addJobToCache(queryClient, job),
    [queryClient],
  );

  return {
    cancelling,
    addJob,
    retryJob: retry.mutate,
    cancelJob: cancel.mutate,
    dismissJob: dismiss.mutate,
  };
}
