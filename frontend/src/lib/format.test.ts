import { describe, expect, it } from "vitest";
import {
  formatDuration,
  formatRelativeTime,
  formatSize,
  plural,
} from "@/lib/format";

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

describe("formatRelativeTime", () => {
  const now = Date.parse("2026-09-04T12:00:00Z");
  const ago = (iso: string) => formatRelativeTime(iso, now);

  it("calls anything inside the last minute just now", () => {
    expect(ago("2026-09-04T11:59:30Z")).toBe("just now");
  });

  it("counts whole minutes, hours, and days", () => {
    expect(ago("2026-09-04T11:59:00Z")).toBe("1 minute ago");
    expect(ago("2026-09-04T11:48:00Z")).toBe("12 minutes ago");
    expect(ago("2026-09-04T09:30:00Z")).toBe("2 hours ago");
    expect(ago("2026-09-01T12:00:00Z")).toBe("3 days ago");
  });

  it("falls back to a date once the count stops meaning anything", () => {
    // Two months back: "62 days ago" is worse than the date itself.
    expect(ago("2026-07-04T12:00:00Z")).toBe(
      new Date(Date.parse("2026-07-04T12:00:00Z")).toLocaleDateString(),
    );
  });

  it("shows an unparseable timestamp as it came, rather than NaN", () => {
    expect(ago("not a date")).toBe("not a date");
  });

  it("treats a clock a little behind the backend as just now", () => {
    expect(ago("2026-09-04T12:00:05Z")).toBe("just now");
  });
});
