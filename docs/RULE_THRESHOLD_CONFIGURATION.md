# Configurable rule thresholds — design and level of effort

Status: **estimate only**. Nothing here is built except phase 0 (the read-only explanation
catalog, Settings → Rule explanations). This document does not authorise weakening any WCAG
requirement, and it does not propose an unconstrained per-rule editor.

## What exists today (read from the code, 2026-09-18, origin/main `1543b67b`)

| Question | Answer | Where |
|---|---|---|
| Criteria in scope | 38 success criteria | `assessment_policy.RULE_CATALOG` |
| Formats | html, docx, pptx, xlsx, pdf | `remediation_capability.FORMATS` |
| Criterion × format cells | 190 in total; 122 ACP has a capability for (html 33, pptx 25, docx 24, pdf 22, xlsx 18); 68 not applicable | `remediation_capability.remediation_table()` |
| Office-analyser rule IDs | 70 (docx 17, pptx 20, xlsx 18, pdf 15) | `config/rule-catalog.json` (v2026.08.1) |
| Global scoring policy | one rubric: `disabled_rules`, `compliant_threshold`, `severity_weights` | `app_settings` row `rubric_active`; `PUT /rubric`, admin-gated (`_require_admin`) |
| Per-user overrides | namespaced `app_settings` keys, no schema change | `store.set_user_setting` / `resolve_setting` |
| Per-assessment scope | criteria × formats chosen per run; context-local, not persisted as policy | `assessment_selection`, scope presets in `assessment_policy` |
| Tenant | there is no org/tenant table; `owner_email` is the tenant key everywhere | `store.py` (scan_runs note) |
| Reproducibility | `rubric_hash` (sha256 of the canonical rubric JSON) is stamped on every scanned file and keys analysis de-duplication | `scripts/rubric.py`, `handlers.find_prior_analysis` |
| Detector thresholds | module constants, read at import; no detector takes a threshold argument | e.g. `office_structure.PSEUDO_HEADING_MIN_HALF_PT` |

The last row is the whole cost. Every threshold is a constant a detector reads directly, often
shared with its remediator so that detection and fix stay in lock-step (the pseudo-heading
predicate is the example this PR extends). Making one configurable means threading a value
through detector **and** fixer **and** re-scan verification, not adding a form field.

The rubric hash is the good news: anything placed inside the rubric document is hashed, stamped
on every result and already invalidates cached analyses. A threshold block stored in the rubric
therefore gets change detection ("was this file assessed under the current settings?") for
free. It does not get history: see the second finding below.

## Not every rule has a knob

Most rules are presence checks with no number in them (an image has alt text or it does not; a
form control has a Title or it does not). WCAG numbers — 4.5:1 and 3:1 contrast, the 18pt /
14pt-bold large-text boundary — are the **requirement**, not an ACP heuristic, and must not
become editable. Rules that need human judgement (meaningful sequence, sensory characteristics,
whether alt text is accurate) have no numeric setting at all; the honest control for them is the
review lane, which already exists. Only ACP's own heuristics — heading size margins, word limits,
peer counts, geometry tolerances — are candidates. Counts per format are in the table below.

### Counts

Counted from the explanation catalog (`api/rule_explanations/`), whose drift tests re-derive
every number from the code that decides by it:

| Format | Cells | WCAG-standard numbers | ACP heuristic numbers | Word lists | Cells with no number |
|---|---|---|---|---|---|
| docx | 24 | 7 | 32 | 2 | 10 |
| pptx | 25 | 9 | 20 | 1 | 11 |
| xlsx | 18 | 3 | 17 | 1 | 7 |
| pdf | 22 | 6 | 45 | 1 | 7 |
| html | 33 | 1 | 16 | 1 (+ abbreviation glossary) | 22 |

About 15 of the office heuristics are shared by all three formats (OCR images of text, reading
level, language of parts, sensory wording). Roughly **100 distinct heuristic numbers** remain,
and at least 22 of them (pdf 14, html 8) are inline in function bodies rather than named
constants. Those have to be extracted before anything can configure them.

Configurable today without a code change:
- `rubric.disabled_rules` and `rubric.compliant_threshold` (global, `PUT /rubric`).
- Scan scope: a platform default, a per-user widen-only override, and a per-assessment selection.
- Per-file scope rules.
- `level=A|AA` on assess.
- Five or six OCR values, set as deployment environment variables (`ACP_OCR_*`).

None of the detector heuristics are admin-editable.

Two findings from building the catalog bear directly on this work:
- **26 office rule-catalog IDs are never emitted by any code** (e.g. `PPTX-RESIZE-001`,
  `XLSX-CONTRAST-001`). `disabled_rules` matches exact IDs, so disabling one of these changes
  nothing. The explanation payload lists them as `not_emitted`. A settings UI built on top of
  the rubric has to show that, or it will offer switches that do nothing.
- **The rubric has a hash but no history.** Each run stores the rubric name and hash, but the
  configuration behind an old hash is not kept, so an old result cannot be traced back to its
  settings. Phase 5 has to add that history, which it can do as `app_settings` rows keyed by
  hash, with no migration.

## Phases, ranked

Estimates are engineering days for one engineer who knows this codebase, including tests.
They are ranges because the thresholds are not yet threaded anywhere; confidence is stated.

| # | Phase | Days | Confidence | Notes |
|---|---|---|---|---|
| 0 | Read-only explanation catalog + Settings tab | done in this PR | — | `api/rule_explanations/`, `GET /rules/explanations` |
| 1 | Bounded heading heuristic settings (section-label word limit, peer count, size margins), global, validated ranges, stored inside the rubric so the hash changes | 3–5 | medium | One rule family; detector + promoter already share one predicate. Needs a ContextVar (the `assessment_selection` pattern) so workers read the value without changing detector signatures. |
| 2 | Other ACP heuristic families with server-side validation (min/max, type, never past the WCAG number) | 12–20 | low | ~100 distinct heuristic numbers in ~25 families; 22+ must first be extracted from function bodies. Priced per FAMILY (one validated block + one boundary fixture each), not per number. Recommend a curated subset of families, not all ~100. |
| 3 | Defaults, reset-to-default, diff-from-default display | 2–3 | high | Defaults are the constants; reset deletes the rubric key. |
| 4 | Role, audit, tenant isolation | 4–8 | low | Admin gate exists. Audit trail of rubric edits does not. Per-tenant values need a tenant concept that does not exist (owner_email only); per-user keys work without schema change but are not org policy. |
| 5 | Versioned snapshot, reproducible re-scan, stale decisions | 5–10 | low–medium | The hash exists and is stamped per file; the config behind an old hash is not kept. Needed: rubric history (app_settings rows keyed by hash), re-run under a named version, and approved decisions marked stale when a threshold behind their finding changes. |

**Total for phases 1–5: about 26–46 engineering days, low-to-medium confidence.** Phase 1
alone is independently shippable and delivers the concrete ask (tuning the heading heuristic)
without touching anything else. No calendar date is implied.

## Assumptions and open questions

- Global (platform-wide) settings first. Per-organisation settings wait for a real tenant model.
- No schema migration: values live in the existing rubric `app_settings` row.
- WCAG-mandated numbers stay fixed; the UI shows them as "WCAG requirement".
- A threshold change never silently rewrites past results: old results keep their `rubric_hash`,
  and a re-scan is required to see the effect.
- Who may change a heuristic: platform admin (same gate as `PUT /rubric`).
