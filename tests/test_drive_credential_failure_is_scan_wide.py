"""A Drive credential the scan cannot use is one fact, not N unreadable documents.

Observed live 2026-07-31 against the deployed build. Scan f529ed607a26 held a 77-file Drive
inventory awaiting Assess. Its per-user token had expired (registered 17:11 the previous day
against a 1-hour TTL), so the fan-out would have fallen back to ADC — and the deployed ADC
credential is a stock `gcloud auth application-default login` grant:

    openid  email  userinfo.email  cloud-platform  sqlservice.login

No Drive scope at all, so every Drive call returns 403 "Request had insufficient authentication
scopes". Introspected from inside the container against Google's tokeninfo, not inferred.

Two things were wrong with what would have happened next. Only 401 was classified, so the 403
would have been recorded verbatim and the operator would have read 77 unreadable documents
rather than one unusable credential. And nothing was scan-wide: each file ran its own download,
MediaIoBaseDownload retries five times, so the fan-out would have spent ~460 HTTP requests
establishing a fact the first file already knew.

What must NOT change: every file still persists a row. count_files_done() counts file_records
against scan_runs.files, so a file that never writes one leaves the scan permanently
unfinalized — the UI sits at N/M with no error, which is the "stuck scan" false alarm this
codebase produced three times in a single day.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

import handlers  # noqa: E402


class _HttpishError(Exception):
    """googleapiclient.errors.HttpError stringifies to its status + body; that string is all
    the classifier reads, so a stand-in reproduces it without the client library."""


SCOPE_403 = _HttpishError(
    '<HttpError 403 when requesting https://www.googleapis.com/drive/v3/files/155uhU3jEtMo'
    '?alt=json returned "Request had insufficient authentication scopes.". '
    'Details: "[{\'message\': \'Insufficient Permission\', \'domain\': \'global\', '
    '\'reason\': \'insufficientPermissions\'}]"'
)
EXPIRED_401 = _HttpishError(
    '<HttpError 401 when requesting https://www.googleapis.com/drive/v3/files/1abc?alt=json '
    'returned "Invalid Credentials".'
)
ONE_FILE_403 = _HttpishError(
    '<HttpError 403 when requesting https://www.googleapis.com/drive/v3/files/1xyz?alt=json '
    'returned "The user does not have sufficient permissions for this file.". '
    'Details: "[{\'reason\': \'insufficientFilePermissions\'}]"'
)


# ── the classifier ──

def test_the_403_that_broke_production_is_named_and_actionable():
    reason = handlers.drive_auth_failure(SCOPE_403)
    assert reason is not None, "a 403 insufficient-scope was not classified at all"
    assert "drive.readonly" in reason        # says which scope
    assert "restart" in reason.lower()       # says the secret only takes effect on restart
    assert "no file in this scan can be read" in reason


def test_an_expired_token_keeps_the_reason_it_already_had():
    assert "expired mid-scan" in handlers.drive_auth_failure(EXPIRED_401)


def test_one_file_the_credential_may_not_read_does_not_stop_the_scan():
    """403 insufficientFilePermissions is that file's own sharing. The rest of the estate is
    perfectly readable, and halting on it would turn one unshared document into a dead scan."""
    assert handlers.drive_auth_failure(ONE_FILE_403) is None


@pytest.mark.parametrize("exc", [
    ValueError("file exceeds the 150MB download cap — skipped to protect worker memory"),
    _HttpishError('<HttpError 500 when requesting … returned "Internal Error".'),
    _HttpishError('<HttpError 404 when requesting … returned "File not found: 1abc".'),
    RuntimeError("PDF parse failed"),
])
def test_an_ordinary_per_file_failure_is_never_scan_wide(exc):
    assert handlers.drive_auth_failure(exc) is None, f"{exc} would have halted the whole scan"


# ── the halt, and its lifecycle ──

def test_the_first_failure_halts_the_rest_and_says_why(isolated_store, monkeypatch):
    monkeypatch.setattr(handlers.core, "store", isolated_store)
    sid = "s-scope"
    assert handlers.drive_download_halted(sid) is None      # nothing halted to begin with

    handlers._stop_scan_downloads(sid, handlers.drive_auth_failure(SCOPE_403))

    halted = handlers.drive_download_halted(sid)
    assert halted and "drive.readonly" in halted
    # The halt is one audit entry about the SCAN, not one per file.
    kinds = [d["action"] for d in isolated_store.list_decisions(scan_id=sid)]
    assert kinds.count("scan.drive_unusable") == 1


def test_halting_twice_still_records_it_once(isolated_store, monkeypatch):
    """Every remaining file in a batch hits the same failure; the audit trail must not grow a
    row per file for a fact that is true once."""
    monkeypatch.setattr(handlers.core, "store", isolated_store)
    sid = "s-repeat"
    for _ in range(5):
        handlers._stop_scan_downloads(sid, handlers.drive_auth_failure(SCOPE_403))
    kinds = [d["action"] for d in isolated_store.list_decisions(scan_id=sid)]
    assert kinds.count("scan.drive_unusable") == 1


def test_a_re_run_retries_instead_of_trusting_a_stale_halt(isolated_store, monkeypatch):
    """Fix the credential, press Assess again on the same scan: the marker must be forgotten,
    or the fix looks like it did not work. _enqueue_analysis is the one choke point both the
    immediate path and the deferred Assess path pass through."""
    monkeypatch.setattr(handlers.core, "store", isolated_store)
    sid = "s-rerun"
    handlers._stop_scan_downloads(sid, handlers.drive_auth_failure(SCOPE_403))
    assert handlers.drive_download_halted(sid)

    handlers.clear_drive_stop(sid)
    assert handlers.drive_download_halted(sid) is None


def test_enqueue_analysis_clears_it_on_the_way_in(isolated_store, monkeypatch):
    monkeypatch.setattr(handlers.core, "store", isolated_store)
    sid = "s-enqueue"
    handlers._stop_scan_downloads(sid, handlers.drive_auth_failure(SCOPE_403))
    # No items → the empty-estate branch, which still has to have cleared the marker first.
    handlers._enqueue_analysis(sid, "drive", [], ai=False, pii=False, user=None,
                               incremental=True, exclude_remediated=False)
    assert handlers.drive_download_halted(sid) is None


def test_a_scan_that_never_failed_is_untouched(isolated_store, monkeypatch):
    """The overwhelmingly common case: no marker, no extra audit rows, downloads proceed."""
    monkeypatch.setattr(handlers.core, "store", isolated_store)
    sid = "s-healthy"
    assert handlers.drive_download_halted(sid) is None
    assert isolated_store.list_decisions(scan_id=sid) == []


def test_a_marker_that_cannot_be_written_does_not_take_the_scan_down(monkeypatch):
    """The halt is an optimisation and an explanation, never a dependency. If the settings
    write fails, the scan must carry on failing per-file exactly as it did before."""
    class _Broken:
        def get_setting(self, *a, **k): raise RuntimeError("db gone")
        def set_setting(self, *a, **k): raise RuntimeError("db gone")
        def log_decision(self, *a, **k): raise RuntimeError("db gone")

    monkeypatch.setattr(handlers.core, "store", _Broken())
    handlers._stop_scan_downloads("s-broken", "reason")     # must not raise
    assert handlers.drive_download_halted("s-broken") is None
    handlers.clear_drive_stop("s-broken")                   # must not raise


# ── the behaviour the halt exists for, driven through the real fan-out function ──

def _fan_out(store, monkeypatch, files, downloader):
    """Run _analyse_and_persist_one over `files` exactly as scan_batch does, with `downloader`
    standing in for scanner._download. Returns the names it actually tried to download."""
    import datetime as dt
    import scanner

    attempted = []

    def _dl(it, dest, svc=None, sp_token=None):
        attempted.append(it["name"])
        downloader(it)

    store.init_scan_run("s-fan", "drive", len(files), "2026-07-31T15:00:00Z", "default", "rh",
                        owner="auditor@example.com", status="running")
    store.add_inventory("s-fan", [{"file": f, "drive_file_id": f"id-{f}",
                                   "drive_account_id": "offline-account"} for f in files])
    monkeypatch.setattr(handlers.core, "store", store)
    monkeypatch.setattr(scanner, "_download", _dl)
    monkeypatch.setattr(scanner, "cache_source_bytes", lambda *a, **k: None)
    # **kw, not a fixed signature. This stub took (tmp, name, detect_pii) and the caller gained a
    # `scan_id=` kwarg for the progress line; the resulting TypeError was swallowed by handlers'
    # per-file `except Exception` and every document came back status="error". A stub that is
    # narrower than the real function turns an unrelated signature change into a fake
    # product failure, which is exactly what this test then reported.
    monkeypatch.setattr(scanner, "analyse_and_assess",
                        lambda tmp, name, **kw: ({
                            "file": name, "engine": "python/pdf", "status": "analysed",
                            "score": 90, "compliant": 1, "skipped_rules": 0, "issues": []}, None))

    # Tracing is not what this test is about, and lf's surface is wide — a permissive double
    # keeps the test pinned to the download behaviour rather than to Langfuse's API.
    from unittest.mock import MagicMock

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for f in files:
        handlers._analyse_and_persist_one(
            "s-fan", {"file": f, "drive_file_id": f"id-{f}", "drive_account_id": "offline-account"},
            "drive", False, object(), {}, now, MagicMock(), user="auditor@example.com",
            rubric_hash="rh", incremental=False)
    return attempted


def test_only_the_first_file_pays_for_an_unusable_credential(isolated_store, monkeypatch):
    """The whole point. Pre-fix all four would have been downloaded — and each retried five
    times inside MediaIoBaseDownload, so a 77-file estate spent ~460 requests learning one
    fact."""
    files = ["a.pdf", "b.pdf", "c.pdf", "d.pdf"]
    attempted = _fan_out(isolated_store, monkeypatch, files,
                         downloader=lambda it: (_ for _ in ()).throw(SCOPE_403))

    assert attempted == ["a.pdf"], f"downloads continued after the credential failed: {attempted}"

    # …and every file still has a row, so count_files_done() can complete and the scan is not
    # left at N/M forever.
    rows = {r["file"]: r for r in isolated_store.get_scan("s-fan")["files"]} \
        if isolated_store.get_scan("s-fan") else {}
    assert set(rows) == set(files), "a file without a row leaves the scan permanently unfinalized"
    assert all(r["status"] == "error" for r in rows.values())

    # The reason is on every file, not just the one that discovered it.
    details = [d["detail"] for d in isolated_store.list_decisions(scan_id="s-fan")
               if d["action"] == "scan.file_error"]
    assert len(details) == 4
    assert all("drive.readonly" in d for d in details)


def test_a_single_unreadable_file_does_not_stop_the_others(isolated_store, monkeypatch):
    """The guard must not turn one badly-shared document into a dead scan: b.pdf fails on its
    own permissions, and c/d are still downloaded and analysed."""
    def _one_bad(it):
        if it["name"] == "b.pdf":
            raise ONE_FILE_403

    attempted = _fan_out(isolated_store, monkeypatch, ["a.pdf", "b.pdf", "c.pdf", "d.pdf"],
                         downloader=_one_bad)
    assert attempted == ["a.pdf", "b.pdf", "c.pdf", "d.pdf"]

    rows = {r["file"]: r["status"] for r in isolated_store.get_scan("s-fan")["files"]}
    assert rows == {"a.pdf": "analysed", "b.pdf": "error",
                    "c.pdf": "analysed", "d.pdf": "analysed"}


def test_a_healthy_scan_downloads_everything(isolated_store, monkeypatch):
    attempted = _fan_out(isolated_store, monkeypatch, ["a.pdf", "b.pdf"],
                         downloader=lambda it: None)
    assert attempted == ["a.pdf", "b.pdf"]
    assert handlers.drive_download_halted("s-fan") is None
