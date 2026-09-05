"""SQLite persistence for the job queue.

The queue itself stays in memory (``QueueManager`` owns the dispatcher and one
asyncio task per job); this module is the write-through mirror that lets the
queue survive a restart.  Every state transition is written here *before* its
SSE event is emitted, so the table is never behind what a client has seen.

Design notes:

* **stdlib ``sqlite3``, no new dependency.**  One connection, guarded by a
  ``threading.Lock`` and opened with ``check_same_thread=False`` because yt-dlp's
  progress and postprocessor hooks run on executor threads and can trigger a
  status change from there.  Every statement goes through :meth:`JobStore._execute`,
  so the whole module could be moved behind ``asyncio.to_thread`` later if the
  (currently tiny) blocking writes ever show up in SSE latency.
* **WAL** so a reader never blocks the writer, and ``foreign_keys=ON`` so the
  ``parent_id`` self-reference actually cascades when a bulk parent is deleted.
* **Transactions**: the connection is opened with ``autocommit=True`` -- the
  Python 3.12+ spelling of the old ``isolation_level=None`` -- because every
  normal write is a single statement and a long-lived open transaction would
  pin the WAL snapshot.  The multi-statement migration wraps itself in an
  explicit ``BEGIN``/``COMMIT``.
* **Schema versioning** is ``PRAGMA user_version`` against a numbered list of
  migrations applied at open.  No Alembic.

Timestamps are stored as ISO-8601 UTC strings.  Progress is deliberately *not*
stored: after a restart an interrupted job re-runs from zero anyway.
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from app.models import Job, JobKind, JobStatus

logger = logging.getLogger(__name__)

DEFAULT_DATA_PATH = "/config"
DB_FILENAME = "queue.db"

# Statuses a job never comes back from and that the retention sweep may drop.
# ``error`` is deliberately absent: an errored job stays until it is dismissed.
TERMINAL_STATUSES: tuple[JobStatus, ...] = (JobStatus.DONE, JobStatus.CANCELLED)

# How many ids one ``IN (...)`` may carry.  SQLite's default limit is 999 bound
# parameters per statement; 900 leaves room for the other placeholders.
_MAX_IN_PARAMS = 900

# Columns of the ``jobs`` table, in the order the row tuple carries them.
_COLUMNS: tuple[str, ...] = (
    "id",
    "kind",
    "parent_id",
    "status",
    "url",
    "title",
    "thumbnail_url",
    "duration",
    "artist",
    "album",
    "path",
    "target_dir",
    "target_guessed",
    "result_path",
    "error",
    "detail",
    "attempts",
    "restart_attempts",
    "cancel_requested",
    "created_at",
    "updated_at",
    "finished_at",
)

# The three SQL fragments every upsert is built from.  Joined once at import
# rather than per call: a bulk submission writes 2000 rows in one go, and
# rebuilding the same three strings 2000 times on the event loop is pure waste.
_COLUMN_LIST = ", ".join(_COLUMNS)
_PLACEHOLDERS = ", ".join("?" for _ in _COLUMNS)
_UPDATE_ASSIGNMENTS = ", ".join(
    f"{column} = excluded.{column}"
    for column in _COLUMNS
    if column not in ("id", "created_at")
)
_UPSERT_SQL = (
    f"INSERT INTO jobs ({_COLUMN_LIST}) VALUES ({_PLACEHOLDERS}) "
    f"ON CONFLICT(id) DO UPDATE SET {_UPDATE_ASSIGNMENTS}"
)

# Each entry is one schema version: the statements that take the database from
# version N-1 to version N.  Never edit an entry that has shipped; append a new
# one instead.  Migration 1 is Phase 1's schema and has not shipped yet, so it
# is still being edited in place -- Phase 2 added `cancel_requested` and
# Phase 6 added `target_dir` and `target_guessed` to it rather than appending a
# migration; from the
# first release onwards it is frozen.  A file written by an earlier
# pre-release build therefore claims to be at version 1 while missing those
# columns, which `_migrate` catches at open rather than letting it surface as
# an IndexError three calls later.  The full column set is created in migration 1 even though later
# phases (bulk parents, tagging jobs) are what fill most of it in -- adding
# columns later would mean a migration per phase for no benefit.
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE jobs (
            id            TEXT PRIMARY KEY,
            kind          TEXT NOT NULL CHECK(kind IN ('download', 'bulk', 'tagging')),
            parent_id     TEXT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            status        TEXT NOT NULL,
            url           TEXT,
            title         TEXT,
            thumbnail_url TEXT,
            duration      REAL,
            artist        TEXT,
            album         TEXT,
            path          TEXT,
            target_dir    TEXT,
            target_guessed INTEGER NOT NULL DEFAULT 0,
            result_path   TEXT,
            error         TEXT,
            attempts      INTEGER NOT NULL DEFAULT 0,
            restart_attempts INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            finished_at   TEXT NULL
        )
        """,
        # load_active() filters on status; the retention sweep filters on
        # status plus a timestamp; children are looked up by parent.
        "CREATE INDEX idx_jobs_status ON jobs(status)",
        "CREATE INDEX idx_jobs_parent_id ON jobs(parent_id)",
        "CREATE INDEX idx_jobs_created_at ON jobs(created_at)",
    ),
    # Version 2 (phase 8): the tag fix that runs after every download needs
    # somewhere to say "tags not fixed: no match" on a job that is otherwise
    # `done`.  Reusing `error` was the alternative -- no new column -- but an
    # error string on a successful row is exactly the thing every reader of
    # this table (the queue view, the frontend's "Skipped" rendering, a later
    # report) would have to learn an exception for.  A nullable column of its
    # own costs one ALTER and keeps "error means the job failed" true.
    (
        "ALTER TABLE jobs ADD COLUMN detail TEXT",
    ),
)

SCHEMA_VERSION = len(_MIGRATIONS)


class JobStoreError(Exception):
    """Raised when the job database cannot be opened or migrated."""


def get_data_path() -> str:
    """Return the configured ``DATA_PATH``, falling back to the default.

    docker compose substitutes an unset variable with an empty string, so
    ``""`` counts as unset rather than as the current directory.
    """
    return os.environ.get("DATA_PATH") or DEFAULT_DATA_PATH


def get_db_path(data_path: str | None = None) -> Path:
    """Return the path of ``queue.db`` inside *data_path* (or ``DATA_PATH``)."""
    return Path(data_path or get_data_path()) / DB_FILENAME


def _to_iso(value: datetime | None) -> str | None:
    """Serialise a datetime as an ISO-8601 UTC string."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _from_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string written by :func:`_to_iso`."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class JobStore:
    """Write-through SQLite store for :class:`~app.models.Job` rows.

    Args:
        db_path: Path of the database file.  Its parent directory must already
            exist; the app validates ``DATA_PATH`` at startup so a missing or
            read-only directory fails fast there rather than here.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        # Set by close(); writes after it are dropped rather than raising
        # sqlite3.ProgrammingError from whatever thread got there last.
        self._closed = False
        try:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                autocommit=True,
            )
        except sqlite3.Error as exc:
            raise JobStoreError(f"Cannot open job database {self._db_path}: {exc}") from exc

        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        except sqlite3.Error as exc:
            self._conn.close()
            raise JobStoreError(f"Cannot initialise job database {self._db_path}: {exc}") from exc
        except BaseException:
            # Not every failure in here is a sqlite3.Error: _migrate raises
            # JobStoreError itself on a downgrade.  A constructor that raises
            # leaves nobody holding the store, so the connection it opened has
            # to be closed on the way out whatever the reason was.
            self._conn.close()
            raise

        logger.info(
            "Job store open at %s (schema version %s)", self._db_path, SCHEMA_VERSION
        )

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        """Apply every migration the database has not seen yet."""
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise JobStoreError(
                f"Job database {self._db_path} is at schema version {version}, "
                f"newer than this build understands ({SCHEMA_VERSION}). "
                "Downgrading is not supported."
            )
        for target in range(version + 1, SCHEMA_VERSION + 1):
            logger.info("Migrating job database to schema version %s", target)
            self._conn.execute("BEGIN")
            try:
                for statement in _MIGRATIONS[target - 1]:
                    self._conn.execute(statement)
                # PRAGMA user_version does not accept a bound parameter, and
                # `target` is a loop index over our own list, never user input.
                self._conn.execute(f"PRAGMA user_version = {target}")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

        self._check_columns()

    def _check_columns(self) -> None:
        """Refuse a database whose ``jobs`` table is missing a column.

        Migration 1 is still edited in place while the app is unreleased, so a
        ``queue.db`` written by an earlier pre-release build opens cleanly --
        ``user_version`` is 1, and no migration has anything to add -- and then
        fails deep inside :meth:`load_active` with an ``IndexError`` on the
        missing key, or inside :meth:`upsert` with an ``OperationalError``,
        crashing the boot with a bare traceback nobody can act on.  Naming the
        columns and the fix here turns that into one clear sentence.
        """
        present = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")
        }
        missing = [column for column in _COLUMNS if column not in present]
        if missing:
            raise JobStoreError(
                f"Job database {self._db_path} is missing the column(s) "
                f"{', '.join(missing)}.  It was written by an earlier "
                "pre-release build of this app, whose schema is not "
                "upgradeable.  Delete the database and start again "
                "(docker volume rm music-for-arr-data), then restart."
            )

    @property
    def schema_version(self) -> int:
        """Return the ``user_version`` currently recorded in the database.

        After :meth:`close` the connection cannot be read, so the version this
        build migrated the file to is reported instead of raising: a diagnostic
        must not be the thing that brings a shutting-down process down.
        """
        with self._lock:
            if self._closed:
                logger.debug("Job store closed, reporting the built-in schema version")
                return SCHEMA_VERSION
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    # ------------------------------------------------------------------
    # Row <-> Job
    # ------------------------------------------------------------------

    @staticmethod
    def _to_row(job: Job) -> tuple:
        """Flatten a Job into the column order of :data:`_COLUMNS`."""
        return (
            job.id,
            job.kind.value,
            job.parent_id,
            job.status.value,
            job.url,
            job.title,
            job.thumbnail_url,
            job.duration,
            job.artist,
            job.album,
            job.path,
            job.target_dir,
            int(job.target_guessed),
            job.result_path,
            job.error,
            job.detail,
            job.attempts,
            job.restart_attempts,
            int(job.cancel_requested),
            _to_iso(job.created_at),
            _to_iso(job.updated_at),
            _to_iso(job.finished_at),
        )

    @staticmethod
    def _to_job(row: sqlite3.Row) -> Job:
        """Rebuild a Job from a database row.

        ``progress`` is not a column: an interrupted job re-runs from zero, and
        a restored terminal job has no progress worth showing.
        """
        return Job(
            id=row["id"],
            kind=JobKind(row["kind"]),
            parent_id=row["parent_id"],
            status=JobStatus(row["status"]),
            url=row["url"],
            title=row["title"],
            thumbnail_url=row["thumbnail_url"],
            duration=row["duration"],
            artist=row["artist"],
            album=row["album"],
            path=row["path"],
            target_dir=row["target_dir"],
            target_guessed=bool(row["target_guessed"]),
            result_path=row["result_path"],
            error=row["error"],
            detail=row["detail"],
            attempts=row["attempts"],
            restart_attempts=row["restart_attempts"],
            cancel_requested=bool(row["cancel_requested"]),
            created_at=_from_iso(row["created_at"]),
            updated_at=_from_iso(row["updated_at"]),
            finished_at=_from_iso(row["finished_at"]),
        )

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def upsert(self, job: Job) -> None:
        """Insert *job*, or update the stored row if its id already exists.

        Deliberately ``ON CONFLICT ... DO UPDATE`` rather than
        ``INSERT OR REPLACE``: with ``foreign_keys=ON`` a REPLACE *deletes* the
        old row first, which fires ``ON DELETE CASCADE`` and would wipe a bulk
        parent's children every time the parent's status changed.

        One statement, so the connection's autocommit mode makes it durable as
        soon as it returns -- which is what "written before the event is
        emitted" relies on.

        ``created_at`` is insert-only, like ``id``: restore dispatches jobs in
        ``created_at`` order, so an update must not be able to move a job to the
        back of the queue.
        """
        with self._lock:
            if self._closed:
                # Shutdown races an executor thread's last write; there is
                # nothing to persist to any more and nothing to report.
                logger.debug("Job store closed, dropping write for job %s", job.id)
                return
            self._conn.execute(_UPSERT_SQL, self._to_row(job))

    def upsert_many(self, jobs: Sequence[Job]) -> None:
        """Insert or update many jobs in one transaction.

        The batch counterpart of :meth:`upsert`, for the one caller that writes
        a whole set of rows at once: a bulk submission of 2000 children.  Doing
        that as 2000 autocommit statements is 2000 fsyncs on the event loop;
        one explicit transaction with ``executemany`` is a single commit, and
        the rows are durable together -- there is no moment where half a bulk's
        children exist.

        Rows already written by a foreign key (a bulk parent) must be committed
        *before* this call, since the children reference it.
        """
        if not jobs:
            return
        rows = [self._to_row(job) for job in jobs]
        with self._lock:
            if self._closed:
                logger.debug("Job store closed, dropping %d write(s)", len(rows))
                return
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(_UPSERT_SQL, rows)
                self._conn.execute("COMMIT")
            except BaseException:
                # Anything at all, not only ``sqlite3.Error``: a
                # ``KeyboardInterrupt`` or a ``sqlite3.Warning`` that left the
                # transaction open would make every later write on this
                # connection part of a batch nobody is going to commit.  The
                # rollback itself is best-effort -- if even that fails there is
                # nothing left to do but say so and let the original error out.
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    logger.exception("Rollback of a bulk job write failed")
                raise

    def delete(self, job_id: str) -> bool:
        """Delete one job row (and, via ``ON DELETE CASCADE``, its children).

        Returns ``True`` if a row was actually removed.
        """
        with self._lock:
            if self._closed:
                logger.debug("Job store closed, dropping delete of job %s", job_id)
                return False
            cursor = self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return cursor.rowcount > 0

    def load_active(self) -> list[Job]:
        """Return every non-terminal row plus errored rows, oldest first.

        "Active" here means everything the queue still has to show or act on:
        ``queued``, ``downloading``, ``converting``, ``tagging`` and ``error``.
        ``done`` and ``cancelled`` rows are left to the retention sweep.
        """
        terminal = [status.value for status in TERMINAL_STATUSES]
        placeholders = ", ".join("?" for _ in terminal)
        with self._lock:
            if self._closed:
                logger.debug("Job store closed, returning no active jobs")
                return []
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE status NOT IN ({placeholders}) "
                "ORDER BY created_at ASC",
                terminal,
            ).fetchall()
        return [self._to_job(row) for row in rows]

    def get(self, job_id: str) -> Job | None:
        """Return one job by id, or ``None``.  Used by tests and diagnostics."""
        with self._lock:
            if self._closed:
                logger.debug("Job store closed, cannot read job %s", job_id)
                return None
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._to_job(row) if row is not None else None

    def load_children_of(self, parent_ids: Sequence[str]) -> list[Job]:
        """Every child row of the given parents, oldest first.

        The complement of :meth:`load_active`, which deliberately skips
        ``done``/``cancelled`` rows: a bulk parent's finished children are
        exactly what its "N of M" is counted from, so a restart that reloaded
        only the active ones would show a parent that had downloaded 40 of 50
        tracks as "0 of 10".

        Chunked at :data:`_MAX_IN_PARAMS` ids per statement, well inside
        SQLite's variable limit, so a restore with thousands of parents cannot
        raise ``too many SQL variables``.
        """
        ids = [job_id for job_id in parent_ids if job_id]
        if not ids:
            return []
        rows: list[sqlite3.Row] = []
        with self._lock:
            if self._closed:
                logger.debug("Job store closed, returning no children")
                return []
            for start in range(0, len(ids), _MAX_IN_PARAMS):
                chunk = ids[start : start + _MAX_IN_PARAMS]
                placeholders = ", ".join("?" for _ in chunk)
                rows.extend(
                    self._conn.execute(
                        f"SELECT * FROM jobs WHERE parent_id IN ({placeholders}) "
                        "ORDER BY created_at ASC",
                        chunk,
                    ).fetchall()
                )
        return [self._to_job(row) for row in rows]

    def prune_terminal(self, older_than: datetime) -> list[str]:
        """Delete ``done``/``cancelled`` rows that finished before *older_than*.

        Returns the ids removed so the caller can drop the same jobs from its
        in-memory mirror.  ``finished_at`` should always be set on a terminal
        row; ``updated_at`` is the fallback for rows written by an older build.

        Only top-level rows are considered (``parent_id IS NULL``).  A child
        leaves only with its parent, through the row's ``ON DELETE CASCADE``:
        a bulk parent that is still working is full of ``done`` children older
        than the cutoff, and sweeping those out from under it would silently
        rewrite its "N of M" and, once the last one went, retire a live parent
        as ``done``.
        """
        cutoff = _to_iso(older_than)
        terminal = [status.value for status in TERMINAL_STATUSES]
        placeholders = ", ".join("?" for _ in terminal)
        with self._lock:
            if self._closed:
                logger.debug("Job store closed, skipping retention sweep")
                return []
            rows = self._conn.execute(
                f"SELECT id FROM jobs WHERE status IN ({placeholders}) "
                "AND parent_id IS NULL "
                "AND COALESCE(finished_at, updated_at) < ?",
                (*terminal, cutoff),
            ).fetchall()
            removed = [row["id"] for row in rows]
            if removed:
                ids = ", ".join("?" for _ in removed)
                self._conn.execute(f"DELETE FROM jobs WHERE id IN ({ids})", removed)
        if removed:
            logger.info("Retention sweep removed %d finished job(s)", len(removed))
        return removed

    def close(self) -> None:
        """Close the connection.

        Idempotent by construction: the ``_closed`` flag makes a second call a
        no-op, and it also makes any :meth:`upsert`/:meth:`delete` that an
        executor thread issues after shutdown a silently dropped write rather
        than a ``sqlite3.ProgrammingError`` traceback per SSE event.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error as exc:  # pragma: no cover - defensive
                logger.warning("Error closing job database: %s", exc)
        logger.info("Job store at %s closed", self._db_path)
