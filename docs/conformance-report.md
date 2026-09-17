# Accessibility Conformance Report

**Product:** mova.io Accessibility Platform (web application UI)
**Standard:** WCAG 2.1, Level A & AA
**Status:** Not established — draft self-assessment. It contains a known 1.4.3 failure and an incorrect 1.4.13 entry (both corrected below), and screen-reader testing has not been performed. Evidence is now gathered under [`ui-accessibility-evaluation.md`](ui-accessibility-evaluation.md); rows below that lack recorded manual/AT evidence should be read as needing verification.
**Evaluation methods:** Partial automated checks (axe-core) + code / DOM-level semantic review (accessibility tree, keyboard handling, focus management, live-region markup). Automated checks do not establish conformance: the repo's own axe audits (`frontend/e2e/wcag-a11y.spec.js`, `frontend/src/wcagAxeMatrix.test.jsx`) cover selected views and states on simulated data, force the High-contrast palette on, and run WCAG 2.0/2.1 rule tags only; the jsdom matrix also disables `color-contrast` (ui-accessibility-evaluation.md §2). The September 2026 self-assessment reported a 1.4.3 failure; it used Accessibility Insights for Web 2.49.0 (axe-core 4.11.3) in Chrome and covered primary workflow views only. Code and jsdom review shows markup, not what a screen reader announces or how the page renders.
**Not yet performed:** Formal screen-reader user testing (NVDA / JAWS / VoiceOver).

> **Scope:** this report covers the conformance of the **platform's own web UI**, not the conformance of customer documents it remediates (reported separately via the WCAG coverage matrix).

**Conformance key:** Supports · Partially Supports · Not Applicable · *Needs verification* (working state, not a VPAT conformance term — evidence insufficient to claim either way). Rows without recorded manual or assistive-technology evidence should be read as needing verification.

# Part 1 · Platform UI conformance (WCAG 2.1 AA)

## Perceivable
| Criterion | Lvl | Conformance | Notes |
|---|---|---|---|
| 1.1.1 Non-text Content | A | Supports | Icons/images labeled or decorative; charts use `role="img"` + descriptive `aria-label` |
| 1.3.1 Info & Relationships | A | Supports | Headings, lists, tables, form labels, landmarks (`header`/`main`/`nav`) |
| 1.3.2 Meaningful Sequence | A | Supports | DOM order matches visual order |
| 1.4.1 Use of Color | A | Supports | Graph status uses ✓/!/× glyphs + color; legends carry text |
| 1.4.3 Contrast (Minimum) | AA | Partially Supports | Known failure: the September 2026 self-assessment reported insufficient text contrast on the update banner ("A new version of ACP is available.", `VersionToast.jsx`). Our own calculation from source at `73743c10` gives ≈ 3.3:1 (white on `#16a34a`). Fix implemented locally and covered by computed-contrast tests (≈ 5.0:1); not yet merged or deployed, and pending deployed-build and manual validation. Other text not re-measured in the default palette — see ui-accessibility-evaluation.md §10 |
| 1.4.4 / 1.4.10 Resize / Reflow | AA | Supports | Responsive; zoom not blocked |
| 1.4.11 Non-text Contrast | AA | Partially Supports | UI marks & graph dots corrected to ≥ 3:1. On the update banner, the dismiss glyph and focus outline computed below 3:1 from source (ui-accessibility-evaluation.md §10, F2/F5); fix implemented locally, pending deployed-build verification |
| 1.4.12 Text Spacing | AA | Supports | No clipping on spacing overrides |
| 1.4.13 Content on Hover or Focus | AA | *Needs verification* | Applies: `InfoTip`, `Term` and `RemediationOptionHelp` show `role="tooltip"` content on hover and focus. Dismissible/hoverable/persistent behaviour not yet tested (ui-accessibility-evaluation.md §6.9) |

## Operable
| Criterion | Lvl | Conformance | Notes |
|---|---|---|---|
| 2.1.1 Keyboard | A | Supports | All controls operable; graph uses roving tabindex (arrows / Enter / Escape) |
| 2.1.2 No Keyboard Trap | A | Supports | Dialogs trap intentionally; Escape always exits |
| 2.4.1 Bypass Blocks | A | Supports | Skip-to-main link |
| 2.4.2 Page Titled | A | Supports | Document title set |
| 2.4.3 Focus Order | A | Supports | Logical; no positive `tabindex` |
| 2.4.4 Link Purpose (In Context) | A | Supports | Link text is meaningful |
| 2.4.6 Headings & Labels | AA | Supports | Descriptive |
| 2.4.7 Focus Visible | AA | Supports | `:focus-visible` outline on all controls |
| 2.5.3 Label in Name | A | Supports | Visible labels match accessible names |

## Understandable
| Criterion | Lvl | Conformance | Notes |
|---|---|---|---|
| 3.1.1 Language of Page | A | Supports | `<html lang>` set |
| 3.2.1 / 3.2.2 On Focus / On Input | A | Supports | No unexpected change of context |
| 3.2.3 / 3.2.4 Consistent Navigation / Identification | AA | Supports | Consistent navigation & component identity |
| 3.3.1 / 3.3.2 Error Identification / Labels | A | Supports | Inputs labeled; minimal forms |

## Robust
| Criterion | Lvl | Conformance | Notes |
|---|---|---|---|
| 4.1.2 Name, Role, Value | A | Partially Supports | Correct roles/names on custom controls. DOM-level (jsdom) tests found tab-pattern gaps in several in-page tab sets — fixed locally, not deployed — and the Review workspace tabs show the same gaps from source reading (ui-accessibility-evaluation.md §9). Not verified with a screen reader |
| 4.1.3 Status Messages | AA | *Needs verification* | `aria-live`/`role=status` markup is present on scan, chat, monitor, and assess results, but markup does not show announcement: axe has no 4.1.3 rule (`config/acr-manual-test-plans.json`) and screen-reader testing has not been performed. Per-message AT check: ui-accessibility-evaluation.md §6.10 |

# Part 2 · Document remediation coverage (WCAG 2.1 + 2.2)

Beyond its own conformance, the platform detects and remediates accessibility issues in the documents it processes. Coverage across all 87 success criteria:

| Coverage | Count | How |
|---|---|---|
| Live | 28 | Deterministic auto-fix or AI (Claude vision / Whisper) |
| Covered · HITL | 42 | Detect-and-route to a human reviewer |
| Partner-provided | 12 | Partner web scanner |
| Roadmap | 5 | Human-produced media (sign language, audio description) |

| Conformance level | Criteria | Covered | Status |
|---|---|---|---|
| Level A · must-have | 32 | 32 / 32 | Fully covered |
| Level AA · legal target | 24 | 24 / 24 | Every criterion has a coverage method |
| Level AAA · optional | 31 | 26 / 31 | 5 optional (human-produced media) remaining |

"Covered" means the platform has a capability for the criterion — deterministic auto-fix, AI, the partner web scanner, or a human-in-the-loop review workflow — as classified in the capability matrix. It is **capability coverage, not conformance**: it does not mean any individual output document conforms to WCAG at Level A or AA, which depends on that document's own assessment and review. The full per-criterion matrix is available as the accompanying **coverage matrix (Excel)** and **method deck (PowerPoint)**.

## Summary statement
Conformance of the platform UI to WCAG 2.1 Level AA is **not established**. A known 1.4.3 contrast failure on the update banner has a locally implemented and tested fix that is still pending deployed-build and manual validation; 1.4.13 was wrongly marked Not Applicable; 4.1.3 has markup but no assistive-technology verification; and no screen-reader evaluation has been performed. Two issues found during an earlier manual review (an unannounced status update and a missing navigation landmark) were remediated. The protocol and evidence checklist for establishing conformance — including the WCAG 2.2 additions — is [`ui-accessibility-evaluation.md`](ui-accessibility-evaluation.md).

---
*Generated from the in-app self-audit. A one-click PDF export is available from the ♿ accessibility self-check panel, which renders only in development builds or when the URL carries `?a11y` (`SHOW_A11Y` in `App.jsx`). That PDF (`exportConformanceReport` in `frontend/src/pdfReport.js`) carries the same corrected 1.4.3, 1.4.13, 4.1.3 and summary wording; `frontend/src/uiConformanceClaims.test.js` keeps the two in agreement.*
