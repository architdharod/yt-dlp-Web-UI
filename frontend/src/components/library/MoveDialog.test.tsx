import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  MoveDialog,
  buildMoveRequest,
  type MoveTarget,
} from "@/components/library/MoveDialog";
import { LibraryMoveConflict, moveLibraryPath } from "@/lib/api";
import { libraryFixture } from "@/lib/library.fixture";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  moveLibraryPath: vi.fn(),
}));

const moveMock = vi.mocked(moveLibraryPath);

const fixture = libraryFixture();
const bonobo = fixture.artists[0];
const blackSands = bonobo.albums[0];

const trackTarget: MoveTarget = {
  kind: "tracks",
  artist: bonobo,
  album: blackSands,
  tracks: [blackSands.tracks[0]],
};
const albumTarget: MoveTarget = {
  kind: "album",
  artist: bonobo,
  album: blackSands,
};
const artistTarget: MoveTarget = { kind: "artist", artist: bonobo };

function renderDialog(target: MoveTarget, onClose = vi.fn()) {
  const onMoved = vi.fn();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  render(
    <MoveDialog
      target={target}
      library={fixture}
      onClose={onClose}
      onMoved={onMoved}
    />,
    { wrapper },
  );
  return { onClose, onMoved };
}

/** Type *value* into the field labelled *label*. */
function fill(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

function submit(name: RegExp) {
  fireEvent.click(screen.getByRole("button", { name }));
}

beforeEach(() => {
  moveMock.mockReset();
  moveMock.mockResolvedValue({ moved: [], removed: [], destination: null });
});

describe("MoveDialog", () => {
  it("sends the track paths, the artist, and the album", async () => {
    renderDialog(trackTarget);

    fill(/^Artist$/, "Ninja Tune");
    fill(/^Album$/, "Black Sands Remixed");
    submit(/^Move$/);

    await waitFor(() => expect(moveMock).toHaveBeenCalledTimes(1));
    // TanStack Query v5 hands the mutation function a second, contextual
    // argument, so only the first is asserted on.
    expect(moveMock.mock.calls[0][0]).toEqual({
      paths: ["Bonobo/Black Sands/Prelude.flac"],
      artist: "Ninja Tune",
      album: "Black Sands Remixed",
    });
  });

  it("sends a blank album when the field is cleared, which means a Single", async () => {
    renderDialog(trackTarget);

    fill(/^Album$/, "");
    submit(/^Move$/);

    await waitFor(() => expect(moveMock).toHaveBeenCalledTimes(1));
    expect(moveMock.mock.calls[0][0]).toMatchObject({
      artist: "Bonobo",
      album: "",
    });
  });

  it("sends the album folder path when moving an album", async () => {
    renderDialog(albumTarget);

    fill(/^Artist$/, "Ninja Tune");
    submit(/^Move$/);

    await waitFor(() => expect(moveMock).toHaveBeenCalledTimes(1));
    expect(moveMock.mock.calls[0][0]).toEqual({
      path: "Bonobo/Black Sands",
      artist: "Ninja Tune",
      album: "Black Sands",
    });
  });

  it("shows only the artist field when renaming an artist", async () => {
    renderDialog(artistTarget);

    expect(screen.getByText("Rename artist")).toBeTruthy();
    expect(screen.queryByLabelText(/^Album$/)).toBeNull();

    fill(/^Artist$/, "Bonobo (UK)");
    submit(/^Rename$/);

    await waitFor(() => expect(moveMock).toHaveBeenCalledTimes(1));
    expect(moveMock.mock.calls[0][0]).toEqual({
      path: "Bonobo",
      artist: "Bonobo (UK)",
    });
  });

  it("refuses to submit an empty artist and never calls the backend", async () => {
    renderDialog(trackTarget);

    fill(/^Artist$/, "   ");
    submit(/^Move$/);

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("Enter an artist name.")).toBeTruthy();
    expect(moveMock).not.toHaveBeenCalled();
  });

  it("lists every conflicting path a 409 came back with", async () => {
    moveMock.mockRejectedValue(
      new LibraryMoveConflict("2 file(s) already exist; nothing was moved", [
        "Ninja Tune/Black Sands/Kiara.flac",
        "Ninja Tune/Black Sands/Kong.flac",
      ]),
    );
    const { onClose } = renderDialog(albumTarget);

    fill(/^Artist$/, "Ninja Tune");
    submit(/^Move$/);

    expect(
      await screen.findByText(/nothing was moved/),
    ).toBeTruthy();
    expect(screen.getByText("Ninja Tune/Black Sands/Kiara.flac")).toBeTruthy();
    expect(screen.getByText("Ninja Tune/Black Sands/Kong.flac")).toBeTruthy();
    // The dialog stays open on a refusal, so the user can pick somewhere else.
    expect(onClose).not.toHaveBeenCalled();
  });

  it("shows any other failure as a plain message", async () => {
    moveMock.mockRejectedValue(new Error("that folder is not part of the library"));
    renderDialog(trackTarget);

    submit(/^Move$/);

    expect(
      await screen.findByText("that folder is not part of the library"),
    ).toBeTruthy();
  });

  it("reports the move, not a cancel, once it succeeds", async () => {
    const result = {
      moved: [
        { from: "Bonobo/Black Sands/Prelude.flac", to: "Bonobo/Prelude.flac" },
      ],
      removed: [],
      destination: null,
    };
    moveMock.mockResolvedValue(result);
    const { onClose, onMoved } = renderDialog(trackTarget);

    submit(/^Move$/);

    await waitFor(() => expect(onMoved).toHaveBeenCalledTimes(1));
    expect(onMoved.mock.calls[0][0]).toEqual(result);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("says a blank album keeps the folder name when an album is moving", () => {
    renderDialog(albumTarget);

    expect(
      screen.getByText('Optional — leave blank to keep the name "Black Sands".'),
    ).toBeTruthy();
    expect(screen.queryByText(/file the track loose/)).toBeNull();
  });

  it("says a blank album files tracks loose when tracks are moving", () => {
    renderDialog(trackTarget);

    expect(screen.getByText(/file the track loose under the artist/)).toBeTruthy();
    expect(screen.queryByText(/keep the name/)).toBeNull();
  });

  it("drops the conflict list as soon as the destination is edited", async () => {
    moveMock.mockRejectedValue(
      new LibraryMoveConflict("1 file(s) already exist; nothing was moved", [
        "Ninja Tune/Black Sands/Kiara.flac",
      ]),
    );
    renderDialog(albumTarget);

    fill(/^Artist$/, "Ninja Tune");
    submit(/^Move$/);
    expect(
      await screen.findByText("Ninja Tune/Black Sands/Kiara.flac"),
    ).toBeTruthy();

    fill(/^Artist$/, "Ninja Tune Records");

    await waitFor(() =>
      expect(screen.queryByText("Ninja Tune/Black Sands/Kiara.flac")).toBeNull(),
    );
    expect(screen.queryByText(/nothing was moved/)).toBeNull();
  });

  it("drops the blank-artist complaint on the next keystroke", () => {
    renderDialog(trackTarget);

    fill(/^Artist$/, "   ");
    submit(/^Move$/);
    expect(screen.getByText("Enter an artist name.")).toBeTruthy();

    fill(/^Artist$/, "N");

    expect(screen.queryByText("Enter an artist name.")).toBeNull();
  });

  it("suggests the artists already in the library", () => {
    renderDialog(trackTarget);

    fireEvent.click(
      screen.getByRole("button", { name: /Show existing artist names/ }),
    );

    expect(screen.getByRole("option", { name: "Bonobo" })).toBeTruthy();
    // The synthetic bucket is not a folder, so it is not somewhere to move to.
    expect(screen.queryByRole("option", { name: "Unknown Artist" })).toBeNull();
  });
});

describe("buildMoveRequest", () => {
  it("trims the names it is given", () => {
    expect(buildMoveRequest(trackTarget, "  Ninja Tune  ", "  Sands  ")).toEqual({
      paths: ["Bonobo/Black Sands/Prelude.flac"],
      artist: "Ninja Tune",
      album: "Sands",
    });
  });

  it("sends no album at all for an artist rename", () => {
    expect(buildMoveRequest(artistTarget, "New Name", "ignored")).toEqual({
      path: "Bonobo",
      artist: "New Name",
    });
  });
});
