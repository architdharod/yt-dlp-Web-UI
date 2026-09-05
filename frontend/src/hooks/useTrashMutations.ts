import { useMutation, useQueryClient } from "@tanstack/react-query";
import { emptyTrash, restoreTrashEntry } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import type {
  TrashEmptyResponse,
  TrashRestoreRequest,
  TrashRestoreResponse,
} from "@/lib/types";

/**
 * `POST /library/trash/restore` as a mutation.
 *
 * A restore both empties a trash row and puts files back in the library, so
 * both queries are invalidated. A 409 rejects with a `LibraryMoveConflict` and
 * nothing has moved, which is what the Trash tab turns into the move dialog.
 */
export function useRestoreMutation() {
  const queryClient = useQueryClient();

  return useMutation<TrashRestoreResponse, Error, TrashRestoreRequest>({
    mutationFn: restoreTrashEntry,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.library });
      void queryClient.invalidateQueries({ queryKey: queryKeys.trash });
    },
  });
}

/**
 * `POST /library/trash/empty` as a mutation.
 *
 * The library query is invalidated as well even though emptying the trash
 * takes nothing out of the library: the backend's own cleanup of `.trash` is
 * the only thing that ran, but a client whose tree predates the deletes that
 * filled the trash is stale either way, and one refetch is cheap.
 */
export function useEmptyTrashMutation() {
  const queryClient = useQueryClient();

  return useMutation<TrashEmptyResponse, Error, void>({
    mutationFn: emptyTrash,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.library });
      void queryClient.invalidateQueries({ queryKey: queryKeys.trash });
    },
  });
}
