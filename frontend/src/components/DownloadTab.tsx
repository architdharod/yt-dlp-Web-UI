import { useState } from "react";
import { CollectionPreview } from "@/components/download/CollectionPreview";
import {
  BLANK_FIELDS,
  DownloadForm,
  type DownloadFields,
} from "@/components/DownloadForm";
import { QueueDisplay } from "@/components/QueueDisplay";
import { useQueueActions } from "@/hooks/useQueueActions";
import type { CollectionPreview as CollectionPreviewData } from "@/lib/types";

/** An open checklist: the preview, and the artist it was deduped against. */
interface PendingCollection {
  preview: CollectionPreviewData;
  artist: string;
}

/** The download form and the in-flight queue it feeds. */
export function DownloadTab() {
  const { addJob } = useQueueActions();
  const [pending, setPending] = useState<PendingCollection | null>(null);
  /**
   * The form's fields live here so the checklist can take the form's place
   * without taking the typed URL with it.
   */
  const [fields, setFields] = useState<DownloadFields>(BLANK_FIELDS);

  // The checklist takes the whole tab while it is open — a preview can run to
  // hundreds of rows, and it needs the height to scroll in. Replacing the form
  // rather than sitting beside it is also what keeps CollectionPreview's
  // one-time initialisers honest: every preview gets a fresh mount.
  if (pending !== null) {
    return (
      <CollectionPreview
        preview={pending.preview}
        initialArtist={pending.artist}
        onCancel={() => setPending(null)}
        onQueued={(parent) => {
          addJob(parent);
          setPending(null);
          setFields(BLANK_FIELDS);
        }}
      />
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-6">
      <div className="shrink-0">
        <DownloadForm
          fields={fields}
          onFieldsChange={setFields}
          onJobCreated={addJob}
          onCollection={(preview, artist) => setPending({ preview, artist })}
        />
      </div>
      <QueueDisplay />
    </div>
  );
}
