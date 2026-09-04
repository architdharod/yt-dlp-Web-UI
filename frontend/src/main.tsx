import React from "react";
import ReactDOM from "react-dom/client";
import "@/index.css";
import { DownloadForm } from "@/components/DownloadForm";
import { QueueDisplay } from "@/components/QueueDisplay";
import { useSSE } from "@/hooks/useSSE";

function App() {
  const {
    jobs,
    connected,
    error,
    cancelling,
    addJob,
    retryJob,
    cancelJob,
    dismissJob,
  } = useSSE();

  return (
    <div className="mx-auto flex h-dvh max-w-2xl flex-col gap-6 overflow-hidden p-4 sm:p-6">
      <header className="shrink-0">
        <h1 className="text-2xl font-bold tracking-tight">yt-dlp Web UI</h1>
        <p className="text-sm text-muted-foreground">
          Download royalty free audio content from different sources
        </p>
        {!connected && (
          <p className="text-xs text-muted-foreground">Reconnecting to queue…</p>
        )}
        {error && <p className="text-xs text-destructive">{error}</p>}
      </header>

      <main className="flex min-h-0 flex-1 flex-col gap-6">
        <div className="shrink-0">
          <DownloadForm onJobCreated={addJob} />
        </div>
        <QueueDisplay
          jobs={jobs}
          cancelling={cancelling}
          onRetry={retryJob}
          onCancel={cancelJob}
          onDismiss={dismissJob}
        />
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
