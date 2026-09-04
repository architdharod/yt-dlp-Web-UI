import { useState } from "react";
import { AlbumGrid } from "@/components/library/AlbumGrid";
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
import { plural } from "@/lib/format";
import {
  ARTISTS_VIEW,
  resolveLocation,
  searchLibrary,
  type LibraryLocation,
  type LibrarySearchResult,
  type LibraryView,
} from "@/lib/library";
import type { LibraryResponse } from "@/lib/types";

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

  const location = resolveLocation(view, data);

  /** Go somewhere, dropping the highlight the last search result left behind. */
  function navigate(next: LibraryView, highlight: string | null = null) {
    setView(next);
    setHighlightPath(highlight);
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
        />
      )}
    </div>
  );
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
}: {
  library: LibraryResponse;
  location: LibraryLocation;
  highlightPath: string | null;
  onNavigate: (view: LibraryView, highlight?: string | null) => void;
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

  if (location.level === "artist") {
    return (
      <>
        <LibraryBreadcrumb
          crumbs={[{ label: "Library", view: ARTISTS_VIEW }]}
          current={artist.name}
          onNavigate={onNavigate}
        />
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
      />
      <AlbumHeader artist={artist} album={album} />
      <TrackList tracks={album.tracks} highlightPath={highlightPath} />
    </>
  );
}
