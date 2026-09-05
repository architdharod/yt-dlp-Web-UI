import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useActiveJobCount } from "@/hooks/useQueueQuery";
import { queryKeys } from "@/lib/queryKeys";
import type { Job, JobStatus } from "@/lib/types";

// The badge is read straight out of the cache; mocking the request keeps the
// "not fetched yet" case from firing a real fetch("/queue") into jsdom, which
// has no server behind it.
vi.mock("@/lib/api", () => ({ getQueue: vi.fn(() => new Promise<Job[]>(() => {})) }));

function job(id: string, status: JobStatus): Job {
  return {
    id,
    kind: "download",
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

/** Render a hook against a cache seeded with *jobs*, with no network at all. */
function renderWithQueue(jobs: Job[] | undefined) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
    },
  });
  if (jobs !== undefined) {
    queryClient.setQueryData(queryKeys.queue, jobs);
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return renderHook(() => useActiveJobCount(), { wrapper });
}

describe("useActiveJobCount", () => {
  it("is zero while the queue has not been fetched", () => {
    expect(renderWithQueue(undefined).result.current).toBe(0);
  });

  it("counts only the jobs still working", () => {
    const { result } = renderWithQueue([
      job("a", "queued"),
      job("b", "downloading"),
      job("c", "converting"),
      job("d", "tagging"),
      job("e", "error"),
    ]);

    expect(result.current).toBe(4);
  });

  it("is zero when every job has stopped, so the badge hides", () => {
    const { result } = renderWithQueue([job("a", "error")]);

    expect(result.current).toBe(0);
  });
});
