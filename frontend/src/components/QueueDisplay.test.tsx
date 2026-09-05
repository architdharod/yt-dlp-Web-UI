import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueueDisplay } from "@/components/QueueDisplay";
import { cancelJob, dismissJob, retryJob } from "@/lib/api";
import { queryKeys } from "@/lib/queryKeys";
import type { Job, JobStatus } from "@/lib/types";

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
