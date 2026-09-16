"""Legacy scanner progress lines must not expose source filenames."""
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

import scanner


SENSITIVE_NAME = "Acquisition-secret-board-list.html"
OPAQUE_DOC = "opaque-file-123"


@pytest.fixture
def isolated_analysis(monkeypatch, tmp_path):
    (tmp_path / SENSITIVE_NAME).write_text("<html><title>Fixture</title></html>")
    rubric = SimpleNamespace(disabled=set(), assess=lambda succeeded, issues, errors: {
        "status": "certifiable" if succeeded else "error", "score": 100,
        "compliant": bool(succeeded), "skipped_rules": len(errors),
    })
    monkeypatch.setattr(scanner.Rubric, "load_active", lambda *_: rubric)
    monkeypatch.setattr(scanner, "_sc_enabled", lambda *_: False)
    monkeypatch.setattr(scanner, "detect_acp_stamp", lambda *_: False)
    monkeypatch.setattr(scanner, "_file_extent", lambda *_: {})
    monkeypatch.setattr(scanner, "_analyse_html", lambda *_: {
        "succeeded": True, "issues": [], "errors": [],
    })
    return tmp_path


def _assert_redacted(output, opaque=OPAQUE_DOC):
    lowered = output.lower()
    assert SENSITIVE_NAME.lower() not in lowered
    assert "acquisition" not in lowered
    assert "secret" not in lowered
    if opaque:
        assert opaque in output


def test_success_progress_uses_opaque_document_identifier(isolated_analysis, capsys):
    result, _ = scanner.analyse_and_assess(
        isolated_analysis, SENSITIVE_NAME, doc_ref=OPAQUE_DOC)

    assert result["status"] == "certifiable"
    output = capsys.readouterr().out
    _assert_redacted(output)
    assert "analysing opaque-file-123 (.html)" in output
    assert "opaque-file-123: 0 finding(s)" in output


def test_failure_progress_and_structured_event_remain_opaque(
        isolated_analysis, monkeypatch, capsys):
    def fail(*_):
        raise RuntimeError("synthetic engine failure")
    monkeypatch.setattr(scanner, "_analyse_html", fail)

    with pytest.raises(RuntimeError, match="synthetic engine failure"):
        scanner.analyse_and_assess(
            isolated_analysis, SENSITIVE_NAME, doc_ref=OPAQUE_DOC)

    output = capsys.readouterr().out
    _assert_redacted(output)
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    assert any(record.get("event") == "stage.error"
               and record.get("stage") == "analyse.html"
               and record.get("doc") == OPAQUE_DOC
               and record.get("error_type") == "RuntimeError"
               for record in records)


def test_scanned_pdf_progress_uses_same_opaque_identifier(
        isolated_analysis, monkeypatch, capsys):
    pdf_name = SENSITIVE_NAME.removesuffix(".html") + ".pdf"
    (isolated_analysis / pdf_name).write_bytes(b"synthetic")
    monkeypatch.setattr(scanner, "_analyse_pdf", lambda *_: {
        "succeeded": True, "issues": [], "errors": [],
    })
    monkeypatch.setattr(scanner, "_sc_enabled", lambda code: code == "1.1.1")
    monkeypatch.setattr(scanner, "_collapse_duplicate_alt", lambda rows: rows)
    monkeypatch.setattr(scanner, "_collapse_reading_order", lambda rows: rows)
    monkeypatch.setitem(sys.modules, "pdf_vision_assess", SimpleNamespace(
        enabled=lambda: True, is_scanned_pdf=lambda *_: True,
        extract_layout=lambda *_args, **_kwargs: [{"page": 1}],
    ))
    monkeypatch.setitem(sys.modules, "pdf_vision_review", SimpleNamespace(
        findings_from_layouts=lambda *_args, **_kwargs: [{"ruleId": "1.1.1"}],
    ))
    import core
    monkeypatch.setattr(core, "store", SimpleNamespace(
        save_scanned_pdf_layout=lambda *_args, **_kwargs: None,
    ))
    import assessment_selection
    monkeypatch.setattr(assessment_selection, "filter_findings", lambda rows: rows)
    monkeypatch.setattr(assessment_selection, "allowed_rules", lambda: None)

    scanner.analyse_and_assess.__wrapped__(
        isolated_analysis, pdf_name, scan_id="synthetic-scan", doc_ref=OPAQUE_DOC)

    output = capsys.readouterr().out
    _assert_redacted(output)
    assert "scanned-PDF Tier A: opaque-file-123" in output
    assert "scanned-PDF Tier B: opaque-file-123" in output


@pytest.mark.parametrize("failure", ["exit", "timeout", "launch", "output"])
def test_office_cli_diagnostics_allowlist_metadata_only(
        tmp_path, monkeypatch, capsys, failure):
    name = "Acquisition-secret-board-list.docx"
    (tmp_path / name).write_bytes(b"synthetic")
    sentinel = f"private detail mentions {name} and {tmp_path}"
    if failure == "exit":
        monkeypatch.setattr(scanner.subprocess, "run", lambda *_args, **_kwargs:
                            SimpleNamespace(returncode=9, stderr=sentinel, stdout=sentinel))
    elif failure == "timeout":
        monkeypatch.setattr(scanner.subprocess, "run", lambda *_args, **_kwargs:
                            (_ for _ in ()).throw(subprocess.TimeoutExpired(sentinel, 1)))
    elif failure == "launch":
        monkeypatch.setattr(scanner.subprocess, "run", lambda *_args, **_kwargs:
                            (_ for _ in ()).throw(OSError(sentinel)))
    else:
        (tmp_path / "_o.json").write_text("{not json")
        monkeypatch.setattr(scanner.subprocess, "run", lambda *_args, **_kwargs:
                            SimpleNamespace(returncode=0, stderr="", stdout=""))

    scanner._analyse_office(tmp_path)

    output = capsys.readouterr().out
    _assert_redacted(output, opaque=None)
    assert str(tmp_path) not in output
    assert sentinel not in output
    assert "doc=h:" in output


def test_unreadable_docx_diagnostic_hashes_reported_filename(tmp_path, monkeypatch, capsys):
    name = "Acquisition-secret-board-list.docx"
    (tmp_path / name).write_bytes(b"synthetic")
    monkeypatch.setattr(scanner, "_docx_body_readable", lambda *_: False)

    scanner._flag_unreadable_docx(tmp_path, {name: {"errors": []}})

    output = capsys.readouterr().out
    _assert_redacted(output, opaque=None)
    assert "h:" in output
