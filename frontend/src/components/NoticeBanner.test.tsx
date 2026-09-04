import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { act } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NoticeBanner } from "@/components/NoticeBanner";
import { fetchNotices } from "@/lib/api";
import { applyQueueEvent } from "@/lib/queueCache";
import { queryKeys } from "@/lib/queryKeys";
import type { Notice, SSEEvent } from "@/lib/types";

vi.mock("@/lib/api", () => ({ fetchNotices: vi.fn() }));

const fetchNoticesMock = vi.mocked(fetchNotices);

function notice(overrides: Partial<Notice> = {}): Notice {
  return {
    id: "n1",
    level: "error",
    source: "navidrome",
    message: "Navidrome rejected the credentials",
    created_at: "2026-09-04T10:00:00+00:00",
    ...overrides,
  };
}

/**
 * The SSE event the backend sends whenever the open set changes: the whole
 * current list, never a delta.
 */
function noticesEvent(...open: Notice[]): SSEEvent {
  return {
    event: "notices",
    job_id: null,
    data: { notices: open.map((n) => ({ ...n })) },
  };
}

function renderBanner(initial: Notice[] = []) {
  fetchNoticesMock.mockResolvedValue(initial);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  render(<NoticeBanner />, { wrapper });
  return queryClient;
}

/**
 * Wait for the mount fetch to land before an event is delivered.
 *
 * Patching the cache while `GET /notices` is still in flight would be undone
 * when the (empty) response arrives, which is the request's right to do — it is
 * fresher than nothing. Only the test needs to be explicit about the order.
 */
async function settled(client: QueryClient) {
  await vi.waitFor(() =>
    expect(client.getQueryData<Notice[]>(queryKeys.notices)).toBeDefined(),
  );
}

/** Deliver an SSE event the way `useQueueStream` would. */
function deliver(client: QueryClient, event: SSEEvent) {
  act(() => {
    applyQueueEvent(client, event);
  });
}

beforeEach(() => {
  fetchNoticesMock.mockReset();
});

describe("NoticeBanner", () => {
  it("shows nothing when there is nothing wrong", async () => {
    const client = renderBanner();
    await settled(client);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("stops showing a notice the backend cleared", async () => {
    const client = renderBanner([notice()]);
    await screen.findByRole("alert");

    deliver(client, noticesEvent());

    await vi.waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("shows a recurrence once, not twice, when nothing was dismissed", async () => {
    // The rescan fails, succeeds (clearing the notice), then fails again. The
    // second failure carries the same text under a fresh id; the clear in
    // between is what has to take the first line off the screen.
    const client = renderBanner();
    await settled(client);

    deliver(client, noticesEvent(notice()));
    await screen.findByRole("alert");

    deliver(client, noticesEvent());
    deliver(client, noticesEvent(notice({ id: "n2" })));

    const alerts = await screen.findAllByRole("alert");
    expect(alerts).toHaveLength(1);
  });

  it("paints a notice fetched on mount", async () => {
    renderBanner([notice()]);
    expect((await screen.findByRole("alert")).textContent).toMatch(
      /Navidrome.*rejected the credentials/,
    );
  });

  it("paints a notice that arrives over the stream", async () => {
    const client = renderBanner();
    await settled(client);

    deliver(client, noticesEvent(notice()));

    expect((await screen.findByRole("alert")).textContent).toMatch(
      /rejected the credentials/,
    );
  });

  it("hides a notice the user dismisses", async () => {
    const client = renderBanner();
    await settled(client);
    deliver(client, noticesEvent(notice()));
    await screen.findByRole("alert");

    fireEvent.click(
      screen.getByRole("button", { name: /Dismiss the Navidrome message/ }),
    );

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps a dismissed notice hidden when the same one is re-delivered", async () => {
    const client = renderBanner();
    await settled(client);
    deliver(client, noticesEvent(notice()));
    await screen.findByRole("alert");
    fireEvent.click(
      screen.getByRole("button", { name: /Dismiss the Navidrome message/ }),
    );

    deliver(client, noticesEvent(notice()));

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the problem again under a new id", async () => {
    const client = renderBanner();
    await settled(client);
    deliver(client, noticesEvent(notice()));
    await screen.findByRole("alert");
    fireEvent.click(
      screen.getByRole("button", { name: /Dismiss the Navidrome message/ }),
    );

    // The backend cleared the notice on a success and raised it afresh.
    deliver(client, noticesEvent(notice({ id: "n2" })));

    expect((await screen.findByRole("alert")).textContent).toMatch(
      /rejected the credentials/,
    );
  });

  it("shows one line per open notice", async () => {
    renderBanner([
      notice(),
      notice({
        id: "n2",
        level: "warning",
        source: "lidarr",
        message: "Lidarr is set to scrub audio tags",
      }),
    ]);

    // An error is a `role="alert"`, a warning a `role="status"` — an assertive
    // announcement is for the thing that is broken, not the thing that is odd.
    const alerts = await screen.findAllByRole("alert");
    expect(alerts).toHaveLength(1);
    expect(alerts[0].textContent).toMatch(/Navidrome.*rejected the credentials/);

    const statuses = await screen.findAllByRole("status");
    expect(statuses).toHaveLength(1);
    expect(statuses[0].textContent).toMatch(/Lidarr.*scrub audio tags/);
  });

  it("ignores a malformed notice event", async () => {
    const client = renderBanner();
    await settled(client);

    deliver(client, {
      event: "notices",
      job_id: null,
      data: { notices: [{ id: 7 }] },
    });

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("drops only the malformed entries of a notices event", async () => {
    const client = renderBanner();
    await settled(client);

    deliver(client, {
      event: "notices",
      job_id: null,
      data: { notices: [{ id: 7 }, { ...notice() }] },
    });

    expect((await screen.findByRole("alert")).textContent).toMatch(
      /rejected the credentials/,
    );
  });
});
