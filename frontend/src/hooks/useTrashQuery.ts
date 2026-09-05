import { useQuery } from "@tanstack/react-query";
import { getTrash } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";

/**
 * What is in `.trash`, from `GET /library/trash`.
 *
 * Fetched on mount rather than only when the Trash tab is open: the tab does
 * not exist until this query says there is something in it, so nothing else
 * would ever ask. It is small — one row per entry — and every write to the
 * library invalidates it, so no stale window and no polling.
 */
export function useTrashQuery() {
  return useQuery({
    queryKey: queryKeys.trash,
    queryFn: ({ signal }) => getTrash(signal),
    refetchOnWindowFocus: true,
  });
}

/**
 * How many entries are in the trash — the number on the Trash tab badge, and
 * what decides whether the tab exists at all.
 *
 * Zero while the query is still loading or has failed, which keeps the tab
 * from flickering into existence on a request that never arrives.
 */
export function useTrashCount(): number {
  const { data } = useTrashQuery();
  return data?.entries.length ?? 0;
}
