import { useState } from "react";
import { AlertTriangle, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useNotices } from "@/hooks/useNotices";
import { cn } from "@/lib/utils";
import type { Notice } from "@/lib/types";

/** What the user should call each service in the banner text. */
const SOURCE_LABELS: Record<Notice["source"], string> = {
  navidrome: "Navidrome",
  lidarr: "Lidarr",
};

/**
 * The open Navidrome and Lidarr problems, one dismissible line each.
 *
 * The wrapper is a plain layout container with no `role` or `aria-live`, kept
 * mounted even with nothing to say so the rows below it do not shift as
 * notices come and go. The live regions are the rows themselves:
 * `role="alert"` for an error, which is assertive and is announced reliably
 * even when the element is injected rather than filled in, and `role="status"`
 * for a warning, which is polite. Putting an `aria-live` on the wrapper as
 * well would nest one live region inside another and double-announce every
 * row — MDN warns that iOS VoiceOver speaks such content twice — so the
 * wrapper stays plain.
 *
 * Dismissal is client-side only, and by notice id: the backend gives a
 * re-raised problem a new id, so dismissing "bad password" hides it until the
 * rescan fails again after a success in between. Keeping the ids in component
 * state rather than storage is deliberate — a reload should show the user what
 * is still wrong.
 */
export function NoticeBanner() {
  const notices = useNotices();
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(new Set());

  const visible = notices.filter((notice) => !dismissed.has(notice.id));

  const dismiss = (id: string) =>
    setDismissed((current) => new Set(current).add(id));

  return (
    <div
      className={cn(
        "flex shrink-0 flex-col gap-2",
        // Present but weightless when there is nothing to say: an empty flex
        // column is already zero-height, so all that is left is to cancel the
        // gap the parent puts after it.
        visible.length === 0 && "-mb-4",
      )}
    >
      {visible.map((notice) => (
        <div
          key={notice.id}
          role={notice.level === "error" ? "alert" : "status"}
          className={cn(
            "flex items-start gap-2 rounded-lg border px-3 py-2 text-xs",
            notice.level === "error"
              ? "border-destructive/40 bg-destructive/10 text-destructive"
              : "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
          )}
        >
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          <p className="flex-1">
            <span className="font-medium">{SOURCE_LABELS[notice.source]}: </span>
            {notice.message}
          </p>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`Dismiss the ${SOURCE_LABELS[notice.source]} message`}
            onClick={() => dismiss(notice.id)}
          >
            <X aria-hidden />
          </Button>
        </div>
      ))}
    </div>
  );
}
