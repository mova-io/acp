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
  buildComparison, buildComparisonFromFacts, findingCardsFromFacts, factsFindings,
  boundList, locationLabel, technicalText, humanText, verificationText, clippedNote,
  isHumanOutstanding, RESPONSE_NOTICE, sameScanHistoryText,
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
// A FINDING's own state (facts.findings[].state) — distinct from its criterion's outcome above.
const FINDING_STATE_TXT = {
  open: 'Open', unresolved: 'Unresolved', awaiting_review: 'Awaiting human review',
  resolved_verified: 'Resolved — verified by re-scan', unknown: 'Not recorded',
}

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
  .filter((x) => x && (x.verified === true || x.verification === 'verified'))
  .map((x) => scOfValue(x.sc) || scOfValue(x.rule_id ?? x.ruleId)).filter(Boolean))

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
// The remediation Reviewer packet's OVERALL card bound (S3). Full evidence has none.
export const REMEDIATION_REVIEWER_TOTAL_CAP = 300
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
  // `d.facts` is the server's report-facts document. When it is present it is the AUTHORITY for
  // every count below: the client re-shapes it and never re-derives a number the server declined to
  // record. Without it the report falls back to the coverage rows, which can support criterion-level
  // statements only — see findingsVerified/findingsRemaining, which stay null in that case.
  const facts = d.facts && typeof d.facts === 'object' ? d.facts : null
  const fid = facts?.identity || {}
  const acc = facts?.accounting || null
  const assessment = facts?.assessment || null
  const level = d.targetLevel || fid.targetLevel || 'AA'
  const fileName = d.file || fid.file || 'document'
  const fmt = fmtOfFile(fileName)
  // The document is identified ONCE, in full, by the identity table (and the cover subtitle and
  // running header the renderer builds from it). Prose refers to it by its file name: a 140-char
  // SharePoint path repeated in every sentence costs a one-page summary two lines per mention and
  // adds nothing — the identity table is the authority for which document this is.
  const shortName = String(fileName).split('/').filter(Boolean).pop() || fileName
  // Saved changes: BOTH the verified remediation_diff records and the applied-but-unverified ones.
  // The unverified changes are exactly what a human has to look at, so they are never filtered out.
  const factsChanges = facts && Array.isArray(facts.savedChanges) ? facts.savedChanges : null
  const allDiffs = (factsChanges || d.diffs || []).filter((x) => x && (x.before != null || x.after != null))
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
  // TECHNICAL completion only: every in-scope criterion passed, was attested, or had a saved edit
  // that cleared re-validation. Human confirmation is a separate question, answered below.
  const technicalReady = c.ready
  const generated = d.timestamp || d.date
  const generatedAt = d.identity?.generatedAt || d.generatedAt || new Date().toISOString()
  const artifact = d.artifact || {}
  const identity = {
    scanId: d.identity?.scanId ?? fid.scanId ?? d.scanId ?? null,
    file: d.identity?.file ?? fid.file ?? d.file ?? null,
    sourceChecksum: d.identity?.sourceChecksum ?? fid.sourceChecksum ?? null,
    sourceChecksumKind: d.identity?.sourceChecksumKind ?? fid.sourceChecksumKind ?? null,
    sourceSha256: d.identity?.sourceSha256 ?? fid.sourceSha256 ?? artifact.sourceSha256 ?? null,
    correctedSha256: d.identity?.correctedSha256 ?? fid.correctedSha256 ?? artifact.correctedSha256 ?? null,
    currentArtifact: d.identity?.currentArtifact ?? fid.currentArtifact ?? null,
    artifactVersion: d.identity?.artifactVersion ?? null,
    generatedAt,
    platformVersion: d.identity?.platformVersion ?? fid.platformVersion ?? d.platformVersion ?? null,
    targetLevel: level,
    // Required by the server render route: it recomputes the digest and answers 409 when the facts
    // this model was built from are no longer current.
    factsDigest: d.identity?.factsDigest ?? facts?.factsDigest ?? null,
  }
  // The bytes a reviewer decision is measured against. With facts this is the server's
  // currentArtifact and nothing else — falling back to the SOURCE checksum for a file that has a
  // corrected copy would silently bind (or un-stale) a decision against bytes nobody reviewed.
  // A reviewer decision binds to the CORRECTED copy and to nothing else. When no corrected digest
  // is recorded there is no artifact to measure a decision against, so every review reads
  // "freshness unknown" — never accepted, and never matched against the SOURCE checksum, which
  // identifies bytes the reviewer did not look at.
  // `currentArtifact.kind` 'source' or 'unknown' both mean there is no corrected copy this decision
  // could have been made against — the first because none exists, the second because the server
  // cannot say which bytes are current. Either way the answer is "unknown", not the source hash.
  const currentSha = facts
    ? (identity.currentArtifact
      ? (identity.currentArtifact.kind === 'corrected'
        ? (identity.currentArtifact.sha256 ?? identity.correctedSha256 ?? null)
        : null)
      : (identity.correctedSha256 ?? null))
    : (artifact.currentSha256 ?? identity.correctedSha256 ?? identity.sourceSha256 ?? null)
  const tone = (good) => (good ? { color: GREEN, bg: '#EEF5E8' } : { color: AMBER, bg: '#FBF1DF' })

  // Evidence counts. Null means the evidence was not supplied — rendered "Not recorded", never 0.
  // A diff list that could not be READ (d.diffsError) is not an empty list.
  // The applied-but-unverified records are the ones a human still has to look at. When the server
  // could not read them, their number is UNKNOWN — and an unknown number of pending reviews must
  // never render as "none outstanding", which is exactly what an empty list would have said.
  const unverifiedUnavailable = facts != null && facts.savedChangesUnverifiedSource === 'unavailable'
  const unverifiedUnreadableNote = unverifiedUnavailable
    ? 'The saved changes awaiting review could not be read, so this report cannot say how many are outstanding. Absence from the list below is not evidence that there are none.'
    : null
  const diffsError = typeof d.diffsError === 'string' && d.diffsError ? d.diffsError : null
  const diffsKnown = (factsChanges != null || Array.isArray(d.diffs)) && !diffsError
  const diffsComplete = (facts ? facts.savedChangesComplete !== false : d.diffsComplete !== false) && !diffsError
  const diffsTotal = facts
    ? (Number.isFinite(facts.savedChangesTotal) ? facts.savedChangesTotal : allDiffs.length)
    : (Number.isFinite(d.diffsTotal) ? d.diffsTotal : (diffsKnown && diffsComplete ? allDiffs.length : null))
  // The store CLIPS before/after when it records them and keeps no untruncated copy, so a clipped
  // value can only be disclosed — never pointed at "the full evidence report", which does not have it.
  const storeCap = facts
    ? (Number.isFinite(facts.limits?.valueMaxChars) ? facts.limits.valueMaxChars : null)
    : (Number.isFinite(d.diffValueCap) ? d.diffValueCap : null)
  const reviewsError = typeof d.reviewsError === 'string' && d.reviewsError ? d.reviewsError : null
  const previewReason = d.previewStatus && typeof d.previewStatus.reason === 'string' ? d.previewStatus.reason : null
  const editsSaved = diffsTotal != null && !(diffsTotal === 0 && (fixedPN + fixedVN) > 0) ? diffsTotal : null
  const reviewsSource = facts ? facts.reviews : d.reviews
  const reviewsLoaded = reviewsSource != null && typeof reviewsSource === 'object'
  const cardClamp = mode !== 'summary'
  const changeCards = buildChangeCards({
    file: fileName, diffs: allDiffs, reviews: reviewsLoaded ? reviewsSource : null, previews: d.previews,
    currentSha256: currentSha, locationHref: d.locationHref, clamp: cardClamp,
    valueMaxChars: storeCap, previewProvenance: d.previewProvenance ?? null,
    fullRefPrefix: mode === 'full' ? '' : 'Full evidence report · record ',
  }).map((x) => ({ ...x, beforeStoredClipped: x.valueClipped, afterStoredClipped: x.valueClipped }))
  const storedClippedN = changeCards.filter((x) => x.valueClipped).length
  const storedClipNote = storedClippedN
    ? `${plural(storedClippedN, 'change has', 'changes have')} a value the store clipped when it recorded it. ${clippedNote(storeCap)}`
    : null

  // ── Changes, findings and criteria are THREE different counts ─────────────────────────────
  // Nothing below lets one stand in for another. A criterion-wide `verified` flag says a criterion
  // cleared its re-scan; it is not evidence about how many FINDINGS of that criterion were resolved.
  const changesVerifiedN = acc && Number.isFinite(acc.savedChangesVerified)
    ? acc.savedChangesVerified : changeCards.filter((x) => x.verification === 'verified').length
  const changesUnverifiedN = acc && Number.isFinite(acc.savedChangesUnverified)
    ? acc.savedChangesUnverified : changeCards.filter((x) => x.verification === 'not_verified').length
  // Resolution may be credited per finding ONLY from a per-finding ledger. Without one the answer is
  // "Not recorded" — which is the honest reading of one saved change against two findings.
  // Criteria counted from the CHANGE records themselves, not from coverage rows. Deriving it from
  // rows produced "Saved changes verified by re-scan: 30 · Criteria whose saved edit cleared
  // re-validation: 0" — true of the rows (the criterion still FAILs on other findings) and
  // nonsense beside the change count. These two numbers must describe the same records.
  const verifiedChangeCriteriaN = new Set(changeCards
    .filter((x) => x.verification === 'verified').map((x) => x.criterion).filter(Boolean)).size
  const ledger = acc?.resolutionLedger ?? (facts ? 'none' : null)
  const findingsVerified = ledger === 'per_finding'
    ? (Number.isFinite(acc?.findingsResolvedVerified) ? acc.findingsResolvedVerified : null)
    : null
  const fixedFindingsN = countIssues(c.fixedVerified) + countIssues(c.fixedPending)
  // Remaining findings come from the CURRENT assessment's finding list, never from catalog rows and
  // never by subtracting a criterion's saved changes from its finding count.
  const findingsRemaining = facts
    ? (Number.isFinite(acc?.findingsOpen) ? acc.findingsOpen : null)
    : (anyUncounted(c.fail) || fixedFindingsN > 0 ? null : countIssues(c.fail))
  // "Are the findings accounted for finding by finding?" — the question the ready wording depends on.
  // With facts, the coverage rows are irrelevant: what matters is whether the server holds a ledger,
  // and whether there is anything to attribute (no saved changes, or no findings, and the question
  // does not arise). Reading it off the rows said "attributed" for a document whose facts carried
  // three findings and two unledgered saved changes, because the caller passed no rows at all.
  const findingsAttributed = ledger === 'per_finding'
    || (facts
      ? (allDiffs.length === 0 || (acc?.findingsTotal ?? (facts.findings || []).length) === 0)
      : fixedFindingsN === 0)

  const factsFindingList = facts ? factsFindings(facts, { locationHref: d.locationHref }) : null
  const findingCards = facts
    ? findingCardsFromFacts(facts, { assignee: d.assignee, locationHref: d.locationHref })
    : buildFindingCards({ file: fileName, rows, assignee: d.assignee, locationHref: d.locationHref })

  // ── Human confirmation: separate from technical verification, and never assumed ───────────
  const byHuman = (s) => changeCards.filter((x) => x.human.status === s).length
  const humanOutstandingCards = changeCards.filter((x) => isHumanOutstanding(x.human.status))
  const confirmedN = changeCards.filter((x) => x.human.confirmed).length
  const decided = confirmedN + byHuman('correction_requested') + byHuman('rejected') + byHuman('unable')
  const staleN = byHuman('stale')
  const awaitingN = byHuman('pending')
  // Decisions were never loaded: unknown, which is not the same as "nobody has anything to do".
  const humanUnknown = !reviewsLoaded && changeCards.length > 0
  const humanOutstandingN = humanOutstandingCards.length
  const humanParts = [
    awaitingN ? `${plural(awaitingN, 'saved change', 'saved changes')} awaiting confirmation` : null,
    byHuman('correction_requested') ? `${byHuman('correction_requested')} with a correction requested (proposed only — not applied)` : null,
    byHuman('rejected') ? `${byHuman('rejected')} rejected by a reviewer` : null,
    byHuman('unable') ? `${byHuman('unable')} a reviewer could not verify` : null,
    staleN ? `${staleN} whose decision is stale because the file changed afterwards` : null,
    byHuman('freshness_unknown') ? `${byHuman('freshness_unknown')} accepted against a file version that cannot be confirmed` : null,
  ].filter(Boolean)
  const hitlN = Array.isArray(d.hitl) ? d.hitl.length : null

  const blocks = []
  const H = (text, lvl = 1) => blocks.push({ k: 'heading', text, level: lvl })
  const T = (text, o) => blocks.push({ k: 'text', text, o: o || {} })

  // ── Decision summary ────────────────────────────────────────────────────────────────────
  H('Decision summary')
  // "Documents assessed" is a fact about the ASSESSMENT, not about the coverage catalog: catalog
  // rows exist for documents a scan never opened, and `rows.length ? 1 : 0` reported those as
  // assessed. 'not_assessed' / 'error' / 'partial' with no findings is never "no findings".
  const assessState = assessment ? (assessment.state ? String(assessment.state) : 'unknown') : null
  const stateWhy = assessment && assessment.stateReason ? ` (${assessment.stateReason})` : ''
  let assessedValue = null
  let assessedDetail = 'No assessment state was recorded for this document.'
  if (assessState === 'assessed') {
    assessedValue = 1
    assessedDetail = `Assessed${assessment.assessedAt ? ` ${assessment.assessedAt}` : ''} · ${inScopeN ? `${inScopeN} in-scope criteria ` : ''}(Level ${level})`
  } else if (assessState === 'partial') {
    assessedValue = 1
    assessedDetail = `PARTIAL — ${(facts.findings || []).length} of ${assessment.findingsTotal ?? 'an unknown number of'} findings listed${stateWhy}. An absent finding is not evidence there is none.`
  } else if (assessState === 'not_assessed') {
    assessedValue = 0
    assessedDetail = `This document has NOT been assessed${stateWhy}. A coverage row is not an assessment, and an empty finding list here does not mean the document has no findings.`
  } else if (assessState === 'error') {
    assessedValue = 0
    assessedDetail = `The assessment did not complete${stateWhy}. No finding list can be read from a failed assessment.`
  } else if (assessState) {
    assessedDetail = `Assessment state not recorded${stateWhy}.`
  } else {
    // Legacy input: only evidence of an actual assessment counts, never the length of the catalog.
    const evidence = d.score != null || rows.some((r) => r.outcome && r.outcome !== 'UNCHECKED')
    assessedValue = evidence ? 1 : null
    assessedDetail = evidence
      ? `${inScopeN} in-scope criteria at WCAG 2.1 Level ${level}`
      : 'No assessment state was supplied with this report, and coverage rows alone do not show that this document was opened.'
  }

  const outstanding = [
    failN ? `${plural(findingsRemaining ?? failN, 'open finding', 'open findings')}${findingsRemaining == null ? ' (criteria)' : ''}` : null,
    fixedPN ? `${crit(fixedPN)} with a saved edit awaiting re-validation` : null,
    humanN ? `${crit(humanN)} needing a human check` : null,
    uncheckedN ? `${crit(uncheckedN)} not checked` : null,
    !findingsAttributed ? `${fixedFindingsN} finding${fixedFindingsN === 1 ? '' : 's'} recorded on criteria with a saved edit, which no per-finding record accounts for` : null,
  ].filter(Boolean)
  // Ready = technically complete AND every saved change humanly confirmed AND the findings actually
  // accounted for one by one. Any of the three missing and the report says what is outstanding.
  const ready = technicalReady && changesUnverifiedN === 0 && !unverifiedUnavailable
    && !humanUnknown && humanOutstandingN === 0 && findingsAttributed
  // Saved changes are stated as their own fact. An AI edit that no re-scan has checked is
  // outstanding work even when every coverage row reads clean, so it is never silently absorbed
  // into the criteria sentence (and when there are no in-scope rows at all, it is all we have).
  const changesSentence = unverifiedUnavailable
    ? ` Saved changes: ${changesVerifiedN} verified by re-scan; the applied-but-unverified records COULD NOT BE READ, so the number awaiting review is unknown.`
    : changeCards.length
      ? ` Saved changes: ${changesVerifiedN} verified by re-scan, ${changesUnverifiedN} applied by AI but not verified by a re-scan.`
      : ''
  const humanSentence = unverifiedUnavailable
    ? 'Human confirmation: UNKNOWN — the changes awaiting review could not be read, so this report cannot say what is confirmed.'
    : !changeCards.length
      ? 'Human confirmation: no saved changes require a reviewer decision.'
    : humanUnknown
      ? `Human confirmation: UNKNOWN — reviewer decisions were not loaded${reviewsError ? ` (${reviewsError})` : ''}, so none of the ${changeCards.length} saved changes can be reported as confirmed.`
      : humanOutstandingN
        ? `Human confirmation: outstanding — ${humanParts.join('; ')} (${confirmedN} of ${changeCards.length} confirmed).`
        : `Human confirmation: all ${plural(changeCards.length, 'saved change is', 'saved changes are')} confirmed by a reviewer against the current file version.`
  // With no coverage rows the report cannot speak about criteria — but the facts still know what
  // the assessment found, and "no in-scope criteria were evaluated" is FALSE for a document the
  // server reports as assessed with findings. Say what is actually recorded.
  const technicalSentence = technicalReady
    ? `Technical checks: nothing outstanding among the ${inScopeN} in-scope WCAG 2.1 Level ${level} criteria ACP checked — each passed, was resolved by a recorded human attestation, or had a saved edit that cleared re-validation.`
    : inScopeN === 0
      ? (facts && assessState === 'assessed'
        ? `Technical checks: the per-criterion coverage table is not part of this report; the current assessment records ${findingsRemaining == null ? 'an unrecorded number of' : findingsRemaining} open finding${findingsRemaining === 1 ? '' : 's'} for "${shortName}".`
        : `Technical checks: no in-scope criteria were evaluated for "${shortName}".`)
      : `Technical checks: ${outstanding.join('; ')}.`
  const nextStep = ready
    ? 'Next step: nothing outstanding in this record — publish, or re-assess if the document has changed since.'
    : `Next step: ${humanOutstandingN || humanUnknown ? 'record a reviewer decision for every saved change, then ' : ''}work through the remaining findings and re-validate. The Reviewer packet lists each one.`
  blocks.push({
    k: 'callout',
    text: ready
      ? `No outstanding items for "${shortName}" among the ${inScopeN} in-scope WCAG 2.1 Level ${level} criteria ACP checked. ${technicalSentence}${changesSentence} ${humanSentence} ${nextStep}`
      : inScopeN === 0 && !changeCards.length
        ? `No in-scope criteria were evaluated for "${shortName}", so this report cannot support a publication decision. ${nextStep}`
        : `Outstanding before publication of "${shortName}". ${technicalSentence}${changesSentence} ${humanSentence} ${nextStep}`,
    o: tone(ready),
  })
  blocks.push({
    k: 'decisionSummary',
    caption: 'Decision evidence',
    items: [
      { key: 'documentsAssessed', label: 'Documents assessed', value: assessedValue, detail: assessedDetail },
      { key: 'editsSaved', label: 'Edits saved', value: unverifiedUnavailable ? null : editsSaved,
        detail: unverifiedUnavailable
          ? `${changesVerifiedN} verified by re-scan. The applied-but-unverified records could not be read, so the total is not recorded.`
          : editsSaved == null
          ? (diffsError ? `Saved-edit records could not be read: ${diffsError}`
            : (fixedPN + fixedVN) ? `Edits were recorded for ${crit(fixedPN + fixedVN)}, but no per-change record was available` : 'Saved-edit records were not loaded')
          : `${changesVerifiedN} verified by re-scan; ${changesUnverifiedN} applied but NOT verified${changeCards.length - changesVerifiedN - changesUnverifiedN > 0 ? `; ${changeCards.length - changesVerifiedN - changesUnverifiedN} with no recorded verification status` : ''}${diffsComplete ? '' : ` · Partial: ${allDiffs.length} of ${diffsTotal} records loaded`}` },
      { key: 'findingsVerifiedResolved', label: 'Findings verified resolved', value: findingsVerified,
        // THREE counts, each named in its own unit and never spanned across another. Phrasing one
        // as "N changes across M criteria" produced "one verified change across zero criteria" when
        // a verified change belonged to a criterion the coverage rows did not carry as FIXED.
        detail: ledger === 'per_finding'
          ? `Counted finding by finding from the resolution ledger. Saved changes verified by re-scan: ${changesVerifiedN}.`
          : `NOT RECORDED — ${acc?.accountingReason || 'no per-finding record links a saved change to the finding it resolved'}. `
            + `Saved changes verified by re-scan: ${changesVerifiedN}. `
            + `Criteria those changes belong to: ${verifiedChangeCriteriaN}. `
            + 'Findings are a third count, with no evidence here.' },
      { key: 'findingsRemaining', label: 'Findings remaining', value: findingsRemaining,
        detail: findingsRemaining == null
          // The server's reason is stated once, on the row above; repeating it here cost the
          // one-page summary three lines and said nothing new.
          ? (facts
            ? (assessState && assessState !== 'assessed'
              ? `Not recorded — the assessment state is "${assessState}", so no open-finding count can be read from it. Zero is not the answer.`
              : 'The current assessment did not report an open-finding count (see the row above).')
            : `NOT RECORDED — ${fixedFindingsN} finding${fixedFindingsN === 1 ? '' : 's'} sit on criteria with a saved edit and no per-finding record says which of them the edit resolved.`)
          : facts
            ? `From the current assessment's finding list${assessment && assessment.findingsComplete === false ? ' (PARTIAL — the list is not complete)' : ''}`
            : `On ${crit(failN)} with open findings` },
      { key: 'humanChecksPending', label: 'Human checks pending', value: unverifiedUnavailable ? null : humanN + humanOutstandingN,
        detail: unverifiedUnavailable
          ? `Not recorded — the changes awaiting review could not be read. ${crit(humanN)} a person must verify from the coverage rows.`
          : `${crit(humanN)} a person must verify${attestedN ? `; ${attestedN} already attested` : ''}${changeCards.length ? `; ${humanOutstandingN} of ${changeCards.length} saved changes await a reviewer decision${humanUnknown ? ' (decisions were not loaded, so none count as confirmed)' : ''}` : ''}` },
      { key: 'checksNotPerformed', label: 'Checks not performed', value: uncheckedN,
        detail: 'No automated check for this file type and no recorded human result — not counted as passing' },
    ],
  })
  if (atLeast('reviewer')) blocks.push({
    k: 'stageStrip',
    items: [
      { key: 'suggestions', label: 'Suggestions', value: hitlN, status: hitlN == null ? 'unknown' : hitlN > 0 ? 'done' : 'not_started',
        detail: hitlN == null ? 'AI and rule suggestions were not loaded into this report' : `${plural(hitlN, 'suggestion', 'suggestions')} recorded for review` },
      { key: 'savedEdits', label: 'Saved edits', value: editsSaved, status: statusOfStage(editsSaved),
        detail: editsSaved == null ? 'Not recorded' : `${plural(editsSaved, 'change', 'changes')} written to the corrected copy` },
      { key: 'technicalChecks', label: 'Technical re-checks', value: changesVerifiedN,
        status: unverifiedUnavailable ? 'unknown' : changesUnverifiedN || fixedPN ? 'pending' : changesVerifiedN || fixedVN ? 'done' : 'not_started',
        detail: `${plural(changesVerifiedN, 'saved change', 'saved changes')} verified by re-scan; ${changesUnverifiedN} applied but not verified${fixedPN ? `; ${crit(fixedPN)} awaiting re-validation` : ''}` },
      { key: 'humanConfirmation', label: 'Human confirmation', value: unverifiedUnavailable ? null : reviewsLoaded ? confirmedN : null,
        status: unverifiedUnavailable ? 'unknown' : !changeCards.length ? 'not_started' : !reviewsLoaded ? 'unknown' : humanOutstandingN ? 'pending' : 'done',
        detail: unverifiedUnavailable ? 'The changes awaiting review could not be read'
          : !changeCards.length ? 'No saved changes to confirm'
          : !reviewsLoaded ? (reviewsError ? `Reviewer decisions could not be read: ${reviewsError}` : 'Reviewer decisions were not loaded into this report')
          : `${confirmedN} of ${changeCards.length} changes confirmed; ${decided} decided${humanOutstandingN ? ` · outstanding: ${humanParts.join('; ')}` : ''}` },
      { key: 'publication', label: 'Publication', value: null,
        status: d.publishedAt ? 'done' : d.publishedAt === null ? 'not_started' : 'unknown',
        detail: d.publishedAt ? `Published ${d.publishedAt}` : d.publishedAt === null ? 'Not published' : 'Publication status not recorded in this report' },
    ],
  })
  // Summary is ONE page: the counts above, what is outstanding, the next action, and identity +
  // scope/comparison below. The per-criterion breakdown belongs to the Reviewer packet.
  if (atLeast('reviewer')) {
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
      changeCards.length ? `${changesVerifiedN} of ${changeCards.length} saved changes verified by re-scan; ${changesUnverifiedN} applied by AI but not verified` : null,
      changeCards.length ? (humanUnknown ? 'Reviewer decisions were not loaded, so no saved change can be reported as confirmed' : `${confirmedN} of ${changeCards.length} saved changes confirmed by a reviewer`) : null,
      ready ? 'No outstanding items: the evidence supports a publication decision.' : 'Next step: work through Remaining work and Changes to confirm, then re-validate.',
    ].filter(Boolean),
    o: {},
  })
  T(`Assessment score: ${d.score != null ? `${d.score}/100` : NR} — a secondary indicator. The counts above are the decision evidence.`, { size: 9, color: MUTED })
  }

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
      ['Source checksum', `${orNR(identity.sourceSha256 ?? identity.sourceChecksum)}${identity.sourceChecksumKind ? ` (${identity.sourceChecksumKind})` : ''}`],
      ['Corrected copy SHA-256', orNR(identity.correctedSha256)],
      ['Version reviewed', identity.currentArtifact
        ? `${identity.currentArtifact.kind === 'corrected' ? 'Corrected copy' : identity.currentArtifact.kind === 'source' ? 'Original' : 'Unknown'} · ${orNR(identity.currentArtifact.sha256)}`
        : NR],
      ['Artifact version', orNR(identity.artifactVersion)],
      ['Target', `WCAG 2.1 Level ${level}`],
      ['Report generated', orNR(identity.generatedAt)],
      ['Platform version', orNR(identity.platformVersion)],
      ['Report mode', MODE_LABEL[mode]],
    ],
    widths: [150, CW - 150],
  })

  // ── Since the previous assessment ───────────────────────────────────────────────────────
  // Only a REAL comparable snapshot — same document identity, same scope, findings itemised on both
  // sides — produces a comparison. Aggregate counts are never subtracted to imply a change: "12 last
  // time, 9 now" says nothing about WHICH findings, and a different scope explains it just as well.
  H('Since the previous assessment')
  const scopeParts = [
    `WCAG 2.1 Level ${level}`,
    `${inScopeN} in-scope criteria`,
    oosN ? `${crit(oosN)} recorded out of scope` : null,
    facts && fid.scopeDigest ? `scope digest ${String(fid.scopeDigest).slice(0, 12)}` : null,
    facts && fid.scanScope ? `scan scope ${fid.scanScope}` : null,
  ].filter(Boolean)
  // One line. The comparison block immediately below already states why a snapshot is or is not
  // comparable, so repeating the rule here costs a line the summary page does not have.
  T(`Scope of this assessment: ${scopeParts.join(' · ')}.${atLeast('reviewer') ? ' A comparison is only reported against a snapshot of the same document with the same scope.' : ''}`, { size: 9, color: MUTED })
  let comparison
  if (facts) {
    comparison = buildComparisonFromFacts(facts, { locationHref: d.locationHref })
  } else {
    const currentFindings = c.fail.concat(c.fixedPending).some((r) => !(r.fileIssues || []).length && (r.count || 0) > 0)
      ? null
      : rows.filter((r) => r.outcome === 'FAIL' || (r.outcome === 'FIXED' && !c.isVerified(r)) || (r.fileIssues || []).some((i) => i.severity === 'REVIEW'))
        .flatMap((r) => r.fileIssues || [])
    // Current findings come from the SAME server record shape the previous snapshot was built
    // from (d.currentFindings, stream C), so ids match; the rows are only the fallback.
    comparison = buildComparison(d.previous, {
      file: identity.file,
      scope: d.scope === undefined ? { targetLevel: level } : d.scope,
      findings: Array.isArray(d.currentFindings) ? d.currentFindings : currentFindings,
    })
    if (!d.previous && typeof d.previousReason === 'string' && d.previousReason.trim()) comparison.reason = d.previousReason.trim()
  }
  blocks.push(comparison)
  // C6 (R-B2): the assessment a re-assessment REPLACED inside this same scan. A compared or
  // refused snapshot is stated from the Reviewer packet up; "none recorded" / "cannot be read" is
  // the common case and is stated in Full evidence — worded so it never reads as "assessed once".
  const sameScan = facts && facts.sameScanHistory && typeof facts.sameScanHistory === 'object' ? facts.sameScanHistory : null
  if (sameScan) {
    const quiet = sameScan.status === 'not_recorded' || sameScan.status === 'not_available'
    if (quiet ? atLeast('full') : atLeast('reviewer')) {
      T(sameScanHistoryText(sameScan), { size: 9, color: sameScan.status === 'compared' ? undefined : MUTED })
    }
  }

  // ── Reviewer packet ─────────────────────────────────────────────────────────────────────
  if (atLeast('reviewer')) {
    H('Changes to confirm')
    if (changeCards.length) {
      T(`${plural(changeCards.length, 'saved change', 'saved changes')} recorded for this file: ${changesVerifiedN} verified by re-scan and ${changesUnverifiedN} applied by AI but NOT verified. Technical verification (the re-scan) and human confirmation are separate: a verified change can still be wrong in context, and an unverified one has had neither check.${diffsComplete ? '' : ` Partial: ${allDiffs.length} of ${diffsTotal ?? 'an unknown number of'} change records were available.`}`, { size: 9, color: MUTED })
      blocks.push({ k: 'callout', text: RESPONSE_NOTICE, o: { color: PLUM } })
      if (!reviewsLoaded) T(reviewsError ? `Reviewer decisions could not be read (${reviewsError}), so every change below shows as awaiting confirmation.` : 'Reviewer decisions were not loaded, so every change below shows as awaiting confirmation.', { color: AMBER })
      if (unverifiedUnreadableNote) T(unverifiedUnreadableNote, { bold: true, color: AMBER })
      if (storedClipNote) T(storedClipNote, { color: AMBER })
      if (previewReason) T(previewReason, { size: 9, color: MUTED })
      const cap = mode === 'full' ? null : REVIEWER_CARD_CAP
      const b = boundList(changeCards, cap)
      b.shown.forEach((x) => blocks.push(x))
      if (b.omitted) T(`${b.omitted} more saved change${b.omitted === 1 ? '' : 's'} (of ${b.total}) are listed in the Full evidence report.`, { bold: true })
    } else if (unverifiedUnreadableNote) {
      T(unverifiedUnreadableNote, { bold: true, color: AMBER })
    } else if (diffsError) {
      T(`Saved changes could not be read: ${diffsError} Nothing is listed here for that reason — not because there are none.`, { color: AMBER })
    } else if (fixedPN + fixedVN > 0) {
      T(`Edits were recorded for ${crit(fixedPN + fixedVN)}, but no per-change before/after record is available, so there is nothing itemised to confirm.`, { color: AMBER })
    } else {
      T('No saved changes are recorded for this file.', { color: MUTED })
    }

    // C11: review-queue approvals whose pin moved (source revision, value or proposals changed
    // after the approval). Stated as a state of the record — nothing here re-queues them.
    const approvals = facts && facts.approvals && typeof facts.approvals === 'object' ? facts.approvals : null
    if (approvals && approvals.source === 'unavailable') {
      H('Approvals that need a recheck', 2)
      T('The review-queue approvals for this document could not be read, so this report cannot say whether any needs a recheck.', { color: AMBER })
    } else if (approvals && Array.isArray(approvals.recheckRequired) && approvals.recheckRequired.length) {
      H('Approvals that need a recheck', 2)
      T(`${plural(approvals.recheckRequired.length, 'approval', 'approvals')} in the review queue ${approvals.recheckRequired.length === 1 ? 'was' : 'were'} recorded against a source version or proposed value that has since changed. ${approvals.note || ''}`.trim(), { color: AMBER })
      blocks.push({
        k: 'bullets',
        items: approvals.recheckRequired.map((a) => `${a.sc || a.ruleId || 'Criterion not recorded'}${a.ruleName ? ` · ${a.ruleName}` : ''} — approved ${a.reviewedAt || 'at a time not recorded'}${a.approvedSourceRevision ? ` against revision ${String(a.approvedSourceRevision).slice(0, 12)}` : ''} (queue item ${a.id})`),
        o: {},
      })
    }
    // C7: decisions recorded on EARLIER scans of this document are evidence about the bytes
    // reviewed then; they are never carried forward, and when they cannot be listed that is said.
    if (atLeast('full') && facts && 'priorDecisions' in facts) {
      if (Array.isArray(facts.priorDecisions) && facts.priorDecisions.length) {
        H('Decisions recorded on earlier scans', 2)
        T(facts.priorDecisionsReason || 'Recorded against earlier scans of this document; not carried forward.', { size: 9, color: MUTED })
        blocks.push({ k: 'bullets', items: facts.priorDecisions.map((p) => `${p.verdictLabel || p.verdict || 'Decision'} on ${p.changeId || 'a change'} in scan ${p.scanId || NR}${p.at ? ` at ${p.at}` : ''}${p.reviewer ? ` by ${p.reviewer}` : ''} — not carried forward`), o: {} })
        // R-B3 bounds: a page of the history is never presented as the whole of it.
        const bounds = facts.priorDecisionsBounds
        if (bounds && bounds.truncated) {
          T(`Showing ${facts.priorDecisions.length} of ${bounds.total ?? 'more'} earlier decisions (newest first); the rest are not listed here.`, { bold: true })
        }
      } else if (facts.priorDecisionsReason) {
        // None (not readable / not available) and [] (none recorded) are different answers; the
        // reason says which, and neither is silently dropped.
        T(facts.priorDecisionsReason, { size: 9, color: MUTED })
      }
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
    const catOf = (cat) => catAgg[cat] || (catAgg[cat] = { fail: 0, human: 0, pending: 0, unchecked: 0, unverified: 0, confirm: 0, ok: 0 })
    rows.forEach((r) => {
      if (r.disposition?.kind === 'out_of_scope') return
      const a = catOf(CAT_OF(r.id))
      if (r.outcome === 'FAIL') a.fail++
      else if (r.outcome === 'HUMAN' && !r.disposition) a.human++
      else if (r.outcome === 'FIXED' && !c.isVerified(r)) a.pending++
      else if (r.outcome === 'UNCHECKED' && !r.disposition) a.unchecked++
      else a.ok++
    })
    // A criterion whose rows all read clean can still carry an unverified edit or an unconfirmed
    // reviewer decision. "✓ No outstanding items" over an area that holds one of those is the same
    // kind of overstatement as "Pass" over an unchecked criterion.
    changeCards.forEach((x) => {
      const a = catOf(CAT_OF(x.criterion || ''))
      if (x.verification === 'not_verified') a.unverified++
      if (isHumanOutstanding(x.human.status) || !reviewsLoaded) a.confirm++
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
          a.unverified ? `… ${a.unverified} AI edit(s) not verified` : null,
          a.confirm ? `◐ ${a.confirm} change(s) awaiting a reviewer decision` : null,
        ].filter(Boolean)
        return [cat, parts.length ? parts.join(' · ')
          : unverifiedUnavailable ? '— changes awaiting review could not be read'
          : '✓ No outstanding items']
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
        x.verification ? verificationText(x.verification) : technicalText(x.technical.status),
        `${humanText(x.human.status)}${x.human.reviewer ? ` — ${x.human.reviewer}` : ''}${x.human.at ? ` at ${x.human.at}` : ''}`,
        appliedAt[x.criterion] || NR,
        x.valueClipped ? `Clipped by the store at ${storeCap ?? 'its'} characters when recorded; the untruncated text was not kept` : 'Complete as stored',
      ]),
    })
    // With facts the findings appendix is the server's per-finding list, each row carrying that
    // finding's OWN state — not its criterion's outcome, which is what let one verified change read
    // as "every finding of 1.1.1 resolved".
    if (factsFindingList) {
      blocks.push({
        k: 'appendixTable',
        id: 'appendix-findings',
        complete: assessment?.findingsComplete !== false,
        totalRecords: Number.isFinite(assessment?.findingsTotal) ? assessment.findingsTotal : factsFindingList.length,
        limitNote: assessment?.findingsComplete === false ? 'the findings the assessment recorded for this request' : null,
        headers: ['Finding id', 'Criterion', 'Severity', 'Location', 'Detail', 'State of this finding', 'Why'],
        caption: 'All findings',
        rows: factsFindingList.map((i) => [
          i.id, `${i.sc || i.ruleId || NR}`, i.severity || NR, locationLabel(i.location), i.detail || NR,
          FINDING_STATE_TXT[i.state] || i.state || NR, i.stateReason || NR,
        ]),
      })
    } else {
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
          // A CRITERION outcome, said as one. It is not a statement about this finding.
          r.outcome === 'FIXED' ? (c.isVerified(r) ? 'Criterion fixed · verified (not a per-finding result)' : 'Criterion fixed · awaiting re-validation') : (COV_OUT_TXT[r.outcome] || r.outcome),
        ]),
      })
    }
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
  // Methodology and disclaimer prose belong to the Reviewer packet and Full evidence. A one-page
  // summary that spends a third of its page on them is not a summary — the identity table already
  // states the target, and the Full evidence report carries the standing.
  if (atLeast('reviewer')) {
    H('What this report is, and is not')
    blocks.push({
      k: 'callout',
      text: `This report records what ACP checked, changed and verified for "${fileName}" against the WCAG 2.1 Level ${level} criteria in this engagement's scope. It is not a conformance determination, a certification or legal advice. It can support an ADA, Section 508 or EN 301 549 / European Accessibility Act review as evidence, alongside a qualified human evaluation.`,
      o: tone(ready),
    })
    T(`Generated: ${generated || generatedAt}${d.platformVersion ? ` · Platform v${d.platformVersion}` : ''} · ${MODE_LABEL[mode]}`, { size: 9, color: MUTED, gapAfter: 4 })
    T(RESPONSE_NOTICE, { size: 8, color: MUTED, lh: 12 })
  }

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
    // The three things `ready` is made of, kept separate so a caller can never mistake one for
    // another: the re-scan, the reviewer, and whether findings are accounted for one by one.
    technicalReady,
    humanConfirmation: {
      loaded: reviewsLoaded,
      changes: changeCards.length,
      confirmed: confirmedN,
      outstanding: humanOutstandingN,
      correctionRequested: byHuman('correction_requested'),
      rejected: byHuman('rejected'),
      unable: byHuman('unable'),
      stale: staleN,
      freshnessUnknown: byHuman('freshness_unknown'),
      pending: awaitingN,
    },
    savedChanges: { verified: changesVerifiedN, unverified: changesUnverifiedN, total: changeCards.length },
    findingsAccounting: {
      ledger: ledger ?? 'none',
      resolvedVerified: findingsVerified,
      remaining: findingsRemaining,
      attributed: findingsAttributed,
    },
    assessmentState: assessState,
    factsDigest: identity.factsDigest,
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
                                        platformVersion = null, evidenceNotes = null,
                                        unitemisedDocuments = null, savedChangesVerified = null,
                                        savedChangesUnverified = null, itemisedChanges = null,
                                        filesIndexComplete = null, facts = null,
                                        factsDigest = null } = {}) {
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
    // S3: what remediationReportData gathered about the parts it could NOT itemise. Carried
    // through so the report states them rather than the gather's notes dying in the caller.
    evidenceNotes: Array.isArray(evidenceNotes) ? evidenceNotes.filter((x) => typeof x === 'string' && x) : [],
    unitemisedDocuments: Array.isArray(unitemisedDocuments) ? unitemisedDocuments : [],
    // Server totals over EVERY document (null = not recorded, never 0).
    serverTotals: {
      savedChangesVerified: Number.isFinite(savedChangesVerified) ? savedChangesVerified : null,
      savedChangesUnverified: Number.isFinite(savedChangesUnverified) ? savedChangesUnverified : null,
    },
    itemisedChanges: itemisedChanges && typeof itemisedChanges === 'object' ? itemisedChanges : null,
    filesIndexComplete,
    factsDigest: factsDigest ?? facts?.factsDigest ?? null,
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
  const verifiedLoaded = all.filter((x) => x.technical.status === 'verified').length
  const diffsPartial = m.diffsComplete === false
  const partial = m.partial != null || diffsPartial
  // S3: documents the gather could not itemise. Their changes are NOT in `all`, so every count
  // taken from `all` is a count of the itemised part only and is labelled as such; the server's
  // own totals (over every document) are preferred where it recorded them.
  const unitemised = Array.isArray(m.unitemisedDocuments) ? m.unitemisedDocuments : []
  const unitemisedSet = new Set(unitemised.map((d) => d.file))
  const st = m.serverTotals || {}
  const serverSaved = Number.isFinite(st.savedChangesVerified) && Number.isFinite(st.savedChangesUnverified)
    ? st.savedChangesVerified + st.savedChangesUnverified : null
  const savedTotal = diffsPartial ? (m.diffsTotal ?? serverSaved ?? null) : all.length
  const verified = diffsPartial ? (Number.isFinite(st.savedChangesVerified) ? st.savedChangesVerified : null) : verifiedLoaded

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
      { key: 'editsSaved', label: 'Edits saved', value: savedTotal, detail: diffsPartial ? `Partial: ${all.length} records itemised in this report${savedTotal != null ? ' (the total is the server index\'s own count over every document)' : '; the total is not recorded'}` : 'Per-change records' },
      { key: 'findingsVerifiedResolved', label: 'Changes verified by re-scan', value: verified, detail: diffsPartial ? `${verifiedLoaded} among the itemised records${verified != null ? '; the value is the server index\'s count over every document' : '; the total is not recorded'}` : 'Counted from the loaded change records' },
      { key: 'findingsRemaining', label: 'Findings remaining', value: null, detail: 'Not part of the remediation record — see the assessment report' },
      // An awaiting-confirmation count over a PARTIAL list is not the number awaiting: null.
      { key: 'humanChecksPending', label: 'Changes awaiting confirmation', value: decided == null || diffsPartial ? null : all.length - decided, detail: decided == null ? 'Reviewer decisions were not loaded' : diffsPartial ? `${all.length - decided} of the ${all.length} itemised changes await a decision; changes in documents not itemised are not counted here` : `${decided} of ${all.length} decided` },
      { key: 'checksNotPerformed', label: 'Criteria with no recorded time', value: totals.unstamped ?? null, detail: 'Times are shown only where the write was recorded' },
    ],
  })
  blocks.push({
    k: 'stageStrip',
    items: [
      { key: 'suggestions', label: 'Suggestions', value: null, status: 'unknown', detail: 'Not part of this report' },
      { key: 'savedEdits', label: 'Saved edits', value: savedTotal, status: savedTotal == null ? 'unknown' : savedTotal ? 'done' : 'not_started', detail: savedTotal == null ? 'Not recorded' : plural(savedTotal, 'change', 'changes') },
      { key: 'technicalChecks', label: 'Technical re-checks', value: verified, status: !all.length ? 'not_started' : verified == null ? 'unknown' : verified === savedTotal ? 'done' : 'pending', detail: `${verifiedLoaded} of ${all.length} loaded changes verified by re-scan${diffsPartial ? ' (partial list)' : ''}` },
      { key: 'humanConfirmation', label: 'Human confirmation', value: diffsPartial ? null : decided, status: decided == null || diffsPartial ? 'unknown' : !all.length ? 'not_started' : decided === all.length ? 'done' : 'pending', detail: decided == null ? 'Reviewer decisions were not loaded' : `${decided} of ${all.length} ${diffsPartial ? 'itemised changes ' : ''}decided` },
      { key: 'publication', label: 'Publication', value: null, status: 'unknown', detail: 'Publication status not recorded in this report' },
    ],
  })
  if (totals.unstamped) T(`${totals.unstamped} of ${totals.items} criteria carry no recorded time; they read "Time not recorded" rather than borrowing the document's timestamp.`, { size: 9, color: MUTED })
  if (m.partial != null) T(`PARTIAL: the server returned its maximum of ${m.partial} fix-time records, so later records are not listed.`, { bold: true, color: AMBER })
  if (diffsPartial) T(`PARTIAL: ${all.length} of ${savedTotal ?? 'an unknown number of'} change records were loaded.`, { bold: true, color: AMBER })
  ;(m.evidenceNotes || []).forEach((note) => T(note, { bold: true, color: AMBER }))

  if (atLeast('reviewer')) {
    H('Changes to confirm')
    blocks.push({ k: 'callout', text: RESPONSE_NOTICE, o: { color: PLUM } })
    // S3: the Reviewer packet is bounded OVERALL, not only per document — 300 documents × 100
    // cards was the same size as Full evidence. The bound is stated where it bites and in total.
    let budget = md === 'full' ? Infinity : REMEDIATION_REVIEWER_TOTAL_CAP
    let omittedOverall = 0
    const docsCut = []
    for (const { doc, cards } of cardsByDoc) {
      H(doc.name, 2)
      T(`${doc.dir ? `Folder: ${doc.dir}` : 'Folder: (root)'} · Format: .${doc.fmt} · ${doc.remediatedAt ? `Remediated ${doc.remediatedAt}` : 'Remediation time not recorded'}${doc.awaiting ? ` · ${doc.awaiting} item(s) awaiting review in ACP` : ''}`, { size: 9, color: MUTED })
      if (unitemisedSet.has(doc.file)) {
        const u = unitemised.find((x) => x.file === doc.file)
        T(`Not itemised in this report — ${u?.reason || 'its changes were not loaded'}. It is listed in the Full evidence appendix "Documents not itemised"; this is not a statement that it has no changes.`, { color: AMBER })
        continue
      }
      if (!cards.length) { T('This document was remediated, but no per-change record was stored for it.', { color: MUTED }); continue }
      const cap = md === 'full' ? null : Math.max(0, Math.min(REVIEWER_CARD_CAP, budget))
      const b = boundList(cards, cap)
      budget -= b.shown.length
      b.shown.forEach((x) => blocks.push(x))
      if (b.omitted) {
        omittedOverall += b.omitted
        if (b.shown.length < Math.min(REVIEWER_CARD_CAP, cards.length)) docsCut.push(doc.file)
        T(`${b.omitted} more change(s) for this document (of ${b.total}) are listed in the Full evidence report.`, { bold: true })
      }
    }
    if (md !== 'full' && omittedOverall) {
      T(`This Reviewer packet shows at most ${REMEDIATION_REVIEWER_TOTAL_CAP} change cards in total and ${REVIEWER_CARD_CAP} per document: ${all.length - omittedOverall} of ${all.length} itemised changes are shown${docsCut.length ? `, and ${docsCut.length} document(s) reached the overall limit` : ''}. Every change is in the Full evidence report.`, { bold: true, color: AMBER })
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
    if (unitemised.length) {
      // S3: EVERY document the report could not itemise, by name, with the server index's own
      // counts for it — so none is stranded, and none reads as "no changes".
      blocks.push({
        k: 'appendixTable',
        id: 'appendix-remediation-unitemised',
        complete: true,
        totalRecords: unitemised.length,
        limitNote: null,
        headers: ['Document', 'Why it is not itemised', 'Verified changes (server index)', 'Not verified (server index)', 'Decisions pending (server index)'],
        caption: 'Documents not itemised',
        rows: unitemised.map((u) => [u.file, u.reason || 'Not recorded',
          u.savedChangesVerified == null ? NR : String(u.savedChangesVerified),
          u.savedChangesUnverified == null ? NR : String(u.savedChangesUnverified),
          u.decisionsPending == null ? NR : String(u.decisionsPending)]),
      })
    }
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
      factsDigest: m.factsDigest ?? null,
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
