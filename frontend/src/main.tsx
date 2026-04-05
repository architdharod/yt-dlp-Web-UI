import React from "react";
import ReactDOM from "react-dom/client";
import "@/index.css";
import { DownloadForm } from "@/components/DownloadForm";
import { QueueDisplay } from "@/components/QueueDisplay";
import { useSSE } from "@/hooks/useSSE";

function App() {
  const { jobs, addJob, retryJob } = useSSE();

  return (
    <div className="mx-auto flex h-dvh max-w-2xl flex-col gap-6 overflow-hidden p-4 sm:p-6">
      <header className="shrink-0">
        <h1 className="text-2xl font-bold tracking-tight">yt-dlp Web UI</h1>
        <p className="text-sm text-muted-foreground">
          Download royalty free audio content from different sources
        </p>
      </header>

      <main className="flex min-h-0 flex-1 flex-col gap-6">
        <div className="shrink-0">
          <DownloadForm onJobCreated={addJob} />
        </div>
        <QueueDisplay jobs={jobs} onRetry={retryJob} />
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
