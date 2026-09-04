import { useMutation, useQueryClient } from "@tanstack/react-query";
import { moveLibraryPath } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import type { LibraryMoveRequest, LibraryMoveResponse } from "@/lib/types";

/**
 * `POST /library/move` as a mutation.
 *
 * The library query is invalidated here rather than left to the SSE
 * `library_changed` event: the event arrives too, and invalidating twice costs
 * one deduplicated refetch, while relying on it alone would leave the tree
 * stale for anyone whose stream happened to be reconnecting.
 *
 * Nothing optimistic: a move is all-or-nothing on the backend and can be
 * refused with conflicts, so the only honest tree is the one that comes back.
 */
export function useMoveMutation() {
  const queryClient = useQueryClient();

  return useMutation<LibraryMoveResponse, Error, LibraryMoveRequest>({
    mutationFn: moveLibraryPath,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.library });
    },
  });
}
