import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "@/App";
import {
  connectQueueStream,
  fetchNotices,
  getLibrary,
  getQueue,
  getTrash,
} from "@/lib/api";
import { libraryFixture } from "@/lib/library.fixture";
import { queryKeys } from "@/lib/queryKeys";
import type { TrashEntry, TrashResponse } from "@/lib/types";

// jsdom has no EventSource, so the whole transport is replaced: the tab bar
// under test needs the stream only to not blow up.
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  connectQueueStream: vi.fn(() => () => {}),
  getQueue: vi.fn(),
  getLibrary: vi.fn(),
  getTrash: vi.fn(),
  fetchNotices: vi.fn(),
}));

const entry: TrashEntry = {
  id: "t1",
  path: "Bonobo/Black Sands",
  kind: "album",
  paths: ["Bonobo/Black Sands/Prelude.flac"],
  deleted_at: new Date().toISOString(),
  track_count: 12,
};

function renderApp(trash: TrashResponse) {
  vi.mocked(getTrash).mockResolvedValue(trash);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  render(<App />, { wrapper });
  return queryClient;
}

/** The tab currently selected, by its accessible name. */
function selectedTab(): string {
  return (
    screen
      .getAllByRole("tab")
      .find((tab) => tab.getAttribute("aria-selected") === "true")
      ?.textContent ?? ""
  );
}

beforeEach(() => {
  vi.mocked(connectQueueStream).mockClear();
  vi.mocked(getQueue).mockReset().mockResolvedValue([]);
  vi.mocked(getLibrary).mockReset().mockResolvedValue(libraryFixture());
  vi.mocked(fetchNotices).mockReset().mockResolvedValue([]);
  vi.mocked(getTrash).mockReset();
});

describe("App tabs", () => {
  it("hides the Trash tab while the trash is empty", async () => {
    renderApp({ entries: [], track_count: 0 });

    await waitFor(() => expect(getTrash).toHaveBeenCalled());
    expect(screen.queryByRole("tab", { name: /Trash/ })).toBeNull();
  });

  it("shows the Trash tab with its count once something is in it", async () => {
    renderApp({ entries: [entry], track_count: 12 });

    const tab = await screen.findByRole("tab", { name: /Trash/ });
    expect(tab.textContent).toBe("Trash1");
  });

  it("falls back to the Library when the trash empties under the open tab", async () => {
    const queryClient = renderApp({ entries: [entry], track_count: 12 });
    fireEvent.click(await screen.findByRole("tab", { name: /Trash/ }));
    await waitFor(() => expect(selectedTab()).toContain("Trash"));

    // What a restore or an empty-trash leaves behind: no entries, so the tab
    // the user is standing on stops existing.
    act(() => {
      queryClient.setQueryData<TrashResponse>(queryKeys.trash, {
        entries: [],
        track_count: 0,
      });
    });

    await waitFor(() => expect(screen.queryByRole("tab", { name: /Trash/ })).toBeNull());
    expect(selectedTab()).toBe("Library");
  });

  it("stays on the Library when the trash fills up again", async () => {
    const queryClient = renderApp({ entries: [entry], track_count: 12 });
    fireEvent.click(await screen.findByRole("tab", { name: /Trash/ }));
    await waitFor(() => expect(selectedTab()).toContain("Trash"));

    act(() => {
      queryClient.setQueryData<TrashResponse>(queryKeys.trash, {
        entries: [],
        track_count: 0,
      });
    });
    await waitFor(() => expect(selectedTab()).toBe("Library"));

    // Deleting something else brings the tab back. The fallback has to have
    // been written into state, or the user is thrown onto a tab they left.
    act(() => {
      queryClient.setQueryData<TrashResponse>(queryKeys.trash, {
        entries: [entry],
        track_count: 12,
      });
    });

    await screen.findByRole("tab", { name: /Trash/ });
    expect(selectedTab()).toBe("Library");
  });
});
