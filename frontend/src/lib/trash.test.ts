import { describe, expect, it } from "vitest";
import { entryLabel, restoreTarget } from "@/lib/trash";
import type { TrashEntry, TrashEntryKind } from "@/lib/types";

function entry(
  kind: TrashEntryKind,
  path: string,
  paths: string[] = [],
): TrashEntry {
  return {
    id: "t1",
    path,
    kind,
    paths,
    deleted_at: "2026-09-04T11:00:00Z",
    track_count: paths.length || 1,
  };
}

describe("restoreTarget", () => {
  it("gives an artist entry no album to choose", () => {
    expect(restoreTarget(entry("artist", "Bonobo"))).toMatchObject({
      artist: "Bonobo",
      album: null,
    });
  });

  it("splits an album entry into its artist and its name", () => {
    expect(restoreTarget(entry("album", "Bonobo/Black Sands"))).toMatchObject({
      artist: "Bonobo",
      album: "Black Sands",
    });
  });

  it("reads a track's folder out of the file it holds", () => {
    const target = restoreTarget(
      entry("track", "Bonobo/Black Sands/Prelude.flac", [
        "Bonobo/Black Sands/Prelude.flac",
      ]),
    );

    expect(target).toMatchObject({ artist: "Bonobo", album: "Black Sands" });
  });

  it("leaves the album blank for a loose Single", () => {
    const target = restoreTarget(
      entry("track", "Bonobo/Cirrus.flac", ["Bonobo/Cirrus.flac"]),
    );

    expect(target).toMatchObject({ artist: "Bonobo", album: "" });
  });

  it("takes a multi-track entry's folder from its first file", () => {
    const target = restoreTarget(
      entry("tracks", "Bonobo/Migration", [
        "Bonobo/Migration/Kerala.flac",
        "Bonobo/Migration/Outlier.flac",
      ]),
    );

    expect(target).toMatchObject({ artist: "Bonobo", album: "Migration" });
  });

  it("leaves both blank for a file that sat at the library root", () => {
    const target = restoreTarget(
      entry("track", "Tycho - Awake.flac", ["Tycho - Awake.flac"]),
    );

    expect(target).toMatchObject({ artist: "", album: "" });
  });
});

describe("entryLabel", () => {
  it("shows the path an entry came from", () => {
    expect(entryLabel(entry("album", "Bonobo/Black Sands"))).toBe(
      "Bonobo/Black Sands",
    );
  });

  it("names the library root, which has no path of its own", () => {
    const rootTracks = entry("tracks", "", [
      "Tycho - Awake.flac",
      "Tycho - Dive.flac",
    ]);

    expect(entryLabel(rootTracks)).toBe("Library root");
  });
});
