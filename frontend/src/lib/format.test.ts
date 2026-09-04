import { describe, expect, it } from "vitest";
import { formatDuration, formatSize, plural } from "@/lib/format";

describe("formatDuration", () => {
  it("renders minutes and padded seconds", () => {
    expect(formatDuration(233)).toBe("3:53");
  });

  it("adds an hours field for a long album total", () => {
    expect(formatDuration(3725)).toBe("1:02:05");
  });

  it("says nothing useful for a track with no duration", () => {
    expect(formatDuration(null)).toBe("--:--");
  });
});

describe("formatSize", () => {
  it("leaves bytes whole", () => {
    expect(formatSize(512)).toBe("512 B");
  });

  it("steps up a unit at a time", () => {
    expect(formatSize(25_000_000)).toBe("23.8 MB");
    expect(formatSize(3 * 1024 ** 3)).toBe("3.0 GB");
  });
});

describe("plural", () => {
  it("drops the s for one", () => {
    expect(plural(1, "album")).toBe("1 album");
    expect(plural(0, "album")).toBe("0 albums");
  });
});
