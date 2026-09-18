"""R-B1 — one batched baseline read for a whole scan (api/report_history.py).

`previous_assessments_for_scan` must hand every file EXACTLY what the per-file read hands it —
same identity rule, same owner/source filter, same ordering, same run dict, same issue rows — in a
number of queries that does not grow with the number of files. The fixture is built to make each
of those rules able to fail:

  * a renamed document keeps its provider id, so it must match by id and NOT by name;
  * a document without a provider id matches by path only, and only against id-less rows;
  * another owner's newer scan and a different-source newer scan hold the same documents and
    must never be chosen;
  * a scan later than the current one must never be "previous";
  * one baseline instant is `assessed_at` while its `completed_at` is older (COALESCE order);
  * two earlier scans share one timestamp, inserted in the order that would let the query plan
    pick the loser, so only an explicit tie-break gives the stated winner.

Synthetic data only. Written straight into the three tables the read uses, so a 400-document
scan costs milliseconds rather than a full save_scan per file.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ACP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACP / "api"))

import report_history as rh   # noqa: E402
import store as store_mod     # noqa: E402

OWNER = "owner@example.test"
OTHER = "someone-else@example.test"
CUR = "s-cur"

HOOKS_LANDED = hasattr(store_mod.Store, "previous_assessments_for_scan")
DELEGATES = "report_history" in inspect.getsource(store_mod.Store.previous_assessment_for_file)
HOOK_REASON = ("store.py hooks from /tmp/acp-vr-requests.md 'G -> D' (R-B1) have not landed: "
               "Store.previous_assessments_for_scan / delegating previous_assessment_for_file")


def _run(store, sid, *, owner=OWNER, source="gdrive", completed=None, assessed=None,
         started="2025-01-01T00:00:00+00:00", scope=None, certifiable=None):
    with store._db.cursor() as cur:
        store._db.execute(cur,
            "INSERT INTO scan_runs(id,started_at,completed_at,assessed_at,source,rubric_name,"
            "rubric_hash,owner_email,status,scope,certifiable) "
            "VALUES(%s,%s,%s,%s,%s,'wcag-aa','h1',%s,'done',%s,%s)",
            (sid, started, completed, assessed, source, owner, scope, certifiable))


def _file(store, sid, name, *, drive=None, status="uncertain", issues=()):
    with store._db.cursor() as cur:
        store._db.execute(cur,
            "INSERT INTO file_records(scan_id,file,engine,status,score,compliant,skipped_rules,"
            "drive_file_id,checksum) VALUES(%s,%s,'.net/office',%s,70,0,0,%s,%s)",
            (sid, name, status, drive, f"md5-{sid}-{name}"))
        for rule, page, location in issues:
            store._db.execute(cur,
                "INSERT INTO issue_records(scan_id,file,rule_id,wcag,severity,detail,page,location) "
                "VALUES(%s,%s,%s,%s,'SERIOUS',%s,%s,%s)",
                (sid, name, rule, "1.1.1 Non-text Content", f"{rule} in {sid}", page, location))


def _issues(i, sid):
    # Several rows per file, deliberately inserted out of sort order, some with NULL page/location.
    n = i % 4
    return [(f"R{(n - k) % 4}", None if k == 1 else k + 1, None if k == 2 else f"docx:image:{k}")
            for k in range(n)]


def build(store, n: int, *, tie_order=("s-tie-b", "s-tie-a")) -> dict:
    """Earlier scans of an n-document current scan; returns the expected facts per file."""
    _run(store, "s-old", completed="2026-01-01T00:00:00+00:00", scope='{"scan_scope": ["1.1.1"]}')
    # assessed_at wins over an OLDER completed_at, so this run sorts at 2026-02-15.
    _run(store, "s-assessed", completed="2025-06-01T00:00:00+00:00",
         assessed="2026-02-15T00:00:00+00:00", certifiable=3)
    for sid in tie_order:     # equal instants; the insertion order must not decide
        _run(store, sid, completed="2026-02-01T00:00:00+00:00")
    _run(store, "s-foreign", owner=OTHER, completed="2026-03-01T00:00:00+00:00")
    _run(store, "s-sharepoint", source="sharepoint", completed="2026-03-01T00:00:00+00:00")
    _run(store, "s-future", completed="2026-06-01T00:00:00+00:00")
    _run(store, CUR, completed="2026-04-01T00:00:00+00:00")

    expected = {}
    for i in range(n):
        name = f"doc-{i:04d}.docx"
        drive = f"d{i}" if i % 2 == 0 else None
        _file(store, CUR, name, drive=drive)
        # Every document has an old baseline — renamed ones under their old name.
        old_name = f"old-{i:04d}.docx" if (drive and i % 5 == 0) else name
        _file(store, "s-old", old_name, drive=drive, issues=_issues(i, "s-old"))
        expected[name] = ("s-old", old_name)
        if i % 3 == 0:
            _file(store, "s-assessed", name, drive=drive, issues=_issues(i + 1, "s-assessed"))
            expected[name] = ("s-assessed", name)
        elif i % 7 == 1:
            for sid in tie_order:
                _file(store, sid, name, drive=drive, issues=_issues(i + 2, sid))
            expected[name] = ("s-tie-b", name)       # r.id DESC
        # Newer, but foreign / other source / later than the current scan: never chosen.
        for sid in ("s-foreign", "s-sharepoint", "s-future"):
            _file(store, sid, name, drive=drive, issues=[("R9", 1, "docx:image:9")])
    # A path-only document whose same NAME carries a provider id earlier: not the same document.
    _file(store, CUR, "pathonly.docx")
    _file(store, "s-old", "pathonly.docx", drive="d-unrelated")
    # Documents that exist earlier only for another owner / another source.
    _file(store, CUR, "only-foreign.docx", drive="d-foreign-only")
    _file(store, "s-foreign", "only-foreign.docx", drive="d-foreign-only")
    _file(store, CUR, "only-sharepoint.docx")
    _file(store, "s-sharepoint", "only-sharepoint.docx")
    return expected


@pytest.fixture()
def s(isolated_store):
    return isolated_store


def _original(store, file):
    """The per-file read as store.py implements it (before or after its delegation)."""
    return store.previous_assessment_for_file(CUR, file, owner=OWNER)


def _norm_issues(found):
    return sorted(found["issues"], key=rh.issue_sort_key) if found else None


@pytest.mark.parametrize("n", [40, 400])
def test_every_file_gets_exactly_the_per_file_answer(s, n):
    expected = build(s, n)
    got = rh.previous_assessments_for_scan(s, CUR, owner=OWNER)
    names = [f"doc-{i:04d}.docx" for i in range(n)]
    assert set(got) == set(names), "a file with a baseline is missing, or one without has one"
    for name in names:
        want_sid, want_file = expected[name]
        assert got[name]["file_row"]["scan_id"] == want_sid, name
        assert got[name]["file_row"]["file"] == want_file, name
        assert got[name]["file_row"]["run_id"] == want_sid
        assert got[name]["run"]["id"] == want_sid
        ref = _original(s, name)
        if expected[name][0] == "s-tie-b" and not DELEGATES:
            # The pre-hook per-file query has no tie-break; its winner is the plan's choice.
            continue
        assert got[name]["run"] == ref["run"], name
        assert got[name]["file_row"] == ref["file_row"], name
        # Issue ORDER is only defined once the per-file read delegates; the rows are identical.
        assert got[name]["issues"] == _norm_issues(ref), name
    for absent in ("pathonly.docx", "only-foreign.docx", "only-sharepoint.docx"):
        assert absent not in got
        assert _original(s, absent) is None


def test_the_run_dict_is_the_one_the_per_file_read_builds(s):
    build(s, 12)
    got = rh.previous_assessments_for_scan(s, CUR, owner=OWNER)
    old = got["doc-0002.docx"]["run"]
    assert old["scope"] == {"scan_scope": ["1.1.1"]} and old["scan_scope"] == ["1.1.1"]
    # _fill_run_aggregate filled the NULL counters rather than leaving them for a client to 0.
    assert isinstance(old["certifiable"], int) and isinstance(old["uncertain"], int)
    assessed = got["doc-0003.docx"]["run"]
    assert assessed["certifiable"] == 3 and assessed["scan_scope"] is None
    # Each file owns its dict: mutating one baseline cannot corrupt another's.
    old["scan_scope"] = "mutated"
    assert got["doc-0004.docx"]["run"]["scan_scope"] == ["1.1.1"]


def test_equal_timestamps_resolve_the_same_way_in_either_insertion_order(isolated_store,
                                                                          monkeypatch, tmp_path):
    winners = []
    for order in (("s-tie-b", "s-tie-a"), ("s-tie-a", "s-tie-b")):
        monkeypatch.setattr(store_mod, "_SQLITE_PATH", tmp_path / f"{order[0]}.db")
        store = store_mod.Store()
        build(store, 20, tie_order=order)
        got = rh.previous_assessments_for_scan(store, CUR, owner=OWNER)
        winners.append({k: v["file_row"]["scan_id"] for k, v in got.items()})
    assert winners[0] == winners[1]
    assert winners[0]["doc-0001.docx"] == "s-tie-b"


def _count(store, monkeypatch):
    calls = []
    original = store._db.execute

    def counting(cur, sql, params=()):
        calls.append(sql)
        return original(cur, sql, params)
    monkeypatch.setattr(store._db, "execute", counting)
    return calls


def test_query_count_does_not_grow_with_the_number_of_files(isolated_store, monkeypatch,
                                                             tmp_path):
    counts = {}
    for n in (40, 400):
        monkeypatch.setattr(store_mod, "_SQLITE_PATH", tmp_path / f"n{n}.db")
        store = store_mod.Store()
        build(store, n)
        calls = _count(store, monkeypatch)
        got = rh.previous_assessments_for_scan(store, CUR, owner=OWNER)
        counts[n] = len(calls)
        # Bound: scan row + identities + drive window + path window + runs + issues = 6 fixed,
        # plus _fill_run_aggregate for each DISTINCT baseline run (<= 3 statements each).
        runs = {v["run"]["id"] for v in got.values()}
        assert counts[n] <= 6 + 3 * len(runs), (n, counts[n], calls)
    assert counts[40] == counts[400], counts


def test_per_file_calls_would_have_cost_linear_queries(s, monkeypatch):
    """The reason for R-B1, measured: N per-file reads cost >= 4N statements."""
    build(s, 40)
    calls = _count(s, monkeypatch)
    for i in range(40):
        rh.previous_assessment_for_file(s, CUR, f"doc-{i:04d}.docx", owner=OWNER)
    assert len(calls) >= 4 * 40


def test_chunking_changes_the_statement_count_not_the_answer(s, monkeypatch):
    build(s, 60)
    whole = rh.previous_assessments_for_scan(s, CUR, owner=OWNER)
    names = sorted(whole) + ["pathonly.docx", "not-in-this-scan.docx"]
    monkeypatch.setattr(rh, "CHUNK", 7)
    chunked = rh.previous_assessments_for_scan(s, CUR, owner=OWNER, files=names)
    assert chunked == whole


def test_files_filter_and_a_name_the_scan_does_not_hold(s):
    build(s, 10)
    _file(s, "s-old", "not-in-this-scan.docx")
    got = rh.previous_assessments_for_scan(
        s, CUR, owner=OWNER, files=["doc-0000.docx", "doc-0001.docx", "not-in-this-scan.docx",
                                    "doc-0000.docx"])
    assert set(got) == {"doc-0000.docx", "doc-0001.docx", "not-in-this-scan.docx"}
    # The LEFT JOIN in the per-file read gives a name outside the scan path identity; same here.
    assert got["not-in-this-scan.docx"]["file_row"]["scan_id"] == "s-old"
    assert got["not-in-this-scan.docx"] == rh.previous_assessment_for_file(
        s, CUR, "not-in-this-scan.docx", owner=OWNER)


def test_foreign_owner_and_unknown_scan_read_nothing(s):
    build(s, 10)
    assert rh.previous_assessments_for_scan(s, CUR, owner=OTHER) == {}
    assert rh.previous_assessments_for_scan(s, "no-such-scan", owner=OWNER) == {}
    assert rh.previous_assessment_for_file(s, CUR, "doc-0000.docx", owner=OTHER) is None


def test_a_scan_without_a_source_has_no_baseline(s):
    """`r.source = NULL` matches nothing in SQL — the per-file read has always behaved so."""
    _run(s, "s-early", source=None, completed="2026-01-01T00:00:00+00:00")
    _run(s, "s-now", source=None, completed="2026-04-01T00:00:00+00:00")
    _file(s, "s-early", "a.docx")
    _file(s, "s-now", "a.docx")
    assert rh.previous_assessments_for_scan(s, "s-now", owner=OWNER) == {}


# ── through the Store, once D has pasted the hooks ────────────────────────────────────────

@pytest.mark.skipif(not (HOOKS_LANDED and DELEGATES), reason=HOOK_REASON)
@pytest.mark.parametrize("n", [40, 400])
def test_store_methods_share_one_selection(s, n):
    build(s, n)
    batched = s.previous_assessments_for_scan(CUR, owner=OWNER)
    assert batched == rh.previous_assessments_for_scan(s, CUR, owner=OWNER)
    names = [f"doc-{i:04d}.docx" for i in range(n)] + ["pathonly.docx", "only-foreign.docx"]
    for name in names:
        # Including the equal-timestamp files: the tie-break is now one rule for both reads.
        assert batched.get(name) == s.previous_assessment_for_file(CUR, name, owner=OWNER), name
    assert s.previous_assessments_for_scan(CUR, owner=OTHER) == {}
