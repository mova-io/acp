import pytest
from document_wide_chunk_contract import ENABLED, build_pptx_plan, reconcile_complete_set

SHA = "a" * 64

def findings(count=21):
    return [{"finding_id": f"f{i:02d}", "locator": f"ppt/slides/slide{i + 1}.xml#rId2"}
            for i in range(count)]

def build(**overrides):
    values = dict(owner_id="owner@example.com", run_id="run", scan_id="scan",
                  file="deck.pptx", source_sha256=SHA,
                  assessment_snapshot_id="assessment-snapshot",
                  remediation_source_revision="assessment-manifest", findings=findings(),
                  created_at="2026-09-17T00:00:00Z", max_findings=20,
                  max_cost_units_per_chunk=3, pricing_ref="verified-price-v1")
    values.update(overrides)
    return build_pptx_plan(**values)

def receipt(admission, chunk):
    plan = admission.plan
    return {"chunk_id": chunk["chunk_id"], "source_sha256": plan["source_sha256"],
            "assessment_snapshot_id": plan["assessment_snapshot_id"],
            "remediation_source_revision": plan["remediation_source_revision"],
            "finding_ids": list(chunk["finding_ids"])}

def test_disabled_plan_is_deterministic_and_matches_ledger_admission_contract():
    assert ENABLED is False
    admission = build()
    assert admission == build()
    assert len(admission.chunks) == 2
    assert set(admission.finding_ids) == {f"f{i:02d}" for i in range(21)}
    for ordinal, chunk in enumerate(admission.chunks):
        assert chunk["attempt_id"] == f"{admission.plan['plan_id']}:chunk:{ordinal}"
        assert chunk["max_cost_units"] == 3
        assert chunk["pricing_ref"] == "verified-price-v1"
    assert "admitted_budget_units" not in admission.plan

def test_planner_output_is_accepted_by_the_single_durable_admission_contract(
        isolated_store, monkeypatch):
    from document_wide_chunk_admission import ChunkPlanAdmission
    from test_document_wide_chunk_store import seed
    seed(isolated_store, monkeypatch)
    with isolated_store._db.cursor() as cur:
        isolated_store._db.execute(cur, "UPDATE ai_spending_budgets SET cap_units=100 "
                                        "WHERE owner_id=%s AND run_id=%s",
                                   ("owner@example.com", "run"))
    admission = build(remediation_source_revision="remediation-source")
    result = ChunkPlanAdmission(isolated_store).admit(admission.plan, list(admission.chunks))
    assert result["plan_id"] == admission.plan["plan_id"]
    assert result["attempt_ids"] == [chunk["attempt_id"] for chunk in admission.chunks]

def test_boundary_keeps_one_slide_whole_and_refuses_oversized_slide():
    same_slide = [{"finding_id": f"f{i}", "locator": "ppt/slides/slide7.xml#rId2"}
                  for i in range(21)]
    with pytest.raises(ValueError, match="one slide"):
        build(findings=same_slide)

def test_planner_enforces_canonical_ledger_value_domain():
    maximum = build(max_cost_units_per_chunk=2**63 - 1, pricing_ref="p" * 512)
    assert maximum.chunks[0]["max_cost_units"] == 2**63 - 1
    assert maximum.chunks[0]["pricing_ref"] == "p" * 512
    for overrides in (
            {"max_cost_units_per_chunk": 2**63},
            {"pricing_ref": "p" * 513},
            {"owner_id": "o" * 513},
            {"run_id": "r" * 513}):
        with pytest.raises(ValueError, match="invalid chunk plan inputs"):
            build(**overrides)

def test_plan_identity_binds_durable_creation_timestamp_and_rejects_slide_zero():
    assert build(created_at="2026-09-17T00:00:00Z").plan["plan_id"] != \
           build(created_at="2026-09-17T00:00:01Z").plan["plan_id"]
    with pytest.raises(ValueError, match="one-based"):
        build(findings=[{"finding_id": "f0", "locator": "ppt/slides/slide0.xml#rId2"}])

def test_complete_set_requires_exact_order_independent_union_and_current_identity():
    admission = build()
    receipts = [receipt(admission, chunk) for chunk in reversed(admission.chunks)]
    ordered = reconcile_complete_set(admission, receipts, source_sha256=SHA,
        assessment_snapshot_id="assessment-snapshot",
        remediation_source_revision="assessment-manifest")
    assert [row["chunk_id"] for row in ordered] == [c["chunk_id"] for c in admission.chunks]

@pytest.mark.parametrize("mutation", ["missing", "stale", "overlap", "duplicate"])
def test_missing_stale_overlapping_or_duplicate_receipts_fail_closed(mutation):
    admission = build()
    receipts = [receipt(admission, chunk) for chunk in admission.chunks]
    if mutation == "missing": receipts.pop()
    elif mutation == "stale": receipts[0]["source_sha256"] = "b" * 64
    elif mutation == "overlap": receipts[1]["finding_ids"] = [admission.chunks[0]["finding_ids"][0]]
    else: receipts[1] = dict(receipts[0])
    with pytest.raises(ValueError):
        reconcile_complete_set(admission, receipts, source_sha256=SHA,
            assessment_snapshot_id="assessment-snapshot",
            remediation_source_revision="assessment-manifest")
