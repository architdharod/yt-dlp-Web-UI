/**
 * Every TanStack Query key used by the app, in one place.
 *
 * The SSE stream patches and invalidates these keys from outside the
 * components that read them, so a key typed in two places would silently stop
 * matching. `as const` keeps the tuples readonly and comparable.
 */
export const queryKeys = {
  /** GET /queue — the in-flight and errored jobs. */
  queue: ["queue"] as const,
  /**
   * GET /library — the scanned tree of artists, albums, and tracks.
   * `useLibraryQuery` reads this key, and `queueCache` invalidates it when the
   * `library_changed` SSE event says the folder on disk moved on.
   */
  library: ["library"] as const,
  /**
   * GET /library/trash — the deleted entries waiting to be restored or
   * destroyed. Everything that writes to the library invalidates this
   * alongside `library`: a delete fills it, a restore or an empty-trash
   * drains it, and the tab itself only exists while it is non-empty.
   */
  trash: ["trash"] as const,
  /**
   * GET /notices — the open Navidrome and Lidarr problems. The `notices` SSE
   * event overwrites this key with the whole open set, so a failure — or a
   * clear — that happens while the tab is open needs no refetch.
   */
  notices: ["notices"] as const,
};
