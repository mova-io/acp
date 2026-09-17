# Document-wide chunking groundwork

`api/document_wide_chunk_contract.py` is feature-disabled and is not imported by
remediation dispatch. It covers PPTX because existing findings carry stable
slide-part locators that provide deterministic boundaries.

The planner emits the exact plan/chunk vocabulary consumed by
`ChunkPlanAdmission`: source SHA-256, Assessment snapshot, remediation source
revision, nonoverlapping slide boundary, ordered finding IDs, one durable
plan-creation timestamp, and the exact per-chunk price and maximum. Only the ledger-backed admission transaction can
grant spending authority; the planner accepts no caller-asserted budget proof.
Reconciliation requires the complete current set and exact finding union.

The disabled planner can represent more than 20 findings by keeping each
provider request at the existing 20-finding limit. It does not change the live
aggregate limit or any source, archive, page, or image limit and does not
authorize an automatic customer rerun. Live integration requires a separately
reviewed aggregate cost/memory/source cap, validated receipt persistence, and
publish gating on a persisted complete-set reconciliation.
