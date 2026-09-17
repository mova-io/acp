"""Feature-disabled deterministic PPTX plans for ChunkPlanAdmission.

Nothing imports this module from live dispatch. It produces the one durable
plan/chunk vocabulary accepted by document_wide_chunk_admission; budget
authority comes only from that ledger-backed admission transaction.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import re

ENABLED = False
_SLIDE = re.compile(r"(?:^slide\s+|ppt/slides/slide)(\d+)(?:\.xml)?(?:#|$)", re.I)
_MAX_LEDGER_IDENTIFIER = 512
_MAX_LEDGER_UNITS = 2**63 - 1

def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def _ledger_identifier(value) -> bool:
    return (isinstance(value, str) and bool(value.strip())
            and len(value) <= _MAX_LEDGER_IDENTIFIER)

@dataclass(frozen=True)
class AdmissionPlan:
    plan: dict
    chunks: tuple[dict, ...]

    @property
    def finding_ids(self) -> tuple[str, ...]:
        return tuple(fid for chunk in self.chunks for fid in chunk["finding_ids"])

def build_pptx_plan(*, owner_id: str, run_id: str, scan_id: str, file: str,
                    source_sha256: str, assessment_snapshot_id: str,
                    remediation_source_revision: str, findings: list[dict],
                    created_at: str, max_findings: int = 20,
                    max_cost_units_per_chunk: int, pricing_ref: str) -> AdmissionPlan:
    """Build stable slide chunks; this function grants no spending authority."""
    identities = (scan_id, file, assessment_snapshot_id,
                  remediation_source_revision, created_at)
    if (not _ledger_identifier(owner_id) or not _ledger_identifier(run_id)
            or not _ledger_identifier(pricing_ref)
            or any(not isinstance(value, str) or not value.strip() for value in identities)
            or not re.fullmatch(r"[a-f0-9]{64}", source_sha256 or "")
            or type(max_findings) is not int or max_findings < 1
            or type(max_cost_units_per_chunk) is not int
            or not 0 <= max_cost_units_per_chunk <= _MAX_LEDGER_UNITS):
        raise ValueError("invalid chunk plan inputs")
    rows, seen = [], set()
    for finding in findings:
        fid, locator = finding.get("finding_id"), finding.get("locator")
        match = _SLIDE.match(locator or "")
        if not isinstance(fid, str) or not fid or fid in seen or not match:
            raise ValueError("findings require unique ids and exact PPTX slide locators")
        seen.add(fid)
        slide = int(match.group(1))
        if slide < 1:
            raise ValueError("PPTX slide locators are one-based")
        rows.append((slide, fid))
    if not rows:
        raise ValueError("at least one finding is required")
    rows.sort(key=lambda row: (row[0], row[1]))
    groups, group = [], []
    for row in rows:
        same_slide = group and group[-1][0] == row[0]
        if group and not same_slide and len(group) + 1 > max_findings:
            groups.append(group)
            group = []
        group.append(row)
        if len(group) > max_findings:
            raise ValueError("one slide exceeds the chunk finding limit")
    groups.append(group)
    boundaries = [{"first_slide": g[0][0], "last_slide": g[-1][0],
                   "finding_ids": [fid for _, fid in g]} for g in groups]
    identity = {"owner_id": owner_id, "run_id": run_id, "scan_id": scan_id,
                "file": file, "source_sha256": source_sha256,
                "assessment_snapshot_id": assessment_snapshot_id,
                "remediation_source_revision": remediation_source_revision,
                # The caller supplies one durable plan-creation timestamp. It is
                # identity, not the wall clock of an individual replay.
                "created_at": created_at,
                "format": "pptx", "chunks": boundaries,
                "max_cost_units_per_chunk": max_cost_units_per_chunk,
                "pricing_ref": pricing_ref}
    plan_id = _digest(identity)
    chunks = []
    for ordinal, boundary in enumerate(boundaries):
        chunk_id = _digest({"plan_id": plan_id, "ordinal": ordinal, **boundary})
        chunks.append({"chunk_id": chunk_id,
                       "boundary": {key: boundary[key] for key in ("first_slide", "last_slide")},
                       "finding_ids": boundary["finding_ids"],
                       "attempt_id": f"{plan_id}:chunk:{ordinal}",
                       "max_cost_units": max_cost_units_per_chunk,
                       "pricing_ref": pricing_ref})
    plan = {"owner_id": owner_id, "run_id": run_id, "plan_id": plan_id,
            "scan_id": scan_id, "file": file, "source_sha256": source_sha256,
            "assessment_snapshot_id": assessment_snapshot_id,
            "remediation_source_revision": remediation_source_revision,
            "format": "pptx", "eligible_finding_ids": [fid for _, fid in rows],
            "created_at": created_at}
    return AdmissionPlan(plan, tuple(chunks))

def reconcile_complete_set(admission: AdmissionPlan, receipts: list[dict], *,
                           source_sha256: str, assessment_snapshot_id: str,
                           remediation_source_revision: str) -> tuple[dict, ...]:
    """Return ordered receipts only for the exact current complete chunk set."""
    plan = admission.plan
    if ((source_sha256, assessment_snapshot_id, remediation_source_revision) !=
            (plan["source_sha256"], plan["assessment_snapshot_id"],
             plan["remediation_source_revision"])):
        raise ValueError("stale chunk set")
    expected, actual, claimed = {c["chunk_id"]: c for c in admission.chunks}, {}, set()
    for receipt in receipts:
        chunk_id = receipt.get("chunk_id")
        if chunk_id not in expected or chunk_id in actual:
            raise ValueError("unknown or duplicate chunk")
        if ((receipt.get("source_sha256"), receipt.get("assessment_snapshot_id"),
             receipt.get("remediation_source_revision")) !=
                (source_sha256, assessment_snapshot_id, remediation_source_revision)):
            raise ValueError("stale chunk receipt")
        ids = tuple(receipt.get("finding_ids") or ())
        if ids != tuple(expected[chunk_id]["finding_ids"]) or claimed.intersection(ids):
            raise ValueError("chunk finding coverage mismatch")
        claimed.update(ids)
        actual[chunk_id] = receipt
    if set(actual) != set(expected) or claimed != set(admission.finding_ids):
        raise ValueError("incomplete chunk set")
    return tuple(actual[c["chunk_id"]] for c in admission.chunks)
