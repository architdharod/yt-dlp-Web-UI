import { Tile, TileGrid } from "@/components/library/Tile";
import { CoverImage } from "@/components/library/CoverImage";
import { coverUrl } from "@/lib/api";
import { plural } from "@/lib/format";
import type { LibraryAlbum } from "@/lib/types";

/** The albums of one artist, as tiles. */
export function AlbumGrid({
  albums,
  onOpen,
}: {
  albums: readonly LibraryAlbum[];
  onOpen: (album: LibraryAlbum) => void;
}) {
  return (
    <TileGrid>
      {albums.map((album) => (
        <Tile
          key={album.path}
          onClick={() => onOpen(album)}
          cover={
            <CoverImage
              src={album.has_cover ? coverUrl(album) : null}
              label={album.name}
            />
          }
          name={album.name}
          detail={plural(album.track_count, "track")}
        />
      ))}
    </TileGrid>
  );
}
