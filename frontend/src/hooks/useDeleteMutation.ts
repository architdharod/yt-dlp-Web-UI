import { useMutation, useQueryClient } from "@tanstack/react-query";
import { deleteLibraryPath } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import type { LibraryDeleteRequest, LibraryDeleteResponse } from "@/lib/types";

/**
 * `POST /library/delete` as a mutation.
 *
 * Both queries are invalidated here rather than left to the SSE
 * `library_changed` event, for the same reason the move does it: the event
 * arrives too and the second invalidation costs one deduplicated refetch,
 * while relying on it alone would leave the tree — and the Trash tab that has
 * just gained an entry — stale for anyone whose stream is reconnecting.
 *
 * Nothing optimistic: a delete is all-or-nothing on the backend and can be
 * refused by the in-flight guard.
 *
 * A failure invalidates them too: the one 500 a delete can answer with says
 * some tracks did move and are in the Trash tab, and that message is only
 * useful if the tab it names has actually caught up.
 */
export function useDeleteMutation() {
  const queryClient = useQueryClient();

  return useMutation<LibraryDeleteResponse, Error, LibraryDeleteRequest>({
    mutationFn: deleteLibraryPath,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.library });
      void queryClient.invalidateQueries({ queryKey: queryKeys.trash });
    },
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.library });
      void queryClient.invalidateQueries({ queryKey: queryKeys.trash });
    },
  });
}
