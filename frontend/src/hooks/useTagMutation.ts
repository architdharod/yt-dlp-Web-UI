import { useCallback, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { tagLibraryPath } from "@/lib/api";
import { addJobToCache } from "@/lib/queueCache";
import type { Job } from "@/lib/types";

/** What the Library tab says after an "Update metadata" click. */
export interface TagFeedback {
  message: string;
  failed: boolean;
}

/** The line shown when the backend took the request. */
const QUEUED_MESSAGE = "Metadata update queued — watch the Download tab.";

/** The line shown when a failure arrived with no message of its own. */
const FAILED_MESSAGE = "Could not queue the metadata update";

/**
 * `POST /library/tag` as a mutation, with the per-path pending state the row
 * and album-header buttons need.
 *
 * The new job is written straight into the `["queue"]` cache rather than
 * invalidating it, exactly as a submitted download is: the backend answers with
 * the row it just created, and everything after that arrives on the SSE
 * stream. The library query is deliberately *not* invalidated here — nothing
 * has changed on disk yet, and the `library_changed` event the run emits when
 * it finishes is what refreshes the tree and the cover art.
 *
 * Pending is a set of paths, not the mutation's own `isPending`: one hook
 * instance serves every row in the list, so a boolean would grey out all of
 * them at once.
 */
export function useTagMutation() {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set());
  const [feedback, setFeedback] = useState<TagFeedback | null>(null);

  const mutation = useMutation<Job, Error, string>({
    mutationFn: (path: string) => tagLibraryPath({ path }),
    onMutate: (path) => {
      setPending((current) => new Set(current).add(path));
      // The previous answer is about a click the user has moved on from.
      setFeedback(null);
    },
    onSuccess: (job) => {
      addJobToCache(queryClient, job);
      setFeedback({ message: QUEUED_MESSAGE, failed: false });
    },
    onError: (error) => {
      // A 409 ("already being tagged", "a download is aiming at this folder")
      // carries the backend's own sentence; anything without one falls back.
      const message =
        error.message.trim().length > 0 ? error.message : FAILED_MESSAGE;
      setFeedback({ message, failed: true });
    },
    onSettled: (_job, _error, path) => {
      setPending((current) => {
        const next = new Set(current);
        next.delete(path);
        return next;
      });
    },
  });

  const { mutate } = mutation;
  const tagPath = useCallback(
    (path: string) => {
      // A second click while the first request is out would only earn a 409.
      if (pending.has(path)) return;
      mutate(path);
    },
    [mutate, pending],
  );

  const clearFeedback = useCallback(() => setFeedback(null), []);

  return { tagPath, pending, feedback, clearFeedback };
}
