import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LibraryMoveConflict, tagLibraryPath } from "@/lib/api";
import { useTagMutation } from "@/hooks/useTagMutation";
import { queryKeys } from "@/lib/queryKeys";
import type { Job } from "@/lib/types";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  tagLibraryPath: vi.fn(),
}));

const tagMock = vi.mocked(tagLibraryPath);

function taggingJob(path: string): Job {
  return {
    id: "job-1",
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
    path,
    progress_done: 0,
    progress_total: 12,
    created_at: "2026-09-05T09:00:00Z",
  };
}

let queryClient: QueryClient;

beforeEach(() => {
  tagMock.mockReset();
  queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
});

function render(seedQueue: Job[] = []) {
  queryClient.setQueryData(queryKeys.queue, seedQueue);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return renderHook(() => useTagMutation(), { wrapper });
}

/** The jobs currently in the ["queue"] cache. */
function queue(): Job[] {
  return queryClient.getQueryData<Job[]>(queryKeys.queue)!;
}

describe("useTagMutation", () => {
  it("posts the path and puts the new job straight into the queue", async () => {
    const job = taggingJob("Bonobo/Black Sands");
    tagMock.mockResolvedValue(job);
    const { result } = render();

    act(() => result.current.tagPath("Bonobo/Black Sands"));

    await waitFor(() => expect(queue()).toHaveLength(1));
    expect(tagMock).toHaveBeenCalledWith({ path: "Bonobo/Black Sands" });
    expect(queue()[0]).toEqual(job);
    expect(result.current.feedback).toEqual({
      message: "Metadata update queued — watch the Download tab.",
      failed: false,
    });
  });

  it("holds the path pending while the request is out", async () => {
    let settle: (job: Job) => void = () => {};
    tagMock.mockReturnValue(
      new Promise<Job>((resolve) => {
        settle = resolve;
      }),
    );
    const { result } = render();

    act(() => result.current.tagPath("Bonobo/Black Sands"));

    await waitFor(() =>
      expect(result.current.pending.has("Bonobo/Black Sands")).toBe(true),
    );
    // A second click while the first is out would only earn a 409.
    act(() => result.current.tagPath("Bonobo/Black Sands"));
    expect(tagMock).toHaveBeenCalledTimes(1);

    act(() => settle(taggingJob("Bonobo/Black Sands")));

    await waitFor(() => expect(result.current.pending.size).toBe(0));
  });

  it("shows the backend's sentence when the path is already being tagged", async () => {
    tagMock.mockRejectedValue(
      new LibraryMoveConflict("Bonobo/Black Sands is already being tagged", [
        "Bonobo/Black Sands",
      ]),
    );
    const { result } = render();

    act(() => result.current.tagPath("Bonobo/Black Sands"));

    await waitFor(() => expect(result.current.feedback).not.toBeNull());
    expect(result.current.feedback).toEqual({
      message: "Bonobo/Black Sands is already being tagged",
      failed: true,
    });
    // Nothing was queued, so nothing joins the in-flight list.
    expect(queue()).toHaveLength(0);
  });

  it("falls back to its own message when the failure carries none", async () => {
    tagMock.mockRejectedValue(new Error(""));
    const { result } = render();

    act(() => result.current.tagPath("Bonobo/Cirrus.flac"));

    await waitFor(() => expect(result.current.feedback).not.toBeNull());
    expect(result.current.feedback?.message).toBe(
      "Could not queue the metadata update",
    );
  });

  it("drops the previous answer when a new request goes out", async () => {
    tagMock.mockRejectedValueOnce(new Error("nope"));
    const { result } = render();

    act(() => result.current.tagPath("a.flac"));
    await waitFor(() => expect(result.current.feedback?.failed).toBe(true));

    tagMock.mockResolvedValueOnce(taggingJob("b.flac"));
    act(() => result.current.tagPath("b.flac"));

    await waitFor(() => expect(result.current.feedback?.failed).toBe(false));
    expect(result.current.feedback?.message).toBe(
      "Metadata update queued — watch the Download tab.",
    );
  });
});
