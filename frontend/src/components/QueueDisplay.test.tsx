import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueueDisplay } from "@/components/QueueDisplay";
import { cancelJob, dismissJob, retryJob } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import {
  ALREADY_IN_LIBRARY_PREFIX,
  type Job,
  type JobStatus,
} from "@/lib/types";

vi.mock("@/lib/api", () => ({
  // Never resolves: the rows under test are seeded into the cache, and a
  // resolved snapshot would overwrite them with whatever the mock returned.
  getQueue: vi.fn(() => new Promise<Job[]>(() => {})),
  cancelJob: vi.fn(),
  dismissJob: vi.fn(),
  retryJob: vi.fn(),
}));

function taggingJob(status: JobStatus, overrides: Partial<Job> = {}): Job {
  return {
    id: "tag-1",
    kind: "tagging",
    parent_id: null,
    url: "",
    status,
    title: "Black Sands",
    thumbnail_url: null,
    duration: null,
    progress: 0,
    error: null,
    artist: "Bonobo",
    album: "Black Sands",
    path: "Bonobo/Black Sands",
    progress_done: 3,
    progress_total: 12,
    created_at: "2026-09-05T09:00:00Z",
    ...overrides,
  };
}

let queryClient: QueryClient;

beforeEach(() => {
  vi.mocked(cancelJob).mockReset();
  vi.mocked(dismissJob).mockReset();
  vi.mocked(retryJob).mockReset();
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
      mutations: { retry: false },
    },
  });
});

function renderQueue(jobs: Job[]) {
  queryClient.setQueryData(queryKeys.queue, jobs);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  render(<QueueDisplay />, { wrapper });
}

describe("a tagging job in the queue", () => {
  it("reads as a metadata update, with the album it is about", () => {
    renderQueue([taggingJob("tagging")]);

    expect(screen.getByText("Black Sands")).toBeTruthy();
    expect(
      screen.getByText(/Updating metadata · Bonobo · Black Sands/),
    ).toBeTruthy();
    expect(screen.getByText("Tagging")).toBeTruthy();
  });

  it("counts tracks instead of showing a percentage", () => {
    renderQueue([taggingJob("tagging")]);

    expect(screen.getByText("3 of 12")).toBeTruthy();
    expect(screen.queryByText(/%$/)).toBeNull();
  });

  it("shows the size of the run before it starts", () => {
    renderQueue([taggingJob("queued", { progress_done: 0 })]);

    expect(screen.getByText("Queued")).toBeTruthy();
    expect(screen.getByText("0 of 12")).toBeTruthy();
  });

  it("counts nothing for a single track, which has nothing to count", () => {
    renderQueue([
      taggingJob("tagging", {
        title: "Cirrus",
        album: null,
        progress_done: null,
        progress_total: null,
      }),
    ]);

    expect(screen.queryByText(/ of /)).toBeNull();
    expect(screen.getByText(/Updating metadata · Bonobo/)).toBeTruthy();
  });

  it("is cancellable while queued and while tagging", async () => {
    vi.mocked(cancelJob).mockResolvedValue(
      taggingJob("cancelled", { status: "cancelled" }),
    );
    renderQueue([taggingJob("tagging")]);

    const cancel = screen.getByRole("button", {
      name: "Cancel metadata update",
    });
    fireEvent.click(cancel);

    // The mutation calls its fn with (variables, context), so only the first
    // argument is the job id.
    await waitFor(() =>
      expect(vi.mocked(cancelJob).mock.calls[0]?.[0]).toBe("tag-1"),
    );
  });

  it("offers Retry and Dismiss once it has failed", async () => {
    renderQueue([
      taggingJob("error", { error: "MusicBrainz is unreachable" }),
    ]);

    expect(screen.getByText("MusicBrainz is unreachable")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Cancel/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Retry/ }));
    await waitFor(() =>
      expect(vi.mocked(retryJob).mock.calls[0]?.[0]).toBe("tag-1"),
    );

    fireEvent.click(screen.getByRole("button", { name: /Dismiss/ }));
    await waitFor(() =>
      expect(vi.mocked(dismissJob).mock.calls[0]?.[0]).toBe("tag-1"),
    );
  });

  it("shows the note a partial run leaves behind", () => {
    renderQueue([
      taggingJob("error", {
        error: "not every track matched",
        detail: "partial: 9 of 12",
      }),
    ]);

    expect(screen.getByText("partial: 9 of 12")).toBeTruthy();
  });
});

describe("a download job in the queue", () => {
  const download: Job = {
    id: "dl-1",
    kind: "download",
    parent_id: null,
    url: "https://example.com/x",
    status: "downloading",
    title: "Kong",
    thumbnail_url: null,
    duration: 240,
    progress: 42,
    error: null,
    artist: "Bonobo",
    album: null,
    created_at: "2026-09-05T09:00:00Z",
  };

  it("still shows its percentage and its own cancel label", () => {
    renderQueue([download]);

    expect(screen.getByText("42%")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Cancel download" })).toBeTruthy();
    expect(screen.queryByText(/Updating metadata/)).toBeNull();
  });
});

describe("a bulk collection in the queue", () => {
  /** A child of the collection below. */
  function child(
    id: string,
    status: JobStatus,
    overrides: Partial<Job> = {},
  ): Job {
    return {
      id,
      kind: "download",
      parent_id: "p1",
      url: `https://youtu.be/${id}`,
      status,
      title: `Track ${id}`,
      thumbnail_url: null,
      duration: 200,
      progress: 0,
      error: null,
      artist: "Bonobo",
      album: "Black Sands",
      created_at: "2026-09-05T09:00:00Z",
      ...overrides,
    };
  }

  /** The bulk parent, with whatever children the test needs. */
  function collection(kids: Job[], overrides: Partial<Job> = {}): Job {
    return {
      id: "p1",
      kind: "bulk",
      parent_id: null,
      url: "https://youtube.com/playlist?list=X",
      status: "downloading",
      title: "Black Sands",
      thumbnail_url: null,
      duration: null,
      progress: 0,
      error: null,
      artist: "Bonobo",
      album: null,
      progress_done: kids.filter((k) => k.status === "done").length,
      progress_total: kids.length,
      created_at: "2026-09-05T09:00:00Z",
      children: kids,
      ...overrides,
    };
  }

  const mixed = collection([
    child("c1", "done"),
    child("c2", "done"),
    child("c3", "done"),
    child("c4", "error", { error: "ffmpeg exploded" }),
    child("c5", "error", {
      error: `${ALREADY_IN_LIBRARY_PREFIX}Bonobo/Kong.flac`,
    }),
    child("c6", "error", {
      error: `${ALREADY_IN_LIBRARY_PREFIX}Bonobo/Kiara.flac`,
    }),
    child("c7", "downloading"),
    child("c8", "queued"),
    child("c9", "queued"),
    child("c10", "queued"),
  ]);

  it("is one row with its progress and what became of its tracks", () => {
    renderQueue([mixed]);

    expect(screen.getByText("Black Sands")).toBeTruthy();
    expect(screen.getByText("Bonobo")).toBeTruthy();
    expect(screen.getByText("3 of 10")).toBeTruthy();
    expect(
      screen.getByText("3 done \u00B7 1 failed \u00B7 2 skipped \u00B7 4 active"),
    ).toBeTruthy();
  });

  it("leaves the zero counts out of the summary", () => {
    renderQueue([collection([child("c1", "queued"), child("c2", "queued")])]);

    expect(screen.getByText("2 active")).toBeTruthy();
    expect(screen.queryByText(/0 failed/)).toBeNull();
  });

  it("falls back to the URL when the source named nothing", () => {
    renderQueue([collection([child("c1", "queued")], { title: null })]);

    expect(
      screen.getByText("https://youtube.com/playlist?list=X"),
    ).toBeTruthy();
  });

  it("keeps its tracks hidden until the row is expanded", () => {
    renderQueue([mixed]);

    const toggle = screen.getByRole("button", { name: "Show tracks" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Track c1")).toBeNull();

    fireEvent.click(toggle);

    expect(screen.getByText("Track c1")).toBeTruthy();
    expect(screen.getByText("Track c10")).toBeTruthy();
    const hide = screen.getByRole("button", { name: "Hide tracks" });
    expect(hide.getAttribute("aria-expanded")).toBe("true");

    fireEvent.click(hide);
    expect(screen.queryByText("Track c1")).toBeNull();
  });

  it("retries one failed track by its own id, not the collection's", async () => {
    renderQueue([mixed]);
    fireEvent.click(screen.getByRole("button", { name: "Show tracks" }));

    // Only the one real failure offers a Retry; the two skips do not.
    const retries = screen.getAllByRole("button", { name: /Retry/ });
    expect(retries).toHaveLength(1);

    fireEvent.click(retries[0]);
    await waitFor(() =>
      expect(vi.mocked(retryJob).mock.calls[0]?.[0]).toBe("c4"),
    );
  });

  it("dismisses one failed track by its own id", async () => {
    renderQueue([mixed]);
    fireEvent.click(screen.getByRole("button", { name: "Show tracks" }));

    const dismissals = screen.getAllByRole("button", { name: /Dismiss/ });
    fireEvent.click(dismissals[0]);

    await waitFor(() =>
      expect(vi.mocked(dismissJob).mock.calls[0]?.[0]).toBe("c4"),
    );
  });

  it("reads a duplicate track as skipped, with nothing to retry", () => {
    renderQueue([mixed]);
    fireEvent.click(screen.getByRole("button", { name: "Show tracks" }));

    expect(screen.getAllByText("Skipped")).toHaveLength(2);
    expect(
      screen.getByText(`${ALREADY_IN_LIBRARY_PREFIX}Bonobo/Kong.flac`),
    ).toBeTruthy();
  });

  it("cancels the whole collection by the parent's id", async () => {
    vi.mocked(cancelJob).mockResolvedValue(mixed);
    renderQueue([mixed]);

    fireEvent.click(screen.getByRole("button", { name: "Cancel collection" }));

    await waitFor(() =>
      expect(vi.mocked(cancelJob).mock.calls[0]?.[0]).toBe("p1"),
    );
  });

  it("offers Dismiss but never Retry once the collection has failed", () => {
    // Retry on a parent is a 400 — what failed is a track, and that child's
    // own Retry is what re-queues it.
    renderQueue([
      collection([child("c1", "error", { error: "ffmpeg exploded" })], {
        status: "error",
      }),
    ]);

    expect(screen.getByRole("button", { name: /Dismiss/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Retry/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel collection" })).toBeNull();
  });

  it("reads a collection of nothing but duplicates as skipped", () => {
    // Every track was already on disk: the parent's status is `error` (the
    // reasons have to stay readable) but nothing failed and nothing is left
    // to retry, so it reads neutral.
    renderQueue([
      collection(
        [
          child("c1", "error", {
            error: `${ALREADY_IN_LIBRARY_PREFIX}Bonobo/Kong.flac`,
          }),
          child("c2", "error", {
            error: `${ALREADY_IN_LIBRARY_PREFIX}Bonobo/Kiara.flac`,
          }),
        ],
        { status: "error", progress_done: 2 },
      ),
    ]);

    expect(screen.getByText("Skipped")).toBeTruthy();
    expect(screen.queryByText("Failed")).toBeNull();
    expect(screen.getByText("2 tracks were already in the library")).toBeTruthy();
    expect(screen.queryByText("Some tracks could not be downloaded")).toBeNull();
    expect(screen.getByRole("button", { name: /Dismiss/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Retry/ })).toBeNull();
  });

  it("still reads as failed when a real failure is mixed in with the skips", () => {
    renderQueue([
      collection(
        [
          child("c1", "error", { error: "ffmpeg exploded" }),
          child("c2", "error", {
            error: `${ALREADY_IN_LIBRARY_PREFIX}Bonobo/Kiara.flac`,
          }),
        ],
        { status: "error" },
      ),
    ]);

    expect(screen.getByText("Some tracks could not be downloaded")).toBeTruthy();
    expect(screen.queryByText(/already in the library/)).toBeNull();
  });

  it("shows a refused cancel on the collection row", () => {
    renderQueue([
      collection([child("c1", "queued")], { error: "Job already finished" }),
    ]);

    expect(screen.getByText("Job already finished")).toBeTruthy();
  });
});
