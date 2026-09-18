"""Comparison with an earlier assessment — contract 4 of the report follow-up.

Before this module the server shipped a baseline (`facts.previous`) and the BROWSER classified it.
Four things went wrong there, three of them reproduced against the real store:

* C1 — a finding with no detector location gets a synthetic, snapshot-scoped identity
  (`aggregate-instance:<scan>:…`, `comparable: false`). The browser ignored the flag, so an
  unchanged "document language is not set" in both runs read as resolved AND newly introduced.
* C2 — `previous_assessment_for_file` returns the latest earlier ROW, whatever happened to it. A
  baseline whose analyser errored (0 findings) made every current finding "new since then".
* C3 — a renamed file matched by the provider's own id was compared on the server and then
  rejected by the browser as "a different document", because the names differ.
* C4 — the scan report had no comparison at all (`previous: None`, hard-coded).

So the classification is made HERE, once, from the baseline the server actually loaded, and the
browser renders it. The rules:

* `comparable: false` findings are NEVER introduced or resolved. They are counted, per criterion,
  in `notComparable`, so a reader sees "3.1.1: 1 before, 1 now — not matched individually".
* A baseline that did not finish — its file errored, was not assessed, was partly assessed
  (skipped rules), or its scan was cancelled/interrupted/superseded/failed — is
  `baseline_unusable` with the reason. It is never "everything is new".
* A current assessment that did not finish is `not_comparable`: its finding list is not a
  statement about the document, so "resolved since then" would be a claim nobody checked.
* Identity is the provider's file id (or source+path), so a rename is `compared` and says so.
* `reopened` is reported only where it is DETERMINABLE — the baseline scan's own per-finding
  ledger recorded the finding as resolved and it is reported again. Otherwise it is `null`, not [].
"""
from __future__ import annotations

UNUSABLE_RUN_STATUSES = frozenset({"cancelled", "interrupted", "superseded", "error", "failed"})

# status values (contract 4) — 'not_comparable' is the fourth, for an unfinished CURRENT side.
COMPARED = "compared"
NO_BASELINE = "no_baseline"
BASELINE_UNUSABLE = "baseline_unusable"
NOT_COMPARABLE = "not_comparable"

REASONS = {
    "no_earlier_assessment": ("no earlier assessment of this same document under the same rubric "
                              "and scope is recorded"),
    "lookup_failed": "no earlier assessment could be read for this document",
    "different_scope": ("the earlier assessment of this document used a different rubric or scope, "
                        "so its counts are not comparable"),
    "baseline_error": ("the most recent earlier assessment of this document did not produce a "
                       "result (recorded status: {status}), so it is not a baseline — its empty "
                       "finding list would make every current finding look new"),
    "baseline_not_assessed": ("the most recent earlier record of this document was never assessed "
                              "(recorded status: {status}), so it is not a baseline"),
    "baseline_partial": ("the most recent earlier assessment of this document skipped {skipped} "
                         "rule(s), so its finding list is incomplete and not a baseline"),
    "baseline_run_unfinished": ("the scan that holds the most recent earlier assessment of this "
                                "document was {status}, so that assessment is not a baseline"),
    "current_not_assessed": ("this document's current assessment is {state} ({why}), so a "
                             "difference from an earlier one would not describe the document"),
    # ── same-scan history (C6, R-B2) ──
    "reader_unavailable": ("ACP cannot yet read an assessment that a re-assessment replaced inside "
                           "this scan, so changes within one scan are not compared; this is not "
                           "evidence that the document was assessed only once"),
    "not_recorded": ("no replaced assessment of this document was recorded in this scan — it was "
                     "assessed once here, or re-assessed before ACP began recording replaced "
                     "assessments; this is not evidence that there was no earlier one"),
    "context_not_recorded": ("the replaced assessment's {what} {was} not recorded when it was "
                             "written, so whether it is comparable with the current one is not "
                             "known"),
    "same_scan_different_context": ("the replaced assessment used a different {what}, so its "
                                    "findings are not comparable"),
    "same_scan_failed": ("the replaced assessment did not produce a result (outcome: {outcome}), "
                         "so it is not a baseline — its empty finding list would make every "
                         "current finding look new"),
    "same_scan_issues_unavailable": ("the replaced assessment's finding list was not recorded as a "
                                     "complete set, so it is not a baseline"),
    "same_scan_partial": ("the replaced assessment was {completeness}{rules}, so its finding list "
                          "is incomplete and not a baseline"),
    "same_scan_issues_changed": ("the replaced assessment's finding rows changed after they were "
                                 "written, so they are not the assessment as recorded"),
    "same_scan_no_ledger": ("this scan's per-finding ledger describes the current assessment, not "
                            "the one it replaced, so whether a finding reported again had been "
                            "fixed in between is not recorded"),
}


def reason(code: str, **values) -> str:
    text = REASONS.get(code, code)
    try:
        return text.format(**values)
    except (KeyError, IndexError):
        return text


def baseline_problem(found: dict, *, assessed_statuses, error_statuses) -> tuple[str, str] | None:
    """(reasonCode, reason) when the loaded baseline cannot be used, else None."""
    file_row = found.get("file_row") or {}
    run = found.get("run") or {}
    status = str(file_row.get("status") or "").strip().lower()
    if status in error_statuses:
        return "baseline_error", reason("baseline_error", status=status)
    if status not in assessed_statuses:
        return "baseline_not_assessed", reason("baseline_not_assessed", status=status or "none")
    skipped = int(file_row.get("skipped_rules") or 0)
    if skipped > 0:
        return "baseline_partial", reason("baseline_partial", skipped=skipped)
    run_status = str(run.get("status") or "").strip().lower()
    if run_status in UNUSABLE_RUN_STATUSES:
        return "baseline_run_unfinished", reason("baseline_run_unfinished", status=run_status)
    return None


def baseline_ref(found: dict, *, state: str | None = None) -> dict:
    run = found.get("run") or {}
    file_row = found.get("file_row") or {}
    return {
        "scanId": run.get("id"),
        "file": file_row.get("file"),
        "generatedAt": run.get("assessed_at") or run.get("completed_at"),
        # The earlier file's own recorded status, and its scan's — both are what made it usable
        # or not, so both are stated rather than folded into one word.
        "status": state or str(file_row.get("status") or "") or None,
        "runStatus": run.get("status"),
    }


def _summary(finding: dict) -> dict:
    return {"id": finding["id"], "ruleId": finding.get("ruleId"), "sc": finding.get("sc"),
            "detail": finding.get("detail"), "location": finding.get("location")}


def _by_criterion(current: list[dict], previous: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for side, rows in (("current", current), ("previous", previous)):
        for f in rows:
            if f.get("comparable"):
                continue
            key = (f.get("sc") or "", f.get("ruleId") or "")
            entry = groups.setdefault(key, {"sc": f.get("sc"), "ruleId": f.get("ruleId"),
                                            "current": 0, "previous": 0})
            entry[side] += 1
    return [groups[k] for k in sorted(groups)]


def classify(current: list[dict], previous: list[dict],
             previous_states: dict | None) -> dict:
    """Finding-by-finding classification. Both lists carry ids in the SAME id space."""
    cur = {f["id"]: f for f in current if f.get("comparable")}
    prev = {f["id"]: f for f in previous if f.get("comparable")}
    introduced = [fid for fid in cur if fid not in prev]
    both = [fid for fid in cur if fid in prev]
    resolved = [_summary(prev[fid]) for fid in prev if fid not in cur]
    if previous_states is None:
        reopened = None
        persisting = both
    else:
        reopened = [fid for fid in both if previous_states.get(fid) == "resolved_verified"]
        persisting = [fid for fid in both if fid not in set(reopened)]
    return {
        "introduced": introduced,
        "persisting": persisting,
        "resolved": resolved,
        "reopened": reopened,
        "notComparable": {"current": sum(1 for f in current if not f.get("comparable")),
                          "previous": sum(1 for f in previous if not f.get("comparable"))},
        "notComparableByCriterion": _by_criterion(current, previous),
    }


def empty(status: str, code: str, text: str, baseline: dict | None = None) -> dict:
    return {"status": status, "reasonCode": code, "reason": text, "baseline": baseline,
            "renamed": None, "introduced": [], "persisting": [], "resolved": [],
            "reopened": None, "reopenedReason": None,
            "notComparable": None, "notComparableByCriterion": []}


# ── same-scan history (C6, R-B2) ──────────────────────────────────────────────────────────────
# Two statuses beyond contract 4's, because "nothing to compare with" has two different causes
# here and they must not read alike: the store cannot tell (not_available), or it can and recorded
# no replaced assessment (not_recorded — still not evidence that none happened).
SAME_SCAN_NOT_AVAILABLE = "not_available"
SAME_SCAN_NOT_RECORDED = "not_recorded"

_BASIS_WORDS = {"rubric": "rubric", "scope": "scan scope", "file_scope": "per-file scope"}


def same_scan_empty(status: str, code: str, snapshot: dict | None = None, *,
                    text: str | None = None) -> dict:
    out = empty(status, code, text or reason(code),
                baseline=None if snapshot is None else {
                    "scanId": snapshot.get("scanId"), "file": snapshot.get("file"),
                    "generatedAt": snapshot.get("writtenAt"),
                    "status": snapshot.get("assessmentOutcome"), "runStatus": None,
                    "sameScanSnapshot": True})
    out["snapshot"] = snapshot
    return out


def same_scan_snapshot(found: dict, scan_id: str) -> dict:
    """The facts' view of R-B2's `snapshot` block: what was recorded at the replaced write."""
    snap = found.get("snapshot") or {}
    file_row = found.get("file_row") or {}
    basis = snap.get("comparison_basis")
    return {
        "scanId": scan_id,
        "file": file_row.get("file"),
        "historyId": snap.get("history_id"),
        "seq": snap.get("seq"),
        "historyTotal": snap.get("history_total"),
        "writtenAt": snap.get("written_at"),
        "supersededAt": snap.get("superseded_at"),
        "contextSource": snap.get("context_source"),
        "assessmentOutcome": snap.get("assessment_outcome"),
        "issuesState": snap.get("issues_state"),
        # null, never 0, unless the replaced list was recorded as the whole finding set
        "issueCount": snap.get("issue_count") if snap.get("issues_state") == "recorded" else None,
        "zeroFindings": snap.get("zero_findings") is True,
        "completeness": snap.get("completeness"),
        "rulesNotChecked": snap.get("rules_not_checked"),
        "integrity": snap.get("integrity"),
        "comparisonBasis": dict(basis) if isinstance(basis, dict) else None,
    }


def same_scan_problem(found: dict) -> tuple[str, str] | None:
    """(reasonCode, reason) when the replaced assessment cannot be compared, else None.

    Only "same" on every comparison-basis field is comparable. "not_recorded" is UNKNOWN, never
    "different" — the owner's contract, and the reason `run` is not scope-digested here.
    """
    snap = found.get("snapshot") or {}
    basis = snap.get("comparison_basis") if isinstance(snap.get("comparison_basis"), dict) else {}
    if snap.get("context_source") != "recorded_at_write":
        return "context_not_recorded", reason("context_not_recorded",
                                              what="rubric and scope", was="were")
    different = [_BASIS_WORDS[k] for k in _BASIS_WORDS if basis.get(k) == "different"]
    if different:
        return "same_scan_different_context", reason("same_scan_different_context",
                                                     what=" and ".join(different))
    unknown = [_BASIS_WORDS[k] for k in _BASIS_WORDS if basis.get(k) != "same"]
    if unknown:
        return "context_not_recorded", reason("context_not_recorded", what=" and ".join(unknown),
                                              was="was" if len(unknown) == 1 else "were")
    outcome = snap.get("assessment_outcome")
    if outcome != "assessed":
        return "same_scan_failed", reason("same_scan_failed", outcome=outcome or "unknown")
    if snap.get("issues_state") != "recorded":
        return "same_scan_issues_unavailable", reason("same_scan_issues_unavailable")
    completeness = snap.get("completeness")
    if completeness != "complete":
        not_checked = snap.get("rules_not_checked")
        rules = (f" ({len(not_checked)} rule(s) not checked)"
                 if isinstance(not_checked, list) and not_checked else "")
        words = {"partial": "only partly assessed", "not_assessed": "not assessed"}.get(
            completeness, "of unrecorded completeness")
        return "same_scan_partial", reason("same_scan_partial", completeness=words, rules=rules)
    if "issues_changed_after_write" in str(snap.get("integrity") or "").split(";"):
        return "same_scan_issues_changed", reason("same_scan_issues_changed")
    return None


def compact(comparison: dict | None) -> dict | None:
    """The per-row summary the scan index carries: counts, never lists."""
    if not comparison:
        return None
    compared = comparison["status"] == COMPARED
    n = (lambda key: len(comparison[key]) if compared else None)
    reopened = comparison.get("reopened")
    return {
        "status": comparison["status"],
        "reasonCode": comparison.get("reasonCode"),
        "reason": comparison.get("reason"),
        "baseline": comparison.get("baseline"),
        "renamed": comparison.get("renamed"),
        "introduced": n("introduced"),
        "resolved": n("resolved"),
        "persisting": n("persisting"),
        "reopened": len(reopened) if compared and reopened is not None else None,
        "notComparable": comparison.get("notComparable") if compared else None,
    }


def aggregate_same_scan(rows: list[dict | None]) -> dict:
    """Scan-level counts of the per-file same-scan comparisons (C6). Every file is counted in
    exactly one files* bucket; finding counts only over files that were compared."""
    t = {"filesCompared": 0, "filesNotRecorded": 0, "filesNotAvailable": 0,
         "filesBaselineUnusable": 0, "filesNotComparable": 0,
         "introduced": 0, "resolved": 0, "persisting": 0}
    for row in rows:
        status = (row or {}).get("status")
        if status == COMPARED:
            t["filesCompared"] += 1
            for key in ("introduced", "resolved", "persisting"):
                t[key] += row.get(key) or 0
        elif status == SAME_SCAN_NOT_RECORDED:
            t["filesNotRecorded"] += 1
        elif status == BASELINE_UNUSABLE:
            t["filesBaselineUnusable"] += 1
        elif status == NOT_COMPARABLE:
            t["filesNotComparable"] += 1
        else:
            t["filesNotAvailable"] += 1
    return t


def aggregate(rows: list[dict], *, same_scan_history: bool,
              same_scan_rows: list[dict | None] | None = None) -> dict:
    """Scan-level totals from the per-file comparisons (C4). Every file is counted in exactly one
    of filesCompared / filesNoBaseline / filesBaselineUnusable / filesNotComparable."""
    totals = {"introduced": 0, "resolved": 0, "persisting": 0, "reopened": 0,
              "notComparable": 0, "filesCompared": 0, "filesNoBaseline": 0,
              "filesBaselineUnusable": 0, "filesNotComparable": 0, "filesNew": 0,
              "filesRenamed": 0, "filesReopenedUndetermined": 0}
    for row in rows:
        c = row or {}
        status = c.get("status")
        if status == COMPARED:
            totals["filesCompared"] += 1
            for key in ("introduced", "resolved", "persisting"):
                totals[key] += c.get(key) or 0
            nc = c.get("notComparable") or {}
            totals["notComparable"] += int(nc.get("current") or 0)
            if c.get("renamed"):
                totals["filesRenamed"] += 1
            if c.get("reopened") is None:
                totals["filesReopenedUndetermined"] += 1
            else:
                totals["reopened"] += c["reopened"]
        elif status == NO_BASELINE:
            totals["filesNoBaseline"] += 1
            if c.get("reasonCode") == "no_earlier_assessment":
                totals["filesNew"] += 1
        elif status == BASELINE_UNUSABLE:
            totals["filesBaselineUnusable"] += 1
        else:
            totals["filesNotComparable"] += 1
    # A total over files whose reopened state is unknown is not a total: null, with the partial
    # sum kept under its own name so nobody reads it as the whole.
    reopened_known = totals["reopened"]
    if totals["filesReopenedUndetermined"]:
        totals["reopened"] = None
    totals["reopenedDetermined"] = reopened_known
    documents = len(rows)
    if totals["filesCompared"]:
        status = COMPARED
        parts = [f"{totals['filesCompared']} of {documents} document(s) were compared finding by "
                 f"finding with their own most recent earlier assessment"]
    elif totals["filesBaselineUnusable"]:
        status = BASELINE_UNUSABLE
        parts = ["no document has a usable earlier assessment"]
    elif documents:
        status = NO_BASELINE
        parts = ["no document in this scan has an earlier assessment under the same rubric "
                 "and scope"]
    else:
        status = NO_BASELINE
        parts = ["this scan holds no documents"]
    def has(n):
        return f"{n} {'has' if n == 1 else 'have'}"
    if totals["filesNew"]:
        parts.append(f"{has(totals['filesNew'])} no earlier assessment at all")
    other_no = totals["filesNoBaseline"] - totals["filesNew"]
    if other_no:
        parts.append(f"{has(other_no)} no comparable earlier assessment")
    if totals["filesBaselineUnusable"]:
        parts.append(f"{has(totals['filesBaselineUnusable'])} an earlier assessment that did not "
                     f"finish or used a different rubric or scope, so it is not a baseline")
    if totals["filesNotComparable"]:
        n = totals["filesNotComparable"]
        parts.append(f"{n} {'was' if n == 1 else 'were'} not fully assessed this time, so "
                     f"{'it is' if n == 1 else 'they are'} not compared")
    notes = []
    same_scan = aggregate_same_scan(same_scan_rows) if same_scan_rows is not None else None
    if not same_scan_history:
        notes.append("Re-assessing a document inside the same scan replaces its earlier finding "
                     "list, so changes within one scan are not compared.")
    elif same_scan is not None and rows:
        # ONE note (the scan summary is a single page): what was compared inside this scan, what
        # could not be, and — always, because it is the common case — that "none recorded" is not
        # "none happened".
        s = same_scan
        parts = []
        if s["filesCompared"]:
            parts.append(f"{s['filesCompared']} document(s) were compared with the assessment each "
                         f"replaced ({s['introduced']} new, {s['resolved']} no longer reported, "
                         f"{s['persisting']} still reported)")
        unusable = s["filesBaselineUnusable"] + s["filesNotComparable"]
        if unusable:
            parts.append(f"{unusable} could not be compared, each document says why")
        if s["filesNotRecorded"]:
            parts.append(f"{s['filesNotRecorded']} have no replaced assessment recorded, which is "
                         f"not evidence that none was replaced")
        if parts:
            notes.append("Re-assessments inside this scan: " + "; ".join(parts) + ".")
    if totals["notComparable"]:
        notes.append(f"{totals['notComparable']} current finding(s) have no detector location, so "
                     f"they cannot be matched one by one and are neither new nor resolved.")
    return {"status": status, "reason": "; ".join(parts) + ".", "totals": totals,
            "complete": True, "notes": notes,
            # C6: kept beside `totals`, not inside it — the across-scan totals are one population.
            "sameScan": same_scan}
