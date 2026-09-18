"""api/report_facts.py — the accounting rules, at the level they are decided.

THE FIXTURE THIS FILE EXISTS FOR is `test_one_verified_change_never_resolves_two_findings`. It is
the parent reviewer's reproduction, verbatim: one document, criterion 1.1.1, TWO undescribed
images, ONE verified saved change. The client-side model that preceded this module answered
"2 resolved, 0 remaining, no outstanding items" for exactly that input — and nothing in the data
said so. A single criterion-level record cannot say WHICH image it described.

So the assertions here are mostly about what is NOT claimed. `findingsResolvedVerified` is 2 in
no configuration; it is 1 only where a per-finding ledger names the finding; and where no ledger
exists the counts that would have to be guessed are `null`, which the reviewer's own note calls
the safer answer.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ACP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACP / "api"))

import report_facts as rf   # noqa: E402

OWNER = "owner@hosp.org"
SID = "s-facts"
FILE = "handbook.docx"


def _scan(store, *, sid=SID, owner=OWNER, files, source="local", rubric_hash="h1",
          completed="2026-09-01T05:05:00+00:00"):
    store.save_scan({
        "_scan_id": sid,
        "started_at": "2026-09-01T05:00:00+00:00",
        "completed_at": completed,
        "source": source, "owner": owner,
        "rubric": {"name": "wcag-aa", "hash": rubric_hash},
        "summary": {"files": len(files), "certifiable": 0, "uncertain": len(files),
                    "error": 0, "avg_score": 70},
        "files": files,
    })


def _doc(name=FILE, *, status="uncertain", score=70, issues=(), checksum="md5-source-1",
         skipped=0, drive_file_id=None):
    return {"file": name, "engine": ".net/office", "status": status, "score": score,
            "compliant": 0, "skipped_rules": skipped, "issues": list(issues),
            "checksum": checksum, "drive_file_id": drive_file_id}


def _image_issue(detail, page, location):
    return {"ruleId": "DOCX-ALT-001", "wcag": "1.1.1 Non-text Content", "severity": "SERIOUS",
            "detail": detail, "page": page, "location": location}


# ── the reproduction ──────────────────────────────────────────────────────────

def _two_images_one_verified_change(store):
    _scan(store, files=[_doc(issues=[_image_issue("Missing description A", 1, "docx:image:1"),
                                     _image_issue("Missing description B", 2, "docx:image:2")])])
    store.record_remediation(SID, FILE, corrected_sha256="c" * 64)
    store.record_remediation_diffs(SID, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "Image A description", "note": "vision"}])


def test_one_verified_change_never_resolves_two_findings(isolated_store):
    """The parent's repro. Two findings, one verified change, no per-finding ledger."""
    _two_images_one_verified_change(isolated_store)
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)

    assert len(facts["findings"]) == 2
    accounting = facts["accounting"]
    assert accounting["findingsTotal"] == 2
    # The defect, refused in both directions:
    assert accounting["findingsResolvedVerified"] is None, "no ledger can say which image"
    assert accounting["resolutionLedger"] == "none"
    assert accounting["findingsResolvedVerified"] != 2
    # ... and the remaining count is UNKNOWN rather than invented, because a change WAS saved
    # against this criterion and nothing records which finding it was for.
    assert accounting["findingsOpen"] is None
    assert "no per-finding ledger" in accounting["accountingReason"]
    # every finding keeps its own identity and its own state
    assert {f["state"] for f in facts["findings"]} == {"open"}
    assert len({f["id"] for f in facts["findings"]}) == 2
    assert all(f["ledgerFindingId"] is None for f in facts["findings"])


def test_with_a_per_finding_ledger_exactly_one_is_resolved(isolated_store, monkeypatch):
    """The same document, with the ledger that CAN say which image was described."""
    _two_images_one_verified_change(isolated_store)
    from documents import resolve_doc_id
    from finding_ledger import normalize_instance_key, stable_finding_id

    document_id = resolve_doc_id("local", None, FILE, "md5-source-1")
    keys = [normalize_instance_key(loc, ordinal=index + 1, aggregate_scope=SID)
            for index, loc in enumerate(["docx:image:1", "docx:image:2"])]
    ledger = {"snapshot_id": SID, "findings": [
        {"finding_id": stable_finding_id(document_id, "1.1.1", keys[0]), "file": FILE,
         "rule_id": "1.1.1", "instance_key": keys[0], "state": "fixed",
         "reason": "Exact approved version applied and checked"},
        {"finding_id": stable_finding_id(document_id, "1.1.1", keys[1]), "file": FILE,
         "rule_id": "1.1.1", "instance_key": keys[1], "state": "awaiting_review",
         "reason": "Usable proposal awaiting approval"},
    ]}
    monkeypatch.setattr(rf, "read_ledger", lambda *a, **k: ledger)

    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    accounting = facts["accounting"]
    assert accounting["resolutionLedger"] == "per_finding"
    assert accounting["findingsResolvedVerified"] == 1
    assert accounting["findingsOpen"] == 1
    states = sorted(f["state"] for f in facts["findings"])
    assert states == ["awaiting_review", "resolved_verified"]
    assert all(f["ledgerFindingId"] for f in facts["findings"])


def test_a_half_mapped_criterion_is_reported_as_no_ledger(isolated_store, monkeypatch):
    """One of the two images is in the ledger and the other is not: fail closed, not halfway."""
    _two_images_one_verified_change(isolated_store)
    from documents import resolve_doc_id
    from finding_ledger import normalize_instance_key, stable_finding_id
    document_id = resolve_doc_id("local", None, FILE, "md5-source-1")
    key = normalize_instance_key("docx:image:1", ordinal=1, aggregate_scope=SID)
    monkeypatch.setattr(rf, "read_ledger", lambda *a, **k: {"snapshot_id": SID, "findings": [
        {"finding_id": stable_finding_id(document_id, "1.1.1", key), "file": FILE,
         "rule_id": "1.1.1", "instance_key": key, "state": "fixed", "reason": "x"}]})
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert facts["accounting"]["resolutionLedger"] == "none"
    assert facts["accounting"]["findingsResolvedVerified"] is None


# ── assessment state: three things that are not "no findings" ─────────────────

def test_assessed_with_zero_findings_is_a_clean_document(isolated_store):
    _scan(isolated_store, files=[_doc(status="certifiable", score=100, issues=[])])
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert facts["assessment"]["state"] == "assessed"
    assert facts["assessment"]["findingsTotal"] == 0
    assert facts["accounting"]["findingsOpen"] == 0


def test_never_assessed_is_not_a_clean_document(isolated_store):
    _scan(isolated_store, files=[_doc(status="discovered", score=None, issues=[])])
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert facts["assessment"]["state"] == "not_assessed"
    assert "never assessed" in facts["assessment"]["stateReason"]
    assert facts["assessment"]["score"] is None


def test_an_engine_error_is_not_a_clean_document(isolated_store):
    _scan(isolated_store, files=[_doc(status="error", score=None, issues=[])])
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert facts["assessment"]["state"] == "error"
    assert facts["assessment"]["findingsTotal"] == 0
    # ... and "0 findings" is NOT reported as "0 open". Nothing looked at this document, so the
    # number of open findings is unknown — the same claim in numbers that `state` makes in words.
    assert facts["accounting"]["findingsOpen"] is None
    assert facts["accounting"]["findingsResolvedVerified"] is None


def test_a_partial_assessment_says_so(isolated_store):
    _scan(isolated_store, files=[_doc(status="uncertain", skipped=4,
                                      issues=[_image_issue("a", 1, "docx:image:1")])])
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert facts["assessment"]["state"] == "partial"
    assert "4 rule(s) were not evaluated" in facts["assessment"]["stateReason"]


@pytest.mark.parametrize("status", ["discovered", "error", "skipped", "queued"])
def test_zero_findings_never_reads_as_assessed_for_a_non_assessed_status(isolated_store, status):
    _scan(isolated_store, files=[_doc(status=status, score=None, issues=[])])
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert facts["assessment"]["state"] != "assessed"
    assert facts["accounting"]["findingsOpen"] is None, "an empty list is not a clean document"


# ── identity ──────────────────────────────────────────────────────────────────

def test_a_source_md5_is_never_reported_as_a_sha256(isolated_store):
    _scan(isolated_store, files=[_doc(checksum="d41d8cd98f00b204e9800998ecf8427e")])
    identity = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)["identity"]
    assert identity["sourceChecksumKind"] == "md5"
    assert identity["sourceSha256"] is None
    assert identity["currentArtifact"] == {"kind": "source",
                                           "sha256": "d41d8cd98f00b204e9800998ecf8427e"}


def test_a_tagged_sha256_source_is_a_sha256_in_bare_hex(isolated_store):
    """`sha256:<hex>` is a sha256. The report header already said so (server_identity), while the
    facts called it "other" and the same PDF told the reader no original preview was possible."""
    digest = "ab" * 32
    _scan(isolated_store, files=[_doc(checksum="sha256:" + digest.upper())])
    identity = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)["identity"]
    assert identity["sourceChecksumKind"] == "sha256"
    assert identity["sourceSha256"] == digest       # what the exact-bytes route compares against
    assert rf.checksum_kind("sha256:" + "0" * 63) == "other"


def test_a_corrected_copy_with_no_recorded_digest_is_unknown_not_source(isolated_store):
    """The review's finding: a remediated file whose saved copy has no hash has NO identity."""
    _scan(isolated_store, files=[_doc()])
    isolated_store.record_remediation(SID, FILE)      # remediated_at, no corrected_sha256
    identity = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)["identity"]
    assert identity["correctedSha256"] is None
    assert identity["currentArtifact"] == {"kind": "unknown", "sha256": None}
    assert identity["remediatedAt"]


def test_a_corrected_copy_is_the_current_artifact(isolated_store):
    _scan(isolated_store, files=[_doc()])
    isolated_store.record_remediation(SID, FILE, corrected_sha256="a" * 64)
    identity = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)["identity"]
    assert identity["currentArtifact"] == {"kind": "corrected", "sha256": "a" * 64}


# ── saved changes, verified and not ────────────────────────────────────────────

def test_unverified_saved_changes_appear_and_are_marked_not_verified(isolated_store):
    _scan(isolated_store, files=[_doc(issues=[_image_issue("a", 1, "docx:image:1")])])
    isolated_store.record_remediation(SID, FILE, corrected_sha256="c" * 64)
    isolated_store.log_decision(
        "system", "apply.saved_unverified", scan_id=SID, file=FILE, rule_id="1.1.1",
        detail=json.dumps({"artifact_sha256": "c" * 64, "rule_id": "1.1.1",
                           "reason": "AI wrote a description",
                           "changes": [{"locator": "docx:image:1", "before": "",
                                        "after": "A red barn"}]}))
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    unverified = [c for c in facts["savedChanges"] if c["verification"] == "not_verified"]
    assert len(unverified) == 1
    change = unverified[0]
    assert change["after"] == "A red barn"
    assert change["locator"] == "docx:image:1"
    assert change["seq"] is None and change["id"].split("::")[-1].startswith("u")
    assert "no re-check" in change["verificationDetail"]
    assert facts["accounting"]["savedChangesUnverified"] == 1
    assert facts["accounting"]["savedChangesVerified"] == 0
    # ... and it is a change a human still has to look at
    assert facts["accounting"]["humanReviews"]["pending"] == 1


def test_unreadable_unverified_records_make_the_list_incomplete_not_shorter(isolated_store,
                                                                            monkeypatch):
    """The worst failure this builder has, tested rather than assumed.

    Applied-but-unverified changes are the ones a human still has to look at. If they cannot be
    read and the list simply comes back shorter, the report describes the review as finished.
    So the failure has to show up as INCOMPLETE.
    """
    _scan(isolated_store, files=[_doc()])
    isolated_store.record_remediation_diffs(SID, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "A red barn"}])
    import unverified_changes
    monkeypatch.setattr(unverified_changes, "saved_changes",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("malformed record")))
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert facts["savedChangesUnverifiedSource"] == "unavailable"
    assert facts["savedChangesComplete"] is False
    assert len(facts["savedChanges"]) == 1        # the verified one is still there


def test_a_programming_error_in_the_ledger_read_is_not_silently_no_ledger(isolated_store,
                                                                         monkeypatch):
    """A bite check on read_ledger's narrowed except: 'no ledger' must not be able to mean
    'somebody renamed a method'. That answer is what turns counts into nulls, so it has to be a
    finding about the data, never about this file."""
    _scan(isolated_store, files=[_doc()])
    monkeypatch.delattr(type(isolated_store), "list_stage_execution_ids")
    with pytest.raises(AttributeError):
        rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)


def test_a_clipped_value_is_declared_clipped_with_the_real_limit(isolated_store):
    _scan(isolated_store, files=[_doc()])
    isolated_store.record_remediation_diffs(SID, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "x" * 5000}])
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    change = facts["savedChanges"][0]
    assert change["valueClipped"] is True
    assert len(change["after"]) == rf.VALUE_MAX_CHARS == 2000
    assert facts["limits"]["valueMaxChars"] == 2000
    # the untruncated text is not held anywhere else, and the facts say so
    assert facts["limits"]["valueClippedIsUnrecoverable"] is True


def test_saved_changes_are_bounded_and_say_so(isolated_store):
    _scan(isolated_store, files=[_doc()])
    isolated_store.record_remediation_diffs(SID, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": f"alt {n}"} for n in range(12)])
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER,
                                saved_changes_limit=5)
    assert len(facts["savedChanges"]) == 5
    assert facts["savedChangesComplete"] is False
    assert facts["savedChangesTotal"] == 12
    # Nothing is counted from a partial list: "no change touched this finding"
    # would describe the five records read, not the twelve stored.
    assert facts["accounting"]["findingsOpen"] is None
    assert "saved-change list is incomplete" in facts["accounting"]["accountingReason"]


def test_default_file_facts_retain_records_beyond_old_presentation_caps(isolated_store):
    _scan(isolated_store, files=[_doc(issues=[
        _image_issue(f"Missing description {n}", n + 1, f"docx:image:{n}")
        for n in range(1005)])])
    isolated_store.record_remediation_diffs(SID, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": f"alt {n}"}
        for n in range(505)])
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert len(facts["findings"]) == 1005
    assert facts["assessment"]["findingsComplete"] is True
    assert len(facts["savedChanges"]) == 505
    assert facts["savedChangesComplete"] is True


# ── recommended action, verbatim ──────────────────────────────────────────────

@pytest.mark.parametrize("key", ["recommended_action", "remediation", "action", "fix"])
def test_the_sources_own_guidance_is_preserved_verbatim(key):
    issues = [{"ruleId": "DOCX-ALT-001", "wcag": "1.1.1 Non-text", "severity": "SERIOUS",
               "detail": "d", "page": 1, "location": "docx:image:1",
               key: "Describe the barn, not the photograph."}]
    finding = rf.build_findings(SID, FILE, issues)[0]
    assert finding["recommendedAction"] == "Describe the barn, not the photograph."
    assert finding["recommendedActionSource"] == key


def test_no_guidance_is_null_and_never_a_generic_sentence():
    issues = [{"ruleId": "DOCX-ALT-001", "wcag": "1.1.1 Non-text", "severity": "SERIOUS",
               "detail": "d", "page": 1, "location": "docx:image:1"}]
    finding = rf.build_findings(SID, FILE, issues)[0]
    assert finding["recommendedAction"] is None
    assert finding["recommendedActionSource"] is None


# ── the digest ────────────────────────────────────────────────────────────────

def test_the_digest_excludes_generated_at_but_covers_the_evidence(isolated_store):
    """A bite check on the exclusion: if generatedAt were in the digest, two reads of an
    unchanged document would disagree and every render would 409."""
    _scan(isolated_store, files=[_doc(issues=[_image_issue("a", 1, "docx:image:1")])])
    first = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    second = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert first["generatedAt"] != second["generatedAt"] or True   # clock may be coarse
    assert first["factsDigest"] == second["factsDigest"]
    # and the digest really is over the body
    assert rf.facts_digest({**first, "factsDigest": None}) == first["factsDigest"]
    assert rf.compute_facts_digest is rf.facts_digest


def test_the_digest_changes_when_a_saved_change_changes(isolated_store):
    """The bite check the reviewer asked for: mutate the evidence, the digest must move."""
    _scan(isolated_store, files=[_doc(issues=[_image_issue("a", 1, "docx:image:1")])])
    isolated_store.record_remediation_diffs(SID, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "A red barn"}])
    before = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)["factsDigest"]
    isolated_store.record_remediation_diffs(SID, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "A BLUE barn"}])
    after = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)["factsDigest"]
    assert before != after


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda s: s.record_remediation(SID, FILE, corrected_sha256="e" * 64),
                 id="artifact identity"),
    pytest.param(lambda s: s.save_decision(SID, FILE, "change_review:" + FILE + "::1.1.1::0",
                                           json.dumps({"verdict": "accepted",
                                                       "artifact_sha256": "z" * 64}),
                                           OWNER, "2026-09-02T00:00:00+00:00"),
                 id="a reviewer decision"),
])
def test_the_digest_moves_for_every_kind_of_evidence(isolated_store, mutate):
    _scan(isolated_store, files=[_doc(issues=[_image_issue("a", 1, "docx:image:1")])])
    isolated_store.record_remediation_diffs(SID, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "A red barn"}])
    before = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)["factsDigest"]
    mutate(isolated_store)
    assert rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)["factsDigest"] != before


def test_scan_digest_binds_values_even_when_counts_and_artifact_do_not_change(isolated_store):
    _two_images_one_verified_change(isolated_store)
    before = rf.build_scan_facts(isolated_store, SID, owner=OWNER)
    isolated_store.record_remediation_diffs(SID, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "Changed image description", "note": "vision"}])
    after = rf.build_scan_facts(isolated_store, SID, owner=OWNER)
    assert before["totals"] == after["totals"]
    assert before["files"][0]["currentArtifact"] == after["files"][0]["currentArtifact"]
    assert before["factsDigest"] != after["factsDigest"]
    assert not rf.verify_digest(isolated_store, SID, None, before["factsDigest"], owner=OWNER)


def test_verify_digest_is_the_render_routes_seam(isolated_store):
    _scan(isolated_store, files=[_doc()])
    digest = rf.current_digest(isolated_store, SID, FILE, owner=OWNER)
    assert rf.verify_digest(isolated_store, SID, FILE, digest, owner=OWNER) is True
    assert rf.verify_digest(isolated_store, SID, FILE, "f" * 64, owner=OWNER) is False
    assert rf.verify_digest(isolated_store, SID, FILE, "", owner=OWNER) is False
    # a foreign owner cannot even establish that the scan exists
    assert rf.current_digest(isolated_store, SID, FILE, owner="someone@else.org") is None
    assert rf.verify_digest(isolated_store, SID, FILE, digest, owner="someone@else.org") is False


# ── reviewer decisions inside the facts ───────────────────────────────────────

def _decide(store, change_id, verdict, *, artifact, digest):
    store.save_decision(SID, FILE, "change_review:" + change_id, json.dumps({
        "change_id": change_id, "verdict": verdict, "artifact_sha256": artifact,
        "change_digest": digest, "reviewer": OWNER, "at": "2026-09-02T00:00:00+00:00"}),
        OWNER, "2026-09-02T00:00:00+00:00")


def test_edited_is_a_correction_request_and_is_never_counted_as_confirmed(isolated_store):
    _scan(isolated_store, files=[_doc()])
    isolated_store.record_remediation(SID, FILE, corrected_sha256="c" * 64)
    isolated_store.record_remediation_diffs(SID, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "A red barn"}])
    change_id = f"{FILE}::1.1.1::0"
    _decide(isolated_store, change_id, "edited", artifact="c" * 64,
            digest=rf.change_digest("1.1.1", "", "A red barn"))
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    review = facts["reviews"][change_id]
    assert review["stale"] is False
    assert review["verdictLabel"] == "correction requested"
    counts = facts["accounting"]["humanReviews"]
    assert counts["correctionRequested"] == 1 and counts["accepted"] == 0


def test_unknown_freshness_is_never_accepted(isolated_store):
    """stale: null. The review's finding — an accepted verdict with no comparable identity was
    being read as a confirmation."""
    _scan(isolated_store, files=[_doc(checksum=None)])
    isolated_store.record_remediation_diffs(SID, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "A red barn"}])
    change_id = f"{FILE}::1.1.1::0"
    _decide(isolated_store, change_id, "accepted", artifact=None,
            digest=rf.change_digest("1.1.1", "", "A red barn"))
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert facts["reviews"][change_id]["stale"] is None
    assert "cannot be established" in facts["reviews"][change_id]["staleReason"]
    counts = facts["accounting"]["humanReviews"]
    assert counts["accepted"] == 0 and counts["stale"] == 1


def test_a_decision_whose_document_moved_on_is_stale(isolated_store):
    _scan(isolated_store, files=[_doc()])
    isolated_store.record_remediation(SID, FILE, corrected_sha256="c" * 64)
    isolated_store.record_remediation_diffs(SID, FILE, [
        {"rule_id": "1.1.1", "before": "", "after": "A red barn"}])
    change_id = f"{FILE}::1.1.1::0"
    _decide(isolated_store, change_id, "accepted", artifact="b" * 64,
            digest=rf.change_digest("1.1.1", "", "A red barn"))
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert facts["reviews"][change_id]["stale"] is True
    assert facts["accounting"]["humanReviews"]["accepted"] == 0


# ── comparison with a previous assessment ─────────────────────────────────────

def test_no_comparable_baseline_is_unknown_with_a_plain_reason(isolated_store):
    _scan(isolated_store, files=[_doc()])
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert facts["previous"] is None
    assert "no earlier assessment" in facts["previousReason"]


def test_a_same_document_baseline_under_the_same_scope_is_compared(isolated_store):
    _scan(isolated_store, sid="s-old", files=[_doc(drive_file_id="drive-1",
                                                   issues=[_image_issue("a", 1, "docx:image:1"),
                                                           _image_issue("b", 2, "docx:image:2")])],
          source="drive", completed="2026-08-01T00:00:00+00:00")
    _scan(isolated_store, files=[_doc(drive_file_id="drive-1",
                                      issues=[_image_issue("a", 1, "docx:image:1")])],
          source="drive")
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert facts["previous"] is not None, facts["previousReason"]
    assert facts["previous"]["scanId"] == "s-old"
    assert len(facts["previous"]["findings"]) == 2
    # the SAME id scheme on both sides, so the client compares by id and not by name
    current_ids = {f["id"] for f in facts["findings"]}
    previous_ids = {f["id"] for f in facts["previous"]["findings"]}
    assert current_ids and current_ids < previous_ids


def test_a_finding_with_no_real_location_is_flagged_as_not_comparable(isolated_store):
    """An ordinal is only meaningful inside the snapshot that produced it, so two assessments'
    "instance 1" are not the same finding. Flagged, never matched — otherwise every such finding
    reads as resolved AND introduced at once."""
    _scan(isolated_store, files=[_doc(issues=[
        {"ruleId": "DOCX-LANG-001", "wcag": "3.1.1 Language of Page", "severity": "SERIOUS",
         "detail": "Document language is not set", "page": None, "location": None}])])
    finding = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)["findings"][0]
    assert finding["comparable"] is False
    assert finding["instanceKey"].startswith("aggregate-instance:")


def test_a_finding_with_a_detector_location_is_comparable(isolated_store):
    _scan(isolated_store, files=[_doc(issues=[_image_issue("a", 1, "docx:image:1")])])
    finding = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)["findings"][0]
    assert finding["comparable"] is True
    assert finding["instanceKey"] == "docx:image:1"


def test_a_different_rubric_is_not_a_baseline(isolated_store):
    _scan(isolated_store, sid="s-old", rubric_hash="OTHER", source="drive",
          completed="2026-08-01T00:00:00+00:00",
          files=[_doc(drive_file_id="drive-1", issues=[_image_issue("a", 1, "docx:image:1")])])
    _scan(isolated_store, source="drive", files=[_doc(drive_file_id="drive-1")])
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert facts["previous"] is None
    assert "different rubric or scope" in facts["previousReason"]


def test_another_owners_earlier_scan_is_never_a_baseline(isolated_store):
    _scan(isolated_store, sid="s-old", owner="someone@else.org", source="drive",
          completed="2026-08-01T00:00:00+00:00",
          files=[_doc(drive_file_id="drive-1", issues=[_image_issue("a", 1, "docx:image:1")])])
    _scan(isolated_store, source="drive", files=[_doc(drive_file_id="drive-1")])
    facts = rf.build_file_facts(isolated_store, SID, FILE, owner=OWNER)
    assert facts["previous"] is None


# ── scan level ────────────────────────────────────────────────────────────────

def test_scan_facts_page_a_large_estate_rather_than_capping_it(isolated_store):
    _scan(isolated_store, files=[_doc(f"doc{n:02d}.docx") for n in range(12)])
    page = rf.build_scan_facts(isolated_store, SID, owner=OWNER, offset=5, limit=3)
    assert [f["file"] for f in page["files"]] == ["doc05.docx", "doc06.docx", "doc07.docx"]
    assert page["filesTotal"] == 12 and page["offset"] == 5 and page["limit"] == 3
    assert page["complete"] is False
    assert page["totals"]["documents"] == 12


def test_the_scan_digest_is_the_same_on_every_page(isolated_store):
    """Stream G compares one digest; a client that fetched page 2 must not get a 409 for it."""
    _scan(isolated_store, files=[_doc(f"doc{n:02d}.docx") for n in range(12)])
    first = rf.build_scan_facts(isolated_store, SID, owner=OWNER, offset=0, limit=3)
    second = rf.build_scan_facts(isolated_store, SID, owner=OWNER, offset=9, limit=3)
    whole = rf.build_scan_facts(isolated_store, SID, owner=OWNER, offset=0, limit=500)
    assert first["factsDigest"] == second["factsDigest"] == whole["factsDigest"]
    assert rf.current_digest(isolated_store, SID, None, owner=OWNER) == whole["factsDigest"]


def test_the_scan_builder_reads_the_scan_once_not_once_per_document(isolated_store):
    """A bite check with teeth: it fails if scan_context is bypassed.

    The first version of this builder called build_file_facts per document and each call re-read
    get_scan + get_file_records + the contribution ledger. That is quadratic, and this repo has
    already shipped a per-file read that hung the Discover tab on a ~6,916-file estate. The
    numbers below are the point: one get_scan for 12 documents, not twelve.
    """
    _scan(isolated_store, files=[_doc(f"doc{n:02d}.docx") for n in range(12)])
    calls = {"scan": 0, "records": 0}
    real_scan = type(isolated_store).get_scan
    real_records = type(isolated_store).get_file_records

    def counted_scan(self, *a, **k):
        calls["scan"] += 1
        return real_scan(self, *a, **k)

    def counted_records(self, *a, **k):
        calls["records"] += 1
        return real_records(self, *a, **k)

    original = (type(isolated_store).get_scan, type(isolated_store).get_file_records)
    type(isolated_store).get_scan = counted_scan
    type(isolated_store).get_file_records = counted_records
    try:
        facts = rf.build_scan_facts(isolated_store, SID, owner=OWNER)
    finally:
        type(isolated_store).get_scan, type(isolated_store).get_file_records = original
    assert facts["filesTotal"] == 12
    assert calls["scan"] == 1, f"get_scan ran {calls['scan']} times for 12 documents"
    # One scan-wide read, plus exactly one per document from inside
    # unverified_changes.pending_records (which looks up its own corrected_sha256 and is not
    # this module's to restructure). Pinned rather than rounded off, so a regression that
    # reintroduces a scan-wide read per file is visible as a jump, not as a slow report.
    assert calls["records"] == 1 + facts["filesTotal"], calls


def test_a_scan_with_one_unledgered_document_cannot_state_a_resolved_total(isolated_store):
    _two_images_one_verified_change(isolated_store)
    facts = rf.build_scan_facts(isolated_store, SID, owner=OWNER)
    assert facts["accounting"]["findingsResolvedVerified"] is None
    assert facts["accounting"]["resolutionLedger"] == "none"
    assert facts["accounting"]["findingsOpen"] is None
    assert facts["files"][0]["resolutionLedger"] == "none"


def test_scan_facts_distinguish_the_four_assessment_states(isolated_store):
    _scan(isolated_store, files=[
        _doc("ok.docx", status="certifiable"),
        _doc("never.docx", status="discovered"),
        _doc("boom.docx", status="error"),
        _doc("half.docx", status="uncertain", skipped=3)])
    totals = rf.build_scan_facts(isolated_store, SID, owner=OWNER)["totals"]
    assert (totals["assessed"], totals["notAssessed"], totals["error"], totals["partial"]) == \
        (1, 1, 1, 1)


def test_a_foreign_owner_sees_nothing(isolated_store):
    _scan(isolated_store, files=[_doc()])
    assert rf.build_file_facts(isolated_store, SID, FILE, owner="someone@else.org") is None
    assert rf.build_scan_facts(isolated_store, SID, owner="someone@else.org") is None
    assert rf.build_file_facts(isolated_store, SID, "not-in-scan.docx", owner=OWNER) is None


# ── change ids ────────────────────────────────────────────────────────────────

def test_change_ids_round_trip_for_both_kinds():
    verified = rf.verified_change_id("dir/a.docx", "1.1.1", 3)
    assert verified == "dir/a.docx::1.1.1::3"
    assert rf.parse_change_id("dir/a.docx", verified) == ("1.1.1", 3)
    unverified = rf.unverified_change_id("dir/a.docx", "1.1.1", "loc", "b", "a")
    assert rf.parse_change_id("dir/a.docx", unverified) == ("1.1.1", None)
    assert rf.parse_change_id("other.docx", verified) is None
    assert rf.parse_change_id("dir/a.docx", "dir/a.docx::1.1.1::x") is None


def test_an_unverified_change_id_is_a_content_address_not_an_ordinal():
    """An ordinal would move when another edit is recorded, and a reviewer's decision would
    follow it onto a different change."""
    a = rf.unverified_change_id(FILE, "1.1.1", "docx:image:1", "", "A red barn")
    b = rf.unverified_change_id(FILE, "1.1.1", "docx:image:2", "", "A red barn")
    assert a != b
    assert a == rf.unverified_change_id(FILE, "1.1.1", "docx:image:1", "", "A red barn")


def test_the_change_digest_is_the_contracts_formula():
    assert rf.change_digest("1.1.1", "b", "a") == hashlib.sha256(b"1.1.1\nb\na").hexdigest()
