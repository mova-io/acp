"""R-B3 — reviewer verdicts recorded on EARLIER scans of the same document (api/report_history.py).

Earlier verdicts are evidence about the document's past, never decisions about this scan's
changes. So the read is held to three rules, each of which a fixture below can break:

  * the SAME document, by the rule previous_assessment_for_file uses (provider id, else id-less
    path), the same owner and the same source — another owner's verdict on the same provider file
    is not this reviewer's history;
  * strictly EARLIER scans — not the current scan, not a later one, not one with an equal instant;
  * bounded, with the total stated, so a truncated list never reads as the whole history.

And it writes nothing: no verdict is copied to the new scan and none becomes current.
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
PREFIX = "change_review:"

HOOKS_LANDED = hasattr(store_mod.Store, "change_reviews_for_document")
HOOK_REASON = ("store.py hook from /tmp/acp-vr-requests.md 'G -> D' (R-B3) has not landed: "
               "Store.change_reviews_for_document")


def _scan(store, sid, files, *, owner=OWNER, source="gdrive",
          completed="2026-01-01T00:00:00+00:00"):
    store.save_scan({"_scan_id": sid, "started_at": "2025-12-31T00:00:00+00:00",
                     "completed_at": completed, "source": source, "owner": owner,
                     "rubric": {"name": "wcag-aa", "hash": "h1"},
                     "summary": {"files": len(files), "certifiable": 0, "uncertain": len(files),
                                 "error": 0, "avg_score": 70},
                     "files": [{"file": name, "engine": ".net/office", "status": "uncertain",
                                "score": 70, "compliant": 0, "skipped_rules": 0, "issues": [],
                                "drive_file_id": drive, "checksum": f"md5-{sid}"}
                               for name, drive in files]})


def _verdict(store, sid, name, change_id, *, owner=OWNER, when="2026-01-02T00:00:00+00:00",
             verdict="accepted"):
    value = json.dumps({"change_id": change_id, "verdict": verdict, "reviewer": owner,
                        "at": when, "artifact_sha256": "a" * 64}, sort_keys=True)
    store.save_decision(sid, name, PREFIX + change_id, value, owner, when)


def read(store, sid="s-new", name="new-name.docx", owner=OWNER, limit=200):
    return rh.change_reviews_for_document(store, sid, name, owner=owner, limit=limit)


@pytest.fixture()
def s(isolated_store):
    st = isolated_store
    _scan(st, "s-old", [("old-name.docx", "d1")], completed="2026-01-01T00:00:00+00:00")
    _verdict(st, "s-old", "old-name.docx", "1.1.1:0")
    _scan(st, "s-new", [("new-name.docx", "d1")], completed="2026-03-01T00:00:00+00:00")
    return st


def test_a_renamed_document_brings_its_earlier_verdict_by_provider_id(s):
    got = read(s)
    assert got["total"] == 1 and got["truncated"] is False
    [row] = got["decisions"]
    assert (row["scan_id"], row["file"], row["change_id"]) == ("s-old", "old-name.docx", "1.1.1:0")
    assert row["kind"] == PREFIX + "1.1.1:0"
    assert json.loads(row["value"])["verdict"] == "accepted"
    assert row["ts"] == "2026-01-02T00:00:00+00:00"
    assert row["scan_at"] == "2026-01-01T00:00:00+00:00"


def test_foreign_owner_and_other_source_verdicts_are_not_this_documents_history(s):
    _scan(s, "s-foreign", [("new-name.docx", "d1")], owner=OTHER,
          completed="2026-02-01T00:00:00+00:00")
    _verdict(s, "s-foreign", "new-name.docx", "1.1.1:9", owner=OTHER)
    _scan(s, "s-sp", [("new-name.docx", "d1")], source="sharepoint",
          completed="2026-02-01T00:00:00+00:00")
    _verdict(s, "s-sp", "new-name.docx", "1.1.1:8")
    # A verdict row on MY earlier scan written under another owner's name is not mine either.
    _verdict(s, "s-old", "old-name.docx", "1.1.1:7", owner=OTHER)
    got = read(s)
    assert [d["change_id"] for d in got["decisions"]] == ["1.1.1:0"]
    assert read(s, owner=OTHER) is None, "another owner cannot read through my scan"


def test_only_strictly_earlier_scans_count(s):
    _scan(s, "s-equal", [("new-name.docx", "d1")], completed="2026-03-01T00:00:00+00:00")
    _verdict(s, "s-equal", "new-name.docx", "equal:0")
    _scan(s, "s-later", [("new-name.docx", "d1")], completed="2026-05-01T00:00:00+00:00")
    _verdict(s, "s-later", "new-name.docx", "later:0")
    _verdict(s, "s-new", "new-name.docx", "current:0")        # this scan's own verdict
    assert [d["scan_id"] for d in read(s)["decisions"]] == ["s-old"]


def test_other_decision_kinds_and_other_documents_are_excluded(s):
    s.save_decision("s-old", "old-name.docx", "triage", "inscope", OWNER,
                    "2026-01-03T00:00:00+00:00")
    _scan(s, "s-other-doc", [("new-name.docx", "d2")], completed="2026-02-01T00:00:00+00:00")
    _verdict(s, "s-other-doc", "new-name.docx", "otherdoc:0")
    assert [d["change_id"] for d in read(s)["decisions"]] == ["1.1.1:0"]


def test_a_path_only_document_matches_only_id_less_rows(isolated_store):
    st = isolated_store
    _scan(st, "p-old", [("plain.docx", None)], completed="2026-01-01T00:00:00+00:00")
    _verdict(st, "p-old", "plain.docx", "p:0")
    _scan(st, "p-idd", [("plain.docx", "d5")], completed="2026-02-01T00:00:00+00:00")
    _verdict(st, "p-idd", "plain.docx", "p:1")
    _scan(st, "p-new", [("plain.docx", None)], completed="2026-03-01T00:00:00+00:00")
    got = read(st, sid="p-new", name="plain.docx")
    assert [d["change_id"] for d in got["decisions"]] == ["p:0"]


def test_newest_first_bounded_and_the_total_is_stated(s):
    for n in range(5):
        _verdict(s, "s-old", "old-name.docx", f"extra:{n}", when=f"2026-01-1{n}T00:00:00+00:00")
    got = read(s, limit=2)
    assert got["total"] == 6 and got["returned"] == 2 and got["limit"] == 2
    assert got["truncated"] is True
    assert [d["change_id"] for d in got["decisions"]] == ["extra:4", "extra:3"]
    whole = read(s)
    assert whole["truncated"] is False and whole["returned"] == 6
    assert [d["ts"] for d in whole["decisions"]] == sorted(
        (d["ts"] for d in whole["decisions"]), reverse=True)


def test_limit_is_validated_and_capped(s):
    for bad in (0, -1, True, "5", None):
        with pytest.raises(ValueError):
            read(s, limit=bad)
    assert read(s, limit=10 ** 6)["limit"] == rh.MAX_DECISIONS


def test_reading_writes_nothing_and_makes_nothing_current(s):
    def decisions():
        with s._db.cursor() as cur:
            s._db.execute(cur, "SELECT * FROM scan_decisions ORDER BY scan_id,file,kind")
            return s._db.fetchall(cur)
    before = decisions()
    assert read(s)["total"] == 1
    assert decisions() == before
    assert s.get_change_reviews("s-new", "new-name.docx", owner=OWNER) == {}


def test_unknown_scan_reads_none(s):
    assert read(s, sid="no-such-scan") is None


# ── through the Store, once D has pasted the hook ─────────────────────────────────────────

@pytest.mark.skipif(not HOOKS_LANDED, reason=HOOK_REASON)
def test_store_method_is_the_module_read(s):
    assert s.change_reviews_for_document("s-new", "new-name.docx", owner=OWNER) == read(s)
    assert s.change_reviews_for_document("s-new", "new-name.docx", owner=OWNER,
                                         limit=1)["limit"] == 1
    assert s.change_reviews_for_document("s-new", "new-name.docx", owner=OTHER) is None
