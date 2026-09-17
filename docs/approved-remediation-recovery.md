# Approved remediation recovery — September 2026

PR #2129 follows the report-quality release (#2125) and fixes failures reproduced during the UTSW discharge-document test. No customer document is stored in the fixtures.

| Reproduced issue | Live implementation | Regression evidence |
| --- | --- | --- |
| Quality-first vision refused configured cloud models before any call | `vision_generation._CaptionGenerator` preserves the configured zone map | `test_quality_first_zone_propagation.py`: actual managed dispatch, ledger, and non-cloud refusal; removing propagation fails |
| Empty Word crop tag blocks an approved text replacement | `apply_office_image_replacement` checks validated zero geometry and retains real-crop/rotation/shared-placement guards | `test_image_of_text_writer_crop_geometry.py`: saved OOXML and refusal cases |
| Already approved item counted as another human decision; misleading Saved banner | `remediationInboxModel`, responsibility helpers, and actual `Remediate` mapping preserve approval versus application | `approvedWriteRecovery.test.jsx`, `approvedWriteRecoveryLive.test.jsx`: live page, queue counts, real API route, no second PUT decision |
| No action for approved but unwritten content | Owner-scoped POST `/hitl/queue/{id}/retry-write`, exact item scope and worker-time binding/byte checks | `test_approved_writer_admission_recovery.py`: auth, foreign owner, stale/missing binding, scoped values, unchanged decision, terminal job, concurrent retry, actual writer |
| Unnamed drawing assessed but omitted from document-wide AI | Packager admits only a unique relationship resolving to the exact writer target; manifest preserves assessed identity and visual evidence | `test_document_wide_unnamed_docx.py`: real Office scanner before/after, two objects, collision/shared target refusal, unchanged graphic, unavailable unsafe preview |
| Generic AI pause hides the dispatch failure | Owner-scoped pre-dispatch refusal event plus bounded cause codes in recovery evidence | `test_vision_refusal_evidence.py`: no fabricated provider call or spend, cancellation and cross-owner checks |

An approval retry is bounded to one attempt per unchanged approval. Missing immutable proposal records or saved artifact identity cause a clear refusal. Known changes return the item to recheck; approval is never invented or marked applied merely to drain a queue. Pending source checks, failed verification and unsupported findings remain visible.

The document's device-instruction illustration is not assumed to be disposable. Deployment does not retry or delete customer graphics. Alt-text writing preserves the image; replacement remains an explicitly approved operation with existing geometry and verification checks. Automated checks cannot establish perfect semantic accuracy, and no 100% remediation claim is made.

Claude implemented three independent frontend, writer/recovery, and vision streams. Its session limit interrupted the second integration round and fourth packaging stream. Codex completed the packaging change and integration, including correcting a mismatched endpoint contract and discovering the page's missing application-state mapping through a real mounted-page test.

Validation: 807 frontend files / 9,985 tests and build passed; 253 related backend tests, 94 concurrency/standing-approval tests, and 33 focused binding/unnamed tests passed (overlapping selections). Matrix and TODO guards passed. Final progress-trailer guard and complete backend, PostgreSQL, and Playwright checks run in CI on the committed head. The unnamed-target test also fails when that implementation is removed.
