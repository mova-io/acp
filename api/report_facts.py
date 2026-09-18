"""Server-owned report FACTS — the one place a report's numbers come from.

WHY THIS MODULE EXISTS. Before it, the browser assembled a report out of several partial
projections (criterion rows, remediation diffs, a reviews map) and did the accounting itself.
That arrangement produced the defect this module is the fix for: ONE verified before/after record
for criterion 1.1.1 marked EVERY 1.1.1 finding of the document resolved, so a document with two
undescribed images and one saved description reported "2 resolved, 0 remaining, no outstanding
items". Nothing in the data said that. The client inferred it from a criterion-level record, and
an inference like that is invisible once it has been rendered into a PDF somebody signs.

So the rules below are deliberately conservative, and each one is a `null` where a number would
have been a guess:

* A verified saved change for a criterion NEVER credits the findings of that criterion.
  `findingsResolvedVerified` is non-null ONLY when a PER-FINDING ledger exists —
  `remediation_contribution`, which binds each proposal to explicit baseline finding ids. Note
  that `finding_disposition` is NOT such a ledger: `Store.set_finding_group_disposition` moves
  every finding of a (file, rule) group together, which is the criterion-wide crediting this
  module refuses to repeat.
* Zero findings is "no findings" only when the document was actually ASSESSED. `error`,
  `partial` and `not_assessed` are distinct states and none of them reads as a clean document.
* Saved changes include the applied-but-NOT-verified writes (`unverified_changes.saved_changes`)
  as well as the verified ones (`remediation_diff`). The unverified ones are precisely what a
  human has to look at, and a report that omits them describes a review as finished.
* Values the store clipped are declared clipped, with the real limit in `limits.valueMaxChars`.
  `Store.record_remediation_diffs` truncates before/after at 2000 characters on the way in, and
  the untruncated text is not retained anywhere else — so "clipped" here means GONE, not
  "see the appendix".

`factsDigest` is what binds a rendered report to the evidence it was built from
(`POST /scans/{sid}/report-render` refuses a stale one). It is computed over the canonical JSON
of the facts with TWO keys removed: `factsDigest` itself, and `generatedAt`. Excluding
`generatedAt` is not an oversight — it is a clock read, not evidence, and including it would make
every fetch produce a different digest and therefore every render 409.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone

FACTS_VERSION = 1

# Store.record_remediation_diffs: `str(...)[:2000]` for before/after, `[:500]` for note.
VALUE_MAX_CHARS = 2000
NOTE_MAX_CHARS = 500

# Per-document exports retain every stored record. The scan index is paginated,
# reviewer cards are bounded in the model, and PDF requests have a byte limit;
# none of those presentation limits should discard the full HTML evidence.
SAVED_CHANGES_LIMIT = None
FINDINGS_LIMIT = None
FILE_PAGE_DEFAULT = 100
# Raised from 500 (S1): the paging memo makes every page after the first cost a slice, so a larger
# page only saves round trips. A 1,000-row page of compact index rows is ~1 MB of JSON.
FILE_PAGE_MAX = 1000

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SC = re.compile(r"(?:SC[_-])?([1-4])[._]([0-9]+)[._]([0-9]+)")

# A status this repo writes into file_records that MEANS the document was opened and analysed.
# Deliberately an allow-list: an unknown status reads as not_assessed (and names itself in
# `stateReason`) rather than as a clean assessment nobody performed.
ASSESSED_STATUSES = frozenset({"certifiable", "uncertain", "compliant", "non_compliant",
                               "needs_review", "review", "reviewed", "remediated", "published"})
ERROR_STATUSES = frozenset({"error", "failed"})

# remediation_contribution finding state -> the facts vocabulary.
_LEDGER_STATE = {
    "fixed": "resolved_verified",
    "approved": "awaiting_review",
    "awaiting_review": "awaiting_review",
    "unresolved": "unresolved",
    "processing": "unknown",
    "unavailable": "unknown",
}


# ── canonical form and digests ────────────────────────────────────────────────

def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=str)


def facts_digest(facts: dict) -> str:
    """sha256 over the canonical facts, with `factsDigest` and `generatedAt` removed.

    `generatedAt` is excluded because it is a clock read: leaving it in would change the digest
    on every fetch and make the render route's staleness check fire on documents nothing had
    touched. What this digest covers is EVIDENCE — identity, assessment state, findings, saved
    changes, reviews, accounting, comparison — which is exactly what a rendered report asserts.
    """
    body = {k: v for k, v in (facts or {}).items() if k not in ("factsDigest", "generatedAt")}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


# Stream G's monkeypatchable seam; same function, the name the render route imports.
compute_facts_digest = facts_digest


def change_digest(rule_id: str, before: str, after: str) -> str:
    """The reviewer-decision binding for one saved change (contract: sha256 of rule/before/after)."""
    return hashlib.sha256(
        f"{rule_id}\n{before or ''}\n{after or ''}".encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sc_of(value) -> str | None:
    match = _SC.search(str(value or ""))
    return ".".join(match.groups()) if match else None


# ── change ids ────────────────────────────────────────────────────────────────
# Two shapes, because there are two kinds of saved change and only one of them has a database
# sequence to name it by:
#   verified   `{file}::{ruleId}::{seq}`        seq = remediation_diff.seq (an integer)
#   unverified `{file}::{ruleId}::u{16 hex}`    a content address, because the applied-but-
#                                               unverified records live in decision_log JSON and
#                                               have no stable ordinal of their own — an ordinal
#                                               would move the moment another edit was recorded,
#                                               and a reviewer's decision would follow it onto a
#                                               different change.

_UNVERIFIED_SUFFIX = re.compile(r"^u[0-9a-f]{16}$")


def verified_change_id(file: str, rule_id: str, seq) -> str:
    return f"{file}::{rule_id}::{int(seq)}"


def unverified_change_id(file: str, rule_id: str, locator, before, after) -> str:
    token = hashlib.sha256(
        f"{locator or ''}\n{before or ''}\n{after or ''}".encode("utf-8")).hexdigest()[:16]
    return f"{file}::{rule_id}::u{token}"


def parse_change_id(filename: str, change_id: str) -> tuple[str, int | None] | None:
    """(rule_id, seq|None) for a change id belonging to `filename`, or None when it does not.

    Returns seq=None for an applied-but-unverified id: those have no sequence, which is the
    caller's signal to look the change up by id rather than by (rule, seq).
    """
    prefix = f"{filename}::"
    if not change_id.startswith(prefix):
        return None
    rest = change_id[len(prefix):]
    rule_id, sep, tail = rest.rpartition("::")
    if not sep or not rule_id:
        return None
    if tail.isdigit():
        return rule_id, int(tail)
    if _UNVERIFIED_SUFFIX.match(tail):
        return rule_id, None
    return None


# ── artifact identity ─────────────────────────────────────────────────────────

CHECKSUM_KINDS = ("sha256", "md5", "quickxorhash", "other")


def checksum_kind(value) -> str | None:
    """What KIND of hash the source system gave us.

    A Drive md5 or a SharePoint quickXorHash is not a sha256 and must never be reported as one —
    the review's finding. Length/charset is the only signal available, so a 32-hex value is
    called md5 and anything else unrecognised is `other`, never silently promoted.
    """
    text = str(value or "").strip().lower()
    if not text:
        return None
    # `sha256:<64 hex>` is ACP's own tagged form (and the one the report header already accepts
    # as SHA-256 — report_render.server_identity). Calling it "other" here made the same report
    # say "SHA-256" in its identity table and "not sha-256, so no preview" beside the previews.
    if _SHA256.match(text) or _SHA256.match(text.removeprefix("sha256:")):
        return "sha256"
    if re.fullmatch(r"[0-9a-f]{32}", text):
        return "md5"
    if re.fullmatch(r"[0-9a-z+/=]{20,32}", text) and not text.isdigit():
        return "quickxorhash"
    return "other"


def artifact_identity(record: dict) -> dict:
    """Identity of the bytes a decision or a preview is about.

    `currentArtifact.kind` is 'corrected' ONLY when a corrected sha256 is recorded. A file that
    has been remediated but whose saved copy's digest was never written is 'unknown' — not
    'source', because the source checksum does not identify the bytes that now exist.
    """
    corrected = (record or {}).get("corrected_sha256") or None
    source = (record or {}).get("checksum") or None
    remediated_at = (record or {}).get("remediated_at") or None
    if corrected:
        current = {"kind": "corrected", "sha256": corrected}
    elif remediated_at:
        current = {"kind": "unknown", "sha256": None}
    elif source:
        current = {"kind": "source", "sha256": source}
    else:
        current = {"kind": "unknown", "sha256": None}
    kind = checksum_kind(source)
    return {
        "sourceChecksum": source,
        "sourceChecksumKind": kind,
        # Bare lower-case hex: the exact-bytes route compares against hashlib's hexdigest.
        "sourceSha256": str(source).strip().lower().removeprefix("sha256:") if kind == "sha256" else None,
        "correctedSha256": corrected,
        "currentArtifact": current,
        "remediatedAt": remediated_at,
    }


def current_artifact_sha256(record: dict) -> str | None:
    """The identity of the bytes that currently exist, or None when none is recorded."""
    return artifact_identity(record)["currentArtifact"]["sha256"]


def decision_binding_sha256(record: dict) -> str | None:
    """The ONLY digest a verdict on a saved change may be bound to: the saved copy's.

    Never the source checksum, and this is the review's finding rather than a nicety. A saved
    change is an edit to the CORRECTED copy. Binding a reviewer's "accepted" to the hash of the
    document that copy replaced produces a decision that stays fresh while the reviewed bytes are
    rewritten underneath it — and the source checksum is usually not even a sha256 (Drive md5,
    SharePoint quickXorHash), so it cannot identify ACP's own output at all. With no corrected
    digest recorded there is nothing to bind to: `None`, which reads as "freshness unknown" and
    refuses the write (409).
    """
    return (record or {}).get("corrected_sha256") or None


# ── reviewer decisions ────────────────────────────────────────────────────────

def evaluate_stale(decision: dict, current_sha256: str | None,
                   current_change_digest: str | None) -> tuple[bool | None, str]:
    """(stale, reason). `None` means FRESHNESS UNKNOWN and is never "accepted"."""
    bound = (decision or {}).get("artifact_sha256")
    if not current_sha256 or not bound:
        return None, ("the reviewed copy's identity is not recorded, so this decision's "
                      "freshness cannot be established")
    if bound != current_sha256:
        return True, "the document changed after this decision was recorded"
    if current_change_digest is None:
        return True, "the change this decision was about is no longer among the saved changes"
    if (decision or {}).get("change_digest") != current_change_digest:
        return True, "the change itself was rewritten after this decision was recorded"
    return False, "bound to the current copy and the current change"


# `edited` is a CORRECTION REQUESTED: PUT /change-reviews records the proposed replacement text
# and writes nothing into the document. It is never a confirmation.
VERDICT_LABELS = {
    "accepted": "confirmed",
    "edited": "correction requested",
    "rejected": "rejected",
    "unable": "unable to verify",
}


def _review_bucket(verdict: str, stale) -> str:
    if stale is True or stale is None:
        return "stale"
    return {"accepted": "accepted", "edited": "correctionRequested",
            "rejected": "rejected", "unable": "unable"}.get(verdict, "stale")


def _change_review_rows(store, scan_id: str, filename: str, *, owner: str,
                        context: dict | None) -> dict:
    """{kind: {value, updated_at}} for one file — from the scan build's ONE batched read (R-B4
    `change_reviews_for_scan`) when prefetch_scan_inputs took it, else the per-file read."""
    batched = (context or {}).get("reviews_by_file")
    if batched is not None:
        return batched.get(filename) or {}
    return store.get_change_reviews(scan_id, filename, owner=owner) or {}


def read_reviews(store, scan_id: str, filename: str, *, owner: str,
                 current_sha256: str | None, digests_by_id: dict,
                 context: dict | None = None) -> dict:
    """{changeId: decision + stale + staleReason}, freshness RE-EVALUATED on every read."""
    prefix = getattr(store, "CHANGE_REVIEW_KIND_PREFIX", "change_review:")
    out: dict[str, dict] = {}
    for kind, row in _change_review_rows(store, scan_id, filename, owner=owner,
                                         context=context).items():
        try:
            decision = json.loads(row.get("value") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(decision, dict):
            continue
        change_id = decision.get("change_id") or kind[len(prefix):]
        current = digests_by_id.get(change_id)
        stale, reason = evaluate_stale(decision, current_sha256, current)
        out[change_id] = {**decision, "change_id": change_id,
                          "current_change_digest": current,
                          "verdictLabel": VERDICT_LABELS.get(decision.get("verdict"), "unknown"),
                          "stale": stale, "staleReason": reason}
    return out


def review_counts(reviews: dict, saved_change_ids) -> dict:
    counts = {"pending": 0, "accepted": 0, "correctionRequested": 0, "rejected": 0,
              "unable": 0, "stale": 0}
    ids = list(saved_change_ids)
    for change_id in ids:
        review = reviews.get(change_id)
        if not review:
            counts["pending"] += 1
        else:
            counts[_review_bucket(review.get("verdict"), review.get("stale"))] += 1
    # Decisions whose change is gone are still stale facts about this document.
    for change_id, review in reviews.items():
        if change_id not in ids:
            counts["stale"] += 1
    return counts


# ── saved changes ─────────────────────────────────────────────────────────────

def _clipped(value) -> bool:
    return len(str(value or "")) >= VALUE_MAX_CHARS


def _diff_rows(store, scan_id: str, filename: str, context: dict | None) -> list[dict]:
    """One file's verified diff rows — from the scan build's ONE batched read (R-B4
    `remediation_diffs_for_scan`, whose per-file value IS get_remediation_diffs') when
    prefetch_scan_inputs took it, else the per-file read."""
    batched = (context or {}).get("diffs_by_file")
    if batched is not None:
        return list(batched.get(filename) or [])
    return store.get_remediation_diffs(scan_id, filename) or []


def saved_changes_from_pending(filename: str, entries) -> list[dict]:
    """unverified_changes.saved_changes' projection, applied to already-read pending records.

    The SAME comprehension that function applies to pending_records(store, scan, file) — so the
    scan-wide reader (R-B4, Stream E: "a scan-wide saved_changes can be built by the caller from
    this result with the same list comprehension saved_changes uses") gives the per-file list.
    Pinned against unverified_changes.saved_changes itself by
    tests/test_report_facts_scan_readers.py, so a change to the owner's projection fails there.
    """
    return [{**change, 'file': filename, 'rule_id': entry['rule_id'],
             'applied': True, 'verified': False, 'verification': 'not_verified',
             'artifact_sha256': entry['artifact_sha256'], 'reason': entry.get('reason'),
             'note': 'AI applied · not verified'}
            for entry in entries for change in entry.get('changes', [])]


def _pending_changes(store, scan_id: str, filename: str,
                     context: dict | None) -> tuple[list[dict], str]:
    """(applied-but-unverified changes, 'ok'|'unavailable') for one file.

    With the scan build's batched read (R-B4 `pending_records_for_scan`): a file the reader put in
    `errors` could not be read — 'unavailable', the count UNKNOWN, never zero. A file the reader
    neither answered nor reported is not assumed empty either: it is read per file.
    """
    import unverified_changes
    batched = (context or {}).get("pending_by_file")
    if batched is not None:
        if filename in (context.get("pending_errors") or {}):
            return [], "unavailable"
        if filename in batched:
            try:
                return saved_changes_from_pending(filename, batched[filename] or []), "ok"
            except (KeyError, TypeError, ValueError):
                return [], "unavailable"
    try:
        return unverified_changes.saved_changes(store, scan_id, filename) or [], "ok"
    except (KeyError, TypeError, ValueError):
        # pending_records parses JSON written by other subsystems; a malformed record is a data
        # problem, not a programming error. Anything else propagates rather than being turned
        # into an empty list — see blocks_certification, which fails the same three ways.
        return [], "unavailable"


def build_saved_changes(store, scan_id: str, filename: str, record: dict,
                        *, limit: int | None = SAVED_CHANGES_LIMIT,
                        context: dict | None = None) -> tuple[list[dict], bool, int, str]:
    """(changes, complete, total, unverifiedSource) — verified AND applied-but-unverified.

    `unverifiedSource` is 'ok' or 'unavailable'. It exists because silently dropping the
    applied-but-unverified records is the worst failure this function has: those are the changes
    a human still has to look at, and a report that omits them describes the review as finished.
    So a failure to read them makes the list INCOMPLETE and says so, rather than returning a
    shorter list that looks whole.
    """
    import report_location
    fmt = report_location.fmt_of_name(filename)
    rows: list[dict] = []
    for diff in _diff_rows(store, scan_id, filename, context):
        rule_id = str(diff.get("rule_id") or "")
        before, after = diff.get("before") or "", diff.get("after") or ""
        # R1 (store schema v59, every diff reader): `locator`, `page` and `location_source` —
        # "recorded" (the writer stored them), "legacy_note" (the store reconstructed the locator
        # at read time from an exact writer-note prefix; never a page) or None (unknown: both
        # are None, and the report says "Location not recorded" — never page 1).
        # The store's `location_source` is AUTHORITATIVE: a locator it did not vouch for is not
        # shown. Only a pre-R1 store (no key at all) is classified here.
        if "location_source" in diff:
            source = diff.get("location_source")
            if source not in (report_location.LOCATION_RECORDED,
                              report_location.LOCATION_LEGACY_NOTE):
                source = None
        else:
            source = (report_location.LOCATION_RECORDED
                      if (diff.get("locator") or diff.get("page")) else None)
        verified_locator = (diff.get("locator") or None) if source else None
        location = report_location.saved_change_location(
            verified_locator, diff.get("page") if source else None, fmt, source)
        rows.append({
            "id": verified_change_id(filename, rule_id, diff.get("seq") or 0),
            "ruleId": rule_id, "sc": sc_of(rule_id), "seq": int(diff.get("seq") or 0),
            "locator": verified_locator,
            "location": location,
            # Also on `location.source`; kept here for consumers of the first R1 shape. None
            # whenever there is no location to qualify (a Word "page" alone is not one).
            "locationSource": source if location is not None else None,
            "before": before, "after": after,
            "note": diff.get("note") or None,
            "verification": "verified",
            "verificationDetail": ("A re-check of the saved copy confirmed this change cleared "
                                   "the finding it was made for."),
            "artifactSha256": (record or {}).get("corrected_sha256") or None,
            "valueClipped": _clipped(before) or _clipped(after),
            "changeDigest": change_digest(rule_id, before, after),
            "findingIds": None,
            "source": "remediation_diff",
        })
    pending, unverified_source = _pending_changes(store, scan_id, filename, context)
    for change in pending:
        rule_id = str(change.get("rule_id") or "")
        before, after = change.get("before") or "", change.get("after") or ""
        locator = change.get("locator")
        # The writer's own change record: whatever it holds was recorded by the writer.
        location = report_location.saved_change_location(
            locator, change.get("page"), fmt,
            report_location.LOCATION_RECORDED if (locator or change.get("page")) else None)
        rows.append({
            "id": unverified_change_id(filename, rule_id, locator, before, after),
            "ruleId": rule_id, "sc": sc_of(rule_id), "seq": None,
            "locator": locator,
            "location": location,
            "locationSource": report_location.LOCATION_RECORDED if location is not None else None,
            "before": before, "after": after,
            "note": change.get("reason") or change.get("note") or None,
            "verification": "not_verified",
            "verificationDetail": ("AI applied this change and saved it; no re-check has "
                                   "confirmed it. A human has to look at this one."),
            "artifactSha256": change.get("artifact_sha256") or None,
            "valueClipped": False,
            "changeDigest": change_digest(rule_id, before, after),
            "findingIds": None,
            "source": "unverified_apply",
        })
    total = len(rows)
    return rows[:limit], (limit is None or total <= limit) and unverified_source == "ok", total, unverified_source


# ── assessment state ──────────────────────────────────────────────────────────

def assessment_state(file_row: dict, run: dict) -> dict:
    """assessed / partial / error / not_assessed — from what actually happened to THIS file.

    Never derived from the presence of catalog/coverage rows: those exist for every rule of every
    supported format whether or not the scan ever opened the document (the review's finding).
    """
    raw = str((file_row or {}).get("status") or "").strip().lower()
    skipped = int((file_row or {}).get("skipped_rules") or 0)
    if raw in ERROR_STATUSES:
        return {"state": "error",
                "stateReason": "the analyser did not produce a result for this document"}
    if raw not in ASSESSED_STATUSES:
        return {"state": "not_assessed",
                "stateReason": (f"this document was never assessed in this scan "
                                f"(recorded status: {raw or 'none'})")}
    if skipped > 0:
        return {"state": "partial",
                "stateReason": (f"{skipped} rule(s) were not evaluated for this document, so "
                                f"its finding list is incomplete")}
    run_status = str((run or {}).get("status") or "").strip().lower()
    if run_status in ("cancelled", "interrupted", "superseded"):
        return {"state": "partial",
                "stateReason": f"the scan that produced this assessment was {run_status}"}
    return {"state": "assessed", "stateReason": "the document was assessed and every in-scope "
                                                "rule was evaluated"}


# ── findings ──────────────────────────────────────────────────────────────────

def _location(issue: dict, fmt: str | None = None) -> dict | None:
    """Contract 1: the structured location (api/report_location.py is the one parser).

    Before it, this copied the detector's machine string into `label` and hard-coded
    slide/sheet/cell to None — a reviewer read "docx:paragraph:14", and PPTX shapes and XLSX
    cells were lost (audit gap L2).
    """
    import report_location
    return report_location.location_of_issue(issue or {}, fmt)


def _recommended_action(issue: dict) -> tuple[str | None, str | None]:
    """The source's OWN guidance, verbatim, or None — never a generic sentence.

    The review's finding: upstream records use `recommended_action` or `remediation` and a report
    that substitutes "Review and correct this issue" for a detector's specific instruction has
    thrown the useful half away. Nothing invents a value here; `None` renders as "Not recorded".
    """
    for key in ("recommended_action", "recommendedAction", "remediation", "action", "fix"):
        value = issue.get(key)
        if isinstance(value, str) and value.strip():
            return value, key
    return None, None


def _finding_id(scan_id: str, filename: str, rule_id: str, instance_key: str) -> str:
    return hashlib.sha256(canonical_json(
        ["finding-report-v1", scan_id, filename, rule_id, instance_key]).encode()).hexdigest()[:32]


def _instance_keys(issues: list[dict], sc: str, scope: str) -> list[str]:
    """Per-finding locators for one criterion group, using the ledger's own normalisation."""
    from document_wide_manifest import assessed_locations
    from finding_ledger import normalize_instance_key
    locations = assessed_locations(issues, sc, len(issues))
    return [normalize_instance_key(locations[i] if locations else None, ordinal=i + 1,
                                   aggregate_scope=scope)
            for i in range(len(issues))]


def build_findings(scan_id: str, filename: str, issues: list[dict], *,
                   ledger_scope: str | None = None) -> list[dict]:
    """One entry per issue row of the CURRENT assessment, each with a stable server-side id."""
    import report_location
    fmt = report_location.fmt_of_name(filename)
    groups: dict[str, list[dict]] = {}
    for issue in issues or []:
        sc = sc_of(issue.get("wcag")) or sc_of(issue.get("rule_id")) or sc_of(issue.get("ruleId"))
        groups.setdefault(sc or "", []).append(issue)
    out: list[dict] = []
    for sc in sorted(groups):
        # A deterministic order, so the ids and ordinals below do not move between two reads of
        # the same unchanged document (issue_records has no ordering of its own).
        rows = sorted(groups[sc], key=lambda i: (str(i.get("location") or ""),
                                                 -1 if i.get("page") is None else int(i["page"]),
                                                 str(i.get("detail") or ""),
                                                 str(i.get("rule_id") or i.get("ruleId") or "")))
        keys = _instance_keys(rows, sc, ledger_scope or scan_id) if sc else [
            f"row:{index}" for index in range(len(rows))]
        for index, issue in enumerate(rows):
            rule_id = str(issue.get("rule_id") or issue.get("ruleId") or "")
            action, action_source = _recommended_action(issue)
            out.append({
                "id": _finding_id(scan_id, filename, rule_id, keys[index]),
                "ledgerFindingId": None,
                "instanceKey": keys[index],
                "ruleId": rule_id,
                "sc": sc or None,
                "detail": issue.get("detail") or None,
                "severity": issue.get("severity") or None,
                "recommendedAction": action,
                "recommendedActionSource": action_source,
                "location": _location(issue, fmt),
                # Whether this finding's identity survives a RE-ASSESSMENT, and therefore whether
                # it can be compared with an earlier one. A real detector location does; a
                # synthesized ordinal does not — finding_ledger.normalize_instance_key is explicit
                # that an aggregate ordinal is only meaningful inside the snapshot that produced
                # the count. Matching two snapshots' ordinal 1 would invent a correspondence, so
                # these are flagged rather than compared, and never silently read as
                # "resolved plus introduced".
                "comparable": not keys[index].startswith(("aggregate-instance:", "row:")),
                "state": "open",
                "stateReason": "recorded by the current assessment and not resolved by any "
                               "per-finding record",
            })
    return out


# ── the per-finding resolution ledger ─────────────────────────────────────────

def read_ledger(store, scan_id: str, *, owner: str) -> dict | None:
    """The per-finding contribution ledger for this scan's remediate run, or None.

    `remediation_contribution` is the ONLY per-finding ledger in this repo: it binds each
    proposal to explicit baseline finding ids, so it can say WHICH finding a verified write
    resolved. `finding_disposition` cannot — set_finding_group_disposition moves a whole (file,
    rule) group at once, which is exactly the criterion-wide crediting this module exists to
    refuse — so it is deliberately not consulted here.
    """
    import remediation_contribution
    # NOT inside the try: a missing method here is a programming error, and swallowing it would
    # make "no per-finding ledger" — the answer that turns counts into nulls — indistinguishable
    # from a typo. Only read_contribution's own documented refusals are tolerated.
    run_ids = store.list_stage_execution_ids(scan_id, "remediate", owner=owner)
    for run_id in run_ids:
        try:
            result = remediation_contribution.read_contribution(store, owner, scan_id, run_id)
        except (PermissionError, KeyError, TypeError, ValueError):
            continue
        if result and result.get("findings"):
            return result
    return None


def apply_ledger(findings: list[dict], ledger: dict | None, *, filename: str,
                 document_id: str | None) -> str:
    """Map ledger finding states onto the facts findings. Returns 'per_finding' or 'none'.

    Matching is by EXACT identity — the ledger's own `stable_finding_id(document_id, rule_id,
    instance_key)` recomputed from this document's locations — and it is all-or-nothing per
    criterion group. A partial match is reported as no ledger at all, because a half-mapped
    criterion is the shape that produced the original defect: some findings credited from one
    record and the rest assumed.
    """
    if not ledger or not findings or not document_id:
        return "none"
    from finding_ledger import stable_finding_id
    by_id = {row.get("finding_id"): row for row in (ledger.get("findings") or [])
             if row.get("file") == filename}
    if not by_id:
        return "none"
    # The ledger keys findings by the dotted SUCCESS CRITERION (scan_rule_traces.rule_id), not by
    # the detector's own rule id — so the grouping here has to be by `sc` for the recomputed
    # identity to be the same string the ledger stored.
    groups: dict[str, list[dict]] = {}
    for finding in findings:
        groups.setdefault(finding["sc"] or "", []).append(finding)
    for sc, rows in groups.items():
        if not sc:
            continue
        pairs = [(f, by_id.get(stable_finding_id(document_id, sc, f["instanceKey"])))
                 for f in rows]
        if any(row is None for _f, row in pairs):
            continue                      # fail closed: this criterion has no usable ledger
        for finding, row in pairs:
            finding["ledgerFindingId"] = row.get("finding_id")
            finding["state"] = _LEDGER_STATE.get(row.get("state"), "unknown")
            finding["stateReason"] = row.get("reason") or finding["state"]
    # All or nothing. A half-mapped document would mix ledger facts with assumptions about the
    # rest, which is the shape the original defect had.
    return "per_finding" if all(f["ledgerFindingId"] for f in findings) else "none"


# ── accounting ────────────────────────────────────────────────────────────────

def build_accounting(findings: list[dict], findings_complete: bool, findings_total,
                     saved_changes: list[dict], reviews: dict, ledger_kind: str,
                     *, state: str = "assessed", state_reason: str = "",
                     saved_changes_complete: bool = True) -> dict:
    verified = [c for c in saved_changes if c["verification"] == "verified"]
    unverified = [c for c in saved_changes if c["verification"] != "verified"]
    resolved = None
    open_count = None
    if state != "assessed":
        # ZERO FINDINGS IS NOT "NO FINDINGS" HERE, and this is the review's finding stated as a
        # number rather than as a label. An error/partial/never-assessed document has an empty
        # issue list because nothing looked, so "0 open" would be a claim about a document ACP
        # never opened. The count is unknown; only `assessment.state` has anything to say.
        reason = f"{state_reason or state} — the number of open findings is not known"
    elif ledger_kind == "per_finding" and findings_complete:
        resolved = sum(1 for f in findings if f["state"] == "resolved_verified")
        open_count = sum(1 for f in findings if f["state"] != "resolved_verified")
        reason = ("each finding's state comes from the per-finding remediation ledger")
    else:
        touched = {c["sc"] for c in saved_changes if c["sc"]}
        overlapping = any(f["sc"] in touched for f in findings)
        if not findings_complete:
            reason = "the finding list is incomplete, so nothing can be counted from it"
        elif not saved_changes_complete:
            # `touched` is derived from the saved changes we HAVE. With a bounded or unreadable
            # list, "no saved change touches a criterion with a finding" is a statement about the
            # part we read — which is the shape of every wrong count in this module's history.
            reason = ("the saved-change list is incomplete, so whether a change already addressed "
                      "one of these findings cannot be established")
        elif overlapping:
            # The original defect, refused. A change was saved for a criterion that still has
            # findings and no record says WHICH finding it resolved — so the honest count of
            # what remains is unknown, not "all of them" and not "all but one".
            reason = ("saved changes exist for criteria that still have findings, but no "
                      "per-finding ledger records which finding each change resolved")
        else:
            open_count = len(findings)
            reason = ("no saved change touches any criterion with a finding, so every recorded "
                      "finding is still open")
    return {
        "findingsTotal": findings_total,
        "findingsOpen": open_count,
        "findingsResolvedVerified": resolved,
        "resolutionLedger": ledger_kind,
        "accountingReason": reason,
        "savedChangesVerified": len(verified),
        "savedChangesUnverified": len(unverified),
        "humanReviews": review_counts(reviews, [c["id"] for c in saved_changes]),
    }


# ── comparison with an earlier assessment ─────────────────────────────────────

def scope_digest(run: dict) -> str | None:
    scope = (run or {}).get("scan_scope")
    rubric = (run or {}).get("rubric_hash")
    if scope is None and not rubric:
        return None
    return hashlib.sha256(canonical_json({"scan_scope": scope, "rubric_hash": rubric})
                          .encode()).hexdigest()


# ── report-history reads (R-B1/R-B2/R-B3, the #2131 owner's Stream G) ──────────────────────────
# Stream G implements them as functions taking the store in `api/report_history.py`; Stream D adds
# thin Store wrappers with the same names. Whichever has landed is used — the Store method first,
# then `report_history.<fn>(store, …)` — and with neither the facts say the read is not available
# rather than implying there was nothing to read.

_report_history_missing = False


def _report_history():
    import sys
    module = sys.modules.get("report_history")
    if module is not None:
        return module
    global _report_history_missing
    if _report_history_missing:
        return None
    try:
        import report_history   # noqa: F401  (Stream G's module; absent on older bases)
    except ImportError:
        _report_history_missing = True
        return None
    return sys.modules.get("report_history")


def history_reader(store, name: str):
    """`store.<name>` when the Store has it, else `report_history.<name>` bound to the store, else
    None. Both take the same arguments after the store."""
    method = getattr(store, name, None)
    if callable(method):
        return method
    module = _report_history()
    fn = getattr(module, name, None) if module is not None else None
    if callable(fn):
        return lambda *args, **kwargs: fn(store, *args, **kwargs)
    return None


def prefetch_baselines(store, scan_id: str, names: list[str], *, owner: str,
                       context: dict) -> str:
    """Load every file's baseline for a scan-index build in one store read, when the store has it.

    Returns 'batched' or 'per_file'. `previous_assessments_for_scan` is R-B1 (owner response):
    `{file: previous}` with EXACTLY `previous_assessment_for_file`'s value per file, files with no
    baseline absent. Until it exists — or if it fails — each file falls back to the per-file read
    inside `_load_baseline`, memoised for this build. The per-file route goes through
    `_load_baseline` too, which uses the SAME batched reader (with `files=[name]`) whenever it
    exists — so the index row and the per-file route share one selection and one digest even
    before the Store's per-file method is made to delegate to it.
    """
    batched = history_reader(store, "previous_assessments_for_scan")
    if batched is None:
        return "per_file"
    try:
        got = batched(scan_id, owner=owner, files=list(names)) or {}
    except Exception:
        return "per_file"
    memo = context.setdefault("previous_by_file", {})
    for name in names:
        memo[name] = (got.get(name), False)
    return "batched"


def _unverified_changes_module():
    import unverified_changes
    return unverified_changes


def prefetch_scan_inputs(store, scan_id: str, names: list[str], *, owner: str,
                         context: dict) -> dict:
    """R-B4: read a scan-index build's three per-file inputs ONCE each, when the readers exist.

    Returns {"diffs", "reviews", "unverified"} → 'batched' | 'per_file'. Each batched value is fed
    to the SAME per-file builders (build_saved_changes / read_reviews), so a row's projection and
    digest are byte-identical to the per-file route's, which never prefetches. Every reader is
    feature-detected, and one that is missing, refuses, raises, or answers with the wrong shape
    leaves THAT input on its per-file read — an unreadable batch is never an empty one.

    - `remediation_diffs_for_scan(scan_id, *, owner, files)` (Store method or report_history):
      `result.get(f, [])` IS get_remediation_diffs(scan_id, f), every row, v59 locations.
    - `change_reviews_for_scan(scan_id, owner, *, files)`: `result.get(f, {})` IS
      get_change_reviews(scan_id, f, owner=owner). The owner is REQUIRED — the per-file read drops
      its filter for a falsy owner and the batched one refuses, so a falsy owner stays per file.
    - `unverified_changes.pending_records_for_scan(store, scan_id, files, *, errors)` (Stream E):
      per file EXACTLY pending_records(); a file it could not read is in `errors` and becomes
      unverifiedSource 'unavailable'. Asked for every name, so a name it neither answers nor
      reports is read per file rather than assumed to have nothing pending.
    """
    modes = {"diffs": "per_file", "reviews": "per_file", "unverified": "per_file"}
    diffs = history_reader(store, "remediation_diffs_for_scan")
    if diffs is not None:
        try:
            got = diffs(scan_id, owner=owner)
        except Exception:
            got = None
        if isinstance(got, dict):
            context["diffs_by_file"] = got
            modes["diffs"] = "batched"
    reviews = history_reader(store, "change_reviews_for_scan")
    if reviews is not None and isinstance(owner, str) and owner:
        try:
            got = reviews(scan_id, owner)
        except Exception:
            got = None
        if isinstance(got, dict):
            context["reviews_by_file"] = got
            modes["reviews"] = "batched"
    pending = getattr(_unverified_changes_module(), "pending_records_for_scan", None)
    if callable(pending):
        errors: dict = {}
        try:
            got = pending(store, scan_id, list(names), errors=errors)
        except Exception:
            got = None
        if isinstance(got, dict):
            context["pending_by_file"] = got
            context["pending_errors"] = errors
            modes["unverified"] = "batched"
    return modes


def _load_baseline(store, scan_id: str, filename: str, *, owner: str,
                   context: dict | None) -> tuple[dict | None, bool]:
    """(found|None, lookup_failed) — memoised per build so a scan index never reads twice."""
    memo = (context or {}).get("previous_by_file")
    if memo is not None and filename in memo:
        return memo[filename]
    result = None
    batched = history_reader(store, "previous_assessments_for_scan")
    if batched is not None:
        try:
            result = ((batched(scan_id, owner=owner, files=[filename]) or {}).get(filename), False)
        except Exception:
            result = None      # same fallback the scan-index prefetch takes
    if result is None:
        try:
            result = (store.previous_assessment_for_file(scan_id, filename, owner=owner), False)
        except Exception:
            result = (None, True)
    if memo is not None:
        memo[filename] = result
    return result


def _previous_ledger(store, run_id: str, *, owner: str, context: dict | None) -> dict | None:
    memo = (context or {}).setdefault("prev_ledgers", {}) if context is not None else None
    if memo is not None and run_id in memo:
        return memo[run_id]
    ledger = read_ledger(store, run_id, owner=owner)
    if memo is not None:
        memo[run_id] = ledger
    return ledger


def build_previous(store, scan_id: str, filename: str, run: dict, *, owner: str,
                   context: dict | None = None, current_findings: list[dict] | None = None,
                   current_state: dict | None = None) -> tuple:
    """(previous|None, reason, comparison) — an earlier assessment of THE SAME document, or why not.

    "Same document" is source identity (the provider's own file id, or source+path when there is
    none), NOT the filename and NOT the checksum of a rewritten copy: a remediated copy has a
    different hash and a renamed file has a different name, and neither fact says anything about
    whether the two assessments are comparable. The rubric and frozen scope must match too — a
    count taken under a different criterion set is not a baseline, it is a different question.

    `comparison` is contract 4 (api/report_comparison.py): the finding-by-finding classification,
    made here from the baseline actually loaded, so no client re-derives it from ids.
    """
    import report_comparison as rc
    found, failed = _load_baseline(store, scan_id, filename, owner=owner, context=context)
    if failed:
        text = rc.reason("lookup_failed")
        return None, text, rc.empty(rc.BASELINE_UNUSABLE, "lookup_failed", text)
    if not found:
        text = rc.reason("no_earlier_assessment")
        return None, text, rc.empty(rc.NO_BASELINE, "no_earlier_assessment", text)
    previous_run = found["run"]
    previous_file = found["file_row"]
    ref = rc.baseline_ref(found)
    if scope_digest(previous_run) != scope_digest(run):
        text = rc.reason("different_scope")
        return None, text, rc.empty(rc.BASELINE_UNUSABLE, "different_scope", text, ref)
    # C2: an earlier row that did not finish is not a baseline. Its empty finding list would make
    # every current finding read "new since then".
    problem = rc.baseline_problem(found, assessed_statuses=ASSESSED_STATUSES,
                                  error_statuses=ERROR_STATUSES)
    if problem:
        code, text = problem
        return None, text, rc.empty(rc.BASELINE_UNUSABLE, code, text, ref)

    # The baseline scan's OWN per-finding ledger, when it has one, says which earlier findings had
    # been resolved — the only evidence that a finding reported again was "reopened" (C5).
    previous_ledger = _previous_ledger(store, previous_run["id"], owner=owner, context=context)
    previous_findings = build_findings(
        previous_run["id"], previous_file["file"], found.get("issues") or [],
        ledger_scope=(previous_ledger or {}).get("snapshot_id") or previous_run["id"])
    previous_kind = apply_ledger(
        previous_findings, previous_ledger, filename=previous_file["file"],
        document_id=_document_id(previous_run, previous_file, previous_file["file"], previous_file))
    # Ids are scan-scoped by construction, so re-key the baseline onto THIS scan's id space —
    # otherwise every finding would read as both resolved and introduced.
    for finding in previous_findings:
        finding["id"] = _finding_id(scan_id, filename, finding["ruleId"], finding["instanceKey"])
    identity = artifact_identity(previous_file)
    renamed = previous_file.get("file") != filename
    previous = {
        "scanId": previous_run["id"],
        "file": previous_file["file"],
        "generatedAt": previous_run.get("assessed_at") or previous_run.get("completed_at"),
        "sha256": identity["sourceSha256"],
        "sourceChecksum": identity["sourceChecksum"],
        "sourceChecksumKind": identity["sourceChecksumKind"],
        "scopeDigest": scope_digest(previous_run),
        "scanScope": previous_run.get("scan_scope"),
        "score": previous_file.get("score"),
        # The server matched the two rows by source identity, so a different name is a RENAME of
        # this document, not a different one (C3).
        "sameDocument": True,
        "matchedBy": "provider_file_id" if previous_file.get("drive_file_id") else "source_path",
        "findings": [{"id": f["id"], "ruleId": f["ruleId"], "sc": f["sc"],
                      "detail": f["detail"], "location": f["location"],
                      "comparable": f["comparable"]}
                     for f in previous_findings],
        "comparableFindings": sum(1 for f in previous_findings if f["comparable"]),
    }
    previous_reason = "an earlier assessment of the same document under the same rubric and scope"
    state = (current_state or {}).get("state", "assessed")
    if state != "assessed":
        text = rc.reason("current_not_assessed", state=state,
                         why=(current_state or {}).get("stateReason") or state)
        return previous, previous_reason, rc.empty(rc.NOT_COMPARABLE, "current_not_assessed",
                                                   text, ref)
    states = ({f["id"]: f["state"] for f in previous_findings}
              if previous_kind == "per_finding" else None)
    classified = rc.classify(current_findings or [], previous_findings, states)
    when = previous["generatedAt"] or "an earlier date"
    text = (f"matched finding by finding against the assessment of {when} of the same document "
            f"({'same provider file id' if previous['matchedBy'] == 'provider_file_id' else 'same source and path'}), "
            f"under the same rubric and scope"
            + (f"; the document was named {previous_file.get('file')} then" if renamed else ""))
    comparison = {
        "status": rc.COMPARED, "reasonCode": "compared", "reason": text, "baseline": ref,
        "renamed": renamed, **classified,
        "reopenedReason": None if states is not None else (
            "the earlier assessment has no per-finding resolution ledger, so whether a finding "
            "reported again had been fixed in between is not recorded"),
    }
    return previous, previous_reason, comparison


# ── the builders ──────────────────────────────────────────────────────────────

def _platform_version() -> str | None:
    import os
    return (os.environ.get("ACP_BUILD_VERSION") or "").strip() or None


def _target_level() -> str:
    try:
        from assessment_policy import config_target
        return config_target()
    except Exception:
        return "AA"


def _document_id(run: dict, file_row: dict, filename: str, record: dict) -> str | None:
    try:
        from documents import resolve_doc_id
        return resolve_doc_id(run.get("source") or "local",
                              (record or {}).get("drive_file_id") or file_row.get("drive_file_id"),
                              filename, (record or {}).get("checksum"))
    except Exception:
        return None


def scan_context(store, scan_id: str, *, owner: str) -> dict | None:
    """The three scan-wide reads every file's facts need, taken ONCE.

    Without this the scan-level builder is quadratic: it called build_file_facts per document and
    each call re-read the whole scan, every file record, and the contribution ledger. This repo
    has already been bitten by a per-file read on a ~6,916-file estate (the Discover tab hung
    indefinitely, api/store.py's get_scan comment records it), and a report of a large scan is
    exactly that shape again.
    """
    scan = store.get_scan(scan_id, owner=owner)
    if scan is None:
        return None
    return {"scan": scan, "run": scan.get("run") or {},
            # A dict, not a list scan: build_file_facts looked each file up with `next(...)` over
            # the whole list, which made a scan-index build quadratic in the file count (S1).
            "files_by_name": {f.get("file"): f for f in (scan.get("files") or []) if f.get("file")},
            "records": store.get_file_records(scan_id, owner=owner) or {},
            "ledger": read_ledger(store, scan_id, owner=owner),
            # Filled lazily, once per build: baselines, baseline ledgers, queue approvals.
            "previous_by_file": {}, "prev_ledgers": {}}


# ── queue approvals that need a recheck (audit gap C11) ───────────────────────

APPROVAL_RECHECK_NOTE = (
    "Approved in the review queue against a source version or proposed value that has since "
    "changed, so it needs a reviewer to approve it again against the current version. This "
    "report records that state; it does not re-queue or change the approval.")


def _approvals_by_file(store, scan_id: str, *, owner: str, context: dict) -> dict | None:
    """{file: [queue rows]} for this scan, read ONCE per build; None when it could not be read."""
    if "hitl_by_file" not in context:
        try:
            rows = store.list_hitl_queue(scan_id=scan_id, owner=owner) or []
        except (KeyError, TypeError, ValueError):
            context["hitl_by_file"] = None
        else:
            grouped: dict[str, list[dict]] = {}
            for row in rows:
                grouped.setdefault(row.get("file"), []).append(row)
            context["hitl_by_file"] = grouped
    return context["hitl_by_file"]


def build_approvals(rows: list[dict] | None) -> dict:
    """Queue approvals for one document, with the ones whose binding moved named individually.

    `approval_recheck_required` is computed by Store.list_hitl_queue from the approval's own pin
    (source revision, value digest, proposal snapshot). The report states it; it never implies
    that marking something stale re-queued it — nothing here writes.
    """
    if rows is None:
        return {"source": "unavailable", "approvedAwaitingWrite": None, "pending": None,
                "recheckRequired": None, "note": APPROVAL_RECHECK_NOTE}
    approved = [r for r in rows if str(r.get("status") or "") == "approved" and not r.get("applied")]
    recheck = [{"id": r.get("id"), "ruleId": r.get("rule_id"), "sc": sc_of(r.get("rule_id")),
                "ruleName": r.get("rule_name"), "reviewedAt": r.get("reviewed_at"),
                "approvedSourceRevision": r.get("approved_source_revision"),
                "decisionVersion": r.get("decision_version")}
               for r in approved if r.get("approval_recheck_required") is True]
    recheck.sort(key=lambda r: (str(r["ruleId"] or ""), str(r["id"] or "")))
    return {"source": "ok", "approvedAwaitingWrite": len(approved),
            "pending": sum(1 for r in rows if str(r.get("status") or "") == "pending"),
            "recheckRequired": recheck, "note": APPROVAL_RECHECK_NOTE}


# ── reviewer decisions recorded on EARLIER scans of this document (audit gap C7) ─

PRIOR_DECISIONS_UNAVAILABLE = (
    "Decisions recorded against earlier scans of this document are not shown: ACP cannot yet "
    "list them by document. They are not carried forward to this scan's changes, and their "
    "absence here is not evidence that there were none.")
# (Store request R-B3, /tmp/acp-report-followup-store-requests.md, is what lifts this.)


PRIOR_DECISIONS_LIMIT = 200


def build_prior_decisions(store, scan_id: str, filename: str, *, owner: str) -> tuple:
    """(list|None, reason, bounds|None). Earlier decisions are EVIDENCE about the document, never
    current decisions about this scan's changes — each is marked not_carried_forward.

    R-B3 (owner response): `change_reviews_for_document(scan_id, file, *, owner, limit)` returns
    None for an unknown/foreign scan, else `{"decisions": [...], "total", "returned", "limit",
    "truncated"}`. `bounds` carries those counts, so a truncated list reads "showing N of TOTAL"
    and is never mistaken for the whole history.
    """
    reader = history_reader(store, "change_reviews_for_document")
    if reader is None:
        return None, PRIOR_DECISIONS_UNAVAILABLE, None
    try:
        result = reader(scan_id, filename, owner=owner, limit=PRIOR_DECISIONS_LIMIT)
    except (KeyError, TypeError, ValueError):
        return (None, "decisions recorded against earlier scans of this document could not be "
                      "read", None)
    if result is None:
        return (None, "decisions recorded against earlier scans of this document are not "
                      "available for this scan", None)
    if isinstance(result, dict):
        rows = result.get("decisions") or []
        total = result.get("total")
        bounds = {"total": total if isinstance(total, int) else None,
                  "returned": len(rows),
                  "limit": result.get("limit"),
                  "truncated": bool(result.get("truncated"))}
    else:
        # The first request's shape (a bare list); nothing about truncation is known then.
        rows = list(result)
        bounds = {"total": None, "returned": len(rows), "limit": None, "truncated": None}
    out = []
    for row in rows:
        try:
            value = json.loads(row.get("value") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        out.append({"scanId": row.get("scan_id"), "file": row.get("file"),
                    "changeId": value.get("change_id") or row.get("change_id"),
                    "verdict": value.get("verdict"),
                    "verdictLabel": VERDICT_LABELS.get(value.get("verdict"), "unknown"),
                    "reviewer": value.get("reviewer"), "at": value.get("at") or row.get("ts"),
                    "scanAt": row.get("scan_at"),
                    "artifactSha256": value.get("artifact_sha256"),
                    "status": "not_carried_forward"})
    reason = ("decisions recorded against earlier scans of this document; they apply to the "
              "bytes reviewed then and are not carried forward to this scan's changes")
    if bounds["truncated"]:
        total = bounds["total"] if bounds["total"] is not None else "more"
        reason = f"{reason} (showing {bounds['returned']} of {total}, newest first)"
    elif not rows:
        reason = "no decision is recorded against an earlier scan of this document"
    bounds["unreadable"] = len(rows) - len(out)
    if bounds["unreadable"]:
        reason = f"{reason}; {bounds['unreadable']} recorded decision(s) could not be read"
    return out, reason, bounds


# ── the assessment this one REPLACED inside the same scan (audit gap C6, R-B2) ───────────────

def build_same_scan_history(store, scan_id: str, filename: str, *, owner: str,
                            current_findings: list[dict], current_state: dict,
                            ledger_scope: str | None) -> dict:
    """This document compared with the assessment its current one replaced in THIS scan.

    R-B2 (owner response): `prior_assessment_in_scan(scan_id, file, *, owner)` returns None when
    no replaced assessment was CAPTURED — which is not evidence that none existed — or
    `previous_assessment_for_file`'s keys plus a `snapshot` block describing what was recorded at
    the replaced write. Usability is decided from that block ONLY (report_comparison.
    same_scan_problem): the envelope's `run` is never fed to scope_digest (a not_recorded run
    would read as "different scope"), and this scan's per-finding ledger is never applied to the
    snapshot — it describes the current assessment, not the replaced one.
    """
    import report_comparison as rc
    reader = history_reader(store, "prior_assessment_in_scan")
    if reader is None:
        return rc.same_scan_empty(rc.SAME_SCAN_NOT_AVAILABLE, "reader_unavailable")
    try:
        found = reader(scan_id, filename, owner=owner)
    except (KeyError, TypeError, ValueError):
        return rc.same_scan_empty(rc.BASELINE_UNUSABLE, "lookup_failed")
    if not found:
        return rc.same_scan_empty(rc.SAME_SCAN_NOT_RECORDED, "not_recorded")
    snapshot = rc.same_scan_snapshot(found, scan_id)
    problem = rc.same_scan_problem(found)
    if problem:
        return rc.same_scan_empty(rc.BASELINE_UNUSABLE, problem[0], snapshot, text=problem[1])
    if (current_state or {}).get("state", "assessed") != "assessed":
        text = rc.reason("current_not_assessed", state=current_state.get("state"),
                         why=current_state.get("stateReason") or current_state.get("state"))
        return rc.same_scan_empty(rc.NOT_COMPARABLE, "current_not_assessed", snapshot, text=text)
    # Built in THIS document's id space (same scan, current name), so ids match the current
    # findings; a finding without a detector location stays not comparable, as across scans.
    previous_findings = build_findings(scan_id, filename, found.get("issues") or [],
                                       ledger_scope=ledger_scope or scan_id)
    classified = rc.classify(current_findings or [], previous_findings, None)
    total = snapshot.get("historyTotal")
    of_n = f" (the most recent of {total})" if isinstance(total, int) and total > 1 else ""
    text = (f"matched finding by finding against the assessment this one replaced inside this "
            f"scan, written {snapshot.get('writtenAt') or 'at a time not recorded'}{of_n}, under "
            f"the same rubric and scope as recorded at both writes")
    return {**rc.same_scan_empty(rc.COMPARED, "compared", snapshot, text=text), **classified,
            "reopened": None,
            "reopenedReason": rc.reason("same_scan_no_ledger")}


def build_file_facts(store, scan_id: str, filename: str, *, owner: str,
                     saved_changes_limit: int | None = SAVED_CHANGES_LIMIT,
                     context: dict | None = None,
                     include_previous: bool = True) -> dict | None:
    """The authoritative facts for ONE document, or None when the caller cannot see it."""
    context = context or scan_context(store, scan_id, owner=owner)
    if context is None:
        return None
    run = context["run"]
    files_by_name = context.get("files_by_name")
    if files_by_name is None:     # a hand-built context (tests); same answer, built once
        files_by_name = context["files_by_name"] = {
            f.get("file"): f for f in (context["scan"].get("files") or []) if f.get("file")}
    file_row = files_by_name.get(filename)
    if file_row is None:
        return None
    record = context["records"].get(filename) or {}
    identity = artifact_identity(record)
    state = assessment_state(file_row, run)

    issues = list(file_row.get("issues") or [])
    findings_total = len(issues)
    findings_complete = FINDINGS_LIMIT is None or findings_total <= FINDINGS_LIMIT
    ledger = context["ledger"]
    ledger_scope = (ledger or {}).get("snapshot_id") or scan_id
    findings = build_findings(scan_id, filename, issues[:FINDINGS_LIMIT],
                              ledger_scope=ledger_scope)
    ledger_kind = apply_ledger(findings, ledger, filename=filename,
                               document_id=_document_id(run, file_row, filename, record))

    changes, changes_complete, changes_total, unverified_source = build_saved_changes(
        store, scan_id, filename, record, limit=saved_changes_limit, context=context)
    # Freshness is judged against the SAVED COPY's digest, not the current artifact generally:
    # see decision_binding_sha256. With no corrected digest recorded every decision reads
    # "freshness unknown", which is the honest answer and never a confirmation.
    reviews = read_reviews(store, scan_id, filename, owner=owner,
                           current_sha256=decision_binding_sha256(record),
                           digests_by_id={c["id"]: c["changeDigest"] for c in changes},
                           context=context)
    if include_previous:
        previous, previous_reason, comparison = build_previous(
            store, scan_id, filename, run, owner=owner, context=context,
            current_findings=findings, current_state=state)
    else:
        # Only for callers that explicitly do not want the comparison. The scan index does NOT
        # use this: contract 3a requires its rows to be the per-file route's exact projection,
        # so row.factsDigest equals the per-file factsDigest for unchanged evidence.
        previous, previous_reason, comparison = None, ("comparison was not requested"), None
    by_file = _approvals_by_file(store, scan_id, owner=owner, context=context)
    approvals = build_approvals(None if by_file is None else by_file.get(filename, []))
    prior_decisions, prior_decisions_reason, prior_decisions_bounds = build_prior_decisions(
        store, scan_id, filename, owner=owner)
    same_scan = build_same_scan_history(
        store, scan_id, filename, owner=owner, current_findings=findings,
        current_state=state, ledger_scope=ledger_scope)

    facts = {
        "factsVersion": FACTS_VERSION,
        "factsDigest": None,
        "generatedAt": _now(),
        "kind": "file",
        "identity": {
            "scanId": scan_id, "file": filename,
            "sourceChecksum": identity["sourceChecksum"],
            "sourceChecksumKind": identity["sourceChecksumKind"],
            "sourceSha256": identity["sourceSha256"],
            "correctedSha256": identity["correctedSha256"],
            "currentArtifact": identity["currentArtifact"],
            "remediatedAt": identity["remediatedAt"],
            "platformVersion": _platform_version(),
            "targetLevel": _target_level(),
            "targetLevelSource": "workspace_config",
            "scopeDigest": scope_digest(run),
            "scanScope": run.get("scan_scope"),
            "rubricHash": run.get("rubric_hash"),
        },
        "assessment": {
            "state": state["state"], "stateReason": state["stateReason"],
            "rawStatus": file_row.get("status"),
            "assessedAt": run.get("assessed_at") or run.get("completed_at"),
            "assessedAtSource": "scan",
            "score": file_row.get("score"),
            "engine": file_row.get("engine"),
            # Whether the recorded findings describe the source or the corrected copy. With no
            # corrected copy the answer is 'source'; with one, nothing in file_records says which
            # artifact the stored findings came from, and 'unknown' is the truthful answer.
            "artifactAssessed": "source" if not identity["correctedSha256"] else "unknown",
            "findingsComplete": findings_complete,
            "findingsTotal": findings_total,
        },
        "findings": findings,
        "savedChanges": changes,
        "savedChangesComplete": changes_complete,
        "savedChangesTotal": changes_total,
        "savedChangesLimit": saved_changes_limit,
        # 'unavailable' means the applied-but-unverified records could not be read. Those are the
        # ones a human must review, so their absence is reported, never rendered as "none".
        "savedChangesUnverifiedSource": unverified_source,
        "reviews": reviews,
        "accounting": build_accounting(findings, findings_complete, findings_total,
                                       changes, reviews, ledger_kind,
                                       state=state["state"], state_reason=state["stateReason"],
                                       saved_changes_complete=changes_complete),
        "previous": previous,
        "previousReason": previous_reason,
        # Contract 4: the server's classification. Clients render it; they do not re-derive it.
        "comparison": comparison,
        # C11: review-queue approvals whose pin moved. C7: decisions on earlier scans.
        "approvals": approvals,
        "priorDecisions": prior_decisions,
        "priorDecisionsReason": prior_decisions_reason,
        # {total, returned, limit, truncated, unreadable} from R-B3, or None when not read.
        "priorDecisionsBounds": prior_decisions_bounds,
        # C6 (R-B2): this document against the assessment it REPLACED inside this same scan.
        "sameScanHistory": same_scan,
        "limits": {"valueMaxChars": VALUE_MAX_CHARS, "noteMaxChars": NOTE_MAX_CHARS,
                   "savedChangesLimit": saved_changes_limit,
                   "findingsLimit": FINDINGS_LIMIT,
                   "valueClippedIsUnrecoverable": True},
    }
    facts["factsDigest"] = facts_digest(facts)
    return facts


class ScanFactsChanged(Exception):
    """A page was requested for a snapshot digest the evidence no longer has (contract 3 → 409)."""

    def __init__(self, requested: str, current: str | None):
        super().__init__(SNAPSHOT_CHANGED_DETAIL)
        self.requested = requested
        self.current = current


SNAPSHOT_CHANGED_DETAIL = "report evidence changed while paging; restart the export"

# The memoised full index (S1). A PAGING AID ONLY (contract RESUME-2): it is served solely to a
# page request that carries `digest=X` equal to the memo's digest, for at most this long. A request
# with no digest always rebuilds; `verify_digest` (the render route) and the per-file route never
# read it. So the worst a stale memo can do is hand a caller the remaining pages of the snapshot
# they asked for by digest — which is exactly what they asked for — and the render route then
# re-verifies against a fresh build.
SCAN_INDEX_TTL_SECONDS = 60
_SCAN_INDEX_CACHE_MAX = 16
_scan_index_cache: dict = {}
_scan_index_lock = threading.Lock()


def _index_row(facts: dict) -> dict:
    import report_comparison as rc
    accounting = facts["accounting"]
    approvals = facts.get("approvals") or {}
    return {
        "file": facts["identity"]["file"],
        # Contract 3a: THE per-file route's digest. The row is built from exactly the facts that
        # route returns (same projection, same comparison), so the two are equal for unchanged
        # evidence and a mismatch is real drift. Counts alone cannot bind a report: notes,
        # descriptions or before/after values can change without moving any total.
        "factsDigest": facts["factsDigest"],
        "assessment": {"state": facts["assessment"]["state"],
                       "stateReason": facts["assessment"]["stateReason"]},
        "score": facts["assessment"]["score"],
        "findingsTotal": facts["assessment"]["findingsTotal"],
        "findingsOpen": accounting["findingsOpen"],
        "findingsResolvedVerified": accounting["findingsResolvedVerified"],
        "resolutionLedger": accounting["resolutionLedger"],
        "savedChangesVerified": accounting["savedChangesVerified"],
        # The applied-but-unverified records could not be read: their number is UNKNOWN, and an
        # index row saying 0 would take this document off every "needs a person" list.
        "savedChangesUnverified": (None if facts["savedChangesUnverifiedSource"] == "unavailable"
                                   else accounting["savedChangesUnverified"]),
        "savedChangesComplete": facts["savedChangesComplete"],
        "humanReviews": accounting["humanReviews"],
        "currentArtifact": facts["identity"]["currentArtifact"],
        "comparison": rc.compact(facts.get("comparison")),
        "approvalsRecheckRequired": (None if approvals.get("recheckRequired") is None
                                     else len(approvals["recheckRequired"])),
    }


# Contract 8: the finding records an `include=findings` scan page carries per row — exactly the
# per-file facts' own findings (same server ids, same structured location, every occurrence).
FINDING_RECORD_KEYS = ("id", "ruleId", "sc", "severity", "detail", "location", "comparable",
                       "state", "stateReason", "recommendedAction")
# Rows per page when findings are included: a page then carries every finding of every row on it,
# so it is bounded by rows rather than by the index's FILE_PAGE_MAX. The response states `limit`,
# and the client pages by what it was given.
FINDINGS_PAGE_MAX = 200


def finding_records(facts: dict) -> list[dict]:
    return [{k: copy.deepcopy(f.get(k)) for k in FINDING_RECORD_KEYS}
            for f in (facts.get("findings") or [])]


def build_scan_index(store, scan_id: str, *, owner: str,
                     include_findings: bool = False) -> dict | None:
    """The FULL scan index and its digest, built fresh. None when the caller cannot see the scan.

    Returns {"facts": <scan facts without files/paging>, "index": [rows], "digest", "builtAt",
    "baselineRead": 'batched'|'per_file', "inputsRead": {diffs, reviews, unverified},
    "findingsByFile": {file: {"findings": [...], "remediatedAt"}} | None}.

    `findingsByFile` (contract 8) is kept OUTSIDE the rows and outside the digest: the digest is
    the same whether or not a caller asked for findings (contract 7), and a build nobody asked
    findings of does not hold them.
    """
    import report_comparison as rc
    context = scan_context(store, scan_id, owner=owner)
    if context is None:
        return None
    run = context["run"]
    names = list(context["files_by_name"])
    baseline_read = prefetch_baselines(store, scan_id, names, owner=owner, context=context)
    inputs_read = prefetch_scan_inputs(store, scan_id, names, owner=owner, context=context)
    findings_by_file: dict | None = {} if include_findings else None

    index: list[dict] = []
    # C6 per-document same-scan summaries: aggregated below, not added to the index row (the
    # row key set is pinned by rf-C's packet recording; the full block is in the per-file facts).
    same_scan_rows: list[dict | None] = []
    totals = {"documents": len(names), "assessed": 0, "notAssessed": 0, "error": 0, "partial": 0,
              "findingsTotal": 0, "savedChangesVerified": 0, "savedChangesUnverified": 0,
              "approvalsRecheckRequired": 0}
    approvals_known = True
    unverified_known = True
    reviews_total = {"pending": 0, "accepted": 0, "correctionRequested": 0, "rejected": 0,
                     "unable": 0, "stale": 0}
    open_known = True
    open_total = 0
    resolved_known = True
    resolved_total = 0
    for name in names:
        facts = build_file_facts(store, scan_id, name, owner=owner, context=context)
        if facts is None:
            continue
        accounting = facts["accounting"]
        state = facts["assessment"]["state"]
        totals[{"assessed": "assessed", "not_assessed": "notAssessed", "error": "error",
                "partial": "partial"}[state]] += 1
        totals["findingsTotal"] += facts["assessment"]["findingsTotal"] or 0
        totals["savedChangesVerified"] += accounting["savedChangesVerified"]
        for key in reviews_total:
            reviews_total[key] += accounting["humanReviews"][key]
        if accounting["findingsOpen"] is None:
            open_known = False
        else:
            open_total += accounting["findingsOpen"]
        if accounting["findingsResolvedVerified"] is None:
            resolved_known = False
        else:
            resolved_total += accounting["findingsResolvedVerified"]
        row = _index_row(facts)
        if row["savedChangesUnverified"] is None:
            unverified_known = False
        else:
            totals["savedChangesUnverified"] += row["savedChangesUnverified"]
        if row["approvalsRecheckRequired"] is None:
            approvals_known = False
        else:
            totals["approvalsRecheckRequired"] += row["approvalsRecheckRequired"]
        index.append(row)
        same_scan_rows.append(rc.compact(facts.get("sameScanHistory")))
        if findings_by_file is not None:
            findings_by_file[name] = {"findings": finding_records(facts),
                                      "remediatedAt": facts["identity"]["remediatedAt"]}
    if not approvals_known:
        totals["approvalsRecheckRequired"] = None
    if not unverified_known:
        totals["savedChangesUnverified"] = None

    comparison = rc.aggregate(
        [r["comparison"] for r in index],
        same_scan_history=history_reader(store, "prior_assessment_in_scan") is not None,
        same_scan_rows=same_scan_rows)
    facts = {
        "factsVersion": FACTS_VERSION,
        "factsDigest": None,
        "generatedAt": None,
        "kind": "scan",
        "identity": {
            "scanId": scan_id, "file": None,
            "source": run.get("source"),
            "platformVersion": _platform_version(),
            "targetLevel": _target_level(),
            "targetLevelSource": "workspace_config",
            "scopeDigest": scope_digest(run),
            "scanScope": run.get("scan_scope"),
            "rubricHash": run.get("rubric_hash"),
        },
        "scan": {"status": run.get("status"),
                 "startedAt": run.get("started_at"),
                 "assessedAt": run.get("assessed_at") or run.get("completed_at")},
        "totals": totals,
        "accounting": {
            "findingsTotal": totals["findingsTotal"],
            "findingsOpen": open_total if open_known else None,
            "findingsResolvedVerified": resolved_total if resolved_known else None,
            "resolutionLedger": "per_finding" if resolved_known and index else "none",
            "accountingReason": (
                "every document has a per-finding resolution ledger" if resolved_known and index
                else "at least one document has no per-finding ledger, so the scan-level "
                     "resolved count cannot be stated"),
            "savedChangesVerified": totals["savedChangesVerified"],
            "savedChangesUnverified": totals["savedChangesUnverified"],
            "humanReviews": reviews_total,
        },
        # C4: a REAL estate comparison, aggregated from every document's own finding-by-finding
        # comparison against its own baseline — not inferred from scores, and not a null.
        "comparison": comparison,
        # Kept for older clients: there is no single estate-wide baseline snapshot. The
        # comparison above is the answer; this says where it came from.
        "previous": None,
        "previousReason": ("the estate comparison is aggregated from each document's own "
                           "comparison with its most recent earlier assessment"),
        "limits": {"valueMaxChars": VALUE_MAX_CHARS, "savedChangesLimit": SAVED_CHANGES_LIMIT,
                   "filePageMax": FILE_PAGE_MAX},
        # The FULL index takes part in the digest; a response carries only one page of it.
        "_files_all": index,
    }
    digest = facts_digest(facts)
    facts.pop("_files_all")
    facts["factsDigest"] = digest
    return {"facts": facts, "index": index, "digest": digest, "builtAt": _now(),
            "baselineRead": baseline_read, "inputsRead": inputs_read,
            "findingsByFile": findings_by_file}


def _cache_key(store, owner: str, scan_id: str) -> tuple:
    # id(store) keeps two stores (tests, or a store swapped at runtime) from sharing a snapshot.
    return (id(store), owner, scan_id)


def _cached_index(store, scan_id: str, owner: str, digest: str,
                  include_findings: bool = False) -> dict | None:
    now = time.monotonic()
    with _scan_index_lock:
        entry = _scan_index_cache.get(_cache_key(store, owner, scan_id))
        if entry and entry["digest"] == digest and entry["expires"] > now:
            # A memo built without findings cannot serve a page that asks for them: that page
            # is rebuilt (and 409s if the evidence moved), never served without its records.
            if include_findings and entry["built"].get("findingsByFile") is None:
                return None
            return entry["built"]
    return None


def _remember_index(store, scan_id: str, owner: str, built: dict) -> None:
    now = time.monotonic()
    with _scan_index_lock:
        for key in [k for k, v in _scan_index_cache.items() if v["expires"] <= now]:
            _scan_index_cache.pop(key, None)
        while len(_scan_index_cache) >= _SCAN_INDEX_CACHE_MAX:
            _scan_index_cache.pop(min(_scan_index_cache,
                                      key=lambda k: _scan_index_cache[k]["expires"]), None)
        _scan_index_cache[_cache_key(store, owner, scan_id)] = {
            "digest": built["digest"], "built": built,
            "expires": now + SCAN_INDEX_TTL_SECONDS}


def clear_scan_index_cache() -> None:
    with _scan_index_lock:
        _scan_index_cache.clear()


def build_scan_facts(store, scan_id: str, *, owner: str, offset: int = 0,
                     limit: int = FILE_PAGE_DEFAULT, digest: str | None = None,
                     include_findings: bool = False) -> dict | None:
    """Scan-level totals plus a BOUNDED per-file index — one page of one snapshot.

    The digest is computed over the FULL, unpaginated index, so two clients that fetched
    different pages agree on it and neither gets a spurious "the document changed" on render.

    `digest` (contract 3): the snapshot the caller is paging. When it is the memo's, the page is
    served from the memo; otherwise the index is rebuilt and, if the fresh digest differs,
    ScanFactsChanged is raised (the route answers 409) — pages of two snapshots are never mixed.
    Without `digest` the index is always rebuilt.

    `include_findings` (contract 8): each row on THIS page also carries `findings` — the per-file
    facts' own finding records — and `remediatedAt`. Nothing else changes: the digest, the row
    digests and every other key are the same with or without it (contract 7). Pages are then at
    most FINDINGS_PAGE_MAX rows.
    """
    built = (_cached_index(store, scan_id, owner, digest, include_findings)
             if digest else None)
    served_from = "memo" if built else "fresh"
    if built is None:
        built = build_scan_index(store, scan_id, owner=owner, include_findings=include_findings)
        if built is None:
            return None
        _remember_index(store, scan_id, owner, built)
        if digest and built["digest"] != digest:
            raise ScanFactsChanged(digest, built["digest"])
    index = built["index"]
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or FILE_PAGE_DEFAULT),
                       FINDINGS_PAGE_MAX if include_findings else FILE_PAGE_MAX))
    page = index[offset:offset + limit]
    facts = copy.deepcopy(built["facts"])
    facts["generatedAt"] = built["builtAt"]
    facts["files"] = copy.deepcopy(page)
    if include_findings:
        extra = built["findingsByFile"] or {}
        for row in facts["files"]:
            got = extra.get(row["file"])
            # Built in the same loop as the row, so always present; if it ever were not, the
            # records are "not supplied" (null), never an empty list that reads "no findings".
            row["findings"] = copy.deepcopy(got["findings"]) if got else None
            row["remediatedAt"] = got.get("remediatedAt") if got else None
    facts["filesTotal"] = len(index)
    facts["offset"] = offset
    facts["limit"] = limit
    facts["complete"] = len(page) == len(index)
    facts["snapshot"] = {"factsDigest": built["digest"], "filesTotal": len(index),
                         "builtAt": built["builtAt"], "servedFrom": served_from,
                         "ttlSeconds": SCAN_INDEX_TTL_SECONDS}
    return facts


# ── the seam the render route binds a report to ───────────────────────────────

def current_digest(store, scan_id: str, file: str | None, *, owner: str) -> str | None:
    """The facts digest a report of this kind must carry, or None when there is nothing to see.

    ALWAYS a fresh build — never the paging memo. This is what the render route verifies against.
    """
    if file:
        facts = build_file_facts(store, scan_id, file, owner=owner)
        return None if facts is None else facts["factsDigest"]
    built = build_scan_index(store, scan_id, owner=owner)
    return None if built is None else built["digest"]


def verify_digest(store, scan_id: str, file: str | None, digest: str, *, owner: str) -> bool:
    """True when `digest` still describes the evidence this scan/file holds RIGHT NOW."""
    if not digest or not isinstance(digest, str):
        return False
    current = current_digest(store, scan_id, file, owner=owner)
    return bool(current) and current == digest
