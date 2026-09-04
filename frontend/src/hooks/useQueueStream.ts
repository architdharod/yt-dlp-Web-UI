import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { connectQueueStream } from "@/lib/api";
import { applyQueueEvent, resyncAfterReconnect } from "@/lib/queueCache";

/**
 * Hold the queue's SSE connection open and patch the query cache with what it
 * delivers.
 *
 * The hook owns no job state of its own: every event goes through
 * `applyQueueEvent`, which is a plain function over the `QueryClient`. All it
 * keeps is whether the stream is up, for the header notice.
 */
export function useQueueStream(): {
  connected: boolean;
  error: string | null;
} {
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** True once the stream has errored, so the next onopen is a reconnect. */
  const hadErrorRef = useRef(false);

  useEffect(() => {
    const close = connectQueueStream(
      (event) => applyQueueEvent(queryClient, event),
      () => {
        hadErrorRef.current = true;
        setConnected(false);
        setError("Lost the connection to the queue; reconnecting…");
      },
      () => {
        setConnected(true);
        setError(null);
        // Events emitted while the stream was down were lost, and a backend
        // restart may have re-queued jobs this client has never seen.
        if (hadErrorRef.current) {
          hadErrorRef.current = false;
          resyncAfterReconnect(queryClient);
        }
      },
    );

    return close;
  }, [queryClient]);

  return { connected, error };
}
