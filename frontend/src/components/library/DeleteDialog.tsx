import type { MoveTarget } from "@/components/library/MoveDialog";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useDeleteMutation } from "@/hooks/useDeleteMutation";
import { plural } from "@/lib/format";
import type { LibraryDeleteRequest, LibraryDeleteResponse } from "@/lib/types";

/**
 * How many tracks *target* takes with it — the number the confirmation names.
 *
 * All three come from the library query the browser is already rendering:
 * there is no count to fetch and nothing to be out of date with the tiles the
 * user is looking at.
 */
export function deleteTrackCount(target: MoveTarget): number {
  if (target.kind === "artist") return target.artist.track_count;
  if (target.kind === "album") return target.album.track_count;
  return target.tracks.length;
}

/**
 * The question, worded as the prototype words it: the item by name and its
 * track count, or a plain count for a selection, which has no one name.
 */
export function deleteQuestion(target: MoveTarget): string {
  const count = deleteTrackCount(target);
  if (target.kind === "tracks" && target.tracks.length !== 1) {
    return `Move ${plural(count, "track")} to trash?`;
  }
  const name =
    target.kind === "artist"
      ? target.artist.name
      : target.kind === "album"
        ? target.album.name
        : target.tracks[0].title;
  return `Move "${name}" (${plural(count, "track")}) to trash?`;
}

/**
 * The request body for *target*.
 *
 * An album or an artist travels as one `path` so the backend moves the whole
 * folder as a single trash entry and Restore brings it back intact, `cover.jpg`
 * and all. Tracks always travel as `paths`, one entry covering the selection,
 * whether the user ticked six of them or used one row's own Delete.
 */
export function buildDeleteRequest(target: MoveTarget): LibraryDeleteRequest {
  if (target.kind === "artist") return { path: target.artist.path };
  if (target.kind === "album") return { path: target.album.path };
  return { paths: target.tracks.map((track) => track.path) };
}

/**
 * The one confirmation before anything is trashed.
 *
 * Deliberately not a warning: a delete is a rename into `.trash` that Restore
 * undoes, and the second line says so. The dialog that cannot be undone is
 * the empty-trash one in the Trash tab.
 */
export function DeleteDialog({
  target,
  onClose,
  onDeleted,
}: {
  target: MoveTarget;
  /** Cancel, Escape, or a press outside: nothing happened, nothing changes. */
  onClose: () => void;
  /** The delete landed; the result carries the trash entry it became. */
  onDeleted: (result: LibraryDeleteResponse) => void;
}) {
  const remove = useDeleteMutation();

  function confirm() {
    remove.mutate(buildDeleteRequest(target), { onSuccess: onDeleted });
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Move to trash?</DialogTitle>
          <DialogDescription>{deleteQuestion(target)}</DialogDescription>
        </DialogHeader>

        <p className="text-xs text-muted-foreground">
          You can restore it from Trash until you empty it.
        </p>

        {remove.error !== null && (
          <p role="alert" className="text-sm text-destructive">
            {remove.error.message}
          </p>
        )}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={confirm}
            disabled={remove.isPending}
          >
            {remove.isPending ? "Deleting…" : "Delete"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
