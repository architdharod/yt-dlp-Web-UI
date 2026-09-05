import { afterEach, describe, expect, it, vi } from "vitest";
import {
  LibraryMoveConflict,
  coverUrl,
  deleteLibraryPath,
  emptyTrash,
  getTrash,
  moveLibraryPath,
  restoreTrashEntry,
} from "@/lib/api";

describe("coverUrl", () => {
  it("passes the album path and its cover version", () => {
    expect(coverUrl({ path: "Bonobo/Black Sands", cover_version: 7 })).toBe(
      "/library/cover?path=Bonobo%2FBlack%20Sands&v=7",
    );
  });

  it("encodes the characters that would otherwise be URL syntax", () => {
    // A folder may legitimately be called any of these; unencoded, "#" would
    // start a fragment, "&" a second parameter, and "?" a second query string.
    expect(
      coverUrl({ path: "AC#DC/Hits & Misses/Why?", cover_version: 1 }),
    ).toBe("/library/cover?path=AC%23DC%2FHits%20%26%20Misses%2FWhy%3F&v=1");
  });

  it("keeps a plus sign from being read as a space", () => {
    expect(coverUrl({ path: "A+B/Best of", cover_version: 2 })).toBe(
      "/library/cover?path=A%2BB%2FBest%20of&v=2",
    );
  });
});

describe("moveLibraryPath", () => {
  const request = { path: "Bonobo", artist: "Bonobo (UK)" };

  function respond(status: number, body: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts the request body as JSON", async () => {
    respond(200, { moved: [], removed: [] });

    await moveLibraryPath(request);

    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(url).toBe("/library/move");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual(request);
  });

  it("turns a 409 into a conflict carrying every occupied path", async () => {
    respond(409, {
      detail: {
        message: "1 file(s) already exist; nothing was moved",
        conflicts: ["Ninja Tune/Black Sands/Kong.flac"],
      },
    });

    await expect(moveLibraryPath(request)).rejects.toMatchObject({
      name: "LibraryMoveConflict",
      conflicts: ["Ninja Tune/Black Sands/Kong.flac"],
    });
  });

  it("reports a 409 that is not a conflict list with its own message", async () => {
    // The body is read once: a second read would throw, and the message would
    // fall back to the bare status text.
    respond(409, { detail: "a download is in progress" });

    await expect(moveLibraryPath(request)).rejects.toThrow(
      "a download is in progress",
    );
  });

  it("reports a 400 as a plain error with the backend's message", async () => {
    respond(400, { detail: "artist must not be '.' or '..'" });

    await expect(moveLibraryPath(request)).rejects.toThrow(
      "artist must not be '.' or '..'",
    );
  });
});


/** Stub `fetch` with one JSON response, and hand back the mock to inspect. */
function respondWith(status: number, body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The parsed JSON body of the one request the stub received. */
function sentBody(fetchMock: ReturnType<typeof vi.fn>): unknown {
  const init = fetchMock.mock.calls[0][1] as RequestInit;
  return JSON.parse(init.body as string);
}

const entry = {
  id: "t1",
  path: "Bonobo/Black Sands",
  kind: "album" as const,
  paths: ["Bonobo/Black Sands/Prelude.flac"],
  deleted_at: "2026-09-04T11:00:00Z",
  track_count: 1,
};

describe("the trash endpoints", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts a delete and returns the entry it became", async () => {
    const fetchMock = respondWith(200, { entry, removed: ["Bonobo"] });

    const result = await deleteLibraryPath({ path: "Bonobo/Black Sands" });

    expect(fetchMock.mock.calls[0][0]).toBe("/library/delete");
    expect(sentBody(fetchMock)).toEqual({ path: "Bonobo/Black Sands" });
    expect(result.entry.id).toBe("t1");
  });

  it("carries a selection of tracks as paths", async () => {
    const fetchMock = respondWith(200, { entry, removed: [] });

    await deleteLibraryPath({ paths: ["a.flac", "b.flac"] });

    expect(sentBody(fetchMock)).toEqual({ paths: ["a.flac", "b.flac"] });
  });

  it("raises the backend's message when a delete is refused", async () => {
    respondWith(409, { detail: "a download is writing into Bonobo" });

    await expect(deleteLibraryPath({ path: "Bonobo" })).rejects.toThrow(
      "a download is writing into Bonobo",
    );
  });

  it("reads the trash listing", async () => {
    const fetchMock = respondWith(200, { entries: [entry], track_count: 1 });

    const result = await getTrash();

    expect(fetchMock.mock.calls[0][0]).toBe("/library/trash");
    expect(result.entries).toHaveLength(1);
  });

  it("explains a failed trash listing", async () => {
    respondWith(500, { detail: "the trash folder is unreadable" });

    await expect(getTrash()).rejects.toThrow("the trash folder is unreadable");
  });

  it("posts a restore by id", async () => {
    const fetchMock = respondWith(200, { restored: [] });

    await restoreTrashEntry({ id: "t1" });

    expect(fetchMock.mock.calls[0][0]).toBe("/library/trash/restore");
    expect(sentBody(fetchMock)).toEqual({ id: "t1" });
  });

  it("carries the artist and album of a restore aimed somewhere else", async () => {
    const fetchMock = respondWith(200, { restored: [] });

    await restoreTrashEntry({ id: "t1", artist: "Bonobo", album: "" });

    expect(sentBody(fetchMock)).toEqual({
      id: "t1",
      artist: "Bonobo",
      album: "",
    });
  });

  it("throws the conflict a 409 restore carries", async () => {
    respondWith(409, {
      detail: {
        message: "already in the library",
        conflicts: ["Bonobo/Black Sands"],
      },
    });

    const failure = await restoreTrashEntry({ id: "t1" }).catch(
      (error: unknown) => error,
    );

    expect(failure).toBeInstanceOf(LibraryMoveConflict);
    expect((failure as LibraryMoveConflict).conflicts).toEqual([
      "Bonobo/Black Sands",
    ]);
  });

  it("empties the trash and reports what went", async () => {
    const fetchMock = respondWith(200, { removed: 2, track_count: 13 });

    const result = await emptyTrash();

    expect(fetchMock.mock.calls[0][0]).toBe("/library/trash/empty");
    expect(result).toEqual({ removed: 2, track_count: 13 });
  });
});
