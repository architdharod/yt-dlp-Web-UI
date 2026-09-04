import { useQuery } from "@tanstack/react-query";
import { fetchNotices } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import type { Notice } from "@/lib/types";

/**
 * The open Navidrome and Lidarr problems.
 *
 * Fetched on mount and again after an SSE reconnect (`resyncAfterReconnect`
 * invalidates this key); in between, the `notices` event overwrites the cache
 * with the whole open set, so there is nothing to poll.
 *
 * The data is deliberately left stale rather than pinned with `staleTime:
 * Infinity`: the push is authoritative, but a remount or a refocus then costs
 * one cheap request, which recovers the display if a push is ever missed. The
 * two cannot race: applying a `notices` event cancels whatever fetch is in
 * flight before it writes, so a request that left first cannot land on top of
 * the push.
 *
 * A failed fetch is not surfaced: the backend being unreachable already shows
 * as the stream error, and a second red line saying the same thing helps
 * nobody.
 */
export function useNotices(): Notice[] {
  const { data } = useQuery({
    queryKey: queryKeys.notices,
    queryFn: fetchNotices,
  });
  return data ?? [];
}
