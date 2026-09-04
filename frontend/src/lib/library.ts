import type {
  LibraryAlbum,
  LibraryArtist,
  LibraryResponse,
  LibraryTrack,
} from "@/lib/types";

/**
 * Where the Library tab is pointing.
 *
 * Everything is keyed by path rather than by array index, because a refetch
 * after `library_changed` hands back a whole new tree: an index would silently
 * move the user to a different artist, a path either still resolves or does
 * not. `resolveView` handles the "does not" case.
 */
export type LibraryView =
  | { level: "artists" }
  | { level: "artist"; artistPath: string }
  | { level: "album"; artistPath: string; albumPath: string };

/** The artists view, spelled once so callers can compare and reset to it. */
export const ARTISTS_VIEW: LibraryView = { level: "artists" };

/** The artist with *path*, or undefined. The synthetic bucket's path is "". */
function findArtist(
  library: LibraryResponse,
  path: string,
): LibraryArtist | undefined {
  return library.artists.find((artist) => artist.path === path);
}

/** The album with *path* under *artist*, or undefined. */
function findAlbum(
  artist: LibraryArtist,
  path: string,
): LibraryAlbum | undefined {
  return artist.albums.find((album) => album.path === path);
}

/**
 * A view resolved against a tree: the same three levels, but carrying the
 * artist and album themselves rather than paths that might no longer exist.
 * Rendering off this is what keeps the components free of lookups that
 * "cannot" fail.
 */
export type LibraryLocation =
  | { level: "artists" }
  | { level: "artist"; artist: LibraryArtist }
  | { level: "album"; artist: LibraryArtist; album: LibraryAlbum };

/**
 * Resolve *view* against the current tree, dropping a level at a time when a
 * folder has gone.
 *
 * A deleted or renamed album leaves the user on its artist, a deleted artist
 * on the artist grid — the closest thing to "stay where you were" a changed
 * tree allows. Derived during render, so no effect has to chase the data.
 */
export function resolveLocation(
  view: LibraryView,
  library: LibraryResponse | undefined,
): LibraryLocation {
  if (view.level === "artists" || library === undefined) {
    return { level: "artists" };
  }

  const artist = findArtist(library, view.artistPath);
  if (artist === undefined) return { level: "artists" };
  if (view.level === "artist") return { level: "artist", artist };

  const album = findAlbum(artist, view.albumPath);
  return album === undefined
    ? { level: "artist", artist }
    : { level: "album", artist, album };
}

/** Total playing time of *tracks*; tracks with no duration count as zero. */
export function totalDuration(tracks: readonly LibraryTrack[]): number {
  return tracks.reduce((total, track) => total + (track.duration ?? 0), 0);
}

/** What kind of thing a search result points at. */
export type SearchResultKind = "artist" | "album" | "track";

/** One row of the flat search list. */
export interface LibrarySearchResult {
  /** Stable React key: the kind and the path, unique across the whole list. */
  key: string;
  kind: SearchResultKind;
  /** The matched name — artist name, album name, or track title. */
  name: string;
  /** Secondary line: where the match lives. */
  parent: string;
  /** Where clicking the row takes the user. */
  view: LibraryView;
  /** The track to highlight on arrival, for track results only. */
  trackPath: string | null;
}

/**
 * How many results the flat search renders. Far more than anyone reads, and
 * low enough that a one-letter query over a big library stays cheap to paint.
 */
const SEARCH_RESULT_LIMIT = 100;

/** The label a loose track's location gets, since it has no album folder. */
const SINGLES_LABEL = "Singles";

function matches(haystack: string, needle: string): boolean {
  return haystack.toLowerCase().includes(needle);
}

/**
 * Flat, case-insensitive substring search across artists, albums, and tracks.
 *
 * Results come out in tree order — artists, then each artist's albums, then
 * its tracks — so the same query always produces the same list. A track that
 * is a Single points at its artist page, since there is no album page to open.
 */
export function searchLibrary(
  library: LibraryResponse | undefined,
  query: string,
  limit: number = SEARCH_RESULT_LIMIT,
): LibrarySearchResult[] {
  const needle = query.trim().toLowerCase();
  if (library === undefined || needle === "") return [];

  const results: LibrarySearchResult[] = [];

  const push = (result: LibrarySearchResult): boolean => {
    results.push(result);
    return results.length < limit;
  };

  for (const artist of library.artists) {
    const artistView: LibraryView = {
      level: "artist",
      artistPath: artist.path,
    };

    if (matches(artist.name, needle)) {
      if (
        !push({
          key: `artist:${artist.path}`,
          kind: "artist",
          name: artist.name,
          parent: "Artist",
          view: artistView,
          trackPath: null,
        })
      ) {
        return results;
      }
    }

    for (const album of artist.albums) {
      const albumView: LibraryView = {
        level: "album",
        artistPath: artist.path,
        albumPath: album.path,
      };

      if (matches(album.name, needle)) {
        if (
          !push({
            key: `album:${album.path}`,
            kind: "album",
            name: album.name,
            parent: artist.name,
            view: albumView,
            trackPath: null,
          })
        ) {
          return results;
        }
      }

      for (const track of album.tracks) {
        if (!matches(track.title, needle)) continue;
        if (
          !push({
            key: `track:${track.path}`,
            kind: "track",
            name: track.title,
            parent: `${artist.name} · ${album.name}`,
            view: albumView,
            trackPath: track.path,
          })
        ) {
          return results;
        }
      }
    }

    for (const track of artist.singles) {
      if (!matches(track.title, needle)) continue;
      if (
        !push({
          key: `track:${track.path}`,
          kind: "track",
          name: track.title,
          parent: `${artist.name} · ${SINGLES_LABEL}`,
          view: artistView,
          trackPath: track.path,
        })
      ) {
        return results;
      }
    }
  }

  return results;
}
