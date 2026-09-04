import { describe, expect, it } from "vitest";
import {
  libraryFixture,
  library,
  artist,
  album,
  track,
} from "@/lib/library.fixture";
import {
  ARTISTS_VIEW,
  resolveLocation,
  searchLibrary,
  totalDuration,
  type LibraryView,
} from "@/lib/library";

const tree = libraryFixture();

const albumView: LibraryView = {
  level: "album",
  artistPath: "Bonobo",
  albumPath: "Bonobo/Migration",
};

describe("resolveLocation", () => {
  it("carries the artist and album the view names", () => {
    const location = resolveLocation(albumView, tree);

    expect(location.level).toBe("album");
    expect(location.level === "album" && location.album.name).toBe("Migration");
  });

  it("falls back to the artist grid before the first fetch lands", () => {
    expect(resolveLocation(albumView, undefined).level).toBe("artists");
  });

  it("drops to the artist when the album has gone", () => {
    const without = library([artist("Bonobo", [], [])]);

    const location = resolveLocation(albumView, without);

    expect(location.level).toBe("artist");
    expect(location.level === "artist" && location.artist.path).toBe("Bonobo");
  });

  it("drops to the artist grid when the artist has gone", () => {
    const location = resolveLocation(albumView, library([]));

    expect(location).toEqual(ARTISTS_VIEW);
  });

  it("resolves the synthetic bucket, whose path is the empty string", () => {
    const location = resolveLocation({ level: "artist", artistPath: "" }, tree);

    expect(location.level === "artist" && location.artist.name).toBe(
      "Unknown Artist",
    );
  });
});

describe("searchLibrary", () => {
  it("returns nothing for a blank query", () => {
    expect(searchLibrary(tree, "   ")).toEqual([]);
  });

  it("matches artists, albums, and tracks in one flat list", () => {
    const results = searchLibrary(tree, "o");

    expect(results.some((r) => r.kind === "artist")).toBe(true);
    expect(results.some((r) => r.kind === "album")).toBe(true);
    expect(results.some((r) => r.kind === "track")).toBe(true);
  });

  it("is case-insensitive and matches on a substring", () => {
    const results = searchLibrary(tree, "kEraL");

    expect(results).toHaveLength(1);
    expect(results[0]).toMatchObject({
      kind: "track",
      name: "Kerala",
      parent: "Bonobo · Migration",
      trackPath: "Bonobo/Migration/Kerala.mp3",
      view: albumView,
    });
  });

  it("sends a Single to its artist page, since it has no album page", () => {
    const [result] = searchLibrary(tree, "Cirrus");

    expect(result.view).toEqual({ level: "artist", artistPath: "Bonobo" });
    expect(result.parent).toBe("Bonobo · Singles");
    expect(result.trackPath).toBe("Bonobo/Cirrus.flac");
  });

  it("points an album result at that album", () => {
    const [result] = searchLibrary(tree, "Black Sands");

    expect(result.kind).toBe("album");
    expect(result.view).toEqual({
      level: "album",
      artistPath: "Bonobo",
      albumPath: "Bonobo/Black Sands",
    });
  });

  it("stops at the limit rather than rendering a whole big library", () => {
    const many = library([
      artist("Prolific", [
        album(
          "Prolific/Everything",
          Array.from({ length: 500 }, (_, i) =>
            track(`Prolific/Everything/Song ${i}.flac`, {
              title: `Song ${i}`,
            }),
          ),
        ),
      ]),
    ]);

    // No explicit limit: 100 is `SEARCH_RESULT_LIMIT`, the default.
    expect(searchLibrary(many, "song")).toHaveLength(100);
  });
});

describe("totalDuration", () => {
  it("adds durations up and counts an unreadable track as zero", () => {
    expect(totalDuration(tree.artists[0].albums[1].tracks)).toBe(240);
  });
});
