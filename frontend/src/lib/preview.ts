/**
 * The checklist rules behind a collection preview, kept apart from the
 * component so the ticking decisions can be read (and tested) on their own.
 *
 * Two rules do all the work: a row is only ever ticked if something could be
 * downloaded from it, and the user's own ticks survive everything except a
 * change in what the backend says about that row.
 */

import type {
  BulkDownloadRequest,
  BulkTrack,
  CollectionPreview,
  PreviewRow,
  PreviewRowStatus,
} from "@/lib/types";

/**
 * The longest artist folder name the backend will take.
 *
 * Kept as a cap on the input rather than a validation message: a name this
 * long is a paste accident, and the field is the only place to say so before
 * the request goes out.
 */
export const MAX_FOLDER_NAME = 200;

/** What a row is called when the flat pass gave it no title: its URL. */
export function rowLabel(row: PreviewRow): string {
  return row.title ?? row.url;
}

/** Whether the user is allowed to tick *row* at all. */
export function isSelectable(row: PreviewRow): boolean {
  return row.status !== "unavailable";
}

/**
 * The rows "Select all" ticks: the available ones, and only those.
 *
 * An `in_library` row can still be ticked by hand — the backend takes it and
 * skips it with a visible reason — but a blanket Select all that re-downloaded
 * everything already on disk would defeat the dedup pass entirely.
 */
export function selectableByDefault(row: PreviewRow): boolean {
  return row.status === "available";
}

/** Every row Select all would tick. */
export function selectAll(rows: readonly PreviewRow[]): ReadonlySet<string> {
  return new Set(rows.filter(selectableByDefault).map((row) => row.id));
}

/**
 * What is ticked when a preview first opens: everything available, or nothing
 * at all past the 500-row mark, where preselecting would hand the user a
 * several-hundred-track download they never chose.
 */
export function initialSelection(
  preview: CollectionPreview,
): ReadonlySet<string> {
  return preview.large ? new Set<string>() : selectAll(preview.rows);
}

/**
 * Carry a selection across a re-probe with a corrected artist.
 *
 * The rows are the same tracks; only the dedup verdict can have moved, because
 * the enumeration is cached and the artist folder is all that was re-read. So
 * the user's ticks are kept, except where the verdict itself changed: a row
 * that has just turned out to be in the library is unticked, and one that has
 * just become available — whether it was a duplicate before or unavailable —
 * goes back to being ticked, unless the preview is `large`, where nothing is
 * ever ticked on the user's behalf.
 */
export function reconcileSelection(
  selected: ReadonlySet<string>,
  previousRows: readonly PreviewRow[],
  nextRows: readonly PreviewRow[],
  large: boolean,
): ReadonlySet<string> {
  const before = new Map<string, PreviewRowStatus>(
    previousRows.map((row) => [row.id, row.status]),
  );
  const next = new Set<string>();

  for (const row of nextRows) {
    if (!isSelectable(row)) continue;
    const was = before.get(row.id);

    // A row the previous pass never showed follows the opening rule.
    if (was === undefined) {
      if (!large && selectableByDefault(row)) next.add(row.id);
      continue;
    }
    // Newly a duplicate: untick, whatever the user had chosen.
    if (row.status === "in_library" && was !== "in_library") continue;
    // Newly downloadable, from either kind of verdict: tick it, as it would
    // have been on opening.
    if (row.status === "available" && was !== "available") {
      if (!large) next.add(row.id);
      continue;
    }
    if (selected.has(row.id)) next.add(row.id);
  }

  return next;
}

/** One selected row as the bulk endpoint wants it. */
function toBulkTrack(row: PreviewRow): BulkTrack {
  return {
    url: row.url,
    title: row.title,
    album: row.album,
    duration: row.duration,
    thumbnail_url: row.thumbnail_url,
    source_id: row.source_id,
  };
}

/**
 * The body of `POST /download/bulk` for the current selection.
 *
 * Tracks go in the order they were shown, which is the order the children are
 * created in and therefore the order they download in. The artist is trimmed
 * here because it is the one field the user typed, and a trailing space would
 * become a folder name.
 */
export function buildBulkRequest(
  preview: CollectionPreview,
  artist: string,
  selected: ReadonlySet<string>,
): BulkDownloadRequest {
  return {
    url: preview.url,
    artist: artist.trim(),
    title: preview.title,
    tracks: preview.rows.filter((row) => selected.has(row.id)).map(toBulkTrack),
  };
}
