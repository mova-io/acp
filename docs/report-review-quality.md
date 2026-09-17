# Report review quality — requirement coverage

Branch `codex/report-review-quality`. Every requirement maps to the code that implements it and the
test that proves it. Where the honest answer is a limitation, it is stated in **Limitations** below
rather than being left to look implemented.

The shape of the work: the **server** builds the report facts (`api/report_facts.py`) and the browser
renders them. A report is bound to the evidence it was built from by `factsDigest`, so a stale
client model is refused (409) instead of being re-stamped with current identity.

## Approved product scope

| # | Requirement | Code | Test |
|---|---|---|---|
| 1 | One-page decision summary: documents assessed, edits saved, findings verified resolved, remaining findings, human checks pending, checks not performed; score secondary | `reportModel.js` (`decisionSummary`, `stageStrip`), `api/report_render.py` summary layout | `reportEvidenceTruth.test.js`, `tests/test_report_render.py` (one page × 3 kinds), `reportFactsAccounting.test.js` |
| 2 | Changes-to-confirm cards: location, before/after, reason, visual evidence, technical verification separate from human confirmation, Accept/Correction/Reject/Unable + notes, bound to artifact identity and stale on change | `reportEvidence.js` `buildChangeCards`, `ChangeReviewPanel.jsx`, `api/routes/change_review.py`, `changeReview.js` | `ChangeReviewPanel.test.jsx`, `changeReviewRefusals.test.jsx`, `tests/test_change_review_routes.py` |
| 3 | Remaining work: per-finding cards, actionable priority, impact, steps, owner, recheck condition — no collapsing distinct objects under a criterion | `reportEvidence.js` `buildFindingCards` / `rankFindingCards`, `api/report_facts.py` findings ledger | `reportEvidenceTruth.test.js`, `tests/test_report_facts.py` |
| 4 | Complete evidence appendix: all records, stable ids, artifact identity, scope, timestamps, reviewer records | `reportModel.js` full mode, `htmlReport.js`, `api/report_render.py` | `reportEvidenceTruth.test.js`, `htmlReport.test.js`, `tests/test_report_render.py` |
| 5 | Summary / Reviewer packet / Full evidence in the live scan, file and remediation UI; large scans handled | `ReportModeMenu.jsx`, `FileDrawer.jsx`, `Overview.jsx`, `Transparency.jsx`, `Remediate.jsx`, `fileReportData.js` paging | `fileDrawerReports.test.jsx`, `remediationReportModes.test.jsx`, `remediationReportMenu.test.jsx`, `scanReportLiveWiring.test.jsx` |
| 6 | What changed since the previous assessment, from comparable snapshots only | `api/report_facts.py` `previous`/`previousReason`, `reportEvidence.js` `buildComparison` | `tests/test_report_facts.py`, `scanReportLiveWiring.test.jsx`, `reportEvidenceTruth.test.js` |
| 7 | Object/page links where possible, explicit "unavailable" otherwise; not page-one-only | exact-bytes preview `GET /scans/{sid}/files/{f}/artifact/{sha256}/page/{page}`, `fileReportData.js` preview collection | `tests/test_report_facts_routes.py`, `fileReportData.test.js` |
| 8 | Reviewer decisions preserved, version-bound, stale flagged, reusing the existing store | `scan_decisions` kind `change_review:<id>` (no schema change), `api/store.py` `save_change_review`, `api/routes/change_review.py` | `tests/test_change_review_routes.py`, `ChangeReviewPanel.test.jsx` |

## Audit findings from the first review

| Finding | Fix | Test |
|---|---|---|
| `fullyConformant` ignored FIXED-awaiting-revalidation and UNCHECKED | readiness is a four-part conjunction (`technicalReady`, saved changes, human confirmation, per-finding accounting) | `reportEvidenceTruth.test.js` |
| `routing.fixed` = automation classification labelled "Auto-fixed" | `routing.eligibleAuto`; eligible / saved / verified are three separate counts | `reportEvidenceTruth.test.js` |
| `computeCoverageRows` dropped findings the model needed | `reportEvidence.js` `attachFileIssues`, server finding ids | `fileReportData.test.js`, `reportEvidenceTruth.test.js` |
| before/after capped at 24 items, 600 chars | full retention in full mode; clipping disclosed where the STORE clipped at 2000 chars | `reportEvidenceTruth.test.js`, `beforeAfterReport.test.js` |
| browser jsPDF: untagged, no bookmarks, broken glyphs | server renderer (`api/report_render.py`), jsPDF retained but unmounted | `tests/test_report_render.py` |
| WeasyPrint chart labels clipped, no page numbers | `api/report_weasy.py` | `tests/test_report_weasy_structure.py` |
| release follow-up duplicated each finding | one integrated card per finding | `tests/test_release_reports.py` |

## Parent review findings (second round)

| Finding | Fix | Test |
|---|---|---|
| One change credited two findings of the same criterion | resolution only from a per-finding ledger; otherwise null + `accountingReason` | `tests/test_report_facts.py`, `reportFactsAccounting.test.js` |
| Pending/rejected/correction-requested/unable/stale reviews absent from "no outstanding items" | readiness conjunction includes human confirmation | `reportEvidenceTruth.test.js` |
| Documents assessed derived from catalog rows | from `assessment.state`; error/partial never reads as "no findings" | `tests/test_report_facts.py` |
| Three new routes unmapped in the capability map | mapped, with role tests | `tests/test_capability_map_is_complete.py`, `tests/test_report_route_authorization.py` |
| `expected_sha256` / `change_digest` optional on mutation | both required (422), mismatch 409, compare-and-set in one transaction | `tests/test_change_review_routes.py` |
| Decisions bindable to a source checksum | bind only to `corrected_sha256`; refuse 409 when it is not recorded | `tests/test_change_review_routes.py` |
| Unknown freshness rendered as accepted | `stale: null` → "freshness unknown" | `reportEvidenceTruth.test.js` |
| "Edited" implied edits were written | "correction requested — not applied", never counted as confirmation | `tests/test_change_review_routes.py`, `reportEvidenceTruth.test.js` |
| `/page` preview captioned "after" though provenance is ambiguous | exact-bytes route by digest; ambiguous route captioned "version not verified" | `tests/test_report_facts_routes.py`, `fileReportData.test.js` |
| Stale client model stamped with current server identity | `factsDigest` required; 409 "report data is out of date" | `tests/test_report_render_routes.py`, `reportRenderStale.test.js` |
| Reports omitted applied-but-unverified AI edits | facts include `unverified_changes.saved_changes`, marked "AI applied · not verified" | `tests/test_report_facts.py`, `reportEvidenceTruth.test.js` |
| Summary PDF was two pages | summary trimmed in the model and in the renderer | `tests/test_report_render.py` |
| `recommended_action` / `remediation` lost | preserved verbatim through facts → model → HTML/PDF | `reportEvidenceTruth.test.js` |
| `generateScanReport` discarded the renderer outcome | returns `{...renderResult, model}`; every caller reads it | `scanReportLiveWiring.test.jsx` |
| Exact-bytes preview swallowed by a greedy route | `report_facts.router` registered first, pinned by a test | `tests/test_report_facts_routes.py` |

## Limitations (honest, not deferred silently)

Final integration checks also cover a scan digest changing when before/after values change
without changing counts (`test_scan_digest_binds_values_even_when_counts_and_artifact_do_not_change`),
retention beyond 1,000 findings / 500 changes (`test_default_file_facts_retain_records_beyond_old_presentation_caps`),
and rejecting corpus path traversal (`test_local_preview_cannot_escape_corpus`). Per-document facts
retain all stored records by default; the scan index is paginated. When the PDF renderer reaches
its size limit, the same complete model is downloaded as HTML with an explicit notice
(`reportRenderClient.test.js`). Authentication and stale-evidence refusals never use that fallback.

- **Clipped values are unrecoverable.** The store clips saved-change before/after text at 2000
  characters (notes at 500) when recording it, and the untruncated text is kept nowhere. The report
  says "clipped when recorded" rather than pointing at a fuller record that does not exist.
- **Per-finding resolution needs a ledger.** Where `remediation_contribution` holds no per-finding
  record, "findings verified resolved" is "Not recorded" with the reason, not a number.
- **Page previews are PDF-only**, bounded per report, and the report names the pages it left out.
- **No deep link** from a report to a specific document/page exists in the app, so location links are
  reported as unavailable rather than invented.
- **veraPDF automated checks are not a PDF/UA conformance claim.** What is tested is structure
  (tags, roles, language, title, bookmarks, embedded fonts) plus veraPDF's automated ua1 checks;
  the manual checks PDF/UA requires are not performed.
