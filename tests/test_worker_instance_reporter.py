from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from worker_telemetry import WorkerInstanceReporter


class _Store:
    def __init__(self):
        self.rows = []
        self.events = []

    def upsert_worker_instance(self, worker_id, **fields):
        self.rows.append({"worker_id": worker_id, **fields})

    def append_orchestration_event(self, **fields):
        self.events.append(fields)


class _Worker:
    def __init__(self, active=None, job_types=(), unhealthy=False):
        self.active_job_id = active
        self.job_types = job_types
        self.unhealthy = unhealthy


class _Core:
    WORKERS = 2
    WORKER_INSTANCE_FRESHNESS_SECONDS = 30

    def __init__(self, handles):
        self._worker_handles = [(worker, object()) for worker in handles]
        self.store = _Store()

    def get_store(self): return self.store
    def worker_process_instance_id(self, role): return f"{role}:replica-a:process-a"
    def _replica_id(self): return "replica-a"


def test_reports_busy_slots_and_emits_only_lifecycle_transitions(monkeypatch):
    monkeypatch.setenv("ACP_WORKER_ROLE", "assess")
    core = _Core([_Worker("opaque-job", ("scan_assess",)), _Worker(None, ("scan_file",))])
    reporter = WorkerInstanceReporter(core)
    reporter.record("starting")
    reporter.record()
    reporter.record()

    assert core.store.rows[-1]["active_job_count"] == 1
    assert core.store.rows[-1]["concurrency_limit"] == 2
    assert core.store.rows[-1]["available_slots"] == 1
    assert core.store.rows[-1]["supported_job_types"] == ["scan_assess", "scan_file"]
    assert [event["kind"] for event in core.store.events] == ["worker.starting", "worker.busy"]
    assert all(event["owner_email"] == "system" for event in core.store.events)


def test_unrestricted_mixed_worker_reports_all_registered_handlers(monkeypatch):
    import worker
    monkeypatch.setenv("ACP_WORKER_ROLE", "mixed")
    monkeypatch.setattr(worker, "HANDLERS", {"one": object(), "two": object()})
    core = _Core([_Worker(None, None), _Worker(None, ("one",))])
    reporter = WorkerInstanceReporter(core)
    reporter.record()
    assert core.store.rows[-1]["supported_job_types"] == ["one", "two"]


def test_a_slot_that_cannot_claim_marks_the_process_unhealthy(monkeypatch):
    monkeypatch.setenv("ACP_WORKER_ROLE", "assess")
    core = _Core([_Worker(unhealthy=True, job_types=("scan_assess",))])
    reporter = WorkerInstanceReporter(core)
    reporter.record()
    assert core.store.rows[-1]["state"] == "unhealthy"
    assert core.store.events[-1]["kind"] == "worker.unhealthy"


def test_draining_keeps_reporting_active_work_until_stopped(monkeypatch):
    monkeypatch.setenv("ACP_WORKER_ROLE", "assess")
    worker = _Worker("job-in-flight", ("scan_assess",))
    core = _Core([worker])
    reporter = WorkerInstanceReporter(core)

    reporter.draining()
    reporter.record()  # the next periodic heartbeat must not revert to busy or ready

    assert core.store.rows[-1]["state"] == "draining"
    assert core.store.rows[-1]["active_job_count"] == 1
    assert core.store.rows[-1]["last_claimed_job_id"] == "job-in-flight"
    assert [event["kind"] for event in core.store.events] == ["worker.draining"]


def test_app_wires_the_same_reporter_around_embedded_workers():
    source = (Path(__file__).resolve().parent.parent / "api" / "app.py").read_text()
    assert "_embedded_worker_reporter = WorkerInstanceReporter(core)" in source
    assert ('_startup_phase.run("worker_reporter_start", _embedded_worker_reporter.start' in source)
    assert source.index("_embedded_worker_reporter.draining()") < source.index("core.stop_workers()")
    assert source.index("core.stop_workers()") < source.index("_embedded_worker_reporter.stop()")
    assert "_embedded_worker_reporter.stop()" in source
    assert "_embedded_worker_reporter.offline()" in source


def test_standalone_worker_reports_draining_until_workers_finish():
    source = (Path(__file__).resolve().parent.parent / "api" / "worker_main.py").read_text()
    drain = source.index("reporter.draining()")
    workers = source.index("core.stop_workers()", drain)
    stop = source.index("reporter.stop()", workers)
    offline = source.index("reporter.offline()", stop)
    assert drain < workers < stop < offline
