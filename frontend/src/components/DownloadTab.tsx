import { DownloadForm } from "@/components/DownloadForm";
import { QueueDisplay } from "@/components/QueueDisplay";
import { useQueueActions } from "@/hooks/useQueueActions";

/** The download form and the in-flight queue it feeds. */
export function DownloadTab() {
  const { addJob } = useQueueActions();

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-6">
      <div className="shrink-0">
        <DownloadForm onJobCreated={addJob} />
      </div>
      <QueueDisplay />
    </div>
  );
}
