import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import type { LibraryView } from "@/lib/library";

/** One clickable ancestor in the trail. */
interface Crumb {
  label: string;
  view: LibraryView;
}

/**
 * `Library > Artist > Album`, with every ancestor clickable and the current
 * level as plain text. The trail is built from the resolved view, so it can
 * never point at a folder the tree no longer has.
 */
export function LibraryBreadcrumb({
  crumbs,
  current,
  onNavigate,
  children,
}: {
  crumbs: readonly Crumb[];
  current: string;
  onNavigate: (view: LibraryView) => void;
  /** Level actions (Rename, Move album) — the prototype puts them here. */
  children?: ReactNode;
}) {
  return (
    <nav
      aria-label="Library breadcrumb"
      className="flex flex-wrap items-center gap-1 text-sm text-muted-foreground"
    >
      {/* Keyed by level, not label: an artist may well be named "Library". */}
      {crumbs.map((crumb) => (
        <span key={crumb.view.level} className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => onNavigate(crumb.view)}
            className="rounded px-1 font-medium text-foreground/70 outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            {crumb.label}
          </button>
          <ChevronRight className="size-3.5" aria-hidden />
        </span>
      ))}
      <span className="px-1 font-medium text-foreground" aria-current="page">
        {current}
      </span>
      {children !== undefined && (
        <span className="ml-auto flex items-center gap-1">{children}</span>
      )}
    </nav>
  );
}
