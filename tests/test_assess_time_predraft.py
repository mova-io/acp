"""Assess-time pre-draft: _propose_text_findings runs during _analyse_and_persist_one_impl.

Before this change, HITL review cards for image alt-text were blank until remediation
ran — the only call to `_propose_text_findings` was inside `_remediate_file`. A reviewer
opening a card immediately after Assess saw no draft and had to click the on-demand
/ai/suggest button per image.

The fix wires `_propose_text_findings` into `_analyse_and_persist_one_impl` (the per-file
scan worker), so proposals — including AI image descriptions via `describe_image_structured`
with its local → cloud escalation — are stored in `hitl_queue.proposals` at scan time.

The assertions here are the wiring contract:
  - Called for fresh downloads of .docx / .pptx / .xlsx / .pdf when AI is enabled.
  - NOT called for dedup/reuse paths (no bytes in tmp).
  - NOT called when AI is globally disabled.
  - NOT called for unsupported formats (.html, .csv, …).
  - NOT called when the file status is error / skipped / unanalysable.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ACP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACP / "api"))

import handlers  # noqa: E402
import core      # noqa: E402


# ── shared stubs ────────────────────────────────────────────────────────────────────────

FAKE_BYTES = b"PK\x03\x04" + b"\x00" * 100  # minimal zip-like sentinel

_NOW = "2026-09-04T00:00:00Z"
_SCAN_ID = "scan-assess-predraft"

# lf stub: every method is a no-op; span objects support .end()
_SPAN = SimpleNamespace(end=lambda **kw: None)
_LF = SimpleNamespace(
    file_trace=lambda *a, **kw: _SPAN,
    discover_span=lambda *a, **kw: _SPAN,
    pii_span=lambda *a, **kw: None,
    file_error_span=lambda *a, **kw: None,
    flush=lambda: None,
)


@pytest.fixture(autouse=True)
def isolate_lifecycle_boundary(monkeypatch):
    # These wiring fixtures have no durable scan/inventory. Lifecycle ownership and terminal
    # behavior are exercised against real Store rows in test_queued_terminal_content_gate.
    monkeypatch.setattr(handlers, "_queued_terminal_exclusion", lambda *args: None)


def _patch_all(monkeypatch, *, ai_enabled=True, dedup=None, status="done"):
    """Monkeypatch the minimal set of collaborators for _analyse_and_persist_one_impl.

    Returns a list that collects (scan_id, filename) tuples each time
    _propose_text_findings is called.
    """
    import scanner
    import stage_timing
    import documents

    predraft_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(handlers, "_propose_text_findings",
                        lambda sid, fname, data, ai: predraft_calls.append((sid, fname)))

    # scanner
    def _fake_download(it, tmp, svc, sp_token=None):
        (tmp / it["name"]).write_bytes(FAKE_BYTES)

    monkeypatch.setattr(scanner, "_download", _fake_download)
    monkeypatch.setattr(scanner, "read_cached_source", lambda *a, **kw: None)
    monkeypatch.setattr(scanner, "cache_source_bytes", lambda *a, **kw: None)
    monkeypatch.setattr(scanner, "analyse_and_assess",
                        lambda tmp, name, **kw: (
                            {"file": name, "engine": "test", "status": status,
                             "score": 80, "compliant": 1, "skipped_rules": 0, "issues": []},
                            None))

    # store
    monkeypatch.setattr(core.store, "find_by_checksum", lambda *a, **kw: dedup)
    monkeypatch.setattr(core.store, "find_prior_analysis", lambda *a, **kw: dedup)
    monkeypatch.setattr(core.store, "save_file_result", lambda *a, **kw: None)
    monkeypatch.setattr(core.store, "record_file_timing", lambda *a, **kw: None)
    monkeypatch.setattr(core.store, "get_document", lambda *a: None)
    monkeypatch.setattr(core.store, "upsert_document", lambda *a, **kw: None)
    monkeypatch.setattr(core.store, "get_ai_enabled", lambda: ai_enabled)

    # handlers utilities
    monkeypatch.setattr(handlers, "_emit_realtime_file_assess", lambda *a, **kw: None)
    monkeypatch.setattr(handlers, "_assess_level", lambda *a: "summary")
    monkeypatch.setattr(handlers, "drive_download_halted", lambda *a: None)

    # stage_timing
    class FakeTimings:
        def add(self, *a): pass
        def as_dict(self): return {}
    monkeypatch.setattr(stage_timing, "ScanTimings", FakeTimings)

    # documents
    monkeypatch.setattr(documents, "resolve_doc_id", lambda *a, **kw: "doc1")
    monkeypatch.setattr(documents, "compute_triage_score", lambda **kw: (50, "ok"))

    return predraft_calls


def _run(filename, monkeypatch, **patch_kw):
    """Run _analyse_and_persist_one_impl for `filename` and return predraft_calls."""
    calls = _patch_all(monkeypatch, **patch_kw)
    item = {"file": filename, "drive_file_id": "fid1"}
    handlers._analyse_and_persist_one_impl(
        _SCAN_ID, item, "drive", False, None, {}, _NOW, _LF)
    return calls


# ── golden path ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("filename", [
    "report.docx",
    "slides.pptx",
    "budget.xlsx",
    "contract.pdf",
])
def test_predraft_runs_for_supported_formats(filename, monkeypatch):
    calls = _run(filename, monkeypatch)
    assert calls == [(_SCAN_ID, filename)], (
        f"_propose_text_findings must be called once at scan time for {filename}")


# ── negative: format not supported ──────────────────────────────────────────────────────

@pytest.mark.parametrize("filename", [
    "page.html",
    "data.csv",
    "image.png",
    "archive.zip",
])
def test_predraft_skipped_for_unsupported_formats(filename, monkeypatch):
    calls = _run(filename, monkeypatch)
    assert calls == [], (
        f"_propose_text_findings must not run at scan time for {filename}")


# ── negative: AI globally disabled ─────────────────────────────────────────────────────

def test_predraft_skipped_when_ai_disabled(monkeypatch):
    calls = _run("report.docx", monkeypatch, ai_enabled=False)
    assert calls == [], "_propose_text_findings must not run when AI is globally disabled"


# ── negative: file in error state ──────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_status", ["error", "skipped", "unanalysable"])
def test_predraft_skipped_for_bad_status(bad_status, monkeypatch):
    calls = _run("report.docx", monkeypatch, status=bad_status)
    assert calls == [], (
        f"_propose_text_findings must not run when file status is '{bad_status}'")


# ── negative: dedup / reuse path ───────────────────────────────────────────────────────

def test_predraft_skipped_for_dedup(monkeypatch):
    """Dedup paths never write bytes to tmp, so the pre-draft is skipped to avoid
    a read of a non-existent file."""
    dedup_result = {"file": "report.docx", "engine": "test", "status": "done",
                    "score": 80, "compliant": 1, "skipped_rules": 0, "issues": [],
                    "dedup_of": None, "reused_from_scan": None, "pii": None}

    import scanner
    import stage_timing
    import documents

    predraft_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(handlers, "_propose_text_findings",
                        lambda sid, fname, data, ai: predraft_calls.append((sid, fname)))

    monkeypatch.setattr(core.store, "find_by_checksum", lambda *a, **kw: dict(dedup_result))
    monkeypatch.setattr(core.store, "find_prior_analysis", lambda *a, **kw: None)
    monkeypatch.setattr(core.store, "save_file_result", lambda *a, **kw: None)
    monkeypatch.setattr(core.store, "record_file_timing", lambda *a, **kw: None)
    monkeypatch.setattr(core.store, "get_document", lambda *a: None)
    monkeypatch.setattr(core.store, "upsert_document", lambda *a, **kw: None)
    monkeypatch.setattr(core.store, "get_ai_enabled", lambda: True)
    monkeypatch.setattr(core.store, "scope_for_file", lambda *a, **kw: None)
    monkeypatch.setattr(core.store, "get_scan_scope", lambda *a, **kw: None)
    monkeypatch.setattr(handlers, "_emit_realtime_file_assess", lambda *a, **kw: None)
    monkeypatch.setattr(handlers, "_assess_level", lambda *a: "summary")

    class FakeTimings:
        def add(self, *a): pass
        def as_dict(self): return {}
    monkeypatch.setattr(stage_timing, "ScanTimings", FakeTimings)
    monkeypatch.setattr(documents, "resolve_doc_id", lambda *a, **kw: "doc1")
    monkeypatch.setattr(documents, "compute_triage_score", lambda **kw: (50, "ok"))

    try:
        import scanner as sc_mod
        monkeypatch.setattr(sc_mod, "rescore_reused", lambda *a, **kw: {})
    except (ImportError, AttributeError):
        pass

    item = {"file": "report.docx", "drive_file_id": "fid1", "checksum": "sha256-abc"}
    handlers._analyse_and_persist_one_impl(
        _SCAN_ID, item, "drive", False, None, {}, _NOW, _LF, incremental=False)

    assert predraft_calls == [], "dedup path must not call _propose_text_findings (no bytes in tmp)"


# ── structural: the call must survive _propose_text_findings raising ─────────────────

def test_predraft_exception_does_not_fail_the_scan(monkeypatch):
    """A crash in _propose_text_findings must not propagate — the scan must finalize."""
    import scanner
    import stage_timing
    import documents

    _patch_all(monkeypatch)
    monkeypatch.setattr(handlers, "_propose_text_findings",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("vision offline")))

    item = {"file": "report.docx", "drive_file_id": "fid1"}
    # Must not raise
    handlers._analyse_and_persist_one_impl(
        _SCAN_ID, item, "drive", False, None, {}, _NOW, _LF)
