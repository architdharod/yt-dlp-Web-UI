import { Search, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/**
 * The one search box, shown at every level. Clearing it puts the user back on
 * whatever they were browsing, because the query overlays the navigation
 * rather than replacing it.
 */
export function LibrarySearch({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="relative">
      <Search
        className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
        aria-hidden
      />
      <Input
        type="search"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        aria-label="Search the library"
        placeholder="Search artists, albums, tracks"
        className="px-8"
      />
      {value !== "" && (
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label="Clear search"
          onClick={() => onChange("")}
          className="absolute top-1/2 right-1 -translate-y-1/2"
        >
          <X />
        </Button>
      )}
    </div>
  );
}
