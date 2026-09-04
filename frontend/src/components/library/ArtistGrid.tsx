import { CoverImage } from "@/components/library/CoverImage";
import { Tile, TileGrid } from "@/components/library/Tile";
import { Badge } from "@/components/ui/badge";
import { coverUrl } from "@/lib/api";
import { plural } from "@/lib/format";
import type { LibraryArtist } from "@/lib/types";

/**
 * The top level of the browser: one tile per artist folder.
 *
 * The artist's art is its `cover_album_path` album's art; an artist with no
 * album (only Singles, or the synthetic root bucket) gets the local
 * placeholder rather than a request that could only answer with one.
 */
export function ArtistGrid({
  artists,
  onOpen,
}: {
  artists: readonly LibraryArtist[];
  onOpen: (artist: LibraryArtist) => void;
}) {
  return (
    <TileGrid>
      {artists.map((artist) => {
        const coverAlbum = artist.albums.find(
          (album) => album.path === artist.cover_album_path,
        );
        return (
          <Tile
            key={artist.path}
            onClick={() => onOpen(artist)}
            cover={
              <CoverImage
                src={
                  coverAlbum?.has_cover === true ? coverUrl(coverAlbum) : null
                }
                label={artist.name}
              />
            }
            name={artist.name}
            detail={`${plural(artist.album_count, "album")} · ${plural(artist.track_count, "track")}`}
            badge={
              artist.synthetic ? (
                <Badge variant="outline">Needs sorting</Badge>
              ) : undefined
            }
          />
        );
      })}
    </TileGrid>
  );
}
