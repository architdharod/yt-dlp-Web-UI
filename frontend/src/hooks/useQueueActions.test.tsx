import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, act, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cancelJob, dismissJob, retryJob } from "@/lib/api";
import { useQueueActions } from "@/hooks/useQueueActions";
import { queryKeys } from "@/lib/queryKeys";
import type { Job, JobStatus } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  cancelJob: vi.fn(),
  dismissJob: vi.fn(),
  retryJob: vi.fn(),
}));

function job(id: string, status: JobStatus = "downloading"): Job {
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

let queryClient: QueryClient;

beforeEach(() => {
  vi.mocked(cancelJob).mockReset();
  vi.mocked(dismissJob).mockReset();
  vi.mocked(retryJob).mockReset();
  queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
});

/** Render the actions against a cache seeded with *jobs*. */
function render(jobs: Job[]) {
  queryClient.setQueryData(queryKeys.queue, jobs);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return renderHook(() => useQueueActions(), { wrapper });
}

/** The jobs currently in the ["queue"] cache. */
function queue(): Job[] {
  return queryClient.getQueryData<Job[]>(queryKeys.queue)!;
}

describe("cancel", () => {
  it("leaves the row alone when the job is still running", async () => {
    // The response is a snapshot from before the request, so writing it into
    // the cache would revert whatever the stream has delivered since.
    vi.mocked(cancelJob).mockResolvedValue(job("a", "downloading"));
    const { result } = render([job("a", "converting")]);

    act(() => result.current.cancelJob("a"));
    // (TanStack passes a context object as a second argument.)
    await waitFor(() => expect(cancelJob).toHaveBeenCalled());
    expect(vi.mocked(cancelJob).mock.calls[0][0]).toBe("a");
    await waitFor(() => expect(result.current.cancelling.has("a")).toBe(false));

    expect(queue()).toEqual([job("a", "converting")]);
  });

  it("removes the row when the job came back already cancelled", async () => {
    vi.mocked(cancelJob).mockResolvedValue(job("a", "cancelled"));
    const { result } = render([job("a", "queued"), job("b")]);

    act(() => result.current.cancelJob("a"));

    await waitFor(() => expect(queue().map((j) => j.id)).toEqual(["b"]));
  });

  it("disables the button while the request is in flight", async () => {
    let release!: (job: Job) => void;
    vi.mocked(cancelJob).mockReturnValue(
      new Promise<Job>((resolve) => {
        release = resolve;
      }),
    );
    const { result } = render([job("a", "queued")]);

    act(() => result.current.cancelJob("a"));
    await waitFor(() => expect(result.current.cancelling.has("a")).toBe(true));

    await act(async () => {
      release(job("a", "cancelled"));
    });

    await waitFor(() => expect(result.current.cancelling.has("a")).toBe(false));
  });

  it("writes a refused cancel onto the row", async () => {
    vi.mocked(cancelJob).mockRejectedValue(new Error("Job already finished"));
    const { result } = render([job("a", "queued")]);

    act(() => result.current.cancelJob("a"));

    await waitFor(() =>
      expect(queue()[0]).toMatchObject({
        id: "a",
        status: "queued",
        error: "Job already finished",
      }),
    );
  });
});

describe("dismiss", () => {
  it("removes the row once the backend agreed", async () => {
    vi.mocked(dismissJob).mockResolvedValue(undefined);
    const { result } = render([job("a", "error"), job("b")]);

    act(() => result.current.dismissJob("a"));

    await waitFor(() => expect(queue().map((j) => j.id)).toEqual(["b"]));
  });

  it("writes a failed dismiss onto the row", async () => {
    vi.mocked(dismissJob).mockRejectedValue(new Error("nope"));
    const { result } = render([job("a", "error")]);

    act(() => result.current.dismissJob("a"));

    await waitFor(() => expect(queue()[0].error).toBe("nope"));
  });
});

describe("retry", () => {
  it("replaces the row with the re-queued job", async () => {
    vi.mocked(retryJob).mockResolvedValue(job("a", "queued"));
    const { result } = render([
      { ...job("a", "error"), error: "ffmpeg exploded" },
      job("b"),
    ]);

    act(() => result.current.retryJob("a"));

    await waitFor(() =>
      expect(queue()[0]).toMatchObject({ status: "queued", error: null }),
    );
    expect(queue().map((j) => j.id)).toEqual(["a", "b"]);
  });
});

describe("addJob", () => {
  it("puts a newly submitted job in the cache", () => {
    const { result } = render([job("a")]);

    act(() => result.current.addJob(job("b", "queued")));

    expect(queue().map((j) => j.id)).toEqual(["a", "b"]);
  });
});
