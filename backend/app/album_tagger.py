"""The album metadata pass: a whole folder against one MusicBrainz release.

What this adds to :mod:`app.tagger`
-----------------------------------
The per-track fix answers "what is this recording called".  The album pass
asks the further question a folder makes possible: *are these tracks all the
same release?*  When they are -- every FLAC in the folder matched, and one
release is common to all of them -- the pass additionally writes
``TRACKNUMBER`` and ``DISCNUMBER`` from that release's tracklist and fetches
``cover.jpg`` from the Cover Art Archive.  When they are not, it falls back to
exactly what the per-track fix would have done for each track that matched,
writes no numbers and no cover, and reports ``partial: 9 of 12``.

That all-or-nothing rule is the metadata ticket's, and the reason for it is
that a track number is only meaningful relative to a release.  Numbering nine
of twelve tracks from a release the other three are not on produces a folder
that sorts wrongly and claims to be authoritative about it.

Finding the one release, in two goes
------------------------------------
A recording search returns a page of results per track, and MusicBrainz lists
a popular recording once per release that duplicated it.  On a heavily
duplicated album -- *Dark Side of the Moon*, *Discovery* -- no release is
mentioned by every track's page, however deep the page goes, and intersecting
them (:func:`choose_release`) answers "no common release" for an album the
data plainly supports.  What the intersection *does* find is as often as not a
compilation or a pseudo-release, which duplicate the whole album's recordings
and are therefore likelier to be on every track's page than the album is: both
goes put a release through the same free filter (:func:`_plausible_release`)
before believing it.

So when every track matched and the intersection is empty -- or everything in
it was implausible -- the pass asks the question the other way round:
:func:`rank_release_candidates` takes the releases the folder points at *at
all*, throws out the ones the search result already disqualifies (wrong
artist credit, not ``Official``, a compilation), ranks what is left by track
count then by how many tracks named it, and
:func:`_release_by_tracklist` reads up to
:data:`MAX_RELEASE_FETCHES` of those tracklists and takes the first that
supplies a bar-clearing row for every track.  That costs one to three extra
MusicBrainz requests on a folder the intersection could not resolve, and
nothing at all on one it could.

Rules this module does not get to bend
--------------------------------------
* **No MusicBrainz id is ever written.**  The release id fetches a tracklist
  and a cover and is then dropped; Navidrome groups on ``ALBUMARTIST`` +
  ``ALBUM``, which is what the folders say.
* ``ALBUM``, ``ALBUMARTIST``, ``SOURCEID``, ``SOURCEURL``, ``DATE`` and the
  embedded picture blocks are never touched.  ``TRACKNUMBER``/``DISCNUMBER``
  are in :data:`app.tagger.PRESERVED_TAGS` and stay untouchable *there*:
  :func:`apply_fix` still never writes them.  The album pass is the one caller
  the ticket allows to, and it does it here, in :func:`apply_numbers`, so that
  permission is visible rather than buried in a flag.
* An existing sidecar cover is never overwritten.  Neither is an embedded one:
  the fetch writes a file next to the tracks and leaves the pictures alone.
* Only FLAC takes part.  A non-FLAC track still counts towards the folder's
  total -- it is a track the user can see -- but it is never "fixed", which is
  what keeps an album with one stray MP3 out of the full-match path.

Structure
---------
Every function here is *blocking* and takes its external dependency as an
argument: the MusicBrainz search, the release fetch, the HTTP get.  Nothing in
this module knows about asyncio, jobs or the queue.  :func:`tag_album` is the
one coroutine, and it does no work itself -- it hands each blocking step to
the ``run`` callable its caller supplies, which is what puts every MusicBrainz
call on the queue's single tagging thread with the queue's timeout around it
(see ``QueueManager._run_tag_step``).  The pass is therefore cancellable
between steps and testable without a network, a thread or an event loop.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Sequence, TypeVar

import httpx
import musicbrainzngs
from mutagen.flac import FLAC

from app.cover_art import downscale_cover
from app.library import COVER_FILENAMES, sniff_image_mime
from app.library_ops import is_audio, walk_files
from app.tagger import (
    DURATION_TOLERANCE_SECONDS,
    MB_APP_NAME,
    MB_APP_VERSION,
    NOTE_NO_MATCH,
    NOTE_PREFIX,
    NOTE_UNAVAILABLE,
    NOTE_WRITE_FAILED,
    Candidate,
    Match,
    ReleaseRef,
    SearchCallable,
    apply_fix,
    artist_credit,
    clearing_candidates,
    configure_musicbrainz,
    musicbrainz_contact,
    normalise,
    probe_track,
    run_search,
    search_recordings,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# The file the fetched art is written as.  ``cover.jpg`` for the JPEG the
# archive all but always serves and the downscaler all but always produces,
# because the library serves that name first of the sidecar names it knows.
COVER_FILENAME = "cover.jpg"

# What to call a cover that is *not* a JPEG.  The downscaler re-encodes to
# JPEG, but it hands the original bytes back when ffmpeg is missing or fails,
# and those can be a PNG.  Keyed on what the bytes really are (never on a
# declared type), and every value is one of :data:`app.library.COVER_FILENAMES`
# -- a name the library does not know is a file the scanner would never serve.
COVER_FILENAME_BY_TYPE: dict[str, str] = {
    "image/jpeg": COVER_FILENAME,
    "image/png": "cover.png",
}

# Cover Art Archive size.  500 px is what Navidrome and every client actually
# display; the originals are routinely 3000 px scans of several megabytes, and
# fetching those to downscale them costs bandwidth for nothing.
COVER_SIZE = 500
COVER_ART_ARCHIVE_URL = "https://coverartarchive.org"

# Seconds one cover fetch may take, redirect included.  The archive answers a
# ``front`` request with a 307 to archive.org, so this covers two round trips.
COVER_TIMEOUT_SECONDS = 30.0

# Largest cover the fetch will accept, before downscaling.  A front-500 is tens
# of kilobytes; anything past this is not the image we asked for.
MAX_COVER_BYTES = 12 * 1024 * 1024

# Notes the album pass adds to the tagger's vocabulary.  They share its prefix
# so a reader (and the frontend) can recognise one without matching sentences.
NOTE_NO_TRACKS = f"{NOTE_PREFIX}: no tracks in this folder"
NOTE_NO_RELEASE = "no common release; track numbers and cover not written"
COVER_NOTE_PREFIX = "cover not fetched"


def partial_note(matched: int, total: int) -> str:
    """The ``partial: 9 of 12`` note the ticket names, built in one place."""
    return f"partial: {matched} of {total}"


class TagStepFailed(Exception):
    """A step the pass cannot go on without failed.

    Carries the note the job's ``error`` gets.  Raised by the ``run`` callable
    when a step timed out or raised, and by the pass itself when MusicBrainz
    could not be reached -- the three things the metadata ticket says end a
    *manual* tagging job in ``error`` rather than in ``done`` with a note.
    """

    def __init__(self, note: str) -> None:
        super().__init__(note)
        self.note = note


# The caller's "run this blocking step and give me its result" hook.  It is
# what owns the thread and the timeout; see the module docstring.
RunStep = Callable[[Callable[[], T]], Awaitable[T]]


@dataclass(frozen=True)
class TrackLookup:
    """What one track's MusicBrainz lookup came back with.

    ``candidates`` is empty for a track that did not clear the bar, and
    ``note`` then says why -- "no match", "not a FLAC", "MusicBrainz
    unavailable".  A matched lookup keeps *every* candidate that cleared the
    bar, in MusicBrainz's order, because its recording id is what the
    release's tracklist is keyed on and its release list is what the folder is
    reconciled into one release through -- and MusicBrainz routinely lists the
    same recording several times, so the release the folder actually is can
    hang off a later candidate than the best-scoring one.

    ``candidate`` is the first of them: what a per-track fix would have
    picked, and what the pass writes when the folder agreed on no release.

    ``cleaned_title`` and ``duration`` are what the file itself offered -- the
    left-hand side of the match bar.  They are kept because the release-first
    fallback checks a *release's* tracklist rows against the same bar, and it
    has to check them against the file rather than against a candidate that
    already cleared it.
    """

    path: Path
    candidates: tuple[Candidate, ...] = ()
    note: str | None = None
    cleaned_title: str = ""
    duration: float | None = None

    @property
    def candidate(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def matched(self) -> bool:
        return bool(self.candidates)

    @property
    def match(self) -> Match | None:
        if self.candidate is None:
            return None
        return Match(
            title=self.candidate.title, artist=self.candidate.artist_credit or ""
        )


@dataclass
class AlbumTagResult:
    """What one pass over a folder did.

    ``matched`` of ``total`` is the N of M the job reports; ``changed`` are the
    absolute paths whose bytes the pass wrote -- the caller expresses them
    relative to the library root before they go out as ``library_changed``.  ``detail`` is the note
    for a job that finished anyway -- ``partial: 9 of 12``, a cover that could
    not be fetched -- and is ``None`` when there is nothing to say.
    """

    total: int = 0
    matched: int = 0
    changed: list[Path] = field(default_factory=list)
    detail: str | None = None
    cancelled: bool = False
    # Whether the folder resolved to exactly one release and got its numbers.
    complete: bool = False
    cover_written: bool = False


# ---------------------------------------------------------------------------
# The folder
# ---------------------------------------------------------------------------


def album_tracks(folder: Path) -> list[Path]:
    """Every audio file at or below *folder*, in a stable order.

    Nested folders are included because the library scanner flattens them: an
    album with a ``Disc 2`` subfolder is one album, and a pass that ignored the
    subfolder would call a complete album partial.  Sorted by path so two runs
    over the same folder number and report the same tracks in the same order,
    whatever the filesystem hands back.
    """
    return sorted(path for path in walk_files(folder) if is_audio(path))


# ---------------------------------------------------------------------------
# One track
# ---------------------------------------------------------------------------


def lookup_track(
    path: Path,
    folder_artist: str | None,
    *,
    search: SearchCallable = search_recordings,
) -> TrackLookup:
    """Ask MusicBrainz about one track; write nothing.

    The album pass splits what :func:`app.tagger.fix_track` does into a lookup
    and a write, because the write depends on what the *other* tracks matched:
    a track number can only be written once the folder has agreed on a
    release.  The bar and the cleaning are the same function calls the
    per-track fix makes, so the two can never disagree about what "matched"
    means.
    """
    probe = probe_track(path, folder_artist)
    if not probe.usable:
        return TrackLookup(path=path, note=probe.note)

    asked = {"cleaned_title": probe.cleaned_title, "duration": probe.duration}

    candidates, note = run_search(
        search, probe.cleaned_title, folder_artist, probe.duration
    )
    if note is not None:
        return TrackLookup(path=path, note=note, **asked)

    clearing = clearing_candidates(
        candidates, probe.cleaned_title, folder_artist, probe.duration
    )
    if not clearing:
        logger.info(
            "Album pass: no MusicBrainz match for %r by %r", probe.cleaned_title, folder_artist
        )
        return TrackLookup(path=path, note=NOTE_NO_MATCH, **asked)
    return TrackLookup(path=path, candidates=tuple(clearing), **asked)


def apply_numbers(
    flac_path: Path, track_number: int | None, disc_number: int | None
) -> bool:
    """Write ``TRACKNUMBER``/``DISCNUMBER``; return whether the file changed.

    Separate from :func:`app.tagger.apply_fix` on purpose.  Those two tags are
    in ``PRESERVED_TAGS`` -- nothing the *per-track* fix does may touch them,
    because a per-track fix has no idea what release the folder is -- and the
    album pass is the single exception the metadata ticket carves out.  Keeping
    it a function of its own means the exception is one call site rather than a
    parameter every caller of ``apply_fix`` could set.

    Values are written as plain integers ("3", not "3/12"): the library parses
    both, and the total is the folder's business, not a tag's.
    """
    audio = FLAC(flac_path)
    tags = audio.tags
    if tags is None:
        audio.add_tags()
        tags = audio.tags

    changed = False
    for key, value in (("TRACKNUMBER", track_number), ("DISCNUMBER", disc_number)):
        if value is None:
            continue
        wanted = [str(value)]
        if list(tags.get(key, [])) != wanted:
            tags[key] = wanted
            changed = True

    # As in apply_fix: a save that would change nothing still rewrites the
    # file's bytes and its mtime, which invalidates the scan cache and wakes
    # the rescan hook for no reason.
    if changed:
        audio.save()
    return changed


def write_track(
    path: Path,
    match: Match,
    numbers: tuple[int | None, int | None] | None = None,
) -> bool:
    """Apply one track's fix, and its numbers when the folder earned them.

    Returns whether anything was written.  Two saves at most, and only when
    there is something to save; both halves compare before they write.

    A file that cannot be written raises :class:`TagStepFailed`: a manual
    tagging job that could not write is a job that failed, and the note it
    carries is the one the user is shown.  Tracks written before it stay
    written -- there is no way to unwrite them that is not another write.
    """
    try:
        changed = apply_fix(path, match)
        if numbers is not None:
            track_number, disc_number = numbers
            changed = apply_numbers(path, track_number, disc_number) or changed
    except Exception as exc:
        logger.warning("Album pass: could not write %s: %r", path, exc)
        raise TagStepFailed(NOTE_WRITE_FAILED) from exc
    return changed


# ---------------------------------------------------------------------------
# The release
# ---------------------------------------------------------------------------


def choose_release(
    lookups: Sequence[TrackLookup], track_count: int, folder_artist: str | None = None
) -> ReleaseRef | None:
    """The one release every matched track appears on, or ``None``.

    "Common to all" is a hard requirement, not a vote: this is what decides
    whether the folder is a release at all.  But being common to every track
    is not on its own enough to be believed: a pseudo-release, a bootleg, a
    tribute band's re-recording and a hits compilation all carry the same
    recordings as the album does, and a compilation is *more* likely to be
    common to all of them than the album is.  So the releases that clear the
    intersection are put through :func:`_plausible_release` -- the same free
    filter :func:`rank_release_candidates` applies on the fallback path, so the
    two goes can never disagree about what a release has to look like -- and
    what is left is ranked:

    * one whose track count equals the folder's own wins -- a folder of twelve
      tracks is the twelve-track album, not the ninety-track compilation that
      also contains all of them;
    * failing that, the first in MusicBrainz's own order, which is the only
      ranking a recording search gives.

    A track contributes the releases of *every* candidate that cleared the
    bar, not only its best-scoring one.  MusicBrainz lists a popular recording
    once per release that duplicated it, and the top-scoring entity for one
    track of an album is often a duplicate that sits on a single unrelated
    release; intersecting only those would fail albums the data plainly
    supports.  The pass re-points each track at the candidate the chosen
    release actually lists (see :func:`tag_album`), so a number and the tags
    written beside it always come from one recording.

    Returns ``None`` for an empty list, and for a folder whose every common
    release the filter threw out -- both leave the caller's fallback to have a
    go, which will decline the same releases for the same reasons.
    """
    matched = [lookup for lookup in lookups if lookup.matched]
    if not matched:
        return None

    common: set[str] | None = None
    for lookup in matched:
        ids = {
            release.id
            for candidate in lookup.candidates
            for release in candidate.releases
        }
        common = ids if common is None else (common & ids)
        if not common:
            return None
    if not common:
        return None

    # The first matched track's order is MusicBrainz's relevance order, and
    # every id in `common` is in it by construction.
    ordered: list[ReleaseRef] = []
    seen: set[str] = set()
    for candidate in matched[0].candidates:
        for release in candidate.releases:
            if release.id in common and release.id not in seen:
                seen.add(release.id)
                ordered.append(release)
    wanted_artist = normalise(folder_artist)
    ordered = [ref for ref in ordered if _plausible_release(ref, wanted_artist)]
    for release in ordered:
        if release.track_count == track_count:
            return release
    return ordered[0] if ordered else None


# How many releases the fallback below may fetch before it gives up.  Every
# fetch is one MusicBrainz request on the app's single 1-per-second worker, so
# this is the whole extra cost of the fallback: three requests on a folder the
# intersection could not resolve, and none at all on one it could.
MAX_RELEASE_FETCHES = 3

# The release-group secondary type that disqualifies a release outright: a
# greatest-hits collection genuinely does contain every track of the album in
# the folder, which is exactly why it must not win.
COMPILATION_TYPE = "compilation"

# The only release status a folder is allowed to be.  Bootlegs and
# pseudo-releases (MusicBrainz's placeholder for a transliterated tracklist)
# duplicate a real release's recordings without being an edition anyone owns.
OFFICIAL_STATUS = "official"


def rank_release_candidates(
    lookups: Sequence[TrackLookup], folder_artist: str | None, track_count: int
) -> list[ReleaseRef]:
    """The releases worth fetching when no single one is common to every track.

    :func:`choose_release` needs a release *every* track's search listed, and
    a search that returned eight recordings per track cannot promise that: one
    track's page of results routinely omits the album the other eleven agree
    on.  This is the softer question -- which releases does the folder point at
    *at all* -- answered without spending a request:

    * every release id any clearing candidate of any matched track mentions,
      with ``votes`` counting the distinct tracks that mentioned it;
    * then the hard filters, which cost nothing because the search result
      already carried the fields: a release credited to somebody other than the
      folder's artist is a tribute or a various-artists disc, a release that is
      not ``Official`` is a bootleg, and a ``Compilation`` contains the album
      without being it.  An *absent* credit phrase is a match, not a miss:
      MusicBrainz omits it when it equals the recording's own credit.
    * then the ranking, and the order of its terms is the load-bearing part.
      A release whose track count is the folder's own comes first, *before*
      votes: on a heavily-duplicated album the hits compilation collects more
      votes than the album does, and ranking by votes first picks it.  Votes
      break the remaining tie, then the shorter secondary-type list (a plain
      album over a soundtrack edition of it), then the id so the order is
      stable.

    The result is a shortlist to verify, not an answer: nothing here has read a
    tracklist yet, and :func:`match_tracklist` is what decides.
    """
    matched = [lookup for lookup in lookups if lookup.matched]
    if not matched:
        return []

    wanted_artist = normalise(folder_artist)
    refs: dict[str, ReleaseRef] = {}
    votes: dict[str, int] = {}
    for lookup in matched:
        seen: set[str] = set()
        for candidate in lookup.candidates:
            for release in candidate.releases:
                refs.setdefault(release.id, release)
                if release.id not in seen:
                    seen.add(release.id)
                    votes[release.id] = votes.get(release.id, 0) + 1

    kept = [ref for ref in refs.values() if _plausible_release(ref, wanted_artist)]
    kept.sort(
        key=lambda ref: (
            0 if ref.track_count == track_count else 1,
            -votes[ref.id],
            len(ref.secondary_types),
            ref.id,
        )
    )
    return kept


def _plausible_release(release: ReleaseRef, wanted_artist: str) -> bool:
    """Whether *release* could be the folder, judged on the search result alone."""
    if release.artist_credit_phrase and normalise(release.artist_credit_phrase) != wanted_artist:
        return False
    # An absent status is unknown, not disqualifying: MusicBrainz simply omits
    # the field on plenty of ordinary releases, and refusing those would throw
    # away the right album for saying nothing.  Only a status that is present
    # and says something other than "Official" -- a bootleg, a pseudo-release
    # -- rules a release out, mirroring the credit-phrase rule just above,
    # which also judges the field only when the search returned one.
    if release.status and release.status.casefold() != OFFICIAL_STATUS:
        return False
    return not any(
        one.casefold() == COMPILATION_TYPE for one in release.secondary_types
    )


def match_tracklist(
    rows: Sequence[ReleaseTrack],
    lookups: Sequence[TrackLookup],
    folder_artist: str | None,
) -> dict[Path, ReleaseTrack] | None:
    """Which row of *rows* is each lookup's track, or ``None`` if any has none.

    The bar is :func:`app.tagger.clearing_candidates`'s, applied to a release's
    own tracklist instead of to a search result: the normalised titles are
    equal, the lengths are within :data:`~app.tagger.DURATION_TOLERANCE_SECONDS`
    of each other, and the folder's artist is the row's credit phrase or one of
    the individually credited names -- the second half being what lets a folder
    called "Bonobo" accept a row credited to "Bonobo feat. Andreya Triana".

    All or nothing, for the reason the whole pass is: a release that supplies
    eleven of twelve tracks is not the album in the folder, and numbering the
    eleven from it would be a confident lie.

    Each row is used at most once, and that is why this is a *matching* rather
    than a loop that takes each track's first free row.  Two files can clear
    the bar against overlapping sets of rows -- an album with two mixes of the
    same track a few seconds apart -- and taking rows greedily in folder order
    strands the second file on a row the first had already taken, which either
    gives two files the same number or refuses a release that fits perfectly.
    So the clearing rows are collected per track and paired off by augmenting
    path (Kuhn's algorithm): every track gets a row of its own if any such
    pairing exists at all, and ``None`` means none does.

    Before the pairing runs, a track whose own search candidates name a row of
    this release is pinned to that row.  The title-and-length bar cannot tell
    two rows of the same recording apart -- a deluxe edition that repeats a
    track leaves both rows clearing every file -- so without the pin the
    pairing hands out numbers in folder order rather than by what the file
    actually is.  The pin is intersected with the track's clearing rows (an id
    that names a row the file does not clear is no evidence) and applied only
    when uncontested: if two tracks pin the same row, both stay free to take
    any clearing row, so a release that pairs off perfectly is never refused
    for the sake of the hint.
    """
    wanted_artist = normalise(folder_artist)
    if not wanted_artist:
        return None
    options: list[list[int]] = []
    for lookup in lookups:
        clearing = [
            index
            for index, row in enumerate(rows)
            if _row_clears(row, lookup, wanted_artist)
        ]
        if not clearing:
            return None
        options.append(clearing)

    by_recording: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        if row.recording_id:
            by_recording.setdefault(row.recording_id, []).append(index)
    # A track whose own search candidates name a row of this release belongs to
    # that row, not to whichever equally-clearing row the pairing reaches first:
    # a deluxe edition that repeats a track leaves two rows clearing the bar,
    # and only the recording id says which one the file is.
    claims: dict[int, list[int]] = {}
    for which, lookup in enumerate(lookups):
        ids = {c.recording_id for c in lookup.candidates if c.recording_id}
        pinned = sorted(
            index
            for recording_id in ids
            for index in by_recording.get(recording_id, ())
            if index in options[which]
        )
        if pinned:
            claims[which] = pinned
    for which, pinned in claims.items():
        # Only an uncontested claim narrows the search: two files naming the
        # same row must stay free to take different ones, or a release that
        # pairs off perfectly would be refused.
        rest = [other for other in claims if other != which]
        if not any(set(claims[other]) & set(pinned) for other in rest):
            options[which] = pinned

    owner: dict[int, int] = {}

    def pair(which: int, tried: set[int]) -> bool:
        """Give track *which* a row, moving whoever holds it if it can be."""
        for index in options[which]:
            if index in tried:
                continue
            tried.add(index)
            held_by = owner.get(index)
            if held_by is None or pair(held_by, tried):
                owner[index] = which
                return True
        return False

    for which in range(len(options)):
        if not pair(which, set()):
            return None
    return {lookups[which].path: rows[index] for index, which in owner.items()}


def _row_clears(row: ReleaseTrack, lookup: TrackLookup, wanted_artist: str) -> bool:
    """Whether one tracklist row is the file *lookup* was taken from."""
    if row.length_ms is None or lookup.duration is None:
        return False
    if abs(row.length_ms / 1000.0 - lookup.duration) > DURATION_TOLERANCE_SECONDS:
        return False
    if not lookup.cleaned_title or normalise(row.title) != normalise(lookup.cleaned_title):
        return False
    credits = [row.artist_credit, *row.artist_names]
    return any(normalise(name) == wanted_artist for name in credits)


def get_release(release_id: str) -> dict:
    """Fetch one release with its tracklist.

    ``recordings`` is the include that brings the media and their tracks back,
    which is where an exact track and disc number lives -- the search result's
    own release entries carry only the medium a matching recording sits on.
    ``artist-credits`` comes along in the *same* request and is what makes the
    rows usable as evidence: without it a track's recording carries no credit
    at all, and the release-first fallback could not check that the tracklist
    is this folder's artist rather than a tribute band's.
    """
    configure_musicbrainz()
    return musicbrainzngs.get_release_by_id(
        release_id, includes=["recordings", "artist-credits"]
    )


ReleaseFetcher = Callable[[str], dict]


@dataclass(frozen=True)
class ReleaseTrack:
    """One row of a fetched release's tracklist.

    Everything the pass can learn about a track from the release itself: what
    the release calls it, how long it is, who it credits, and where it sits.
    The fallback in :func:`tag_album` writes a track's ``TITLE``/``ARTIST``
    from the row it cleared the bar against rather than from a search
    candidate, so the tags and the number a file ends up with always come from
    the same place -- the release the folder turned out to be.
    """

    recording_id: str
    title: str
    length_ms: int | None
    artist_credit: str
    artist_names: tuple[str, ...]
    track_number: int | None
    disc_number: int | None


def release_tracks(
    release_id: str, *, fetch: ReleaseFetcher = get_release
) -> list[ReleaseTrack]:
    """Every tracklist row of *release_id*, in the order the release lists them.

    One request, whichever of the two things the caller wants out of it: the
    numbers to write, and the titles/lengths/credits the release-first
    fallback verifies a folder against.

    Raises :class:`TagStepFailed` when MusicBrainz could not be asked: a
    release the pass cannot read is a manual job that fails, not one that
    quietly writes nothing.
    """
    try:
        payload = fetch(release_id)
    except (
        musicbrainzngs.WebServiceError,
        musicbrainzngs.NetworkError,
        musicbrainzngs.ResponseError,
        OSError,
    ) as exc:
        logger.info("Album pass: could not fetch release %s: %s", release_id, exc)
        raise TagStepFailed(NOTE_UNAVAILABLE) from exc
    except Exception as exc:
        logger.warning("Album pass: unexpected failure fetching release %s: %r", release_id, exc)
        raise TagStepFailed(NOTE_UNAVAILABLE) from exc

    release = (payload or {}).get("release") or {}
    rows: list[ReleaseTrack] = []
    for medium in release.get("medium-list") or []:
        if not isinstance(medium, dict):
            continue
        disc = _as_int(medium.get("position"))
        for track in medium.get("track-list") or []:
            if not isinstance(track, dict):
                continue
            recording = track.get("recording")
            recording = recording if isinstance(recording, dict) else {}
            # The track may override the recording's title and credit (a
            # release that renames a track, a split credit on a compilation);
            # where it does not, the recording is the answer.
            phrase, names = artist_credit(track if track.get("artist-credit") else recording)
            # `position` is the track's place on its medium and is always
            # there; `number` is the printed number, which on a vinyl is "A1".
            position = _as_int(track.get("position")) or _as_int(track.get("number"))
            rows.append(
                ReleaseTrack(
                    recording_id=str(recording.get("id") or ""),
                    title=str(track.get("title") or recording.get("title") or ""),
                    length_ms=_as_int(recording.get("length"))
                    or _as_int(track.get("length")),
                    artist_credit=phrase,
                    artist_names=names,
                    track_number=position,
                    disc_number=disc,
                )
            )
    return rows


def track_numbers(
    rows: Sequence[ReleaseTrack],
) -> dict[str, tuple[int | None, int | None]]:
    """Map each recording id in *rows* to ``(track number, disc number)``.

    Keyed on the *recording* id rather than the track id because that is what
    a recording search returns and so what a matched track is known by here.

    ``setdefault``, not assignment: a release that lists the same recording on
    two media (a bonus disc reprise, a vinyl side split) would otherwise end up
    numbered from the last medium it appears on.  The first is the one the
    tracklist leads with.
    """
    numbers: dict[str, tuple[int | None, int | None]] = {}
    for row in rows:
        if not row.recording_id:
            continue
        numbers.setdefault(row.recording_id, (row.track_number, row.disc_number))
    return numbers


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Cover art
# ---------------------------------------------------------------------------


def has_sidecar_cover(folder: Path) -> bool:
    """Whether the folder already has cover art of its own on disk.

    Every name the library serves counts, not just ``cover.jpg``: writing one
    next to an existing ``folder.jpg`` would silently take over as the album's
    art, which is an overwrite in everything but the filename.

    Matched case-insensitively, on a listing rather than one ``exists()`` per
    name, because that is how :mod:`app.library` decides a folder has a cover:
    a ``Folder.jpg`` the library happily serves must count here too, on a
    case-sensitive filesystem as well as on a case-folding one.
    """
    try:
        present = {entry.name.casefold() for entry in os.scandir(folder)}
    except OSError:
        return False
    return any(name in present for name in COVER_FILENAMES)


def cover_user_agent() -> str:
    """The User-Agent the archive is asked with.

    Same identification the MusicBrainz calls carry: an unattributed client is
    what the archive's operators ask people not to be.
    """
    return f"{MB_APP_NAME}/{MB_APP_VERSION} ( {musicbrainz_contact()} )"


def fetch_cover_bytes(url: str, *, timeout: float = COVER_TIMEOUT_SECONDS) -> bytes | None:
    """GET *url*, following the archive's redirect; ``None`` when there is none.

    A 404 is the ordinary answer for "this release has no front image" and is
    not an error.  ``follow_redirects`` is on because the archive answers every
    ``front`` request with a 307 to archive.org -- these are plain GETs of
    public images, so following them costs nothing.

    The body is streamed and abandoned the moment it passes
    :data:`MAX_COVER_BYTES`, so a redirect that landed on something enormous
    costs that much memory and no more -- a declared ``Content-Length`` over
    the cap is not read at all.
    """
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": cover_user_agent()},
    ) as client:
        with client.stream("GET", url) as response:
            if response.status_code == 404:
                return None
            response.raise_for_status()
            declared = _as_int(response.headers.get("Content-Length"))
            if declared is not None and declared > MAX_COVER_BYTES:
                return None
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_COVER_BYTES:
                    return None
    if not data:
        return None
    return bytes(data)


CoverFetcher = Callable[[str], "bytes | None"]


def fetch_cover(
    release: ReleaseRef, *, fetch: CoverFetcher = fetch_cover_bytes
) -> bytes | None:
    """The release's front cover, else its release group's, else ``None``.

    The order is the ticket's: the release is the exact edition the folder is,
    and the release group is the fallback for an edition nobody has uploaded
    art for.  Raises nothing -- a cover is a nice-to-have, and the caller turns
    a ``None`` into a note on an otherwise finished job.
    """
    urls = [f"{COVER_ART_ARCHIVE_URL}/release/{release.id}/front-{COVER_SIZE}"]
    if release.release_group_id:
        urls.append(
            f"{COVER_ART_ARCHIVE_URL}/release-group/{release.release_group_id}"
            f"/front-{COVER_SIZE}"
        )
    for url in urls:
        data = fetch(url)
        if data and _looks_like_an_image(data):
            return data
    return None


def _looks_like_an_image(data: bytes) -> bool:
    """Whether *data* starts with an image's magic number.

    The archive redirects, and a redirect that ends at an error page would
    otherwise be written into the library as ``cover.jpg``.  The same sniffer
    the library serves covers by, so "an image" means one thing in this app.
    Broader than what :func:`write_cover` will *store*: a GIF is an image and
    still not a cover the scanner would ever look for.
    """
    return sniff_image_mime(data) is not None


def write_cover(folder: Path, data: bytes) -> tuple[Path | None, str | None]:
    """Write *data* into *folder*, downscaled; never overwrite.

    Returns ``(path written, note)``.  Both are ``None`` when the folder
    already had art -- a second check, because the fetch happened in between.

    The name comes from what the bytes turn out to be after downscaling, not
    from what was asked for: the downscaler re-encodes to JPEG, but hands the
    original back when ffmpeg is missing or fails, and a PNG saved as
    ``cover.jpg`` is a file whose name lies about it.  A GIF or a WebP has no
    name in :data:`app.library.COVER_FILENAMES`, so it is not written at all
    and the note says so -- a ``cover.gif`` the scanner never looks for is
    litter, not art.

    Written to a temporary file in the same directory and renamed into place,
    so a half-written cover is never visible to the scanner or to Navidrome.
    """
    if has_sidecar_cover(folder):
        return None, None
    scaled = downscale_cover(data)
    name = COVER_FILENAME_BY_TYPE.get(sniff_image_mime(scaled) or "")
    if name is None:
        logger.info(
            "Album pass: the cover for %s is not a format the library serves; "
            "not writing it",
            folder.name,
        )
        return None, f"{COVER_NOTE_PREFIX}: it is not a JPEG or a PNG"
    target = folder / name
    temporary = folder / f".{name}.tmp"
    try:
        temporary.write_bytes(scaled)
        # os.replace is atomic within a directory; the cover appears whole or
        # not at all.  It would also overwrite, which is why the existence
        # check above is the guard rather than the rename's own behaviour.
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return target, None


def store_cover(
    folder: Path, release: ReleaseRef, *, fetch: CoverFetcher = fetch_cover_bytes
) -> tuple[Path | None, str | None]:
    """Fetch and write the folder's cover; return ``(path written, note)``.

    Never raises: art is the last and least of what the pass does, and a
    failure here is a note on a job that is otherwise ``done`` -- the tags are
    already right, and telling the user their album pass failed because
    archive.org was slow would be a lie.
    """
    if has_sidecar_cover(folder):
        logger.info("Album pass: %s already has cover art, leaving it alone", folder.name)
        return None, None
    try:
        data = fetch_cover(release, fetch=fetch)
    except httpx.HTTPError as exc:
        logger.info("Album pass: could not fetch cover art for %s: %s", folder.name, exc)
        return None, f"{COVER_NOTE_PREFIX}: the Cover Art Archive could not be reached"
    except Exception as exc:
        logger.warning("Album pass: unexpected cover art failure for %s: %r", folder.name, exc)
        return None, f"{COVER_NOTE_PREFIX}: unexpected error"

    if not data:
        return None, f"{COVER_NOTE_PREFIX}: no front image for this release"

    try:
        return write_cover(folder, data)
    except OSError as exc:
        logger.warning("Album pass: could not write cover art for %s: %s", folder.name, exc)
        return None, f"{COVER_NOTE_PREFIX}: it could not be written"


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


async def _release_by_tracklist(
    matched: Sequence[TrackLookup],
    folder_artist: str | None,
    total: int,
    *,
    run: RunStep,
    fetch_release: ReleaseFetcher,
    stop: Callable[[], bool],
    skip: set[str],
) -> tuple[ReleaseRef | None, dict[Path, ReleaseTrack]]:
    """Verify the shortlist from :func:`rank_release_candidates` by fetching it.

    The first release whose tracklist supplies a bar-clearing row for *every*
    track wins, and the mapping it won with comes back with it -- so the number
    a file gets and the title and artist written beside it come from one row of
    one release.

    At most :data:`MAX_RELEASE_FETCHES` releases are read, which is the whole
    price of this path: three MusicBrainz requests on a folder that would
    otherwise have been reported "no common release", and none at all on one
    :func:`choose_release` already resolved.  Giving up returns ``(None, {})``
    and leaves that outcome exactly as it was.

    A fetch that fails is one shortlisted release the pass could not read, not
    a failed job: this whole path is a rescue attempt for a folder that was
    already going to be reported "no common release", and letting a timeout on
    the second of three speculative requests throw away the tags every track
    was about to get would make the folder worse for having tried.  The
    primary path's fetch still fails hard -- there the release *is* the answer.
    """
    shortlist = [
        ref
        for ref in rank_release_candidates(matched, folder_artist, total)
        if ref.id not in skip
    ][:MAX_RELEASE_FETCHES]
    for ref in shortlist:
        if stop():
            return None, {}
        try:
            rows = await run(
                lambda chosen=ref: release_tracks(chosen.id, fetch=fetch_release)
            )
        except TagStepFailed as exc:
            logger.warning(
                "Album pass: could not read the tracklist of %s (%r): %s; "
                "trying the next candidate",
                ref.id,
                ref.title,
                exc.note,
            )
            continue
        assignment = match_tracklist(rows, matched, folder_artist)
        if assignment is not None:
            logger.info(
                "Album pass: release %s (%r) supplies every track; numbering from it",
                ref.id,
                ref.title,
            )
            return ref, assignment
    return None, {}


async def tag_album(
    folder: Path,
    folder_artist: str | None,
    *,
    run: RunStep,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    search: SearchCallable = search_recordings,
    fetch_release: ReleaseFetcher = get_release,
    fetch_cover_art: CoverFetcher = fetch_cover_bytes,
) -> AlbumTagResult:
    """Look every track in *folder* up, then write what the folder agreed on.

    The two phases are deliberate.  Every lookup happens first, because the
    decision each write depends on -- one release, or none -- cannot be made
    until the last track has answered; then the writes go out in one sweep.
    That also puts the slow part (one network round trip per track, serialised
    by the queue's tagging lock) in the phase progress is reported from, so
    "7 of 12" means seven tracks asked about rather than seven files touched.

    Cancellation is checked before every lookup and before every write, which
    is what the ticket's "already-written tracks stay tagged and the rest are
    untouched" means in practice.

    Raises :class:`TagStepFailed` when MusicBrainz could not be reached or a
    file could not be written -- the endings a *manual* tagging job reports as
    an error.  Everything else comes back in the result.
    """
    stop = should_cancel or (lambda: False)

    tracks = await run(lambda: album_tracks(folder))
    total = len(tracks)
    if on_progress is not None:
        on_progress(0, total)
    if total == 0:
        return AlbumTagResult(detail=NOTE_NO_TRACKS)

    lookups: list[TrackLookup] = []
    for index, path in enumerate(tracks, start=1):
        if stop():
            return AlbumTagResult(total=total, cancelled=True)
        lookup = await run(
            lambda path=path: lookup_track(path, folder_artist, search=search)
        )
        if lookup.note == NOTE_UNAVAILABLE:
            # MusicBrainz being unreachable is not "this track has no match":
            # carrying on would file a wrong "partial" verdict and leave the
            # user with no reason to run the pass again.
            raise TagStepFailed(NOTE_UNAVAILABLE)
        lookups.append(lookup)
        if on_progress is not None:
            on_progress(index, total)

    matched = [lookup for lookup in lookups if lookup.matched]
    result = AlbumTagResult(total=total, matched=len(matched))
    if stop():
        # Checked before the release fetch, not only before each write: a
        # cancelled pass must not spend one more MusicBrainz request on a
        # verdict nobody is going to read.
        result.cancelled = True
        return result

    numbers: dict[str, tuple[int | None, int | None]] = {}
    release: ReleaseRef | None = None
    # Which of each track's clearing candidates the chosen release lists.  A
    # track can clear the bar several times over (MusicBrainz duplicates
    # recordings across releases), and only one of those entities is on the
    # release the folder turned out to be -- that is the one whose number the
    # track gets and whose title and artist are written beside it.
    chosen_candidate: dict[Path, Candidate] = {}
    # Which tracklist row of the chosen release each track is, when the pass
    # got its answer from a tracklist rather than from the search results --
    # the release-first fallback, or the primary path rescuing a release whose
    # recording ids the search did not line up with.  A row carries the title,
    # the credit and the number together, so on that path it is the row -- not
    # a search candidate -- that a file is written from.
    chosen_row: dict[Path, ReleaseTrack] = {}
    # Release ids the pass has already fetched and rejected, so the fallback
    # never spends a second request on the same tracklist.
    fetched: set[str] = set()
    whole_folder = bool(matched) and len(matched) == total
    if whole_folder:
        release = choose_release(matched, total, folder_artist)
    if release is not None:
        fetched.add(release.id)
        # One request, both answers: the numbers to write, and the rows the
        # containment check below falls back on.
        rows = await run(
            lambda chosen=release: release_tracks(chosen.id, fetch=fetch_release)
        )
        numbers = track_numbers(rows)
        for lookup in matched:
            on_release = next(
                (
                    candidate
                    for candidate in lookup.candidates
                    if candidate.recording_id in numbers
                ),
                None,
            )
            if on_release is None:
                # The release we picked does not list every recording we
                # matched.  That is often only MusicBrainz duplicating a
                # recording -- the file is on this release under an id the
                # search did not return -- so before giving the release up, ask
                # its tracklist directly, which costs no further request.
                assignment = match_tracklist(rows, matched, folder_artist)
                if assignment is not None:
                    logger.info(
                        "Album pass: release %s lists a different recording of "
                        "one track; numbering from its tracklist instead",
                        release.id,
                    )
                    chosen_row = assignment
                    chosen_candidate = {}
                    # ``numbers`` is left as it is: ``match_tracklist`` is
                    # all-or-nothing, so every matched path is in
                    # ``chosen_row`` and the write loop never reaches the
                    # ``numbers`` branch.
                    break
                logger.info(
                    "Album pass: release %s does not contain every matched "
                    "recording; writing no numbers",
                    release.id,
                )
                release = None
                numbers = {}
                chosen_candidate = {}
                break
            chosen_candidate[lookup.path] = on_release

    if release is None and whole_folder:
        # Every track matched, but no one release was on every track's search
        # results -- the ordinary shape of a heavily-duplicated album, where a
        # page of eight recordings per track cannot hold the album they share.
        # Ask the other way round: rank the releases the folder points at and
        # check their actual tracklists, up to MAX_RELEASE_FETCHES of them.
        release, chosen_row = await _release_by_tracklist(
            matched,
            folder_artist,
            total,
            run=run,
            fetch_release=fetch_release,
            stop=stop,
            skip=fetched,
        )

    result.complete = release is not None
    for lookup in matched:
        if stop():
            result.cancelled = True
            break
        row = chosen_row.get(lookup.path)
        if row is not None:
            match = Match(title=row.title, artist=row.artist_credit or "")
            numbering = (row.track_number, row.disc_number)
        else:
            candidate = chosen_candidate.get(lookup.path) or lookup.candidate
            assert candidate is not None  # `matched` means it cleared the bar
            match = Match(title=candidate.title, artist=candidate.artist_credit or "")
            numbering = (
                numbers.get(candidate.recording_id or "") if result.complete else None
            )
        changed = await run(
            lambda lookup=lookup, match=match, numbering=numbering: write_track(
                lookup.path, match, numbering
            )
        )
        if changed:
            result.changed.append(lookup.path)

    if result.cancelled:
        return result

    if result.complete:
        written, note = await run(
            lambda chosen=release: store_cover(
                folder, chosen, fetch=fetch_cover_art
            )
        )
        if written is not None:
            result.changed.append(written)
            result.cover_written = True
        result.detail = note
    elif whole_folder:
        # Everything matched, but no single release holds all of it -- not in
        # the searches, and not in the tracklists the fallback read either:
        # the tags are fixed and the numbers deliberately are not.
        result.detail = NOTE_NO_RELEASE
    else:
        result.detail = partial_note(len(matched), total)

    return result
