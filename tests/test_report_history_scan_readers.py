"""R-B4 — scan-wide readers for the per-file report facts inputs (api/report_history.py).

After R-B1 a scan-index build still paid get_remediation_diffs and get_change_reviews once per
document. These two reads answer the whole scan at once and must hand every document EXACTLY what
its per-file read hands it — including the v59 location projection (recorded locator/page, the
legacy-note reconstruction, and the at-cap note that must stay unknown), which is why the scan read
calls store._with_diff_location rather than repeating it.

Equality is asserted against the real per-file readers over a 50-document scan with every shape of
row the projection distinguishes, plus a statement-count bound that does not move with the number
of documents. Synthetic data only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ACP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACP / "api"))

import report_history as rh   # noqa: E402
import store as store_mod     # noqa: E402

OWNER = "owner@example.test"
OTHER = "someone-else@example.test"
SID = "s-scanwide"

HOOKS_LANDED = all(hasattr(store_mod.Store, name) for name in
                   ("remediation_diffs_for_scan", "change_reviews_for_scan"))
HOOK_REASON = ("store.py hooks from /tmp/acp-vr-requests.md 'G -> D R-B4' have not landed: "
               "Store.remediation_diffs_for_scan / Store.change_reviews_for_scan")


def _name(i):
    return f"doc-{i:04d}.docx"


def build(store, n: int, *, sid=SID, owner=OWNER) -> list[str]:
    names = [_name(i) for i in range(n)]
    store.save_scan({"_scan_id": sid, "started_at": "2026-09-01T00:00:00+00:00",
                     "completed_at": "2026-09-01T01:00:00+00:00", "source": "gdrive",
                     "owner": owner, "rubric": {"name": "wcag-aa", "hash": "h1"},
                     "summary": {"files": n, "certifiable": 0, "uncertain": n, "error": 0,
                                 "avg_score": 70},
                     "files": [{"file": name, "engine": ".net/office", "status": "uncertain",
                                "score": 70, "compliant": 0, "skipped_rules": 0, "issues": [],
                                "checksum": f"md5-{name}"} for name in names]})
    legacy = store_mod.Store._LEGACY_LOCATION_NOTE_PREFIXES[0]
    cap = store_mod.Store._DIFF_NOTE_MAX
    for i, name in enumerate(names):
        kind = i % 6
        if kind == 5:
            continue                                  # no verified change at all
        diffs = [
            # recorded location (v59 columns), PDF page
            {"rule_id": "1.1.1", "before": "", "after": f"Alt {i}a",
             "note": "vision", "locator": f"pdf:figure:{i}", "page": 1 + i % 3},
            # legacy: location only in the reviewer note
            {"rule_id": "1.1.1", "before": "", "after": f"Alt {i}b",
             "note": legacy + f"docx:image:{i}"},
            # a note at the storage cap: may be cut short, so its location stays unknown
            {"rule_id": "2.4.2", "before": "old", "after": "New title",
             "note": (legacy + "docx:x:" + "y" * cap)},
            # nothing known
            {"rule_id": "1.3.1", "before": "a", "after": "b", "note": ""},
        ][: kind + 1]
        # Application order deliberately not sorted by rule, so ORDER BY rule_id, seq matters.
        store.record_remediation_diffs(sid, name, list(reversed(diffs)))
        for k in range(i % 4):
            value = json.dumps({"change_id": f"1.1.1:{k}", "verdict": "accepted",
                                "reviewer": owner}, sort_keys=True)
            store.save_decision(sid, name, f"change_review:1.1.1:{k}", value, owner,
                                f"2026-09-02T00:00:0{k}+00:00")
        # Not change reviews, and not this owner's: excluded by both reads.
        store.save_decision(sid, name, "triage", "inscope", owner, "2026-09-02T00:00:00+00:00")
        if i % 7 == 0:
            store.save_decision(sid, name, "change_review:foreign:0", "{}", OTHER,
                                "2026-09-02T00:00:00+00:00")
    return names


def _count(store, monkeypatch):
    calls = []
    original = store._db.execute

    def counting(cur, sql, params=()):
        calls.append(sql)
        return original(cur, sql, params)
    monkeypatch.setattr(store._db, "execute", counting)
    return calls


def test_diffs_for_scan_equal_the_per_file_reader_for_every_file(isolated_store):
    s = isolated_store
    names = build(s, 50)
    got = rh.remediation_diffs_for_scan(s, SID)
    sources = set()
    for name in names:
        per_file = s.get_remediation_diffs(SID, name)
        assert got.get(name, []) == per_file, name
        sources.update(row["location_source"] for row in per_file)
    assert sources == {"recorded", "legacy_note", None}, "fixture must exercise every projection"
    assert all(got[n] for n in got), "a file with no verified change must be absent, not []"
    # Not the capped list: every row of the scan is present.
    assert sum(len(v) for v in got.values()) == sum(len(s.get_remediation_diffs(SID, n))
                                                   for n in names)


def test_reviews_for_scan_equal_the_per_file_reader_for_every_file(isolated_store):
    s = isolated_store
    names = build(s, 50)
    got = rh.change_reviews_for_scan(s, SID, OWNER)
    for name in names:
        assert got.get(name, {}) == s.get_change_reviews(SID, name, owner=OWNER), name
        assert got.get(name, {}) == s.get_change_reviews(SID, name, owner=OWNER)
    assert any(len(v) == 3 for v in got.values())
    foreign = rh.change_reviews_for_scan(s, SID, OTHER)
    for name in names:
        assert foreign.get(name, {}) == s.get_change_reviews(SID, name, owner=OTHER)
    assert all("change_review:foreign:0" in v for v in foreign.values())


def test_owner_is_required_and_scopes_the_diff_read(isolated_store):
    s = isolated_store
    build(s, 6)
    for bad in (None, "", 0):
        with pytest.raises(ValueError):
            rh.change_reviews_for_scan(s, SID, bad)
    assert rh.remediation_diffs_for_scan(s, SID, owner=OTHER) == {}
    assert rh.remediation_diffs_for_scan(s, "no-such-scan", owner=OWNER) == {}
    assert rh.remediation_diffs_for_scan(s, SID, owner=OWNER) == rh.remediation_diffs_for_scan(s, SID)


def test_other_scans_do_not_leak_in(isolated_store):
    s = isolated_store
    build(s, 10)
    build(s, 10, sid="s-other-scan")
    got = rh.remediation_diffs_for_scan(s, SID)
    for name, rows in got.items():
        assert rows == s.get_remediation_diffs(SID, name)
    assert rh.change_reviews_for_scan(s, SID, OWNER) == {
        n: s.get_change_reviews(SID, n, owner=OWNER) for n in got
        if s.get_change_reviews(SID, n, owner=OWNER)}


def test_files_filter_with_chunking_gives_the_same_answer(isolated_store, monkeypatch):
    s = isolated_store
    names = build(s, 50)
    whole_d = rh.remediation_diffs_for_scan(s, SID)
    whole_r = rh.change_reviews_for_scan(s, SID, OWNER)
    monkeypatch.setattr(rh, "CHUNK", 7)
    wanted = names[:31] + ["not-in-scan.docx", names[0]]
    assert rh.remediation_diffs_for_scan(s, SID, files=wanted) == {
        k: v for k, v in whole_d.items() if k in wanted}
    assert rh.change_reviews_for_scan(s, SID, OWNER, files=wanted) == {
        k: v for k, v in whole_r.items() if k in wanted}


def test_statement_count_is_bounded_and_independent_of_file_count(isolated_store, monkeypatch,
                                                                  tmp_path):
    counts = {}
    for n in (50, 300):
        monkeypatch.setattr(store_mod, "_SQLITE_PATH", tmp_path / f"n{n}.db")
        s = store_mod.Store()
        names = build(s, n)
        calls = _count(s, monkeypatch)
        rh.remediation_diffs_for_scan(s, SID)
        rh.remediation_diffs_for_scan(s, SID, owner=OWNER)
        rh.change_reviews_for_scan(s, SID, OWNER)
        rh.remediation_diffs_for_scan(s, SID, files=names)
        rh.change_reviews_for_scan(s, SID, OWNER, files=names)
        counts[n] = len(calls)
        # 1 + 2 (owner check) + 1 + 1 chunk + 1 chunk, whatever n is (n <= CHUNK).
        assert counts[n] == 6, (n, calls)
        calls.clear()
        for name in names:                      # what the per-file build paid
            s.get_remediation_diffs(SID, name)
            s.get_change_reviews(SID, name, owner=OWNER)
        assert len(calls) == 2 * n
    assert counts[50] == counts[300]


# ── through the Store, once D has pasted the hooks ────────────────────────────────────────

@pytest.mark.skipif(not HOOKS_LANDED, reason=HOOK_REASON)
def test_store_methods_are_the_module_reads(isolated_store):
    s = isolated_store
    names = build(s, 50)
    assert s.remediation_diffs_for_scan(SID) == rh.remediation_diffs_for_scan(s, SID)
    assert s.remediation_diffs_for_scan(SID, owner=OTHER) == {}
    assert s.change_reviews_for_scan(SID, OWNER) == rh.change_reviews_for_scan(s, SID, OWNER)
    reviews = s.change_reviews_for_scan(SID, OWNER, files=names[:5])
    assert set(reviews) <= set(names[:5])
