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

/** The thresholds `formatRelativeTime` steps through, largest unit last. */
const RELATIVE_UNITS: readonly { seconds: number; noun: string }[] = [
  { seconds: 60, noun: "minute" },
  { seconds: 60 * 60, noun: "hour" },
  { seconds: 24 * 60 * 60, noun: "day" },
];

/** Past this, "43 days ago" means less to a reader than the date itself. */
const RELATIVE_LIMIT_DAYS = 30;

/**
 * How long ago *iso* was, in words: "just now", "12 minutes ago", "3 days
 * ago", and a plain local date once that stops being informative.
 *
 * Hand-rolled rather than `Intl.RelativeTimeFormat` so the wording does not
 * depend on the browser's locale, which is the one thing about a deleted-at
 * line that has to read the same everywhere. *now* is a parameter so tests do
 * not have to freeze the clock.
 */
export function formatRelativeTime(iso: string, now: number = Date.now()): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;

  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < RELATIVE_UNITS[0].seconds) return "just now";
  if (seconds > RELATIVE_LIMIT_DAYS * 24 * 60 * 60) {
    return new Date(then).toLocaleDateString();
  }

  // The unit is the largest one the elapsed time fills; the divisor is the
  // unit below it, which is where its own length is written down.
  let divisor = 1;
  let noun = "second";
  for (const unit of RELATIVE_UNITS) {
    if (seconds < unit.seconds) break;
    divisor = unit.seconds;
    noun = unit.noun;
  }
  return `${plural(Math.floor(seconds / divisor), noun)} ago`;
}


/** The trailing "45 s" of a sentence that ends in a number of seconds. */
const TRAILING_SECONDS = / \d+ s$/;

/**
 * A sentence that ends in a number of seconds, with that number recomputed
 * from an absolute instant.
 *
 * The backend sends each such sentence once — a job's wait note ("YouTube rate
 * limit, retry 2 of 5 in 45 s"), a rate-limit banner — together with the
 * instant it is counting down to, instead of a tick every second. One event
 * per state rather than one per second per waiting job, and for a notice it is
 * the difference between a banner that stays dismissed and one that comes back
 * every fifteen seconds. That leaves the number in the sentence to go stale,
 * so it is replaced here with what is actually left.
 *
 * Only the trailing clause is matched, so the words in front of it stay the
 * backend's to write and a new wording needs no change here; a sentence that
 * does not end in seconds gets the countdown appended instead. An unreadable
 * instant returns the sentence untouched.
 */
export function withCountdown(
  text: string,
  instant: string,
  now: number = Date.now(),
): string {
  const until = Date.parse(instant);
  if (Number.isNaN(until)) return text;
  const seconds = Math.max(0, Math.ceil((until - now) / 1000));
  return TRAILING_SECONDS.test(text)
    ? text.replace(TRAILING_SECONDS, ` ${seconds} s`)
    : `${text} in ${seconds} s`;
}
