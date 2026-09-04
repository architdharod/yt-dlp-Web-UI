import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { connectQueueStream } from "@/lib/api";
import { useQueueStream } from "@/hooks/useQueueStream";
import { queryKeys } from "@/lib/queryKeys";

// jsdom has no EventSource, so the transport is replaced wholesale: the mock
// hands the test the callbacks the hook registered, which is the entire
// contract this hook has with the outside world.
vi.mock("@/lib/api", () => ({ connectQueueStream: vi.fn() }));

type Handlers = {
  onEvent: Parameters<typeof connectQueueStream>[0];
  onError: NonNullable<Parameters<typeof connectQueueStream>[1]>;
  onOpen: NonNullable<Parameters<typeof connectQueueStream>[2]>;
};

let handlers: Handlers;
let close: ReturnType<typeof vi.fn<() => void>>;
let queryClient: QueryClient;
let invalidated: unknown[];

beforeEach(() => {
  close = vi.fn<() => void>();
  vi.mocked(connectQueueStream).mockImplementation((onEvent, onError, onOpen) => {
    handlers = {
      onEvent,
      onError: onError!,
      onOpen: onOpen!,
    };
    return close;
  });

  queryClient = new QueryClient();
  invalidated = [];
  vi.spyOn(queryClient, "invalidateQueries").mockImplementation(
    (filters?: unknown) => {
      invalidated.push((filters as { queryKey?: unknown })?.queryKey);
      return Promise.resolve();
    },
  );
});

function render() {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return renderHook(() => useQueueStream(), { wrapper });
}

describe("useQueueStream", () => {
  it("does not resync on the first open: nothing was missed yet", () => {
    const { result } = render();
    expect(result.current.connected).toBe(false);

    act(() => handlers.onOpen());

    expect(result.current).toEqual({ connected: true, error: null });
    expect(invalidated).toEqual([]);
  });

  it("resyncs on the open that follows an error", () => {
    const { result } = render();
    act(() => handlers.onOpen());

    act(() => handlers.onError(new Event("error")));
    expect(result.current.connected).toBe(false);
    expect(result.current.error).not.toBeNull();

    act(() => handlers.onOpen());

    expect(result.current).toEqual({ connected: true, error: null });
    // Events emitted while it was down were lost, so both queries refetch.
    expect(invalidated).toEqual([queryKeys.queue, queryKeys.library]);
  });

  it("resyncs only once per outage", () => {
    render();
    act(() => handlers.onError(new Event("error")));
    act(() => handlers.onOpen());
    act(() => handlers.onOpen());

    expect(invalidated).toEqual([queryKeys.queue, queryKeys.library]);
  });

  it("patches the cache with the events it receives", () => {
    queryClient.setQueryData(queryKeys.queue, [
      {
        id: "a",
        url: "https://example.com/a",
        status: "downloading",
        title: "a",
        thumbnail_url: null,
        duration: null,
        progress: 0,
        error: null,
        artist: null,
        album: null,
        created_at: "2026-09-04T00:00:00Z",
      },
    ]);
    render();

    act(() =>
      handlers.onEvent({
        event: "progress",
        job_id: "a",
        data: { progress: 42 },
      }),
    );

    expect(queryClient.getQueryData<{ progress: number }[]>(queryKeys.queue)![0]
      .progress).toBe(42);
  });

  it("closes the stream on unmount", () => {
    const { unmount } = render();
    expect(close).not.toHaveBeenCalled();

    unmount();

    expect(close).toHaveBeenCalledTimes(1);
  });
});
