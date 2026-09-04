import { describe, expect, it } from "vitest";
import { coverUrl } from "@/lib/api";

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
