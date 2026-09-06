import { afterEach, describe, expect, it, vi } from "vitest";
import {
  LibraryMoveConflict,
  coverUrl,
  deleteLibraryPath,
  emptyTrash,
  fetchNotices,
  getTrash,
  moveLibraryPath,
  probeUrl,
  restoreTrashEntry,
  submitBulkDownload,
  tagLibraryPath,
} from "@/lib/api";
import type { BulkDownloadRequest } from "@/lib/types";

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

describe("tagLibraryPath", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const job = {
    id: "j1",
    kind: "tagging",
    parent_id: null,
    url: "",
    status: "queued",
    title: "Black Sands",
    thumbnail_url: null,
    duration: null,
    progress: 0,
    error: null,
    artist: "Bonobo",
    album: "Black Sands",
    path: "Bonobo/Black Sands",
    progress_done: 0,
    progress_total: 12,
    created_at: "2026-09-05T09:00:00Z",
  };

  it("posts the path and returns the tagging job it created", async () => {
    const fetchMock = respondWith(200, job);

    const created = await tagLibraryPath({ path: "Bonobo/Black Sands" });

    expect(fetchMock.mock.calls[0][0]).toBe("/library/tag");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("POST");
    expect(sentBody(fetchMock)).toEqual({ path: "Bonobo/Black Sands" });
    expect(created.kind).toBe("tagging");
    expect(created.progress_total).toBe(12);
  });

  it("raises the backend's message when the path is already being tagged", async () => {
    respondWith(409, {
      detail: {
        message: "Bonobo/Black Sands is already being tagged",
        conflicts: ["Bonobo/Black Sands"],
      },
    });

    const failure = await tagLibraryPath({
      path: "Bonobo/Black Sands",
    }).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(LibraryMoveConflict);
    expect((failure as Error).message).toBe(
      "Bonobo/Black Sands is already being tagged",
    );
  });

  it("reports a 404 with the backend's message", async () => {
    respondWith(404, { detail: "no such track" });

    await expect(tagLibraryPath({ path: "Nope.flac" })).rejects.toThrow(
      "no such track",
    );
  });
});

describe("probeUrl", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts the URL and reads back a single track", async () => {
    const fetchMock = respondWith(200, {
      type: "track",
      title: "Kong",
      duration: 240,
      thumbnail_url: null,
      artist: "Bonobo",
      album: null,
    });

    const probed = await probeUrl("https://youtu.be/abc");

    expect(fetchMock.mock.calls[0][0]).toBe("/download/probe");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("POST");
    expect(sentBody(fetchMock)).toEqual({ url: "https://youtu.be/abc" });
    // The union is discriminated on `type`, so the caller narrows on it.
    expect(probed.type).toBe("track");
  });

  it("reads back a collection preview with its rows and counts", async () => {
    respondWith(200, {
      type: "collection",
      preview: {
        url: "https://youtube.com/playlist?list=X",
        title: "Black Sands",
        artist: "Bonobo",
        source: "youtube",
        rows: [
          {
            id: "r1",
            url: "https://youtu.be/1",
            source_id: "youtube:1",
            title: "Prelude",
            album: "Black Sands",
            duration: 71,
            thumbnail_url: null,
            status: "in_library",
            reason: "already in library",
          },
        ],
        total: 1,
        in_library: 1,
        unavailable: 0,
        large: false,
        notices: [],
      },
    });

    const probed = await probeUrl("https://youtube.com/playlist?list=X");

    expect(probed.type).toBe("collection");
    if (probed.type !== "collection") throw new Error("expected a collection");
    expect(probed.preview.rows[0].status).toBe("in_library");
    expect(probed.preview.in_library).toBe(1);
  });

  it("sends the artist the form is showing so dedup runs against it", async () => {
    const fetchMock = respondWith(200, {
      type: "track",
      title: "Kong",
      duration: 240,
      thumbnail_url: null,
      artist: "Bonobo",
      album: null,
    });

    await probeUrl("https://youtu.be/abc", "Zoe Keating");

    expect(sentBody(fetchMock)).toEqual({
      url: "https://youtu.be/abc",
      artist: "Zoe Keating",
    });
  });

  it("leaves the artist out when there is none to send", async () => {
    const fetchMock = respondWith(200, {
      type: "track",
      title: "Kong",
      duration: 240,
      thumbnail_url: null,
      artist: null,
      album: null,
    });

    await probeUrl("https://youtu.be/abc", "");

    expect(sentBody(fetchMock)).toEqual({ url: "https://youtu.be/abc" });
  });

  it("raises the 2000-track stop as the backend worded it", async () => {
    respondWith(400, {
      detail: "This collection has more than 2000 tracks; try a narrower URL.",
    });

    await expect(probeUrl("https://youtube.com/@huge")).rejects.toThrow(
      "This collection has more than 2000 tracks; try a narrower URL.",
    );
  });

  it("raises a timeout with the backend's message", async () => {
    respondWith(504, { detail: "the source took too long to answer" });

    await expect(probeUrl("https://youtube.com/@slow")).rejects.toThrow(
      "the source took too long to answer",
    );
  });
});

describe("submitBulkDownload", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const request: BulkDownloadRequest = {
    url: "https://youtube.com/playlist?list=X",
    artist: "Bonobo",
    title: "Black Sands",
    tracks: [
      {
        url: "https://youtu.be/1",
        title: "Prelude",
        album: "Black Sands",
        album_final: false,
        duration: 71,
        thumbnail_url: null,
        source_id: "youtube:1",
      },
    ],
  };

  const parent = {
    id: "p1",
    kind: "bulk",
    parent_id: null,
    url: request.url,
    status: "queued",
    title: "Black Sands",
    thumbnail_url: null,
    duration: null,
    progress: 0,
    error: null,
    artist: "Bonobo",
    album: null,
    progress_done: 0,
    progress_total: 1,
    created_at: "2026-09-05T09:00:00Z",
    children: [
      {
        id: "c1",
        kind: "download",
        parent_id: "p1",
        url: "https://youtu.be/1",
        status: "queued",
        title: "Prelude",
        thumbnail_url: null,
        duration: 71,
        progress: 0,
        error: null,
        artist: "Bonobo",
        album: "Black Sands",
        created_at: "2026-09-05T09:00:00Z",
      },
    ],
  };

  it("posts the whole selection and returns the parent with its children", async () => {
    const fetchMock = respondWith(200, parent);

    const created = await submitBulkDownload(request);

    expect(fetchMock.mock.calls[0][0]).toBe("/download/bulk");
    expect(sentBody(fetchMock)).toEqual(request);
    expect(created.kind).toBe("bulk");
    expect(created.children).toHaveLength(1);
    expect(created.children![0].parent_id).toBe("p1");
  });

  it("raises the 409 a collection already in the queue answers with", async () => {
    respondWith(409, { detail: "this collection is already in the queue" });

    await expect(submitBulkDownload(request)).rejects.toThrow(
      "this collection is already in the queue",
    );
  });
});

describe("fetchNotices", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function respond(body: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    );
  }

  const notice = {
    id: "n1",
    level: "warning",
    source: "youtube",
    message: "YouTube is rate limiting this server.",
    hold_until: "2026-09-06T12:01:00Z",
    reason: "rate_limit",
    held_since: "2026-09-06T12:00:00Z",
    created_at: "2026-09-06T12:00:00Z",
  };

  it("goes through the same parser the SSE event does", async () => {
    // An action that is not a path must not survive either route in.
    respond([
      {
        ...notice,
        action: { label: "Go", method: "POST", path: "//evil.example/x" },
      },
    ]);

    const [parsed] = await fetchNotices();

    expect(parsed.action).toBeNull();
    expect(parsed.hold_until).toBe("2026-09-06T12:01:00Z");
  });

  it("drops an entry that is not a notice at all", async () => {
    respond([{ id: 7 }, notice]);

    expect(await fetchNotices()).toHaveLength(1);
  });
});
