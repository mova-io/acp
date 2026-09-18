"""R-B4: a scan-index build reads its three per-file inputs ONCE, and nothing about a row changes.

report_facts.prefetch_scan_inputs feeds the per-file builders from three scan-wide readers when
they exist:

  * `remediation_diffs_for_scan(scan_id, *, owner, files)`   (Stream G, Store wrapper pending)
  * `change_reviews_for_scan(scan_id, owner, *, files)`      (Stream G, Store wrapper pending)
  * `unverified_changes.pending_records_for_scan(store, scan_id, files, *, errors)` (Stream E)

NONE OF THE THREE IS ON THIS BRANCH. They live, uncommitted, in the owner's worktree, and this
branch must not copy them. So this file carries TEST-ONLY implementations of exactly the owner
contracts (/tmp/acp-report-followup-owner-response.md, "R-B4"), each a real scan-wide SQL read
against the real isolated store — one statement per input (plus an owner check / chunking), never
a loop over the per-file readers:

  * diffs: one SELECT over remediation_diff for the scan, ordered (file, rule_id, seq), each row
    projected through Store._with_diff_location exactly as get_remediation_diffs projects it;
  * reviews: one SELECT over scan_decisions (kind LIKE 'change_review:%', owner_email = owner) —
    the table and filter get_change_reviews uses;
  * pending: the file records (1 statement) and the four tables pending_records reads, each read
    ONCE for every file with a corrected copy (`file IN (…)`, chunked at 500), then REPLAYED through
    the unchanged unverified_changes.pending_records per file via a read-only stand-in store. So the
    per-file answer is pending_records' own logic, and the statements are scan-wide.

What is proved here: (a) every row digest, every per-file facts object and the scan digest are
byte-identical with and without the batched readers; (b) the statement counts, measured by wrapping
the store's DB execute, are constant in N for the batched path and linear for the per-file path;
(c) a file the pending reader reports in `errors` is unverifiedSource 'unavailable' (count null,
never 0), and a reader that raises or omits a file falls back to the per-file read; (d) the owner
filter holds for reviews and a foreign scan's rows never leak.

What is NOT proved here and waits for the owner's commit: that the OWNER's real readers satisfy
these contracts (their own tests, e.g. tests/test_report_history_scan_readers.py and
tests/test_pending_records_for_scan.py, do that), and the combined integration. When they land,
delete the stand-ins' monkeypatching and run (a)-(d) against the real ones.
"""
from __future__ import annotations

import contextlib
import json
import re
import sys
import types
from pathlib import Path

import pytest

ACP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACP / "api"))

OWNER = "owner@hosp.org"
INTRUDER = "someone-else@hosp.org"
SID, OTHER_SID = "s-rb4-current", "s-rb4-foreign"
CHUNK = 400
PENDING_CHUNK = 500


# ── test-only implementations of the owner's R-B4 contracts ──────────────────────────────────

def _chunks(names, size):
    names = list(dict.fromkeys(names))
    return [names[i:i + size] for i in range(0, len(names), size)] or [[]]


def rb4_remediation_diffs_for_scan(store, scan_id, *, owner=None, files=None):
    """Owner contract: `result.get(f, [])` == store.get_remediation_diffs(scan_id, f)."""
    out: dict[str, list[dict]] = {}
    with store._db.cursor() as cur:
        if owner is not None:
            store._db.execute(cur, "SELECT 1 AS ok FROM scan_runs WHERE id=%s AND owner_email=%s",
                              (scan_id, owner))
            if not store._db.fetchone(cur):
                return {}
        groups = _chunks(files, CHUNK) if files is not None else [None]
        for group in groups:
            if group == []:
                continue
            sql = ("SELECT file,rule_id,seq,before,after,note,locator,page,TRUE AS verified "
                   "FROM remediation_diff WHERE scan_id=%s")
            params = [scan_id]
            if group is not None:
                sql += " AND file IN (" + ",".join(["%s"] * len(group)) + ")"
                params.extend(group)
            store._db.execute(cur, sql + " ORDER BY file, rule_id, seq", tuple(params))
            for row in store._db.fetchall(cur):
                name = row.pop("file")
                out.setdefault(name, []).append(
                    store._with_diff_location({**row, "verified": bool(row["verified"])}))
    return out


def rb4_change_reviews_for_scan(store, scan_id, owner, *, files=None):
    """Owner contract: `result.get(f, {})` == store.get_change_reviews(scan_id, f, owner=owner);
    the owner is REQUIRED (the per-file read drops its filter for a falsy owner; this never does)."""
    if not isinstance(owner, str) or not owner:
        raise ValueError("owner is required")
    out: dict[str, dict] = {}
    with store._db.cursor() as cur:
        for group in (_chunks(files, CHUNK) if files is not None else [None]):
            if group == []:
                continue
            sql = ("SELECT file,kind,value,updated_at FROM scan_decisions "
                   "WHERE scan_id=%s AND kind LIKE %s AND owner_email=%s")
            params = [scan_id, store.CHANGE_REVIEW_KIND_PREFIX + "%", owner]
            if group is not None:
                sql += " AND file IN (" + ",".join(["%s"] * len(group)) + ")"
                params.extend(group)
            store._db.execute(cur, sql, tuple(params))
            for row in store._db.fetchall(cur):
                out.setdefault(row["file"], {})[row["kind"]] = {
                    "value": row["value"], "updated_at": row["updated_at"]}
    return out


# The four per-file statements pending_records runs, recognised by what they read. A statement the
# replay does not recognise fails loudly: pending_records changed, so this stand-in is stale.
_PENDING_READS = (
    ("events", lambda s: "FROM decision_log" in s and "apply.saved_unverified" in s),
    ("retries", lambda s: "FROM decision_log" in s and "office_retry.saved" in s),
    ("confirmations", lambda s: "JOIN hitl_events" in s),
    ("edges", lambda s: "FROM ai_validation_outcomes" in s and "source_revision IS NOT NULL" in s),
)


def _batched_pending_sql(kind: str, n: int) -> str:
    marks = ",".join(["%s"] * n)
    return {
        "events": ("SELECT file,id,ts,rule_id,action,detail FROM decision_log WHERE scan_id=%s "
                   f"AND file IN ({marks}) AND action IN ('apply.saved_unverified','apply.reverified') "
                   "ORDER BY ts,id"),
        "retries": ("SELECT file,detail FROM decision_log WHERE scan_id=%s "
                    f"AND file IN ({marks}) AND action='office_retry.saved'"),
        "edges": ("SELECT file,actual_source_sha256,artifact_sha256 FROM ai_validation_outcomes "
                  f"WHERE scan_id=%s AND file IN ({marks}) AND source_revision IS NOT NULL"),
        "confirmations": (
            "SELECT v.file AS file,v.item_id,v.rule_id,v.artifact_sha256,v.proposal_snapshot_id,"
            "v.actual_approved_value_sha256,v.created_at,e.approved_value_sha256,e.proposal_snapshot_ids "
            "FROM ai_validation_outcomes v JOIN hitl_events e ON e.id=v.approval_event_id "
            f"WHERE v.scan_id=%s AND v.file IN ({marks}) AND e.scan_id=v.scan_id AND e.file=v.file "
            "AND e.item_id=v.item_id AND e.action IN ('approve','edit') "
            "AND v.outcome='verified_cleared'"),
    }[kind]


class _Replay:
    """A read-only store stand-in for ONE file: pending_records' reads answered from rows that were
    already fetched scan-wide. It has no connection of its own."""

    def __init__(self, record, tables):
        self._record = record
        self._tables = tables
        self._db = self

    def get_file_record(self, scan_id, file):
        return self._record

    @contextlib.contextmanager
    def cursor(self):
        yield types.SimpleNamespace(rows=[])

    def execute(self, cur, sql, params=()):
        kind = next((k for k, match in _PENDING_READS if match(sql)), None)
        if kind is None:
            raise AssertionError(f"pending_records ran a read this stand-in does not know: {sql}")
        cur.rows = [dict(r) for r in self._tables[kind]]

    def fetchall(self, cur):
        return cur.rows


def rb4_pending_records_for_scan(store, scan_id, files=None, *, errors=None):
    """Stream E contract: per file EXACTLY pending_records(store, scan_id, file); a file whose
    records are malformed is OMITTED and put in `errors` (when given), else the error propagates."""
    import unverified_changes
    records = store.get_file_records(scan_id) if files is None else {}
    if files is not None:
        for group in _chunks(files, PENDING_CHUNK):
            if group:
                records.update(store.get_file_records(scan_id, files=group))
    names = list(dict.fromkeys(files)) if files is not None else list(records)
    with_copy = [n for n in names if (records.get(n) or {}).get("corrected_sha256")]
    tables: dict[str, dict[str, list]] = {k: {} for k, _ in _PENDING_READS}
    with store._db.cursor() as cur:
        for group in _chunks(with_copy, PENDING_CHUNK):
            if not group:
                continue
            for kind, _ in _PENDING_READS:
                store._db.execute(cur, _batched_pending_sql(kind, len(group)),
                                  (scan_id, *group))
                for row in store._db.fetchall(cur):
                    tables[kind].setdefault(row.pop("file"), []).append(row)
    out: dict[str, list] = {}
    for name in names:
        if name not in with_copy:
            out[name] = []
            continue
        replay = _Replay(records.get(name), {k: tables[k].get(name, []) for k in tables})
        try:
            out[name] = unverified_changes.pending_records(replay, scan_id, name)
        except (KeyError, TypeError, ValueError) as exc:
            if errors is None:
                raise
            errors[name] = exc
    return out


# ── the estate ────────────────────────────────────────────────────────────────────────────────

def _name(i: int) -> str:
    # Nested and encoded names, so a reader keyed on anything but the exact name would miss.
    return f"Board/Q{i % 4 + 1} #{i} – Überblick.docx" if i % 5 == 0 else f"docs/file-{i:04d}.docx"


def build_estate(store, n: int) -> list[str]:
    import report_facts as rf
    names = [_name(i) for i in range(n)]

    def files(owner_tag):
        return [{"file": name, "engine": ".net/office", "status": "uncertain", "score": 70,
                 "compliant": 0, "skipped_rules": 0, "checksum": f"md5-{owner_tag}-{i}",
                 "issues": [{"ruleId": "DOCX-ALT-001", "wcag": "1.1.1 Non-text Content",
                             "severity": "SERIOUS", "detail": f"Image {k} has no description",
                             "page": None, "location": f"docx:image:{k}"} for k in (1, 2)]}
                for i, name in enumerate(names)]

    for sid, owner, when in ((SID, OWNER, "2026-09-01T09:00:00+00:00"),
                             (OTHER_SID, INTRUDER, "2026-09-02T09:00:00+00:00")):
        rows = files(sid)
        store.save_scan({"_scan_id": sid, "started_at": when, "completed_at": when,
                         "source": "drive", "owner": owner,
                         "rubric": {"name": "wcag-aa", "hash": "rubric-1"},
                         "summary": {"files": n, "certifiable": 0, "uncertain": n, "error": 0,
                                     "avg_score": 70}, "files": rows})
    for i, name in enumerate(names):
        if i % 3 == 0:
            store.record_remediation(SID, name, corrected_sha256=f"{i:064x}")
            store.record_remediation_diffs(SID, name, [
                {"rule_id": "1.1.1", "before": "", "after": f"Alt {i}", "locator": "word:p:3"},
                {"rule_id": "1.3.1", "before": "Body", "after": "Heading 2",
                 "note": "approved by a reviewer · word/document.xml#rId7"}])
            change_id = rf.verified_change_id(name, "1.1.1", 0)
            store.save_decision(SID, name, "change_review:" + change_id, json.dumps({
                "change_id": change_id, "verdict": "accepted", "artifact_sha256": f"{i:064x}",
                "change_digest": rf.change_digest("1.1.1", "", f"Alt {i}"),
                "reviewer": OWNER, "at": "2026-09-03T08:00:00+00:00"}), OWNER,
                "2026-09-03T08:00:00+00:00")
            # An applied-but-unverified change on the saved copy (pending_records' main path).
            store.log_decision("system", "apply.saved_unverified", scan_id=SID, file=name,
                               rule_id="1.1.1", detail=json.dumps({
                                   "artifact_sha256": f"{i:064x}", "rule_id": "1.1.1",
                                   "changes": [{"locator": "docx:image:2", "before": "",
                                                "after": f"Cow {i}"}]}))
        if i % 7 == 0:
            # A verdict recorded under ANOTHER owner on this scan: the per-file read filters it
            # out by owner, and the batched read must too.
            store.save_decision(SID, name, f"change_review:{name}::9.9.9::0", json.dumps({
                "change_id": f"{name}::9.9.9::0", "verdict": "rejected"}), INTRUDER,
                "2026-09-03T09:00:00+00:00")
        # The foreign scan has diffs and verdicts under the SAME file names; none may leak.
        if i % 4 == 0:
            store.record_remediation_diffs(OTHER_SID, name, [
                {"rule_id": "2.4.2", "before": "", "after": "Foreign title"}])
            store.save_decision(OTHER_SID, name, f"change_review:{name}::2.4.2::0",
                                json.dumps({"change_id": f"{name}::2.4.2::0", "verdict": "accepted"}),
                                OWNER, "2026-09-03T09:00:00+00:00")
    # One malformed unverified record: pending_records raises ValueError for this file.
    bad = names[3]
    store.record_remediation(SID, bad, corrected_sha256="b" * 64)
    store.log_decision("system", "apply.saved_unverified", scan_id=SID, file=bad, rule_id="1.1.1",
                       detail="{not json")
    return names


@pytest.fixture()
def rf(monkeypatch):
    import report_facts
    report_facts.clear_scan_index_cache()
    monkeypatch.setattr(report_facts, "_now", lambda: "2026-09-18T12:00:00+00:00")
    monkeypatch.delenv("ACP_BUILD_VERSION", raising=False)
    # The owner's report_history module is not on this branch; make sure no stray copy is used.
    monkeypatch.setitem(sys.modules, "report_history", types.ModuleType("report_history"))
    yield report_facts
    report_facts.clear_scan_index_cache()


def install_readers(monkeypatch, store, *, diffs=True, reviews=True, pending=True):
    import unverified_changes
    if diffs:
        monkeypatch.setattr(store, "remediation_diffs_for_scan",
                            lambda scan_id, *, owner=None, files=None:
                            rb4_remediation_diffs_for_scan(store, scan_id, owner=owner, files=files),
                            raising=False)
    if reviews:
        monkeypatch.setattr(store, "change_reviews_for_scan",
                            lambda scan_id, owner, *, files=None:
                            rb4_change_reviews_for_scan(store, scan_id, owner, files=files),
                            raising=False)
    if pending:
        monkeypatch.setattr(unverified_changes, "pending_records_for_scan",
                            rb4_pending_records_for_scan, raising=False)


def _rows(built):
    return {r["file"]: r for r in built["index"]}


# ── the stand-ins honour the owner contracts (so what follows tests report_facts, not them) ──────

def test_the_stand_in_readers_equal_the_per_file_reads(isolated_store):
    import unverified_changes
    names = build_estate(isolated_store, 30)
    diffs = rb4_remediation_diffs_for_scan(isolated_store, SID, owner=OWNER)
    reviews = rb4_change_reviews_for_scan(isolated_store, SID, OWNER)
    errors: dict = {}
    pending = rb4_pending_records_for_scan(isolated_store, SID, names, errors=errors)
    for name in names:
        assert diffs.get(name, []) == isolated_store.get_remediation_diffs(SID, name)
        assert reviews.get(name, {}) == isolated_store.get_change_reviews(SID, name, owner=OWNER)
        if name in errors:
            with pytest.raises(ValueError):
                unverified_changes.pending_records(isolated_store, SID, name)
        else:
            assert pending[name] == unverified_changes.pending_records(isolated_store, SID, name)
    assert list(errors) == [names[3]]
    assert rb4_remediation_diffs_for_scan(isolated_store, SID, owner=INTRUDER) == {}
    with pytest.raises(ValueError):
        rb4_change_reviews_for_scan(isolated_store, SID, None)


def test_saved_changes_from_pending_is_unverified_changes_own_projection(isolated_store, rf):
    """report_facts rebuilds saved_changes() from the scan-wide pending records; pin it to the
    owner's own function so a change to that projection fails here, not silently in a report."""
    import unverified_changes
    names = build_estate(isolated_store, 12)
    for name in names:
        if name == names[3]:
            continue
        assert rf.saved_changes_from_pending(
            name, unverified_changes.pending_records(isolated_store, SID, name)) == \
            unverified_changes.saved_changes(isolated_store, SID, name)


# ── (a) identical projection and digest ───────────────────────────────────────────────────────

def test_every_row_the_scan_digest_and_the_per_file_facts_are_identical_with_batched_readers(
        isolated_store, rf, monkeypatch):
    names = build_estate(isolated_store, 40)
    per_file = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    assert per_file["inputsRead"] == {"diffs": "per_file", "reviews": "per_file",
                                      "unverified": "per_file"}
    install_readers(monkeypatch, isolated_store)
    batched = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    assert batched["inputsRead"] == {"diffs": "batched", "reviews": "batched",
                                     "unverified": "batched"}
    assert batched["digest"] == per_file["digest"]
    assert batched["index"] == per_file["index"]
    assert batched["facts"] == per_file["facts"]
    # …and every row still equals the per-file route's facts (which never prefetch).
    for name in names:
        facts = rf.build_file_facts(isolated_store, SID, name, owner=OWNER)
        assert _rows(batched)[name]["factsDigest"] == facts["factsDigest"]
    # The data exercises every input: verified + unverified changes, reviews, and 'unavailable'.
    row = _rows(batched)[names[0]]
    assert row["savedChangesVerified"] == 2 and row["savedChangesUnverified"] == 1
    assert row["humanReviews"]["accepted"] == 1


def test_bite_a_batched_reader_that_drops_one_row_moves_the_digest(isolated_store, rf, monkeypatch):
    """The equality above is not vacuous: one missing diff row in the batch is a different digest."""
    build_estate(isolated_store, 12)
    per_file = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    install_readers(monkeypatch, isolated_store)

    def lossy(scan_id, *, owner=None, files=None):
        got = rb4_remediation_diffs_for_scan(isolated_store, scan_id, owner=owner, files=files)
        first = sorted(got)[0]
        got[first] = got[first][:-1]
        return got
    monkeypatch.setattr(isolated_store, "remediation_diffs_for_scan", lossy)
    assert rf.build_scan_index(isolated_store, SID, owner=OWNER)["digest"] != per_file["digest"]


# ── (b) the statement count ───────────────────────────────────────────────────────────────────

_CATEGORIES = {
    # The diff reader's owner check is part of its read (the contract's "+1 owner check").
    "diffs": lambda s: "FROM remediation_diff" in s or "AS ok FROM scan_runs" in s,
    "reviews": lambda s: "FROM scan_decisions" in s and re.search(r"(?<!NOT )LIKE", s) is not None,
    "unverified": lambda s: ("FROM decision_log" in s or "FROM ai_validation_outcomes" in s
                             or ("FROM file_records f" in s and "f.file IN" in s)),
}


def _count_statements(store, monkeypatch, fn):
    seen: list[str] = []
    real = store._db.execute

    def counting(cur, sql, params=()):
        seen.append(sql)
        return real(cur, sql, params)
    monkeypatch.setattr(store._db, "execute", counting)
    try:
        result = fn()
    finally:
        monkeypatch.setattr(store._db, "execute", real)
    counts = {k: sum(1 for s in seen if match(s)) for k, match in _CATEGORIES.items()}
    counts["total"] = len(seen)
    return result, counts


MEASURED: dict = {}


@pytest.mark.parametrize("n", [50, 300])
def test_the_batched_path_reads_each_input_a_constant_number_of_times(isolated_store, rf,
                                                                     monkeypatch, n):
    build_estate(isolated_store, n)
    per_file, before = _count_statements(
        isolated_store, monkeypatch, lambda: rf.build_scan_index(isolated_store, SID, owner=OWNER))
    install_readers(monkeypatch, isolated_store)
    batched, after = _count_statements(
        isolated_store, monkeypatch, lambda: rf.build_scan_index(isolated_store, SID, owner=OWNER))
    MEASURED[n] = {"per_file": before, "batched": after}
    print(f"\nR-B4 statements at N={n}: per-file {before}, batched {after}")
    assert batched["digest"] == per_file["digest"]
    # Per file: one diff read and one review read per document; pending_records reads the file
    # record for every document plus four tables for every document with a corrected copy.
    with_copy = len(range(0, n, 3))
    assert before["diffs"] == n
    assert before["reviews"] == n
    # (the malformed file stops after its first table read: 1 + 1 instead of 1 + 4)
    assert before["unverified"] == n + 4 * with_copy - 3
    # Batched: one diff statement + one owner check; one review statement; one file-record read
    # and four table reads per 500 documents with a corrected copy (one chunk at these sizes).
    assert after["diffs"] == 2
    assert after["reviews"] == 1
    assert after["unverified"] == 1 + 4
    # The whole build: everything else (baselines per file until R-B1 lands, etc.) is unchanged,
    # so the saving is exactly the three inputs'.
    assert before["total"] - after["total"] == (
        before["diffs"] + before["reviews"] + before["unverified"]
        - after["diffs"] - after["reviews"] - after["unverified"])


# ── (c) unreadable is 'unavailable', never zero; failures fall back per file ─────────────────

def test_a_file_the_pending_reader_reports_in_errors_is_unavailable_not_zero(isolated_store, rf,
                                                                            monkeypatch):
    names = build_estate(isolated_store, 12)
    install_readers(monkeypatch, isolated_store)
    calls = []
    real = rb4_pending_records_for_scan

    def spy(store, scan_id, files=None, *, errors=None):
        got = real(store, scan_id, files, errors=errors)
        calls.append(dict(errors))
        return got
    import unverified_changes
    monkeypatch.setattr(unverified_changes, "pending_records_for_scan", spy)
    built = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    assert built["inputsRead"]["unverified"] == "batched"
    assert list(calls[0]) == [names[3]]                       # the batched reader reported it
    row = _rows(built)[names[3]]
    assert row["savedChangesUnverified"] is None              # unknown, never 0
    assert row["savedChangesComplete"] is False
    assert built["facts"]["totals"]["savedChangesUnverified"] is None


def test_errors_are_honoured_even_when_the_records_would_have_read(isolated_store, rf, monkeypatch):
    """Absent-because-unreadable is never read as "nothing pending": a file in `errors` is
    'unavailable' whatever the per-file read would have said."""
    names = build_estate(isolated_store, 9)
    install_readers(monkeypatch, isolated_store)

    def says_unreadable(store, scan_id, files=None, *, errors=None):
        got = rb4_pending_records_for_scan(store, scan_id, files, errors=errors)
        got.pop(names[0], None)
        errors[names[0]] = ValueError("could not be read")
        return got
    import unverified_changes
    monkeypatch.setattr(unverified_changes, "pending_records_for_scan", says_unreadable)
    facts_row = _rows(rf.build_scan_index(isolated_store, SID, owner=OWNER))[names[0]]
    assert facts_row["savedChangesUnverified"] is None


def test_a_file_the_pending_reader_omits_without_an_error_is_read_per_file(isolated_store, rf,
                                                                           monkeypatch):
    names = build_estate(isolated_store, 9)
    per_file = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    install_readers(monkeypatch, isolated_store)

    def forgets(store, scan_id, files=None, *, errors=None):
        got = rb4_pending_records_for_scan(store, scan_id, files, errors=errors)
        got.pop(names[0])
        return got
    import unverified_changes
    monkeypatch.setattr(unverified_changes, "pending_records_for_scan", forgets)
    built = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    assert _rows(built)[names[0]]["savedChangesUnverified"] == 1     # not silently 0
    assert built["digest"] == per_file["digest"]


@pytest.mark.parametrize("which", ["diffs", "reviews", "unverified"])
def test_a_reader_that_raises_falls_back_to_per_file_reads(isolated_store, rf, monkeypatch, which):
    build_estate(isolated_store, 9)
    per_file = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    install_readers(monkeypatch, isolated_store)

    def boom(*a, **k):
        raise RuntimeError("the batched read failed")
    import unverified_changes
    target, attr = {"diffs": (isolated_store, "remediation_diffs_for_scan"),
                    "reviews": (isolated_store, "change_reviews_for_scan"),
                    "unverified": (unverified_changes, "pending_records_for_scan")}[which]
    monkeypatch.setattr(target, attr, boom)
    built = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    assert built["inputsRead"][which] == "per_file"
    assert built["digest"] == per_file["digest"]                      # never silently empty


# ── (d) owner filter and isolation ────────────────────────────────────────────────────────────

def test_the_owner_filter_and_scan_isolation_hold_on_the_batched_path(isolated_store, rf,
                                                                      monkeypatch):
    names = build_estate(isolated_store, 15)
    install_readers(monkeypatch, isolated_store)
    seen = []
    real = isolated_store.change_reviews_for_scan

    def spy(scan_id, owner, *, files=None):
        seen.append(owner)
        return real(scan_id, owner, files=files)
    monkeypatch.setattr(isolated_store, "change_reviews_for_scan", spy)
    built = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    assert seen == [OWNER]
    row = _rows(built)[names[0]]            # has an INTRUDER verdict (i % 7 == 0) and a foreign-scan one
    assert row["humanReviews"] == {"pending": 2, "accepted": 1, "correctionRequested": 0,
                                   "rejected": 0, "unable": 0, "stale": 0}
    facts = rf.build_file_facts(isolated_store, SID, names[0], owner=OWNER)
    assert set(facts["reviews"]) == {rf.verified_change_id(names[0], "1.1.1", 0)}
    assert all(c["after"] != "Foreign title" for c in facts["savedChanges"])
    # A foreign owner still sees nothing at all.
    assert rf.build_scan_index(isolated_store, SID, owner=INTRUDER) is None


def test_a_falsy_owner_never_takes_the_batched_review_read(isolated_store, rf, monkeypatch):
    build_estate(isolated_store, 5)
    install_readers(monkeypatch, isolated_store)
    context = rf.scan_context(isolated_store, SID, owner=OWNER)
    modes = rf.prefetch_scan_inputs(isolated_store, SID, list(context["files_by_name"]),
                                    owner="", context=context)
    assert modes["reviews"] == "per_file" and "reviews_by_file" not in context
