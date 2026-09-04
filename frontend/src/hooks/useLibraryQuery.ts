import { useQuery } from "@tanstack/react-query";
import { getLibrary } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";

/**
 * How long a fetched library counts as fresh.
 *
 * The backend tells us when anything changes (`library_changed` invalidates
 * this key from `queueCache`), so the only thing a stale window buys is cover
 * for changes made outside the app — a file dropped in over SMB. Half a minute
 * keeps tab switches and window focus from re-fetching the whole tree while
 * still picking those up promptly.
 */
const LIBRARY_STALE_TIME_MS = 30_000;

/**
 * The scanned library from `GET /library`.
 *
 * No `refetchInterval`: the SSE stream invalidates this query on
 * `library_changed`, and `refetchOnWindowFocus` covers edits made on disk
 * while the tab was in the background. Polling a full scan would be the one
 * expensive request in the app.
 */
export function useLibraryQuery() {
  return useQuery({
    queryKey: queryKeys.library,
    queryFn: ({ signal }) => getLibrary(signal),
    staleTime: LIBRARY_STALE_TIME_MS,
    refetchOnWindowFocus: true,
  });
}
