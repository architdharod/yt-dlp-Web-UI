import { afterEach, describe, expect, it, vi } from "vitest";
import { coverUrl, moveLibraryPath } from "@/lib/api";

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
