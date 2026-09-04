import { Info } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover";
import { formatDuration, formatSize } from "@/lib/format";
import type { LibraryTrack } from "@/lib/types";

/** One `label: value` line of the detail list. */
function Detail({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="truncate text-muted-foreground">{label}</dt>
      <dd className="break-words">{value}</dd>
    </>
  );
}

/**
 * Everything about a track that the row itself has no space for: where the
 * file is, how big it is, how it was encoded, and its whole tag set.
 *
 * The tags come from the backend as `name -> values`, because Vorbis comments
 * legitimately repeat a key (several ARTIST lines); the values are joined so
 * every tag stays one row of the list.
 */
export function TrackDetailPopover({ track }: { track: LibraryTrack }) {
  const encoding = [
    track.bitrate === null ? null : `${Math.round(track.bitrate / 1000)} kbps`,
    track.sample_rate === null
      ? null
      : `${(track.sample_rate / 1000).toFixed(1)} kHz`,
  ].filter((part): part is string => part !== null);

  const tagNames = Object.keys(track.tags).sort();

  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label={`Details for ${track.title}`}
          />
        }
      >
        <Info />
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80">
        <PopoverTitle className="truncate">{track.title}</PopoverTitle>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
          <Detail label="File" value={track.name} />
          <Detail label="Path" value={track.path} />
          <Detail label="Size" value={formatSize(track.size)} />
          <Detail label="Duration" value={formatDuration(track.duration)} />
          <Detail label="Format" value={track.format.toUpperCase()} />
          {encoding.length > 0 && (
            <Detail label="Encoding" value={encoding.join(" · ")} />
          )}
        </dl>
        <div className="flex flex-col gap-1">
          <p className="text-xs font-medium">Tags</p>
          {tagNames.length === 0 ? (
            <p className="text-xs text-muted-foreground">No tags.</p>
          ) : (
            <dl className="grid max-h-56 grid-cols-[auto_1fr] gap-x-3 gap-y-1 overflow-y-auto text-xs">
              {tagNames.map((name) => (
                <Detail
                  key={name}
                  label={name}
                  value={track.tags[name].join(", ")}
                />
              ))}
            </dl>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
