import type {
  LibraryAlbum,
  LibraryArtist,
  LibraryResponse,
  LibraryTrack,
} from "@/lib/types";

/**
 * A small library tree shared by the tests, covering every shape the domain
 * model allows: albums, a loose Single, the synthetic root bucket, a non-FLAC
 * track, and a track whose tags could not be read.
 *
 * Not test code itself, so it can be imported from both the unit tests over
 * `lib/library.ts` and the component tests.
 */
export function track(
  path: string,
  overrides: Partial<LibraryTrack> = {},
): LibraryTrack {
  const name = path.slice(path.lastIndexOf("/") + 1);
  return {
    path,
    name,
    title: name.replace(/\.[^.]+$/, ""),
    artist: null,
    album: null,
    album_artist: null,
    track_number: null,
    disc_number: null,
    duration: 200,
    format: "flac",
    bitrate: 900_000,
    sample_rate: 44_100,
    size: 25_000_000,
    mtime: "2026-09-01T10:00:00Z",
    has_embedded_art: true,
    tags: {},
    error: null,
    ...overrides,
  };
}

export function album(
  path: string,
  tracks: LibraryTrack[],
  overrides: Partial<LibraryAlbum> = {},
): LibraryAlbum {
  return {
    name: path.slice(path.lastIndexOf("/") + 1),
    path,
    track_count: tracks.length,
    cover_version: 1,
    has_cover: true,
    tracks,
    ...overrides,
  };
}

export function artist(
  path: string,
  albums: LibraryAlbum[],
  singles: LibraryTrack[] = [],
  overrides: Partial<LibraryArtist> = {},
): LibraryArtist {
  const trackCount =
    albums.reduce((total, a) => total + a.tracks.length, 0) + singles.length;
  return {
    name: path,
    path,
    synthetic: false,
    album_count: albums.length,
    track_count: trackCount,
    albums,
    singles,
    cover_album_path: albums[0]?.path ?? null,
    ...overrides,
  };
}

/** Wrap *artists* in a response, deriving the totals the header line shows. */
export function library(artists: LibraryArtist[]): LibraryResponse {
  return {
    artists,
    artist_count: artists.length,
    album_count: artists.reduce((total, a) => total + a.albums.length, 0),
    track_count: artists.reduce((total, a) => total + a.track_count, 0),
    scanned_at: "2026-09-04T12:00:00Z",
  };
}

/** The tree every test starts from. */
export function libraryFixture(): LibraryResponse {
  return library([
    artist(
      "Bonobo",
      [
        album("Bonobo/Black Sands", [
          track("Bonobo/Black Sands/Prelude.flac", {
            title: "Prelude",
            track_number: 1,
            duration: 79,
            tags: { TITLE: ["Prelude"], ARTIST: ["Bonobo", "Andreya Triana"] },
          }),
          track("Bonobo/Black Sands/Kiara.flac", {
            title: "Kiara",
            track_number: 2,
            duration: 233,
          }),
        ]),
        album("Bonobo/Migration", [
          track("Bonobo/Migration/Kerala.mp3", {
            title: "Kerala",
            track_number: 8,
            duration: 240,
            format: "mp3",
            bitrate: 320_000,
          }),
          track("Bonobo/Migration/Outlier.flac", {
            title: "Outlier.flac",
            duration: null,
            error: "could not read tags",
          }),
        ]),
      ],
      [track("Bonobo/Cirrus.flac", { title: "Cirrus", duration: 346 })],
    ),
    artist("", [], [track("Tycho - Awake.flac", { title: "Tycho - Awake" })], {
      name: "Unknown Artist",
      synthetic: true,
      cover_album_path: null,
    }),
  ]);
}
