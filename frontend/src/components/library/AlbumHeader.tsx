import { CoverImage } from "@/components/library/CoverImage";
import { coverUrl } from "@/lib/api";
import { formatDuration, plural } from "@/lib/format";
import { totalDuration } from "@/lib/library";
import type { LibraryAlbum, LibraryArtist } from "@/lib/types";

/** Large cover, artist, album name, and the totals, above the track list. */
export function AlbumHeader({
  artist,
  album,
}: {
  artist: LibraryArtist;
  album: LibraryAlbum;
}) {
  return (
    <div className="flex items-end gap-4">
      <div className="w-24 shrink-0 sm:w-32">
        <CoverImage
          src={album.has_cover ? coverUrl(album) : null}
          label={album.name}
        />
      </div>
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="truncate text-xs text-muted-foreground">
          {artist.name}
        </span>
        <h2 className="text-lg font-semibold text-balance">{album.name}</h2>
        <span className="text-xs text-muted-foreground">
          {plural(album.track_count, "track")} ·{" "}
          {formatDuration(totalDuration(album.tracks))}
        </span>
      </div>
    </div>
  );
}
