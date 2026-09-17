// SINGLE SOURCE OF TRUTH for the per-file WCAG certification report.
//
// This module builds a renderer-agnostic content MODEL (an ordered list of typed
// blocks) from the same coverage rows the FileDrawer shows on screen. Both the PDF
// renderer (pdfReport.js) and the HTML renderer (htmlReport.js) consume this one
// model, so the two exports can never drift: change the wording, the checklist, the
// audit trail or the conformance logic HERE and both downloads update together.
//
// A block is `{ k: <type>, ... }`. The PDF renderer maps each block onto a makeDoc
// primitive (heading/callout/bullets/text/metricGrid/donut/barChart/table); the HTML
// renderer maps each onto a semantic element. Text/callout/bullet blocks carry the
// exact option object the PDF primitive expects (size/color/bold/lh) so the PDF
// output is byte-for-byte what it was before this model was extracted — the HTML
// renderer reads the same options loosely (colour + emphasis; sizes are relative).

import {
  scOfValue, fmtOfFile, isSafeHref, normaliseRowIssues, buildChangeCards, buildFindingCards,
  buildComparison, boundList, locationLabel, technicalText, humanText, RESPONSE_NOTICE,
} from './reportEvidence.js'

// Shared ink palette. Every colour used as TEXT here is dark enough to clear WCAG
// 1.4.3 (>= 4.5:1) on the white report background — the HTML report dogfoods the
// product, so it must itself pass contrast.
export const INK = '#2B2330', MUTED = '#6B6670', LINE = '#E4E0E8', PLUM = '#4B3460',
  GREEN = '#3B6D11', AMBER = '#854F0B', RED = '#A32D2D', BLUE = '#1F5FA8'

// A4 content width in pt (595.28 page − 2×50 margin). Table column widths are
// authored against this so the PDF lays out exactly as before; the HTML renderer
// turns the same widths into proportional columns.
export const CW = 495.28


const PRINCIPLE = { 1: 'Perceivable', 2: 'Operable', 3: 'Understandable', 4: 'Robust' }
const PRIN_CLR = { 1: BLUE, 2: GREEN, 3: AMBER, 4: PLUM }

const COV_OUT_TXT = { PASS: 'Pass', FAIL: 'Open finding', FIXED: 'Fixed · re-validate', HUMAN: 'Human review', UNCHECKED: 'Not auto-checked', WEB: 'Web-only (n/a)' }

// ── Certification-report evidence maps (curated content; no fabricated data) ──
const CHANGE_LABEL = {
  '1.1.1': 'image descriptions (alt text) added', '3.1.1': 'document language declared',
  '2.4.2': 'document title set', '1.3.1': 'table headers / structure added',
  '1.4.3': 'colour contrast adjusted', '1.4.6': 'enhanced contrast adjusted',
  '2.4.6': 'headings / labels clarified', '1.3.2': 'reading order corrected',
}
const HUMAN_GUIDE = {
  '1.1.1': { why: 'AI can draft alt text but cannot confirm it conveys the image’s purpose in context.', how: ['Open each flagged image', 'Confirm the description states the image’s meaning, not just its contents'], min: 2 },
  '1.2.1': { why: 'AI cannot confirm a transcript fully conveys the audio/video content.', how: ['Play the media', 'Confirm the transcript captures all meaningful content'], min: 5 },
  '1.2.2': { why: 'AI cannot confirm captions are accurate and complete.', how: ['Play the video with captions on', 'Confirm captions match the audio and note speakers/sounds'], min: 5 },
  '1.2.3': { why: 'AI cannot confirm audio description covers the meaningful visuals.', how: ['Play the video', 'Confirm every meaningful visual event is described in narration or a text alternative'], min: 5 },
  '1.4.1': { why: 'AI detected colour-coded meaning; only a person can confirm a non-colour cue also exists.', how: ['Find where colour signals meaning (e.g. red = error)', 'Confirm a label, icon or text also communicates it'], min: 3 },
  '1.3.5': { why: 'AI cannot confirm form fields declare the right input purpose (autocomplete).', how: ['Check name/email/address fields', 'Confirm the correct autocomplete/purpose is set'], min: 3 },
  '2.1.1': { why: 'AI cannot operate the document to confirm full keyboard access.', how: ['Tab through all interactive controls', 'Confirm each is reachable and operable by keyboard alone, with no trap'], min: 3 },
  '2.5.3': { why: 'AI cannot confirm the visible label matches the name a screen reader announces.', how: ['For each labelled control, confirm the spoken name includes the visible label text'], min: 3 },
  '3.3.1': { why: 'AI cannot confirm error messages clearly identify the problem field.', how: ['Trigger a form error', 'Confirm the message names the field and the problem'], min: 2 },
  '4.1.2': { why: 'AI cannot confirm custom controls expose the right name/role/value to assistive tech.', how: ['Navigate custom controls with a screen reader', 'Confirm each announces its name, role and state'], min: 4 },
}
const DEFAULT_HUMAN = { why: 'This criterion needs human judgement that automated checks can’t provide.', how: ['Review the flagged content against the WCAG success criterion'], min: 3 }
export const VERIFY_GUIDE = {
  pptx: { app: 'PowerPoint', mac: ['PowerPoint → Review → Check Accessibility', 'Resolve every item under “Inspection Results”'], win: ['PowerPoint → Review → Check Accessibility', 'Work through the “Inspection Results” pane'], sr: ['macOS: VoiceOver (⌘F5) — arrow through each slide; confirm image descriptions, heading order and table headers are announced', 'Windows: NVDA — Tab / arrow keys; confirm reading order, headings, links and image alt text'], checks: ['Alt text on every image', 'Reading order per slide', 'Slide titles', 'Table header rows'] },
  docx: { app: 'Word', mac: ['Word → Review → Check Accessibility'], win: ['Word → Review → Check Accessibility'], sr: ['macOS: VoiceOver (⌘F5)', 'Windows: NVDA — verify heading levels, alt text, table headers and link text'], checks: ['Alt text on images', 'Heading hierarchy', 'Table header rows', 'Descriptive link text', 'Document language'] },
  xlsx: { app: 'Excel', mac: ['Excel → Review → Check Accessibility'], win: ['Excel → Review → Check Accessibility'], sr: ['Windows: NVDA — verify table headers and sheet names are announced'], checks: ['Table header rows', 'Named sheets', 'No merged cells that break navigation'] },
  pdf: { app: 'Acrobat', mac: ['Preview shows text but can’t verify tags — use Acrobat Pro', 'Acrobat Pro → Accessibility → Full Check'], win: ['Acrobat Pro → Accessibility → Full Check', 'Review the Accessibility Report'], sr: ['macOS: VoiceOver', 'Windows: NVDA / JAWS — verify tag reading order, headings, alt text and table structure'], checks: ['Tagged structure', 'Reading order', 'Alt text', 'Document language & title'] },
  html: { app: 'Browser', mac: ['Chrome/Edge → DevTools → Lighthouse → Accessibility', 'axe DevTools extension → Scan all of my page'], win: ['Chrome/Edge → Lighthouse → Accessibility', 'axe DevTools extension → Scan'], sr: ['macOS: VoiceOver (⌘F5) in Safari', 'Windows: NVDA in Firefox/Chrome — verify landmarks, headings, link purpose and form labels'], checks: ['Keyboard-only navigation', 'Colour contrast', '200% zoom / reflow', 'Screen-reader landmarks & headings'] },
}
const CAT_OF = (sc) => {
  if (sc.startsWith('1.1') || sc === '1.4.5' || sc === '1.4.9') return 'Images'
  if (sc.startsWith('1.2')) return 'Audio & Video'
  if (sc === '1.3.1' || sc === '1.3.2') return 'Tables & Structure'
  if (sc === '2.4.2' || sc === '2.4.6' || sc === '2.4.10') return 'Headings & Titles'
  if (sc === '2.4.4' || sc === '2.4.9') return 'Links'
  if (sc === '1.4.3' || sc === '1.4.6') return 'Contrast'
  if (sc.startsWith('2.1')) return 'Keyboard'
  if (sc === '3.3.1' || sc === '3.3.2' || sc === '3.3.3' || sc === '4.1.2' || sc === '1.3.5') return 'Forms'
  if (sc === '3.1.1' || sc === '3.1.2') return 'Language'
  return 'Other'
}
const CAT_ORDER = ['Images', 'Headings & Titles', 'Tables & Structure', 'Links', 'Contrast', 'Language', 'Forms', 'Keyboard', 'Audio & Video', 'Other']

// ── Evidence-truth helpers ───────────────────────────────────────────────────────────────────
//
// FIXED means "an edit was saved; re-validation not yet recorded" UNLESS the row is verified —
// a remediation_diff record exists for the criterion, and those are only written for fixes that
// cleared the post-fix re-scan (api/store.py list_remediation_diffs). A FIXED row with no such
// record is outstanding work, never a pass.
const verifiedCriteria = (diffs) => new Set((diffs || [])
  .filter((x) => x && x.verified === true).map((x) => scOfValue(x.rule_id ?? x.ruleId)).filter(Boolean))

export function classifyRows(rows = [], diffs = []) {
  const vset = verifiedCriteria(diffs)
  const isVerified = (r) => r.verified === true || vset.has(r.id)
  const outOfScope = rows.filter((r) => r.disposition?.kind === 'out_of_scope')
  const oos = new Set(outOfScope.map((r) => r.id))
  const inScope = rows.filter((r) => !oos.has(r.id))
  const by = (pred) => inScope.filter(pred)
  const c = {
    inScope, outOfScope,
    pass: by((r) => r.outcome === 'PASS'),
    fixedVerified: by((r) => r.outcome === 'FIXED' && isVerified(r)),
    fixedPending: by((r) => r.outcome === 'FIXED' && !isVerified(r)),
    fail: by((r) => r.outcome === 'FAIL'),
    human: by((r) => r.outcome === 'HUMAN' && !r.disposition),
    attested: by((r) => r.disposition?.kind === 'attested'),
    unchecked: by((r) => r.outcome === 'UNCHECKED' && !r.disposition),
    web: by((r) => r.outcome === 'WEB'),
    isVerified,
  }
  // The ONLY condition under which a report may say "no outstanding items" (contract): every
  // in-scope row is PASS, attested, or FIXED+verified, and nothing is unchecked.
  c.ready = inScope.length > 0 && c.fail.length === 0 && c.human.length === 0
    && c.fixedPending.length === 0 && c.unchecked.length === 0
    && inScope.every((r) => r.outcome === 'PASS' || r.outcome === 'WEB' || r.disposition?.kind === 'attested'
      || (r.outcome === 'FIXED' && isVerified(r)))
  return c
}
export const isReadyToPublish = (rows, diffs) => classifyRows(rows, diffs).ready

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`
const crit = (n) => plural(n, 'criterion', 'criteria')
const NR = 'Not recorded'
const orNR = (v) => (v == null || v === '' ? NR : String(v))
const MODES = ['summary', 'reviewer', 'full']
export const MODE_LABEL = { summary: 'Summary', reviewer: 'Reviewer packet', full: 'Full evidence' }
const REVIEWER_CARD_CAP = 100
const countIssues = (rows) => rows.reduce((n, r) => {
  const list = Array.isArray(r.fileIssues) ? r.fileIssues.length : 0
  return n + (list || (Number.isFinite(r.count) ? r.count : 0))
}, 0)
const anyUncounted = (rows) => rows.some((r) => !(Array.isArray(r.fileIssues) && r.fileIssues.length) && !Number.isFinite(r.count))

const statusOfStage = (value) => (value == null ? 'unknown' : value > 0 ? 'done' : 'not_started')

// The per-file report model. d.mode ∈ summary | reviewer | full (default full).
// Returns { docTitle, filename, lang, mode, kind, identity, targetLevel, fullyConformant, ready,
// footerVersion, footerGenerated, cover, blocks }.
export function buildFileReportModel(d = {}) {
  const mode = MODES.includes(d.mode) ? d.mode : 'full'
  const atLeast = (m) => MODES.indexOf(mode) >= MODES.indexOf(m)
  const level = d.targetLevel || 'AA'
  const fileName = d.file || 'document'
  const fmt = fmtOfFile(fileName)
  const allDiffs = (d.diffs || []).filter((x) => x && (x.before != null || x.after != null))
  const rows = (d.rows || []).map((r) => ({ ...r, fileIssues: normaliseRowIssues(r, fileName, { locationHref: d.locationHref }) }))
  const c = classifyRows(rows, allDiffs)
  const inScopeN = c.inScope.length
  const passN = c.pass.length
  const fixedVN = c.fixedVerified.length
  const fixedPN = c.fixedPending.length
  const failN = c.fail.length
  const humanN = c.human.length
  const attestedN = c.attested.length
  const uncheckedN = c.unchecked.length
  const oosN = c.outOfScope.length
  const disposed = rows.filter((r) => r.disposition && r.disposition.kind)
  const ready = c.ready
  const generated = d.timestamp || d.date
  const generatedAt = d.identity?.generatedAt || d.generatedAt || new Date().toISOString()
  const artifact = d.artifact || {}
  const identity = {
    scanId: d.identity?.scanId ?? d.scanId ?? null,
    file: d.identity?.file ?? d.file ?? null,
    sourceSha256: d.identity?.sourceSha256 ?? artifact.sourceSha256 ?? null,
    correctedSha256: d.identity?.correctedSha256 ?? artifact.correctedSha256 ?? null,
    artifactVersion: d.identity?.artifactVersion ?? null,
    generatedAt,
    platformVersion: d.identity?.platformVersion ?? d.platformVersion ?? null,
    targetLevel: level,
  }
  const currentSha = artifact.currentSha256 ?? identity.correctedSha256 ?? identity.sourceSha256 ?? null
  const tone = (good) => (good ? { color: GREEN, bg: '#EEF5E8' } : { color: AMBER, bg: '#FBF1DF' })

  // Evidence counts. Null means the evidence was not supplied — rendered "Not recorded", never 0.
  // A diff list that could not be READ (d.diffsError) is not an empty list.
  const diffsError = typeof d.diffsError === 'string' && d.diffsError ? d.diffsError : null
  const diffsKnown = Array.isArray(d.diffs) && !diffsError
  const diffsComplete = d.diffsComplete !== false && !diffsError
  const diffsTotal = Number.isFinite(d.diffsTotal) ? d.diffsTotal : (diffsKnown && diffsComplete ? allDiffs.length : null)
  // remediation_diff stores before/after clipped at this many characters (store.record_remediation_diffs).
  const storeCap = Number.isFinite(d.diffValueCap) ? d.diffValueCap : null
  const atCap = (v) => storeCap != null && v != null && String(v).length >= storeCap
  const reviewsError = typeof d.reviewsError === 'string' && d.reviewsError ? d.reviewsError : null
  const previewReason = d.previewStatus && typeof d.previewStatus.reason === 'string' ? d.previewStatus.reason : null
  const editsSaved = diffsTotal != null && !(diffsTotal === 0 && (fixedPN + fixedVN) > 0) ? diffsTotal : null
  const findingsRemaining = anyUncounted(c.fail) ? null : countIssues(c.fail)
  const findingsVerified = anyUncounted(c.fixedVerified) ? null : countIssues(c.fixedVerified)
  const reviewsLoaded = d.reviews != null && typeof d.reviews === 'object'
  const cardClamp = mode !== 'summary'
  const changeCards = buildChangeCards({
    file: fileName, diffs: allDiffs, reviews: reviewsLoaded ? d.reviews : null, previews: d.previews,
    currentSha256: currentSha, locationHref: d.locationHref, clamp: cardClamp,
    fullRefPrefix: mode === 'full' ? '' : 'Full evidence report · record ',
  }).map((x) => ({ ...x, beforeStoredClipped: atCap(x.before), afterStoredClipped: atCap(x.after) }))
  const storedClippedN = changeCards.filter((x) => x.beforeStoredClipped || x.afterStoredClipped).length
  const storedClipNote = storedClippedN
    ? `${plural(storedClippedN, 'change has', 'changes have')} a value at the ${storeCap.toLocaleString('en-US')}-character storage limit; the stored text may be incomplete — the corrected copy is the source of truth.`
    : null
  const findingCards = buildFindingCards({ file: fileName, rows, assignee: d.assignee, locationHref: d.locationHref })
  const decided = changeCards.filter((x) => ['accepted', 'edited', 'rejected', 'unable'].includes(x.human.status)).length
  const staleN = changeCards.filter((x) => x.human.status === 'stale').length
  const awaitingN = changeCards.length - decided
  const hitlN = Array.isArray(d.hitl) ? d.hitl.length : null

  const blocks = []
  const H = (text, lvl = 1) => blocks.push({ k: 'heading', text, level: lvl })
  const T = (text, o) => blocks.push({ k: 'text', text, o: o || {} })

  // ── Decision summary ────────────────────────────────────────────────────────────────────
  H('Decision summary')
  const outstanding = [
    failN ? `${plural(findingsRemaining ?? failN, 'open finding', 'open findings')}${findingsRemaining == null ? ' (criteria)' : ''}` : null,
    fixedPN ? `${crit(fixedPN)} with a saved edit awaiting re-validation` : null,
    humanN ? `${crit(humanN)} needing a human check` : null,
    uncheckedN ? `${crit(uncheckedN)} not checked` : null,
  ].filter(Boolean)
  blocks.push({
    k: 'callout',
    text: ready
      ? `No outstanding items for "${fileName}" among the ${inScopeN} in-scope WCAG 2.1 Level ${level} criteria ACP checked: each passed, was resolved by a recorded human attestation, or had a saved edit that cleared re-validation. This records what ACP checked, changed and verified; it is not a conformance determination.`
      : inScopeN === 0
        ? `No in-scope criteria were evaluated for "${fileName}", so this report cannot support a publication decision.`
        : `Outstanding before publication of "${fileName}": ${outstanding.join('; ')}.`,
    o: tone(ready),
  })
  blocks.push({
    k: 'decisionSummary',
    caption: `Decision evidence for ${fileName}`,
    items: [
      { key: 'documentsAssessed', label: 'Documents assessed', value: rows.length ? 1 : 0, detail: `${inScopeN} in-scope criteria at WCAG 2.1 Level ${level}` },
      { key: 'editsSaved', label: 'Edits saved', value: editsSaved,
        detail: editsSaved == null
          ? (diffsError ? `Saved-edit records could not be read: ${diffsError}`
            : (fixedPN + fixedVN) ? `Edits were recorded for ${crit(fixedPN + fixedVN)}, but no per-change record was available` : 'Saved-edit records were not loaded')
          : diffsComplete ? 'Per-change records' : `Partial: ${allDiffs.length} of ${diffsTotal} records loaded` },
      { key: 'findingsVerifiedResolved', label: 'Findings verified resolved', value: findingsVerified,
        detail: fixedVN ? `Across ${crit(fixedVN)} whose saved edit cleared the re-scan` : 'No saved edit has a recorded re-scan result' },
      { key: 'findingsRemaining', label: 'Findings remaining', value: findingsRemaining,
        detail: fixedPN ? `Excludes ${crit(fixedPN)} with a saved edit awaiting re-validation` : `Across ${crit(failN)}` },
      { key: 'humanChecksPending', label: 'Human checks pending', value: humanN,
        detail: `${crit(humanN)} a person must verify${attestedN ? `; ${attestedN} already attested` : ''}` },
      { key: 'checksNotPerformed', label: 'Checks not performed', value: uncheckedN,
        detail: 'Criteria with no automated check for this file type and no recorded human result — not counted as passing' },
    ],
  })
  blocks.push({
    k: 'stageStrip',
    items: [
      { key: 'suggestions', label: 'Suggestions', value: hitlN, status: hitlN == null ? 'unknown' : hitlN > 0 ? 'done' : 'not_started',
        detail: hitlN == null ? 'AI and rule suggestions were not loaded into this report' : `${plural(hitlN, 'suggestion', 'suggestions')} recorded for review` },
      { key: 'savedEdits', label: 'Saved edits', value: editsSaved, status: statusOfStage(editsSaved),
        detail: editsSaved == null ? 'Not recorded' : `${plural(editsSaved, 'change', 'changes')} written to the corrected copy` },
      { key: 'technicalChecks', label: 'Technical re-checks', value: fixedVN,
        status: fixedPN ? 'pending' : fixedVN ? 'done' : 'not_started',
        detail: `${crit(fixedVN)} verified by re-scan${fixedPN ? `; ${crit(fixedPN)} awaiting re-validation` : ''}` },
      { key: 'humanConfirmation', label: 'Human confirmation', value: reviewsLoaded ? decided : null,
        status: !changeCards.length ? 'not_started' : !reviewsLoaded ? 'unknown' : awaitingN ? 'pending' : 'done',
        detail: !changeCards.length ? 'No saved changes to confirm'
          : !reviewsLoaded ? (reviewsError ? `Reviewer decisions could not be read: ${reviewsError}` : 'Reviewer decisions were not loaded into this report')
          : `${decided} of ${changeCards.length} changes decided${staleN ? `; ${staleN} stale because the file changed` : ''}` },
      { key: 'publication', label: 'Publication', value: null,
        status: d.publishedAt ? 'done' : d.publishedAt === null ? 'not_started' : 'unknown',
        detail: d.publishedAt ? `Published ${d.publishedAt}` : d.publishedAt === null ? 'Not published' : 'Publication status not recorded in this report' },
    ],
  })
  H('Criteria outcomes', 2)
  blocks.push({
    k: 'bullets',
    items: [
      `${passN} of ${inScopeN} in-scope criteria pass`,
      fixedVN ? `${crit(fixedVN)} resolved by a saved edit that cleared re-validation` : null,
      fixedPN ? `${crit(fixedPN)} with a saved edit awaiting re-validation — not counted as passing` : null,
      failN ? `${crit(failN)} with open findings` : null,
      humanN ? `${crit(humanN)} need a human reviewer` : null,
      attestedN ? `${crit(attestedN)} manually attested by a human (verified outside ACP) — see Dispositions` : null,
      oosN ? `${crit(oosN)} recorded out of scope for this engagement — see Dispositions` : null,
      uncheckedN ? `${crit(uncheckedN)} not auto-checked for this file type — reported, not assumed passing` : null,
      ready ? 'No outstanding items: the evidence supports a publication decision.' : 'Next step: work through Remaining work and Changes to confirm, then re-validate.',
    ].filter(Boolean),
    o: {},
  })
  T(`Assessment score: ${d.score != null ? `${d.score}/100` : NR} — a secondary indicator. The counts above are the decision evidence.`, { size: 9, color: MUTED })

  // ── Identity ────────────────────────────────────────────────────────────────────────────
  H('Document identity')
  blocks.push({
    k: 'table',
    // The server renderer replaces this table with the identity it read from its own store.
    role: 'identity',
    headers: ['Field', 'Value'],
    caption: 'Document and report identity',
    rows: [
      ['Document', orNR(identity.file)],
      ['Assessment (scan) id', orNR(identity.scanId)],
      ['Source SHA-256', orNR(identity.sourceSha256)],
      ['Corrected copy SHA-256', orNR(identity.correctedSha256)],
      ['Artifact version', orNR(identity.artifactVersion)],
      ['Target', `WCAG 2.1 Level ${level}`],
      ['Report generated', orNR(identity.generatedAt)],
      ['Platform version', orNR(identity.platformVersion)],
      ['Report mode', MODE_LABEL[mode]],
    ],
    widths: [150, CW - 150],
  })

  // ── Since the previous assessment ───────────────────────────────────────────────────────
  H('Since the previous assessment')
  const currentFindings = c.fail.concat(c.fixedPending).some((r) => !(r.fileIssues || []).length && (r.count || 0) > 0)
    ? null
    : rows.filter((r) => r.outcome === 'FAIL' || (r.outcome === 'FIXED' && !c.isVerified(r)) || (r.fileIssues || []).some((i) => i.severity === 'REVIEW'))
      .flatMap((r) => r.fileIssues || [])
  // Current findings come from the SAME server record shape the previous snapshot was built
  // from (d.currentFindings, stream C), so ids match; the rows are only the fallback.
  const comparison = buildComparison(d.previous, {
    file: identity.file,
    scope: d.scope === undefined ? { targetLevel: level } : d.scope,
    findings: Array.isArray(d.currentFindings) ? d.currentFindings : currentFindings,
  })
  if (!d.previous && typeof d.previousReason === 'string' && d.previousReason.trim()) comparison.reason = d.previousReason.trim()
  blocks.push(comparison)

  // ── Reviewer packet ─────────────────────────────────────────────────────────────────────
  if (atLeast('reviewer')) {
    H('Changes to confirm')
    if (changeCards.length) {
      T(`${plural(changeCards.length, 'saved change', 'saved changes')} recorded for this file. Technical verification (the re-scan) and human confirmation are separate: a verified change can still be wrong in context.${diffsComplete ? '' : ` Partial: ${allDiffs.length} of ${diffsTotal ?? 'an unknown number of'} change records were available.`}`, { size: 9, color: MUTED })
      blocks.push({ k: 'callout', text: RESPONSE_NOTICE, o: { color: PLUM } })
      if (!reviewsLoaded) T(reviewsError ? `Reviewer decisions could not be read (${reviewsError}), so every change below shows as awaiting confirmation.` : 'Reviewer decisions were not loaded, so every change below shows as awaiting confirmation.', { color: AMBER })
      if (storedClipNote) T(storedClipNote, { color: AMBER })
      if (previewReason) T(previewReason, { size: 9, color: MUTED })
      const cap = mode === 'full' ? null : REVIEWER_CARD_CAP
      const b = boundList(changeCards, cap)
      b.shown.forEach((x) => blocks.push(x))
      if (b.omitted) T(`${b.omitted} more saved change${b.omitted === 1 ? '' : 's'} (of ${b.total}) are listed in the Full evidence report.`, { bold: true })
    } else if (diffsError) {
      T(`Saved changes could not be read: ${diffsError} Nothing is listed here for that reason — not because there are none.`, { color: AMBER })
    } else if (fixedPN + fixedVN > 0) {
      T(`Edits were recorded for ${crit(fixedPN + fixedVN)}, but no per-change before/after record is available, so there is nothing itemised to confirm.`, { color: AMBER })
    } else {
      T('No saved changes are recorded for this file.', { color: MUTED })
    }

    H('Remaining work')
    const prinCount = {}
    c.fail.concat(c.human).forEach((r) => { const k = r.id.match(/^(\d)/)?.[1]; if (PRINCIPLE[k]) prinCount[k] = (prinCount[k] || 0) + 1 })
    const prinItems = Object.keys(PRINCIPLE).filter((k) => prinCount[k]).map((k) => ({ label: PRINCIPLE[k], value: prinCount[k], color: PRIN_CLR[k] }))
    if (prinItems.length) {
      T('Criteria with open findings or human checks, by WCAG principle:', { size: 9, color: MUTED })
      blocks.push({ k: 'barChart', items: prinItems, o: { labelW: 150 } })
    }
    if (findingCards.length) {
      T('One card per finding, most actionable first: open findings, then human checks, then criteria that were not checked.', { size: 9, color: MUTED })
      const cap = mode === 'full' ? null : REVIEWER_CARD_CAP
      const b = boundList(findingCards, cap)
      b.shown.forEach((x) => blocks.push(x))
      if (b.omitted) T(`${b.omitted} more item${b.omitted === 1 ? '' : 's'} (of ${b.total}) are listed in the Full evidence report.`, { bold: true })
    } else {
      T(fixedPN ? 'No open findings; saved edits still await re-validation (see Changes to confirm).' : 'No remaining work is recorded for this file.', { color: MUTED })
    }

    // Checklist by area — never "Pass" over an unchecked or unverified criterion.
    H('Checklist by area')
    const catAgg = {}
    rows.forEach((r) => {
      if (r.disposition?.kind === 'out_of_scope') return
      const cat = CAT_OF(r.id)
      const a = catAgg[cat] || (catAgg[cat] = { fail: 0, human: 0, pending: 0, unchecked: 0, ok: 0 })
      if (r.outcome === 'FAIL') a.fail++
      else if (r.outcome === 'HUMAN' && !r.disposition) a.human++
      else if (r.outcome === 'FIXED' && !c.isVerified(r)) a.pending++
      else if (r.outcome === 'UNCHECKED' && !r.disposition) a.unchecked++
      else a.ok++
    })
    blocks.push({
      k: 'table',
      headers: ['Area', 'Status'],
      caption: 'Checklist by area',
      rows: CAT_ORDER.filter((cat) => catAgg[cat]).map((cat) => {
        const a = catAgg[cat]
        const parts = [
          a.fail ? `✗ ${a.fail} open` : null,
          a.human ? `◐ ${a.human} human review` : null,
          a.pending ? `… ${a.pending} awaiting re-validation` : null,
          a.unchecked ? `— ${a.unchecked} not checked` : null,
        ].filter(Boolean)
        return [cat, parts.length ? parts.join(' · ') : '✓ No outstanding items']
      }),
      widths: [CW - 220, 220],
    })

    if (disposed.length) {
      H('Manual attestations & dispositions')
      T(`${disposed.length} criteri${disposed.length !== 1 ? 'a were' : 'on was'} resolved outside ACP's automated checks — ${attestedN} manually attested (verified out-of-band) and ${oosN} recorded as out of scope for this engagement. Each is documented below with the reason on record.`, { size: 9, color: MUTED, gapAfter: 8 })
      blocks.push({
        k: 'table',
        headers: ['WCAG', 'Criterion', 'Disposition', 'Reason'],
        caption: 'Manual attestations and out-of-scope dispositions',
        rows: disposed.map((r) => [
          r.id, r.plain || r.name,
          r.disposition.kind === 'attested' ? 'Manually attested' : 'Out of scope',
          `${r.disposition.reason || 'Reason not recorded'}${r.disposition.actor ? ` — ${r.disposition.actor}` : ''}`,
        ]),
        widths: [52, 150, 100, CW - 52 - 150 - 100],
      })
      T('A manual attestation is a human’s recorded verification, not an automated pass; an out-of-scope criterion is excluded from the in-scope denominator above. Both are decisions in the audit trail.', { size: 8.5, color: MUTED, lh: 12 })
    }

    if (c.human.length) {
      H('Human review — how to verify')
      T('These criteria need a person to decide them. For each: why automated checks can’t, and how to check.', { size: 9, color: MUTED, gapAfter: 10 })
      c.human.forEach((r) => {
        const g = HUMAN_GUIDE[r.id] || DEFAULT_HUMAN
        T(`${r.id} · ${r.plain || r.name}`, { bold: true, size: 11, gapAfter: 3 })
        T(`Why a human: ${g.why}`, { size: 9.5, lh: 13, gapAfter: 3 })
        blocks.push({ k: 'bullets', items: g.how, o: { size: 9.5 } })
        T(`Estimated time: ~${g.min} min (estimate)`, { size: 9, color: MUTED, gapAfter: 11 })
      })
    }

    const vg = VERIFY_GUIDE[fmt] || VERIFY_GUIDE.html
    H('Manual verification guide')
    T(`Independently check this ${vg.app} document — no ACP account needed. Steps for macOS and Windows, plus a screen-reader pass.`, { size: 9, color: MUTED, gapAfter: 10 })
    T('macOS', { bold: true, size: 10.5, gapAfter: 3 }); blocks.push({ k: 'bullets', items: vg.mac, o: { size: 9.5 } })
    T('Windows', { bold: true, size: 10.5, gapAfter: 3 }); blocks.push({ k: 'bullets', items: vg.win, o: { size: 9.5 } })
    T('Screen-reader pass', { bold: true, size: 10.5, gapAfter: 3 }); blocks.push({ k: 'bullets', items: vg.sr, o: { size: 9.5 } })
    T('Confirm each of:', { bold: true, size: 10.5, gapAfter: 3 }); blocks.push({ k: 'bullets', items: vg.checks.map((x) => `☐ ${x}`), o: { size: 9.5 } })
  }

  // ── Full evidence ───────────────────────────────────────────────────────────────────────
  if (atLeast('full')) {
    H('Result')
    blocks.push({
      k: 'metricGrid',
      cards: [
        { label: 'Pass or verified', value: passN + fixedVN, color: GREEN },
        { label: 'Open findings', value: failN, color: failN ? RED : GREEN },
        { label: 'Awaiting re-validation', value: fixedPN, color: fixedPN ? AMBER : GREEN },
        { label: 'Human review', value: humanN, color: humanN ? AMBER : GREEN },
        { label: 'Score (secondary)', value: d.score != null ? `${d.score}/100` : NR, color: INK },
      ],
    })
    H('Coverage at a glance')
    blocks.push({
      k: 'donut',
      items: [
        { label: 'Pass', value: passN, color: GREEN },
        { label: 'Fixed · verified', value: fixedVN, color: '#5C9B2E' },
        { label: 'Fixed · awaiting re-validation', value: fixedPN, color: '#C99A3E' },
        { label: 'Open finding', value: failN, color: RED },
        { label: 'Human review', value: humanN, color: AMBER },
        { label: 'Attested', value: attestedN, color: BLUE },
        { label: 'Not checked', value: uncheckedN, color: '#B6B0BC' },
      ],
    })
    if (fixedVN + fixedPN > 0) {
      H('What ACP changed')
      T(`Edits were saved for ${crit(fixedVN + fixedPN)}: ${fixedVN} cleared the re-scan, ${fixedPN} still await re-validation.`, { size: 9, color: MUTED, gapAfter: 8 })
      blocks.push({
        k: 'bullets',
        items: c.fixedVerified.concat(c.fixedPending).map((r) => {
          const n = r.count || 1
          return `${r.id} — ${CHANGE_LABEL[r.id] || `${r.plain || r.name} edited`}${n > 1 ? ` (${n} occurrences)` : ''} · ${c.isVerified(r) ? 'verified by re-scan' : 'awaiting re-validation'}`
        }),
        o: {},
      })
    }

    blocks.push({ k: 'pageBreak' })
    H('Complete evidence appendix')
    T('Every record ACP holds for this report, in full, with stable identifiers. Values are reproduced exactly; nothing in this appendix is abbreviated.', { size: 9, color: MUTED })
    const links = []
    // Unsafe hrefs are nulled here AND refused by every renderer; the text still prints.
    const addLink = (text, href) => {
      if (href == null && !text) return
      links.push({ k: 'link', text: String(text || href || ''), href: isSafeHref(href) ? href : null })
    }
    if (d.links && typeof d.links === 'object') {
      if (Array.isArray(d.links)) d.links.forEach((l) => addLink(l?.text, l?.href))
      else {
        addLink('Open the original document', d.links.original)
        addLink('Open the corrected copy', d.links.corrected)
      }
    }
    if (links.length) { H('Artifacts', 2); links.forEach((l) => blocks.push(l)) }

    H('Saved changes — full before and after', 2)
    if (allDiffs.length) {
      blocks.push({ k: 'beforeAfter', items: changeCards.map((x) => ({
        id: x.id,
        label: `${x.title} · ${locationLabel(x.location)}`,
        note: x.reason || '',
        before: x.before,
        after: x.after,
        location: x.location,
        technical: x.technical.status,
        human: x.human.status,
      })) })
    } else {
      T(diffsKnown ? 'No per-change records exist for this file.'
        : diffsError ? `Per-change records could not be read: ${diffsError}` : 'Per-change records were not loaded — not recorded.', { color: MUTED })
    }
    if (storedClipNote) T(storedClipNote, { color: AMBER })
    const appliedAt = {}
    ;(d.appliedFixes || []).forEach((f) => {
      const k = scOfValue(f.rule_id)
      if (k && f.created_at && (!appliedAt[k] || String(f.created_at) < String(appliedAt[k]))) appliedAt[k] = f.created_at
    })
    blocks.push({
      k: 'appendixTable',
      id: 'appendix-changes',
      complete: diffsComplete,
      totalRecords: diffsTotal,
      limitNote: diffsComplete ? null : diffsError ? `records that could be read (${diffsError})` : 'the change records the server returned for this request',
      headers: ['Record id', 'Criterion', 'Location', 'Reason', 'Technical', 'Human decision', 'Saved at', 'Stored value'],
      caption: 'Saved change records',
      rows: changeCards.map((x) => [
        x.id, x.title, locationLabel(x.location), x.reason || 'Reason not recorded',
        technicalText(x.technical.status),
        `${humanText(x.human.status)}${x.human.reviewer ? ` — ${x.human.reviewer}` : ''}${x.human.at ? ` at ${x.human.at}` : ''}`,
        appliedAt[x.criterion] || NR,
        (x.beforeStoredClipped || x.afterStoredClipped) ? `At the ${storeCap}-character storage limit; may be incomplete` : 'Complete as stored',
      ]),
    })
    const findings = rows.flatMap((r) => (r.fileIssues || []).map((i) => ({ r, i })))
    blocks.push({
      k: 'appendixTable',
      id: 'appendix-findings',
      complete: !anyUncounted(rows) && rows.every((r) => !(r.count > 0) || (r.fileIssues || []).length > 0),
      totalRecords: findings.length,
      limitNote: 'criteria whose findings were counted but not itemised',
      headers: ['Finding id', 'Criterion', 'Severity', 'Location', 'Detail', 'Criterion outcome'],
      caption: 'All findings',
      rows: findings.map(({ r, i }) => [
        i.id, `${r.id} · ${r.plain || r.name}`, i.severity || NR, locationLabel(i.location), i.detail || NR,
        r.outcome === 'FIXED' ? (c.isVerified(r) ? 'Fixed · verified' : 'Fixed · awaiting re-validation') : (COV_OUT_TXT[r.outcome] || r.outcome),
      ]),
    })
    const reviewRows = changeCards.filter((x) => x.human.status !== 'pending')
    blocks.push({
      k: 'appendixTable',
      id: 'appendix-reviews',
      complete: reviewsLoaded,
      totalRecords: reviewsLoaded ? reviewRows.length : null,
      limitNote: reviewsLoaded ? null : 'reviewer decisions were not loaded',
      headers: ['Change id', 'Decision', 'Reviewer', 'Recorded at', 'Bound to SHA-256', 'Current SHA-256', 'Note'],
      caption: 'Recorded reviewer decisions',
      rows: reviewRows.map((x) => [x.id, humanText(x.human.status), orNR(x.human.reviewer), orNR(x.human.at),
        orNR(x.human.boundSha256), orNR(x.human.currentSha256), x.human.note || '']),
    })

    H('Full WCAG coverage', 2)
    T(`Every criterion in this engagement's scope at the ${level} target.${uncheckedN ? ` ${crit(uncheckedN)} had no automated check for this file type and are reported as not checked, not passing.` : ''}`, { size: 9, color: MUTED, gapAfter: 8 })
    blocks.push({
      k: 'table',
      headers: ['WCAG', 'Criterion', 'Level', 'Fix approach', 'Outcome', 'Confidence'],
      caption: `Full WCAG coverage at the ${level} target`,
      rows: rows.map((r) => [r.id, r.plain || r.name, r.level, (r.fix || '').replace(/[⚡✎✋]\s*/, ''),
        r.disposition ? (r.disposition.kind === 'attested' ? 'Attested (human)' : 'Out of scope')
          : r.outcome === 'FIXED' ? (c.isVerified(r) ? 'Fixed · verified' : 'Fixed · awaiting re-validation')
          : COV_OUT_TXT[r.outcome] || r.outcome,
        r.disposition ? (r.disposition.kind === 'attested' ? 'Human' : '—') : r.confidence ? r.confidence.level.label : '—']),
      widths: [52, CW - 52 - 44 - 84 - 92 - 62, 44, 84, 92, 62],
    })
    // Confidence is evidence-based, never a fabricated % (ADR 0016).
    T('Confidence is derived from concrete pipeline evidence (rule determinism, PII checksum validation, and residual re-scan verification) — never an invented percentage. High = deterministic check, checksum-validated match, or a fix that cleared re-scan; Medium = AI/heuristic detection or pattern-only match; Low = requires human review.', { size: 8, color: MUTED, lh: 11 })

    H('Audit trail', 2)
    const auditRows = [
      ['Assessed', `ACP · WCAG ${level}`, `${rows.length} criteria evaluated · score ${d.score ?? NR}`, fileName],
      ...(fixedVN + fixedPN > 0 ? [['Edits saved', 'ACP remediation', `${crit(fixedVN + fixedPN)} edited; ${fixedVN} verified by re-scan`, fileName]] : []),
      ...(humanN > 0 ? [['Pending human review', 'Review queue', `${crit(humanN)} awaiting a reviewer`, fileName]] : []),
      ...(attestedN > 0 ? [['Manually attested', 'human disposition', `${crit(attestedN)} verified out-of-band`, fileName]] : []),
      ...(oosN > 0 ? [['Marked out of scope', 'human disposition', `${crit(oosN)} excluded with a recorded reason`, fileName]] : []),
      ...reviewRows.map((x) => [`Change ${humanText(x.human.status).toLowerCase()}`, orNR(x.human.reviewer), `${x.title} (${x.id})${x.human.at ? ` at ${x.human.at}` : ''}`, fileName]),
      ['Report generated', 'ACP', `${ready ? 'No outstanding items' : `${outstanding.length ? outstanding.join('; ') : 'items outstanding'}`}`, fileName],
    ]
    blocks.push({ k: 'table', headers: ['Step', 'Actor', 'Action', 'Document'], caption: 'Audit trail', rows: auditRows, widths: [72, 108, CW - 72 - 108 - 140, 140] })
  }

  // ── What this report is ─────────────────────────────────────────────────────────────────
  H('What this report is, and is not')
  blocks.push({
    k: 'callout',
    text: `This report records what ACP checked, changed and verified for "${fileName}" against the WCAG 2.1 Level ${level} criteria in this engagement's scope. It is not a conformance determination, a certification or legal advice. It can support an ADA, Section 508 or EN 301 549 / European Accessibility Act review as evidence, alongside a qualified human evaluation.`,
    o: tone(ready),
  })
  T(`Generated: ${generated || generatedAt}${d.platformVersion ? ` · Platform v${d.platformVersion}` : ''} · ${MODE_LABEL[mode]}`, { size: 9, color: MUTED, gapAfter: 4 })
  T(RESPONSE_NOTICE, { size: 8, color: MUTED, lh: 12 })

  const base = fileName.replace(/\.[^.]+$/, '')
  return {
    docTitle: `Accessibility Assessment Report — ${fileName}`,
    filename: d.filename || `mova-${base}-${mode === 'full' ? 'evidence' : mode}-report`,
    lang: d.lang || 'en-US',
    mode,
    kind: 'file',
    identity,
    targetLevel: level,
    // Kept for callers of the old certification model. Now true ONLY under the evidence-truth rule.
    fullyConformant: ready,
    ready,
    footerVersion: d.platformVersion,
    footerGenerated: generated,
    cover: {
      title: 'Accessibility Assessment Report',
      subtitle: fileName,
      meta: [
        `${MODE_LABEL[mode]} · generated ${generated || generatedAt}${d.engine ? ` · ${d.engine}` : ''}`,
        `WCAG 2.1 Level ${level}${d.sourceName ? ` · ${d.sourceName}` : ''}${d.department ? ` · ${d.department}` : ''}`,
      ],
      ring: null,
    },
    blocks,
  }
}

// Compatibility alias: the old per-file export was the whole record, so it maps to Full evidence.
export function buildFileCertificationModel(d = {}) {
  return buildFileReportModel({ ...d, mode: 'full' })
}

// ── Remediation report ────────────────────────────────────────────────────────────────────
//
// The deliverable a reviewer signs: WHAT was changed, WHEN, and a checkbox per item so a human
// can confirm each one in the real application. Distinct from the certification report, which
// states conformance — this states WORK DONE and asks someone to verify it.
//
// TIMESTAMPS ARE NOT INVENTED. `remediation_diff` carries no time column at all
// (scan_id, file, rule_id, seq, before, after, note); the clock lives in
// `applied_fixes.created_at` and `file_records.remediated_at`. So an item is stamped only when
// applied_fixes holds a row for that (file, criterion). Everything else reads "time not
// recorded" rather than borrowing the document's timestamp and implying a precision we do not
// have — a signed remediation record is exactly the wrong place to round up.
// This module stays pure (no I/O). Its only import is the pure evidence layer; criterion names for
// the remediation model still arrive as `scNames` from the caller, which owns the catalog.
const _sc = (w) => ((String(w || '')).replace(/^SC_/, '').replace(/_/g, '.').match(/\d+\.\d+\.\d+/) || [])[0]
const _fmtOf = (f) => (String(f || '').split('.').pop() || '').toLowerCase()
const _base = (p) => String(p || '').split('/').filter(Boolean).pop() || ''
const _dir = (p) => { const s = String(p || '').split('/').filter(Boolean); return s.length > 1 ? s.slice(0, -1).join('/') : '' }
const _when = (iso) => {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d.toLocaleString(undefined,
    { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

// files        the scan's file rows (needs .file and .remediated_at)
// diffsByFile  { [file]: [{ rule_id, before, after, note }] } — the evidence
// appliedFixes [{ file, rule_id, created_at }] — the clock. May be empty (SIM, or a scan whose
//              fixes predate the table); absence degrades to "time not recorded", never a guess.
// cappedAt     the server's row limit, when the caller hit it, so the report can SAY it is
//              partial instead of presenting a truncated list as complete.
// scNames      { [sc]: 'Non-text Content' } — supplied by the caller, which owns the catalog.
export function buildRemediationModel({ files = [], diffsByFile = {}, appliedFixes = [],
                                        level = 'AA', org = '', generatedAt = null,
                                        scanId = null, reviewByFile = {}, cappedAt = null,
                                        scNames = {}, diffsComplete = true, diffsTotal = null,
                                        platformVersion = null } = {}) {
  const stamp = {}
  for (const f of appliedFixes || []) {
    const k = `${f.file} ${_sc(f.rule_id)}`
    // Earliest wins. A criterion fixed across nineteen images was DONE when the first write
    // landed; the API returns newest-first, which would otherwise report the last one.
    if (!stamp[k] || String(f.created_at) < String(stamp[k])) stamp[k] = f.created_at
  }

  const documents = []
  for (const rec of files) {
    const diffs = diffsByFile[rec.file] || []
    if (!diffs.length && !rec.remediated_at) continue     // nothing was done to this document
    const seen = new Set()
    const items = []
    for (const d of diffs) {
      const sc = _sc(d.rule_id)
      if (!sc || seen.has(sc)) continue                   // one checkbox per criterion, not per image
      seen.add(sc)
      const at = stamp[`${rec.file} ${sc}`] || null
      items.push({
        sc,
        category: CAT_OF(sc),
        name: scNames[sc] || d.rule_id,
        note: d.note || null,
        before: d.before,
        after: d.after,
        at: _when(at),
        atIso: at,
      })
    }
    items.sort((a, b) => (CAT_ORDER.indexOf(a.category) - CAT_ORDER.indexOf(b.category)) || a.sc.localeCompare(b.sc))
    // `items` is the one-checkbox-per-criterion view; `changes` keeps EVERY record, so the evidence
    // appendix never loses the other images behind one checkbox.
    const changes = diffs.filter((x) => x && (x.before != null || x.after != null)).map((x) => {
      const at = stamp[`${rec.file} ${_sc(x.rule_id)}`] || null
      return { ...x, at: _when(at), atIso: at }
    })
    documents.push({
      file: rec.file,
      name: _base(rec.file),
      dir: _dir(rec.file),
      fmt: _fmtOf(rec.file),
      remediatedAt: _when(rec.remediated_at),
      awaiting: reviewByFile[rec.file] || 0,
      items,
      changes,
    })
  }
  documents.sort((a, b) => a.name.localeCompare(b.name))

  const totalItems = documents.reduce((n, d) => n + d.items.length, 0)
  const stamped = documents.reduce((n, d) => n + d.items.filter((i) => i.at).length, 0)
  // Only the formats actually present get a "how to verify" section. A report on three PDFs has
  // no business explaining PowerPoint.
  const formats = [...new Set(documents.map((d) => d.fmt))].filter((f) => VERIFY_GUIDE[f]).sort()

  return {
    org, level, scanId,
    generatedAt: _when(generatedAt) || _when(new Date().toISOString()),
    documents,
    formats,
    totals: {
      documents: documents.length,
      items: totalItems,
      stamped,
      unstamped: totalItems - stamped,
      awaiting: documents.reduce((n, d) => n + d.awaiting, 0),
      criteria: new Set(documents.flatMap((d) => d.items.map((i) => i.sc))).size,
      changes: documents.reduce((n, d) => n + d.changes.length, 0),
    },
    // Said out loud in the PDF, never swallowed: a truncated list that looks complete is worse
    // than one that admits it is truncated.
    partial: cappedAt != null && (appliedFixes || []).length >= cappedAt ? cappedAt : null,
    diffsComplete: diffsComplete !== false,
    diffsTotal: Number.isFinite(diffsTotal) ? diffsTotal : null,
    platformVersion,
  }
}

// ── Remediation report → contract blocks (kind 'remediation') ────────────────────────────────
// Turns buildRemediationModel output into the renderer-agnostic block list the server PDF and the
// HTML renderer consume. Every saved change becomes a changeCard (never one per criterion), and
// Full evidence lists every record. `reviewsByFile` is { [file]: reviews map } from the versioned
// change-review store; absent means "not loaded", never "nobody decided".
export function remediationReportModel(m = {}, { mode = 'full', reviewsByFile = null, previewsByFile = null, currentShaByFile = null } = {}) {
  const md = MODES.includes(mode) ? mode : 'full'
  const atLeast = (x) => MODES.indexOf(md) >= MODES.indexOf(x)
  const blocks = []
  const H = (text, level = 1) => blocks.push({ k: 'heading', text, level })
  const T = (text, o) => blocks.push({ k: 'text', text, o: o || {} })
  const totals = m.totals || {}
  const docs = m.documents || []
  const cardsByDoc = docs.map((doc) => ({
    doc,
    cards: buildChangeCards({
      file: doc.file, diffs: doc.changes || doc.items || [],
      reviews: reviewsByFile ? (reviewsByFile[doc.file] || {}) : null,
      previews: previewsByFile ? previewsByFile[doc.file] : null,
      currentSha256: currentShaByFile ? currentShaByFile[doc.file] ?? null : null,
      clamp: md !== 'summary',
      fullRefPrefix: md === 'full' ? '' : 'Full evidence report · record ',
    }),
  }))
  const all = cardsByDoc.flatMap((x) => x.cards)
  const decided = reviewsByFile ? all.filter((x) => ['accepted', 'edited', 'rejected', 'unable'].includes(x.human.status)).length : null
  const verified = all.filter((x) => x.technical.status === 'verified').length
  const diffsPartial = m.diffsComplete === false
  const partial = m.partial != null || diffsPartial
  const savedTotal = diffsPartial ? (m.diffsTotal ?? null) : all.length

  H('Decision summary')
  blocks.push({
    k: 'callout',
    text: docs.length
      ? `${plural(savedTotal ?? all.length, 'saved change', 'saved changes')} across ${plural(docs.length, 'document', 'documents')}. Each change has already been written to a corrected copy; this report records that work and who has confirmed it in ACP.${partial ? ' This report is PARTIAL — see the limits stated below.' : ''}`
      : 'No remediation has been applied to this scan yet, so there is nothing to confirm.',
    o: { color: PLUM },
  })
  blocks.push({
    k: 'decisionSummary',
    caption: 'Remediation decision evidence',
    items: [
      { key: 'documentsAssessed', label: 'Documents with saved edits', value: docs.length, detail: 'Documents with a saved corrected copy or change record' },
      { key: 'editsSaved', label: 'Edits saved', value: savedTotal, detail: diffsPartial ? `Partial: ${all.length} records loaded` : 'Per-change records' },
      { key: 'findingsVerifiedResolved', label: 'Changes verified by re-scan', value: verified, detail: 'Counted from the loaded change records' },
      { key: 'findingsRemaining', label: 'Findings remaining', value: null, detail: 'Not part of the remediation record — see the assessment report' },
      { key: 'humanChecksPending', label: 'Changes awaiting confirmation', value: decided == null ? null : all.length - decided, detail: decided == null ? 'Reviewer decisions were not loaded' : `${decided} of ${all.length} decided` },
      { key: 'checksNotPerformed', label: 'Criteria with no recorded time', value: totals.unstamped ?? null, detail: 'Times are shown only where the write was recorded' },
    ],
  })
  blocks.push({
    k: 'stageStrip',
    items: [
      { key: 'suggestions', label: 'Suggestions', value: null, status: 'unknown', detail: 'Not part of this report' },
      { key: 'savedEdits', label: 'Saved edits', value: savedTotal, status: savedTotal == null ? 'unknown' : savedTotal ? 'done' : 'not_started', detail: savedTotal == null ? 'Not recorded' : plural(savedTotal, 'change', 'changes') },
      { key: 'technicalChecks', label: 'Technical re-checks', value: verified, status: !all.length ? 'not_started' : verified === all.length ? 'done' : 'pending', detail: `${verified} of ${all.length} loaded changes verified by re-scan` },
      { key: 'humanConfirmation', label: 'Human confirmation', value: decided, status: decided == null ? 'unknown' : !all.length ? 'not_started' : decided === all.length ? 'done' : 'pending', detail: decided == null ? 'Reviewer decisions were not loaded' : `${decided} of ${all.length} decided` },
      { key: 'publication', label: 'Publication', value: null, status: 'unknown', detail: 'Publication status not recorded in this report' },
    ],
  })
  if (totals.unstamped) T(`${totals.unstamped} of ${totals.items} criteria carry no recorded time; they read "Time not recorded" rather than borrowing the document's timestamp.`, { size: 9, color: MUTED })
  if (m.partial != null) T(`PARTIAL: the server returned its maximum of ${m.partial} fix-time records, so later records are not listed.`, { bold: true, color: AMBER })
  if (diffsPartial) T(`PARTIAL: ${all.length} of ${m.diffsTotal ?? 'an unknown number of'} change records were loaded.`, { bold: true, color: AMBER })

  if (atLeast('reviewer')) {
    H('Changes to confirm')
    blocks.push({ k: 'callout', text: RESPONSE_NOTICE, o: { color: PLUM } })
    for (const { doc, cards } of cardsByDoc) {
      H(doc.name, 2)
      T(`${doc.dir ? `Folder: ${doc.dir}` : 'Folder: (root)'} · Format: .${doc.fmt} · ${doc.remediatedAt ? `Remediated ${doc.remediatedAt}` : 'Remediation time not recorded'}${doc.awaiting ? ` · ${doc.awaiting} item(s) awaiting review in ACP` : ''}`, { size: 9, color: MUTED })
      if (!cards.length) { T('This document was remediated, but no per-change record was stored for it.', { color: MUTED }); continue }
      const b = boundList(cards, md === 'full' ? null : REVIEWER_CARD_CAP)
      b.shown.forEach((x) => blocks.push(x))
      if (b.omitted) T(`${b.omitted} more change(s) for this document (of ${b.total}) are listed in the Full evidence report.`, { bold: true })
    }
    H('How to verify these changes yourself')
    for (const fmt of m.formats || []) {
      const g = VERIFY_GUIDE[fmt]
      if (!g) continue
      H(`.${fmt} — ${g.app}`, 2)
      T('On macOS', { bold: true }); blocks.push({ k: 'bullets', items: g.mac || [], o: {} })
      T('On Windows', { bold: true }); blocks.push({ k: 'bullets', items: g.win || [], o: {} })
      if (g.checks?.length) { T('What to look for', { bold: true }); blocks.push({ k: 'bullets', items: g.checks, o: {} }) }
      if (g.sr?.length) { T('Screen-reader spot check', { bold: true }); blocks.push({ k: 'bullets', items: g.sr, o: {} }) }
    }
  }

  if (atLeast('full')) {
    blocks.push({ k: 'pageBreak' })
    H('Complete evidence appendix')
    blocks.push({
      k: 'appendixTable',
      id: 'appendix-remediation-changes',
      complete: !diffsPartial,
      totalRecords: savedTotal,
      limitNote: diffsPartial ? 'the change records the server returned for this request' : null,
      headers: ['Record id', 'Document', 'Criterion', 'Location', 'Reason', 'Technical', 'Human decision', 'Saved at'],
      caption: 'Every saved change',
      rows: cardsByDoc.flatMap(({ doc, cards }) => cards.map((x, i) => [
        x.id, doc.file, x.title, locationLabel(x.location), x.reason || 'Reason not recorded',
        technicalText(x.technical.status),
        `${humanText(x.human.status)}${x.human.reviewer ? ` — ${x.human.reviewer}` : ''}${x.human.at ? ` at ${x.human.at}` : ''}`,
        (doc.changes || [])[i]?.atIso || 'Time not recorded',
      ])),
    })
    H('Saved changes — full before and after', 2)
    blocks.push({ k: 'beforeAfter', items: all.map((x) => ({
      id: x.id, label: `${x.title} · ${locationLabel(x.location)}`, note: x.reason || '', before: x.before, after: x.after,
      location: x.location, technical: x.technical.status, human: x.human.status,
    })) })
  }

  H('What this report is, and is not')
  T('A record of the edits ACP saved and of the confirmations recorded in ACP. It is not a conformance determination or a certification. Ticks or signatures on a printed copy are not recorded decisions.', { size: 9, color: MUTED })

  return {
    docTitle: `Remediation Report${m.org ? ` — ${m.org}` : ''}`,
    filename: `mova-remediation-${md === 'full' ? 'evidence' : md}-report-${m.scanId || 'scan'}`,
    lang: 'en-US',
    mode: md,
    kind: 'remediation',
    identity: {
      scanId: m.scanId ?? null, file: null, sourceSha256: null, correctedSha256: null, artifactVersion: null,
      generatedAt: new Date().toISOString(), platformVersion: m.platformVersion ?? null, targetLevel: m.level || 'AA',
    },
    targetLevel: m.level || 'AA',
    cover: {
      title: 'Accessibility Remediation Report',
      subtitle: `${m.org || 'Document estate'} · WCAG 2.1 Level ${m.level || 'AA'}`,
      meta: [`${MODE_LABEL[md]}${m.generatedAt ? ` · generated ${m.generatedAt}` : ''}`, m.scanId ? `Scan ${m.scanId}` : null].filter(Boolean),
    },
    blocks,
  }
}
