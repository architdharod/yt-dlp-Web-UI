import React from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@/index.css";
import { App } from "@/App";

/**
 * One client for the whole app.
 *
 * `refetchOnWindowFocus` stays on (the default) so a tab that was in the
 * background catches up on changes made elsewhere, and no query sets
 * `refetchInterval`: the SSE stream is what keeps the queue live.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
