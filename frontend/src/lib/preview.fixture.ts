/**
 * Collection previews for the tests, built the way the probe builds them: the
 * counts and the `large` flag are derived from the rows rather than passed in,
 * so a fixture can never disagree with itself.
 */

import type { CollectionPreview, PreviewRow } from "@/lib/types";

/** The row count above which the backend sets `large` (LARGE_COLLECTION_TRACKS). */
export const LARGE_COLLECTION_TRACKS = 500;

/** One preview row, with everything but the id and title defaulted. */
export function previewRow(
  id: string,
  overrides: Partial<PreviewRow> = {},
): PreviewRow {
  return {
    id,
    url: `https://youtube.com/watch?v=${id}`,
    source_id: `youtube:${id}`,
    title: `Track ${id}`,
    album: "Black Sands",
    album_final: false,
    duration: 214,
    thumbnail_url: null,
    status: "available",
    reason: null,
    ...overrides,
  };
}

/** A preview over *rows*, with the counts the backend would have sent. */
export function collectionPreview(
  rows: readonly PreviewRow[],
  overrides: Partial<CollectionPreview> = {},
): CollectionPreview {
  return {
    url: "https://youtube.com/playlist?list=PL1",
    title: "Black Sands",
    artist: "Bonobo",
    source: "youtube",
    rows: [...rows],
    total: rows.length,
    in_library: rows.filter((row) => row.status === "in_library").length,
    unavailable: rows.filter((row) => row.status === "unavailable").length,
    large: rows.length > LARGE_COLLECTION_TRACKS,
    notices: [],
    ...overrides,
  };
}
