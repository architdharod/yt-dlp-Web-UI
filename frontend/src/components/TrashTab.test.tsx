import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TrashTab } from "@/components/TrashTab";
import {
  LibraryMoveConflict,
  emptyTrash,
  getLibrary,
  getTrash,
  restoreTrashEntry,
} from "@/lib/api";
import { libraryFixture } from "@/lib/library.fixture";
import type { TrashEntry, TrashEntryKind } from "@/lib/types";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  getLibrary: vi.fn(),
  getTrash: vi.fn(),
  restoreTrashEntry: vi.fn(),
  emptyTrash: vi.fn(),
}));

const getTrashMock = vi.mocked(getTrash);
const getLibraryMock = vi.mocked(getLibrary);
const restoreMock = vi.mocked(restoreTrashEntry);
const emptyMock = vi.mocked(emptyTrash);

function entry(
  id: string,
  overrides: Partial<TrashEntry> & { kind?: TrashEntryKind } = {},
): TrashEntry {
  return {
    id,
    path: "Bonobo/Black Sands",
    kind: "album",
    paths: ["Bonobo/Black Sands/Prelude.flac"],
    deleted_at: anHourAgo(),
    track_count: 12,
    ...overrides,
  };
}

function renderTrash(entries: TrashEntry[], trackCount = 12) {
  getTrashMock.mockResolvedValue({ entries, track_count: trackCount });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  render(<TrashTab />, { wrapper });
}

function click(name: RegExp | string) {
  fireEvent.click(screen.getByRole("button", { name }));
}

/** Type *value* into the dialog field labelled *label*. */
function fill(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

/** What the field labelled *label* currently holds. */
function fieldValue(label: RegExp): string {
  return (screen.getByLabelText(label) as HTMLInputElement).value;
}

const occupied = new LibraryMoveConflict("Already in the library", [
  "Bonobo/Black Sands",
]);

/** An hour ago, by whatever clock the test run is on. */
function anHourAgo(): string {
  return new Date(Date.now() - 60 * 60 * 1000).toISOString();
}

beforeEach(() => {
  getTrashMock.mockReset();
  getLibraryMock.mockReset().mockResolvedValue(libraryFixture());
  restoreMock.mockReset().mockResolvedValue({ restored: [] });
  emptyMock.mockReset().mockResolvedValue({ removed: 1, track_count: 12 });
});

describe("TrashTab", () => {
  it("heads the list with the item count", async () => {
    renderTrash([entry("a"), entry("b", { path: "Bonobo/Migration" })]);

    expect(await screen.findByText("Trash · 2 items")).toBeTruthy();
  });

  it("shows the original path, the track count, and how long ago it went", async () => {
    renderTrash([entry("a")]);

    expect(await screen.findByText("Bonobo/Black Sands")).toBeTruthy();
    expect(screen.getByText("12 tracks · deleted 1 hour ago")).toBeTruthy();
  });

  it("restores by id when Restore is pressed", async () => {
    renderTrash([entry("a")]);
    await screen.findByText("Bonobo/Black Sands");

    click(/^Restore Bonobo/);

    await waitFor(() => expect(restoreMock).toHaveBeenCalledTimes(1));
    expect(restoreMock.mock.calls[0][0]).toEqual({ id: "a" });
  });

  it("reports a restore that failed for a reason the user cannot answer", async () => {
    restoreMock.mockRejectedValue(new Error("the trash folder is unreadable"));
    renderTrash([entry("a")]);
    await screen.findByText("Bonobo/Black Sands");

    click(/^Restore Bonobo/);

    expect(
      await screen.findByText("the trash folder is unreadable"),
    ).toBeTruthy();
  });

  it("opens the move dialog prefilled when the original path is occupied", async () => {
    restoreMock.mockRejectedValueOnce(occupied);
    renderTrash([entry("a")]);
    await screen.findByText("Bonobo/Black Sands");

    click(/^Restore Bonobo/);

    expect(await screen.findByText("Restore elsewhere")).toBeTruthy();
    expect(
      screen.getByText('Restore "Bonobo/Black Sands" (12 tracks) somewhere else.'),
    ).toBeTruthy();
    // Prefilled from the entry's own path, and carrying the conflict the 409
    // named so the user knows what is in the way.
    expect(fieldValue(/^Artist$/)).toBe("Bonobo");
    expect(fieldValue(/^Album$/)).toBe("Black Sands");
    expect(screen.getByText("Already in the library")).toBeTruthy();
  });

  it("re-sends the restore with the artist and album the dialog collected", async () => {
    restoreMock.mockRejectedValueOnce(occupied);
    renderTrash([entry("a")]);
    await screen.findByText("Bonobo/Black Sands");
    click(/^Restore Bonobo/);
    await screen.findByText("Restore elsewhere");

    fill(/^Album$/, "Black Sands Remixed");
    click(/^Restore$/);

    await waitFor(() => expect(restoreMock).toHaveBeenCalledTimes(2));
    expect(restoreMock.mock.calls[1][0]).toEqual({
      id: "a",
      artist: "Bonobo",
      album: "Black Sands Remixed",
    });
    // It landed, so the dialog goes.
    await waitFor(() =>
      expect(screen.queryByText("Restore elsewhere")).toBeNull(),
    );
  });

  it("names the library root for tracks that had no folder above them", async () => {
    renderTrash([entry("a", { kind: "tracks", path: "", paths: ["Awake.flac"] })]);

    expect(await screen.findByText("Library root")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Restore Library root" }),
    ).toBeTruthy();
  });

  it("names the library root in the dialog a conflicted restore opens", async () => {
    restoreMock.mockRejectedValueOnce(occupied);
    renderTrash([entry("a", { kind: "tracks", path: "", paths: ["Awake.flac"] })]);
    await screen.findByText("Library root");

    click(/^Restore Library root$/);

    expect(
      await screen.findByText('Restore "Library root" (12 tracks) somewhere else.'),
    ).toBeTruthy();
  });

  it("reports a conflict in place while the library is still loading", async () => {
    // No dialog can open without the library's names, so the 409 has nowhere
    // to go but the top of the tab.
    getLibraryMock.mockImplementation(() => new Promise(() => {}));
    restoreMock.mockRejectedValueOnce(occupied);
    renderTrash([entry("a")]);
    await screen.findByText("Bonobo/Black Sands");

    click(/^Restore Bonobo/);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("Already in the library");
    expect(screen.queryByText("Restore elsewhere")).toBeNull();
  });

  it("reports a conflict in place when the library query failed", async () => {
    getLibraryMock.mockRejectedValue(new Error("the library is unreadable"));
    restoreMock.mockRejectedValueOnce(occupied);
    renderTrash([entry("a")]);
    await screen.findByText("Bonobo/Black Sands");

    click(/^Restore Bonobo/);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("Already in the library");
    expect(screen.queryByText("Restore elsewhere")).toBeNull();
  });

  it("leaves no error behind when the conflict dialog is cancelled", async () => {
    restoreMock.mockRejectedValueOnce(occupied);
    renderTrash([entry("a")]);
    await screen.findByText("Bonobo/Black Sands");
    click(/^Restore Bonobo/);
    await screen.findByText("Restore elsewhere");

    click(/^Cancel$/);

    await waitFor(() =>
      expect(screen.queryByText("Restore elsewhere")).toBeNull(),
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("offers an artist entry only an artist to restore under", async () => {
    restoreMock.mockRejectedValueOnce(occupied);
    renderTrash([
      entry("a", { kind: "artist", path: "Bonobo", paths: ["Bonobo/x.flac"] }),
    ]);
    await screen.findByText("Bonobo");

    click(/^Restore Bonobo$/);
    await screen.findByText("Restore elsewhere");

    expect(screen.queryByLabelText(/^Album$/)).toBeNull();

    fill(/^Artist$/, "Bonobo (UK)");
    click(/^Restore$/);

    await waitFor(() => expect(restoreMock).toHaveBeenCalledTimes(2));
    expect(restoreMock.mock.calls[1][0]).toEqual({
      id: "a",
      artist: "Bonobo (UK)",
    });
  });

  it("confirms an empty with the totals before destroying anything", async () => {
    renderTrash([entry("a"), entry("b", { path: "Bonobo/Migration" })], 13);
    await screen.findByText("Trash · 2 items");

    click(/^Empty trash$/);

    expect(
      await screen.findByText("Permanently delete 2 items (13 tracks)?"),
    ).toBeTruthy();
    expect(screen.getByText("This permanently deletes the files.")).toBeTruthy();
    expect(emptyMock).not.toHaveBeenCalled();

    // Two buttons now read "Empty trash"; the one in the dialog is the last.
    const buttons = screen.getAllByRole("button", { name: /^Empty trash$/ });
    fireEvent.click(buttons[buttons.length - 1]);

    await waitFor(() => expect(emptyMock).toHaveBeenCalledTimes(1));
  });

  it("leaves the trash alone when the empty is cancelled", async () => {
    renderTrash([entry("a")]);
    await screen.findByText("Trash · 1 item");

    click(/^Empty trash$/);
    await screen.findByText("Permanently delete 1 item (12 tracks)?");
    click(/^Cancel$/);

    await waitFor(() =>
      expect(screen.queryByText("Permanently delete 1 item (12 tracks)?")).toBeNull(),
    );
    expect(emptyMock).not.toHaveBeenCalled();
  });

  it("does not carry a failed empty into the next confirmation", async () => {
    emptyMock.mockRejectedValue(new Error("Disk is read-only"));
    renderTrash([entry("a")]);
    await screen.findByText("Trash · 1 item");

    click(/^Empty trash$/);
    await screen.findByText("Permanently delete 1 item (12 tracks)?");
    const buttons = screen.getAllByRole("button", { name: /^Empty trash$/ });
    fireEvent.click(buttons[buttons.length - 1]);
    await screen.findByText("Disk is read-only");

    click(/^Cancel$/);
    await waitFor(() =>
      expect(screen.queryByText("Disk is read-only")).toBeNull(),
    );

    click(/^Empty trash$/);
    await screen.findByText("Permanently delete 1 item (12 tracks)?");
    expect(screen.queryByText("Disk is read-only")).toBeNull();
  });
});
