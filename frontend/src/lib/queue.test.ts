import { describe, expect, it } from "vitest";
import {
  childCounts,
  countActiveJobs,
  isSkipped,
  sortJobs,
} from "@/lib/queue";
import {
  ALREADY_IN_LIBRARY_PREFIX,
  type Job,
  type JobStatus,
} from "@/lib/types";

function job(id: string, status: JobStatus): Job {
  return {
    id,
    kind: "download",
    parent_id: null,
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

describe("tagging jobs in the badge", () => {
  it("counts a manual metadata update while it waits and while it runs", () => {
    const tagging = (id: string, status: JobStatus): Job => ({
      ...job(id, status),
      kind: "tagging",
      parent_id: null,
      url: "",
      path: "Bonobo/Black Sands",
    });
    // A tagging job is in flight in exactly the states a download is: the
    // badge is one count over the whole queue, whatever made each row.
    expect(
      countActiveJobs([tagging("a", "queued"), tagging("b", "tagging")]),
    ).toBe(2);
    expect(
      countActiveJobs([
        tagging("a", "queued"),
        tagging("b", "error"),
        tagging("c", "cancelled"),
        job("d", "downloading"),
      ]),
    ).toBe(2);
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

/** A bulk parent holding *children*, with the counters the backend derives. */
function bulk(children: Job[]): Job {
  return {
    ...job("p1", "downloading"),
    kind: "bulk",
    title: "Black Sands",
    children,
  };
}

/** A child of "p1" in *status*, optionally carrying an error. */
function child(id: string, status: JobStatus, error: string | null = null): Job {
  return { ...job(id, status), parent_id: "p1", error };
}

describe("a bulk parent in the badge", () => {
  it("counts once, however many children it is running", () => {
    // GET /queue lists no child at the top level, so the children are only
    // reachable through the parent — and a forty-track collection must not
    // read as forty downloads on the tab badge.
    const parent = bulk([
      child("c1", "downloading"),
      child("c2", "queued"),
      child("c3", "queued"),
    ]);

    expect(countActiveJobs([parent])).toBe(1);
    expect(countActiveJobs([parent, job("d", "downloading")])).toBe(2);
  });

  it("stops counting once its derived status is terminal", () => {
    expect(
      countActiveJobs([{ ...bulk([child("c1", "error")]), status: "error" }]),
    ).toBe(0);
  });
});

describe("isSkipped", () => {
  it("is true only for the already-in-library error", () => {
    expect(
      isSkipped(child("c", "error", `${ALREADY_IN_LIBRARY_PREFIX}Kong.flac`)),
    ).toBe(true);
    expect(isSkipped(child("c", "error", "ffmpeg exploded"))).toBe(false);
    expect(isSkipped(child("c", "downloading"))).toBe(false);
  });

  it("is false for a job carrying no error at all", () => {
    expect(isSkipped(job("a", "done"))).toBe(false);
  });
});

describe("childCounts", () => {
  it("tallies every outcome a child can reach", () => {
    const parent = bulk([
      child("c1", "done"),
      child("c2", "done"),
      child("c3", "error", "ffmpeg exploded"),
      child("c4", "error", `${ALREADY_IN_LIBRARY_PREFIX}Kong.flac`),
      child("c5", "cancelled"),
      child("c6", "downloading"),
      child("c7", "queued"),
      child("c8", "converting"),
      child("c9", "tagging"),
    ]);

    expect(childCounts(parent)).toEqual({
      done: 2,
      failed: 1,
      skipped: 1,
      cancelled: 1,
      active: 4,
    });
  });

  it("keeps a duplicate out of the failure count", () => {
    // Half a collection already in the library has not half failed: the skip
    // is a neutral outcome with no Retry behind it.
    const parent = bulk([
      child("c1", "error", `${ALREADY_IN_LIBRARY_PREFIX}Kong.flac`),
      child("c2", "done"),
    ]);

    expect(childCounts(parent)).toMatchObject({ failed: 0, skipped: 1 });
  });

  it("tallies zeroes for a parent whose children have not been fetched", () => {
    const parent = { ...bulk([]), children: undefined };

    expect(childCounts(parent)).toEqual({
      done: 0,
      failed: 0,
      skipped: 0,
      cancelled: 0,
      active: 0,
    });
  });
});
