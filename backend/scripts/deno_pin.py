"""Print the deno version yt-dlp pins in its ``pin-deno`` extra.

Run as ``python3 scripts/deno_pin.py`` against an environment that has
yt-dlp installed (CI installs ``backend/requirements.txt`` for exactly this).

The image installs deno itself, at the version ``backend/Dockerfile`` pins in
``ARG DENO_VERSION``.  That has to be the version yt-dlp pins, or the runtime
its YouTube extractor gets is not the one it was tested against.  The extra is
declaration-only -- nothing installs it -- so the pin is only readable out of
the installed distribution's metadata, which is what this does.

Exits non-zero with a message naming what it did find when the pin is not
there in exactly one row, which is what a yt-dlp release that renames or drops
the extra looks like.
"""

import importlib.metadata
import re
import sys

# `deno==2.9.5; extra == "pin-deno"`, with the spacing left free because the
# metadata is generated and has changed shape before.
_PIN = re.compile(r"\s*deno\s*==\s*([0-9][0-9.]*)")

_EXTRA = "pin-deno"


def deno_pin() -> str:
    requires = importlib.metadata.metadata("yt-dlp").get_all("Requires-Dist") or []
    rows = [row for row in requires if _EXTRA in row.lower()]
    pins = [match.group(1) for row in rows if (match := _PIN.match(row))]
    if len(pins) != 1:
        denoish = [row for row in requires if "deno" in row.lower()]
        raise SystemExit(
            f"deno_pin: expected exactly one deno pin in yt-dlp's {_EXTRA!r} "
            f"extra, found {len(pins)}. deno-ish Requires-Dist rows: "
            f"{denoish or 'none'}"
        )
    return pins[0]


if __name__ == "__main__":
    print(deno_pin())
    sys.exit(0)
