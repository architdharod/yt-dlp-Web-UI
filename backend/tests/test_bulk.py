"""Tests for bulk downloads: the parent/child job model and its two routes.

Covers the queue side (derived parent status, cancel cascade, retry and
dismiss rules, restart recovery, retention) and the API side
(``POST /download/probe``, ``POST /download/bulk``, and the nesting
``GET /queue`` now does).
"""

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.downloader import ALREADY_IN_LIBRARY_PREFIX, DownloadError
from app.file_organizer import resolve_artist_album
from app.job_store import JobStore
from app.models import MAX_REASON, Job, JobKind, JobStatus
from app.probe import (
    Enumeration,
    EnumeratedTrack,
    ProbeError,
    ProbeTimeout,
    SingleTrack,
    clear_cache,
)
from app.queue_manager import (
    BULK_RETRY_MESSAGE,
    RETENTION_DAYS,
    QueueError,
    QueueManager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


COLLECTION_URL = "https://www.youtube.com/playlist?list=PL123"


def _parent(**overrides) -> Job:
    defaults = {
        "id": "bulk-1",
        "kind": JobKind.BULK,
        "url": COLLECTION_URL,
        "title": "Chill mix",
        "artist": "Bonobo",
    }
    defaults.update(overrides)
    return Job(**defaults)


def _child(index: int, parent_id: str = "bulk-1", **overrides) -> Job:
    defaults = {
        "id": f"child-{index}",
        "kind": JobKind.DOWNLOAD,
        "parent_id": parent_id,
        "url": f"https://www.youtube.com/watch?v=vid{index}",
        "title": f"Track {index}",
        "artist": "Bonobo",
        "target_dir": "Bonobo",
    }
    defaults.update(overrides)
    return Job(**defaults)


def _blocking_download(release: threading.Event):
    """A download that parks until *release* is set, so jobs stay in flight.

    It honours the cancel token the queue hands it, exactly as the real
    downloader does -- without that a cancelled job would go on to finish
    ``done`` and the cascade could not be observed at all.
    """

    def fake_download(job, on_progress=None, cancel=None, **kwargs):
        while not release.wait(timeout=0.01):
            if cancel is not None and cancel.is_set():
                raise DownloadError("Download cancelled")
        if cancel is not None and cancel.is_set():
            raise DownloadError("Download cancelled")
        return "/data/music/output.flac"

    return fake_download


def _wait_outside_the_loop(
    qm: QueueManager, job_id: str, status: JobStatus, timeout=5.0
) -> None:
    """Poll from a synchronous test, where the loop belongs to the TestClient.

    A route test cannot ``await``: ``TestClient`` runs the app's event loop on
    a thread of its own, so the only way to watch a job move is to sleep on
    this one and let that one work.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = qm.get_job(job_id)
        if job is not None and job.status is status:
            return
        time.sleep(0.01)
    raise AssertionError(f"{job_id} did not reach {status.value}")


async def _wait_for(qm: QueueManager, job_id: str, status: JobStatus, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        job = qm.get_job(job_id)
        if job is not None and job.status == status:
            return
        await asyncio.sleep(0.01)
    job = qm.get_job(job_id)
    raise AssertionError(
        f"{job_id} did not reach {status.value} "
        f"(current: {job.status.value if job else 'gone'})"
    )


# ===========================================================================
# Derived status
# ===========================================================================


class TestDerivedStatus:
    """The parent's status is a pure function of its children."""

    def _statuses(self, *statuses) -> list[Job]:
        return [
            _child(index, status=status) for index, status in enumerate(statuses, 1)
        ]

    def test_any_running_child_makes_the_parent_downloading(self):
        for running in (JobStatus.DOWNLOADING, JobStatus.CONVERTING, JobStatus.TAGGING):
            children = self._statuses(JobStatus.DONE, running, JobStatus.QUEUED)
            assert QueueManager.derive_bulk_status(children) is JobStatus.DOWNLOADING

    def test_a_waiting_child_makes_the_parent_queued(self):
        children = self._statuses(JobStatus.DONE, JobStatus.QUEUED, JobStatus.ERROR)
        assert QueueManager.derive_bulk_status(children) is JobStatus.QUEUED

    def test_an_error_outranks_a_cancellation(self):
        children = self._statuses(JobStatus.CANCELLED, JobStatus.ERROR, JobStatus.DONE)
        assert QueueManager.derive_bulk_status(children) is JobStatus.ERROR

    def test_all_cancelled_is_cancelled(self):
        children = self._statuses(JobStatus.CANCELLED, JobStatus.DONE)
        assert QueueManager.derive_bulk_status(children) is JobStatus.CANCELLED

    def test_all_done_is_done(self):
        children = self._statuses(JobStatus.DONE, JobStatus.DONE)
        assert QueueManager.derive_bulk_status(children) is JobStatus.DONE

    def test_no_children_is_done(self):
        assert QueueManager.derive_bulk_status([]) is JobStatus.DONE


# ===========================================================================
# add_bulk_job
# ===========================================================================


class TestAddBulkJob:
    async def test_a_parent_and_a_child_per_track_are_queued(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        release = threading.Event()
        with patch("app.queue_manager.download_audio", side_effect=_blocking_download(release)):
            parent = qm.add_bulk_job(_parent(), [_child(1), _child(2), _child(3)])
            children = qm.children_of(parent.id)

            assert len(children) == 3
            assert all(child.parent_id == parent.id for child in children)
            assert all(child.kind is JobKind.DOWNLOAD for child in children)
            assert parent.status in (JobStatus.QUEUED, JobStatus.DOWNLOADING)
            release.set()
        for child in children:
            await _wait_for(qm, child.id, JobStatus.DONE)

    async def test_children_keep_the_folder_they_will_land_in(self):
        qm = QueueManager(max_concurrent=1, timeout=10)
        parent = qm.add_bulk_job(
            _parent(),
            [
                _child(1, target_dir="Bonobo/Black Sands", album="Black Sands"),
                _child(2, target_dir="Bonobo"),
            ],
        )
        assert [child.target_dir for child in qm.children_of(parent.id)] == [
            "Bonobo/Black Sands",
            "Bonobo",
        ]
        assert all(not child.target_guessed for child in qm.children_of(parent.id))

    async def test_a_track_already_on_disk_is_skipped_not_downloaded(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        with patch("app.queue_manager.download_audio") as download:
            parent = qm.add_bulk_job(
                _parent(),
                [_child(1), _child(2)],
                {"child-2": "Bonobo/Black Sands/Kiara.flac"},
            )
            await _wait_for(qm, "child-1", JobStatus.DONE)

        skipped = qm.get_job("child-2")
        assert skipped.status is JobStatus.ERROR
        assert skipped.error == (
            f"{ALREADY_IN_LIBRARY_PREFIX}Bonobo/Black Sands/Kiara.flac"
        )
        # Never dispatched: the whole point is not to spend a slot on it.
        assert [call.args[0].id for call in download.call_args_list] == ["child-1"]
        assert qm.get_job(parent.id).status is JobStatus.ERROR

    async def test_a_track_already_in_flight_elsewhere_is_an_error_child(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        release = threading.Event()
        with patch("app.queue_manager.download_audio", side_effect=_blocking_download(release)):
            standalone = Job(
                id="solo",
                url="https://www.youtube.com/watch?v=vid1",
                title="Track 1",
            )
            qm.add_job(standalone)
            qm.add_bulk_job(_parent(), [_child(1), _child(2)])

            blocked = qm.get_job("child-1")
            assert blocked.status is JobStatus.ERROR
            assert "already in the queue (job solo" in blocked.error
            release.set()
        await _wait_for(qm, "child-2", JobStatus.DONE)

    async def test_the_same_url_twice_in_one_selection_only_runs_once(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        release = threading.Event()
        with patch("app.queue_manager.download_audio", side_effect=_blocking_download(release)):
            qm.add_bulk_job(
                _parent(),
                [_child(1), _child(2, url="https://www.youtube.com/watch?v=vid1")],
            )
            assert qm.get_job("child-1").status is JobStatus.QUEUED
            assert qm.get_job("child-2").status is JobStatus.ERROR
            assert "already in the queue (job child-1" in qm.get_job("child-2").error
            release.set()
        await _wait_for(qm, "child-1", JobStatus.DONE)

    async def test_the_same_collection_twice_is_refused(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        release = threading.Event()
        with patch("app.queue_manager.download_audio", side_effect=_blocking_download(release)):
            qm.add_bulk_job(_parent(), [_child(1)])
            with pytest.raises(QueueError, match="already in the queue"):
                qm.add_bulk_job(_parent(id="bulk-2"), [_child(9, parent_id="bulk-2")])
            release.set()
        await _wait_for(qm, "child-1", JobStatus.DONE)

    async def test_a_parent_counts_as_in_flight_for_a_single_download(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        release = threading.Event()
        with patch("app.queue_manager.download_audio", side_effect=_blocking_download(release)):
            qm.add_bulk_job(_parent(), [_child(1)])
            assert qm.find_in_flight(COLLECTION_URL) is not None
            with pytest.raises(QueueError):
                qm.add_job(Job(id="solo", url=COLLECTION_URL))
            release.set()
        await _wait_for(qm, "child-1", JobStatus.DONE)

    async def test_the_parent_row_is_persisted_before_its_children(self, tmp_path):
        """The child rows carry a foreign key at the parent."""
        store = JobStore(tmp_path / "queue.db")
        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        release = threading.Event()
        with patch("app.queue_manager.download_audio", side_effect=_blocking_download(release)):
            qm.add_bulk_job(_parent(), [_child(1), _child(2)])
            assert store.get("bulk-1") is not None
            assert store.get("child-1").parent_id == "bulk-1"
            release.set()
        await _wait_for(qm, "child-2", JobStatus.DONE)
        store.close()


# ===========================================================================
# Parent events
# ===========================================================================


class TestParentEvents:
    async def test_a_child_transition_emits_an_n_of_m_event_for_the_parent(self):
        events = []
        qm = QueueManager(max_concurrent=2, timeout=10, on_event=events.append)
        with patch("app.queue_manager.download_audio"):
            qm.add_bulk_job(_parent(), [_child(1), _child(2)])
            await _wait_for(qm, "child-1", JobStatus.DONE)
            await _wait_for(qm, "child-2", JobStatus.DONE)

        parent_events = [
            event for event in events if event.job_id == "bulk-1"
        ]
        assert parent_events, "the parent never announced itself"
        last = parent_events[-1]
        assert last.event == "status_change"
        assert last.data["progress_done"] == 2
        assert last.data["progress_total"] == 2
        assert last.data["status"] == "done"

    async def test_a_skipped_duplicate_counts_as_a_finished_child(self):
        """The parent reads 3 of 3: a duplicate is not work still to be done.

        The parent stays ``error`` all the same: that is what keeps Dismiss
        available and the skip reason on the child row.
        """
        qm = QueueManager(max_concurrent=2, timeout=10)
        with patch("app.queue_manager.download_audio"):
            parent = qm.add_bulk_job(
                _parent(),
                [_child(1), _child(2), _child(3)],
                {"child-1": "Bonobo/Black Sands/Kiara.flac"},
            )
            await _wait_for(qm, "child-2", JobStatus.DONE)
            await _wait_for(qm, "child-3", JobStatus.DONE)

        parent = qm.get_job(parent.id)
        assert (parent.progress_done, parent.progress_total) == (3, 3)
        assert parent.status is JobStatus.ERROR

    async def test_every_event_says_its_kind_and_parent(self):
        events = []
        qm = QueueManager(max_concurrent=2, timeout=10, on_event=events.append)
        with patch("app.queue_manager.download_audio"):
            qm.add_bulk_job(_parent(), [_child(1)])
            await _wait_for(qm, "child-1", JobStatus.DONE)

        child_events = [
            event
            for event in events
            if event.job_id == "child-1" and event.event != "library_changed"
        ]
        assert child_events
        assert all(event.data["kind"] == "download" for event in child_events)
        assert all(event.data["parent_id"] == "bulk-1" for event in child_events)
        parent_events = [
            event
            for event in events
            if event.job_id == "bulk-1" and event.event != "library_changed"
        ]
        assert all(event.data["kind"] == "bulk" for event in parent_events)
        assert all(event.data["parent_id"] is None for event in parent_events)

    async def test_a_standalone_job_carries_a_null_parent(self):
        events = []
        qm = QueueManager(max_concurrent=2, timeout=10, on_event=events.append)
        with patch("app.queue_manager.download_audio"):
            qm.add_job(Job(id="solo", url="https://youtu.be/x"))
            await _wait_for(qm, "solo", JobStatus.DONE)

        assert all(
            event.data["parent_id"] is None
            for event in events
            if event.event != "library_changed"
        )


# ===========================================================================
# Cancel, retry, dismiss
# ===========================================================================


class TestCancelRetryDismiss:
    async def test_cancel_on_the_parent_cascades_to_every_child(self):
        qm = QueueManager(max_concurrent=1, timeout=10)
        release = threading.Event()
        with patch("app.queue_manager.download_audio", side_effect=_blocking_download(release)):
            parent = qm.add_bulk_job(_parent(), [_child(1), _child(2), _child(3)])
            await _wait_for(qm, "child-1", JobStatus.DOWNLOADING)

            qm.cancel_job(parent.id)

            # The queued ones are cancelled at once; the running one is only
            # signalled and reaches cancelled when its thread stops.
            assert qm.get_job("child-2").status is JobStatus.CANCELLED
            assert qm.get_job("child-3").status is JobStatus.CANCELLED
            release.set()
            await _wait_for(qm, "child-1", JobStatus.CANCELLED)
        await _wait_for(qm, parent.id, JobStatus.CANCELLED)

    async def test_cancel_on_a_finished_parent_is_refused(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        with patch("app.queue_manager.download_audio"):
            parent = qm.add_bulk_job(_parent(), [_child(1)])
            await _wait_for(qm, parent.id, JobStatus.DONE)

        with pytest.raises(QueueError):
            qm.cancel_job(parent.id)

    async def test_retry_on_the_parent_says_to_retry_the_track(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        with patch("app.queue_manager.download_audio", side_effect=Exception("boom")):
            parent = qm.add_bulk_job(_parent(), [_child(1)])
            await _wait_for(qm, "child-1", JobStatus.ERROR)

        assert qm.get_job(parent.id).status is JobStatus.ERROR
        with pytest.raises(QueueError, match="retry the failed track"):
            qm.retry_job(parent.id)
        assert BULK_RETRY_MESSAGE.endswith("retry the failed track instead")

    async def test_retrying_a_child_puts_the_parent_back_in_flight(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        with patch("app.queue_manager.download_audio", side_effect=Exception("boom")):
            parent = qm.add_bulk_job(_parent(), [_child(1)])
            await _wait_for(qm, parent.id, JobStatus.ERROR)

        release = threading.Event()
        with patch("app.queue_manager.download_audio", side_effect=_blocking_download(release)):
            qm.retry_job("child-1")
            assert qm.get_job(parent.id).status in (
                JobStatus.QUEUED,
                JobStatus.DOWNLOADING,
            )
            assert qm.get_job(parent.id).finished_at is None
            release.set()
            await _wait_for(qm, parent.id, JobStatus.DONE)

    async def test_dismissing_the_parent_deletes_every_child(self, tmp_path):
        store = JobStore(tmp_path / "queue.db")
        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        with patch("app.queue_manager.download_audio", side_effect=Exception("boom")):
            parent = qm.add_bulk_job(_parent(), [_child(1), _child(2)])
            await _wait_for(qm, parent.id, JobStatus.ERROR)

        qm.dismiss_job(parent.id)

        assert qm.get_job(parent.id) is None
        assert qm.get_job("child-1") is None
        assert qm.get_job("child-2") is None
        assert store.get("child-1") is None
        store.close()

    async def test_dismissing_the_last_failed_child_removes_the_finished_parent(self):
        qm = QueueManager(max_concurrent=2, timeout=10)

        def one_fails(job, *args, **kwargs):
            if job.id == "child-2":
                raise Exception("boom")
            return "/data/music/x.flac"

        with patch("app.queue_manager.download_audio", side_effect=one_fails):
            parent = qm.add_bulk_job(_parent(), [_child(1), _child(2)])
            await _wait_for(qm, parent.id, JobStatus.ERROR)

        qm.dismiss_job("child-2")

        assert qm.get_job("child-2") is None
        assert qm.get_job("child-1") is None
        assert qm.get_job(parent.id) is None

    async def test_dismissing_one_failed_child_of_several_keeps_the_parent(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        with patch("app.queue_manager.download_audio", side_effect=Exception("boom")):
            parent = qm.add_bulk_job(_parent(), [_child(1), _child(2)])
            await _wait_for(qm, parent.id, JobStatus.ERROR)

        qm.dismiss_job("child-1")

        assert qm.get_job(parent.id) is not None
        assert qm.get_job(parent.id).status is JobStatus.ERROR
        assert len(qm.children_of(parent.id)) == 1


# ===========================================================================
# The queue view
# ===========================================================================


class TestQueueView:
    async def test_children_are_nested_and_never_top_level(self):
        qm = QueueManager(max_concurrent=1, timeout=10)
        release = threading.Event()
        with patch("app.queue_manager.download_audio", side_effect=_blocking_download(release)):
            parent = qm.add_bulk_job(_parent(), [_child(1), _child(2)])

            view = qm.queue_view()
            assert [job.id for job in view] == [parent.id]
            assert [child.id for child in view[0].children] == ["child-1", "child-2"]
            assert all(child.children == [] for child in view[0].children)
            release.set()
        await _wait_for(qm, "child-2", JobStatus.DONE)

    async def test_a_finished_parent_leaves_the_view(self):
        qm = QueueManager(max_concurrent=2, timeout=10)
        with patch("app.queue_manager.download_audio"):
            parent = qm.add_bulk_job(_parent(), [_child(1)])
            await _wait_for(qm, parent.id, JobStatus.DONE)

        assert qm.queue_view() == []

    async def test_a_parent_with_a_failed_child_stays_visible_with_all_children(self):
        qm = QueueManager(max_concurrent=2, timeout=10)

        def one_fails(job, *args, **kwargs):
            if job.id == "child-2":
                raise Exception("boom")
            return "/data/music/x.flac"

        with patch("app.queue_manager.download_audio", side_effect=one_fails):
            parent = qm.add_bulk_job(_parent(), [_child(1), _child(2)])
            await _wait_for(qm, parent.id, JobStatus.ERROR)

        view = qm.queue_view()
        assert [job.id for job in view] == [parent.id]
        # The done child is still nested: "1 of 2" needs both rows to make sense.
        assert [child.status for child in view[0].children] == [
            JobStatus.DONE,
            JobStatus.ERROR,
        ]

    async def test_the_view_never_mutates_the_stored_jobs(self):
        qm = QueueManager(max_concurrent=1, timeout=10)
        release = threading.Event()
        with patch("app.queue_manager.download_audio", side_effect=_blocking_download(release)):
            parent = qm.add_bulk_job(_parent(), [_child(1)])
            qm.queue_view()
            assert qm.get_job(parent.id).children == []
            release.set()
        await _wait_for(qm, "child-1", JobStatus.DONE)


# ===========================================================================
# Restart and retention
# ===========================================================================


class TestRestartAndRetention:
    async def test_a_restart_resumes_the_remaining_children(self, tmp_path):
        store = JobStore(tmp_path / "queue.db")
        first = QueueManager(max_concurrent=1, timeout=10, store=store)
        release = threading.Event()
        with patch("app.queue_manager.download_audio", side_effect=_blocking_download(release)):
            first.add_bulk_job(_parent(), [_child(1), _child(2)])
            await _wait_for(first, "child-1", JobStatus.DOWNLOADING)
            release.set()
        # The process dies here: child-1 is `downloading` in the table.

        second = QueueManager(max_concurrent=1, timeout=10, store=store)
        with patch("app.queue_manager.download_audio"):
            restored = second.restore_from_store()
            assert {job.id for job in restored} == {"bulk-1", "child-1", "child-2"}
            # The parent is never re-downloaded; only its children run.
            await _wait_for(second, "child-1", JobStatus.DONE)
            await _wait_for(second, "child-2", JobStatus.DONE)
            await _wait_for(second, "bulk-1", JobStatus.DONE)
        store.close()

    async def test_a_restored_parent_derives_its_status_again(self, tmp_path):
        store = JobStore(tmp_path / "queue.db")
        first = QueueManager(max_concurrent=2, timeout=10, store=store)
        with patch("app.queue_manager.download_audio", side_effect=Exception("boom")):
            first.add_bulk_job(_parent(), [_child(1)])
            await _wait_for(first, "bulk-1", JobStatus.ERROR)

        second = QueueManager(max_concurrent=2, timeout=10, store=store)
        second.restore_from_store()

        assert second.get_job("bulk-1").status is JobStatus.ERROR
        assert [job.id for job in second.queue_view()] == ["bulk-1"]
        store.close()

    async def test_a_restart_gets_the_finished_children_back(self, tmp_path):
        """``load_active`` skips them, but they are the parent's "N of M"."""
        store = JobStore(tmp_path / "queue.db")
        store.upsert(_parent(status=JobStatus.DOWNLOADING))
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        statuses = [
            JobStatus.DONE,
            JobStatus.DONE,
            JobStatus.CANCELLED,
            *[JobStatus.QUEUED] * 5,
        ]
        for index, status in enumerate(statuses):
            store.upsert(
                _child(index, status=status, created_at=base + timedelta(minutes=index))
            )

        qm = QueueManager(max_concurrent=1, timeout=10, store=store)
        with patch("app.queue_manager.download_audio", side_effect=_blocking_download(threading.Event())):
            restored = qm.restore_from_store()

            # The finished children are in the queue but are not "restored":
            # there is nothing to recover or dispatch for them.
            assert {job.id for job in restored} == {
                "bulk-1", "child-3", "child-4", "child-5", "child-6", "child-7"
            }
            parent = qm.get_job("bulk-1")
            assert (parent.progress_done, parent.progress_total) == (2, 8)
            view = qm.queue_view()
            assert len(view) == 1
            assert len(view[0].children) == 8
        store.close()

    async def test_a_live_parents_old_children_survive_the_sweep(self, tmp_path):
        """A child leaves only with its parent, never on its own."""
        store = JobStore(tmp_path / "queue.db")
        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        with patch("app.queue_manager.download_audio", side_effect=[Exception("boom"), "/data/music/x.flac"]):
            qm.add_bulk_job(_parent(), [_child(1), _child(2)])
            await _wait_for(qm, "bulk-1", JobStatus.ERROR)

        old = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS + 1)
        done_child = qm.get_job("child-2")
        done_child.finished_at = old
        done_child.updated_at = old
        store.upsert(done_child)

        assert qm.sweep() == 0

        assert qm.get_job("child-2") is not None
        assert store.get("child-2") is not None
        parent = qm.get_job("bulk-1")
        assert (parent.progress_done, parent.progress_total) == (1, 2)
        store.close()

    async def test_the_retention_sweep_takes_the_children_with_the_parent(self, tmp_path):
        store = JobStore(tmp_path / "queue.db")
        qm = QueueManager(max_concurrent=2, timeout=10, store=store)
        with patch("app.queue_manager.download_audio"):
            parent = qm.add_bulk_job(_parent(), [_child(1), _child(2)])
            await _wait_for(qm, parent.id, JobStatus.DONE)

        old = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS + 1)
        for job_id in ("bulk-1", "child-1", "child-2"):
            job = qm.get_job(job_id)
            job.finished_at = old
            job.updated_at = old
            store.upsert(job)

        qm.sweep()

        for job_id in ("bulk-1", "child-1", "child-2"):
            assert qm.get_job(job_id) is None
            assert store.get(job_id) is None
        store.close()


class TestThreadSafeReads:
    """The queue is read off the event loop as well as on it."""

    async def test_children_of_survives_a_concurrent_submission(self):
        """A download worker reads a parent while the loop fills it.

        yt-dlp's ``on_phase("converting")`` hook runs on an executor thread and
        reaches ``_refresh_parent`` -> ``children_of``; a bulk submission is
        meanwhile inserting a child per iteration on the event loop.  Iterating
        ``_jobs`` directly there raises "dictionary changed size during
        iteration" *inside an unrelated child's download*, failing it with
        "Unexpected error".
        """
        qm = QueueManager(max_concurrent=1, timeout=10)
        qm._jobs["bulk-1"] = _parent()
        failures: list[BaseException] = []
        stop = threading.Event()

        def read_from_a_worker_thread() -> None:
            while not stop.is_set():
                try:
                    qm.children_of("bulk-1")
                except BaseException as exc:  # noqa: BLE001 - that is the point
                    failures.append(exc)
                    return

        reader = threading.Thread(target=read_from_a_worker_thread, daemon=True)
        reader.start()
        try:
            for _ in range(20):
                # The insert loop of add_bulk_job, without 2000 dispatches.
                for index in range(2000):
                    qm._jobs[f"child-{index}"] = _child(index)
                for index in range(2000):
                    qm._jobs.pop(f"child-{index}", None)
                if failures:
                    break
        finally:
            stop.set()
            reader.join(timeout=5)

        assert failures == []


# ===========================================================================
# Routes
# ===========================================================================


@pytest.fixture()
def client_and_qm():
    """A TestClient over a fresh QueueManager, as the route tests use."""
    import app.main as main_module

    fresh = QueueManager(
        max_concurrent=2, timeout=10, on_event=main_module._on_queue_event
    )
    original = main_module.queue_manager
    main_module.queue_manager = fresh
    with TestClient(main_module.app) as client:
        yield client, fresh
    main_module.queue_manager = original


def _enumeration(rows=None, **overrides) -> Enumeration:
    defaults = {
        "url": COLLECTION_URL,
        "title": "Chill mix",
        "artist": "Bonobo",
        "source": "youtube",
        "rows": tuple(
            rows
            if rows is not None
            else [
                EnumeratedTrack(
                    id="youtube:vid1",
                    url="https://www.youtube.com/watch?v=vid1",
                    source_id="youtube:vid1",
                    title="Kiara",
                    duration=213.0,
                ),
                EnumeratedTrack(
                    id="youtube:vid2",
                    url="https://www.youtube.com/watch?v=vid2",
                    source_id="youtube:vid2",
                    title="Kong",
                    album="Black Sands",
                    unavailable_reason="DRM protected",
                ),
            ]
        ),
        "notices": (),
    }
    defaults.update(overrides)
    return Enumeration(**defaults)


class TestProbeRoute:
    def test_a_track_url_answers_type_track(self, client_and_qm):
        client, _ = client_and_qm
        single = SingleTrack(
            title="Kiara",
            duration=213.0,
            thumbnail_url="https://img/1.jpg",
            artist="Bonobo",
            album="Black Sands",
        )
        with patch("app.main.probe", return_value=single):
            resp = client.post("/download/probe", json={"url": COLLECTION_URL})

        assert resp.status_code == 200
        assert resp.json() == {
            "type": "track",
            "title": "Kiara",
            "duration": 213.0,
            "thumbnail_url": "https://img/1.jpg",
            "artist": "Bonobo",
            "album": "Black Sands",
        }

    def test_a_collection_answers_a_preview(self, client_and_qm):
        client, _ = client_and_qm
        with patch("app.main.probe", return_value=_enumeration()):
            resp = client.post("/download/probe", json={"url": COLLECTION_URL})

        assert resp.status_code == 200
        body = resp.json()
        assert body["type"] == "collection"
        preview = body["preview"]
        assert preview["artist"] == "Bonobo"
        assert preview["source"] == "youtube"
        assert preview["total"] == 2
        assert preview["unavailable"] == 1
        assert preview["in_library"] == 0
        assert preview["large"] is False
        assert preview["rows"][0]["status"] == "available"
        assert preview["rows"][1]["status"] == "unavailable"
        assert preview["rows"][1]["reason"] == "DRM protected"

    def test_a_row_already_on_disk_is_marked_in_library(self, client_and_qm, isolated_paths):
        client, _ = client_and_qm
        download_dir, _data = isolated_paths
        from tests.test_probe import _write_flac

        _write_flac(
            download_dir / "Bonobo" / "Kiara.flac",
            title="Kiara",
            tags={"SOURCEID": "youtube:vid1"},
        )

        with patch("app.main.probe", return_value=_enumeration()):
            resp = client.post("/download/probe", json={"url": COLLECTION_URL})

        preview = resp.json()["preview"]
        assert preview["in_library"] == 1
        assert preview["rows"][0]["status"] == "in_library"
        assert preview["rows"][0]["reason"] == "Bonobo/Kiara.flac"

    def test_an_over_long_in_library_path_keeps_its_tail(
        self, client_and_qm, isolated_paths
    ):
        """A path longer than MAX_REASON is elided at the *front*.

        The head is the same library root on every row; the tail -- artist,
        album, file -- is what tells the user which track matched.
        """
        client, _ = client_and_qm
        download_dir, _data = isolated_paths
        from tests.test_probe import _write_flac

        artist = "A" * 180
        album = "B" * 180
        _write_flac(
            download_dir / artist / album / "Kiara.flac",
            title="Kiara",
            tags={"SOURCEID": "youtube:vid1"},
        )

        with patch("app.main.probe", return_value=_enumeration()):
            resp = client.post(
                "/download/probe",
                json={"url": COLLECTION_URL, "artist": artist},
            )

        assert resp.status_code == 200
        reason = resp.json()["preview"]["rows"][0]["reason"]
        assert len(reason) == MAX_REASON
        assert reason.startswith("\u2026")
        assert reason.endswith(f"{album}/Kiara.flac")

    def test_a_corrected_artist_dedups_against_that_folder(
        self, client_and_qm, isolated_paths
    ):
        """The suggestion is a slug; the user's edit is where the tracks are."""
        client, _ = client_and_qm
        download_dir, _data = isolated_paths
        from tests.test_probe import _write_flac

        _write_flac(
            download_dir / "Zoe Keating" / "Kiara.flac",
            title="Kiara",
            tags={"SOURCEID": "youtube:vid1"},
        )

        with patch("app.main.probe", return_value=_enumeration(artist="zoekeating")):
            resp = client.post(
                "/download/probe",
                json={"url": COLLECTION_URL, "artist": "Zoe Keating"},
            )

        preview = resp.json()["preview"]
        # The dedup ran against the typed artist...
        assert preview["in_library"] == 1
        assert preview["rows"][0]["status"] == "in_library"
        assert preview["rows"][0]["reason"] == "Zoe Keating/Kiara.flac"
        # ...while the suggestion the form shows is still the source's.
        assert preview["artist"] == "zoekeating"

    def test_a_blank_artist_falls_back_to_the_suggestion(
        self, client_and_qm, isolated_paths
    ):
        client, _ = client_and_qm
        download_dir, _data = isolated_paths
        from tests.test_probe import _write_flac

        _write_flac(
            download_dir / "Bonobo" / "Kiara.flac",
            title="Kiara",
            tags={"SOURCEID": "youtube:vid1"},
        )

        with patch("app.main.probe", return_value=_enumeration()):
            resp = client.post(
                "/download/probe", json={"url": COLLECTION_URL, "artist": "   "}
            )

        assert resp.json()["preview"]["in_library"] == 1

    def test_an_artist_whose_folder_name_is_rewritten_still_dedups(
        self, client_and_qm, isolated_paths
    ):
        """"AC/DC" is filed under "AC⧸DC"; dedup must look in that folder."""
        client, _ = client_and_qm
        download_dir, _data = isolated_paths
        from tests.test_probe import _write_flac

        folder, _album = resolve_artist_album("AC/DC", None, None, None)
        _write_flac(
            download_dir / folder / "Kiara.flac",
            title="Kiara",
            tags={"SOURCEID": "youtube:vid1"},
        )

        with patch("app.main.probe", return_value=_enumeration()):
            resp = client.post(
                "/download/probe", json={"url": COLLECTION_URL, "artist": "AC/DC"}
            )

        preview = resp.json()["preview"]
        assert preview["in_library"] == 1
        assert preview["rows"][0]["reason"] == f"{folder}/Kiara.flac"

    def test_a_non_string_title_is_answered_as_null(self, client_and_qm):
        """yt-dlp may hand back anything; a 500 is never the right answer."""
        client, _ = client_and_qm
        url = "https://www.youtube.com/playlist?list=PLnonstring"

        class _OddlyTypedYoutubeDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def extract_info(self, url, download=False):
                return {
                    "_type": "playlist",
                    "id": "PLnonstring",
                    "title": {"text": "a dict, not a title"},
                    "entries": [
                        {
                            "ie_key": "Youtube",
                            "id": "vid1",
                            "url": "https://www.youtube.com/watch?v=vid1",
                            "title": 123,
                            "artist": ["Bonobo"],
                        }
                    ],
                }

        clear_cache()
        try:
            with patch("app.probe.yt_dlp.YoutubeDL", _OddlyTypedYoutubeDL):
                resp = client.post("/download/probe", json={"url": url})
        finally:
            clear_cache()

        assert resp.status_code == 200
        preview = resp.json()["preview"]
        assert preview["title"] is None
        assert preview["rows"][0]["title"] is None
        assert preview["rows"][0]["status"] == "available"

    def test_a_swallowed_top_level_error_is_reported_to_the_user(
        self, client_and_qm
    ):
        """``ignoreerrors`` hides the reason in the log; the 400 carries it."""
        client, _ = client_and_qm
        message = (
            "ERROR: [youtube:tab] @x: This channel does not have a videos tab"
        )

        class _SilentlyFailingYoutubeDL:
            def __init__(self, opts):
                self._opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def extract_info(self, url, download=False):
                self._opts["logger"].error(message)
                return None

        with patch("app.probe.yt_dlp.YoutubeDL", _SilentlyFailingYoutubeDL):
            resp = client.post("/download/probe", json={"url": COLLECTION_URL})

        assert resp.status_code == 400
        assert "does not have a videos tab" in resp.json()["detail"]
        assert "ERROR:" not in resp.json()["detail"]

    def test_a_too_large_collection_is_a_400_asking_for_a_narrower_url(
        self, client_and_qm
    ):
        from app.probe import CollectionTooLarge

        client, _ = client_and_qm
        with patch("app.main.probe", side_effect=CollectionTooLarge()):
            resp = client.post("/download/probe", json={"url": COLLECTION_URL})

        assert resp.status_code == 400
        assert resp.json()["detail"].startswith(
            "This playlist, album or artist page has more than 2000 tracks"
        )
        assert "narrower URL" in resp.json()["detail"]

    def test_a_failed_probe_is_a_400(self, client_and_qm):
        client, _ = client_and_qm
        with patch("app.main.probe", side_effect=ProbeError("Video unavailable")):
            resp = client.post("/download/probe", json={"url": COLLECTION_URL})

        assert resp.status_code == 400
        assert resp.json()["detail"].startswith("Failed to probe:")

    def test_a_slow_probe_is_a_504(self, client_and_qm):
        client, _ = client_and_qm
        with patch("app.main.probe", side_effect=ProbeTimeout("too slow")):
            resp = client.post("/download/probe", json={"url": COLLECTION_URL})

        assert resp.status_code == 504

    def test_an_unsupported_host_is_refused_before_any_probe(self, client_and_qm):
        client, _ = client_and_qm
        resp = client.post("/download/probe", json={"url": "https://example.com/x"})
        assert resp.status_code == 422

    def test_bandcamp_is_an_allowed_host(self, client_and_qm):
        client, _ = client_and_qm
        with patch("app.main.probe", return_value=_enumeration(source="bandcamp")):
            resp = client.post(
                "/download/probe",
                json={"url": "https://zoekeating.bandcamp.com/album/into-the-trees"},
            )
        assert resp.status_code == 200


class TestAlbumIsFinalRoundTrip:
    """The "the source read the release" flag, end to end.

    The enumeration sets it, the preview sends it, the client sends it back,
    the child job carries it and the database keeps it -- because the
    downloader reads it on a child that a restart re-ran, long after the
    preview is gone.
    """

    ROW = EnumeratedTrack(
        id="youtube:mahal",
        url="https://music.youtube.com/watch?v=mahal",
        source_id="youtube:mahal",
        title="Mahal",
        # A Single: no album, and that is the answer rather than a gap.
        album=None,
        album_final=True,
    )

    def test_it_survives_probe_preview_bulk_and_a_restore(
        self, client_and_qm, tmp_path
    ):
        client, qm = client_and_qm
        with patch("app.main.probe", return_value=_enumeration(rows=[self.ROW])):
            preview = client.post(
                "/download/probe", json={"url": COLLECTION_URL}
            ).json()["preview"]

        assert [row["album"] for row in preview["rows"]] == [None]
        assert [row["album_final"] for row in preview["rows"]] == [True]

        # Exactly what the frontend posts back: the preview row's own fields.
        body = {
            "url": COLLECTION_URL,
            "artist": "Glass Beams",
            "title": "Mahal",
            "tracks": [
                {
                    key: row[key]
                    for key in (
                        "url",
                        "title",
                        "album",
                        "album_final",
                        "duration",
                        "thumbnail_url",
                        "source_id",
                    )
                }
                for row in preview["rows"]
            ],
        }
        release = threading.Event()
        with patch(
            "app.queue_manager.download_audio", side_effect=_blocking_download(release)
        ):
            parent = client.post("/download/bulk", json=body).json()
            child = parent["children"][0]
            assert child["album"] is None
            assert child["album_final"] is True
            release.set()

        store = JobStore(tmp_path / "queue.db")
        try:
            # The parent first: the child's ``parent_id`` is a foreign key.
            store.upsert(qm.get_job(parent["id"]))
            store.upsert(qm.get_job(child["id"]))
            assert store.get(child["id"]).album_final is True
        finally:
            store.close()

    def test_a_row_the_flat_pass_produced_arrives_false(self, client_and_qm):
        client, _ = client_and_qm
        with patch("app.main.probe", return_value=_enumeration()):
            preview = client.post(
                "/download/probe", json={"url": COLLECTION_URL}
            ).json()["preview"]

        assert [row["album_final"] for row in preview["rows"]] == [False, False]

    def test_a_client_that_omits_it_gets_the_old_behaviour(self, client_and_qm):
        """The field is optional, and its default is "not final"."""
        client, _ = client_and_qm
        body = {
            "url": COLLECTION_URL,
            "artist": "Bonobo",
            "tracks": [{"url": "https://www.youtube.com/watch?v=vid1"}],
        }
        release = threading.Event()
        with patch(
            "app.queue_manager.download_audio", side_effect=_blocking_download(release)
        ):
            resp = client.post("/download/bulk", json=body)
            assert resp.status_code == 200
            assert resp.json()["children"][0]["album_final"] is False
            release.set()


class TestBulkRoute:
    def _body(self, **overrides) -> dict:
        body = {
            "url": COLLECTION_URL,
            "artist": "Bonobo",
            "title": "Chill mix",
            "tracks": [
                {
                    "url": "https://www.youtube.com/watch?v=vid1",
                    "title": "Kiara",
                    "album": "Black Sands",
                    "duration": 213.0,
                    "source_id": "youtube:vid1",
                },
                {
                    "url": "https://www.youtube.com/watch?v=vid2",
                    "title": "Kong",
                    "source_id": "youtube:vid2",
                },
            ],
        }
        body.update(overrides)
        return body

    def test_a_selection_becomes_a_parent_with_children(self, client_and_qm):
        client, qm = client_and_qm
        release = threading.Event()
        with patch(
            "app.queue_manager.download_audio", side_effect=_blocking_download(release)
        ):
            resp = client.post("/download/bulk", json=self._body())
            assert resp.status_code == 200
            body = resp.json()
            assert body["kind"] == "bulk"
            assert body["artist"] == "Bonobo"
            assert len(body["children"]) == 2
            assert [child["title"] for child in body["children"]] == ["Kiara", "Kong"]
            assert [child["target_dir"] for child in body["children"]] == [
                "Bonobo/Black Sands",
                "Bonobo",
            ]
            assert all(child["parent_id"] == body["id"] for child in body["children"])
            assert all(child["children"] == [] for child in body["children"])
            release.set()

    def test_the_queue_nests_the_children_and_hides_them_at_the_top(
        self, client_and_qm
    ):
        client, qm = client_and_qm
        release = threading.Event()
        with patch(
            "app.queue_manager.download_audio", side_effect=_blocking_download(release)
        ):
            parent_id = client.post("/download/bulk", json=self._body()).json()["id"]

            queue = client.get("/queue").json()
            assert [job["id"] for job in queue] == [parent_id]
            assert len(queue[0]["children"]) == 2
            release.set()

    def test_a_second_submission_of_the_same_collection_is_a_409(self, client_and_qm):
        client, _ = client_and_qm
        release = threading.Event()
        with patch(
            "app.queue_manager.download_audio", side_effect=_blocking_download(release)
        ):
            assert client.post("/download/bulk", json=self._body()).status_code == 200
            resp = client.post("/download/bulk", json=self._body())
            assert resp.status_code == 409
            release.set()

    def test_a_row_already_on_disk_is_skipped_with_a_reason(
        self, client_and_qm, isolated_paths
    ):
        client, qm = client_and_qm
        download_dir, _data = isolated_paths
        from tests.test_probe import _write_flac

        _write_flac(
            download_dir / "Bonobo" / "Black Sands" / "Kiara.flac",
            title="Kiara",
            tags={"SOURCEID": "youtube:vid1"},
        )

        resp = client.post("/download/bulk", json=self._body())

        assert resp.status_code == 200
        first = resp.json()["children"][0]
        assert first["status"] == "error"
        assert first["error"].startswith(ALREADY_IN_LIBRARY_PREFIX)

    def test_an_artist_whose_folder_name_is_rewritten_still_dedups(
        self, client_and_qm, isolated_paths
    ):
        """The children land in "AC⧸DC", so that is where dedup must look."""
        client, _ = client_and_qm
        download_dir, _data = isolated_paths
        from tests.test_probe import _write_flac

        folder, _album = resolve_artist_album("AC/DC", None, None, None)
        _write_flac(
            download_dir / folder / "Black Sands" / "Kiara.flac",
            title="Kiara",
            tags={"SOURCEID": "youtube:vid1"},
        )

        resp = client.post("/download/bulk", json=self._body(artist="AC/DC"))

        assert resp.status_code == 200
        first = resp.json()["children"][0]
        assert first["status"] == "error"
        assert first["error"] == (
            f"{ALREADY_IN_LIBRARY_PREFIX}{folder}/Black Sands/Kiara.flac"
        )

    def test_a_blank_artist_is_refused(self, client_and_qm):
        client, _ = client_and_qm
        resp = client.post("/download/bulk", json=self._body(artist="   "))
        assert resp.status_code == 422

    def test_an_empty_selection_is_refused(self, client_and_qm):
        client, _ = client_and_qm
        resp = client.post("/download/bulk", json=self._body(tracks=[]))
        assert resp.status_code == 422

    def test_a_track_on_an_unsupported_host_is_refused(self, client_and_qm):
        client, _ = client_and_qm
        resp = client.post(
            "/download/bulk",
            json=self._body(tracks=[{"url": "https://example.com/x"}]),
        )
        assert resp.status_code == 422

    def test_cancelling_the_parent_answers_with_its_children(self, client_and_qm):
        client, qm = client_and_qm
        release = threading.Event()
        with patch(
            "app.queue_manager.download_audio", side_effect=_blocking_download(release)
        ):
            parent_id = client.post("/download/bulk", json=self._body()).json()["id"]

            resp = client.post(f"/queue/{parent_id}/cancel")
            assert resp.status_code == 200
            assert len(resp.json()["children"]) == 2
            release.set()

    def test_retrying_the_parent_is_a_400(self, client_and_qm):
        client, qm = client_and_qm
        with patch("app.queue_manager.download_audio", side_effect=Exception("boom")):
            parent_id = client.post("/download/bulk", json=self._body()).json()["id"]
            _wait_outside_the_loop(qm, parent_id, JobStatus.ERROR)

        resp = client.post(f"/queue/{parent_id}/retry")
        assert resp.status_code == 400
        assert "retry the failed track" in resp.json()["detail"]
