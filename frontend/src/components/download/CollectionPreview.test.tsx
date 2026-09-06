import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CollectionPreview } from "@/components/download/CollectionPreview";
import { DownloadTab } from "@/components/DownloadTab";
import { probeUrl, submitBulkDownload, submitDownload } from "@/lib/api";
import {
  LARGE_COLLECTION_TRACKS,
  collectionPreview,
  previewRow,
} from "@/lib/preview.fixture";
import { queryKeys } from "@/lib/queryKeys";
import type {
  CollectionPreview as CollectionPreviewData,
  Job,
  ProbeResponse,
} from "@/lib/types";

vi.mock("@/lib/api", () => ({
  // Never resolves: the queue rows in these tests come from the cache.
  getQueue: vi.fn(() => new Promise<Job[]>(() => {})),
  probeUrl: vi.fn(),
  submitDownload: vi.fn(),
  submitBulkDownload: vi.fn(),
  cancelJob: vi.fn(),
  dismissJob: vi.fn(),
  retryJob: vi.fn(),
}));

const probeMock = vi.mocked(probeUrl);
const bulkMock = vi.mocked(submitBulkDownload);
const downloadMock = vi.mocked(submitDownload);

function bulkParent(): Job {
  return {
    id: "parent-1",
    kind: "bulk",
    parent_id: null,
    url: "https://youtube.com/playlist?list=PL1",
    status: "queued",
    title: "Black Sands",
    thumbnail_url: null,
    duration: null,
    progress: 0,
    error: null,
    artist: "Bonobo",
    album: null,
    path: null,
    children: [],
    created_at: "2026-09-05T09:00:00Z",
  };
}

let queryClient: QueryClient;

beforeEach(() => {
  probeMock.mockReset();
  bulkMock.mockReset();
  downloadMock.mockReset();
  bulkMock.mockResolvedValue(bulkParent());
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity, gcTime: Infinity },
      mutations: { retry: false },
    },
  });
});

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

function renderPreview(preview: CollectionPreviewData, initialArtist?: string) {
  const onCancel = vi.fn();
  const onQueued = vi.fn();
  render(
    <CollectionPreview
      preview={preview}
      initialArtist={initialArtist}
      onCancel={onCancel}
      onQueued={onQueued}
    />,
    { wrapper },
  );
  return { onCancel, onQueued };
}

/** Matches PROBE_DEBOUNCE_MS in the component. */
const DEBOUNCE_MS = 400;

const PLAYLIST_URL = "https://youtube.com/playlist?list=PL1";

/** A probe answer held open, so two of them can be landed out of order. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

/**
 * Move the frozen clock on by *ms* and let everything it woke settle.
 *
 * `waitFor` cannot be used while the clock is frozen — it polls on a timer of
 * its own — so the tests that drive the debounce flush by hand instead.
 */
async function flush(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
    // React Query batches its notifications onto a zero-delay timer of their
    // own, which is only due once the clock has moved again.
    await vi.advanceTimersByTimeAsync(1);
  });
}

function artistField(): HTMLElement {
  return screen.getByLabelText(/^Artist \*/);
}

function typeArtist(value: string) {
  fireEvent.change(artistField(), { target: { value } });
}

function checkbox(name: string): HTMLElement {
  return screen.getByRole("checkbox", { name: `Select ${name}` });
}

/** Base UI renders a checkbox as a span, so its state is on the ARIA attributes. */
function isChecked(name: string): boolean {
  return checkbox(name).getAttribute("aria-checked") === "true";
}

function isCheckboxDisabled(name: string): boolean {
  return checkbox(name).getAttribute("aria-disabled") === "true";
}

function fieldValue(label: RegExp): string {
  return (screen.getByLabelText(label) as HTMLInputElement).value;
}

function isButtonDisabled(name: RegExp): boolean {
  return screen.getByRole("button", { name }).hasAttribute("disabled");
}

function click(name: RegExp) {
  fireEvent.click(screen.getByRole("button", { name }));
}

const mixedPreview = () =>
  collectionPreview([
    previewRow("a", { title: "Kiara" }),
    previewRow("b", {
      title: "Kong",
      status: "in_library",
      reason: "Bonobo/Black Sands/Kong.flac",
    }),
    previewRow("c", {
      title: "Eyesdown",
      status: "unavailable",
      reason: "Video unavailable",
    }),
  ]);

describe("DownloadTab probing", () => {
  it("opens the preview for a collection URL", async () => {
    const preview = mixedPreview();
    probeMock.mockResolvedValue({ type: "collection", preview });

    render(<DownloadTab />, { wrapper });
    fireEvent.change(screen.getByLabelText(/^URL/), {
      target: { value: "https://youtube.com/playlist?list=PL1" },
    });
    click(/^Download$/);

    await screen.findByLabelText(/^Artist \*/);
    expect(fieldValue(/^Artist \*/)).toBe("Bonobo");
    expect(checkbox("Kiara")).toBeTruthy();
    expect(downloadMock).not.toHaveBeenCalled();
  });

  it("queues a single track with no preview", async () => {
    probeMock.mockResolvedValue({
      type: "track",
      title: "Kiara",
      duration: 214,
      thumbnail_url: null,
      artist: "Bonobo",
      album: "Black Sands",
    });
    downloadMock.mockResolvedValue({ ...bulkParent(), kind: "download" });

    render(<DownloadTab />, { wrapper });
    fireEvent.change(screen.getByLabelText(/^URL/), {
      target: { value: "https://youtube.com/watch?v=a" },
    });
    click(/^Download$/);

    await waitFor(() => expect(downloadMock).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("checkbox")).toBeNull();
    // The form cleared, so the URL field is empty again.
    expect(fieldValue(/^URL/)).toBe("");
  });

  it("shows the probe's own error", async () => {
    probeMock.mockRejectedValue(new Error("Unsupported host"));

    render(<DownloadTab />, { wrapper });
    fireEvent.change(screen.getByLabelText(/^URL/), {
      target: { value: "https://example.com/x" },
    });
    click(/^Download$/);

    expect(await screen.findByText("Unsupported host")).toBeTruthy();
  });

  it("passes the typed artist to the probe", async () => {
    probeMock.mockResolvedValue({ type: "collection", preview: mixedPreview() });

    render(<DownloadTab />, { wrapper });
    fireEvent.change(screen.getByLabelText(/^URL/), {
      target: { value: "https://youtube.com/playlist?list=PL1" },
    });
    fireEvent.change(screen.getByLabelText(/^Artist$/), {
      target: { value: "Bonobo" },
    });
    click(/^Download$/);

    await waitFor(() =>
      expect(probeMock).toHaveBeenCalledWith(
        "https://youtube.com/playlist?list=PL1",
        "Bonobo",
      ),
    );
  });

  it("cancel comes back to a form that still holds what was typed", async () => {
    probeMock.mockResolvedValue({ type: "collection", preview: mixedPreview() });

    render(<DownloadTab />, { wrapper });
    fireEvent.change(screen.getByLabelText(/^URL/), {
      target: { value: PLAYLIST_URL },
    });
    fireEvent.change(screen.getByLabelText(/^Artist$/), {
      target: { value: "Bonobo Live" },
    });
    click(/^Download$/);
    await screen.findByLabelText(/^Artist \*/);

    click(/^Cancel$/);

    expect(fieldValue(/^URL/)).toBe(PLAYLIST_URL);
    expect(fieldValue(/^Artist$/)).toBe("Bonobo Live");
  });
});

describe("CollectionPreview selection", () => {
  it("preselects the available rows and labels the duplicates", () => {
    renderPreview(mixedPreview());

    expect(isChecked("Kiara")).toBe(true);
    expect(isChecked("Kong")).toBe(false);
    expect(screen.getByText("in library")).toBeTruthy();
    expect(screen.getByText("1 selected")).toBeTruthy();
  });

  it("cannot tick an unavailable row", () => {
    renderPreview(mixedPreview());

    expect(isCheckboxDisabled("Eyesdown")).toBe(true);
    fireEvent.click(checkbox("Eyesdown"));
    expect(isChecked("Eyesdown")).toBe(false);
    expect(screen.getByText("Video unavailable")).toBeTruthy();
    expect(screen.getByText("1 selected")).toBeTruthy();
  });

  it("select all takes the available rows, select none clears", () => {
    renderPreview(mixedPreview());

    // Untick the one preselected row, then Select all puts it back — and
    // leaves the in-library row alone.
    fireEvent.click(checkbox("Kiara"));
    expect(screen.getByText("0 selected")).toBeTruthy();

    click(/^Select all$/);
    expect(isChecked("Kiara")).toBe(true);
    expect(isChecked("Kong")).toBe(false);
    expect(screen.getByText("1 selected")).toBeTruthy();

    click(/^Select none$/);
    expect(isChecked("Kiara")).toBe(false);
    expect(screen.getByText("0 selected")).toBeTruthy();
  });

  it("lets a duplicate be ticked by hand", () => {
    renderPreview(mixedPreview());

    fireEvent.click(checkbox("Kong"));
    expect(isChecked("Kong")).toBe(true);
    expect(screen.getByText("2 selected")).toBeTruthy();
  });

  it("warns and preselects nothing above 500 rows", () => {
    const rows = Array.from({ length: LARGE_COLLECTION_TRACKS + 100 }, (_, i) =>
      previewRow(String(i), { title: `Track ${i}` }),
    );
    renderPreview(collectionPreview(rows));

    expect(screen.getByText("0 selected")).toBeTruthy();
    expect(screen.getByText(/nothing is selected/)).toBeTruthy();
    expect(screen.getAllByRole("checkbox")).toHaveLength(rows.length);
    expect(isChecked("Track 0")).toBe(false);
  });

  it("renders the source notices", () => {
    /**
     * A Bandcamp preview carries two of them -- the 128 kbps one and the one
     * about a seller who has streaming turned off -- so this checks that a
     * list of notices renders as a list rather than as only the first.
     */
    const notices = [
      "Bandcamp streams are 128 kbps MP3.",
      "A Bandcamp track whose seller has turned off streaming is listed here as available but fails when downloaded.",
    ];
    renderPreview(
      collectionPreview([previewRow("a")], { source: "bandcamp", notices }),
    );

    for (const notice of notices) {
      expect(screen.getByText(notice)).toBeTruthy();
    }
  });

  it("shows the Spotify match notice and the resolved artist", () => {
    /**
     * A Spotify artist URL previews the YouTube Music discography it was
     * matched to: the artist field carries the name Spotify gave (it is the
     * folder the tracks land in, and the user edits it here), and the notice
     * names the artist that was actually enumerated.
     */
    renderPreview(
      collectionPreview([previewRow("a")], {
        url: "https://open.spotify.com/artist/4Z8W4fKeB5YxbusRsdQVPb",
        artist: "Glass Beams",
        notices: [
          'Matched to the YouTube Music artist "Glass Beams"; its discography ' +
            "may differ from the Spotify one. Edit the artist above if it is wrong.",
        ],
      }),
    );

    expect(screen.getByText(/Matched to the YouTube Music artist/)).toBeTruthy();
    expect(
      (screen.getByLabelText(/Artist/) as HTMLInputElement).value,
    ).toBe("Glass Beams");
  });

  it("names a title-less row by its URL", () => {
    renderPreview(collectionPreview([previewRow("a", { title: null })]));

    expect(checkbox("https://youtube.com/watch?v=a")).toBeTruthy();
  });
});

describe("CollectionPreview artist", () => {
  it("re-probes against the edited artist and applies the new verdicts", async () => {
    renderPreview(mixedPreview());
    // The same rows, deduped against the corrected folder: Kiara is a
    // duplicate there, Kong is not.
    probeMock.mockResolvedValue({
      type: "collection",
      preview: collectionPreview([
        previewRow("a", {
          title: "Kiara",
          status: "in_library",
          reason: "Bonobo Live/Kiara.flac",
        }),
        previewRow("b", { title: "Kong" }),
        previewRow("c", {
          title: "Eyesdown",
          status: "unavailable",
          reason: "Video unavailable",
        }),
      ]),
    });

    fireEvent.change(screen.getByLabelText(/^Artist \*/), {
      target: { value: "Bonobo Live" },
    });

    await waitFor(() =>
      expect(probeMock).toHaveBeenCalledWith(
        "https://youtube.com/playlist?list=PL1",
        "Bonobo Live",
      ),
    );
    await waitFor(() => expect(isChecked("Kiara")).toBe(false));
    expect(isChecked("Kong")).toBe(true);
  });

  it("does not re-probe while the artist is unchanged", async () => {
    renderPreview(mixedPreview());

    fireEvent.click(checkbox("Kong"));
    await waitFor(() => expect(isChecked("Kong")).toBe(true));
    expect(probeMock).not.toHaveBeenCalled();
  });

  it("reports a failed re-probe without losing the rows", async () => {
    renderPreview(mixedPreview());
    probeMock.mockRejectedValue(new Error("probe timed out"));

    fireEvent.change(screen.getByLabelText(/^Artist \*/), {
      target: { value: "Bonobo Live" },
    });

    expect(await screen.findByText(/probe timed out/)).toBeTruthy();
    expect(isChecked("Kiara")).toBe(true);
  });
});

describe("CollectionPreview submission", () => {
  it("submits the selection with the edited artist and adds the parent", async () => {
    const { onQueued } = renderPreview(mixedPreview());

    fireEvent.change(screen.getByLabelText(/^Artist \*/), {
      target: { value: "Bonobo" },
    });
    fireEvent.click(checkbox("Kong"));
    click(/^Download 2 tracks$/);

    await waitFor(() => expect(bulkMock).toHaveBeenCalledTimes(1));
    expect(bulkMock).toHaveBeenCalledWith({
      url: "https://youtube.com/playlist?list=PL1",
      artist: "Bonobo",
      title: "Black Sands",
      tracks: [
        expect.objectContaining({ url: "https://youtube.com/watch?v=a" }),
        expect.objectContaining({ url: "https://youtube.com/watch?v=b" }),
      ],
    });
    await waitFor(() => expect(onQueued).toHaveBeenCalledTimes(1));
  });

  it("carries the edited artist to every child through the tab", async () => {
    probeMock.mockResolvedValue({ type: "collection", preview: mixedPreview() });

    render(<DownloadTab />, { wrapper });
    fireEvent.change(screen.getByLabelText(/^URL/), {
      target: { value: "https://youtube.com/playlist?list=PL1" },
    });
    click(/^Download$/);
    await screen.findByLabelText(/^Artist \*/);

    // The re-probe the edit triggers answers with the same rows.
    probeMock.mockResolvedValue({ type: "collection", preview: mixedPreview() });
    fireEvent.change(screen.getByLabelText(/^Artist \*/), {
      target: { value: "Bonobo Live" },
    });
    await waitFor(() => expect(probeMock).toHaveBeenCalledTimes(2));
    // Download stays shut until the recheck lands and the ticks are about
    // the folder the field now names.
    await waitFor(() =>
      expect(isButtonDisabled(/^Download 1 track$/)).toBe(false),
    );

    click(/^Download 1 track$/);
    await waitFor(() => expect(bulkMock).toHaveBeenCalledTimes(1));
    expect(bulkMock.mock.calls[0][0].artist).toBe("Bonobo Live");

    // Back on the Download tab: the parent is in the queue cache, the preview
    // is gone, and the form is empty again.
    await waitFor(() =>
      expect(queryClient.getQueryData(queryKeys.queue)).toEqual([bulkParent()]),
    );
    expect(screen.queryByLabelText(/^Artist \*/)).toBeNull();
    expect(fieldValue(/^URL/)).toBe("");
  });

  it("will not submit with a blank artist or an empty selection", () => {
    renderPreview(collectionPreview([previewRow("a", { title: "Kiara" })]));

    expect(isButtonDisabled(/^Download 1 track$/)).toBe(false);

    fireEvent.change(screen.getByLabelText(/^Artist \*/), {
      target: { value: "  " },
    });
    expect(isButtonDisabled(/^Download 1 track$/)).toBe(true);

    fireEvent.change(screen.getByLabelText(/^Artist \*/), {
      target: { value: "Bonobo" },
    });
    click(/^Select none$/);
    expect(isButtonDisabled(/^Download 0 tracks$/)).toBe(true);
  });

  it("shows the backend's message when the bulk post fails", async () => {
    renderPreview(mixedPreview());
    bulkMock.mockRejectedValue(new Error("collection already queued"));

    click(/^Download 1 track$/);

    expect(
      await screen.findByText("collection already queued"),
    ).toBeTruthy();
  });

  it("will not cancel out from under a submit", async () => {
    renderPreview(mixedPreview());
    // Never settles: the POST is still in flight.
    bulkMock.mockReturnValue(new Promise<Job>(() => {}));

    click(/^Download 1 track$/);

    await waitFor(() => expect(isButtonDisabled(/^Cancel$/)).toBe(true));
  });

  it("cancel closes without queueing", () => {
    const { onCancel } = renderPreview(mixedPreview());

    click(/^Cancel$/);
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(bulkMock).not.toHaveBeenCalled();
  });
});

describe("CollectionPreview recheck timing", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the artist typed on the form and rechecks neither name", async () => {
    // The preview comes back suggesting "Bonobo" whatever it deduped
    // against; the rows were checked for what the user typed.
    probeMock.mockResolvedValue({ type: "collection", preview: mixedPreview() });

    render(<DownloadTab />, { wrapper });
    fireEvent.change(screen.getByLabelText(/^URL/), {
      target: { value: PLAYLIST_URL },
    });
    fireEvent.change(screen.getByLabelText(/^Artist$/), {
      target: { value: "Bonobo Live" },
    });
    click(/^Download$/);
    await flush();

    expect(fieldValue(/^Artist \*/)).toBe("Bonobo Live");
    await flush(DEBOUNCE_MS * 3);
    expect(probeMock).toHaveBeenCalledTimes(1);
    expect(isButtonDisabled(/^Download 1 track$/)).toBe(false);
  });

  it("lets the newest recheck win when the answers arrive out of order", async () => {
    renderPreview(mixedPreview());
    const first = deferred<ProbeResponse>();
    const second = deferred<ProbeResponse>();
    probeMock
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    typeArtist("Alpha");
    await flush(DEBOUNCE_MS);
    expect(probeMock).toHaveBeenCalledWith(PLAYLIST_URL, "Alpha");

    typeArtist("Beta");
    await flush(DEBOUNCE_MS);
    expect(probeMock).toHaveBeenCalledWith(PLAYLIST_URL, "Beta");

    // Beta answers first, then the superseded Alpha lands behind it.
    second.resolve({
      type: "collection",
      preview: collectionPreview([previewRow("a", { title: "Beta take" })]),
    });
    await flush();
    first.resolve({
      type: "collection",
      preview: collectionPreview([previewRow("a", { title: "Alpha take" })]),
    });
    await flush();

    expect(screen.getByText("Beta take")).toBeTruthy();
    expect(screen.queryByText("Alpha take")).toBeNull();
    expect(isButtonDisabled(/^Download 1 track$/)).toBe(false);
  });

  it("holds Download until the recheck agrees with the field", async () => {
    renderPreview(mixedPreview());
    const answer = deferred<ProbeResponse>();
    probeMock.mockReturnValue(answer.promise);

    typeArtist("Bonobo Live");
    // Still inside the debounce: nothing has been asked yet.
    expect(probeMock).not.toHaveBeenCalled();
    expect(isButtonDisabled(/^Download 1 track$/)).toBe(true);

    await flush(DEBOUNCE_MS);
    expect(probeMock).toHaveBeenCalledTimes(1);
    expect(isButtonDisabled(/^Download 1 track$/)).toBe(true);
    expect(
      screen.getByText(/Rechecking the library for Bonobo Live/),
    ).toBeTruthy();

    answer.resolve({ type: "collection", preview: mixedPreview() });
    await flush();
    expect(isButtonDisabled(/^Download 1 track$/)).toBe(false);
  });

  it("retries a failed recheck for the name already in the field", async () => {
    renderPreview(mixedPreview());
    probeMock.mockRejectedValueOnce(new Error("probe timed out"));

    typeArtist("Bonobo Live");
    await flush(DEBOUNCE_MS);
    expect(screen.getByText(/probe timed out/)).toBeTruthy();
    expect(isButtonDisabled(/^Download 1 track$/)).toBe(true);

    // Nothing was edited, so only the button can ask again.
    probeMock.mockResolvedValueOnce({
      type: "collection",
      preview: mixedPreview(),
    });
    click(/^Retry$/);
    await flush();

    expect(probeMock).toHaveBeenCalledTimes(2);
    expect(probeMock.mock.calls[1][1]).toBe("Bonobo Live");
    expect(screen.queryByRole("button", { name: /^Retry$/ })).toBeNull();
    expect(isButtonDisabled(/^Download 1 track$/)).toBe(false);
  });

  it("cannot retry a recheck once the field is blank", async () => {
    renderPreview(mixedPreview());
    probeMock.mockRejectedValueOnce(new Error("probe timed out"));

    typeArtist("Bonobo Live");
    await flush(DEBOUNCE_MS);
    expect(screen.getByText(/probe timed out/)).toBeTruthy();

    // Blank is not a folder to check against, so Retry has nothing to ask.
    typeArtist("");
    await flush(DEBOUNCE_MS);
    const retry = screen.queryByRole("button", { name: /^Retry$/ });
    if (retry) fireEvent.click(retry);
    await flush(DEBOUNCE_MS);

    expect(probeMock).toHaveBeenCalledTimes(1);
  });

  it("retries once while an edit's debounce is still pending", async () => {
    renderPreview(mixedPreview());
    probeMock.mockRejectedValueOnce(new Error("probe timed out"));

    typeArtist("Bonobo Live");
    await flush(DEBOUNCE_MS);
    expect(screen.getByText(/probe timed out/)).toBeTruthy();

    probeMock.mockResolvedValueOnce({
      type: "collection",
      preview: mixedPreview(),
    });
    // An edit arms a fresh debounce; Retry fires inside it, and the timer it
    // called off must not send a second request for the same name.
    typeArtist("Bonobo Livee");
    await flush(DEBOUNCE_MS / 4);
    click(/^Retry$/);
    await flush(DEBOUNCE_MS * 2);

    expect(probeMock).toHaveBeenCalledTimes(2);
    expect(probeMock.mock.calls[1][1]).toBe("Bonobo Livee");
  });

  it("clears a failed recheck when the field goes back to the probed name", async () => {
    renderPreview(mixedPreview(), "Bonobo Live");
    probeMock.mockRejectedValueOnce(new Error("probe timed out"));

    typeArtist("Bonobo Liv");
    await flush(DEBOUNCE_MS);
    expect(screen.getByText(/probe timed out/)).toBeTruthy();

    // Back on the name the rows already describe: nothing is being rechecked,
    // so no error line and no Retry should sit beside an enabled Download.
    typeArtist("Bonobo Live");
    await flush(DEBOUNCE_MS);

    expect(screen.queryByText(/probe timed out/)).toBeNull();
    expect(screen.queryByRole("button", { name: /^Retry$/ })).toBeNull();
    expect(probeMock).toHaveBeenCalledTimes(1);
    expect(isButtonDisabled(/^Download 1 track$/)).toBe(false);
  });

  it("asks again for a name whose recheck failed", async () => {
    renderPreview(mixedPreview());
    probeMock.mockRejectedValueOnce(new Error("probe timed out"));

    typeArtist("Bonobo Live");
    await flush(DEBOUNCE_MS);
    expect(screen.getByText(/probe timed out/)).toBeTruthy();
    expect(isButtonDisabled(/^Download 1 track$/)).toBe(true);

    probeMock.mockResolvedValueOnce({
      type: "collection",
      preview: mixedPreview(),
    });
    // Edited away and back within one debounce window, so the same name is
    // the one that goes out again.
    typeArtist("Bonobo Liv");
    await flush(DEBOUNCE_MS / 4);
    typeArtist("Bonobo Live");
    await flush(DEBOUNCE_MS);

    expect(probeMock).toHaveBeenCalledTimes(2);
    expect(probeMock.mock.calls[1][1]).toBe("Bonobo Live");
    expect(isButtonDisabled(/^Download 1 track$/)).toBe(false);
  });
});
