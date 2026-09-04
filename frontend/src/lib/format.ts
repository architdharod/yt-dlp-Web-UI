/**
 * Formatting shared by the queue rows and the library browser, kept in one
 * place so a duration reads the same wherever it appears.
 */

/** Format seconds as `m:ss`, or `h:mm:ss` past an hour. Null reads `--:--`. */
export function formatDuration(seconds: number | null): string {
  if (seconds == null) return "--:--";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(sec).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

const SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

/** Format a byte count as a short human-readable size, e.g. `27.4 MB`. */
export function formatSize(bytes: number): string {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < SIZE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  // Bytes are whole; everything above gets one decimal place.
  const rendered = unit === 0 ? String(Math.round(value)) : value.toFixed(1);
  return `${rendered} ${SIZE_UNITS[unit]}`;
}

/** `1 album` / `2 albums` — the pluralisation the tiles and headers repeat. */
export function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}
