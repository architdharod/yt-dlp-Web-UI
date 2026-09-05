import { useState } from "react";
import { MoveDialog } from "@/components/library/MoveDialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useLibraryQuery } from "@/hooks/useLibraryQuery";
import { useTrashQuery } from "@/hooks/useTrashQuery";
import {
  useEmptyTrashMutation,
  useRestoreMutation,
} from "@/hooks/useTrashMutations";
import { LibraryMoveConflict } from "@/lib/api";
import { formatRelativeTime, plural } from "@/lib/format";
import { entryLabel, restoreTarget, type RestoreTarget } from "@/lib/trash";
import type { TrashEntry, TrashResponse } from "@/lib/types";

/** The confirmation for the one action in the app that destroys files. */
function EmptyTrashDialog({
  trash,
  onClose,
  onConfirm,
  isPending,
  error,
}: {
  trash: TrashResponse;
  onClose: () => void;
  onConfirm: () => void;
  isPending: boolean;
  error: Error | null;
}) {
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Empty trash?</DialogTitle>
          <DialogDescription>
            {`Permanently delete ${plural(
              trash.entries.length,
              "item",
            )} (${plural(trash.track_count, "track")})?`}
          </DialogDescription>
        </DialogHeader>

        <p className="text-xs text-muted-foreground">
          This permanently deletes the files.
        </p>

        {error !== null && (
          <p role="alert" className="text-sm text-destructive">
            {error.message}
          </p>
        )}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={onConfirm}
            disabled={isPending}
          >
            {isPending ? "Emptying…" : "Empty trash"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** One deleted item: where it came from, how much of it there is, and Restore. */
function TrashRow({
  entry,
  onRestore,
  isRestoring,
}: {
  entry: TrashEntry;
  onRestore: () => void;
  isRestoring: boolean;
}) {
  return (
    <div
      role="listitem"
      className="flex min-h-9 items-center gap-2.5 rounded-lg px-2 py-1.5 hover:bg-muted"
    >
      <span className="min-w-0 flex-1 truncate font-mono text-xs">
        {entryLabel(entry)}
      </span>
      <span className="shrink-0 text-xs text-muted-foreground">
        {`${plural(entry.track_count, "track")} · deleted ${formatRelativeTime(
          entry.deleted_at,
        )}`}
      </span>
      <Button
        variant="outline"
        size="sm"
        onClick={onRestore}
        disabled={isRestoring}
        aria-label={`Restore ${entryLabel(entry)}`}
      >
        {isRestoring ? "Restoring…" : "Restore"}
      </Button>
    </div>
  );
}

/**
 * The Trash tab: everything deleted so far, with Restore per entry and one
 * Empty trash button.
 *
 * Restore asks nothing first — it is the undo, and a dialog in front of an
 * undo only makes the mistake harder to walk back. What it can hit is a 409:
 * the original path is occupied again, and the move dialog opens prefilled
 * with the entry so the user can put it somewhere else.
 *
 * The tab itself only exists while the trash is non-empty (`App` gates it on
 * the same query), so there is no "trash is empty" state to design.
 */
export function TrashTab() {
  const { data, isPending, isError, error, refetch } = useTrashQuery();
  const library = useLibraryQuery();
  const restore = useRestoreMutation();
  const empty = useEmptyTrashMutation();
  const [confirmingEmpty, setConfirmingEmpty] = useState(false);
  /** The entry whose restore came back 409, being aimed somewhere else. */
  const [conflictTarget, setConflictTarget] = useState<RestoreTarget | null>(
    null,
  );

  /** Put an entry back where it came from. */
  function restoreEntry(entry: TrashEntry) {
    restore.mutate(
      { id: entry.id },
      {
        onError: (failure) => {
          // Only a conflict has somewhere else to go; every other failure is
          // reported where the user pressed the button.
          if (failure instanceof LibraryMoveConflict) {
            setConflictTarget(restoreTarget(entry));
          }
        },
      },
    );
  }

  /** Send the same restore again, aimed at the names the dialog collected. */
  function restoreElsewhere(target: RestoreTarget, artist: string, album: string) {
    restore.mutate(
      {
        id: target.entry.id,
        artist,
        // An artist entry is a folder with no album to file it under, and a
        // blank album for tracks means a loose Single, exactly as in a move.
        ...(target.album === null ? {} : { album }),
      },
      { onSuccess: () => setConflictTarget(null) },
    );
  }

  if (isPending) {
    return (
      <div className="flex flex-col gap-2" aria-hidden>
        <Skeleton className="h-8 w-full" />
        <Skeleton className="h-8 w-full" />
      </div>
    );
  }

  if (isError) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Could not load the trash</CardTitle>
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

  // The conflict dialog needs the library's own artist and album names for its
  // comboboxes, so it waits for that query rather than offering a picker with
  // nothing in it. Until the query resolves there is no dialog, and the
  // conflict has to be reported here instead.
  const conflictDialog =
    conflictTarget !== null && library.data !== undefined
      ? { target: conflictTarget, library: library.data }
      : null;

  // A conflict is shown inside the dialog it opened, next to the fields that
  // can answer it, rather than twice.
  const failure =
    restore.error !== null && conflictDialog === null ? restore.error : null;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">
          {`Trash · ${plural(data.entries.length, "item")}`}
        </h2>
        <Button
          variant="destructive"
          size="sm"
          onClick={() => setConfirmingEmpty(true)}
        >
          Empty trash
        </Button>
      </div>

      {failure !== null && (
        <p role="alert" className="text-sm text-destructive">
          {failure.message}
        </p>
      )}

      <div role="list" className="flex flex-col">
        {data.entries.map((entry) => (
          <TrashRow
            key={entry.id}
            entry={entry}
            onRestore={() => restoreEntry(entry)}
            isRestoring={
              restore.isPending && restore.variables?.id === entry.id
            }
          />
        ))}
      </div>

      {confirmingEmpty && (
        <EmptyTrashDialog
          trash={data}
          onClose={() => {
            setConfirmingEmpty(false);
            // The failure was shown inside the dialog; keeping it would put a
            // stale error back on screen the next time the dialog is opened.
            if (empty.isError) empty.reset();
          }}
          onConfirm={() =>
            empty.mutate(undefined, {
              onSuccess: () => setConfirmingEmpty(false),
            })
          }
          isPending={empty.isPending}
          error={empty.error}
        />
      )}

      {conflictDialog !== null && (
        <MoveDialog
          key={conflictDialog.target.entry.id}
          mode="restore"
          target={conflictDialog.target}
          library={conflictDialog.library}
          onClose={() => {
            setConflictTarget(null);
            // The conflict was shown inside the dialog; leaving it behind as a
            // top-level alert would report a failure the user just dismissed.
            if (restore.isError) restore.reset();
          }}
          restore={{
            submit: ({ artist, album }) =>
              restoreElsewhere(conflictDialog.target, artist, album),
            isPending: restore.isPending,
            error: restore.error,
            onEdit: () => {
              if (restore.isError) restore.reset();
            },
          }}
        />
      )}
    </div>
  );
}
