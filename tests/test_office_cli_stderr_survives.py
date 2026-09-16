"""The office CLI's exception must survive the log line, and one file must not kill the batch.

Two defects, one root cause — a crash that was flagged twice and never diagnosed because the
evidence was destroyed on the way to the log:

1. `api/scanner.py` clipped the CLI's stderr to `[-400:]`. A .NET unhandled-exception trace puts
   the exception TYPE and MESSAGE on the first line and 30+ stack frames after it, so a tail-only
   window is guaranteed to contain nothing but frames. All 8 production crashes on 2026-07-30
   logged `office CLI exited -6: ne)` — a three-character fragment of a frame signature — across
   both analysers and both container apps. Nobody could act on that, twice.

2. `AcpScan.Cli/Program.cs` had no error handling at all, and `File.WriteAllText` runs AFTER the
   file loop. On Unix an unhandled managed exception makes the runtime print the trace and call
   abort(), so the process dies of SIGABRT — returncode -6 — and every file already analysed in
   that batch is discarded with it.

`-6` was read as evidence of a native fault or a .NET runtime-shutdown bug needing a Linux repro.
It is not. It is what this CLI did for ANY unhandled managed exception, on any platform —
verified by pointing it at a missing directory on macOS, which produced `-6` every time. That
removes the reason to wait for a container repro, and it is why the fix is a guard rather than a
hunt: `Program.cs` now exits with a status and a leading `FATAL: <type>: <message>` line.

WHAT THIS DOES NOT FIX, stated plainly. The specific exception production hits is still
unidentified. It escapes `AnalyseAsync` from the unguarded preamble around that method's own
try/catch, and no input reachable from a test provokes it — the demo fixtures exit 0 across
both analysers, with and without a legacy encoding declaration, and under
`DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1`. The customer documents that trigger it are never
retained (the scanner's own guarantee), so there is nothing local to reproduce against. What
these changes buy is that the NEXT occurrence identifies itself instead of being flagged a third
time: the type and message land in `_o.json` as a structured `ANALYSER_UNHANDLED` error if the
throw is inside the loop, and in the scanner's log line via `_clip_diag` if it is anywhere else.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ACP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACP / "api"))
sys.path.insert(0, str(ACP / "scripts"))

import scanner  # noqa: E402
from engines import NO_OFFICE, OFFICE_OK  # noqa: E402
from office_cli_abort_stub import (  # noqa: E402
    DOTNET_TRACE, TRACE_HEAD_MARKER, TRACE_MESSAGE_MARKER, TRACE_TAIL_MARKER,
)

STUB = Path(__file__).resolve().parent / "office_cli_abort_stub.py"
REAL_XLSX = ACP / "demo-fixtures" / "excel-accessibility-demo.xlsx"
REAL_DOCX = ACP / "demo-fixtures" / "word-accessibility-demo.docx"


@pytest.fixture
def office_dir(tmp_path: Path) -> Path:
    d = tmp_path / "scan"
    d.mkdir()
    (d / "book.xlsx").write_bytes(b"PK\x03\x04not-a-real-xlsx")
    return d


@pytest.fixture
def run_stub(monkeypatch):
    def _run(mode: str, dest: Path) -> dict:
        monkeypatch.setattr(scanner, "DOTNET", sys.executable)
        monkeypatch.setattr(scanner, "CLI_DLL", STUB)
        monkeypatch.setenv("ACP_STUB_MODE", mode)
        return scanner._analyse_office(dest)
    return _run


# ── the clip itself ───────────────────────────────────────────────────────────────

def test_clip_keeps_the_exception_type_and_message():
    """The regression, stated as directly as it can be: the head is what names the defect."""
    clipped = scanner._clip_diag(DOTNET_TRACE)
    assert TRACE_HEAD_MARKER in clipped
    assert TRACE_MESSAGE_MARKER in clipped


def test_clip_keeps_the_tail_it_always_kept():
    """Buying the head by dropping the tail would be a lateral move, not a fix.

    For a SIGABRT the runtime's last words arrive after the managed trace, and a native fault
    (`free(): invalid pointer`) is only ever visible there.
    """
    assert TRACE_TAIL_MARKER in scanner._clip_diag(DOTNET_TRACE)


def test_the_old_tail_only_clip_would_have_lost_the_type():
    """Pins that this test file is testing something — the trace must be long enough to matter.

    If a later edit shortens DOTNET_TRACE below the window, every assertion above would still
    pass while proving nothing, because a short trace survives any clip.
    """
    assert TRACE_HEAD_MARKER not in DOTNET_TRACE[-400:]
    assert len(DOTNET_TRACE) > 1200


def test_clip_marks_the_gap_rather_than_splicing_silently():
    clipped = scanner._clip_diag(DOTNET_TRACE)
    assert "elided" in clipped
    assert len(clipped) < len(DOTNET_TRACE)


def test_short_output_is_passed_through_whole():
    assert scanner._clip_diag("System.ArgumentException: boom") == "System.ArgumentException: boom"


# ── the log line an operator actually reads ───────────────────────────────────────

def test_log_line_reports_stream_presence_without_exception_detail(office_dir, run_stub, capsys):
    run_stub("abort_with_dotnet_trace", office_dir)
    log = capsys.readouterr().out
    assert "diagnostic_streams=stderr" in log
    assert TRACE_HEAD_MARKER not in log
    assert TRACE_MESSAGE_MARKER not in log
    assert TRACE_TAIL_MARKER not in log


def test_log_line_still_says_sigabrt_not_just_minus_six(office_dir, run_stub, capsys):
    """-6 is a signal number, and reading it as an exit status is what sent this investigation
    looking for a .NET shutdown bug. The log says both, so neither reading needs a lookup."""
    run_stub("abort_with_dotnet_trace", office_dir)
    log = capsys.readouterr().out
    assert "SIGABRT" in log
    assert "-6" in log


def test_stdout_is_read_when_stderr_is_empty(office_dir, run_stub, capsys):
    run_stub("abort_with_stdout_only", office_dir)
    log = capsys.readouterr().out
    assert "diagnostic_streams=stdout" in log
    assert TRACE_HEAD_MARKER not in log
    assert "stdout" in log


def test_both_streams_empty_is_said_plainly(office_dir, run_stub, capsys):
    """`abort_after_complete_write` writes nothing to either stream — the log must not imply
    it read something, and must not print a bare empty string after the colon."""
    run_stub("abort_after_complete_write", office_dir)
    log = capsys.readouterr().out
    assert "diagnostic_streams=none" in log


# ── the CLI contract these logs describe ──────────────────────────────────────────

@pytest.mark.skipif(not OFFICE_OK, reason=NO_OFFICE)
def test_a_failing_cli_exits_with_a_status_not_a_signal(tmp_path):
    """The Program.cs half of the fix, and the one that retires the `-6` class outright.

    A missing input directory is the cheapest reachable trigger: `Directory.GetFiles` throws
    before any file is touched. Unguarded — which is how this CLI shipped — that exception
    reached the runtime, which on Unix prints the trace and calls abort(), so the caller saw
    SIGABRT. This test asserts the returncode is a POSITIVE status now, which is the property
    that matters: `returncode < 0` means the process was killed, and a killed process is what
    nobody could diagnose. Asserting `== 2` as well so a future edit cannot satisfy it by
    picking some other non-zero code without saying so here.
    """
    proc = subprocess.run([scanner.DOTNET, str(scanner.CLI_DLL),
                           str(tmp_path / "no-such-dir"), str(tmp_path / "out.json")],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode > 0, f"the CLI died by signal {-proc.returncode}, not a status"
    assert proc.returncode == 2


@pytest.mark.skipif(not OFFICE_OK, reason=NO_OFFICE)
def test_a_fatal_error_leads_with_its_type_so_no_clip_can_lose_it(tmp_path):
    """Belt and braces with `_clip_diag`: the scanner keeps the head, and the CLI makes sure the
    head is the useful part. A one-line `FATAL: <type>: <message>` before the trace survives
    truncation by anything downstream, including log pipelines this repo does not control."""
    proc = subprocess.run([scanner.DOTNET, str(scanner.CLI_DLL),
                           str(tmp_path / "no-such-dir"), str(tmp_path / "out.json")],
                          capture_output=True, text=True, timeout=180)
    first = proc.stderr.splitlines()[0]
    assert first.startswith("FATAL: ")
    assert "DirectoryNotFoundException" in first


@pytest.mark.skipif(not OFFICE_OK, reason=NO_OFFICE)
def test_one_unreadable_file_does_not_discard_the_rest_of_the_batch(tmp_path):
    """The batch guarantee `File.WriteAllText`-after-the-loop puts at risk.

    Honest about its reach: a corrupt file is caught *inside* `AnalyseAsync`, so this passes on
    the pre-fix CLI too. It is here as the end-to-end contract — every file reported, the bad one
    marked unanalysable rather than silently absent — not as proof of the per-file catch. That
    catch guards the escape production actually hit, which is in the analyser preamble outside
    its own try, and no input reachable from here provokes it; see this file's module docstring.
    """
    d = tmp_path / "scan"
    d.mkdir()
    for f in (REAL_XLSX, REAL_DOCX):
        (d / f.name).write_bytes(f.read_bytes())
    (d / "corrupt.docx").write_bytes(b"this is not a zip archive at all")

    out = d / "_o.json"
    proc = subprocess.run([scanner.DOTNET, str(scanner.CLI_DLL), str(d), str(out)],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr

    reported = {r["file"]: r for r in json.loads(out.read_text())}
    assert set(reported) == {REAL_XLSX.name, REAL_DOCX.name, "corrupt.docx"}
    # The good files keep their findings...
    assert reported[REAL_XLSX.name]["succeeded"] is True
    assert reported[REAL_DOCX.name]["succeeded"] is True
    assert reported[REAL_XLSX.name]["issues"] and reported[REAL_DOCX.name]["issues"]
    # ...and the bad one is reported as unanalysable rather than as a clean pass.
    assert reported["corrupt.docx"]["succeeded"] is False
    assert reported["corrupt.docx"]["errors"]


@pytest.mark.skipif(not OFFICE_OK, reason=NO_OFFICE)
@pytest.mark.parametrize("fixture", [REAL_XLSX, REAL_DOCX], ids=["xlsx", "docx"])
def test_real_cli_exits_cleanly_on_both_analysers(tmp_path, fixture):
    """Both analysers, because production crashed in both — XlsxAnalyser 5 times and
    DocxAnalyser 3 in the same 12h window, which is what ruled out one bad file.

    This is the regression guard that fails loudly if the abort ever becomes reproducible on a
    fixture we hold: it reports the actual returncode and the head of stderr, not a swallowed line.
    """
    d = tmp_path / "scan"
    d.mkdir()
    (d / fixture.name).write_bytes(fixture.read_bytes())
    out = d / "_o.json"
    proc = subprocess.run([scanner.DOTNET, str(scanner.CLI_DLL), str(d), str(out)],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr[:1500]}"
    assert json.loads(out.read_text())[0]["succeeded"] is True
