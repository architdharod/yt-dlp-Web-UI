import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getQueue } from "@/lib/api";
import { queueQueryOptions } from "@/hooks/useQueueQuery";
import {
  addJobToCache,
  applyQueueEvent,
  parentIdOfCachedJob,
  removeJobFromCache,
  replaceJobInCache,
  resyncAfterReconnect,
  setJobActionError,
} from "@/lib/queueCache";
import { queryKeys } from "@/lib/queryKeys";
import type { Job, JobStatus, Notice, SSEEvent } from "@/lib/types";

// The reconciliation tests drive the real queue query, so the only thing that
// needs faking is the request itself.
vi.mock("@/lib/api", () => ({ getQueue: vi.fn() }));

function job(id: string, status: JobStatus = "downloading"): Job {
  return {
    id,
    kind: "download",
    parent_id: null,
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

function event(
  name: string,
  jobId: string | null,
  data: Record<string, unknown> = {},
): SSEEvent {
  return { event: name, job_id: jobId, data };
}

let queryClient: QueryClient;
let invalidated: unknown[];

beforeEach(() => {
  queryClient = new QueryClient();
  invalidated = [];
  vi.spyOn(queryClient, "invalidateQueries").mockImplementation(
    (filters?: unknown) => {
      invalidated.push((filters as { queryKey?: unknown })?.queryKey);
      return Promise.resolve();
    },
  );
});

/** The jobs currently in the ["queue"] cache. */
function queue(): Job[] | undefined {
  return queryClient.getQueryData<Job[]>(queryKeys.queue);
}

describe("applyQueueEvent", () => {
  it("merges a status_change snapshot into the matching job", () => {
    const untouched = job("b");
    queryClient.setQueryData(queryKeys.queue, [job("a", "queued"), untouched]);

    applyQueueEvent(
      queryClient,
      event("status_change", "a", {
        status: "downloading",
        progress: 12.5,
        title: "Kerala",
        artist: "Bonobo",
      }),
    );

    const [a, b] = queue()!;
    expect(a).toMatchObject({
      id: "a",
      status: "downloading",
      progress: 12.5,
      title: "Kerala",
      artist: "Bonobo",
    });
    // Untouched jobs keep their identity, so React sees no change.
    expect(b).toBe(untouched);
    expect(invalidated).toEqual([]);
  });

  it("carries the tagging status and its note", () => {
    queryClient.setQueryData(queryKeys.queue, [job("a", "converting")]);

    applyQueueEvent(
      queryClient,
      event("status_change", "a", { status: "tagging" }),
    );
    expect(queue()![0].status).toBe("tagging");

    // A `done` job leaves the view, note and all — the detail is for the API
    // and the log, not for a row nobody can see.
    applyQueueEvent(
      queryClient,
      event("status_change", "a", {
        status: "done",
        detail: "tags not fixed: no match",
      }),
    );
    expect(queue()).toEqual([]);
  });

  it("carries a tagging job's N of M through a snapshot", () => {
    queryClient.setQueryData(queryKeys.queue, [
      { ...job("a", "queued"), kind: "tagging" as const, path: "Bonobo/Black Sands" },
    ]);

    applyQueueEvent(
      queryClient,
      event("status_change", "a", {
        status: "tagging",
        progress_done: 0,
        progress_total: 12,
      }),
    );

    expect(queue()![0]).toMatchObject({
      kind: "tagging",
      parent_id: null,
      status: "tagging",
      path: "Bonobo/Black Sands",
      progress_done: 0,
      progress_total: 12,
    });

    applyQueueEvent(
      queryClient,
      event("progress", "a", { progress_done: 7, progress_total: 12 }),
    );

    expect(queue()![0].progress_done).toBe(7);
  });

  it("leaves kind and path alone, since no event carries them", () => {
    queryClient.setQueryData(queryKeys.queue, [
      { ...job("a", "queued"), kind: "tagging" as const, path: "Bonobo/Black Sands" },
    ]);

    applyQueueEvent(
      queryClient,
      event("status_change", "a", {
        status: "tagging",
        kind: "download",
        parent_id: null,
        path: "somewhere/else",
      }),
    );

    expect(queue()![0]).toMatchObject({
      kind: "tagging",
      parent_id: null,
      path: "Bonobo/Black Sands",
    });
  });

  it("leaves the counters alone when an event does not mention them", () => {
    // A download's events carry no counters at all; a tagging job's `error`
    // event may leave them out. Either way the row must not lose its count.
    queryClient.setQueryData(queryKeys.queue, [
      { ...job("a", "tagging"), kind: "tagging" as const, progress_done: 3, progress_total: 12 },
    ]);

    applyQueueEvent(queryClient, event("progress", "a", { progress: 50 }));

    expect(queue()![0]).toMatchObject({ progress_done: 3, progress_total: 12 });
  });

  it("clears the counters when the backend sends an explicit null", () => {
    queryClient.setQueryData(queryKeys.queue, [
      { ...job("a", "tagging"), kind: "tagging" as const, progress_done: 3, progress_total: 12 },
    ]);

    applyQueueEvent(
      queryClient,
      event("status_change", "a", {
        status: "tagging",
        progress_done: null,
        progress_total: null,
      }),
    );

    expect(queue()![0]).toMatchObject({
      progress_done: null,
      progress_total: null,
    });
  });

  it("keeps progress events off the invalidation path", () => {
    queryClient.setQueryData(queryKeys.queue, [job("a")]);

    applyQueueEvent(queryClient, event("progress", "a", { progress: 40 }));

    expect(queue()![0].progress).toBe(40);
    expect(invalidated).toEqual([]);
  });

  it("clears a stale error when the job leaves the error state", () => {
    queryClient.setQueryData(queryKeys.queue, [
      { ...job("a", "error"), error: "boom" },
    ]);

    applyQueueEvent(
      queryClient,
      event("status_change", "a", { status: "queued" }),
    );

    expect(queue()![0]).toMatchObject({ status: "queued", error: null });
  });

  it("clears a stale detail when a job is re-queued by a retry", () => {
    queryClient.setQueryData(queryKeys.queue, [
      {
        ...job("a", "error"),
        kind: "tagging" as const,
        detail: "tags not fixed: no match",
      },
    ]);

    applyQueueEvent(
      queryClient,
      event("status_change", "a", { status: "queued" }),
    );

    expect(queue()![0]).toMatchObject({ status: "queued", detail: null });
  });

  it("keeps the detail on a status change that is not a re-queue", () => {
    queryClient.setQueryData(queryKeys.queue, [
      {
        ...job("a", "queued"),
        kind: "tagging" as const,
        detail: "tags not fixed: no match",
      },
    ]);

    applyQueueEvent(
      queryClient,
      event("status_change", "a", { status: "tagging" }),
    );

    expect(queue()![0].detail).toBe("tags not fixed: no match");
  });

  it("marks a job errored on an error event", () => {
    queryClient.setQueryData(queryKeys.queue, [job("a")]);

    applyQueueEvent(
      queryClient,
      event("error", "a", { error: "ffmpeg exploded" }),
    );

    expect(queue()![0]).toMatchObject({
      status: "error",
      error: "ffmpeg exploded",
    });
  });

  it("removes a job that reaches done, because GET /queue omits it", () => {
    queryClient.setQueryData(queryKeys.queue, [job("a"), job("b")]);

    applyQueueEvent(
      queryClient,
      event("status_change", "a", { status: "done" }),
    );

    expect(queue()!.map((j) => j.id)).toEqual(["b"]);
    expect(invalidated).toEqual([]);
  });

  it("removes a job that reaches cancelled", () => {
    queryClient.setQueryData(queryKeys.queue, [job("a"), job("b")]);

    applyQueueEvent(
      queryClient,
      event("status_change", "b", { status: "cancelled" }),
    );

    expect(queue()!.map((j) => j.id)).toEqual(["a"]);
  });

  it("invalidates the queue for a job it has never seen", () => {
    queryClient.setQueryData(queryKeys.queue, [job("a")]);

    applyQueueEvent(
      queryClient,
      event("status_change", "unknown", { status: "downloading" }),
    );

    expect(invalidated).toEqual([queryKeys.queue]);
    // No job is invented from the snapshot: it has no url or created_at.
    expect(queue()!.map((j) => j.id)).toEqual(["a"]);
  });

  it("invalidates the queue when nothing has been fetched yet", () => {
    applyQueueEvent(queryClient, event("progress", "a", { progress: 3 }));

    expect(invalidated).toEqual([queryKeys.queue]);
    expect(queue()).toBeUndefined();
  });

  it("invalidates the library and the trash on library_changed, and touches the queue not at all", () => {
    queryClient.setQueryData(queryKeys.queue, [job("a")]);

    applyQueueEvent(
      queryClient,
      event("library_changed", "a", { paths: ["Bonobo/Migration/Kerala.flac"] }),
    );

    expect(invalidated).toEqual([queryKeys.library, queryKeys.trash]);
    expect(queue()!.map((j) => j.id)).toEqual(["a"]);
  });

  it("handles a library_changed with no job behind it", () => {
    applyQueueEvent(queryClient, event("library_changed", null, { paths: [] }));

    expect(invalidated).toEqual([queryKeys.library, queryKeys.trash]);
  });
});

/** A notice as the backend puts it on the wire. */
function notice(id: string, message = `problem ${id}`): Notice {
  return {
    id,
    level: "error",
    source: "navidrome",
    message,
    created_at: "2026-09-04T00:00:00Z",
  };
}

/** The notices currently in the ["notices"] cache. */
function notices(): Notice[] | undefined {
  return queryClient.getQueryData<Notice[]>(queryKeys.notices);
}

function noticesEvent(...open: Notice[]): SSEEvent {
  return { event: "notices", job_id: null, data: { notices: open } };
}

describe("the notices event", () => {
  it("seeds a cache GET /notices has not filled yet", () => {
    applyQueueEvent(queryClient, noticesEvent(notice("n1")));

    expect(notices()!.map((n) => n.id)).toEqual(["n1"]);
    expect(invalidated).toEqual([]);
  });

  it("replaces the list rather than merging into it", () => {
    // The event carries the whole open set, so a notice missing from it has
    // been cleared — merging would leave it on screen forever.
    queryClient.setQueryData(queryKeys.notices, [notice("n1"), notice("n2")]);

    applyQueueEvent(queryClient, noticesEvent(notice("n3")));

    expect(notices()!.map((n) => n.id)).toEqual(["n3"]);
  });

  it("empties the cache when the last notice clears", () => {
    queryClient.setQueryData(queryKeys.notices, [notice("n1")]);

    applyQueueEvent(queryClient, noticesEvent());

    expect(notices()).toEqual([]);
  });

  it("keeps the well-formed entries and drops the rest", () => {
    applyQueueEvent(queryClient, {
      event: "notices",
      job_id: null,
      data: {
        notices: [
          { ...notice("n1"), level: "catastrophe" },
          notice("n2"),
          null,
          "n3",
        ],
      },
    });

    expect(notices()!.map((n) => n.id)).toEqual(["n2"]);
  });

  it("ignores an event whose data carries no list", () => {
    queryClient.setQueryData(queryKeys.notices, [notice("n1")]);

    applyQueueEvent(queryClient, { event: "notices", job_id: null, data: {} });

    expect(notices()!.map((n) => n.id)).toEqual(["n1"]);
  });

  it("survives a GET /notices that was already in flight", async () => {
    // `useNotices` sets no staleTime, so a focus refetch can be out when a push
    // arrives; its older answer must not land on top of the pushed one.
    let settle!: (open: Notice[]) => void;
    const inFlight = queryClient.fetchQuery({
      queryKey: queryKeys.notices,
      queryFn: () => new Promise<Notice[]>((resolve) => (settle = resolve)),
    });
    // fetchQuery rejects with a CancelledError once the push cancels it.
    const held = inFlight.catch(() => undefined);

    applyQueueEvent(queryClient, noticesEvent(notice("n1")));
    settle([]);
    await held;

    expect(notices()!.map((n) => n.id)).toEqual(["n1"]);
  });

  it("does not touch the queue", () => {
    queryClient.setQueryData(queryKeys.queue, [job("a")]);

    applyQueueEvent(queryClient, noticesEvent(notice("n1")));

    expect(queue()!.map((j) => j.id)).toEqual(["a"]);
    expect(invalidated).toEqual([]);
  });
});

describe("resyncAfterReconnect", () => {
  it("invalidates every query, since events were lost while it was down", () => {
    resyncAfterReconnect(queryClient);

    expect(invalidated).toEqual([
      queryKeys.queue,
      queryKeys.library,
      queryKeys.trash,
      queryKeys.notices,
    ]);
  });
});

describe("addJobToCache", () => {
  it("appends to a cache that has been fetched", () => {
    queryClient.setQueryData(queryKeys.queue, [job("a")]);

    addJobToCache(queryClient, job("b"));

    expect(queue()!.map((j) => j.id)).toEqual(["a", "b"]);
    expect(invalidated).toEqual([]);
  });

  it("leaves a job that is already there alone", () => {
    const existing = job("a");
    queryClient.setQueryData(queryKeys.queue, [existing]);

    addJobToCache(queryClient, { ...job("a"), title: "stale" });

    expect(queue()![0]).toBe(existing);
  });

  it("seeds an unfetched cache, so a submit after a failed GET /queue shows", () => {
    // Without the seed the job vanishes: writeQueue no-ops on undefined and no
    // SSE event carries enough to rebuild the row.
    addJobToCache(queryClient, job("a"));

    expect(queue()!.map((j) => j.id)).toEqual(["a"]);
    // The invalidation fills in whatever else the queue holds.
    expect(invalidated).toEqual([queryKeys.queue]);
  });
});

/**
 * A `GET /queue` in flight while the cache is patched.
 *
 * These use a real `QueryClient` (no mocked `invalidateQueries`) and the real
 * `queueQueryOptions`, with the response held open on a promise so the SSE
 * patch lands strictly between the request and its answer.
 */
describe("reconciling a fetch that overlapped a cache patch", () => {
  /** A client whose queue query answers with whatever `release` is called with. */
  function gatedClient() {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    let release!: (jobs: Job[]) => void;
    vi.mocked(getQueue).mockReturnValue(
      new Promise<Job[]>((resolve) => {
        release = resolve;
      }),
    );
    return { client, release: (jobs: Job[]) => release(jobs) };
  }

  /**
   * Start the fetch and wait until the request has actually gone out. The
   * pending fetch is handed back wrapped, because awaiting a returned promise
   * would await the fetch itself and the gate is still shut.
   */
  async function startFetch(client: QueryClient): Promise<{ done: Promise<Job[]> }> {
    const done = client.fetchQuery(queueQueryOptions(client));
    await vi.waitFor(() => expect(getQueue).toHaveBeenCalled());
    return { done };
  }

  beforeEach(() => {
    vi.mocked(getQueue).mockReset();
  });

  it("keeps a job the stream finished while the response was on the wire out", async () => {
    const { client, release } = gatedClient();
    client.setQueryData(queryKeys.queue, [job("a"), job("b")]);

    const { done: fetching } = await startFetch(client);
    applyQueueEvent(client, event("status_change", "b", { status: "done" }));
    expect(client.getQueryData<Job[]>(queryKeys.queue)!.map((j) => j.id)).toEqual(
      ["a"],
    );

    // The server answered from before b finished, and also knows about z.
    release([job("a"), job("b"), job("z")]);
    await fetching;

    expect(client.getQueryData<Job[]>(queryKeys.queue)!.map((j) => j.id)).toEqual(
      ["a", "z"],
    );
  });

  it("does the same for a row removed by cancel or dismiss", async () => {
    const { client, release } = gatedClient();
    client.setQueryData(queryKeys.queue, [job("a"), job("b")]);

    const { done: fetching } = await startFetch(client);
    removeJobFromCache(client, "b");

    release([job("a"), job("b")]);
    await fetching;

    expect(client.getQueryData<Job[]>(queryKeys.queue)!.map((j) => j.id)).toEqual(
      ["a"],
    );
  });

  it("forgets drops from before the request, so a retried job can come back", async () => {
    const { client, release } = gatedClient();
    client.setQueryData(queryKeys.queue, [job("a"), job("b")]);

    // b finishes first; only afterwards does a fetch go out — by which time a
    // retry has re-queued b, and the server rightly lists it again.
    applyQueueEvent(client, event("status_change", "b", { status: "done" }));
    const { done: fetching } = await startFetch(client);

    release([job("a"), job("b", "queued")]);
    await fetching;

    expect(client.getQueryData<Job[]>(queryKeys.queue)!.map((j) => j.id)).toEqual(
      ["a", "b"],
    );
  });

  it("keeps a job submitted while the response was on the wire, at its freshest state", async () => {
    const { client, release } = gatedClient();
    client.setQueryData(queryKeys.queue, [job("a")]);

    const { done: fetching } = await startFetch(client);
    addJobToCache(client, job("b", "queued"));
    // The stream then starts it, so the re-append must not replay the queued copy.
    applyQueueEvent(client, event("status_change", "b", { status: "downloading" }));

    // The server answered from before b existed.
    release([job("a")]);
    await fetching;

    const jobs = client.getQueryData<Job[]>(queryKeys.queue)!;
    expect(jobs.map((j) => j.id)).toEqual(["a", "b"]);
    expect(jobs[1].status).toBe("downloading");
  });

  it("does not re-append a job that was added and then dropped", async () => {
    const { client, release } = gatedClient();
    client.setQueryData(queryKeys.queue, [job("a")]);

    const { done: fetching } = await startFetch(client);
    addJobToCache(client, job("b", "queued"));
    removeJobFromCache(client, "b");

    release([job("a"), job("b")]);
    await fetching;

    expect(client.getQueryData<Job[]>(queryKeys.queue)!.map((j) => j.id)).toEqual(
      ["a"],
    );
  });

  it("does not duplicate an added job the response already lists", async () => {
    const { client, release } = gatedClient();
    client.setQueryData(queryKeys.queue, [job("a")]);

    const { done: fetching } = await startFetch(client);
    addJobToCache(client, job("b", "queued"));

    release([job("a"), job("b", "queued")]);
    await fetching;

    expect(client.getQueryData<Job[]>(queryKeys.queue)!.map((j) => j.id)).toEqual(
      ["a", "b"],
    );
  });

  it("forgets adds from before the request, so a finished job is not resurrected", async () => {
    const { client, release } = gatedClient();
    client.setQueryData(queryKeys.queue, [job("a")]);

    // b is submitted first; the fetch leaves afterwards, so the server's answer
    // is authoritative about b — here it has already finished and is omitted.
    addJobToCache(client, job("b", "queued"));
    const { done: fetching } = await startFetch(client);

    release([job("a")]);
    await fetching;

    expect(client.getQueryData<Job[]>(queryKeys.queue)!.map((j) => j.id)).toEqual(
      ["a"],
    );
  });
});

/**
 * Invalidations that overlap an in-flight `GET /queue`.
 *
 * `invalidateQueries` cancels and restarts a fetch that is already out unless
 * told otherwise, so these drive a real, subscribed query (an observer, since
 * only active queries refetch) with the response held on a gate, and count the
 * requests that actually leave.
 */
describe("invalidating while a fetch is in flight", () => {
  /**
   * A client with `[job("a")]` already cached and one refetch on the wire.
   * Every request resolves only when `releaseAll` is called.
   */
  async function gatedActiveClient() {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const releases: ((jobs: Job[]) => void)[] = [];
    vi.mocked(getQueue).mockImplementation(
      () =>
        new Promise<Job[]>((resolve) => {
          releases.push(resolve);
        }),
    );

    client.setQueryData(queryKeys.queue, [job("a")]);
    const observer = new QueryObserver(client, queueQueryOptions(client));
    const unsubscribe = observer.subscribe(() => {});
    await vi.waitFor(() => expect(getQueue).toHaveBeenCalledTimes(1));

    return {
      client,
      unsubscribe,
      releaseAll: (jobs: Job[]) => releases.forEach((r) => r(jobs)),
    };
  }

  beforeEach(() => {
    vi.mocked(getQueue).mockReset();
  });

  it("lets unknown-job progress events join the fetch instead of restarting it", async () => {
    const { client, unsubscribe, releaseAll } = await gatedActiveClient();

    // One progress event per whole percent, none of which may cancel the
    // request that is about to tell us what job z is.
    for (let i = 1; i <= 20; i += 1) {
      applyQueueEvent(client, event("progress", "z", { progress: i }));
    }
    expect(getQueue).toHaveBeenCalledTimes(1);

    releaseAll([job("a"), job("z")]);
    await vi.waitFor(() =>
      expect(
        client.getQueryData<Job[]>(queryKeys.queue)!.map((j) => j.id),
      ).toEqual(["a", "z"]),
    );
    unsubscribe();
  });

  it("still restarts the fetch for an unknown job's status_change", async () => {
    const { client, unsubscribe, releaseAll } = await gatedActiveClient();

    applyQueueEvent(client, event("status_change", "z", { status: "queued" }));
    await vi.waitFor(() => expect(getQueue).toHaveBeenCalledTimes(2));

    releaseAll([job("a"), job("z")]);
    await vi.waitFor(() =>
      expect(
        client.getQueryData<Job[]>(queryKeys.queue)!.map((j) => j.id),
      ).toEqual(["a", "z"]),
    );
    unsubscribe();
  });

  it("still restarts the fetch for an unknown job's error", async () => {
    const { client, unsubscribe, releaseAll } = await gatedActiveClient();

    applyQueueEvent(client, event("error", "z", { error: "boom" }));
    await vi.waitFor(() => expect(getQueue).toHaveBeenCalledTimes(2));

    releaseAll([job("a")]);
    unsubscribe();
  });

  it("does not restart the fetch when the stream reconnects", async () => {
    const { client, unsubscribe, releaseAll } = await gatedActiveClient();

    resyncAfterReconnect(client);
    expect(getQueue).toHaveBeenCalledTimes(1);

    releaseAll([job("a")]);
    unsubscribe();
  });
});

/**
 * A bulk parent and its children, the shape `GET /queue` answers with: the
 * parent at the top level, every child nested under it whatever its status,
 * and no child listed on its own.
 */
function parent(children: Job[], status: JobStatus = "downloading"): Job {
  return {
    ...job("p1", status),
    kind: "bulk",
    title: "Black Sands",
    progress_done: 0,
    progress_total: children.length,
    children,
  };
}

/** A child of "p1". */
function childJob(id: string, status: JobStatus = "queued"): Job {
  return { ...job(id, status), parent_id: "p1" };
}

/** The children of the one cached parent. */
function children(): Job[] {
  return queue()![0].children!;
}

/** An event as the backend emits it for a child: `parent_id` in the data. */
function childEvent(
  name: string,
  jobId: string,
  data: Record<string, unknown> = {},
): SSEEvent {
  return event(name, jobId, { kind: "download", parent_id: "p1", ...data });
}

describe("a bulk parent's children in the cache", () => {
  it("patches the child the event names, not the parent", () => {
    queryClient.setQueryData(queryKeys.queue, [
      parent([childJob("c1"), childJob("c2")]),
    ]);

    applyQueueEvent(
      queryClient,
      childEvent("status_change", "c2", {
        status: "downloading",
        title: "Kong",
      }),
    );

    expect(queue()!.map((j) => j.id)).toEqual(["p1"]);
    expect(children().map((c) => c.status)).toEqual(["queued", "downloading"]);
    expect(children()[1].title).toBe("Kong");
    // The parent's own fields are the backend's to derive; a child event says
    // nothing about them.
    expect(queue()![0].status).toBe("downloading");
    expect(invalidated).toEqual([]);
  });

  it("keeps a child that finishes, because GET /queue keeps listing it", () => {
    // Dropping done children would make the parent's counts lie: "3 done"
    // is read off the children themselves.
    queryClient.setQueryData(queryKeys.queue, [
      parent([childJob("c1"), childJob("c2")]),
    ]);

    applyQueueEvent(queryClient, childEvent("status_change", "c1", { status: "done" }));
    applyQueueEvent(
      queryClient,
      childEvent("status_change", "c2", { status: "cancelled" }),
    );

    expect(children().map((c) => c.status)).toEqual(["done", "cancelled"]);
    expect(queue()!.map((j) => j.id)).toEqual(["p1"]);
  });

  it("marks a failed child errored and keeps its message", () => {
    queryClient.setQueryData(queryKeys.queue, [parent([childJob("c1")])]);

    applyQueueEvent(
      queryClient,
      childEvent("error", "c1", { error: "ffmpeg exploded" }),
    );

    expect(children()[0]).toMatchObject({
      status: "error",
      error: "ffmpeg exploded",
    });
  });

  it("finds a child even when the event carries no parent_id", () => {
    // Belt and braces: the search falls back to scanning the parents rather
    // than treating the child as an unknown job and refetching for it.
    queryClient.setQueryData(queryKeys.queue, [parent([childJob("c1")])]);

    applyQueueEvent(
      queryClient,
      event("progress", "c1", { progress: 40, parent_id: null }),
    );

    expect(children()[0].progress).toBe(40);
    expect(invalidated).toEqual([]);
  });

  it("patches the parent's own fields without touching its children", () => {
    const kids = [childJob("c1", "done"), childJob("c2", "downloading")];
    queryClient.setQueryData(queryKeys.queue, [parent(kids)]);

    applyQueueEvent(
      queryClient,
      event("status_change", "p1", {
        status: "downloading",
        progress_done: 1,
        progress_total: 2,
        kind: "bulk",
        parent_id: null,
      }),
    );

    expect(queue()![0]).toMatchObject({ progress_done: 1, progress_total: 2 });
    expect(children()).toBe(kids);
  });

  it("drops the parent once its derived status is done", () => {
    queryClient.setQueryData(queryKeys.queue, [
      parent([childJob("c1", "done")]),
      job("other"),
    ]);

    applyQueueEvent(
      queryClient,
      event("status_change", "p1", { status: "done", parent_id: null }),
    );

    expect(queue()!.map((j) => j.id)).toEqual(["other"]);
  });

  it("refetches for a child another client submitted, so it can be shown", () => {
    // The snapshot in the event has no url or created_at to build a row from,
    // and the parent has to be re-read anyway for its derived counts.
    queryClient.setQueryData(queryKeys.queue, [parent([childJob("c1")])]);

    applyQueueEvent(
      queryClient,
      childEvent("status_change", "c-new", { status: "downloading" }),
    );

    expect(invalidated).toEqual([queryKeys.queue]);
    expect(children().map((c) => c.id)).toEqual(["c1"]);
  });

  it("refetches for a child whose parent it has never seen", () => {
    queryClient.setQueryData(queryKeys.queue, [job("a")]);

    applyQueueEvent(
      queryClient,
      event("status_change", "c1", { status: "downloading", parent_id: "p9" }),
    );

    expect(invalidated).toEqual([queryKeys.queue]);
  });
});

describe("row actions on a nested child", () => {
  it("puts a retried child back under its parent", () => {
    queryClient.setQueryData(queryKeys.queue, [
      parent([childJob("c1", "error"), childJob("c2", "done")]),
    ]);

    replaceJobInCache(queryClient, {
      ...childJob("c1", "queued"),
      title: "Kong",
    });

    expect(queue()!.map((j) => j.id)).toEqual(["p1"]);
    expect(children().map((c) => [c.id, c.status])).toEqual([
      ["c1", "queued"],
      ["c2", "done"],
    ]);
    expect(children()[0].title).toBe("Kong");
  });

  it("removes a dismissed child and leaves the parent standing", () => {
    queryClient.setQueryData(queryKeys.queue, [
      parent([childJob("c1", "error"), childJob("c2", "done")]),
    ]);

    removeJobFromCache(queryClient, "c1");

    expect(queue()!.map((j) => j.id)).toEqual(["p1"]);
    expect(children().map((c) => c.id)).toEqual(["c2"]);
  });

  it("writes a refused action onto the child row that asked for it", () => {
    queryClient.setQueryData(queryKeys.queue, [
      parent([childJob("c1"), childJob("c2")]),
    ]);

    setJobActionError(queryClient, "c2", "Job already finished");

    expect(children()[1].error).toBe("Job already finished");
    expect(children()[0].error).toBeNull();
  });

  it("names the parent a job hangs off, and nothing for a top-level one", () => {
    queryClient.setQueryData(queryKeys.queue, [
      parent([childJob("c1")]),
      job("a"),
    ]);

    expect(parentIdOfCachedJob(queryClient, "c1")).toBe("p1");
    expect(parentIdOfCachedJob(queryClient, "p1")).toBeNull();
    expect(parentIdOfCachedJob(queryClient, "a")).toBeNull();
    expect(parentIdOfCachedJob(queryClient, "nobody")).toBeNull();
  });
});

describe("reconciling a snapshot that nests children", () => {
  beforeEach(() => {
    vi.mocked(getQueue).mockReset();
  });

  it("keeps a dismissed child out of a response that still lists it", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    let release!: (jobs: Job[]) => void;
    vi.mocked(getQueue).mockReturnValue(
      new Promise<Job[]>((resolve) => {
        release = resolve;
      }),
    );
    client.setQueryData(queryKeys.queue, [
      parent([childJob("c1", "error"), childJob("c2", "done")]),
    ]);

    const fetching = client.fetchQuery(queueQueryOptions(client));
    await vi.waitFor(() => expect(getQueue).toHaveBeenCalled());
    removeJobFromCache(client, "c1");

    // The server answered from before the dismiss landed.
    release([parent([childJob("c1", "error"), childJob("c2", "done")])]);
    await fetching;

    const cached = client.getQueryData<Job[]>(queryKeys.queue)!;
    expect(cached.map((j) => j.id)).toEqual(["p1"]);
    expect(cached[0].children!.map((c) => c.id)).toEqual(["c2"]);
  });
});
