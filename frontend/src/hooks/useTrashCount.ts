/**
 * How many entries are in the trash — the number on the Trash tab badge, and
 * what decides whether the tab exists at all.
 *
 * There is no trash endpoint yet, so this is a constant zero and the tab stays
 * hidden. Phase 7 replaces the body with the `GET /library/trash` query;
 * every caller already treats it as live data.
 */
export function useTrashCount(): number {
  return 0;
}
