"""Persistence tests for ``nanobot.cron.service.CronService``.

These tests target the specific failure mode where a corrupt or partially
written ``jobs.json`` would silently turn into an empty job list on the next
start, deleting every scheduled job.  See ``fix(cron): atomic write for
jobs.json + don't silently overwrite corrupt store``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


def _seeded_store(tmp_path: Path) -> tuple[CronService, Path]:
    """Build a service with one persisted job on disk and return both the
    service and the resolved store path.  Adds the job via the action log
    (the path used when the service is not running) and then triggers a
    merge so ``jobs.json`` is written, mirroring the persisted on-disk
    state seen in production."""
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path)
    service.add_job(
        name="Daily Loving Message",
        schedule=CronSchedule(kind="cron", expr="0 10 * * *", tz="Asia/Kuwait"),
        message="hello",
    )
    # add_job appended to action.jsonl; flush to jobs.json by toggling
    # ``_running`` long enough for ``_merge_action`` to do its rewrite.
    service._running = True
    try:
        service._load_store()
    finally:
        service._running = False
    assert store_path.exists()
    return service, store_path


def test_save_store_is_atomic(tmp_path: Path) -> None:
    """``_save_store`` must use temp-file + rename so an interrupted write
    cannot leave the destination truncated or invalid."""
    service, store_path = _seeded_store(tmp_path)

    # Simulate an arbitrary save and confirm the result parses cleanly and
    # no orphan ``.tmp`` is left behind.
    service._save_store()
    data = json.loads(store_path.read_text(encoding="utf-8"))
    assert len(data["jobs"]) == 1

    tmp_files = list(store_path.parent.glob("*.tmp"))
    assert tmp_files == [], f"unexpected temp files left behind: {tmp_files}"


def test_save_store_failure_does_not_corrupt_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If writing the temp file blows up partway through, the previous
    ``jobs.json`` must remain readable.  This is the regression we are
    actually fixing: pre-fix, ``write_text`` would truncate the destination
    in place and leave it corrupt."""
    service, store_path = _seeded_store(tmp_path)
    original = store_path.read_bytes()

    # Inject a failure inside the temp-file write.  ``os.replace`` should
    # never run; the destination must keep its previous content.
    real_open = open

    def boom(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path).endswith(".tmp"):
            raise OSError("simulated disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", boom)

    with pytest.raises(OSError, match="simulated disk full"):
        service._save_store()

    assert store_path.read_bytes() == original


def test_load_jobs_preserves_corrupt_store_and_returns_none(
    tmp_path: Path,
) -> None:
    """A corrupt ``jobs.json`` must not be silently treated as an empty
    list.  The loader returns ``None`` and the corrupt file is moved aside
    with a ``.corrupt-<ts>`` suffix so an operator can recover it."""
    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text("{not valid json", encoding="utf-8")

    service = CronService(store_path)
    assert service._load_jobs() is None

    # Original path is gone; a ``.corrupt-<ts>`` backup exists alongside it.
    assert not store_path.exists()
    backups = list(store_path.parent.glob("jobs.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json"


def test_start_refuses_to_overwrite_corrupt_store(tmp_path: Path) -> None:
    """``start`` must abort instead of running ``_save_store`` against an
    empty in-memory state when the on-disk store is corrupt.  Otherwise the
    next save would overwrite the (recoverable) corrupt file with an empty
    job list and the user's jobs would be unrecoverable."""
    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text("{still not json", encoding="utf-8")

    service = CronService(store_path)
    import asyncio

    with pytest.raises(RuntimeError, match="corrupt"):
        asyncio.run(service.start())

    # Service is left in a stopped state so the operator notices.
    assert service._running is False

    # And the corrupt file is still recoverable from the .corrupt-<ts> copy.
    backups = list(store_path.parent.glob("jobs.json.corrupt-*"))
    assert len(backups) == 1


def test_load_store_falls_back_to_in_memory_on_corruption_after_start(
    tmp_path: Path,
) -> None:
    """If the store file becomes corrupt *after* a successful start (e.g. a
    rclone-mounted Drive returns a partial read), the service must keep
    using its existing in-memory snapshot instead of dropping every job."""
    service, store_path = _seeded_store(tmp_path)
    # Force load so ``self._store`` is populated.
    service._load_store()
    snapshot = service._store
    assert snapshot is not None and len(snapshot.jobs) == 1

    # Now corrupt the file on disk.
    store_path.write_text("\x00garbage\x00", encoding="utf-8")

    # Subsequent reload returns the in-memory snapshot, not None or empty.
    result = service._load_store()
    assert result is snapshot
    assert len(result.jobs) == 1
    assert result.jobs[0].name == "Daily Loving Message"


def test_full_round_trip_survives_repeated_save_load(tmp_path: Path) -> None:
    """Sanity check: jobs survive add → save → reload across fresh
    ``CronService`` instances pointing at the same store."""
    store_path = tmp_path / "cron" / "jobs.json"

    s1 = CronService(store_path)
    s1.add_job(
        name="Daily Loving Message",
        schedule=CronSchedule(kind="cron", expr="0 10 * * *", tz="Asia/Kuwait"),
        message="hello",
    )

    s2 = CronService(store_path)
    s2._load_store()
    assert s2._store is not None
    assert [j.name for j in s2._store.jobs] == ["Daily Loving Message"]


# ---------------------------------------------------------------------------
# Rollback on save failure tests
# ---------------------------------------------------------------------------


def _started_service(tmp_path: Path) -> tuple[CronService, Path]:
    """Create a started service with one existing job.

    Uses the action log path (service not running) to add a job, then
    merges to jobs.json and marks the service as running so subsequent
    operations go through the running-path code.
    """
    store_path = tmp_path / "cron" / "jobs.json"
    service = CronService(store_path)
    # Add via action log (not running path)
    service.add_job(
        name="existing",
        schedule=CronSchedule(kind="every", every_ms=60000),
        message="test",
    )
    # Now mark as running and load/merge to populate _store
    service._running = True
    service._load_store()
    # Disable _arm_timer since we're not in an async context
    service._arm_timer = lambda: None
    return service, store_path


def _failing_atomic_write(path, content):
    raise IOError("disk full")


def test_add_job_rollback_on_save_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """add_job must rollback in-memory state if _save_store fails."""
    service, store_path = _started_service(tmp_path)
    original_disk = store_path.read_bytes()
    assert len(service._store.jobs) == 1

    monkeypatch.setattr(CronService, "_atomic_write", staticmethod(_failing_atomic_write))

    with pytest.raises(IOError, match="disk full"):
        service.add_job(
            name="new",
            schedule=CronSchedule(kind="every", every_ms=60000),
            message="test2",
        )

    # Check in-memory state was rolled back (not just disk)
    assert len(service._store.jobs) == 1
    assert service._store.jobs[0].name == "existing"
    assert store_path.read_bytes() == original_disk


def test_remove_job_rollback_on_save_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """remove_job must rollback in-memory state if _save_store fails."""
    service, store_path = _started_service(tmp_path)
    original_disk = store_path.read_bytes()
    job_id = service._store.jobs[0].id

    monkeypatch.setattr(CronService, "_atomic_write", staticmethod(_failing_atomic_write))

    with pytest.raises(IOError, match="disk full"):
        service.remove_job(job_id)

    # Check in-memory state was rolled back
    assert len(service._store.jobs) == 1
    assert service._store.jobs[0].id == job_id
    assert store_path.read_bytes() == original_disk


def test_enable_job_rollback_on_save_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enable_job must rollback in-memory state if _save_store fails."""
    service, store_path = _started_service(tmp_path)
    job_id = service._store.jobs[0].id
    original_enabled = service._store.jobs[0].enabled
    original_next_run = service._store.jobs[0].state.next_run_at_ms
    original_updated_at = service._store.jobs[0].updated_at_ms
    original_disk = store_path.read_bytes()

    monkeypatch.setattr(CronService, "_atomic_write", staticmethod(_failing_atomic_write))

    with pytest.raises(IOError, match="disk full"):
        service.enable_job(job_id, enabled=False)

    # Check in-memory state was rolled back
    job = next(j for j in service._store.jobs if j.id == job_id)
    assert job.enabled == original_enabled
    assert job.state.next_run_at_ms == original_next_run
    assert job.updated_at_ms == original_updated_at
    assert store_path.read_bytes() == original_disk


def test_update_job_rollback_on_save_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """update_job must rollback in-memory state if _save_store fails."""
    service, store_path = _started_service(tmp_path)
    job_id = service._store.jobs[0].id
    original_name = service._store.jobs[0].name
    original_message = service._store.jobs[0].payload.message
    original_updated_at = service._store.jobs[0].updated_at_ms
    original_disk = store_path.read_bytes()

    monkeypatch.setattr(CronService, "_atomic_write", staticmethod(_failing_atomic_write))

    with pytest.raises(IOError, match="disk full"):
        service.update_job(job_id, name="new_name", message="new_message")

    # Check in-memory state was rolled back
    job = next(j for j in service._store.jobs if j.id == job_id)
    assert job.name == original_name
    assert job.payload.message == original_message
    assert job.updated_at_ms == original_updated_at
    assert store_path.read_bytes() == original_disk


def test_register_system_job_rollback_on_save_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """register_system_job must rollback in-memory state if _save_store fails."""
    from nanobot.cron.types import CronJob, CronJobState, CronPayload

    service, store_path = _started_service(tmp_path)
    original_disk = store_path.read_bytes()
    original_jobs = [j.id for j in service._store.jobs]

    monkeypatch.setattr(CronService, "_atomic_write", staticmethod(_failing_atomic_write))

    system_job = CronJob(
        id="sys-job",
        name="System Job",
        enabled=True,
        schedule=CronSchedule(kind="every", every_ms=60000),
        payload=CronPayload(kind="system_event", message="check"),
        state=CronJobState(),
        created_at_ms=0,
        updated_at_ms=0,
        delete_after_run=False,
    )

    with pytest.raises(IOError, match="disk full"):
        service.register_system_job(system_job)

    # Check in-memory state was rolled back
    current_jobs = [j.id for j in service._store.jobs]
    assert current_jobs == original_jobs
    assert store_path.read_bytes() == original_disk
