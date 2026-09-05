import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useTrashCount } from "@/hooks/useTrashQuery";
import { getTrash } from "@/lib/api";
import type { TrashEntry } from "@/lib/types";

vi.mock("@/lib/api", () => ({ getTrash: vi.fn() }));

const getTrashMock = vi.mocked(getTrash);

function entry(id: string): TrashEntry {
  return {
    id,
    path: `Bonobo/${id}`,
    kind: "album",
    paths: [`Bonobo/${id}/Prelude.flac`],
    deleted_at: "2026-09-04T11:00:00Z",
    track_count: 1,
  };
}

function renderCount() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return renderHook(() => useTrashCount(), { wrapper });
}

beforeEach(() => {
  getTrashMock.mockReset();
});

describe("useTrashCount", () => {
  it("counts the entries the query returned", async () => {
    getTrashMock.mockResolvedValue({
      entries: [entry("a"), entry("b")],
      track_count: 9,
    });

    const { result } = renderCount();

    await waitFor(() => expect(result.current).toBe(2));
  });

  it("is zero while the trash is still loading", () => {
    getTrashMock.mockReturnValue(new Promise(() => {}));

    expect(renderCount().result.current).toBe(0);
  });

  it("is zero when the request fails, so the tab stays hidden", async () => {
    getTrashMock.mockRejectedValue(new Error("no trash for you"));

    const { result } = renderCount();

    await waitFor(() => expect(getTrashMock).toHaveBeenCalled());
    expect(result.current).toBe(0);
  });
});
