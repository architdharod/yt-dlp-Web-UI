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
   * GET /library — added in a later phase. The key already exists because the
   * `library_changed` SSE event invalidates it; invalidating a key nothing
   * subscribes to is a no-op, so the hook can land before the query does.
   */
  library: ["library"] as const,
};
