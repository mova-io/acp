# ACP — comprehensive to-do

## Current delivery backlog — 14 September 2026

This section is the current operational priority list. Older authored sections below retain historical decisions and may have stale summaries; the generated capability block remains authoritative for coverage.

- **Shipped:** PR #2061 deleted-scan stage protection; PR #2062 human/status count separation; PR #2063 Discovery source tiles; PR #2064 retained activity history, scroll control, filters, actual AI model/location, exact-version evidence, shared compact accounting, timely assessment notifications and accepted-plan popup continuity. Production 2026.9.14.6 contains these changes.
- **Merged:** PR #2067 removes the fixed 200 ms worker-lease test assumption and covers delayed startup. All required CI checks passed before merging; production worker code is unchanged.
- **In progress:** actual HTTP transport durations and owner-bound performance summaries. Legacy request records cannot support local/cloud latency, quality or GPU comparisons; unknown metrics must remain explicitly unknown.
- **Operational validation:** run a fresh owner-authorized scan through remediation, exact-byte verification and destination confirmation. Jeremy's latest 147-file assessment was erased before remediation at 17:51:30 UTC; deleted runs cannot be resumed.
- **Deva delivery closure:**75 published receipts match saved correction hashes;10 delivery records remain incomplete. Recovery requires her original-owner SharePoint credentials. The recovered source-derived document remains a review copy, by explicit user decision.
- **Infrastructure:** provision and validate managed Redis before cutover. Preserve existing TTLs, retain rollback references, prevent writes during the API/worker endpoint transition, and keep the active-job deployment guard. See [managed Redis cutover](runbooks/managed-redis-cutover.md).

Completed UI work above is not remaining polish. Full WCAG conformance and successful publication remain separate claims.

Authored sections reviewed against `d289a63` (main, 2026-07-28). The previous
snapshot was `9d7c7a3` — **387 commits and 19 days earlier**, and several items
below had shipped without being struck through. That is the failure this file's
new split is meant to prevent:

* **Generated** — the coverage-status block below is derived from the code that
  decides it, and `scripts/gen_todo_status.py --check` fails CI when it drifts.
  Nothing forced the old counts to stay current, so they didn't.
* **Authored** — everything else. Whether we build a thing, when, who owns it,
  and why it was deferred are judgements no generator should fake.

Items are struck through when the code says they shipped, with the evidence
named — not when someone remembers closing them.

Current coverage (87 WCAG 2.1/2.2 success criteria) — **now generated**, in the
coverage-status block below. It used to be a hand-maintained table here, carrying
36 / 45 / 6 and this instruction: *"treat them as 2026-07-09 figures until someone
re-counts against the catalog"*, because `frontend/src/wcagCatalog.js` was not in
the checkout when the paragraph was written.

The catalog arrived on 2026-08-28 (`f7475813`, #907) and nobody re-counted. The
real split is **37 / 44 / 6** — one criterion had moved from HITL to shipped and
the table could not say so. That is precisely the failure this header describes
two paragraphs up, recurring in the half the generator had not reached, so the
counts moved into it rather than being corrected in place.

The roadmap bucket stays closed and stays authored, because it is a decision
rather than a count: the 5 pure-media AAA "MDK net-new" SCs
(1.2.6/1.2.7/1.2.8/1.2.9/1.4.7) were already `Human / AT` + Tier 3 and rendered
HUMAN-tier per file, so `75fc6b8` reclassified them `net-new → MDK HITL`. Zero
roadmap.

Every one of the 87 SCs has a closed disposition. **No Required (A/AA) gap** —
every Required criterion is auto-detected or HITL-routed matching the checklist's
own "Human / AT" designation.

**The 4 Required format gaps (1.4.1, 1.3.5, 2.5.3, 4.1.2)** were this file's
headline open item for months, described as "auto-detected for HTML but
UNCHECKED for PDF/Office". Two thirds of that is no longer true, and the
generated table below now answers it with live data every run instead of a
sentence nobody re-checked.

The distinction it asked for — *"doc-applicable, not-auto-here"* versus
*"web-only → N-A"* — is exactly what `Coverage.DECLARED` and
`Coverage.UNSUPPORTED` encode in `api/rule_registry.py`, arrived at from the
other direction while fixing 4.1.2 on PDF. `1.4.1` and `4.1.2` now carry real
per-format signal; `1.3.5` and `2.5.3` remain HTML-only and are the genuine
remainder.

<!-- BEGIN GENERATED: coverage-status — written by scripts/gen_todo_status.py. Do not
     hand-edit: the next run overwrites it, and `--check` fails CI if it is stale. -->

### Coverage status — generated, do not edit by hand

Regenerate with `python scripts/gen_todo_status.py`; CI fails if this block is stale (`--check`). Everything below is read from `api/rule_registry.py`, `api/store.py` (`RULE_FORMATS` / `REVIEW_FORMATS`) and `config/rule-catalog.json` — the code that actually decides it. Intent, priority and ownership are authored above and below; this block never speaks to those.

**Conformance target: AA.** Criteria above the target are not assessed at all (`store.in_target`). Selectable targets are A, AA, so the 7 AAA criteria are never assessed: `1.4.6`, `1.4.8`, `1.4.9`, `2.4.10`, `2.4.9`, `3.1.4`, `3.1.5`.

This is a behaviour change, not bookkeeping: several detectors compute the AA and AAA thresholds in one pass, so AAA findings were previously scored against AA-target files.

**Criterion disposition — 87 success criteria**, counted from `frontend/src/wcagCatalog.js` (its `source:` field). Every criterion has a closed disposition; the buckets are the catalog's own.

| Bucket | Count | Meaning |
|---|---:|---|
| Shipped (demo) | 37 | Real automated validator, verified backing |
| MDK HITL | 44 | Human-judgment criteria — routed to the HITL queue |
| Partner baseline | 6 | Covered by the .NET partner engine (`spike/dotnet/AcpScan.Cli`) |

**Capability registry — 35 (criterion, format) pair(s) migrated.** Coverage is declared beside the detector; only `full` may certify a pass.

| Criterion | Format | Coverage | Confidence | Not covered |
|---|---|---|---|---|
| `1.1.1` | docx | **partial** | high | charts, SmartArt, grouped shapes and embedded OLE objects are non-text content this walk does not reach, and w |
| `1.2.1` | av | **partial** | high | the video-only half of 1.2.1 (a silent moving image that carries information) is not covered — establishing th |
| `1.2.2` | av | **partial** | high | presence only: this reads whether a caption track is in the container and whether a caption or transcript file |
| `1.3.1` | pdf | **partial** | high | semantic correctness and other document relationships require review |
| `1.3.5` | docx | **heuristic** | low | the vocabulary match is approximate |
| `1.3.5` | pdf | **heuristic** | low | the vocabulary match is approximate and some organisational forms will produce false positives |
| `1.4.1` | docx | **partial** | high | colour used as the sole carrier of meaning anywhere else — shaded table rows, coloured glyphs, chart series ke |
| `1.4.1` | pdf | **partial** | high | colour used as the sole carrier of meaning elsewhere — colour-keyed legends, chart series, status indicators — |
| `1.4.1` | pptx | **partial** | high | colour used as the sole carrier of meaning elsewhere — chart series, shaded table cells, status markers withou |
| `1.4.1` | xlsx | **partial** | medium | colour used in cell fills, charts and images is not examined, and whether colour is the sole cue is left to a  |
| `1.4.10` | docx | **partial** | high | whether a wide table actually requires horizontal scrolling at 320px is a rendered outcome not recorded in the |
| `1.4.10` | pptx | **partial** | high | whether the widest table actually requires horizontal scrolling at 320px is a rendered outcome not recorded in |
| `1.4.11` | docx | **partial** | high | gradient or image fills and non-shape non-text elements such as focus indicators and control borders are not e |
| `1.4.11` | pdf | **partial** | medium | gradient fills, bitmap images and most icon glyphs are not examined, and whether a low-contrast element convey |
| `1.4.11` | pptx | **partial** | high | gradient or image fills and non-shape non-text elements such as focus indicators and control borders are not e |
| `1.4.11` | xlsx | **partial** | medium | theme-coloured shapes, gradients, images and control affordances are not examined, and whether a shape conveys |
| `1.4.12` | docx | **partial** | high | whether the fixed spacing clips text when a user applies the WCAG 1.4.12 overrides is a rendered outcome not r |
| `1.4.12` | pdf | **partial** | high | whether text actually clips when the override is applied is a rendered outcome not recorded in the file, and o |
| `1.4.12` | pptx | **partial** | high | whether the fixed box clips the text when a user applies the WCAG 1.4.12 overrides is a rendered outcome not r |
| `1.4.4` | pptx | **partial** | high | whether the contained text visually clips when the user enlarges to 200% is a rendered outcome not recorded in |
| `2.1.2` | docx | **partial** | high | whether focus can actually move away from a control is runtime behaviour that depends on the control's own imp |
| `2.1.2` | pptx | **partial** | high | whether focus can actually move away from a control is runtime behaviour that depends on the control's own imp |
| `2.1.2` | xlsx | **partial** | medium | whether focus can actually move away from a control is runtime behaviour that depends on the control's own imp |
| `2.4.3` | pdf | **partial** | medium | untagged PDFs without a structure tree fall back to checking that pages with widgets declare /Tabs = /S |
| `2.4.3` | pptx | **partial** | high | other focus-order conditions (non-placeholder shape sequences, embedded control tab order) are not examined |
| `2.4.4` | docx | **partial** | high | whether otherwise-descriptive text actually names THIS destination — a link reading 'Annual Report' that point |
| `2.4.4` | pdf | **partial** | high | whether otherwise-descriptive text names the correct destination is a content judgement not examinable from th |
| `2.5.3` | pdf | **partial** | high | text, checkbox, radio, choice, signature fields: accessible name is flagged when it looks like a developer ide |
| `3.1.1` | html | **full** | high | whether the declared language is the CORRECT one is a content question 3.1.1 does not ask |
| `3.1.2` | docx | **partial** | high | a shorter foreign phrase or a single borrowed word is under the length floor langdetect needs to be trusted, a |
| `3.1.2` | xlsx | **partial** | medium | SpreadsheetML has no per-run language element, so shorter phrases and statistical uncertainty in langdetect's  |
| `4.1.2` | docx | **partial** | high | ActiveX controls, embedded OLE objects and other form content are not examined, which would need reading each  |
| `4.1.2` | pdf | **partial** | high | components expressed through the tagged-structure tree are not examined |
| `4.1.2` | pptx | **partial** | high | a clean result means no such controls were found and the criterion does not arise for this deck |
| `4.1.2` | xlsx | **partial** | medium | the name and role live in code that no static read can examine |

**The four Required format gaps** this file's header has tracked since the first snapshot — auto-detected for HTML, historically UNCHECKED for PDF/Office:

| Criterion | HTML | DOCX | XLSX | PPTX | PDF |
|---|---|---|---|---|---|
| `1.4.1` | pass/fail | partial | partial | partial | partial |
| `1.3.5` | pass/fail | heuristic | — | — | heuristic |
| `2.5.3` | pass/fail | — | — | — | partial |
| `4.1.2` | pass/fail | partial | partial | partial | partial |

`partial` / `heuristic` / `full` come from the registry and mean a real detector runs. `review` means a review-lane detector surfaces evidence but never certifies. `—` means no signal of any kind — the genuine remaining gap.

**Undeclared coverage** — detectors emitting for a (criterion, format) that no scope table admits. `scripts/gen_matrix_coverage.py` reports these; all known instances (`1.4.11` xlsx, `2.4.3` pdf, `4.1.2` pdf) are now declared in the registry.

**Undeclared remediation (0)** — a pair ACP assesses (a detector emits it, a review lane admits it, or the registry declares it) with no entry in `api/remediation_capability.REMEDIATION`. Registration says what the DETECTOR examines and nothing about whether a FIXER writes, so the two go stale separately. `scripts/gen_matrix_coverage.py` reports each as an explicit gap with an unknown (null) remediation ceiling rather than inferring "no remediation" from the assessment axis — the inference that hid a working PDF form-field fixer behind "No Remediation" until `4.1.2` pdf got its lane. Open: none — every assessed pair has a declared lane.

<!-- END GENERATED: coverage-status -->

All of this session's new detection code (`office_structure.py` +
`textchecks.py`) went through an adversarial correctness review (`9d7c7a3`)
that found and fixed 9 real defects — most notably the xlsx-contrast check was
reading the wrong style block on essentially every real workbook, and the
reading-level check false-fired on punctuation-free extracted text. Both are
fixed with regression tests. See P1c.

No TODO/FIXME/XXX comments, no skipped tests, and no ADR left in Proposed/Draft
status anywhere in the repo — this file is the single backlog.

---

## P0 — Known bugs

None outstanding. The two correctness bugs found this session (duplicate
findings in FileDrawer, and the 10-SC false-PASS bug where several rules had
zero backend validator at all) are both fixed and committed (`92189ce`,
`db6326c`). If you hit something new, add it here before fixing it blind.

---

## P1 — Tier 2 format-coverage gaps (scoped out of the last pass, real work)

**All four shipped** (`22a7202`, `a916068`, `5ec86b6`, and the pptx audio entry
below). Each landed with real fixtures verified before implementing — no guessed
detection logic.

This paragraph said "three of four shipped; one is genuinely blocked, not just
deferred" while all four items below were already struck through. Item 2 records
its own resolution — the `<p:timing>` blocker was answered structurally rather
than by obtaining the fixture it was waiting on — so the summary outlived the
blocker it described. A header that reports open work where there is none is the
kind of claim that ends an investigation, which is why it is worth correcting
rather than leaving as harmless pessimism.

1. ~~**PDF outline-tree analog for 2.4.1 (Bypass Blocks)**~~ — SHIPPED
   (`22a7202`). `pdf_bypass_blocks_check()` in `office_structure.py`, via
   pikepdf's `/Root/Outlines`. Only checked at 5+ pages (a short memo has no
   real bypass-blocks problem).
2. ~~**pptx embedded-audio autoplay (1.4.2 Audio Control)**~~ — SHIPPED, and
   this entry was stale for most of those 387 commits. `pptx_audio_autoplay_checks()`
   is in `office_structure.py`, dispatched from `checks_for()`'s `.pptx` branch,
   covered by `tests/test_office_structure_audio.py`, and carries an `assisted`
   remediation lane (`remediation_capability.REMEDIATION["pptx"]["1.4.2"]` — a
   one-click play-on-click card a human elects).

   The blocker recorded here was real when written: the `<p:timing>` trigger XML
   distinguishing autoplay from click-to-play could not be verified without a
   ground-truth fixture. It was resolved by reading the condition structurally —
   `<p:cond delay="0">` starts it, `evt="onClick"` does not — rather than by
   obtaining the fixture the entry was waiting on.
3. ~~**docx/pptx form-field labeling (3.3.2 / 4.1.2)**~~ — SHIPPED
   (`a916068`), docx only. `docx_checks()` flags content-control form fields
   (checkbox/date/dropdown/comboBox/picture — the unambiguous input gallery
   types) missing a `w:alias` title. Verified against 3 independent sources
   that `w:sdt` also wraps non-form content (TOC blocks, citations) that
   must NOT be flagged — pptx form controls were assessed as too rare to be
   worth a separate pass. Ships as 3.3.2 only, not 4.1.2 (4.1.2 is broader
   than just labeling and wasn't fully verified as covered).
4. ~~**xlsx contrast (1.4.3 / 1.4.6)**~~ — SHIPPED (`5ec86b6`), deliberately
   narrow. `xlsx_contrast_checks()` resolves cell font/fill color through
   `xl/styles.xml`'s cellXfs → fonts/fills chain, but ONLY direct
   `<color rgb="...">` — theme= and indexed= colors, and non-solid pattern
   fills, resolve to "unknown" and are skipped rather than guessed at (theme
   colors are exactly what Excel's built-in header/table styles use).
   Conditional-formatting (`cfRule`) overrides are out of scope entirely.

---

## P1b — AAA/optional assess gaps pulled out of HITL (`7eafe79`)

Deterministic detection for two document criteria that previously surfaced
only as manual-checklist (HITL) items — now auto-assessed, remediation still
human-only (both are inherently judgement calls to fix):

1. ~~**2.4.10 Section Headings**~~ — SHIPPED (`7eafe79`), docx.
   `docx_checks()` flags a body past a text-bearing-paragraph floor
   (`_MIN_PARAS_FOR_HEADINGS`) with zero heading styles. Short letters/memos
   below the floor are not flagged.
2. ~~**3.1.5 Reading Level**~~ — SHIPPED (`7eafe79`), all formats via
   `textchecks.py`. Flesch-Kincaid grade level over extracted text, flagged
   only well above the SC's grade-9 floor (mid-college+) so it stays
   actionable rather than firing on ordinary professional prose. No new deps
   (heuristic syllable count).

3. ~~**1.4.8 Visual Presentation**~~ — SHIPPED (`eb542d4`), docx. `docx_checks()`
   flags blocks of body text set justified (both margins), an explicit 1.4.8
   failure, past a `_MIN_JUSTIFIED_PARAS` floor. Narrow by design (justified
   alignment only, not the SC's full width/spacing/colour surface) — same
   honest-partial posture as xlsx-contrast and 3.3.2.

Remaining automatable AAA/optional candidate, not yet built (lowest value):
3.1.3 Unusual Words — needs Ollama-assisted glossary detection, i.e.
non-deterministic, which cuts against the compliance-tool principle that
identical input must give identical findings; left unbuilt on purpose.
Everything else doc-applicable is genuine human-judgment (correctly HITL) or
web-only (N/A).

The one deterministic *remediate*-side candidate worth considering: PDF
bookmark-outline auto-generation for 2.4.1 (we already *detect* the gap). NOTE
(revised): a *meaningful* outline needs to know what the headings ARE, which
for an untagged PDF means font-size/style heuristics — that's judgement-laden
and risks emitting garbage bookmarks. Not a clean deterministic fix; only worth
doing for already-tagged PDFs (rare). Left as a decision, with that caveat.

---

## P1c — Correctness hardening of this session's detection code (`9d7c7a3`)

An adversarial review of every new check found **9 real defects** (all
reproduced, all fixed with regression tests) before the code went live. The
two that mattered most:
- **xlsx contrast read the wrong style block.** A cell's `s="N"` indexes only
  `<cellXfs>`, but the regexes matched every `<xf>`/`<font>`/`<fill>` in
  styles.xml — including `<cellStyleXfs>` and the `<dxfs>` conditional-format
  differentials — shifting every index. It mis-read colour on essentially every
  real workbook. Now scoped to each real container block.
- **3.1.5 reading level false-fired on punctuation-free text.** Bulleted/table
  extraction with no terminal punctuation collapsed to one "sentence" and the
  FK grade exploded (a 3rd-grade bullet list scored 86). Newlines now count as
  sentence boundaries; unpunctuated blobs decline to score.
The other 7 (dedup-link false positive on shared URLs, PDF contrast 40-char cap
+ grayscale/CMYK colour handling, pptx title `idx=`/paired-tag forms, `<a:t>`
`xml:space`, docx regex whitespace) are all fixed too. This is the kind of bug
the honest-detection bar exists to catch — worth the review pass before any
unsupervised live window.

---

## P1d — Deva's FINAL ask vs what ACP delivers

Source: the WCAG matrix (`jeremyyuAWS/wcag-matrix`), which scores every cell against **Deva's
FINAL tab** rather than against our own rubric ceiling. As of matrix build `2026.07.29.0031`:
**78.4% of his ask today**, 42 of 94 cells meeting it, 36 short, 30 of those short by more
than the rubric says any tool can deliver, and 16 asking for no automated work at all.

### What the first version of this section got wrong

It listed **10 cells with "real engineering headroom"**. Going to build them found that
**nine already had shipped code behind them** — the matrix was understating ACP, and this
roadmap inherited the error and turned it into a work plan. Corrected in `wcag-matrix#14`,
verified by running the real detectors and proposers against built fixtures:

| Cell | matrix said | actually | observed |
|---|---|---|---|
| 2.4.6 assess XLSX | Human Required | Potential Issue | `XLSX_DEFAULT_LABELS`, 3 default sheet tabs |
| 2.4.6 assess PDF | Human Required | Potential Issue | `PDF_NO_HEADINGS`, tagged 8-page file |
| 2.4.6 fix PDF | No Remediation | AI Generated Fix | heading map from the font hierarchy |
| 2.4.4 assess PDF | Human Required | Potential Issue | `PDF_LINK_RAW_URL` |
| 2.4.4 fix PDF | No Remediation | AI Generated Fix | proposal with AI **off** |
| 3.1.2 fix XLSX/PPTX/PDF | No Remediation | Guided Remediation | 2 proposals each |
| 1.4.3 fix PDF | No Remediation | **Automatically Fixed** | re-scan clean after the fix |

Two things worth keeping from that:

* **`remediation_capability.py` and `RULE_FORMATS` were right all along** and the matrix was
  wrong. Ground rule 4 says a catalog entry is not evidence that code runs; the converse also
  holds, and this is the case that proved it — a matrix cell is not evidence that it doesn't.
* **1.4.3 on PDF** was recorded as No Remediation while `_fix_pdf_text_contrast`
  (`api/remediate_pdf.py:97`) had been deterministically rewriting text fill-colour operators
  in the content stream the whole time. Its rubric CEILING was wrong too, on a rationale
  ("surgery on the page") that shipping code contradicts.

**Correction, 2026-07-28 — the 1.4.3-fix-PDF row above was right for the wrong reason.** The
"re-scan clean after the fix" evidence was a fixture with a **white background**, and both
sides of that round trip shared one defect: the detector never computed a contrast ratio (it
thresholded the declared text colour's luma and ignored the background), and the fixer
inherited the same assumption and darkened anything above the floor. On a white page the two
errors cancel. Off it they compound — a dark-theme document's white-on-black text measures
21:1, and the fixer was rewriting it to #666666 at 3.66:1, an **AA failure it created**,
unattended. The mirror miss was as bad: #4C4C4C on #262626 is 1.76:1 and fired nothing, and
the whole muted-body-text band (#787878 at 4.42:1 through #9E9E9E at 2.68:1) failed AA
without the AA rule ever firing.

Both halves now resolve the background structurally — the topmost filled rect containing the
glyph, else the page default (`office_structure._pdf_char_background`), no render, the same
class of rect-fill resolution `pdf_nontext_contrast_checks` already did. The claim that
changed shape is the remediation one: **"Automatically Fixed" is now an honest PARTIAL**. The
fixer abstains where structure genuinely can't answer — text over an image, or one colour
painted on backgrounds that pull opposite ways — and what it abstains on stays a finding and
routes to a human. Ground rule 3's honest-partial rule is what keeps the cell at auto; the
old cell was not partial, it was *wrong*, and it shipped damage. The capability fixture
(`tests/test_remediation_capability.py`) now leads with a dark cover page, so the round trip
proves what the fixer leaves alone as well as what it clears — a clean re-scan alone never
could.

**Correction, 2026-07-28 — the 2.4.4-fix-PDF row above claimed a fix path that could not
complete.** "Proposal with AI off" was true and beside the point: the proposal was *emitted*,
and it could never be *applied*. Its locator was the raw URI, `apply_pdf_approved` routes only
`pdf:fig:` and `pdf:field:`, and `handlers._apply_approved_values` returns early for any
extension outside `_OFFICE_ALT_MIME` (docx/pptx/xlsx). Approving the card returned
`applied=[]`, `unresolved=[the URI]`, the bytes unchanged, and a re-scan still reported
`PDF_LINK_RAW_URL` — the finding merely looked handled. It was also a trap: an approved row
holding a locator + value is counted by `store.count_unapplied_approved_values` until something
writes it, so the document could never certify and never reached Publish.

Building the writer was the alternative and it is the one the proposer's own docstring ruled
out — the visible label is drawn by text-showing operators, so replacing it re-flows the page's
glyph widths. That is re-authoring, not remediation (ADR 0016), and the same reason 1.3.1
re-tagging is not auto-written.

So the cell is **explain-only**: `REMEDIATION["pdf"]["2.4.4"]` is now `human`, the proposer and
its enqueue are gone, and the residual FAIL reaches the reviewer as a plain 2.4.4 judgement row
carrying no unwritten value — honest about what ACP can do, and unlike the card, resolvable.
Note what did NOT move: the derived ceiling was already `M`, because
`gen_matrix_coverage.load_appliers` had caught the missing write-back and demoted the cell on
its own. That check worked; the lane table was the thing lying, and `/capability` and the
frontend fallback read the lane table directly, with no applier check in front of them.
Assessment is untouched (`Q`) — the detector still fires. Regression:
`tests/test_pdf_link_purpose_explain_only.py`.

### P1d-1 — SHIPPED

1. **2.4.6 Headings and Labels · fix · XLSX** — `propose_xlsx_labels` drafts AI names for
   default sheet tabs and table columns (verified via mocks; `_mock_ai` pattern in
   `tests/test_propose_xlsx_labels.py`). The previous gap: proposals were enqueued but never
   written back — there was no applier, so approved labels sat in the DB and the file stayed
   uncertified. Closed by `api/apply_xlsx_labels.py` + `store.approved_structure_label_values`
   + a new lane in `handlers._apply_approved_values` (PR todo). Capability matrix already said
   ASSISTED; now the write-back makes that claim true. Tests: `tests/test_apply_xlsx_labels.py`
   (9 tests: sheet rename, table column rename, both combined, XML escaping, corruption guard).

### P1d-2 — 29 cells at our ceiling: a decision, not engineering

Up from 21, because the nine corrections above rose to their ceilings and are now at-ceiling
*and still short of his ask*. No amount of building moves them.

Almost all are the same disagreement, stated 29 times:

* **AI Generated Fix where he asked for Automatically Fixed** — 1.1.1 fix (all four), 2.4.4
  fix (docx/xlsx/pptx — PDF dropped to Guided Remediation on 2026-07-28, see the correction
  above), 2.4.6 fix (PDF), 4.1.2 fix (PDF), 1.3.1 fix (PDF).
* **Potential Issue where he asked for Fully Assessed** — 2.4.6, 3.1.2 and 2.4.4 assess (all
  four formats each), 1.4.3 assess (PDF).
* **Guided where he asked for AI** — 3.1.2 fix (all four), 1.3.2 fix (PDF).

Stated once: **he is asking for determinism where our rubric puts an LLM in the decision
path.** That cap is a standing ground rule, not a missing feature — we do not certify a pass
on a generated judgement.

Two honest resolutions, both decisions:

1. **The ceiling is right → renegotiate the ask.** "Automatically Fixed" for 1.1.1 means
   writing generated alt text without review. We can; we will not call it a pass.
2. **The ceiling is wrong → revisit the rubric.** A tier is a human judgement (ground rules 2
   and 3), not physics — and 1.4.3 on PDF is now the proof that a ceiling here can simply be
   too conservative. If a case can be made per format, the rubric cell should change.

**Owner: not engineering.** Route to P2.

### Keeping this section true

Counts are a snapshot of matrix build `2026.07.29.0031` and will drift the moment either side
moves. Authored, not generated — the input is a customer's spreadsheet, not our code. The
first version of this section was written from the matrix without checking the matrix against
the code, which is exactly how it came to describe nine pieces of work that did not exist.
Re-derive from `targetProgress()` on the matrix page, and spot-check against a fixture, before
quoting any of it.

---

## P1e — PRD Phase 3 incremental sync: content_type is lost on every delta-sync reconstruction

**FIXED 2026-08-30**, same day it was tracked. Both halves landed:

1. `store.latest_scan_inventory_items` now selects `content_type`.
2. `scanner._sp_file_from_inventory_row` puts the stored value on the reconstructed raw item
   under `_acp_content_type`, and `_sp_classify_item` restamps it onto the scannable record.
   The private, `_acp_`-prefixed key keeps the dict readable as a faithful raw driveItem —
   a Graph-shaped name would leave a reader unable to tell which fields Graph sent. A live
   Graph item never carries it, so a fresh listing classifies byte-identically, which
   `_sp_classify_item`'s "shared VERBATIM" contract requires.

A file the delta reports as CHANGED still has none, and that is correct rather than a
remaining gap: `apply_sp_delta` replaces a changed id wholly with its fresh raw item, and the
delta's metadata is the authority for a file it says changed. `tests/test_sp_delta_content_type.py`
pins that alongside the carry-forward, so it is not later "fixed" by merging field-by-field.

One correction to the analysis below, worth keeping because it is the reason this survived:
`_sp_file_from_inventory_row`'s docstring asserted that `content_type` "can NEVER be
reconstructed: it is never persisted to scan_inventory". That premise was false — the column
is real and `add_inventory` populates it. The symptom (absent from the baseline) had been read
as its cause (never stored), and the note then made the loss look designed. Only item (1) was
ever actually missing; the cost argument in that note was right and still holds.

The original entry follows.

---

**Not fixed, tracked here (2026-08-30).** SharePoint's per-item Content Type
(`scanner._sp_enrich_content_types`, a best-effort per-item Graph call) is genuinely persisted to
`scan_inventory.content_type` by `store.add_inventory` — but is lost by the time a delta-sync
reconstruction (`core._sp_sync_plan` / `_interactive_sp_sync_plan`) would need it, for two
independent reasons, not one:

1. `store.latest_scan_inventory_items` — the query that reads back a prior scan's inventory as a
   reconstruction baseline — does not `SELECT content_type` at all. An easy, low-risk oversight:
   the column is already in the table and already populated; the SELECT list just never grew to
   include it.
2. Even fixing (1), `scanner._sp_file_from_inventory_row` (which turns a persisted row back into
   a raw-item shape) and `sp_reconstructed_listing`'s call into `_sp_classify_item` have nowhere
   to put a carried-forward `content_type` back onto the reconstructed, classified item —
   `_sp_enrich_content_types` is a live-listing-only post-processing step, and
   `sp_reconstructed_listing` deliberately never makes the live per-item Graph call that feeds
   it (see `_sp_file_from_inventory_row`'s own docstring) — doing so for every carried-forward
   file would spend exactly the cost delta-sync exists to avoid.

So the real fix is (1) plus threading the OLD content_type value through for files NOT in the
delta's `changed` set (a genuinely unchanged file's content type is still valid) — (2) alone,
without (1), is not fixable. Left as a known gap rather than fixed here because it touches the
reconstruction pipeline's shape (`apply_sp_delta` / `_sp_classify_item`'s call site), which is
more than this pass's scope. Low severity: unaffected are Drive (no content-type concept),
every fresh (non-reconstructed) SharePoint listing, and every rule that doesn't key off
Content Type.

---

## LOE — remaining work to full functionality

Principle (owner's call): anything not auto-remediable by a deterministic or AI
fixer **routes to HITL** — so no new auto-fixers are required for the long tail;
non-mechanical findings already surface as HUMAN-tier and go to the review queue.
Route legend: **Auto** (deterministic/AI fix) · **HITL** (human review) · **Ops**
(credential/config) · **Decision**.

| # | Item | Verb | Route | LOE | Notes |
|---|---|---|---|---|---|
| 1 | ~~Fix live "Couldn't remediate this file"~~ | Remediate | Auto | ~~M · 1–2 d~~ **verified stale 2026-08-12** | The 2026-07 diagnosis no longer holds and the failure does not reproduce (checked against `handlers.py` + live `acp-worker` logs). Both blamed causes are already fixed: `DRIVE_SCOPES` grants `drive.file` (write), and the Blob copy is written FIRST and UNCONDITIONALLY (`handlers.py:617`, ADR 0010) while the Drive-mirror 403 is CAUGHT (`handlers.py:679`) — so a write-back denial never fails the job; the fixed file is in Blob regardless, and the mirror succeeds in live logs. `"Couldn't remediate"` (`FileDrawer.jsx:916`) only fires on a genuine job error. The one residual path — an expired GIS token before a queued job runs — is already mitigated: the token rides the durable job payload (`handlers.py:448`). Pinned by `tests/test_remediate_token_resolution.py`. **No engineering left here.** |
| ~~2~~ | ~~Complete the DB-backed HITL queue~~ | Remediate | HITL | **DONE** | DB persistence, assignment/claiming, terminal status, immutable decisions, queue retraction, events, webhook notifications, and reload recovery are shipped and covered by the `test_hitl_*` suites. The final audit also closed owner isolation on every item-by-ID and scan queueing route; opaque queue IDs are no longer treated as authorization. |
| 3 | 3.1.3 Unusual Words | Assess | HITL | ~~XS~~ DONE | Re-tagged Human / AT · Tier 3 HITL in `wcagCatalog.js` (was aspirational "Automated + Agentic"). No AI check built, by decision. |
| 4 | ~~1.4.2 pptx audio autoplay~~ | Assess | — | DONE | Detection shipped (`pptx_audio_autoplay_checks`, dispatched, tested) with an `assisted` remediation lane — no longer blocked on a fixture, and no longer HITL-only. See P1 #2. |
| ~~5~~ | ~~Deploy the mislabel fix (`e83d775`)~~ | Release | — | ~~XS · 0.25 d~~ **DONE** | `AUTO_FIX_SC_BY_TYPE` + `scId()` normalisation already in `sim.js` on `origin/main`; Netlify auto-deployed on merge. Regression guard added: 8 `recommendFor` tests in `simRemediation.test.js` pin auto/assisted routing for both SC_-prefixed and axe-form finding IDs across PDF, HTML, and format-boundary cases. |
| 6 | Drive credential + folder | Ops | Ops | S · 0.5 d | Regenerate the demo SA key with `drive.readonly`, share Deva's folder with the SA email, set `ACP_DRIVE_FOLDER`. Unblocks demo Drive scans (the 403 below) and closes P2 #1. Ops, not eng. |
| 7 | Measure Ollama 8B latency | Verify | — | XS · 0.25 d | Needs live access; include a cold-start number (scale-to-zero). |
| 8 | ADO review cadence | — | Decision | 0 d | Standing reviewer vs. bypass-as-needed. |

**Real engineering build left ≈ near zero.** The old estimate was "≈ 2–3.5
person-days, almost all of it item #1" — but #1 was verified stale on 2026-08-12
(see above): the failure it describes is already fixed and does not reproduce.
With #1 removed, item #2 is T's, #3 is done, #4–7 are hours (mostly Ops/verify),
and #8 is a decision. Nothing substantial remains for engineering to build.

**Two mislabel/UX fixes already landed this session:** the "fully automatic"
misclassification (`e83d775`, `sim.js` — real-vs-sim wcag format mismatch +
format-aware auto set) and the 9 detection-code defects (`9d7c7a3`).

### Live Drive-flow finding (`403 insufficient scopes`)
A headless smoke test of `POST /scans?source=drive` (via the E2E + demo keys)
authenticated and reached Drive, then failed at `files.list` with Google
`403 "Request had insufficient authentication scopes"`. The code correctly
requests `drive.readonly` (`scanner.py` `SCOPES`), so the stored demo ADC
credential can't obtain that scope — a credential/config issue, not a code bug.
Fixed by item #6. (Real usage via signed-in users' own Drive tokens is
unaffected; only the demo/ADC path is broken.)

---

## P2 — Decisions pending on the user (not blocked on engineering)

1. **Point `ACP_DRIVE_FOLDER` at Deva's folder** for scheduled sweeps —
   waiting on the folder ID.
2. **Measure real Ollama `llama3.1:8b` latency** — still open, and it turns out
   this can't be done from a headless/CI context: `acp-ollama` has
   `external:false` ingress (internal-only) and scales to zero, and the app's
   digest endpoint is behind Google SSO. To get the number: sign in to the live
   app and time a compliance-digest generation, OR hand me the E2E test key so a
   headless request can bypass SSO. Container config: 4 CPU / 8Gi, CPU-only,
   scale-to-zero — so the first request pays a cold model-load of the ~4.7GB 8B
   weights on top of CPU inference (expect tens of seconds cold, less warm —
   estimate, NOT a measurement).
3. **ADO review-cadence** — standing reviewer vs. bypass-as-needed for future
   PRs. Bypass-policies permission is already granted; this is a process
   choice, not a technical one.

---

## P3 — In-flight in another concurrent session — do not touch

> **Almost certainly resolved — verify before acting on anything here.** This
> section described *uncommitted* work in a shared checkout as of `9d7c7a3`,
> 387 commits and 19 days ago. Uncommitted work does not survive that; either it
> landed or it was lost. Kept verbatim rather than deleted because it names the
> files that were contested, which is useful history if a conflict shows up —
> but treat every "do not touch" below as expired until re-confirmed.

A second autonomous session ("T") has uncommitted work sharing this same
checkout as of this snapshot. This is **not this backlog's to implement** —
listed here purely so nobody mistakes it for abandoned/stale work and reverts
it:

- Publish rewrite + per-file rescore (`api/handlers.py`, `api/routes/scans.py`,
  `api/store.py` — `record_publish`, `refresh_scan_aggregate`,
  `get_file_record`)
- DB-backed HITL queue touching several frontend views (`App.jsx`,
  `Dashboard.jsx`, `EmptyState.jsx`, `FileDrawer.jsx`, `Overview.jsx`,
  `Publish.jsx`, `Remediate.jsx`, `api.js`)
- Test-corpus reorg — deleting the 100 files under `test-corpus/files/` in
  favor of a new `test-corpus/bulk-200/` + `test-corpus/Legal sample files/`
  layout

If you're picking up this backlog cold and `git status` still shows these
paths modified, **stash them (`git stash push -u -m "..."`) before touching
`api/store.py` or any of the frontend files above** — don't edit over a
stash, and don't commit paths you didn't intend to touch. See the isolation-
dance pattern used for the `RULE_FORMATS` change in `16581bd` if `store.py`
needs another edit while T's WIP is still live: edit the working copy
in-place first, then build a separate clean-`HEAD`-based copy (same edits,
`assert count==1` string replace) to actually stage and commit, then restore
the mixed working copy afterward.

**Gotcha hit during the P1 work (`a916068`):** the "save the mixed copy"
step must happen *after* editing the working copy in place, not before —
saving it first and editing second means the later restore silently reverts
your own edit from the working tree even though the commit itself is fine
(git history stays correct; only the local file drifts). `test_rule_formats.py`
caught this immediately on the next isolation dance because the derived
truth no longer matched `RULE_FORMATS` — that's exactly the kind of drift
the contract test exists to catch, but don't rely on it; get the copy order
right: **edit in place → save mixed copy → build clean-HEAD copy → commit →
restore mixed copy**.

---

## P4 — Certification report as an audit artifact (report presentation)

Theme (external feedback + review, 2026-07-09): the report is a solid *scan
summary* but not yet persuasive to the three audiences that matter —
**executives** ("are we compliant?"), **auditors** ("can I trust this?"),
**remediation teams** ("what exactly changed?"). It states *what* happened but
under-proves *why to trust it*. Most of the data already exists; the work is
**consolidating it into one downloadable per-document certificate**, not new
detection.

**Reconciled 2026-08-19 against `api/report.py`.** Most of this section has since shipped —
the flagship **R1** and quick-wins **R2 / R3 / R4 / R6 / R7 / R8** plus **R-A / R-B**, each struck
below with its rendering code named. The report is no longer "a scan summary": it carries the
certification-decision block, per-issue evidence appendix with sign-off, scope-of-assertion,
chain-of-custody digest, richer inventory, and the POUR breakdown. The genuine remainder is the
honesty-gated KPI/assurance work (**R9 / R10**), and the remaining dependency-bearing item (**R-E**). ~~R15~~ and ~~R-D~~ have since shipped (PRs #689, #698, #704, #711).

**Hard rule for this whole section (ADR 0016 / `4fc6bc1`):** every number is a
real, derivable ratio shown with its basis, or it is omitted. NO fabricated
percentages ("96% effort saved", "AI 92%", invented "4.2s avg"). A fabricated
figure on an evidence document is spotted instantly by an auditor and *lowers*
trust — the honesty discipline is a feature to surface, not a gap to paper over.

Where the data already lives (so these are consolidation, not net-new):
`store.get_remediation_diffs` (before→after, `remediation_diff`),
`store.list_applied_fixes` (real AI values + thumbnails, `applied_fixes`),
`hitl_queue` (approvals, `approved_value`, `proposals`, `validated`),
`decision_log` (immutable who/what/when), `confidence.js` (evidence-based
High/Med/Low + basis), the stamped rubric hash + `RULE_FORMATS`/manifest
(what actually ran), and `api/report.py` (the estate PDF to extend, or add a
per-document certificate renderer alongside it).

### Flagship
| # | Item | Value | Effort | Notes |
|---|---|---|---|---|
| ~~R1~~ | ~~**Per-issue Remediation Evidence Portfolio**~~ — **SHIPPED**: `_evidence_section` ("the audit artifact", backlog R1) in `report.py` renders one mini-section per finding — before→after, source, and the immutable sign-off ("what changed, and on whose authority"). Applied-and-verified fixes are kept strictly apart from proposals awaiting approval. | Highest — turns summary → audit artifact | ~~M · 2–3 d~~ DONE | All data exists (`remediation_diff` + `applied_fixes` + `hitl_queue` + `decision_log`). |

### Quick wins — data exists, mostly presentation (each XS–S)
| # | Item | Notes |
|---|---|---|
| ~~R2~~ | ~~**Certification-decision block**~~ — **SHIPPED** | `_decision_block` (backlog R2/R3), rendered first ("Certification decision (R2)"). Carries a **real** recomputable SHA-256 content digest (`_content_digest`), never decorative. |
| ~~R3~~ | ~~**Why-certifiable prose**~~ — **SHIPPED** | The plain-language WHY is the executive verdict rendered under the decision block (`_decision_block` docstring, R2/R3); status labels reframed away from a bare "100%". |
| ~~R4~~ | ~~**Certification-metadata / chain-of-custody**~~ — **SHIPPED** | Recomputable SHA-256 content digest (`_content_digest`: scan id, rubric hash, target, per-file scores) + the "Scope & methodology" section; "results are reproducible from the stamped rubric hash." |
| ~~R5~~ | ~~**AI reasoning inline**~~ — **SHIPPED** (#722) | `_evidence_section`: `source` field (OCR-grounding / model provenance) and `why_review` rendered for proposed fixes; assurance-tier badge (Deterministic / AI+human / AI+re-scan) on every applied fix. |
| ~~R6~~ | ~~**Richer file inventory**~~ — **SHIPPED** | "File inventory (R6)" table now carries File · Type · Extent · Status · Score · Findings · **Fixed · Open · Approvals** per document. |
| ~~R7~~ | ~~**Explain the score**~~ — **SHIPPED** | The scope section states it explicitly — "A score of 100 therefore means: no blocking findings among the criteria ACP evaluated … not a statement that the document conforms to WCAG 2.1 AA." |
| ~~R8~~ | ~~**POUR breakdown**~~ — **SHIPPED** (#496) | `_pour_section` renders the per-principle pass rate among evaluated criteria; the four principles partition the evaluated set exactly (pinned by test). Deterministic → honest by construction. |

### Honesty-gated — agree only if computed from real data
| # | Item | Guardrail |
|---|---|---|
| ~~R9~~ | ~~**Human-review KPI block**~~ — **SHIPPED** | `_assurance_section`: reviewed / approved / rejected / remediated band from immutable `decision_log`; effort as fixes-cleared ÷ findings (basis named, no modelled saving); edited-draft count and avg review time (only when real timestamps) from `hitl_analytics`. |
| ~~R10~~ | ~~**Assurance/confidence bars**~~ — **SHIPPED** | `_mode_bar()`: stacked horizontal bar showing deterministic ÷ AI-assisted ÷ human-only split of evaluated criteria, with legend. Prose names all three modes with real counts and percentages; "fixed ÷ attempted" omitted (attempted not tracked, per ADR 0016). |
| ~~R11~~ | ~~**"How ACP reached this decision" methodology**~~ — **SHIPPED** | `_provenance_section` (R11): evaluated/auto/ai counts from real scan facts; method narrative names the deterministic engine, AI-assisted review, revalidation re-scan, and human approval gate. |
| ~~R12~~ | ~~**Compliance timeline**~~ — **SHIPPED** | `_provenance_section` (R12): pipeline rendered as `scanned N → evaluated N → N finding(s) → N AI-assisted → N approval(s) → N remediated & re-validated → N/N certifiable`; each count from scan facts. |

### Larger / has a dependency
| # | Item | Notes |
|---|---|---|
| ~~R13~~ | ~~**Manual-verification instructions**~~ — **SHIPPED** | `_manual_verification_section` (R13): per-format table (DOCX/PPTX/XLSX/PDF) with mainstream tool + generic checks; rendered only for formats present in the scan. Wired at `build_report()`. |
| ~~R14~~ | ~~**Per-criterion evidence-of-compliance rows**~~ — **SHIPPED (PR #665)** | `_criterion_table_section`: lists only criteria with open or cleared findings; columns: Criterion · Severity · Rule · Docs affected · Status. |
| ~~R15~~ | ~~**QR code → immutable online report**~~ — **SHIPPED (PR #689)** | `_verify_section` + `_qr_flowable` in `report.py`: SHA-256 content digest (scan payload canonical JSON) printed as hex + QR code side-by-side in the PDF; `/public/verify/{scan_id}` endpoint (unauthenticated) recomputes and returns the same digest so any holder can confirm the report is unaltered. `ACP_PUBLIC_URL` controls the URL embedded in the QR. Remaining: hosted immutable artifact, per-document version chain (`R-E`). |

### My additions (review, 2026-07-09) — weighted toward *auditor* trust
| # | Item | Why |
|---|---|---|
| ~~R-A~~ | ~~**Scope-of-assertion / negative-assurance statement**~~ — **SHIPPED** | `_scope_section` ("What this report covers · and what it does not", R-A) states validator-set size vs the full 87, per-document evaluated / not-evaluated / by-mode, the criteria never run, the file types never opened, and the whole-estate funnel — the over-claim guard against a "100%" misread. |
| ~~R-B~~ | ~~**Immutable audit-log excerpt**~~ — **SHIPPED** | The evidence appendix renders each fix's sign-off inline from the immutable `decision_log` — "{decision} by {reviewer} · {when} UTC", with the approved value — under "what changed, and on whose authority". |
| ~~R-C~~ | ~~**Per-fix assurance-level disclosure**~~ — **SHIPPED (PR #665)** | `_evidence_section` now renders a colored tier badge per fix: Deterministic / Deterministic+human / AI+human / AI+re-scan. |
| ~~R-D~~ | ~~**Reproduce-this-result instructions**~~ — **SHIPPED (#698 / #704)** | `_provenance_section` renders a bordered 3-step table: verify rubric hash at `GET /rubric`, re-run via `POST /scans?source=…`, compare findings. `ACP_PUBLIC_URL` prefixes the URLs; source param taken from `run["source"]`. |
| ~~R-E~~ | ~~**"Supersedes" lineage**~~ — **SHIPPED** | `_supersedes_section` in `report.py`: renders "This report supersedes `<id>` from `<date>`" when `run["previous_scan_id"]` is set. Verified by `test_supersedes_renders_only_with_a_previous_scan` and `test_supersedes_includes_scan_id_when_available` in `tests/test_report_provenance.py`. |

Sequencing suggestion: R1 (flagship) + R2/R3/R6 + R-A first (they land the biggest
trust jump on data that already exists), then R4/R5/R-B/R-C (~~R11/R12 done~~), then the
honesty-gated KPI/bars (R9/R10) once the real ratios are wired, then ~~R15~~/ ~~R-D~~ / ~~R-E~~ (~~R13/R14/R-C done~~).

### Polish / technical debt (surfaced during R15 implementation, 2026-08-24)

**All P-1–P-8 shipped** (verified against `origin/main` 2026-08-24). **P-9–P-12 shipped** (#725). **P-13 shipped** (this PR). **P-14 shipped** (#750). **P-15 shipped** (#748). **P-16 shipped** (#742). **P-17 shipped** (#752). **P-18 shipped** (#744). **P-19 shipped** (this PR). **P-20 shipped** (this PR).

**Sequencing (2026-08-25):** ~~P-13~~/~~P-16~~/~~P-18~~/~~P-20~~ → R9/R10 w/ ~~P-15~~ → ~~P-14~~/~~P-17~~ → ~~R-E~~ → ~~P-19~~/presentation.

| # | Location | Item |
|---|---|---|
| ~~P-1~~ | `api/report.py` | ~~`_MANUAL_VERIFY` dict has no `"html"` key — an HTML scan silently skips the manual-verification table.~~ **DONE** (#699): `"HTML"` key added with axe DevTools instructions. |
| ~~P-2~~ | `api/report.py` | ~~`_esc()` silently truncates strings to 400 chars.~~ **DONE**: limit raised to 2000 with an ellipsis on overflow. |
| ~~P-3~~ | `api/report.py` | ~~Evidence truncation note says "full evidence available via API" — but no such endpoint exists.~~ **DONE**: false claim removed. |
| ~~P-4~~ | `api/report.py` | ~~`_ai_governance_section` has a bare `except Exception: pass` that hides every failure silently.~~ **DONE** (#699): now logs with `_LOG.warning(..., exc_info=True)`. |
| ~~P-5~~ | `api/report.py` | ~~`_decision_block` docstring still tags R2/R3 as "backlog".~~ **DONE**: docstring updated. |
| ~~P-6~~ | `api/blob.py` | ~~`BlobStore.put()` uses `overwrite=True` unconditionally.~~ **DONE** (#706): gates on `overwrite=False` with collision logging. |
| ~~P-7~~ | `api/report.py` | ~~Score denominator is undisclosed.~~ **DONE**: scope section explicitly states separate denominators (discovered / assessable / scored) and caps the meaning of a 100 score. |
| ~~P-8~~ | deployment docs | ~~`ACP_PUBLIC_URL` must be documented.~~ **DONE** (#711): added to `.env.example` and `docs/production-hardening.md` with QR-code and rubric-sensitivity notes. |
| ~~P-9~~ | `api/report.py` | ~~Partial-assessment caveat buried in the verdict paragraph — a reader skimming for a percentage can miss it.~~ **DONE**: stand-alone highlighted notice when `unassessed > 0` or `unanalysable > 0`. |
| ~~P-10~~ | `api/report.py` | ~~Stat band denominator `cert / total` includes unassessed files, overstating coverage.~~ **DONE**: denominator changed to `assessed` (total − unassessed); label says "N of M assessed". |
| ~~P-11~~ | `api/report.py` | ~~No criteria-level outcome breakdown — auditors see file counts but not WCAG criterion outcomes.~~ **DONE**: second stat band row from `facts["scope"]`: passed / with findings / human-review / not-evaluated. |
| ~~P-12~~ | `api/report.py` | ~~No assessment scope declaration at the top of the report.~~ **DONE**: 3×4 table (source / scan window / file types / method / standard+target / rubric) replaces old "Scope & methodology" card. |
| ~~P-13~~ | `api/report.py` | ~~**Add a limitations & exceptions section**~~ — **DONE**: `_limitations_section()` renders a PLUM-bordered notice only when real limitations exist: unanalysable docs named individually, review-recommended criteria by SC id+name, absent owner/author metadata. Positioned before "Outcome summary". 9 tests in `tests/test_report_limitations_p13.py`. |
| ~~P-14~~ | ~~`api/report.py`, `api/store.py`~~ | ~~**Use stable finding identifiers.**~~ — **SHIPPED** | `_finding_id(file, criterion, location)`: SHA-256[:8] hex ID stable across renders, exports and re-assessments; exposed as `FND-{id}` in the evidence appendix heading for every applied and proposed finding. |
| ~~P-15~~ | `api/report.py` | ~~**Clarify finding status and history.**~~ **DONE**: `_finding_status(issue, file_is_certifiable)` derives one of seven named states; file inventory "Findings" cell shows per-finding breakdown (21 tests in `test_report_finding_status_p15.py`). |
| ~~P-16~~ | `api/report.py` | ~~**Add report provenance and freshness.**~~ **SHIPPED (#742)**: `_provenance_section` renders report-generated timestamp, assessment-completion, scan ID, rubric hash, pipeline summary, and reproduce instructions. |
| ~~P-17~~ | `api/report.py` | ~~**Improve evidence presentation.**~~ **SHIPPED (#752)**: location row, redaction/truncation for Before/After, Expected field, Confidence, Collected-at timestamp; `has_decision` guard fixes `KeyError` when `decision` key absent. |
| ~~P-18~~ | `api/report.py` | ~~**Report-level reconciliation checks before rendering.**~~ **DONE**: `_reconciliation_checks()` validates rubric hash presence, orphan facts documents, catalog size, review arithmetic, and remediated_total; RED-bordered warning box rendered when any check fails (9 tests in `test_report_reconciliation_p18.py`). |
| ~~P-19~~ | `api/report.py` | ~~**Print/PDF/AT behaviour.**~~ **SHIPPED (this PR)**: `_make_page_callback` factory draws page header (scan ID + report date) and footer (page number) via `onFirstPage`/`onLaterPages`; `topMargin` raised to 0.85 in; `repeatRows=1` on POUR, remediation-outcomes, open-findings-by-criterion, and file-inventory tables; donut+severity block wrapped in `KeepTogether`; `KeepTogether` imported. |
| ~~P-20~~ | `api/report.py` | ~~**Remove ambiguous assurance language.**~~ **DONE**: POUR section renamed "No-failure rate by WCAG principle"; table columns "Passed"→"No failures", "Pass rate"→"No-failure rate"; file inventory Findings cell "clean"→"no findings". Docstring + prose updated to match. |

---

## Reference — where things live

- `api/office_structure.py` — first-party docx/pptx/pdf/xlsx structural
  checks (2.4.6, 2.4.9, 1.4.3, 1.4.6, 2.4.1, 3.3.2); dispatcher is
  `checks_for(path, ext)`.
- `api/store.py` — `RULE_CATALOG` (the 87-ish rule list) + `RULE_FORMATS`
  (per-rule format applicability) + `_rule_outcome()` (PASS/FAIL/NOT_APPLICABLE).
- `tests/test_rule_formats.py` — contract test deriving `RULE_FORMATS` truth
  independently from source; extend `_derive_formats()`/`_OFFICE_STRUCT_FORMATS`
  whenever a P1 item above lands.
- `frontend/src/wcagCatalog.js` / `wcagCatalog.test.js` — the human-facing
  coverage table + its drift guard (source must be backed by a real validator,
  not just an aspirational label).
- `spike/dotnet/AcpScan.Cli/Program.cs` — the partner engine boundary; we
  cannot extend partner rules ourselves, only add first-party checks
  alongside them (the `office_structure.py` pattern).
