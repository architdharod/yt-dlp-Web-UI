import { useEffect, useState } from "react";
import { AlertTriangle, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useNotices } from "@/hooks/useNotices";
import { runNoticeAction } from "@/lib/api";
import { withCountdown } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { Notice } from "@/lib/types";

/** What the user should call each service or source in the banner text. */
const SOURCE_LABELS: Record<Notice["source"], string> = {
  navidrome: "Navidrome",
  lidarr: "Lidarr",
  youtube: "YouTube",
  soundcloud: "SoundCloud",
  bandcamp: "Bandcamp",
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
 * rescan fails again after a success in between. A rate-limit lane re-raises
 * its notice on every change to the hold, which is what brings a dismissed
 * banner back when the wait moves. Keeping the ids in component state rather
 * than storage is deliberate — a reload should show the user what is still
 * wrong.
 *
 * A notice that carries an `action` also gets a button. The notice names the
 * route, so this component does not know that "Resume now" has anything to do
 * with rate limits, and a notice with a different action needs no change
 * here. The click is fire-and-forget: what it did comes back as a fresh
 * `notices` event and as the queue moving, so there is nothing to await and
 * report. A failure is logged rather than shown — the banner is already the
 * place where something is wrong, and a second line inside it saying the
 * button did not work helps nobody more than trying again does.
 */
/** How often a held lane's banner redraws its countdown. */
const COUNTDOWN_TICK_MS = 1000;

/**
 * A notice's text, with the countdown appended while a source is rate limited.
 *
 * The backend deliberately leaves the seconds out of the message: raising the
 * notice afresh is what un-dismisses a banner and gives it a new id, so a
 * message that counted down would have to be re-raised every second. It sends
 * an absolute `hold_until` instead and this ticks.
 *
 * Only for `rate_limit`. A bot check's `hold_until` is the one-hour ceiling,
 * not a wait anybody is counting down to: "resuming in 58 min" would be an
 * invitation to wait for something that is not going to happen on its own.
 */
function NoticeText({ notice }: { notice: Notice }) {
  const counting = notice.reason === "rate_limit" && notice.hold_until != null;
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!counting) return;
    const timer = setInterval(() => setNow(Date.now()), COUNTDOWN_TICK_MS);
    return () => clearInterval(timer);
  }, [counting]);

  if (!counting) return <>{notice.message}</>;
  return (
    <>
      {notice.message}{" "}
      {withCountdown("Resuming in 0 s", notice.hold_until!, now)}.
    </>
  );
}

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
            <NoticeText notice={notice} />
          </p>
          {notice.action != null && (
            <Button
              variant="outline"
              size="xs"
              className="shrink-0"
              onClick={() => {
                void runNoticeAction(notice.action!).catch((error: unknown) => {
                  console.error("Notice action failed", error);
                });
              }}
            >
              {notice.action.label}
            </Button>
          )}
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
