import { useState } from "react";
import { NameCombobox } from "@/components/library/NameCombobox";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useMoveMutation } from "@/hooks/useMoveMutation";
import { LibraryMoveConflict } from "@/lib/api";
import { plural } from "@/lib/format";
import { entryLabel, type RestoreTarget } from "@/lib/trash";
import type {
  LibraryAlbum,
  LibraryArtist,
  LibraryMoveRequest,
  LibraryMoveResponse,
  LibraryResponse,
  LibraryTrack,
} from "@/lib/types";

/**
 * What the dialog was opened on: a set of tracks from one folder, an album, or
 * an artist. The three map one to one onto the three shapes of
 * `POST /library/move`.
 */
export type MoveTarget =
  | {
      kind: "tracks";
      artist: LibraryArtist;
      /** The album the tracks live in, or null when they are Singles. */
      album: LibraryAlbum | null;
      tracks: readonly LibraryTrack[];
    }
  | { kind: "album"; artist: LibraryArtist; album: LibraryAlbum }
  | { kind: "artist"; artist: LibraryArtist };

/**
 * Everything the dialog can be pointed at: the three move targets, plus a
 * trash entry being restored somewhere other than where it came from. The
 * fields, the validation, and the conflict list are the same choice either
 * way, which is why restore reuses this dialog rather than growing its own.
 */
export type DialogTarget = MoveTarget | RestoreTarget;

/** The trash entry's own path, which is the only name a restore has to show. */
function restoreDescription(target: RestoreTarget): string {
  return `Restore "${entryLabel(target.entry)}" (${plural(
    target.entry.track_count,
    "track",
  )}) somewhere else.`;
}

/** Every artist folder name, for the artist combobox. */
function artistNames(library: LibraryResponse): string[] {
  return library.artists
    .filter((artist) => !artist.synthetic)
    .map((artist) => artist.name);
}

/**
 * The albums the chosen artist already has, so the album field suggests real
 * folders. An artist the user is inventing has none, which is the honest
 * answer: whatever they type becomes a new folder.
 */
function albumNames(library: LibraryResponse, artistName: string): string[] {
  const artist = library.artists.find(
    (candidate) => candidate.name.toLowerCase() === artistName.trim().toLowerCase(),
  );
  return artist === undefined ? [] : artist.albums.map((album) => album.name);
}

/** The sentence above the fields: exactly what is about to move. */
function describe(target: DialogTarget): string {
  if (target.kind === "restore") return restoreDescription(target);
  if (target.kind === "artist") return `Rename "${target.artist.name}".`;
  if (target.kind === "album") {
    return `Album "${target.album.name}" (${plural(
      target.album.track_count,
      "track",
    )}) from ${target.artist.name}.`;
  }
  const from = `${target.artist.name} · ${target.album?.name ?? "Singles"}`;
  return target.tracks.length === 1
    ? `"${target.tracks[0].title}" from ${from}.`
    : `${plural(target.tracks.length, "track")} from ${from}.`;
}

/** The request body for *target* with the names the user settled on. */
export function buildMoveRequest(
  target: MoveTarget,
  artist: string,
  album: string,
): LibraryMoveRequest {
  const trimmedArtist = artist.trim();
  if (target.kind === "artist") {
    return { path: target.artist.path, artist: trimmedArtist };
  }
  if (target.kind === "album") {
    return {
      path: target.album.path,
      artist: trimmedArtist,
      album: album.trim(),
    };
  }
  return {
    paths: target.tracks.map((track) => track.path),
    artist: trimmedArtist,
    album: album.trim(),
  };
}

/**
 * What a blank album field means, which is not the same for every target.
 *
 * For tracks, blank files them loose under the artist and clears their `ALBUM`
 * tag. For an album folder there is nothing loose to become: the backend falls
 * back to the folder's current name, so blank means "keep this name".
 */
function albumHint(target: DialogTarget): string {
  if (target.kind === "restore") {
    return target.entry.kind === "album"
      ? `Optional — leave blank to keep the name "${target.album ?? ""}".`
      : "Optional — leave blank to file the track loose under the artist.";
  }
  if (target.kind === "album") {
    return `Optional — leave blank to keep the name "${target.album.name}".`;
  }
  return "Optional — leave blank to file the track loose under the artist.";
}

/** The artist name the artist field starts out as. */
function initialArtist(target: DialogTarget): string {
  return target.kind === "restore" ? target.artist : target.artist.name;
}

/** What the album field starts out as: the name the thing already carries. */
function initialAlbum(target: DialogTarget): string {
  if (target.kind === "restore") return target.album ?? "";
  if (target.kind === "album") return target.album.name;
  if (target.kind === "tracks") return target.album?.name ?? "";
  return "";
}

/** What the dialog needs from the caller to run a restore in place of a move. */
export interface RestoreControls {
  /** Send the restore again, aimed at the names the user settled on. */
  submit: (names: { artist: string; album: string }) => void;
  isPending: boolean;
  /** The failed restore's error — the 409 that opened this dialog, at first. */
  error: Error | null;
  /** A field was edited: drop an error that described the old names. */
  onEdit?: () => void;
}

/**
 * Move mode, which owns its own mutation, or restore mode, where the caller
 * owns the mutation because the Trash tab has to react to it landing.
 *
 * The union is discriminated on `mode` rather than on `target.kind` so
 * TypeScript narrows the props themselves, and `mode` is optional on the move
 * side so every existing call site stays as it is.
 */
export type MoveDialogProps =
  | {
      mode?: "move";
      target: MoveTarget;
      library: LibraryResponse;
      /** Cancel, Escape, or a press outside: nothing happened, nothing changes. */
      onClose: () => void;
      /** The move landed. The result says where its files ended up. */
      onMoved: (result: LibraryMoveResponse) => void;
    }
  | {
      mode: "restore";
      target: RestoreTarget;
      library: LibraryResponse;
      onClose: () => void;
      restore: RestoreControls;
    };

/**
 * The move dialog: an artist field, an album field, and the 409 conflicts.
 *
 * Mount it with a `key` tied to the target so a new target starts with fresh
 * fields; there is no effect syncing props into state.
 *
 * An artist rename shows only the artist field — an artist is renamed, never
 * moved, so there is no album to choose. Everything else shows both, and a
 * blank album is a deliberate answer: the tracks become loose Singles and the
 * backend clears their `ALBUM` tag.
 *
 * In restore mode it is the same dialog over a trash entry: the 409 the
 * restore came back with is the conflict list, and submitting re-sends the
 * restore with the names the user picked instead of running a move.
 */
export function MoveDialog(props: MoveDialogProps) {
  const { target, library, onClose } = props;
  const restore = props.mode === "restore" ? props.restore : null;

  const [artist, setArtist] = useState(() => initialArtist(target));
  const [album, setAlbum] = useState(() => initialAlbum(target));
  const [invalid, setInvalid] = useState<string | null>(null);
  const move = useMoveMutation();

  const isRename = target.kind === "artist";
  // A restored artist folder has no album to choose, for the same reason a
  // rename has none: the entry is the folder itself.
  const showAlbum =
    !isRename && !(target.kind === "restore" && target.album === null);
  const error = restore === null ? move.error : restore.error;
  const isPending = restore === null ? move.isPending : restore.isPending;
  const conflict = error instanceof LibraryMoveConflict ? error : null;

  const title =
    restore !== null ? "Restore elsewhere" : isRename ? "Rename artist" : "Move";
  const submitLabel = restore !== null ? "Restore" : isRename ? "Rename" : "Move";
  const pendingLabel = restore !== null ? "Restoring…" : "Moving…";

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (artist.trim() === "") {
      setInvalid("Enter an artist name.");
      return;
    }
    setInvalid(null);
    if (props.mode === "restore") {
      props.restore.submit({ artist: artist.trim(), album: album.trim() });
      return;
    }
    move.mutate(buildMoveRequest(props.target, artist, album), {
      onSuccess: props.onMoved,
    });
  }

  /**
   * Wrap a field setter so a keystroke also clears the errors that described
   * the names the user has just changed — a 409 conflict list naming a
   * destination they have moved away from is worse than no message at all.
   *
   * `move.reset()` is guarded by `isError` deliberately: in TanStack Query
   * 5.102 resetting during a *pending* mutation detaches the observer, and the
   * `mutate`-level `onSuccess` that closes this dialog would then never fire.
   */
  function editField(set: (value: string) => void) {
    return (value: string) => {
      set(value);
      setInvalid(null);
      if (restore !== null) restore.onEdit?.();
      else if (move.isError) move.reset();
    };
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
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{describe(target)}</DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="flex flex-col gap-4">
          <NameCombobox
            label="Artist"
            placeholder="Pick an artist or type a new one"
            options={artistNames(library)}
            value={artist}
            onChange={editField(setArtist)}
          />

          {showAlbum && (
            <NameCombobox
              label="Album"
              hint={albumHint(target)}
              placeholder="Pick an album, type a new one, or leave blank"
              options={albumNames(library, artist)}
              value={album}
              onChange={editField(setAlbum)}
            />
          )}

          {invalid !== null && (
            <p role="alert" className="text-sm text-destructive">
              {invalid}
            </p>
          )}

          {conflict !== null && (
            <div role="alert" className="flex flex-col gap-1 text-sm text-destructive">
              <p>{conflict.message}</p>
              <ul className="list-inside list-disc font-mono text-xs">
                {conflict.conflicts.map((path) => (
                  <li key={path}>{path}</li>
                ))}
              </ul>
            </div>
          )}

          {error !== null && conflict === null && (
            <p role="alert" className="text-sm text-destructive">
              {error.message}
            </p>
          )}

          <p className="text-xs text-muted-foreground">
            ALBUMARTIST, ARTIST and ALBUM tags are rewritten to match. Folders
            left without audio are removed.
          </p>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={isPending}>
              {isPending ? pendingLabel : submitLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
