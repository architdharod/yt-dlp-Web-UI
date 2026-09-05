import { useState } from "react";
import { AlbumGrid } from "@/components/library/AlbumGrid";
import { DeleteDialog } from "@/components/library/DeleteDialog";
import { MoveDialog, type MoveTarget } from "@/components/library/MoveDialog";
import { AlbumHeader } from "@/components/library/AlbumHeader";
import { ArtistGrid } from "@/components/library/ArtistGrid";
import { LibraryBreadcrumb } from "@/components/library/LibraryBreadcrumb";
import { LibrarySearch } from "@/components/library/LibrarySearch";
import { SearchResults } from "@/components/library/SearchResults";
import { TrackList } from "@/components/library/TrackList";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useLibraryQuery } from "@/hooks/useLibraryQuery";
import { useTagMutation, type TagFeedback } from "@/hooks/useTagMutation";
import { plural } from "@/lib/format";
import {
  ARTISTS_VIEW,
  resolveLocation,
  searchLibrary,
  type LibraryLocation,
  type LibrarySearchResult,
  type LibraryView,
} from "@/lib/library";
import type {
  LibraryAlbum,
  LibraryMoveResponse,
  LibraryResponse,
  LibraryTrack,
} from "@/lib/types";

/** A small uppercase heading over the Singles list. */
function SectionHeading({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="px-2 text-xs font-semibold tracking-wider text-muted-foreground uppercase">
      {children}
    </h3>
  );
}

/** Placeholder tiles for the first load, before any tree has arrived. */
function LibrarySkeleton() {
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-4" aria-hidden>
      {Array.from({ length: 8 }, (_, index) => (
        <div key={index} className="flex flex-col gap-2 p-1">
          <Skeleton className="aspect-square w-full" />
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-3 w-1/2" />
        </div>
      ))}
    </div>
  );
}

/** The counts line: what the whole library holds, whatever level is on screen. */
function LibraryTotals({ library }: { library: LibraryResponse }) {
  return (
    <>
      {plural(library.artist_count, "artist")} ·{" "}
      {plural(library.album_count, "album")} ·{" "}
      {plural(library.track_count, "track")}
    </>
  );
}

/**
 * The library browser: artist tiles, then album tiles, then the numbered track
 * list, with a breadcrumb back up and one flat search across all three levels.
 *
 * Navigation lives here as component state keyed by path, not in a router:
 * there is no URL to share, and a path survives the whole-tree replacement
 * that a `library_changed` refetch performs. The view is resolved against the
 * data on every render, so a folder that has been renamed or deleted underneath
 * the user drops them one level instead of rendering nothing.
 */
export function LibraryTab() {
  const { data, isPending, isFetching, isError, error, refetch } =
    useLibraryQuery();
  const [view, setView] = useState<LibraryView>(ARTISTS_VIEW);
  const [query, setQuery] = useState("");
  const [highlightPath, setHighlightPath] = useState<string | null>(null);
  // Held as a bare set of paths that a refetch never prunes: consumers
  // intersect it with the current data instead — `TrackList` with
  // `selectedHere` at render time, `trackActions` by filtering `tracks` at
  // submit time — so a background `library_changed` refetch cannot throw away
  // a selection the user is still making.
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [moveTarget, setMoveTarget] = useState<MoveTarget | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<MoveTarget | null>(null);
  // "Update metadata" needs no dialog: it is non-destructive, and the job it
  // queues shows up in the Download tab's in-flight list like any other.
  const {
    tagPath,
    pending: tagPending,
    feedback: tagFeedback,
    clearFeedback: clearTagFeedback,
  } = useTagMutation();

  const location = resolveLocation(view, data);

  /** Go somewhere, dropping the highlight the last search result left behind. */
  function navigate(next: LibraryView, highlight: string | null = null) {
    // The tag line names a path on the level being left behind.
    clearTagFeedback();
    setView(next);
    setHighlightPath(highlight);
    // A selection only ever means something within one folder, so leaving the
    // folder ends it.
    setSelected(new Set());
  }

  function toggleSelected(path: string, isSelected: boolean) {
    setSelected((current) => {
      if (!isSelected) {
        const next = new Set(current);
        next.delete(path);
        return next;
      }
      // One move carries one folder. The scanner flattens an album's `Disc 1`
      // and `Disc 2` subfolders into a single track list, so ticking across
      // them is easy to do by accident and the backend answers 400: all the
      // tracks in one move must come from the same folder. A move is
      // all-or-nothing by design, so this does not split into several
      // requests — reaching into another folder starts a new selection there.
      const folder = parentFolder(path);
      const elsewhere = [...current].some(
        (selectedPath) => parentFolder(selectedPath) !== folder,
      );
      return elsewhere ? new Set([path]) : new Set(current).add(path);
    });
  }

  function openMove(target: MoveTarget) {
    setMoveTarget(target);
  }

  /**
   * A delete is offered on the same three things a move is, so it takes the
   * same target: the dialog only has to name what it is about to trash.
   */
  function openDelete(target: MoveTarget) {
    setDeleteTarget(target);
  }

  /** Cancel or Escape: the dialog goes, the ticks the user made stay. */
  function closeMove() {
    setMoveTarget(null);
  }

  /**
   * The move landed: follow it if it moved the ground under the user, then
   * drop the selection, which described files that are no longer there.
   *
   * `navigate` runs before the invalidated library refetch lands, so for one
   * render the new path resolves against the old tree and the user sees the
   * level above. It settles as soon as the tree arrives.
   */
  function finishMove(result: LibraryMoveResponse) {
    const destination =
      moveTarget === null ? null : destinationView(moveTarget, result);
    setMoveTarget(null);
    if (destination !== null) navigate(destination);
    setSelected(new Set());
  }

  /**
   * The delete landed. An artist or an album that has just been trashed is no
   * longer somewhere to stand, so the user goes up a level; trashed tracks
   * leave their folder standing. The selection described files that are gone.
   */
  function finishDelete() {
    const target = deleteTarget;
    setDeleteTarget(null);
    setSelected(new Set());
    if (target === null) return;
    if (target.kind === "artist") navigate(ARTISTS_VIEW);
    else if (target.kind === "album")
      navigate({ level: "artist", artistPath: target.artist.path });
  }

  function openResult(result: LibrarySearchResult) {
    setQuery("");
    navigate(result.view, result.trackPath);
  }

  if (isPending) {
    return (
      <div className="flex flex-col gap-4">
        <Skeleton className="h-8 w-full" />
        <LibrarySkeleton />
      </div>
    );
  }

  if (isError) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Could not load the library</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col items-start gap-3">
          <p className="text-sm text-muted-foreground">{error.message}</p>
          <Button variant="outline" onClick={() => void refetch()}>
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <LibrarySearch value={query} onChange={setQuery} />

      <div className="flex items-baseline gap-2 text-xs text-muted-foreground">
        <span>
          <LibraryTotals library={data} />
        </span>
        {/* A refetch keeps the current tree on screen; this is the only sign. */}
        {isFetching && <span>Updating…</span>}
      </div>

      <TagFeedbackLine feedback={tagFeedback} />

      {query.trim() !== "" ? (
        <SearchResults
          results={searchLibrary(data, query)}
          onSelect={openResult}
        />
      ) : (
        <LibraryLevel
          library={data}
          location={location}
          highlightPath={highlightPath}
          onNavigate={navigate}
          selected={selected}
          onSelect={toggleSelected}
          onClearSelection={() => setSelected(new Set())}
          onMove={openMove}
          onDelete={openDelete}
          onTag={tagPath}
          tagPending={tagPending}
        />
      )}

      {/* Keyed by what it is moving, so a second target starts with fresh
          fields instead of the previous one's names. */}
      {moveTarget !== null && (
        <MoveDialog
          key={moveTargetKey(moveTarget)}
          target={moveTarget}
          library={data}
          onClose={closeMove}
          onMoved={finishMove}
        />
      )}

      {deleteTarget !== null && (
        <DeleteDialog
          key={moveTargetKey(deleteTarget)}
          target={deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onDeleted={finishDelete}
        />
      )}
    </div>
  );
}

/**
 * What came of the last "Update metadata" click, or nothing yet.
 *
 * One line for the whole tab rather than a message per row: only one request is
 * ever interesting at a time, and a row is too narrow for a 409's sentence.
 * `role="status"` so a screen reader hears the answer to a click that
 * otherwise changes nothing on this tab.
 *
 * The region is always mounted, empty when there is nothing to say: a live
 * region has to be in the tree *before* its content changes for the change to
 * be announced, and `useTagMutation.onMutate` clears the feedback on every
 * click, so a conditionally rendered line would be inserted already carrying
 * its text and might be read out late or not at all. Nothing shows on screen
 * while the text is empty, and the `failed` colour only applies once there is
 * a message to colour.
 */
function TagFeedbackLine({ feedback }: { feedback: TagFeedback | null }) {
  return (
    <p
      role="status"
      className={`text-xs ${
        feedback?.failed === true ? "text-destructive" : "text-muted-foreground"
      }`}
    >
      {feedback?.message ?? ""}
    </p>
  );
}

/** The folder a path sits in; "" for a file directly at the library root. */
function parentFolder(path: string): string {
  const index = path.lastIndexOf("/");
  return index === -1 ? "" : path.slice(0, index);
}

/**
 * Where to stand once *target* has moved, or null to stay put.
 *
 * A rename or an album move takes the folder the user is looking at out from
 * under them, and `resolveLocation` would drop them to the library root for
 * want of the old path. Moving tracks leaves the current folder standing, so
 * nothing has to move.
 *
 * `result.destination` is the backend's own answer for where the folder ended
 * up, and it holds even for a merge that moved no file at all (every track
 * already present, only sidecars skipped). Deriving the path from the first
 * moved file is the fallback for a backend that does not send it.
 */
function destinationView(
  target: MoveTarget,
  result: LibraryMoveResponse,
): LibraryView | null {
  if (result.destination !== null && result.destination !== undefined) {
    const folder = result.destination;
    if (target.kind === "artist") return { level: "artist", artistPath: folder };
    if (target.kind === "album") {
      return {
        level: "album",
        artistPath: folder.split("/")[0],
        albumPath: folder,
      };
    }
    return null;
  }

  const destination = result.moved[0]?.to;
  if (destination === undefined) return null;
  const segments = destination.split("/");
  const artistPath = segments[0];
  if (target.kind === "artist") return { level: "artist", artistPath };
  if (target.kind === "album") {
    return {
      level: "album",
      artistPath,
      albumPath: segments.slice(0, 2).join("/"),
    };
  }
  return null;
}

/** A stable identity for a move target, used as the dialog's React key. */
function moveTargetKey(target: MoveTarget): string {
  if (target.kind === "artist") return `artist:${target.artist.path}`;
  if (target.kind === "album") return `album:${target.album.path}`;
  return `tracks:${target.tracks.map((track) => track.path).join("|")}`;
}

/**
 * Whichever of the three levels the resolved location points at.
 *
 * Split out of the tab so each level is a plain early return rather than
 * another branch of one long conditional. The location carries the artist and
 * album objects, so there is nothing here that could fail to resolve.
 */
function LibraryLevel({
  library,
  location,
  highlightPath,
  onNavigate,
  selected,
  onSelect,
  onClearSelection,
  onMove,
  onDelete,
  onTag,
  tagPending,
}: {
  library: LibraryResponse;
  location: LibraryLocation;
  highlightPath: string | null;
  onNavigate: (view: LibraryView, highlight?: string | null) => void;
  selected: ReadonlySet<string>;
  onSelect: (path: string, selected: boolean) => void;
  onClearSelection: () => void;
  onMove: (target: MoveTarget) => void;
  onDelete: (target: MoveTarget) => void;
  onTag: (path: string) => void;
  tagPending: ReadonlySet<string>;
}) {
  if (location.level === "artists") {
    if (library.artist_count === 0) {
      return (
        <p className="py-10 text-center text-sm text-muted-foreground">
          Nothing in the library yet. Downloads land here once they finish.
        </p>
      );
    }
    return (
      <ArtistGrid
        artists={library.artists}
        onOpen={(artist) =>
          onNavigate({ level: "artist", artistPath: artist.path })
        }
      />
    );
  }

  const { artist } = location;

  /** The tracks and albums of this level, for the row actions below. */
  const trackTarget = (
    tracks: readonly LibraryTrack[],
    album: LibraryAlbum | null,
  ): MoveTarget => ({ kind: "tracks", artist, album, tracks });

  const trackActions = (tracks: readonly LibraryTrack[], album: LibraryAlbum | null) => {
    const ticked = () => tracks.filter((track) => selected.has(track.path));
    return {
      selected,
      onSelect,
      onClearSelection,
      onMove: (track: LibraryTrack) => onMove(trackTarget([track], album)),
      onMoveSelected: () => onMove(trackTarget(ticked(), album)),
      onDelete: (track: LibraryTrack) => onDelete(trackTarget([track], album)),
      onDeleteSelected: () => onDelete(trackTarget(ticked(), album)),
      // Rows in a real artist get the action; the synthetic bucket's do not.
      // Its files sit at the library root, which `POST /library/tag` refuses,
      // so the button could only ever 400 — `TrackList` renders none when
      // `onTag` is undefined. The artist level gets none either: the metadata
      // ticket rules out a per-artist and a whole-library trigger.
      onTag: artist.synthetic
        ? undefined
        : (track: LibraryTrack) => onTag(track.path),
      tagPending,
    };
  };

  if (location.level === "artist") {
    return (
      <>
        <LibraryBreadcrumb
          crumbs={[{ label: "Library", view: ARTISTS_VIEW }]}
          current={artist.name}
          onNavigate={onNavigate}
        >
          {/* The synthetic bucket is not a folder, so there is nothing to
              rename: its files are sorted by moving them out. */}
          {!artist.synthetic && (
            <>
              <Button
                variant="outline"
                size="sm"
                onClick={() => onMove({ kind: "artist", artist })}
              >
                Rename
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={() => onDelete({ kind: "artist", artist })}
              >
                Delete artist
              </Button>
            </>
          )}
        </LibraryBreadcrumb>
        {artist.albums.length > 0 && (
          <AlbumGrid
            albums={artist.albums}
            onOpen={(album) =>
              onNavigate({
                level: "album",
                artistPath: artist.path,
                albumPath: album.path,
              })
            }
          />
        )}
        {artist.singles.length > 0 && (
          <>
            {/* Root-level files are the same list under a blunter heading. */}
            <SectionHeading>
              {artist.synthetic ? "Needs sorting" : "Singles"}
            </SectionHeading>
            <TrackList
              tracks={artist.singles}
              numbered={false}
              highlightPath={highlightPath}
              actions={trackActions(artist.singles, null)}
            />
          </>
        )}
        {artist.albums.length === 0 && artist.singles.length === 0 && (
          <p className="py-10 text-center text-sm text-muted-foreground">
            No tracks.
          </p>
        )}
      </>
    );
  }

  const { album } = location;

  return (
    <>
      <LibraryBreadcrumb
        crumbs={[
          { label: "Library", view: ARTISTS_VIEW },
          {
            label: artist.name,
            view: { level: "artist", artistPath: artist.path },
          },
        ]}
        current={album.name}
        onNavigate={onNavigate}
      >
        <Button
          variant="outline"
          size="sm"
          disabled={tagPending.has(album.path)}
          onClick={() => onTag(album.path)}
        >
          Update metadata
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => onMove({ kind: "album", artist, album })}
        >
          Move album
        </Button>
        <Button
          variant="destructive"
          size="sm"
          onClick={() => onDelete({ kind: "album", artist, album })}
        >
          Delete album
        </Button>
      </LibraryBreadcrumb>
      <AlbumHeader artist={artist} album={album} />
      <TrackList
        tracks={album.tracks}
        highlightPath={highlightPath}
        actions={trackActions(album.tracks, album)}
      />
    </>
  );
}
