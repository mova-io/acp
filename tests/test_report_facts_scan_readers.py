"""R-B4: a scan-index build reads its three per-file inputs ONCE — with the owner's REAL readers.

report_facts.prefetch_scan_inputs feeds the per-file builders from the owner's scan-wide readers
(on this branch since 0f0fd520):

  * `Store.remediation_diffs_for_scan(scan_id, *, owner, files)`  → report_history
  * `Store.change_reviews_for_scan(scan_id, owner, *, files)`     → report_history
  * `unverified_changes.pending_records_for_scan(store, scan_id, files, *, errors)`

Every test below runs those real readers against a real isolated SQLite store. "Per file" means
the same build with the three readers made unavailable TO report_facts — the Store methods removed
from the class and the module function removed, by monkeypatch, for that test only. The real
`report_history` module is never replaced (the Store's other wrappers import it).

Proved here: (a) every row digest, every per-file facts object and the scan digest are identical
batched vs per file; (b) the statement counts — each SQL statement attributed to the reader on the
stack when it ran — are constant in N for the batched path and linear per file, at N=50 and 300;
(c) a file the real pending reader reports in `errors` is unverifiedSource 'unavailable' (null,
never 0), and a reader that raises or omits a file falls back to the per-file read; (d) the owner
filter holds for reviews and a foreign scan's rows never leak.
"""
from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import pytest

ACP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACP / "api"))

OWNER = "owner@hosp.org"
INTRUDER = "someone-else@hosp.org"
SID, OTHER_SID = "s-rb4-current", "s-rb4-foreign"


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
        store.save_scan({"_scan_id": sid, "started_at": when, "completed_at": when,
                         "source": "drive", "owner": owner,
                         "rubric": {"name": "wcag-aa", "hash": "rubric-1"},
                         "summary": {"files": n, "certifiable": 0, "uncertain": n, "error": 0,
                                     "avg_score": 70}, "files": files(sid)})
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
    yield report_facts
    report_facts.clear_scan_index_cache()


READERS = ("diffs", "reviews", "unverified")


def without_readers(monkeypatch, store, which=READERS):
    """Make the chosen R-B4 readers unavailable TO report_facts, for this test only: the Store
    method is removed from the class, the module-level fallback lookup finds nothing, and the
    pending reader is removed from unverified_changes. The real modules are not replaced."""
    import report_facts
    import unverified_changes
    names = {"diffs": "remediation_diffs_for_scan", "reviews": "change_reviews_for_scan"}
    real_lookup = report_facts._report_history
    hidden = {names[w] for w in which if w in names}
    for name in hidden:
        monkeypatch.delattr(type(store), name)

    class _Hide:
        """report_history as report_facts sees it, minus the hidden readers."""
        def __init__(self, module):
            self._m = module

        def __getattr__(self, attr):
            if attr in hidden:
                raise AttributeError(attr)
            return getattr(self._m, attr)
    monkeypatch.setattr(report_facts, "_report_history", lambda: _Hide(real_lookup()))
    if "unverified" in which:
        monkeypatch.delattr(unverified_changes, "pending_records_for_scan")


def _rows(built):
    return {r["file"]: r for r in built["index"]}


def test_the_owners_readers_are_real_on_this_branch(isolated_store):
    """If this fails the owner's readers are gone again, and the rest of this file would be
    testing the per-file path twice."""
    import report_history
    import unverified_changes
    for name in ("remediation_diffs_for_scan", "change_reviews_for_scan"):
        assert callable(getattr(type(isolated_store), name, None))
        assert callable(getattr(report_history, name, None))
    assert callable(getattr(unverified_changes, "pending_records_for_scan", None))


def test_the_real_readers_equal_the_per_file_reads_on_this_estate(isolated_store):
    import unverified_changes
    names = build_estate(isolated_store, 30)
    diffs = isolated_store.remediation_diffs_for_scan(SID, owner=OWNER)
    reviews = isolated_store.change_reviews_for_scan(SID, OWNER)
    errors: dict = {}
    pending = unverified_changes.pending_records_for_scan(isolated_store, SID, names, errors=errors)
    for name in names:
        assert diffs.get(name, []) == isolated_store.get_remediation_diffs(SID, name)
        assert reviews.get(name, {}) == isolated_store.get_change_reviews(SID, name, owner=OWNER)
        if name in errors:
            with pytest.raises(ValueError):
                unverified_changes.pending_records(isolated_store, SID, name)
        else:
            assert pending[name] == unverified_changes.pending_records(isolated_store, SID, name)
    assert list(errors) == [names[3]]


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

def test_every_row_the_scan_digest_and_the_per_file_facts_are_identical_batched_vs_per_file(
        isolated_store, rf, monkeypatch):
    names = build_estate(isolated_store, 40)
    batched = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    assert batched["inputsRead"] == {"diffs": "batched", "reviews": "batched",
                                     "unverified": "batched"}
    assert batched["baselineRead"] == "batched"                      # R-B1, real
    per_file_rows = {}
    with monkeypatch.context() as m:
        without_readers(m, isolated_store)
        per_file = rf.build_scan_index(isolated_store, SID, owner=OWNER)
        assert per_file["inputsRead"] == {"diffs": "per_file", "reviews": "per_file",
                                          "unverified": "per_file"}
        for name in names:
            per_file_rows[name] = rf.build_file_facts(isolated_store, SID, name, owner=OWNER)
    assert batched["digest"] == per_file["digest"]
    assert batched["index"] == per_file["index"]
    assert batched["facts"] == per_file["facts"]
    # Every row equals the per-file route's facts, with and without the readers present.
    for name in names:
        facts = rf.build_file_facts(isolated_store, SID, name, owner=OWNER)
        assert _rows(batched)[name]["factsDigest"] == facts["factsDigest"] \
            == per_file_rows[name]["factsDigest"]
    row = _rows(batched)[names[0]]
    assert row["savedChangesVerified"] == 2 and row["savedChangesUnverified"] == 1
    assert row["humanReviews"]["accepted"] == 1


def test_bite_a_batched_reader_that_drops_one_row_moves_the_digest(isolated_store, rf, monkeypatch):
    """The equality above is not vacuous: one missing diff row in the batch is a different digest."""
    build_estate(isolated_store, 12)
    honest = rf.build_scan_index(isolated_store, SID, owner=OWNER)["digest"]
    real = type(isolated_store).remediation_diffs_for_scan

    def lossy(self, scan_id, *, owner=None, files=None):
        got = real(self, scan_id, owner=owner, files=files)
        first = sorted(got)[0]
        got[first] = got[first][:-1]
        return got
    monkeypatch.setattr(type(isolated_store), "remediation_diffs_for_scan", lossy)
    assert rf.build_scan_index(isolated_store, SID, owner=OWNER)["digest"] != honest


# ── (b) the statement count ───────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def attributed_statements(store, monkeypatch):
    """Count every SQL statement, attributed to the input whose reader was on the stack when it
    ran (nested calls count toward the outermost labelled reader); the rest is 'other'."""
    import unverified_changes
    counts = {"diffs": 0, "reviews": 0, "unverified": 0, "other": 0}
    stack: list[str] = []
    real_execute = store._db.execute

    def counting(cur, sql, params=()):
        counts[stack[0] if stack else "other"] += 1
        return real_execute(cur, sql, params)

    def labelled(label, fn):
        def wrapper(*a, **k):
            stack.append(label)
            try:
                return fn(*a, **k)
            finally:
                stack.pop()
        return wrapper

    cls = type(store)
    with monkeypatch.context() as m:
        m.setattr(store._db, "execute", counting)
        for name, label in (("get_remediation_diffs", "diffs"),
                            ("remediation_diffs_for_scan", "diffs"),
                            ("get_change_reviews", "reviews"),
                            ("change_reviews_for_scan", "reviews")):
            if hasattr(cls, name):
                m.setattr(cls, name, labelled(label, getattr(cls, name)))
        for name in ("saved_changes", "pending_records_for_scan"):
            if hasattr(unverified_changes, name):
                m.setattr(unverified_changes, name,
                          labelled("unverified", getattr(unverified_changes, name)))
        yield counts
    counts["total"] = sum(v for k, v in counts.items() if k != "total")


MEASURED: dict = {}


@pytest.mark.parametrize("n", [50, 300])
def test_the_batched_path_reads_each_input_a_constant_number_of_times(isolated_store, rf,
                                                                     monkeypatch, n):
    build_estate(isolated_store, n)
    with attributed_statements(isolated_store, monkeypatch) as after:
        batched = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    with monkeypatch.context() as m:
        without_readers(m, isolated_store)
        with attributed_statements(isolated_store, m) as before:
            per_file = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    MEASURED[n] = {"per_file": dict(before), "batched": dict(after)}
    print(f"\nR-B4 statements (owner's real readers) at N={n}: per-file {before}, batched {after}")
    assert batched["digest"] == per_file["digest"]
    # Per file: one diff read and one review read per document; pending_records reads the file
    # record for every document plus four tables for every document with a corrected copy (the
    # owner's pending_records reads all four before it parses, so the malformed one costs 1 + 4).
    with_copy = len(range(0, n, 3))
    assert before["diffs"] == n
    assert before["reviews"] == n
    assert before["unverified"] == n + 4 * with_copy
    # Batched (owner contract): diffs 1 statement + 1 owner check; reviews 1; pending 1 file-record
    # read + 4 table reads per 500 documents with a corrected copy — the same at 50 and at 300.
    assert after["diffs"] == 2
    assert after["reviews"] == 1
    assert after["unverified"] == 5
    # Nothing else moved: the saving is exactly the three inputs'.
    assert before["other"] == after["other"]


# ── (c) unreadable is 'unavailable', never zero; failures fall back per file ─────────────────

def test_a_file_the_real_pending_reader_reports_in_errors_is_unavailable_not_zero(
        isolated_store, rf, monkeypatch):
    import unverified_changes
    names = build_estate(isolated_store, 12)
    real = unverified_changes.pending_records_for_scan
    seen = []

    def spy(store, scan_id, files=None, *, errors=None):
        got = real(store, scan_id, files, errors=errors)
        seen.append(dict(errors))
        return got
    monkeypatch.setattr(unverified_changes, "pending_records_for_scan", spy)
    # The per-file read must NOT be what answers for the malformed file.
    monkeypatch.setattr(unverified_changes, "saved_changes", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("per-file pending read ran although the batched reader answered")))
    built = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    assert built["inputsRead"]["unverified"] == "batched"
    assert list(seen[0]) == [names[3]]                        # the real reader reported it
    row = _rows(built)[names[3]]
    assert row["savedChangesUnverified"] is None              # unknown, never 0
    assert row["savedChangesComplete"] is False
    assert built["facts"]["totals"]["savedChangesUnverified"] is None


def test_errors_are_honoured_even_when_the_records_would_have_read(isolated_store, rf, monkeypatch):
    """Absent-because-unreadable is never read as "nothing pending": a file in `errors` is
    'unavailable' whatever the per-file read would have said."""
    import unverified_changes
    names = build_estate(isolated_store, 9)
    real = unverified_changes.pending_records_for_scan

    def says_unreadable(store, scan_id, files=None, *, errors=None):
        got = real(store, scan_id, files, errors=errors)
        got.pop(names[0], None)
        errors[names[0]] = ValueError("could not be read")
        return got
    monkeypatch.setattr(unverified_changes, "pending_records_for_scan", says_unreadable)
    assert _rows(rf.build_scan_index(isolated_store, SID, owner=OWNER))[names[0]][
        "savedChangesUnverified"] is None


def test_a_file_the_pending_reader_omits_without_an_error_is_read_per_file(isolated_store, rf,
                                                                           monkeypatch):
    import unverified_changes
    names = build_estate(isolated_store, 9)
    honest = rf.build_scan_index(isolated_store, SID, owner=OWNER)["digest"]
    real = unverified_changes.pending_records_for_scan

    def forgets(store, scan_id, files=None, *, errors=None):
        got = real(store, scan_id, files, errors=errors)
        got.pop(names[0])
        return got
    monkeypatch.setattr(unverified_changes, "pending_records_for_scan", forgets)
    built = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    assert _rows(built)[names[0]]["savedChangesUnverified"] == 1     # not silently 0
    assert built["digest"] == honest


@pytest.mark.parametrize("which", ["diffs", "reviews", "unverified"])
def test_a_reader_that_raises_falls_back_to_per_file_reads(isolated_store, rf, monkeypatch, which):
    import unverified_changes
    build_estate(isolated_store, 9)
    honest = rf.build_scan_index(isolated_store, SID, owner=OWNER)["digest"]

    def boom(*a, **k):
        raise RuntimeError("the batched read failed")
    target, attr = {"diffs": (type(isolated_store), "remediation_diffs_for_scan"),
                    "reviews": (type(isolated_store), "change_reviews_for_scan"),
                    "unverified": (unverified_changes, "pending_records_for_scan")}[which]
    monkeypatch.setattr(target, attr, boom)
    built = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    assert built["inputsRead"][which] == "per_file"
    assert built["digest"] == honest                                  # never silently empty


def test_absent_readers_leave_every_input_per_file(isolated_store, rf, monkeypatch):
    build_estate(isolated_store, 6)
    honest = rf.build_scan_index(isolated_store, SID, owner=OWNER)["digest"]
    without_readers(monkeypatch, isolated_store)
    built = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    assert set(built["inputsRead"].values()) == {"per_file"}
    assert built["digest"] == honest


# ── (d) owner filter and isolation ────────────────────────────────────────────────────────────

def test_the_owner_filter_and_scan_isolation_hold_on_the_batched_path(isolated_store, rf,
                                                                      monkeypatch):
    names = build_estate(isolated_store, 15)
    seen = []
    real = type(isolated_store).change_reviews_for_scan

    def spy(self, scan_id, owner, *, files=None):
        seen.append(owner)
        return real(self, scan_id, owner, files=files)
    monkeypatch.setattr(type(isolated_store), "change_reviews_for_scan", spy)
    built = rf.build_scan_index(isolated_store, SID, owner=OWNER)
    assert seen == [OWNER]
    assert built["inputsRead"]["reviews"] == "batched"
    row = _rows(built)[names[0]]     # has an INTRUDER verdict (i % 7 == 0) and a foreign-scan one
    assert row["humanReviews"] == {"pending": 2, "accepted": 1, "correctionRequested": 0,
                                   "rejected": 0, "unable": 0, "stale": 0}
    facts = rf.build_file_facts(isolated_store, SID, names[0], owner=OWNER)
    assert set(facts["reviews"]) == {rf.verified_change_id(names[0], "1.1.1", 0)}
    assert all(c["after"] != "Foreign title" for c in facts["savedChanges"])
    # A foreign owner sees nothing at all, and the real diff reader refuses a foreign owner too.
    assert rf.build_scan_index(isolated_store, SID, owner=INTRUDER) is None
    assert isolated_store.remediation_diffs_for_scan(SID, owner=INTRUDER) == {}


def test_a_falsy_owner_never_takes_the_batched_review_read(isolated_store, rf, monkeypatch):
    build_estate(isolated_store, 5)
    context = rf.scan_context(isolated_store, SID, owner=OWNER)
    modes = rf.prefetch_scan_inputs(isolated_store, SID, list(context["files_by_name"]),
                                    owner="", context=context)
    assert modes["reviews"] == "per_file" and "reviews_by_file" not in context
    # (the owner's reader itself refuses one: it is never asked)
    with pytest.raises(ValueError):
        isolated_store.change_reviews_for_scan(SID, "")
