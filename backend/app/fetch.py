"""One bounded HTTP GET, for the two places that read a public web page.

:mod:`app.spotify` reads an artist's name off Spotify, and :mod:`app.bandcamp`
reads a seller's display name off a Bandcamp page.  Neither is a *source* --
yt-dlp does the downloading -- and both want the same narrow thing: a single
GET, against a URL this app built rather than one a user pasted, with a
timeout, a cap on how much is read, no redirect to chase, and a failure that
is an answer rather than an exception escaping to the route.  That is all this
module does.

The body is read with ``response.raw.read1`` in a loop rather than with
``requests``' ``iter_content``.  The read timeout is per *socket read*, not per
response: a server that dribbles a byte at a time resets it on every read, so a
single 8 KiB ``iter_content`` chunk can take hours to assemble and no deadline
check between chunks would ever run.  Reading one raw read at a time is what
lets the caller's deadline be checked after each returned chunk (at most a few
socket reads on a compressed body), so the overrun is normally bounded by a
small multiple of one read timeout rather than by the whole body.  The check
only runs *between* ``read1`` returns, though, and urllib3's ``read1`` with
``decode_content=True`` loops internally until the decoder yields bytes: a
pathological content-encoding that decodes to nothing hands back no chunk to
check between.  Accepted, because every caller pins its host and none follows a
redirect -- the only server that can do it is the one being read.
"""

import logging
from email.message import Message
from collections.abc import Callable

import requests
import urllib3

logger = logging.getLogger(__name__)

# How long one request may take: (connect, read).  These run inside a probe
# that has its own deadline, so they are short.
TIMEOUT = (5, 10)

# How much of a response is read before the rest is thrown away.  The bodies
# these callers want are a sub-kilobyte JSON answer or a page whose head is
# what matters, so this only ever truncates a tail -- but it is what stops a
# hostile or broken response from being read into memory without bound.
MAX_RESPONSE_BYTES = 256 * 1024

# Sent because both sites serve a different (and sometimes no) page to a client
# that does not look like a browser.  Honest about what it is: this is a
# self-hosted app reading a public page, not a crawler pretending otherwise.
USER_AGENT = (
    "Mozilla/5.0 (compatible; music-for-arr/1.0; +https://github.com/) "
    "python-requests"
)


class FetchFailed(Exception):
    """The request never got an answer at all.

    Distinct from :func:`get_text` returning None, which means the server
    answered and the answer was not one we can use (a non-200).  Callers turn
    the two into different things -- only this one is worth retrying.
    """


def _charset(content_type: str | None) -> str | None:
    """The charset a Content-Type explicitly declares, or None.

    Not ``response.encoding``: ``requests`` answers ISO-8859-1 for any
    ``text/*`` that declares nothing, which would mangle a UTF-8 name into
    ``Ã\x81rtist``.  An undeclared body is utf-8 here -- what these hosts send,
    and what JSON is by spec.
    """
    if not content_type:
        return None
    message = Message()
    message["Content-Type"] = content_type
    charset = message.get_param("charset")
    return charset if isinstance(charset, str) else None


def get_text(
    session: requests.Session,
    url: str,
    params: dict[str, str] | None = None,
    *,
    label: str,
    check_deadline: Callable[[], None] | None = None,
) -> str | None:
    """At most :data:`MAX_RESPONSE_BYTES` of *url*'s body, or None.

    *label* is the site's name, used only in the log line for a non-200.

    ``allow_redirects=False`` -- see the module docstring: every caller builds
    a canonical URL that answers 200 directly, so a 3xx here is something this
    code should not be following, and it is reported as "no answer" rather than
    chased.  The body is read one socket read at a time and cut at the cap
    instead of being read whole, so a response that never ends cannot become
    memory *or* time.

    Whatever *check_deadline* raises is left alone on the way out.  A probe's
    ``ProbeTimeout`` is neither a ``urllib3`` error nor an ``OSError``, so the
    ``except`` below does not catch it and it reaches the probe as the deadline
    answer it is, rather than being reported as the site being unreachable.  A
    *check_deadline* that raised an ``OSError`` subclass would be swallowed as
    the site being unreachable; nothing in this tree raises one.

    Raises:
        FetchFailed: The request failed at the transport level.
    """
    try:
        with session.get(
            url,
            params=params,
            timeout=TIMEOUT,
            allow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            stream=True,
        ) as response:
            if response.status_code != 200:
                logger.info("%s answered %s for %s", label, response.status_code, url)
                return None
            body = b""
            while len(body) < MAX_RESPONSE_BYTES:
                # ``read1`` (urllib3 2.x -- see the floor in
                # ``requirements.txt``; 1.26 has no ``read1`` at all) returns
                # whatever it has rather than waiting to fill a buffer, so the
                # deadline below is checked after each returned chunk (at most
                # a few socket reads on a compressed body): the overrun is
                # normally bounded by a small multiple of one read timeout
                # rather than by the whole body.  Only *between* returns,
                # though -- with ``decode_content=True`` this loops inside
                # urllib3 until the decoder yields bytes, so a content-encoding
                # that decodes to nothing never comes back here to be checked.
                # Accepted: the host is pinned and nothing redirects.
                chunk = response.raw.read1(8192, decode_content=True)
                if not chunk:
                    break
                body += chunk
                if check_deadline is not None:
                    check_deadline()
            body = body[:MAX_RESPONSE_BYTES]
            # Read inside the ``with``: the connection is released on the way
            # out and nothing about the response should be touched after that.
            encoding = _charset(response.headers.get("Content-Type")) or "utf-8"
    # ``requests`` only translates urllib3's errors into its own inside
    # ``iter_content``/``.content``; reading ``raw`` ourselves means a
    # ``ReadTimeoutError``, a ``ProtocolError`` on a truncated body or a
    # ``DecodeError`` on corrupt gzip arrives raw and would escape to the route
    # as a 500.  Both arms are needed: ``RequestException`` is an ``OSError``,
    # so that half still covers the connect-time failures, and
    # ``urllib3.exceptions.HTTPError`` is not an ``OSError`` at all.
    except (urllib3.exceptions.HTTPError, OSError) as exc:
        raise FetchFailed(f"{type(exc).__name__}: {exc}") from exc
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:  # a header naming an unknown charset
        return body.decode("utf-8", errors="replace")
