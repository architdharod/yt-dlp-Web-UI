import { useState } from "react";
import { Music } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * The first two initials of *label*, e.g. "Black Sands" -> "BS".
 *
 * Splitting on whitespace leaves empty strings around any leading, trailing,
 * or doubled space, so the blanks are dropped before the first two words are
 * taken — otherwise " Black Sands" would initial to "B". A label with no
 * letters at all falls back to a question mark rather than an empty box.
 */
export function initials(label: string): string {
  const words = label.split(/\s+/).filter(Boolean);
  return (
    words
      .slice(0, 2)
      .map((word) => word[0])
      .join("")
      .toUpperCase() || "?"
  );
}

/**
 * Square cover art with a neutral fallback.
 *
 * *src* is null when there is nothing to ask the backend for — an artist with
 * no album to borrow art from, or an album folder with no `cover.jpg` — and the
 * placeholder renders without a request. A request that fails renders the same
 * placeholder: the broken URL is kept in state rather than a boolean, so a
 * refetch that changes `cover_version` (a new `cover.jpg` landed) gets a fresh
 * attempt instead of staying broken.
 *
 * The art is decorative in every place it is used: inside a labelled tile
 * button, or beside the album heading. It carries an empty `alt` and the
 * placeholder no screen-reader text, so a screen reader reads the name once
 * rather than twice. *label* only feeds the visible initials.
 */
export function CoverImage({
  src,
  label,
  className,
}: {
  src: string | null;
  label: string;
  className?: string;
}) {
  const [brokenSrc, setBrokenSrc] = useState<string | null>(null);
  const broken = src !== null && src === brokenSrc;

  const shell = cn(
    "aspect-square w-full overflow-hidden rounded-lg bg-muted",
    className,
  );

  if (src === null || broken) {
    return (
      <div
        aria-hidden
        className={cn(
          shell,
          "flex flex-col items-center justify-center gap-1 text-muted-foreground",
        )}
      >
        <Music className="size-5" />
        <span className="text-xs font-medium">{initials(label)}</span>
      </div>
    );
  }

  return (
    <img
      src={src}
      alt=""
      data-testid="cover-image"
      loading="lazy"
      decoding="async"
      onError={() => setBrokenSrc(src)}
      className={cn(shell, "object-cover")}
    />
  );
}
