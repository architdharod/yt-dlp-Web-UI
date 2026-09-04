import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LibraryTab } from "@/components/LibraryTab";
import { getLibrary } from "@/lib/api";
import {
  album,
  artist,
  libraryFixture,
  library,
  track,
} from "@/lib/library.fixture";
import { queryKeys } from "@/lib/queryKeys";
import type { LibraryResponse } from "@/lib/types";

// Only the request is faked; `coverUrl` stays real so the <img> src the tiles
// build is the one the app would ask the backend for.
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  getLibrary: vi.fn(),
}));

const getLibraryMock = vi.mocked(getLibrary);

/** Mount the tab against a fresh client, leaving the mock to the caller. */
function renderLibraryTab() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  render(<LibraryTab />, { wrapper });
  return queryClient;
}

function renderTab(response: LibraryResponse = libraryFixture()) {
  getLibraryMock.mockResolvedValue(response);
  return renderLibraryTab();
}

/** The placeholder tiles the first load paints, before any tree has arrived. */
function skeletons() {
  return document.querySelectorAll('[data-slot="skeleton"]');
}

/** Click the tile or row whose accessible name contains *name*. */
function click(name: RegExp | string) {
  fireEvent.click(screen.getByRole("button", { name }));
}

/** Wait for the first fetch to paint, then open Bonobo's page. */
async function openBonobo() {
  await screen.findByRole("button", { name: /Bonobo/ });
  click(/Bonobo/);
}

beforeEach(() => {
  getLibraryMock.mockReset();
});

describe("LibraryTab", () => {
  it("shows the artist grid and the totals once the scan arrives", async () => {
    renderTab();

    expect(
      await screen.findByText(/2 artists · 2 albums · 6 tracks/),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: /Bonobo/ })).toBeTruthy();
    expect(screen.getByText("2 albums · 5 tracks")).toBeTruthy();
  });

  it("paints the skeleton while the scan is still in flight", async () => {
    // Resolved by hand, so the pending render is observable rather than a race
    // against a promise that settles on the next microtask.
    let settle: (response: LibraryResponse) => void = () => {};
    getLibraryMock.mockReturnValue(
      new Promise<LibraryResponse>((resolve) => {
        settle = resolve;
      }),
    );
    renderLibraryTab();

    expect(skeletons().length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: /Bonobo/ })).toBeNull();

    settle(libraryFixture());

    expect(await screen.findByRole("button", { name: /Bonobo/ })).toBeTruthy();
    expect(skeletons()).toHaveLength(0);
  });

  it("marks the synthetic root bucket as needing sorting", async () => {
    renderTab();

    await screen.findByRole("button", { name: /Unknown Artist/ });
    expect(screen.getByText("Needs sorting")).toBeTruthy();
  });

  it("renders no cover request for an artist with no album to borrow from", async () => {
    renderTab();
    await screen.findByRole("button", { name: /Bonobo/ });

    // Exactly one <img>: Bonobo's, borrowed from its cover album. The synthetic
    // bucket has no album, so the local placeholder renders instead of a
    // request that could only answer with one.
    const covers = screen.getAllByTestId("cover-image");
    expect(covers).toHaveLength(1);
    expect(covers[0].getAttribute("src")).toBe(
      "/library/cover?path=Bonobo%2FBlack%20Sands&v=1",
    );
    expect(covers[0].getAttribute("loading")).toBe("lazy");
    // Decorative: the tile button already carries the artist's name.
    expect(covers[0].getAttribute("alt")).toBe("");
  });

  it("renders no cover request for an album folder with no cover.jpg", async () => {
    renderTab(
      library([
        artist("Nils Frahm", [
          album(
            "Nils Frahm/Spaces",
            [
              track("Nils Frahm/Spaces/Says.flac", {
                title: "Says",
                track_number: 1,
              }),
            ],
            { has_cover: false },
          ),
        ]),
      ]),
    );

    await screen.findByRole("button", { name: /Nils Frahm/ });
    click(/Nils Frahm/);
    click(/Spaces/);

    expect(screen.queryAllByTestId("cover-image")).toHaveLength(0);
  });

  it("opens an artist: album tiles above the Singles section", async () => {
    renderTab();
    await openBonobo();

    const tile = screen.getByRole("button", { name: /Black Sands/ });
    expect(screen.getByRole("button", { name: /Migration/ })).toBeTruthy();
    const singles = screen.getByText("Singles");
    expect(screen.getByText("Cirrus")).toBeTruthy();

    // Ordering, not just presence: the albums come before the loose tracks.
    expect(
      tile.compareDocumentPosition(singles) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("opens the synthetic bucket under Needs sorting, not Singles", async () => {
    renderTab();

    await screen.findByRole("button", { name: /Unknown Artist/ });
    click(/Unknown Artist/);

    expect(screen.getByRole("heading", { name: "Needs sorting" })).toBeTruthy();
    expect(screen.queryByText("Singles")).toBeNull();
    expect(screen.getByText("Tycho - Awake")).toBeTruthy();
  });

  it("opens an album: a numbered list, a badge on the MP3 only", async () => {
    renderTab();
    await openBonobo();
    click(/Migration/);

    // Kerala carries TRACKNUMBER 8; Outlier carries none, and shows a dash
    // rather than a position that would read as a track number it does not have.
    expect(screen.getByText("8")).toBeTruthy();
    expect(screen.getByText("\u2014")).toBeTruthy();
    expect(screen.getByText("MP3")).toBeTruthy();
    expect(screen.queryByText("FLAC")).toBeNull();
    expect(screen.getByText("4:00")).toBeTruthy();
  });

  it("groups a multi-disc album under Disc headings", async () => {
    renderTab(
      library([
        artist("Sufjan Stevens", [
          album("Sufjan Stevens/Illinois", [
            track("Sufjan Stevens/Illinois/Concerning.flac", {
              title: "Concerning the UFO",
              track_number: 1,
              disc_number: 1,
            }),
            track("Sufjan Stevens/Illinois/Chicago.flac", {
              title: "Chicago",
              track_number: 1,
              disc_number: 2,
            }),
          ]),
        ]),
      ]),
    );

    await screen.findByRole("button", { name: /Sufjan Stevens/ });
    click(/Sufjan Stevens/);
    click(/Illinois/);

    const discs = screen.getAllByRole("heading", { name: /^Disc / });
    expect(discs.map((heading) => heading.textContent)).toEqual([
      "Disc 1",
      "Disc 2",
    ]);
    // Both discs open on a track 1; the headings are what tells them apart.
    expect(screen.getAllByText("1")).toHaveLength(2);
  });

  it("shows no Disc heading on a single-disc album", async () => {
    renderTab();
    await openBonobo();
    click(/Migration/);

    expect(screen.getByText("Kerala")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: /^Disc / })).toBeNull();
  });

  it("shows no Disc heading in the unnumbered Singles list", async () => {
    // Both singles carry a disc number, and they differ: grouping is suppressed
    // by the list being unnumbered, not by the tracks happening to agree.
    renderTab(
      library([
        artist(
          "Nils Frahm",
          [],
          [
            track("Nils Frahm/Says.flac", { title: "Says", disc_number: 1 }),
            track("Nils Frahm/Ambre.flac", { title: "Ambre", disc_number: 2 }),
          ],
        ),
      ]),
    );

    await screen.findByRole("button", { name: /Nils Frahm/ });
    click(/Nils Frahm/);

    expect(screen.getByText("Says")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: /^Disc / })).toBeNull();
  });

  it("warns on a track whose tags could not be read", async () => {
    renderTab();
    await openBonobo();
    click(/Migration/);

    expect(
      screen.getByText(/Could not read Outlier\.flac: could not read tags/),
    ).toBeTruthy();
  });

  it("shows the album header with the artist and the total duration", async () => {
    renderTab();
    await openBonobo();
    click(/Black Sands/);

    expect(screen.getByRole("heading", { name: "Black Sands" })).toBeTruthy();
    // 79s + 233s.
    expect(screen.getByText("2 tracks · 5:12")).toBeTruthy();
  });

  it("walks back up through the breadcrumb", async () => {
    renderTab();
    await openBonobo();
    click(/Black Sands/);

    click("Bonobo");
    expect(screen.getByText("Singles")).toBeTruthy();

    click("Library");
    expect(screen.getByRole("button", { name: /Unknown Artist/ })).toBeTruthy();
  });

  it("searches flat across levels and lands on the track's album", async () => {
    renderTab();
    await screen.findByRole("button", { name: /Bonobo/ });

    fireEvent.change(screen.getByLabelText("Search the library"), {
      target: { value: "kerala" },
    });

    expect(screen.getByRole("status").textContent).toBe("1 result");

    const result = screen.getByRole("button", { name: /Kerala/ });
    expect(result.textContent).toContain("Bonobo · Migration");
    fireEvent.click(result);

    // The album page, with the row the search pointed at ringed.
    expect(screen.getByRole("heading", { name: "Migration" })).toBeTruthy();
    const highlighted = document.querySelector("[data-highlighted]");
    expect(highlighted?.textContent).toContain("Kerala");
  });

  it("clears the highlight on the next navigation", async () => {
    renderTab();
    await screen.findByRole("button", { name: /Bonobo/ });

    fireEvent.change(screen.getByLabelText("Search the library"), {
      target: { value: "kerala" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Kerala/ }));
    click("Bonobo");

    expect(document.querySelector("[data-highlighted]")).toBeNull();
  });

  it("keeps the browsing position while a query is typed and cleared", async () => {
    renderTab();
    await openBonobo();

    const input = screen.getByLabelText("Search the library");
    fireEvent.change(input, { target: { value: "black" } });
    expect(screen.queryByText("Singles")).toBeNull();

    fireEvent.change(input, { target: { value: "" } });
    expect(screen.getByText("Singles")).toBeTruthy();
  });

  it("says so when nothing matches", async () => {
    renderTab();
    await screen.findByRole("button", { name: /Bonobo/ });

    fireEvent.change(screen.getByLabelText("Search the library"), {
      target: { value: "zzz" },
    });

    expect(screen.getByRole("status").textContent).toBe("No matches");
  });

  it("picks up a new track after library_changed, without moving the user", async () => {
    const queryClient = renderTab();
    await openBonobo();
    click(/Migration/);

    const grown = libraryFixture();
    grown.artists[0].albums[1] = album("Bonobo/Migration", [
      ...grown.artists[0].albums[1].tracks,
      track("Bonobo/Migration/Grains.flac", {
        title: "Grains",
        track_number: 4,
      }),
    ]);
    getLibraryMock.mockResolvedValue(grown);

    // What `applyQueueEvent` does when a `library_changed` event arrives.
    await queryClient.invalidateQueries({ queryKey: queryKeys.library });

    await waitFor(() => expect(screen.getByText("Grains")).toBeTruthy());
    // Still on the album page it was showing.
    expect(screen.getByRole("heading", { name: "Migration" })).toBeTruthy();
  });

  it("drops one level when the album under the user disappears", async () => {
    const queryClient = renderTab();
    await openBonobo();
    click(/Migration/);

    const shrunk = libraryFixture();
    shrunk.artists[0].albums = [shrunk.artists[0].albums[0]];
    getLibraryMock.mockResolvedValue(shrunk);
    await queryClient.invalidateQueries({ queryKey: queryKeys.library });

    // Back on the artist page, not a blank album page.
    await waitFor(() => expect(screen.getByText("Singles")).toBeTruthy());
  });

  it("offers a retry when the scan failed", async () => {
    getLibraryMock.mockRejectedValueOnce(new Error("scan failed"));
    renderLibraryTab();

    await screen.findByText("scan failed");
    getLibraryMock.mockResolvedValue(libraryFixture());
    click("Retry");

    expect(await screen.findByRole("button", { name: /Bonobo/ })).toBeTruthy();
  });

  it("says the library is empty rather than showing an empty grid", async () => {
    renderTab(library([]));

    expect(await screen.findByText(/Nothing in the library yet/)).toBeTruthy();
  });

  it("falls back to initials on a broken cover, and retries a new version", async () => {
    const queryClient = renderTab();
    await screen.findByRole("button", { name: /Bonobo/ });

    fireEvent.error(screen.getByTestId("cover-image"));

    // The <img> is replaced by the local placeholder, initialled from the name.
    expect(screen.queryAllByTestId("cover-image")).toHaveLength(0);
    expect(screen.getByText("B")).toBeTruthy();

    // A new cover.jpg bumps `cover_version`, so the URL is no longer the one
    // that failed and the image is attempted again.
    const repaired = libraryFixture();
    repaired.artists[0].albums[0].cover_version = 2;
    getLibraryMock.mockResolvedValue(repaired);
    await queryClient.invalidateQueries({ queryKey: queryKeys.library });

    await waitFor(() =>
      expect(screen.getByTestId("cover-image").getAttribute("src")).toBe(
        "/library/cover?path=Bonobo%2FBlack%20Sands&v=2",
      ),
    );
  });

  it("shows a track's size, path, and tags in its detail popover", async () => {
    renderTab();
    await openBonobo();
    click(/Black Sands/);

    fireEvent.click(
      screen.getByRole("button", { name: "Details for Prelude" }),
    );

    expect(
      await screen.findByText("Bonobo/Black Sands/Prelude.flac"),
    ).toBeTruthy();
    expect(screen.getByText("23.8 MB")).toBeTruthy();
    expect(screen.getByText("ARTIST")).toBeTruthy();
    // Repeated Vorbis comments are joined, so one tag stays one row.
    expect(screen.getByText("Bonobo, Andreya Triana")).toBeTruthy();
  });
});
