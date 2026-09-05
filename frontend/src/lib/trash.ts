import type { TrashEntry } from "@/lib/types";

/**
 * A trash entry being restored somewhere other than where it came from.
 *
 * The move dialog is reused for the choice, so this stands where a
 * `MoveTarget` normally does. It cannot be one: the artist and album a trashed
 * entry came from are no longer in the library, so there are no
 * `LibraryArtist` or `LibraryAlbum` objects to point at — only the names
 * parsed back out of the entry's own path.
 *
 * `album` is null for an artist entry, which has no album to choose, exactly
 * as an artist rename has none.
 */
export interface RestoreTarget {
  kind: "restore";
  entry: TrashEntry;
  artist: string;
  album: string | null;
}

/** The folder a library path sits in; "" for a file at the library root. */
function parentFolder(path: string): string {
  const index = path.lastIndexOf("/");
  return index === -1 ? "" : path.slice(0, index);
}

/**
 * The artist and album a trash entry came from, for the restore dialog's
 * starting values.
 *
 * An artist entry's path is the artist folder and an album entry's is
 * `Artist/Album`. For tracks the path may be a file or, for a multi-track
 * entry, the folder they shared, so the names come from the first file in
 * `paths` where there is one — a track at the library root leaves both blank,
 * which is the honest starting point for a file that never had an artist.
 */
export function restoreTarget(entry: TrashEntry): RestoreTarget {
  const segments =
    entry.kind === "artist" || entry.kind === "album"
      ? entry.path.split("/")
      : parentFolder(entry.paths[0] ?? entry.path).split("/");

  return {
    kind: "restore",
    entry,
    artist: segments[0] ?? "",
    album: entry.kind === "artist" ? null : (segments[1] ?? ""),
  };
}

/**
 * What to call a trash entry on screen.
 *
 * A `tracks` entry deleted from the library root has no folder above it, so
 * the backend records its path as the empty string. Left as-is it renders as
 * nothing at all, so the root itself gets a name.
 */
export function entryLabel(entry: TrashEntry): string {
  return entry.path === "" ? "Library root" : entry.path;
}
