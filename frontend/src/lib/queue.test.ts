import { describe, expect, it } from "vitest";
import { countActiveJobs, sortJobs } from "@/lib/queue";
import type { Job, JobStatus } from "@/lib/types";

function job(id: string, status: JobStatus): Job {
  return {
    id,
    url: `https://example.com/${id}`,
    status,
    title: id,
    thumbnail_url: null,
    duration: null,
    progress: 0,
    error: null,
    artist: null,
    album: null,
    created_at: "2026-09-04T00:00:00Z",
  };
}

describe("countActiveJobs", () => {
  it("is zero before the queue has been fetched", () => {
    expect(countActiveJobs(undefined)).toBe(0);
    expect(countActiveJobs([])).toBe(0);
  });

  it("counts every working status", () => {
    const jobs = [
      job("a", "queued"),
      job("b", "downloading"),
      job("c", "converting"),
      job("d", "tagging"),
    ];
    expect(countActiveJobs(jobs)).toBe(4);
  });

  it("ignores jobs that have stopped", () => {
    const jobs = [
      job("a", "downloading"),
      job("b", "error"),
      job("c", "done"),
      job("d", "cancelled"),
    ];
    expect(countActiveJobs(jobs)).toBe(1);
  });
});

describe("sortJobs", () => {
  /** A job with an explicit creation time, for ordering. */
  function at(id: string, status: JobStatus, created_at: string): Job {
    return { ...job(id, status), created_at };
  }

  it("returns a new array, leaving the cached one untouched", () => {
    const jobs = [at("a", "error", "2026-09-01T00:00:00Z"), at("b", "queued", "2026-09-02T00:00:00Z")];

    const sorted = sortJobs(jobs);

    expect(sorted).not.toBe(jobs);
    expect(jobs.map((j) => j.id)).toEqual(["a", "b"]);
  });

  it("puts every working job ahead of every stopped one", () => {
    const sorted = sortJobs([
      at("old-error", "error", "2026-09-03T00:00:00Z"),
      at("new-queued", "queued", "2026-09-01T00:00:00Z"),
    ]);

    // The error is newer, but an active job still comes first.
    expect(sorted.map((j) => j.id)).toEqual(["new-queued", "old-error"]);
  });

  it("orders each group newest first", () => {
    const sorted = sortJobs([
      at("active-old", "downloading", "2026-09-01T00:00:00Z"),
      at("stopped-old", "error", "2026-09-02T00:00:00Z"),
      at("active-new", "tagging", "2026-09-04T00:00:00Z"),
      at("stopped-new", "done", "2026-09-05T00:00:00Z"),
    ]);

    expect(sorted.map((j) => j.id)).toEqual([
      "active-new",
      "active-old",
      "stopped-new",
      "stopped-old",
    ]);
  });

  it("handles the empty queue", () => {
    expect(sortJobs([])).toEqual([]);
  });
});
