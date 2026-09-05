import { describe, expect, it } from "vitest";
import {
  buildBulkRequest,
  initialSelection,
  isSelectable,
  reconcileSelection,
  rowLabel,
  selectAll,
} from "@/lib/preview";
import {
  LARGE_COLLECTION_TRACKS,
  collectionPreview,
  previewRow,
} from "@/lib/preview.fixture";

const available = previewRow("a");
const inLibrary = previewRow("b", {
  status: "in_library",
  reason: "Bonobo/Black Sands/Kiara.flac",
});
const unavailable = previewRow("c", {
  status: "unavailable",
  reason: "Video unavailable",
});

describe("rowLabel", () => {
  it("falls back to the URL when the flat pass found no title", () => {
    expect(rowLabel(previewRow("a", { title: null }))).toBe(
      "https://youtube.com/watch?v=a",
    );
  });
});

describe("isSelectable", () => {
  it("refuses unavailable rows and allows the rest", () => {
    expect(isSelectable(available)).toBe(true);
    expect(isSelectable(inLibrary)).toBe(true);
    expect(isSelectable(unavailable)).toBe(false);
  });
});

describe("selectAll", () => {
  it("takes the available rows only", () => {
    expect(selectAll([available, inLibrary, unavailable])).toEqual(
      new Set(["a"]),
    );
  });
});

describe("initialSelection", () => {
  it("preselects everything available", () => {
    const preview = collectionPreview([available, inLibrary, unavailable]);
    expect(initialSelection(preview)).toEqual(new Set(["a"]));
  });

  it("preselects nothing past the large-collection mark", () => {
    const rows = Array.from({ length: LARGE_COLLECTION_TRACKS + 1 }, (_, i) =>
      previewRow(String(i)),
    );
    const preview = collectionPreview(rows);
    expect(preview.large).toBe(true);
    expect(initialSelection(preview)).toEqual(new Set());
  });
});

describe("reconcileSelection", () => {
  const rows = [available, inLibrary, unavailable];

  it("keeps the ticks the user made", () => {
    const next = reconcileSelection(new Set(["b"]), rows, rows, false);
    expect(next).toEqual(new Set(["b"]));
  });

  it("unticks a row that has just become a duplicate", () => {
    const after = [{ ...available, status: "in_library" as const }, inLibrary];
    expect(reconcileSelection(new Set(["a"]), rows, after, false)).toEqual(
      new Set(),
    );
  });

  it("ticks a row that is no longer a duplicate", () => {
    const after = [available, { ...inLibrary, status: "available" as const }];
    expect(reconcileSelection(new Set(["a"]), rows, after, false)).toEqual(
      new Set(["a", "b"]),
    );
  });

  it("ticks a row that is no longer unavailable", () => {
    const after = [available, { ...unavailable, status: "available" as const }];
    expect(reconcileSelection(new Set(["a"]), rows, after, false)).toEqual(
      new Set(["a", "c"]),
    );
  });

  it("never ticks a newly available row on a large preview", () => {
    const after = [available, { ...unavailable, status: "available" as const }];
    expect(reconcileSelection(new Set(["a"]), rows, after, true)).toEqual(
      new Set(["a"]),
    );
  });

  it("never ticks anything on a large preview", () => {
    const after = [available, { ...inLibrary, status: "available" as const }];
    expect(reconcileSelection(new Set(["a"]), rows, after, true)).toEqual(
      new Set(["a"]),
    );
  });

  it("drops rows that became unavailable", () => {
    const after = [{ ...available, status: "unavailable" as const }];
    expect(reconcileSelection(new Set(["a"]), rows, after, false)).toEqual(
      new Set(),
    );
  });

  it("applies the opening rule to rows the previous pass never had", () => {
    const after = [available, previewRow("d")];
    expect(reconcileSelection(new Set(), rows, after, false)).toEqual(
      new Set(["d"]),
    );
  });
});

describe("buildBulkRequest", () => {
  it("carries the trimmed artist and the selected rows in display order", () => {
    const preview = collectionPreview([available, inLibrary, unavailable]);
    const request = buildBulkRequest(preview, "  Bonobo  ", new Set(["b", "a"]));

    expect(request.url).toBe(preview.url);
    expect(request.artist).toBe("Bonobo");
    expect(request.title).toBe("Black Sands");
    expect(request.tracks.map((track) => track.url)).toEqual([
      available.url,
      inLibrary.url,
    ]);
    expect(request.tracks[0]).toEqual({
      url: available.url,
      title: available.title,
      album: available.album,
      album_final: available.album_final,
      duration: available.duration,
      thumbnail_url: available.thumbnail_url,
      source_id: available.source_id,
    });
  });

  it("forwards album_final so the backend knows the album is the whole answer", () => {
    const single = previewRow("s", { album: null, album_final: true });
    const request = buildBulkRequest(
      collectionPreview([single]),
      "Glass Beams",
      new Set(["s"]),
    );

    expect(request.tracks[0].album).toBeNull();
    expect(request.tracks[0].album_final).toBe(true);
  });
});
