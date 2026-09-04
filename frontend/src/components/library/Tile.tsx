/**
 * The shared shell of the artist and album grids.
 *
 * Two tile columns on a phone and four from `sm` up, inside the app's
 * `max-w-2xl` column.
 */

/** The tile grid itself, reused by the album grid so the columns line up. */
export function TileGrid({ children }: { children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">{children}</div>
  );
}

/** The shell every tile shares: a square cover, a name, and a count line. */
export function Tile({
  onClick,
  cover,
  name,
  detail,
  badge,
}: {
  onClick: () => void;
  cover: React.ReactNode;
  name: string;
  detail: string;
  badge?: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex flex-col gap-2 rounded-lg p-1 text-left outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50"
    >
      {cover}
      <span className="text-sm leading-snug font-medium">{name}</span>
      <span className="-mt-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
        {detail}
        {badge}
      </span>
    </button>
  );
}
