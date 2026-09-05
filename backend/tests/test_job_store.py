"""Tests for the SQLite job store.

Covers the schema migration, the Job <-> row round trip, the queries the
QueueManager relies on at boot (``load_active``), the retention sweep
(``prune_terminal``), and the ``parent_id`` cascade that bulk jobs will need.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.job_store import (
    SCHEMA_VERSION,
    JobStore,
    JobStoreError,
    get_data_path,
    get_db_path,
)
from app.models import Job, JobKind, JobStatus


def _make_job(**overrides) -> Job:
    """Create a Job with sensible defaults."""
    defaults = {
        "id": "job-1",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "status": JobStatus.QUEUED,
        "title": "Never Gonna Give You Up",
        "thumbnail_url": "https://img.youtube.com/thumb.jpg",
        "duration": 213.0,
    }
    defaults.update(overrides)
    return Job(**defaults)


@pytest.fixture()
def store(tmp_path):
    """A JobStore on a throwaway database file."""
    store = JobStore(tmp_path / "queue.db")
    yield store
    store.close()


# ===========================================================================
# Configuration helpers
# ===========================================================================


class TestPathHelpers:
    """DATA_PATH resolution."""

    def test_data_path_comes_from_env(self, monkeypatch):
        monkeypatch.setenv("DATA_PATH", "/somewhere")
        assert get_data_path() == "/somewhere"

    def test_empty_data_path_falls_back_to_default(self, monkeypatch):
        """docker compose turns an unset variable into an empty string."""
        monkeypatch.setenv("DATA_PATH", "")
        assert get_data_path() == "/config"

    def test_missing_data_path_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv("DATA_PATH", raising=False)
        assert get_data_path() == "/config"

    def test_db_lives_inside_data_path(self):
        assert str(get_db_path("/config")) == "/config/queue.db"


# ===========================================================================
# Schema
# ===========================================================================


class TestSchema:
    """PRAGMA user_version migrations applied at open."""

    def test_migration_creates_the_jobs_table(self, store):
        rows = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        assert "jobs" in {row["name"] for row in rows}

    def test_migration_sets_user_version(self, store):
        assert store.schema_version == SCHEMA_VERSION
        assert SCHEMA_VERSION >= 1

    def test_every_column_the_store_writes_exists(self, store):
        columns = {
            row["name"] for row in store._conn.execute("PRAGMA table_info(jobs)")
        }
        assert columns == {
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
        }

    def test_indexes_exist(self, store):
        names = {
            row["name"]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert {
            "idx_jobs_status",
            "idx_jobs_parent_id",
            "idx_jobs_created_at",
        } <= names

    def test_a_version_1_database_gains_the_detail_column(self, tmp_path):
        """The migration, not a fresh CREATE TABLE: an existing queue.db from
        before phase 8 has to keep its rows and gain the column."""
        path = tmp_path / "queue.db"
        first = JobStore(path)
        first.upsert(_make_job(id="from-before"))
        first._conn.execute("ALTER TABLE jobs DROP COLUMN detail")
        first._conn.execute("PRAGMA user_version = 1")
        first._conn.close()

        second = JobStore(path)
        try:
            assert second.schema_version == SCHEMA_VERSION
            row = second.get("from-before")
            assert row is not None and row.detail is None
            row.detail = "tags not fixed: no match"
            second.upsert(row)
            assert second.get("from-before").detail == "tags not fixed: no match"
        finally:
            second.close()

    def test_wal_mode_is_enabled(self, store):
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_foreign_keys_are_enforced(self, store):
        assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_reopening_does_not_re_run_migrations(self, tmp_path):
        path = tmp_path / "queue.db"
        first = JobStore(path)
        first.upsert(_make_job(id="survivor"))
        first.close()

        second = JobStore(path)
        try:
            assert second.schema_version == SCHEMA_VERSION
            assert second.get("survivor") is not None
        finally:
            second.close()

    def test_future_schema_version_is_refused(self, tmp_path):
        """A database written by a newer build must not be silently downgraded."""
        path = tmp_path / "queue.db"
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.close()

        with pytest.raises(JobStoreError, match="newer than this build"):
            JobStore(path)

    def test_a_pre_release_database_missing_a_column_is_refused(self, tmp_path):
        """Migration 1 is still edited in place, so an old file can lie.

        A ``queue.db`` written before ``target_dir`` joined migration 1 records
        ``user_version = 1`` and has nothing left to migrate, so it opens
        cleanly and then fails deep inside ``load_active`` with an IndexError,
        or inside ``upsert`` with an OperationalError -- a bare traceback at
        boot.  The columns and the fix belong in the message instead.
        """
        path = tmp_path / "queue.db"
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE jobs (
                id            TEXT PRIMARY KEY,
                kind          TEXT NOT NULL,
                parent_id     TEXT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                status        TEXT NOT NULL,
                url           TEXT,
                title         TEXT,
                thumbnail_url TEXT,
                duration      REAL,
                artist        TEXT,
                album         TEXT,
                path          TEXT,
                result_path   TEXT,
                error         TEXT,
                attempts      INTEGER NOT NULL DEFAULT 0,
                restart_attempts INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                finished_at   TEXT NULL
            )
            """
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        with pytest.raises(JobStoreError, match="target_dir") as caught:
            JobStore(path)
        assert "pre-release" in str(caught.value)
        assert "delete the database" in str(caught.value).lower()

    def test_a_refused_database_does_not_leak_its_connection(self, tmp_path):
        """The refusal is a JobStoreError, which no sqlite3 handler catches."""
        path = tmp_path / "queue.db"
        conn = sqlite3.connect(path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.close()

        opened: list[sqlite3.Connection] = []
        real_connect = sqlite3.connect

        def spy(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with patch("app.job_store.sqlite3.connect", side_effect=spy):
            with pytest.raises(JobStoreError, match="newer than this build"):
                JobStore(path)

        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    def test_unknown_kind_is_rejected_by_the_check_constraint(self, store):
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO jobs (id, kind, status, created_at, updated_at) "
                "VALUES ('x', 'nonsense', 'queued', 'now', 'now')"
            )


# ===========================================================================
# Round trip
# ===========================================================================


class TestRoundTrip:
    """Job -> row -> Job preserves every persisted field."""

    def test_every_field_survives(self, store):
        created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        job = _make_job(
            id="full",
            kind=JobKind.TAGGING,
            parent_id=None,
            status=JobStatus.TAGGING,
            artist="Bonobo",
            album="Black Sands",
            path="Bonobo/Black Sands",
            result_path="Bonobo/Black Sands/Kiara.flac",
            error="something",
            attempts=2,
            cancel_requested=True,
            created_at=created,
            updated_at=created,
            finished_at=created,
        )
        store.upsert(job)

        loaded = store.get("full")
        assert loaded is not None
        for field in (
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
            "result_path",
            "error",
            "detail",
            "attempts",
            "restart_attempts",
            "cancel_requested",
            "created_at",
            "updated_at",
            "finished_at",
        ):
            assert getattr(loaded, field) == getattr(job, field), field

    def test_progress_is_not_persisted(self, store):
        """An interrupted job re-runs from zero, so progress is memory-only."""
        store.upsert(_make_job(id="p", progress=42.0))
        assert store.get("p").progress == 0.0

    def test_upsert_overwrites_an_existing_row(self, store):
        store.upsert(_make_job(id="j", status=JobStatus.QUEUED))
        store.upsert(_make_job(id="j", status=JobStatus.DONE))

        assert store.get("j").status == JobStatus.DONE
        assert store._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1

    def test_upsert_does_not_move_created_at(self, store):
        """Restore dispatches in created_at order, so an update must not touch it."""
        created = datetime(2024, 1, 1, tzinfo=timezone.utc)
        store.upsert(_make_job(id="j", created_at=created))

        store.upsert(
            _make_job(
                id="j",
                status=JobStatus.DONE,
                created_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            )
        )

        assert store.get("j").created_at == created
        assert store.get("j").status == JobStatus.DONE

    def test_get_returns_none_for_a_missing_id(self, store):
        assert store.get("nope") is None

    def test_delete_removes_the_row(self, store):
        store.upsert(_make_job(id="j"))
        assert store.delete("j") is True
        assert store.get("j") is None

    def test_delete_reports_a_missing_row(self, store):
        assert store.delete("nope") is False

    def test_deleting_a_parent_cascades_to_children(self, store):
        """Dismissing a bulk parent must take its children with it."""
        store.upsert(_make_job(id="parent", kind=JobKind.BULK))
        store.upsert(_make_job(id="child", parent_id="parent"))

        store.delete("parent")

        assert store.get("child") is None

    def test_re_upserting_a_parent_keeps_its_children(self, store):
        """INSERT OR REPLACE would delete the parent row and cascade it away."""
        store.upsert(_make_job(id="parent", kind=JobKind.BULK, status=JobStatus.QUEUED))
        store.upsert(_make_job(id="child", parent_id="parent"))

        store.upsert(_make_job(id="parent", kind=JobKind.BULK, status=JobStatus.DONE))

        assert store.get("parent").status == JobStatus.DONE
        assert store.get("child") is not None


class TestWritesAfterClose:
    """Shutdown races the last write from a yt-dlp executor thread."""

    def test_upsert_after_close_is_dropped_silently(self, tmp_path):
        store = JobStore(tmp_path / "queue.db")
        store.close()

        store.upsert(_make_job(id="late"))  # must not raise

    def test_delete_after_close_is_dropped_silently(self, tmp_path):
        store = JobStore(tmp_path / "queue.db")
        store.upsert(_make_job(id="j"))
        store.close()

        assert store.delete("j") is False

    def test_the_schema_version_after_close_is_the_one_this_build_writes(
        self, tmp_path
    ):
        """A diagnostic must not be the thing that brings a shutdown down."""
        store = JobStore(tmp_path / "queue.db")
        store.close()

        assert store.schema_version == SCHEMA_VERSION

    def test_reads_after_close_return_empty_rather_than_raising(self, tmp_path):
        store = JobStore(tmp_path / "queue.db")
        store.upsert(_make_job(id="j"))
        store.close()

        assert store.load_active() == []
        assert store.get("j") is None
        assert store.prune_terminal(datetime.now(timezone.utc)) == []

    def test_close_is_idempotent(self, tmp_path):
        store = JobStore(tmp_path / "queue.db")
        store.close()
        store.close()  # must not raise


# ===========================================================================
# load_active
# ===========================================================================


class TestLoadActive:
    """What the QueueManager reloads at boot."""

    def test_returns_non_terminal_and_error_rows(self, store):
        for index, status in enumerate(
            (
                JobStatus.QUEUED,
                JobStatus.DOWNLOADING,
                JobStatus.CONVERTING,
                JobStatus.TAGGING,
                JobStatus.ERROR,
            )
        ):
            store.upsert(_make_job(id=f"job-{index}", status=status))

        assert len(store.load_active()) == 5

    def test_omits_done_and_cancelled_rows(self, store):
        store.upsert(_make_job(id="done", status=JobStatus.DONE))
        store.upsert(_make_job(id="cancelled", status=JobStatus.CANCELLED))
        store.upsert(_make_job(id="queued", status=JobStatus.QUEUED))

        assert [job.id for job in store.load_active()] == ["queued"]

    def test_orders_by_created_at(self, store):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in (2, 0, 1):
            store.upsert(
                _make_job(
                    id=f"job-{index}",
                    created_at=base + timedelta(minutes=index),
                )
            )

        assert [job.id for job in store.load_active()] == ["job-0", "job-1", "job-2"]


class TestUpsertMany:
    """The batch write a bulk submission's children go through."""

    def test_a_batch_round_trips(self, store):
        jobs = [
            _make_job(id=f"job-{index}", title=f"Track {index}")
            for index in range(5)
        ]

        store.upsert_many(jobs)

        assert [store.get(f"job-{index}").title for index in range(5)] == [
            f"Track {index}" for index in range(5)
        ]

    def test_a_batch_updates_rows_that_already_exist(self, store):
        store.upsert(_make_job(id="job-0", title="Old"))

        store.upsert_many(
            [_make_job(id="job-0", title="New"), _make_job(id="job-1", title="Also new")]
        )

        assert store.get("job-0").title == "New"
        assert store.get("job-1").title == "Also new"

    def test_an_empty_batch_is_a_no_op(self, store):
        store.upsert_many([])

        assert store.load_active() == []

    def test_a_non_sqlite_error_still_rolls_the_batch_back(self, store, tmp_path):
        """``BEGIN`` must never outlive the call that opened it.

        ``sqlite3.Warning`` is not a ``sqlite3.Error``, so a batch that raised
        one used to leave the transaction open -- and every later write on the
        connection would then sit inside a batch nobody was going to commit.
        """
        real = store._conn

        class _Failing:
            """The real connection, minus a working ``executemany``."""

            def __getattr__(self, name):
                return getattr(real, name)

            def executemany(self, *args, **kwargs):
                raise sqlite3.Warning("bad batch")

        store._conn = _Failing()
        try:
            with pytest.raises(sqlite3.Warning):
                store.upsert_many([_make_job(id="job-0"), _make_job(id="job-1")])
        finally:
            store._conn = real

        assert not store._conn.in_transaction
        assert store.get("job-0") is None

        # The connection is usable again, and a following write is durable
        # rather than trapped in the abandoned transaction.
        store.upsert(_make_job(id="job-2", title="After the failure"))
        other = sqlite3.connect(tmp_path / "queue.db")
        try:
            row = other.execute(
                "SELECT title FROM jobs WHERE id = ?", ("job-2",)
            ).fetchone()
        finally:
            other.close()
        assert row == ("After the failure",)

    def test_children_can_reference_a_parent_written_first(self, store):
        store.upsert(_make_job(id="bulk", kind=JobKind.BULK))

        store.upsert_many(
            [
                _make_job(id="child-1", parent_id="bulk"),
                _make_job(id="child-2", parent_id="bulk"),
            ]
        )

        assert store.get("child-2").parent_id == "bulk"


class TestLoadChildrenOf:
    """A restart has to get a parent's *finished* children back too."""

    def test_every_child_comes_back_whatever_its_status(self, store):
        store.upsert(_make_job(id="bulk", kind=JobKind.BULK))
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index, status in enumerate(
            (JobStatus.DONE, JobStatus.CANCELLED, JobStatus.QUEUED)
        ):
            store.upsert(
                _make_job(
                    id=f"child-{index}",
                    parent_id="bulk",
                    status=status,
                    created_at=base + timedelta(minutes=index),
                )
            )

        children = store.load_children_of(["bulk"])

        assert [job.id for job in children] == ["child-0", "child-1", "child-2"]

    def test_only_the_named_parents_children_come_back(self, store):
        for parent_id in ("bulk-a", "bulk-b"):
            store.upsert(_make_job(id=parent_id, kind=JobKind.BULK))
            store.upsert(_make_job(id=f"{parent_id}-child", parent_id=parent_id))
        store.upsert(_make_job(id="standalone"))

        assert [job.id for job in store.load_children_of(["bulk-a"])] == [
            "bulk-a-child"
        ]

    def test_no_parents_is_no_query(self, store):
        assert store.load_children_of([]) == []

    def test_more_parents_than_sqlites_variable_limit(self, store):
        """The ids are chunked, so a thousand parents is not a bad statement."""
        parents = [f"bulk-{index}" for index in range(1000)]
        for parent_id in parents:
            store.upsert(_make_job(id=parent_id, kind=JobKind.BULK))
            store.upsert(_make_job(id=f"{parent_id}-child", parent_id=parent_id))

        assert len(store.load_children_of(parents)) == 1000


# ===========================================================================
# Retention
# ===========================================================================


class TestPruneTerminal:
    """done/cancelled rows expire; error rows never do."""

    @staticmethod
    def _finished(store, job_id: str, status: JobStatus, age_days: float) -> None:
        finished = datetime.now(timezone.utc) - timedelta(days=age_days)
        store.upsert(
            _make_job(
                id=job_id, status=status, updated_at=finished, finished_at=finished
            )
        )

    def test_old_done_rows_are_removed(self, store):
        self._finished(store, "old", JobStatus.DONE, 8)

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert store.prune_terminal(cutoff) == ["old"]
        assert store.get("old") is None

    def test_recent_done_rows_are_kept(self, store):
        self._finished(store, "recent", JobStatus.DONE, 1)

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert store.prune_terminal(cutoff) == []
        assert store.get("recent") is not None

    def test_old_cancelled_rows_are_removed(self, store):
        self._finished(store, "old", JobStatus.CANCELLED, 8)

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert store.prune_terminal(cutoff) == ["old"]

    def test_old_error_rows_are_kept(self, store):
        """Errored jobs stay until the user dismisses them."""
        self._finished(store, "ancient", JobStatus.ERROR, 400)

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert store.prune_terminal(cutoff) == []
        assert store.get("ancient") is not None

    def test_in_flight_rows_are_never_swept(self, store):
        self._finished(store, "stuck", JobStatus.DOWNLOADING, 400)

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert store.prune_terminal(cutoff) == []

    def test_a_child_row_is_never_swept_on_its_own(self, store):
        """Children leave with their parent, through the row's cascade.

        A parent still working through a long collection has done children
        older than the cutoff, and taking those would rewrite its "N of M".
        """
        store.upsert(_make_job(id="bulk", kind=JobKind.BULK, status=JobStatus.ERROR))
        self._finished(store, "child-old", JobStatus.DONE, 400)
        child = store.get("child-old")
        child.parent_id = "bulk"
        store.upsert(child)

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert store.prune_terminal(cutoff) == []
        assert store.get("child-old") is not None

    def test_a_swept_parent_takes_its_children_with_it(self, store):
        self._finished(store, "bulk", JobStatus.DONE, 400)
        store.upsert(_make_job(id="child", parent_id="bulk", status=JobStatus.DONE))

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert store.prune_terminal(cutoff) == ["bulk"]
        assert store.get("child") is None

    def test_falls_back_to_updated_at_when_finished_at_is_null(self, store):
        old = datetime.now(timezone.utc) - timedelta(days=30)
        store.upsert(
            _make_job(id="legacy", status=JobStatus.DONE, updated_at=old, finished_at=None)
        )

        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        assert store.prune_terminal(cutoff) == ["legacy"]


# ===========================================================================
# Threading
# ===========================================================================


class TestThreadSafety:
    """yt-dlp's hooks run on executor threads and can trigger a write."""

    def test_writes_from_another_thread_are_allowed(self, store):
        import threading

        errors: list[Exception] = []

        def writer(index: int) -> None:
            try:
                for inner in range(20):
                    store.upsert(_make_job(id=f"t{index}-{inner}"))
            except Exception as exc:  # pragma: no cover - the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert store._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 80
