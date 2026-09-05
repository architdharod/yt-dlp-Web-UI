import { memo, useCallback, useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { AlertTriangle, Info, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { probeUrl, submitBulkDownload } from "@/lib/api";
import { formatDuration, plural } from "@/lib/format";
import {
  MAX_FOLDER_NAME,
  buildBulkRequest,
  initialSelection,
  isSelectable,
  reconcileSelection,
  rowLabel,
  selectAll,
} from "@/lib/preview";
import type {
  CollectionPreview as CollectionPreviewData,
  Job,
  PreviewRow,
  ProbeResponse,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * How long the artist field is left alone before the dedup pass is re-run.
 *
 * The re-probe is cheap — the enumeration is cached server-side and only the
 * artist folder is read again — but a request per keystroke would still be one
 * per keystroke.
 */
const PROBE_DEBOUNCE_MS = 400;

/** Shown when a re-probe comes back as something other than a collection. */
const NOT_A_COLLECTION_MESSAGE =
  "The URL no longer looks like a playlist or album. Cancel and try again.";

/** What the artist input's `aria-describedby` points at. */
const ARTIST_STATUS_ID = "collection-artist-status";

/** The line under the header: what the collection holds, in one sentence. */
function summarise(preview: CollectionPreviewData): string {
  const parts = [plural(preview.total, "track")];
  if (preview.in_library > 0) parts.push(`${preview.in_library} in library`);
  if (preview.unavailable > 0) parts.push(`${preview.unavailable} unavailable`);
  return parts.join(" · ");
}

/** The right-hand cell: why a row is not simply ready to download. */
function StatusCell({ row }: { row: PreviewRow }) {
  if (row.status === "in_library") {
    return (
      <Badge variant="secondary" title={row.reason ?? undefined}>
        in library
      </Badge>
    );
  }
  if (row.status === "unavailable") {
    return (
      <span
        className="block truncate text-xs text-muted-foreground"
        title={row.reason ?? undefined}
      >
        {row.reason ?? "unavailable"}
      </span>
    );
  }
  return null;
}

/**
 * One row of the checklist.
 *
 * Memoised, and handed a toggle that never changes identity, so ticking one
 * box re-renders that box instead of all two thousand of them.
 */
const PreviewTableRow = memo(function PreviewTableRow({
  row,
  checked,
  onToggle,
}: {
  row: PreviewRow;
  checked: boolean;
  onToggle: (id: string, checked: boolean) => void;
}) {
  const selectable = isSelectable(row);
  return (
    <tr
      className={cn(
        "border-b last:border-0",
        !selectable && "text-muted-foreground opacity-60",
      )}
    >
      <td className="py-1.5 pr-2 align-middle">
        <Checkbox
          checked={checked}
          disabled={!selectable}
          onCheckedChange={(next) => onToggle(row.id, next)}
          aria-label={`Select ${rowLabel(row)}`}
        />
      </td>
      <th
        scope="row"
        className="max-w-0 truncate py-1.5 pr-2 text-left font-normal"
        title={rowLabel(row)}
      >
        {rowLabel(row)}
      </th>
      <td className="max-w-0 truncate py-1.5 pr-2 text-muted-foreground">
        {row.album ?? ""}
      </td>
      <td className="py-1.5 pr-2 text-right tabular-nums text-muted-foreground">
        {formatDuration(row.duration)}
      </td>
      <td className="py-1.5 text-right">
        <StatusCell row={row} />
      </td>
    </tr>
  );
});

/**
 * One re-probe, tagged with the id it was issued under.
 *
 * The id rides along with the request because `useMutation`'s own callbacks
 * run for *every* `mutate` call, superseded ones included; the artist alone
 * could not tell them apart (A → B → A would look current).
 */
interface ReprobeRequest {
  artist: string;
  id: number;
}

interface CollectionPreviewProps {
  /** The preview the probe answered with, as it was first shown. */
  preview: CollectionPreviewData;
  /**
   * The artist the user typed on the form, if any. The backend echoes back its
   * own suggestion whatever it deduped against, so this is the only record of
   * what the rows were actually checked for.
   */
  initialArtist?: string;
  /** Close without queueing anything. */
  onCancel: () => void;
  /** The bulk parent the backend created, on its way to the queue cache. */
  onQueued: (parent: Job) => void;
}

/**
 * The artist the opening rows were deduped against.
 *
 * This mirrors the backend's own `artist or enumeration.artist`: the typed
 * name won there too, so seeding from it means the checklist opens agreeing
 * with itself and no redundant re-probe fires.
 */
function openingArtist(
  initialArtist: string | undefined,
  preview: CollectionPreviewData,
): string {
  return (initialArtist ?? "").trim() || (preview.artist ?? "").trim();
}

/**
 * The flat checklist a collection URL opens: pick the tracks, name the artist
 * folder they all land in, and queue them as one bulk job.
 *
 * The list is a plain table inside one scrolling region — a 2000-row ceiling
 * of text rows costs less than a virtualiser would, and every row stays
 * findable by the browser's own search.
 *
 * Editing the artist re-probes rather than only relabelling: "in library" is a
 * verdict about a specific folder, so a corrected artist has to be re-checked
 * or the ticks would describe a folder the tracks are not going to. Until that
 * recheck lands, Download stays disabled: queueing on verdicts that describe
 * the previous folder is the bug the recheck exists to prevent.
 */
export function CollectionPreview({
  preview,
  initialArtist,
  onCancel,
  onQueued,
}: CollectionPreviewProps) {
  const [current, setCurrent] = useState(preview);
  const [artist, setArtist] = useState(() =>
    openingArtist(initialArtist, preview),
  );
  const [selected, setSelected] = useState<ReadonlySet<string>>(() =>
    initialSelection(preview),
  );
  /** The artist the rows on screen were deduped against. */
  const [probedArtist, setProbedArtist] = useState(() =>
    openingArtist(initialArtist, preview),
  );
  /**
   * The rows a re-probe answer has to be reconciled against. Held in a ref
   * because the answer arrives from a callback that must not be re-created (or
   * go stale) on every keystroke.
   */
  const rowsRef = useRef(preview.rows);
  /** The id of the newest re-probe; anything older is an out-of-order answer. */
  const latestRequestId = useRef(0);
  /** The debounce in flight, so a submit can call it off. */
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /**
   * Whether the last re-probe answered with something that is not a
   * collection.  It leaves Download disabled, so it has to say so: otherwise
   * the button is dead with nothing on screen explaining why.
   */
  const [probeMismatch, setProbeMismatch] = useState(false);

  const reprobe = useMutation<ProbeResponse, Error, ReprobeRequest>({
    mutationFn: (request) => probeUrl(current.url, request.artist),
    onSuccess: (result, request) => {
      // A slower earlier request landing after a newer one: its verdicts are
      // about a folder the user has already moved on from.
      if (request.id !== latestRequestId.current) return;
      // A URL that previewed as a collection cannot become a track; if it
      // somehow did, there is nothing to re-render the checklist from. Bailing
      // out before `probedArtist` moves leaves Download disabled: a
      // non-collection answer fails closed rather than queueing on stale
      // verdicts.
      if (result.type !== "collection") {
        setProbeMismatch(true);
        return;
      }
      setProbeMismatch(false);
      // Only now is the checklist a statement about this folder.
      setProbedArtist(request.artist);
      const next = result.preview;
      // Read the ref now, not inside the updater: React runs an updater at the
      // next render, by which point the ref would already hold the new rows
      // and every verdict would look unchanged.
      const previousRows = rowsRef.current;
      rowsRef.current = next.rows;
      setSelected((previous) =>
        reconcileSelection(previous, previousRows, next.rows, next.large),
      );
      setCurrent(next);
    },
    // Nothing to undo on failure: `probedArtist` is deliberately left behind,
    // so Download stays disabled until a recheck actually lands. Retyping the
    // same name does *not* re-fire the effect — an identical string is an
    // identical dep — which is what the Retry button is for. The error on
    // screen is observer state, and the observer only ever follows the newest
    // `mutate` call, so a stale rejection cannot surface either.
  });

  const submit = useMutation<Job, Error, void>({
    mutationFn: () =>
      submitBulkDownload(buildBulkRequest(current, artist, selected)),
    onSuccess: onQueued,
  });

  const trimmedArtist = artist.trim();
  const { mutate: runProbe, reset: resetProbe, isError: probeFailed } = reprobe;

  useEffect(() => {
    // Blank is not a folder to check against, and the submit button is already
    // disabled on it; the rows keep the last verdict until a name comes back.
    if (trimmedArtist === "") return;
    if (trimmedArtist === probedArtist) {
      // Back on the name the rows already describe: a failure recorded on the
      // way out has nothing left to report, and leaving it up would show an
      // error line and a Retry button beside an enabled Download.  Guarded on
      // `isError` because `reset()` is a state write, and an unconditional one
      // here would loop.
      if (probeFailed) resetProbe();
      setProbeMismatch(false);
      return;
    }
    const timer = setTimeout(() => {
      debounceRef.current = null;
      latestRequestId.current += 1;
      runProbe({ artist: trimmedArtist, id: latestRequestId.current });
    }, PROBE_DEBOUNCE_MS);
    debounceRef.current = timer;
    return () => {
      clearTimeout(timer);
      if (debounceRef.current === timer) debounceRef.current = null;
    };
    // `reset` is bound to the observer and stable across renders, exactly as
    // `mutate` is, so listing it does not re-arm the debounce.
  }, [trimmedArtist, probedArtist, runProbe, resetProbe, probeFailed]);

  // Stable across renders (`setSelected` is), so a memoised row only re-renders
  // when its own tick changes.
  const toggle = useCallback((id: string, checked: boolean) => {
    setSelected((previous) => {
      const next = new Set(previous);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  /**
   * Ask again for the name already in the field.
   *
   * A pending debounce is called off first: the error on screen belongs to the
   * *previous* probe, so an edit made since then can have left a timer due,
   * and letting it fire would send a second request for the same name.
   * Bumping the id keeps any request still in flight from answering over this
   * one.
   */
  function retryProbe() {
    // Nothing to ask about: blank is not a folder, and the effect skips it too.
    if (trimmedArtist === "") return;
    if (debounceRef.current !== null) {
      clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
    setProbeMismatch(false);
    latestRequestId.current += 1;
    runProbe({ artist: trimmedArtist, id: latestRequestId.current });
  }

  function queueSelection() {
    // A recheck that has not started yet would otherwise fire mid-submit and
    // move the ticks under the user.
    if (debounceRef.current !== null) {
      clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
    submit.mutate();
  }

  const title = current.title ?? current.url;
  /** Blank while nothing is wrong; one region, so it is announced when it fills. */
  const artistStatus = reprobe.isPending
    ? `Rechecking the library for ${reprobe.variables?.artist ?? ""}...`
    : reprobe.isError
      ? `Could not recheck the library: ${reprobe.error.message}`
      : probeMismatch
        ? NOT_A_COLLECTION_MESSAGE
        : "";
  const canSubmit =
    trimmedArtist.length > 0 &&
    selected.size > 0 &&
    !submit.isPending &&
    // The ticks have to be about the folder the field names.
    !reprobe.isPending &&
    trimmedArtist === probedArtist;

  return (
    <Card className="flex min-h-0 flex-1 flex-col">
      <CardHeader>
        <CardTitle className="truncate" title={title}>
          {title}
        </CardTitle>
        <CardDescription>{summarise(current)}</CardDescription>
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col gap-4">
        <div className="flex flex-col gap-2">
          <Label htmlFor="collection-artist">Artist *</Label>
          <Input
            id="collection-artist"
            type="text"
            value={artist}
            required
            maxLength={MAX_FOLDER_NAME}
            aria-describedby={ARTIST_STATUS_ID}
            placeholder="Folder every selected track lands in"
            onChange={(e) => setArtist(e.target.value)}
          />
          <div className="flex items-center gap-2">
            <p
              id={ARTIST_STATUS_ID}
              role="status"
              className={cn(
                "flex items-center gap-1.5 text-xs",
                reprobe.isError || probeMismatch
                  ? "text-destructive"
                  : "text-muted-foreground",
              )}
            >
              {reprobe.isPending && <Loader2 className="size-3 animate-spin" />}
              {artistStatus}
            </p>
            {reprobe.isError && !reprobe.isPending && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={retryProbe}
              >
                Retry
              </Button>
            )}
          </div>
        </div>

        {current.large && (
          <p className="flex items-start gap-1.5 rounded-md bg-muted p-2 text-xs">
            <AlertTriangle className="mt-px size-3.5 shrink-0" />
            <span>
              That&apos;s a lot to queue at once, so nothing is selected. Pick
              what you want, or use a narrower URL.
            </span>
          </p>
        )}

        {current.notices.map((notice) => (
          <p
            key={notice}
            className="flex items-start gap-1.5 text-xs text-muted-foreground"
          >
            <Info className="mt-px size-3.5 shrink-0" />
            <span>{notice}</span>
          </p>
        ))}

        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setSelected(selectAll(current.rows))}
          >
            Select all
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setSelected(new Set<string>())}
          >
            Select none
          </Button>
          <span
            role="status"
            aria-live="polite"
            className="text-xs text-muted-foreground tabular-nums"
          >
            {selected.size} selected
          </span>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto rounded-md border">
          <table className="w-full table-fixed text-sm">
            <caption className="sr-only">Tracks in {title}</caption>
            <thead className="sticky top-0 bg-card">
              <tr className="border-b text-xs text-muted-foreground">
                <th scope="col" className="w-8 py-1.5 pl-2">
                  <span className="sr-only">Selected</span>
                </th>
                <th scope="col" className="py-1.5 pr-2 text-left font-normal">
                  Title
                </th>
                <th
                  scope="col"
                  className="w-1/4 py-1.5 pr-2 text-left font-normal"
                >
                  Album
                </th>
                <th
                  scope="col"
                  className="w-16 py-1.5 pr-2 text-right font-normal"
                >
                  Length
                </th>
                <th
                  scope="col"
                  className="w-28 py-1.5 pr-2 text-right font-normal"
                >
                  Status
                </th>
              </tr>
            </thead>
            <tbody className="[&_td]:pl-2 [&_th]:pl-2">
              {current.rows.map((row) => (
                <PreviewTableRow
                  key={row.id}
                  row={row}
                  checked={selected.has(row.id)}
                  onToggle={toggle}
                />
              ))}
            </tbody>
          </table>
        </div>

        <p role="status" className="text-sm text-destructive">
          {submit.isError ? submit.error.message : ""}
        </p>

        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={submit.isPending}
            onClick={onCancel}
          >
            Cancel
          </Button>
          <Button type="button" disabled={!canSubmit} onClick={queueSelection}>
            {submit.isPending
              ? "Queueing..."
              : `Download ${plural(selected.size, "track")}`}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
