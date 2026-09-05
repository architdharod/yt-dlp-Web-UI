import { Fragment, useRef } from "react";
import { AlertTriangle } from "lucide-react";
import { TrackDetailPopover } from "@/components/library/TrackDetailPopover";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { formatDuration, plural } from "@/lib/format";
import type { LibraryTrack } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * The multi-select, Move, and Delete affordances a track list may be given.
 *
 * All optional: the Singles list on an artist page and the album's track list
 * both get them, but a list rendered somewhere read-only simply leaves them
 * out rather than passing no-ops.
 */
export interface TrackListActions {
  /** Paths of the selected tracks in this list. */
  selected?: ReadonlySet<string>;
  onSelect?: (path: string, selected: boolean) => void;
  onClearSelection?: () => void;
  /** Move one track, from its own row action. */
  onMove?: (track: LibraryTrack) => void;
  /** Move everything currently ticked. */
  onMoveSelected?: () => void;
  /** Trash one track, from its own row action. */
  onDelete?: (track: LibraryTrack) => void;
  /** Trash everything currently ticked. */
  onDeleteSelected?: () => void;
}

/** The container format that needs no badge, because almost everything is it. */
const DEFAULT_FORMAT = "flac";

function TrackRow({
  track,
  position,
  highlighted,
  onHighlightMount,
  actions,
}: {
  track: LibraryTrack;
  position: string;
  highlighted: boolean;
  onHighlightMount: (node: HTMLDivElement | null) => void;
  actions: TrackListActions;
}) {
  const { selected, onSelect, onMove, onDelete } = actions;

  return (
    <div
      role="listitem"
      ref={highlighted ? onHighlightMount : undefined}
      data-highlighted={highlighted || undefined}
      className={cn(
        "group/row flex min-h-9 items-center gap-2.5 rounded-lg px-2 py-1.5 hover:bg-muted",
        highlighted && "ring-2 ring-ring/60",
      )}
    >
      {onSelect !== undefined && (
        <Checkbox
          checked={selected?.has(track.path) ?? false}
          onCheckedChange={(checked) => onSelect(track.path, checked)}
          aria-label={`Select ${track.title}`}
        />
      )}
      <span className="w-6 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
        {position}
      </span>
      <span className="min-w-0 flex-1 truncate">{track.title}</span>
      {track.error !== null && (
        <span
          className="flex items-center gap-1 text-xs text-destructive"
          title={track.error}
        >
          <AlertTriangle className="size-3.5" aria-hidden />
          <span className="sr-only">{`Could not read ${track.name}: ${track.error}`}</span>
        </span>
      )}
      {track.format.toLowerCase() !== DEFAULT_FORMAT && (
        <Badge variant="outline">{track.format.toUpperCase()}</Badge>
      )}
      <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
        {formatDuration(track.duration)}
      </span>
      {onMove !== undefined && (
        // Always in the DOM, only visible on hover or keyboard focus: an action
        // that appears on hover alone is unreachable from a keyboard.
        <Button
          variant="ghost"
          size="xs"
          className="opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100"
          onClick={() => onMove(track)}
        >
          Move
        </Button>
      )}
      {onDelete !== undefined && (
        <Button
          variant="ghost"
          size="xs"
          className="text-destructive opacity-0 group-hover/row:opacity-100 focus-visible:opacity-100"
          onClick={() => onDelete(track)}
        >
          Delete
        </Button>
      )}
      <TrackDetailPopover track={track} />
    </div>
  );
}

/** What a track with no `TRACKNUMBER` shows instead of an invented number. */
const NO_TRACK_NUMBER = "—";

/** The disc a track belongs to; an untagged track is on the first disc. */
function discOf(track: LibraryTrack): number {
  return track.disc_number ?? 1;
}

/** One disc's worth of consecutive tracks. */
interface DiscGroup {
  disc: number;
  tracks: LibraryTrack[];
}

/**
 * Split *tracks* into runs of the same disc, in the order given.
 *
 * The backend already sorts by disc then track number, so grouping in order
 * neither reorders anything nor hides a stray track: a disc that somehow
 * appears twice renders as two runs rather than being silently merged.
 */
function discGroups(tracks: readonly LibraryTrack[]): DiscGroup[] {
  const groups: DiscGroup[] = [];
  for (const track of tracks) {
    const disc = discOf(track);
    const last = groups[groups.length - 1];
    if (last !== undefined && last.disc === disc) last.tracks.push(track);
    else groups.push({ disc, tracks: [track] });
  }
  return groups;
}

/** A `Disc 2` subheading, shown only on albums that actually span discs. */
function DiscHeading({ disc }: { disc: number }) {
  return (
    <h4 className="mt-3 px-2 pb-1 text-xs font-semibold tracking-wider text-muted-foreground uppercase first:mt-0">
      {`Disc ${disc}`}
    </h4>
  );
}

/**
 * The numbered list of tracks under an album header, and the Singles list on
 * an artist page.
 *
 * Numbering is whatever the file's own `TRACKNUMBER` says, and an em dash when
 * it says nothing: a position in the list would read as a track number the
 * album does not have, which is worse than admitting the tag is missing.
 * Singles are unnumbered — there is no album for a number to belong to.
 *
 * A multi-disc album gets a `Disc N` subheading before each run, so the two
 * track 1s on screen are plainly on different discs.
 *
 * *highlightPath* is the track a search result jumped to. It is scrolled into
 * view once per path — a ref callback rather than an effect, because the row
 * only exists once the list has rendered, and the ref fires exactly then.
 */
/** The bar under a list with a tick in it: what is selected, and what to do. */
function SelectionBar({
  count,
  onMoveSelected,
  onDeleteSelected,
  onClearSelection,
}: {
  count: number;
  onMoveSelected: () => void;
  onDeleteSelected?: () => void;
  onClearSelection?: () => void;
}) {
  return (
    <div className="mt-2 flex items-center gap-2 rounded-lg bg-muted px-2 py-1.5 text-sm">
      <span className="font-medium">{plural(count, "track")} selected</span>
      <Button variant="outline" size="sm" onClick={onMoveSelected}>
        Move selected
      </Button>
      {onDeleteSelected !== undefined && (
        <Button variant="destructive" size="sm" onClick={onDeleteSelected}>
          Delete selected
        </Button>
      )}
      {onClearSelection !== undefined && (
        <Button variant="ghost" size="sm" onClick={onClearSelection}>
          Clear
        </Button>
      )}
    </div>
  );
}

export function TrackList({
  tracks,
  numbered = true,
  highlightPath = null,
  actions = {},
}: {
  tracks: readonly LibraryTrack[];
  numbered?: boolean;
  highlightPath?: string | null;
  actions?: TrackListActions;
}) {
  const scrolledFor = useRef<string | null>(null);

  const scrollHighlightIntoView = (node: HTMLDivElement | null) => {
    if (node === null || highlightPath === null) return;
    if (scrolledFor.current === highlightPath) return;
    scrolledFor.current = highlightPath;
    // jsdom has no scrollIntoView, and neither does a browser mid-print.
    node.scrollIntoView?.({ block: "center" });
  };

  const groups = discGroups(tracks);
  const showDiscs = numbered && new Set(tracks.map(discOf)).size > 1;

  const row = (track: LibraryTrack) => (
    <TrackRow
      key={track.path}
      track={track}
      position={
        numbered ? (track.track_number?.toString() ?? NO_TRACK_NUMBER) : ""
      }
      highlighted={track.path === highlightPath}
      onHighlightMount={scrollHighlightIntoView}
      actions={actions}
    />
  );

  // The rows carry explicit list roles rather than ul/li: the flex layout the
  // rows need strips the implicit list semantics in Safari/VoiceOver. Each
  // disc is its own list, because the `Disc N` heading between two runs would
  // otherwise sit inside a list as a non-listitem child.
  const selectedHere = tracks.filter(
    (track) => actions.selected?.has(track.path) ?? false,
  ).length;
  const bar =
    selectedHere > 0 && actions.onMoveSelected !== undefined ? (
      <SelectionBar
        count={selectedHere}
        onMoveSelected={actions.onMoveSelected}
        onDeleteSelected={actions.onDeleteSelected}
        onClearSelection={actions.onClearSelection}
      />
    ) : null;

  if (!showDiscs) {
    return (
      <div className="flex flex-col">
        <div role="list" className="flex flex-col">
          {tracks.map(row)}
        </div>
        {bar}
      </div>
    );
  }

  return (
    <div className="flex flex-col">
      {groups.map((group, index) => (
        <Fragment key={`${group.disc}-${index}`}>
          <DiscHeading disc={group.disc} />
          <div role="list" className="flex flex-col">
            {group.tracks.map(row)}
          </div>
        </Fragment>
      ))}
      {bar}
    </div>
  );
}
