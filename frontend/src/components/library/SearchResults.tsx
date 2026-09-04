import { Badge } from "@/components/ui/badge";
import { plural } from "@/lib/format";
import type { LibrarySearchResult } from "@/lib/library";

/**
 * The flat result list that replaces the grid while a query is typed.
 *
 * Every row says what kind of thing it is and where it lives, because the same
 * title can be an album and a track on it, and the name alone would not tell
 * them apart.
 *
 * The count line is a live region and is always in the tree, so typing another
 * letter announces the new total — or "No matches" — to a screen reader that
 * cannot see the list redraw beneath the input.
 */
export function SearchResults({
  results,
  onSelect,
}: {
  results: readonly LibrarySearchResult[];
  onSelect: (result: LibrarySearchResult) => void;
}) {
  const empty = results.length === 0;

  return (
    <div className="flex flex-col">
      <p
        role="status"
        aria-live="polite"
        className={
          empty
            ? "py-10 text-center text-sm text-muted-foreground"
            : "px-2 pb-1 text-xs text-muted-foreground"
        }
      >
        {empty ? "No matches" : plural(results.length, "result")}
      </p>
      {results.map((result) => (
        <button
          key={result.key}
          type="button"
          onClick={() => onSelect(result)}
          className="flex items-center gap-2.5 rounded-lg px-2 py-1.5 text-left outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          <Badge variant="outline" className="shrink-0 capitalize">
            {result.kind}
          </Badge>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm">{result.name}</span>
            <span className="block truncate text-xs text-muted-foreground">
              {result.parent}
            </span>
          </span>
        </button>
      ))}
    </div>
  );
}
