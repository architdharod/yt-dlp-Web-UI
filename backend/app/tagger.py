"""MusicBrainz tag fix for a single FLAC track.

What this is for
----------------
yt-dlp names a file after a *video*, not after a recording: "Artist - Song
(Official Video) [4K]" with an ``ARTIST`` that is really the uploading channel,
plus a pile of container junk (``DESCRIPTION``, ``SYNOPSIS``, ``ENCODER`` ...)
that came along for the ride.  After every download the queue hands the finished
FLAC to this module, which asks MusicBrainz what the recording is actually
called and, *only when it is confident*, rewrites ``TITLE`` and ``ARTIST`` and
drops the junk.

The confidence bar is the metadata ticket's, unchanged (see
``wayfinder/tickets/10-metadata-update-behaviour.md``):

* duration within :data:`DURATION_TOLERANCE_SECONDS` of the file's own length,
* the normalised cleaned title equals the normalised recording title, and
* the artist credit matches the *folder* artist.

Below that bar nothing in the file changes.  A wrong tag is worse than a noisy
one: the noisy one is still the track the user asked for.

What is never touched
---------------------
``ALBUMARTIST``, ``ALBUM``, ``SOURCEID``, ``SOURCEURL``, ``DATE``,
``TRACKNUMBER``, ``DISCNUMBER`` and the picture blocks.  The first two are what
the *folders* say, and folders are the library's source of truth (domain
model), so a lookup that disagreed with them would leave tags and tree telling
different stories.  ``SOURCEID``/``SOURCEURL`` are the provenance pair dedup
reads.  And **no MusicBrainz id is ever written**: Navidrome groups an album by
its release id when one is present, so a per-track id written by an automatic
per-track fix would shatter an album into one entry per track.

Why the library is wrapped rather than used directly
----------------------------------------------------
``musicbrainzngs`` 0.7.1 (January 2020) is the current release and the project
is in maintenance mode -- it works fine on Python 3.12, but it is not moving.
Everything that touches it lives behind :func:`search_recordings`, which
returns our own :class:`Candidate` objects, so swapping in a raw ``httpx`` call
against ``/ws/2/recording`` later means rewriting one function.  It is also
what lets every test in this module run without a network: :func:`fix_track`
takes the search callable as an argument.

Two things about that library are worth knowing here:

* It has **no timeout**.  ``_safe_read`` calls ``opener.open(req)`` with no
  timeout argument, so a hung MusicBrainz blocks the calling thread until the
  OS gives up, and on a 5xx it retries up to eight times with a growing delay
  (~56 s of sleeping in the worst case).  Neither is interruptible.  The queue
  therefore bounds the *call* with :func:`asyncio.wait_for` around
  :func:`asyncio.to_thread` rather than the thread -- see
  ``QueueManager._run_tag_fix``.
* Its rate limiter is a module-global (one process, 1 request/s), which is
  exactly right here because the single tagging worker already serialises every
  call this app makes.
"""

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import musicbrainzngs
from mutagen.flac import FLAC

logger = logging.getLogger(__name__)

# --- MusicBrainz identification ------------------------------------------
# The User-Agent is mandatory: MusicBrainz throttles anonymous and
# library-default agents hard, and the docs require enough information to
# contact whoever is making the requests.  Format:
#   "<app>/<version> ( <contact> )", which set_useragent assembles for us.
MB_APP_NAME = "music-for-arr"
MB_APP_VERSION = "2.0"
DEFAULT_MB_CONTACT = "https://github.com/architdharod/yt-dlp-Web-UI"

# MusicBrainz allows roughly one request per second per IP.  The library
# enforces this itself, in-process, which is enough because every call this app
# makes goes through the queue's single tagging worker.
MB_RATE_LIMIT_SECONDS = 1.0

# How many recordings to ask for.  The search is scored, not filtered (see
# search_recordings), so the right recording is not always first -- but past a
# handful the remaining hits are other songs entirely.
SEARCH_LIMIT = 8

# The match bar's duration window, from the metadata ticket.
DURATION_TOLERANCE_SECONDS = 5.0

# Every note this module produces starts with this, so a caller can recognise
# one without matching on the whole sentence.
NOTE_PREFIX = "tags not fixed"
NOTE_NO_MATCH = f"{NOTE_PREFIX}: no match"
NOTE_UNAVAILABLE = f"{NOTE_PREFIX}: MusicBrainz unavailable"
NOTE_TIMED_OUT = f"{NOTE_PREFIX}: MusicBrainz timed out"
NOTE_CANCELLED = f"{NOTE_PREFIX}: cancelled"
NOTE_FILE_MISSING = f"{NOTE_PREFIX}: file missing"
NOTE_DISABLED = f"{NOTE_PREFIX}: lookup disabled"
NOTE_NOT_FLAC = f"{NOTE_PREFIX}: not a FLAC"
NOTE_UNREADABLE = f"{NOTE_PREFIX}: file could not be read"
NOTE_WRITE_FAILED = f"{NOTE_PREFIX}: file could not be written"
NOTE_FAILED = f"{NOTE_PREFIX}: unexpected error"

# Tags a matched fix removes.  Everything here is yt-dlp/ffmpeg container
# residue -- a video description, an encoder banner, MP4 brand fields -- that
# says nothing about the music and, in the case of DESCRIPTION, can be
# kilobytes of it.  Defined once so the tagger and its tests cannot disagree
# about the list.  Compared case-insensitively; Vorbis comment keys are.
#
# PURL and COMMENT are deliberately *not* here even though yt-dlp writes them.
# The domain model (wayfinder/tickets/03-domain-model.md) reads PURL as the
# source URL for files downloaded before SOURCEID existed, so deleting it would
# throw away the only provenance those files have -- and Phase 9's manual pass
# runs apply_fix over exactly those imported files.
JUNK_TAGS: frozenset[str] = frozenset(
    {
        "DESCRIPTION",
        "SYNOPSIS",
        "ENCODER",
        "ENCODED_BY",
        "ENCODED-BY",
        "ENCODER_OPTIONS",
        "LANGUAGE",
        "COMPATIBLE_BRANDS",
        "MAJOR_BRAND",
        "MINOR_VERSION",
        "HANDLER_NAME",
        "VENDOR_ID",
    }
)

# Tags a fix must never write or delete, spelled out so the intent survives a
# later edit.  Only asserted in tests -- apply_fix simply does not touch them.
PRESERVED_TAGS: frozenset[str] = frozenset(
    {
        "ALBUMARTIST",
        "ALBUM",
        "SOURCEID",
        "SOURCEURL",
        "DATE",
        "TRACKNUMBER",
        "DISCNUMBER",
    }
)


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean env var; empty or missing yields *default*.

    docker compose substitutes an unset variable with an empty string, so
    ``""`` has to count as unset rather than as false.
    """
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def tag_fix_enabled() -> bool:
    """Whether the MusicBrainz lookup runs at all (``TAG_FIX_ENABLED``).

    Read per call rather than at import: it is one string comparison, and it
    means a test (or a homelab that never wants outbound traffic) can turn the
    lookup off without reloading the module.
    """
    return _env_flag("TAG_FIX_ENABLED", True)


def musicbrainz_contact() -> str:
    """The contact string that goes into the User-Agent."""
    return (os.environ.get("MUSICBRAINZ_CONTACT") or "").strip() or DEFAULT_MB_CONTACT


_configured = False


def configure_musicbrainz() -> None:
    """Set the User-Agent and rate limit once per process.

    Both must be set before the first web-service call: ``set_useragent`` is
    mandatory (the library raises ``UsageError`` without it) and
    ``set_rate_limit`` is documented as "must be invoked before the first Web
    service call".  Called lazily from :func:`search_recordings` rather than at
    import so reading ``MUSICBRAINZ_CONTACT`` happens after the app's env is in
    place, and so importing this module never has a side effect.
    """
    global _configured
    if _configured:
        return
    musicbrainzngs.set_useragent(MB_APP_NAME, MB_APP_VERSION, musicbrainz_contact())
    musicbrainzngs.set_rate_limit(limit_or_interval=MB_RATE_LIMIT_SECONDS, new_requests=1)
    _configured = True


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One recording MusicBrainz offered, reduced to what the bar looks at.

    ``artist_credit`` is the joined credit phrase ("Artist feat. Guest") and
    ``artist_names`` the individual credited artists; the bar accepts a match
    on either, so a folder called "Artist" still matches a recording credited
    to "Artist feat. Guest".

    ``length_ms`` is ``None`` when MusicBrainz does not know the recording's
    length, which the bar treats as "cannot be verified" rather than "fits".
    """

    title: str
    artist_credit: str
    artist_names: tuple[str, ...] = ()
    length_ms: int | None = None


@dataclass(frozen=True)
class Match:
    """The recording a candidate list was reduced to, as tags to write."""

    title: str
    artist: str


@dataclass(frozen=True)
class TagFixResult:
    """What one :func:`fix_track` run did.

    ``matched`` says MusicBrainz agreed with the file; ``changed`` says bytes
    were written, which is what decides whether a second ``library_changed``
    is emitted -- a match whose tags were already correct writes nothing.
    ``note`` is the human-readable reason a fix did not apply, and is the text
    that ends up in ``Job.detail``.
    """

    matched: bool = False
    changed: bool = False
    note: str | None = None


# ---------------------------------------------------------------------------
# Title cleaning
# ---------------------------------------------------------------------------

# Bracketed noise: the whole (...) or [...] group is dropped when it is *only*
# noise words.  Deliberately a keyword list rather than "drop every bracket":
# "(Live at Wembley)", "(Remix)" and "(Acoustic Version)" name a different
# recording, and dropping them would match the studio original instead.
_NOISE_KEYWORDS: frozenset[str] = frozenset(
    {
        "official video",
        "official music video",
        "official audio",
        "official lyric video",
        "official lyrics video",
        "official visualizer",
        "official visualiser",
        "official",
        "lyric video",
        "lyrics video",
        "lyrics",
        "lyric",
        "audio",
        "video",
        "visualizer",
        "visualiser",
        "music video",
        "hd",
        "hq",
        "4k",
        "1080p",
        "720p",
        "full song",
        "full video",
        "with lyrics",
        "free download",
    }
)

# A bracket group is noise when its words can be cut into a sequence of
# keywords with nothing left over, so "[Official Video HD]" goes as
# "official video" + "hd" while "(Official Video Live)" stays -- "live" is not
# a keyword, so no cut covers the whole group.  The scan below is the standard
# word-break dynamic program; the alternative (matching keywords greedily)
# fails on overlaps like "official music video" vs "official" + "music video".
_MAX_KEYWORD_WORDS = max(len(k.split()) for k in _NOISE_KEYWORDS)

# What separates words inside a bracket: whitespace and the punctuation
# YouTube uploaders string noise together with ("HD, Official Video").
_INNER_SPLIT = re.compile(r"[\s/,|+&·•\-–—]+")


def _all_noise(tokens: list[str]) -> bool:
    """Whether *tokens* is exactly a run of noise keywords, back to back.

    An empty token list counts as noise -- vacuously, there is nothing in it
    that identifies a recording.  That is what makes the repeated pass in
    :func:`_strip_noise_brackets` collapse nesting: "Song ((Official Video))"
    drops the inner group, and the "( )" left behind is then empty and goes
    too.  It also takes care of an empty "Song ()" from an uploader's stray
    brackets.
    """
    if not tokens:
        return True
    ok = [True] + [False] * len(tokens)
    for i in range(1, len(tokens) + 1):
        for w in range(1, min(_MAX_KEYWORD_WORDS, i) + 1):
            if ok[i - w] and " ".join(tokens[i - w:i]) in _NOISE_KEYWORDS:
                ok[i] = True
                break
    return ok[-1]


_BRACKETED = re.compile(r"[\(\[\{]([^\(\)\[\]\{\}]*)[\)\]\}]")

# "Artist - Topic" is YouTube's auto-generated channel suffix; it turns up in
# titles as well as in uploader names.
_TOPIC_SUFFIX = re.compile(r"\s*[-–—]\s*Topic\s*$", re.IGNORECASE)

# A trailing "| Some Label" / "| Full Album" tail.  Only the *last* pipe group
# goes: a title that is genuinely "A | B" keeps its left side.
_TRAILING_PIPE = re.compile(r"\s*\|[^|]*$")

# "feat. X", "ft X", "featuring X" and friends, from the first such marker to
# the end of the string.  MusicBrainz keeps featured artists in the *artist
# credit*, not in the recording title, so leaving them in the title would fail
# every comparison.
#
# Three rules keep this from eating real titles.
#
# * A *bracketed* credit consumes its own group and stops at the closing
#   bracket, so "Song (feat. X) (Live at Wembley)" keeps the qualifier that
#   names the recording.  Only the bare form runs to the end of the string,
#   where there is no bracket to say where the credit stops.
# * The marker must start on a word boundary, or "Lift Off" loses everything
#   from its "ft" and becomes "Li".  Abbreviated "feat"/"ft" additionally
#   require their dot: "A Great Feat In History" is a title, and a dotless
#   "feat" costs only a lookup that finds nothing.  "featuring" is a whole
#   word already, so it needs no dot.
# * A bare "with" is only a featuring marker *inside* a bracket -- "Song (with
#   Kimbra)" is a credit, while "Dancing With Myself", "Gone with the Wind"
#   and "With or Without You" are titles.
_FEAT = re.compile(
    r"\s*(?:"
    # bracketed: 'with' allowed, and only this group is consumed
    r"[\(\[\{]\s*(?:feat\.|ft\.|featuring|with)\s+[^\)\]\}]*[\)\]\}]"
    # bare: no 'with', and no bracket to stop at, so it runs to the end
    r"|\b(?:feat\.|ft\.|featuring)\s+.*$"
    r")",
    re.IGNORECASE,
)

# Closing brackets are dropped when nothing opened them.  A nested credit --
# "Song (feat. X (Bad))" -- leaves one behind: _FEAT stops at the first closing
# bracket, so the outer group's own closer survives with no opener.
_CLOSERS = {")": "(", "]": "[", "}": "{"}


def _strip_stray_closers(text: str) -> str:
    """Drop closing brackets that nothing opened, leaving pairs alone."""
    open_stack: list[str] = []
    kept: list[str] = []
    for character in text:
        if character in "([{":
            open_stack.append(character)
        elif character in _CLOSERS:
            if not open_stack or open_stack[-1] != _CLOSERS[character]:
                continue
            open_stack.pop()
        kept.append(character)
    return "".join(kept)

_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"))

_SEPARATORS = (" - ", " – ", " — ", " | ")

_WHITESPACE = re.compile(r"\s+")


def _strip_noise_brackets(text: str) -> str:
    """Drop bracketed groups whose contents are only noise words."""

    def replace(match: re.Match[str]) -> str:
        tokens = [
            token.lower().strip(".!")
            for token in _INNER_SPLIT.split(match.group(1))
            if token.strip(".!")
        ]
        return " " if _all_noise(tokens) else match.group(0)

    previous = None
    while previous != text:
        previous = text
        text = _BRACKETED.sub(replace, text)
    return text


def _strip_quotes(text: str) -> str:
    """Remove one layer of matching quotes wrapping the whole string."""
    text = text.strip()
    for opening, closing in _QUOTE_PAIRS:
        if len(text) >= 2 and text.startswith(opening) and text.endswith(closing):
            return text[1:-1].strip()
    return text


def clean_title(raw: str, folder_artist: str | None = None) -> str:
    """Turn a yt-dlp video title into something worth searching for.

    The steps, in order, each of them conservative on purpose -- an
    over-eager cleaner turns "Song (Live at Wembley)" into "Song" and then
    happily matches the studio recording:

    1. bracketed groups whose words are *all* noise are dropped, including
       multi-word runs of them ("[Official Video HD]");
    2. a trailing "- Topic" (YouTube's auto-channel suffix) goes;
    3. a trailing "| ..." tail goes, one level only;
    4. "feat. X" / "ft. X" / "featuring X" -- and a bracketed "(with X)" --
       is cut, because MusicBrainz keeps featured artists in the artist credit
       rather than the title.  A *bracketed* credit takes only its own group,
       so "Song (feat. X) (Live at Wembley)" keeps the qualifier; a bare one
       runs to the end of the string.  An unbracketed "with" is left alone: it
       is far more often part of the title ("Dancing With Myself");
    5. a wrapping pair of quotes is unwrapped;
    6. if the title still splits on " - " (or an en/em dash, or " | ") and the
       left side is the artist -- either the folder artist we were given, or,
       with no folder artist to check against, the first of exactly two parts
       -- the left side is dropped;
    7. closing brackets nothing opened are dropped, whitespace is collapsed
       and stray separators are trimmed.

    Returns the raw title stripped of whitespace when cleaning would leave
    nothing: an empty search term is worse than a noisy one.
    """
    if not raw:
        return ""
    text = _strip_noise_brackets(raw)
    text = _TOPIC_SUFFIX.sub("", text)
    text = _TRAILING_PIPE.sub("", text)
    text = _FEAT.sub("", text)
    text = _strip_quotes(text)
    text = _split_off_artist(text, folder_artist)
    text = _strip_stray_closers(text)
    text = _WHITESPACE.sub(" ", text).strip(" -–—|·•_")
    return text or raw.strip()


def _split_off_artist(text: str, folder_artist: str | None) -> str:
    """Drop a leading "Artist - " when the left side really is the artist.

    With a folder artist to compare against this is safe: the left side is
    dropped only when it normalises to that artist, so "Bob Dylan - Hurricane"
    under ``Bob Dylan/`` loses its prefix while "Careless - Whisper" does not.
    Without one, a single separator is still taken as "artist - title" (the
    overwhelmingly common shape of a music upload), but a title with two or
    more separators is left alone rather than guessed at.
    """
    for separator in _SEPARATORS:
        if separator not in text:
            continue
        parts = [part.strip() for part in text.split(separator)]
        if folder_artist:
            if normalise(parts[0]) == normalise(folder_artist) and len(parts) > 1:
                return separator.join(parts[1:])
            continue
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[1]
    return text


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)

# Curly quotes and dashes normalise to their ASCII shape before punctuation is
# stripped, so "Don’t" and "Don't" collapse to the same string either way.
_UNIFY = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    " ": " ",
}


def normalise(value: str | None) -> str:
    """Reduce a title or an artist name to a comparable core.

    Casefolded, decomposed to NFKD with the combining marks dropped (so
    "Beyoncé" equals "Beyonce"), ``&`` unified with "and", curly punctuation
    unified with its ASCII shape, all remaining punctuation removed and
    whitespace collapsed.  Comparison is only ever equality on this value --
    no fuzzy distance -- because the match bar is meant to be strict.
    """
    if not value:
        return ""
    text = value
    for character, replacement in _UNIFY.items():
        text = text.replace(character, replacement)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold()
    text = re.sub(r"\s*&\s*", " and ", text)
    text = _PUNCTUATION.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# MusicBrainz
# ---------------------------------------------------------------------------


def _credit_names(recording: dict) -> tuple[str, ...]:
    """The individual artist names in a recording's credit.

    ``artist-credit`` is a list that alternates credit dicts and join-phrase
    strings, so the strings are skipped.
    """
    names: list[str] = []
    for entry in recording.get("artist-credit") or []:
        if not isinstance(entry, dict):
            continue
        artist = entry.get("artist") or {}
        name = entry.get("name") or artist.get("name")
        if name:
            names.append(name)
    return tuple(names)


def _credit_phrase(recording: dict, names: Sequence[str]) -> str:
    """The joined artist credit, e.g. "Artist feat. Guest".

    MusicBrainz sends ``artist-credit-phrase`` on a search result; falling back
    to the joined names keeps a stubbed or older response usable.
    """
    phrase = recording.get("artist-credit-phrase")
    if phrase:
        return str(phrase)
    return ", ".join(names)


def _to_candidate(recording: dict) -> Candidate | None:
    """Reduce one search result to a :class:`Candidate`, or drop it."""
    title = recording.get("title")
    if not title:
        return None
    names = _credit_names(recording)
    length = recording.get("length")
    try:
        length_ms = int(length) if length is not None else None
    except (TypeError, ValueError):
        length_ms = None
    return Candidate(
        title=str(title),
        artist_credit=_credit_phrase(recording, names),
        artist_names=names,
        length_ms=length_ms,
    )


def search_recordings(
    title: str,
    artist: str | None,
    duration_seconds: float | None,
    limit: int = SEARCH_LIMIT,
) -> list[Candidate]:
    """Ask MusicBrainz for recordings that could be *title* by *artist*.

    The three terms go in as Lucene fields with ``strict=False``, which is what
    the library uses to join them with spaces rather than ``AND``.  That
    matters for ``dur``: MusicBrainz's index stores an exact millisecond
    length, so an ``AND``ed duration would reject every recording whose length
    differs from ours by a millisecond.  Space-joined it is a *scoring* term --
    it pulls recordings of about the right length to the top, and
    :func:`pick_match` is what actually enforces the ±5 s bar.

    Raises whatever ``musicbrainzngs`` raises; :func:`fix_track` is where those
    turn into a "tags not fixed" result.
    """
    configure_musicbrainz()
    fields: dict[str, str | int] = {"recording": title}
    if artist:
        fields["artist"] = artist
    if duration_seconds:
        fields["dur"] = int(duration_seconds * 1000)
    response = musicbrainzngs.search_recordings(limit=limit, **fields)
    candidates = [
        candidate
        for candidate in (_to_candidate(item) for item in response.get("recording-list", []))
        if candidate is not None
    ]
    logger.debug(
        "MusicBrainz returned %d candidate(s) for %r / %r", len(candidates), title, artist
    )
    return candidates


def pick_match(
    candidates: Iterable[Candidate],
    cleaned_title: str,
    folder_artist: str | None,
    duration_seconds: float | None,
) -> Match | None:
    """Return the first candidate that clears the bar, or ``None``.

    All three conditions are hard:

    * **Duration** within :data:`DURATION_TOLERANCE_SECONDS`.  A candidate with
      no known length, or a file with no known duration, never passes -- the
      condition cannot be checked, and "unknown" is not "close enough".
    * **Title** equal after :func:`normalise`.  No fuzzy distance: the cleaner
      has already removed the noise, and anything still different is a
      different recording.
    * **Artist** equal after :func:`normalise`, against either the whole credit
      phrase or any one credited artist.  The second half is what lets a folder
      called "Artist" match a recording credited to "Artist feat. Guest".

    Candidates are checked in the order MusicBrainz scored them, so the first
    one that clears the bar is also the best-scoring one that does.
    """
    if duration_seconds is None:
        return None
    wanted_title = normalise(cleaned_title)
    wanted_artist = normalise(folder_artist)
    if not wanted_title or not wanted_artist:
        return None

    for candidate in candidates:
        if candidate.length_ms is None:
            continue
        if abs(candidate.length_ms / 1000.0 - duration_seconds) > DURATION_TOLERANCE_SECONDS:
            continue
        if normalise(candidate.title) != wanted_title:
            continue
        credits = [candidate.artist_credit, *candidate.artist_names]
        if not any(normalise(name) == wanted_artist for name in credits):
            continue
        return Match(title=candidate.title, artist=candidate.artist_credit or "")
    return None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def apply_fix(flac_path: Path, match: Match) -> bool:
    """Write *match* into the FLAC at *flac_path*; return whether it changed.

    Writes ``TITLE`` and ``ARTIST`` (the artist *credit*, so a featuring credit
    shows up in the player) and deletes every tag in :data:`JUNK_TAGS`.
    Nothing else is read, written or deleted -- in particular none of
    :data:`PRESERVED_TAGS` and none of the picture blocks.

    A run that would change nothing does not call ``save``: mutagen rewrites
    the whole metadata region on save, so a no-op save would still change the
    file's bytes and its mtime, invalidate the library scan cache, and fire a
    rescan for nothing.  "Did the file change" is therefore decided *before*
    the write, by comparing what is there with what would go in.
    """
    audio = FLAC(flac_path)
    tags = audio.tags
    if tags is None:  # a FLAC with no VORBIS_COMMENT block yet
        audio.add_tags()
        tags = audio.tags

    changed = False

    if match.title and list(tags.get("TITLE", [])) != [match.title]:
        tags["TITLE"] = [match.title]
        changed = True
    if match.artist and list(tags.get("ARTIST", [])) != [match.artist]:
        tags["ARTIST"] = [match.artist]
        changed = True

    # Vorbis comment keys are case-insensitive but stored as written, so the
    # comparison is on the upper-cased key rather than on the key itself.
    for key in list(tags.keys()):
        if key.upper() in JUNK_TAGS:
            del tags[key]
            changed = True

    if changed:
        audio.save()
    return changed


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# What ``fix_track`` needs of a search function: title, artist, duration in,
# candidates out.  ``search_recordings`` takes one more (optional) argument
# than this; a stub in a test only has to take these three.
SearchCallable = Callable[[str, str | None, float | None], Sequence[Candidate]]


def fix_track(
    flac_path: Path | str,
    folder_artist: str | None,
    *,
    search: SearchCallable = search_recordings,
    should_cancel: Callable[[], bool] | None = None,
) -> TagFixResult:
    """Look the track up and fix its tags if MusicBrainz is confident.

    *folder_artist* is the name of the artist folder the file sits under -- the
    library's own answer to "whose track is this", which is what the match bar
    checks the credit against.

    *search* is injectable so the whole path can be exercised without a
    network; production leaves it at :func:`search_recordings`.

    *should_cancel* is polled at two checkpoints -- before the lookup and again
    between the lookup and the write.  A MusicBrainz request in flight cannot
    be interrupted (the library exposes no handle on it), so a cancel that
    arrives mid-request takes effect when the request returns, or when the
    caller's own timeout fires.

    Never raises for anything MusicBrainz does: a network error, a rate limit,
    a malformed response and an unreadable file all come back as a result whose
    ``note`` says so, because a failed tag fix must not fail a download that
    already put a usable file in the library.
    """
    path = Path(flac_path)

    if path.suffix.lower() != ".flac":
        # Domain-model rule: only FLAC takes part in tagging.
        return TagFixResult(note=NOTE_NOT_FLAC)
    if not tag_fix_enabled():
        return TagFixResult(note=NOTE_DISABLED)
    if not path.exists():
        return TagFixResult(note=NOTE_FILE_MISSING)
    if should_cancel is not None and should_cancel():
        return TagFixResult(note=NOTE_CANCELLED)

    try:
        audio = FLAC(path)
    except Exception as exc:
        logger.warning("Tag fix: could not read %s as FLAC: %s", path, exc)
        return TagFixResult(note=NOTE_UNREADABLE)

    raw_title = _first_tag(audio, "TITLE") or path.stem
    duration = getattr(audio.info, "length", None)
    if not duration:
        # A FLAC whose STREAMINFO has no sample count: the duration half of the
        # bar cannot be checked, so nothing could pass it anyway.
        return TagFixResult(note=NOTE_NO_MATCH)

    cleaned = clean_title(raw_title, folder_artist)
    try:
        candidates = search(cleaned, folder_artist, duration)
    except (
        musicbrainzngs.WebServiceError,
        musicbrainzngs.NetworkError,
        musicbrainzngs.ResponseError,
        OSError,
    ) as exc:
        # NetworkError and ResponseError are WebServiceError subclasses; both
        # are named for the reader.  OSError covers a socket timeout that the
        # library's retry loop ran out of patience on.
        logger.info("Tag fix: MusicBrainz lookup for %s failed: %s", path, exc)
        return TagFixResult(note=NOTE_UNAVAILABLE)
    except Exception as exc:
        # A malformed response makes the library raise anything from a KeyError
        # to a ValueError.  A finished download is not worth a traceback.
        logger.warning("Tag fix: unexpected MusicBrainz failure for %s: %r", path, exc)
        return TagFixResult(note=NOTE_UNAVAILABLE)

    match = pick_match(candidates, cleaned, folder_artist, duration)
    if match is None:
        logger.info(
            "Tag fix: no MusicBrainz match for %r by %r (%.0fs)",
            cleaned,
            folder_artist,
            duration,
        )
        return TagFixResult(note=NOTE_NO_MATCH)

    if should_cancel is not None and should_cancel():
        return TagFixResult(matched=True, note=NOTE_CANCELLED)

    try:
        changed = apply_fix(path, match)
    except Exception as exc:
        logger.warning("Tag fix: could not write %s: %r", path, exc)
        return TagFixResult(matched=True, note=NOTE_WRITE_FAILED)

    if changed:
        logger.info(
            "Tag fix: %s is now %r by %r", path.name, match.title, match.artist
        )
    return TagFixResult(matched=True, changed=changed)


def _first_tag(audio: FLAC, key: str) -> str | None:
    """Return the first value of *key*, or ``None`` when it is absent/empty."""
    values = audio.get(key) if audio.tags is not None else None
    if not values:
        return None
    value = str(values[0]).strip()
    return value or None
