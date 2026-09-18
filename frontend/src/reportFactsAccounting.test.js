// Accounting truth for the report model, against the SERVER's report-facts document.
//
// Every test here pins one claim the report is not allowed to make. The numbered items match the
// correction round's brief and the parent's independent review; the first test is the parent's own
// reproduction, kept verbatim so a regression is recognisable as the same defect.
//
// The fixtures are shaped exactly like stream D's `GET /scans/{sid}/files/{file}/report-facts`
// output (contract v2 addendum): the same key names, the same id schemes, the same null-vs-zero
// conventions. A fixture that could not come out of the real store proves nothing.
import { describe, it, expect } from 'vitest'
import { buildFileReportModel } from './reportModel.js'
import { buildScanReportModel, aggregateScanReport } from './scanReport.js'
import { humanStatusOf, buildComparisonFromFacts, findingCardsFromFacts } from './reportEvidence.js'
import { reportHtmlFromModel } from './htmlReport.js'

const SHA_SRC = 'a'.repeat(64)
const SHA_COR = 'b'.repeat(64)
const SHA_OLD = 'c'.repeat(64)
const SCOPE = 's'.repeat(64)
const FILE = 'Reports/handbook.pdf'

const text = (model) => JSON.stringify([model.cover, model.blocks])
const blocksOf = (model, k) => model.blocks.filter((b) => b.k === k)
const decision = (model) => Object.fromEntries(blocksOf(model, 'decisionSummary')[0].items.map((i) => [i.key, i.value]))
const basis = (model, key) => blocksOf(model, 'decisionSummary')[0].items.find((i) => i.key === key).detail
const READY = /No outstanding items/

// A finding id as the server computes it: sha256("finding-report-v1"|scanId|file|ruleId|instanceKey)
// truncated to 32. The exact bytes do not matter here; the SHAPE does — 32 lowercase hex.
const fid = (n) => `${n}`.padStart(2, '0').repeat(16)
// Saved-change ids, both server shapes: verified `{file}::{ruleId}::{seq}` and applied-but-
// unverified `{file}::{ruleId}::u{16hex}`. Clients use the server's id verbatim.
const verifiedId = (rule, seq) => `${FILE}::${rule}::${seq}`
const unverifiedId = (rule, hex) => `${FILE}::${rule}::u${hex}`

const finding = (over = {}) => ({
  id: fid(1), ledgerFindingId: null, ruleId: 'SC_1_1_1', sc: '1.1.1',
  detail: 'Image has no description', severity: 'SERIOUS', recommendedAction: null,
  location: { label: 'Page 1', page: 1, slide: null, sheet: null, cell: null, element: null },
  state: 'open', stateReason: 'reported by the current assessment',
  ...over,
})

const savedChange = (over = {}) => ({
  id: verifiedId('SC_1_1_1', 0), ruleId: 'SC_1_1_1', sc: '1.1.1', seq: 0, locator: null,
  before: '', after: 'Image A description', note: 'AI-drafted alt text',
  verification: 'verified', verificationDetail: 'The post-fix re-scan no longer reported this finding.',
  artifactSha256: SHA_COR, valueClipped: false, changeDigest: 'd'.repeat(64),
  findingIds: null, source: 'remediation_diff',
  ...over,
})

const facts = (over = {}) => ({
  factsVersion: 1,
  factsDigest: 'f'.repeat(64),
  generatedAt: '2026-09-17T12:00:00Z',
  identity: {
    scanId: 'scan-1', file: FILE,
    sourceChecksum: SHA_SRC, sourceChecksumKind: 'sha256',
    sourceSha256: SHA_SRC, correctedSha256: SHA_COR,
    currentArtifact: { kind: 'corrected', sha256: SHA_COR },
    remediatedAt: '2026-09-16T09:00:00Z', platformVersion: '2026.9.1', targetLevel: 'AA',
    scopeDigest: SCOPE, scanScope: 'agreed-estate', rubricHash: 'r'.repeat(64),
  },
  assessment: {
    state: 'assessed', assessedAt: '2026-09-15T08:00:00Z', score: 72, engine: 'acp-1',
    artifactAssessed: 'source', findingsComplete: true, findingsTotal: 2, stateReason: null,
  },
  findings: [finding(), finding({ id: fid(2), detail: 'Image B has no description', location: { label: 'Page 2', page: 2, slide: null, sheet: null, cell: null, element: null } })],
  savedChanges: [savedChange()],
  savedChangesComplete: true, savedChangesTotal: 1, savedChangesLimit: 500,
  savedChangesUnverifiedSource: 'ok',
  reviews: {},
  accounting: {
    findingsTotal: 2, findingsOpen: 2, findingsResolvedVerified: null, resolutionLedger: 'none',
    savedChangesVerified: 1, savedChangesUnverified: 0,
    humanReviews: { pending: 1, accepted: 0, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 },
  },
  previous: null,
  previousReason: 'No earlier assessment of this document with the same scope is recorded.',
  limits: { valueMaxChars: 2000, savedChangesLimit: 500 },
  ...over,
})

const model = (f, over = {}) => buildFileReportModel({
  file: FILE, targetLevel: 'AA', mode: 'full', facts: f, rows: [], ...over,
})

// ── 1. Exact finding accounting ──────────────────────────────────────────────────────────────
describe('one change is never proof that two findings are resolved', () => {
  // The parent's reproduction, verbatim (16:40 UTC, against buildFileReportModel, mode summary).
  // It previously returned ready:true, findingsVerifiedResolved:2, findingsRemaining:0 and opened
  // with "No outstanding items". All four were wrong.
  const repro = () => buildFileReportModel({
    file: 'deck.pptx', targetLevel: 'AA', mode: 'summary', score: 65,
    rows: [{ id: '1.1.1', name: 'Non-text Content', plain: 'Images have alt text', level: 'A', fix: '⚡ auto',
      outcome: 'FIXED', count: 2,
      fileIssues: [{ detail: 'Missing description A', page: 1 }, { detail: 'Missing description B', page: 2 }] }],
    diffs: [{ rule_id: 'SC_1_1_1', seq: 0, before: '', after: 'Image A description', verified: true }],
    reviews: {},
  })

  it("the parent's repro: 2 findings, 1 change, 1 criterion — and no claim beyond the change", () => {
    const m = repro()
    const d = decision(m)
    expect(d.findingsVerifiedResolved).not.toBe(2)
    expect(d.findingsVerifiedResolved).toBeNull()      // "Not recorded", never a number
    expect(d.findingsRemaining).not.toBe(0)
    expect(d.findingsRemaining).toBeNull()
    expect(m.ready).toBe(false)
    expect(text(m)).not.toMatch(READY)
    expect(m.findingsAccounting).toMatchObject({ ledger: 'none', attributed: false })
  })

  it('the three counts are each stated in their own unit, never spanning one another', () => {
    const b = basis(repro(), 'findingsVerifiedResolved')
    expect(b).toMatch(/Saved changes verified by re-scan: 1\./)
    expect(b).toMatch(/Criteria those changes belong to: 1\./)
    expect(b).toMatch(/Findings are a third count/)
    // The contradiction the parent saw in a rendered summary: a change count spanning a criterion
    // count, which produced "one verified change across zero criteria".
    expect(b).not.toMatch(/across \d+ criteri/)
    expect(text(repro())).not.toMatch(/across 0 criteri/)
  })

  it('a verified change is never reported beside a criterion count of zero', () => {
    // The coverage row is FAIL (other findings remain), so the ROW-derived "criteria fixed and
    // verified" count is 0 — while one saved change is verified. Printing those two side by side
    // said "1 verified change · 0 criteria". Both numbers now describe the same change records.
    const m = buildFileReportModel({
      file: 'deck.pptx', targetLevel: 'AA', mode: 'full',
      rows: [{ id: '1.1.1', name: 'Non-text', plain: 'Alt text', level: 'A', outcome: 'FAIL', count: 1, fileIssues: [{ detail: 'no alt' }] }],
      diffs: [{ rule_id: 'SC_1_1_1', seq: 0, before: '', after: 'x', verified: true }],
      reviews: {},
    })
    const b = basis(m, 'findingsVerifiedResolved')
    expect(b).toMatch(/Saved changes verified by re-scan: 1\./)
    expect(b).toMatch(/Criteria those changes belong to: 1\./)
    expect(b).not.toMatch(/Criteria those changes belong to: 0\./)
    expect(text(m)).not.toMatch(/across 0 criteri/)
  })

  it('a nonzero change count is never printed beside a zero criterion count for the same records', () => {
    // The shape of the contradiction the parent saw in a rendered summary. Checked as an INVARIANT
    // over the two numbers rather than as one wording, so a future rephrasing still has to hold it.
    const worst = model(facts({
      savedChanges: Array.from({ length: 30 }, (_, i) => savedChange({ id: verifiedId('SC_1_1_1', i), seq: i })),
      savedChangesTotal: 30,
      accounting: { ...facts().accounting, savedChangesVerified: 30, savedChangesUnverified: 0 },
    }), { rows: [{ id: '1.1.1', name: 'Non-text', plain: 'Alt text', level: 'A', outcome: 'FAIL', count: 80 }] })
    const b = basis(worst, 'findingsVerifiedResolved')
    const changes = Number(b.match(/Saved changes verified by re-scan: (\d+)/)[1])
    const criteria = Number(b.match(/Criteria those changes belong to: (\d+)/)[1])
    expect(changes).toBe(30)
    if (changes > 0) expect(criteria).toBeGreaterThan(0)
    expect(criteria).toBeLessThanOrEqual(changes)
  })

  it('facts with no per-finding ledger: resolution is Not recorded, remaining comes from the assessment', () => {
    const m = model(facts())
    expect(decision(m).findingsVerifiedResolved).toBeNull()
    expect(decision(m).findingsRemaining).toBe(2)       // accounting.findingsOpen, not a subtraction
    expect(basis(m, 'findingsVerifiedResolved')).toMatch(/NOT RECORDED/)
    expect(m.savedChanges).toMatchObject({ verified: 1, unverified: 0 })
  })

  it('facts WITH a per-finding ledger credit exactly what the ledger says', () => {
    const f = facts({
      findings: [finding({ state: 'resolved_verified', ledgerFindingId: 'led-1', stateReason: 'the re-scan no longer reports it' }),
        finding({ id: fid(2), detail: 'Image B has no description' })],
      savedChanges: [savedChange({ findingIds: [fid(1)] })],
      accounting: {
        findingsTotal: 2, findingsOpen: 1, findingsResolvedVerified: 1, resolutionLedger: 'per_finding',
        savedChangesVerified: 1, savedChangesUnverified: 0,
        humanReviews: { pending: 1, accepted: 0, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 },
      },
    })
    const m = model(f)
    expect(decision(m).findingsVerifiedResolved).toBe(1)
    expect(decision(m).findingsRemaining).toBe(1)
    expect(m.findingsAccounting).toMatchObject({ ledger: 'per_finding', attributed: true })
    // The resolved finding is not listed as remaining work; the other one is.
    expect(blocksOf(m, 'findingCard').map((c) => c.id)).toEqual([fid(2)])
  })
})

// ── 2 + 3 + 4. Human review states ───────────────────────────────────────────────────────────
describe('human review states are outstanding work, and technical completion is a separate claim', () => {
  const withReview = (review) => model(facts({
    findings: [], assessment: { ...facts().assessment, findingsTotal: 0 },
    accounting: { ...facts().accounting, findingsTotal: 0, findingsOpen: 0 },
    reviews: { [verifiedId('SC_1_1_1', 0)]: review },
  }), { rows: [{ id: '1.1.1', name: 'Non-text', plain: 'Alt text', level: 'A', outcome: 'FIXED', count: 0 }] })

  const decided = {
    reviewer: 'qa@example.com', at: '2026-09-17T10:00:00Z', note: null, edited_value: null,
    artifact_sha256: SHA_COR, change_digest: 'd'.repeat(64), stale: false, staleReason: null,
    current_change_digest: 'd'.repeat(64),
  }

  it('an accepted, fresh decision on a technically complete file is the ONLY way to nothing outstanding', () => {
    const m = withReview({ ...decided, verdict: 'accepted' })
    expect(m.technicalReady).toBe(true)
    expect(m.ready).toBe(true)
    expect(text(m)).toMatch(READY)
    expect(m.humanConfirmation).toMatchObject({ confirmed: 1, outstanding: 0 })
  })

  for (const [verdict, status, wording] of [
    ['edited', 'correction_requested', /correction requested \(proposed only — not applied\)/],
    ['rejected', 'rejected', /rejected by a reviewer/],
    ['unable', 'unable', /could not verify/],
  ]) {
    it(`a '${verdict}' decision is outstanding work, never "No outstanding items"`, () => {
      const m = withReview({ ...decided, verdict })
      expect(blocksOf(m, 'changeCard')[0].human.status).toBe(status)
      expect(blocksOf(m, 'changeCard')[0].human.confirmed).toBe(false)
      expect(m.technicalReady).toBe(true)          // the re-scan is still clean …
      expect(m.ready).toBe(false)                  // … and that is not the whole answer
      expect(text(m)).not.toMatch(READY)
      expect(blocksOf(m, 'callout')[0].text).toMatch(wording)
    })
  }

  it('a stale decision, and a pending one, are both outstanding', () => {
    const stale = withReview({ ...decided, verdict: 'accepted', artifact_sha256: SHA_OLD, stale: true, staleReason: 'the corrected copy changed' })
    expect(blocksOf(stale, 'changeCard')[0].human.status).toBe('stale')
    expect(stale.ready).toBe(false)
    const pending = model(facts({ findings: [], reviews: {} }))
    expect(blocksOf(pending, 'changeCard')[0].human.status).toBe('pending')
    expect(pending.ready).toBe(false)
  })

  it('reviewer decisions that were never loaded are UNKNOWN, not confirmation and not zero', () => {
    const f = facts()
    delete f.reviews
    const m = model(f)
    expect(m.humanConfirmation).toMatchObject({ loaded: false, confirmed: 0 })
    expect(m.ready).toBe(false)
    expect(blocksOf(m, 'callout')[0].text).toMatch(/Human confirmation: UNKNOWN/)
  })

  it('the callout always says BOTH: technical checks and human confirmation', () => {
    const m = withReview({ ...decided, verdict: 'accepted' })
    const t = blocksOf(m, 'callout')[0].text
    expect(t).toMatch(/Technical checks: /)
    expect(t).toMatch(/Human confirmation: /)
  })
})

// ── 3. 'edited' means correction requested ───────────────────────────────────────────────────
describe("'edited' is a correction REQUESTED — not applied, not a confirmation", () => {
  it('never reads "Accepted with edits", and never counts as confirmed', () => {
    const m = model(facts({
      reviews: {
        [verifiedId('SC_1_1_1', 0)]: {
          verdict: 'edited', note: 'use the caption text', edited_value: 'Quarterly revenue chart',
          reviewer: 'qa@example.com', at: '2026-09-17T10:00:00Z', artifact_sha256: SHA_COR,
          change_digest: 'd'.repeat(64), stale: false, staleReason: null, current_change_digest: 'd'.repeat(64),
        },
      },
    }))
    const card = blocksOf(m, 'changeCard')[0]
    expect(card.human.status).toBe('correction_requested')
    expect(card.human.verdict).toBe('correction_requested')
    expect(card.human.confirmed).toBe(false)
    expect(m.humanConfirmation).toMatchObject({ correctionRequested: 1, confirmed: 0, outstanding: 1 })
    const html = reportHtmlFromModel(m)
    expect(html).toContain('Correction requested — not applied')
    expect(html).not.toMatch(/Accepted with edits/)
    expect(html).toMatch(/recorded as a PROPOSAL and has <strong>not<\/strong> been applied/)
    expect(html).toContain('Quarterly revenue chart')
  })
})

// ── 4. Unknown freshness is not acceptance ───────────────────────────────────────────────────
describe('unknown artifact freshness is not acceptance', () => {
  const accepted = { verdict: 'accepted', reviewer: 'qa', at: '2026-09-17T10:00:00Z' }

  it('stale:null with no current identity renders as freshness unknown, never accepted', () => {
    expect(humanStatusOf({ ...accepted, stale: null, artifact_sha256: SHA_COR }, null))
      .toMatchObject({ status: 'freshness_unknown', confirmed: false, freshness: 'unknown' })
    expect(humanStatusOf({ ...accepted, stale: null }, SHA_COR))
      .toMatchObject({ status: 'freshness_unknown', confirmed: false })
    expect(humanStatusOf({ ...accepted }, null)).toMatchObject({ status: 'freshness_unknown' })
  })

  it('freshness is known only when the server evaluated it or the two shas match', () => {
    expect(humanStatusOf({ ...accepted, stale: false }, null)).toMatchObject({ status: 'accepted', confirmed: true })
    expect(humanStatusOf({ ...accepted, artifact_sha256: SHA_COR }, SHA_COR)).toMatchObject({ status: 'accepted', confirmed: true })
    expect(humanStatusOf({ ...accepted, artifact_sha256: SHA_OLD }, SHA_COR)).toMatchObject({ status: 'stale' })
  })

  it('a change whose digest no longer matches is stale even when the artifact sha does', () => {
    expect(humanStatusOf({ ...accepted, stale: false, artifact_sha256: SHA_COR, change_digest: 'd'.repeat(64), current_change_digest: 'e'.repeat(64) }, SHA_COR))
      .toMatchObject({ status: 'stale' })
  })

  it('with no corrected digest, an acceptance is never fresh — the source checksum is not the artifact', () => {
    // Ratified: a review binds to identity.correctedSha256 and to nothing else. A file with no
    // corrected copy has nothing to bind to, so matching the decision against the SOURCE checksum
    // would confirm it against bytes the reviewer never reviewed.
    const m = model(facts({
      findings: [], accounting: { ...facts().accounting, findingsOpen: 0, findingsTotal: 0 },
      identity: { ...facts().identity, correctedSha256: null, currentArtifact: { kind: 'source', sha256: SHA_SRC } },
      savedChanges: [savedChange({ artifactSha256: SHA_SRC })],
      reviews: { [verifiedId('SC_1_1_1', 0)]: { ...accepted, stale: null, artifact_sha256: SHA_SRC } },
    }), { rows: [{ id: '1.1.1', name: 'Non-text', plain: 'Alt text', level: 'A', outcome: 'FIXED', count: 0 }] })
    const card = blocksOf(m, 'changeCard')[0]
    expect(card.human.status).toBe('freshness_unknown')
    expect(card.human.confirmed).toBe(false)
    expect(card.human.currentSha256).toBeNull()
    expect(m.ready).toBe(false)
  })

  it('an unknown-freshness acceptance blocks "No outstanding items" and says why', () => {
    const m = model(facts({
      findings: [], accounting: { ...facts().accounting, findingsOpen: 0, findingsTotal: 0 },
      identity: { ...facts().identity, currentArtifact: { kind: 'unknown', sha256: null } },
      reviews: { [verifiedId('SC_1_1_1', 0)]: { ...accepted, stale: null, artifact_sha256: SHA_COR } },
    }), { rows: [{ id: '1.1.1', name: 'Non-text', plain: 'Alt text', level: 'A', outcome: 'FIXED', count: 0 }] })
    expect(blocksOf(m, 'changeCard')[0].human.status).toBe('freshness_unknown')
    expect(m.ready).toBe(false)
    expect(text(m)).not.toMatch(READY)
    expect(reportHtmlFromModel(m)).toMatch(/Unknown freshness is not a confirmation/)
  })
})

// ── 5. Documents assessed comes from assessment state ────────────────────────────────────────
describe('"Documents assessed" is a fact about the assessment, not about catalog rows', () => {
  const rows = [{ id: '1.1.1', name: 'Non-text', plain: 'Alt text', level: 'A', outcome: 'UNCHECKED', count: 0 }]
  const withState = (state, over = {}) => model(facts({
    assessment: { ...facts().assessment, state, findingsTotal: 0, ...over },
    findings: [], savedChanges: [],
    accounting: { ...facts().accounting, findingsTotal: null, findingsOpen: null, savedChangesVerified: 0, savedChangesUnverified: 0 },
  }), { rows })

  it('assessed with zero findings IS "no findings"', () => {
    const m = withState('assessed')
    expect(decision(m).documentsAssessed).toBe(1)
    expect(m.assessmentState).toBe('assessed')
    expect(basis(m, 'documentsAssessed')).not.toMatch(/has NOT been assessed/)
  })

  for (const [state, expected, wording] of [
    ['not_assessed', 0, /has NOT been assessed/],
    ['error', 0, /did not complete/],
  ]) {
    it(`${state} with zero findings is NOT "no findings"`, () => {
      const m = withState(state, { stateReason: 'the worker could not open the file' })
      expect(decision(m).documentsAssessed).toBe(expected)
      expect(basis(m, 'documentsAssessed')).toMatch(wording)
      expect(basis(m, 'documentsAssessed')).toMatch(/the worker could not open the file/)
      expect(text(m)).not.toMatch(READY)
    })
  }

  it('partial says how partial, and never implies the list is complete', () => {
    const m = model(facts({
      assessment: { ...facts().assessment, state: 'partial', findingsComplete: false, findingsTotal: 9, stateReason: 'the engine timed out' },
      accounting: { ...facts().accounting, findingsOpen: 2 },
    }), { rows })
    expect(decision(m).documentsAssessed).toBe(1)
    expect(basis(m, 'documentsAssessed')).toMatch(/PARTIAL — 2 of 9 findings listed \(the engine timed out\)/)
    expect(basis(m, 'documentsAssessed')).toMatch(/An absent finding is not evidence there is none/)
    expect(basis(m, 'findingsRemaining')).toMatch(/PARTIAL/)
    const appendix = m.blocks.find((b) => b.id === 'appendix-findings')
    expect(appendix.complete).toBe(false)
    expect(appendix.totalRecords).toBe(9)
  })

  it('without facts, coverage rows alone never count as an assessment', () => {
    const m = buildFileReportModel({ file: FILE, mode: 'full', rows: [{ id: '1.1.1', name: 'x', plain: 'x', level: 'A', outcome: 'UNCHECKED', count: 0 }] })
    expect(decision(m).documentsAssessed).toBeNull()
    expect(basis(m, 'documentsAssessed')).toMatch(/coverage rows alone do not show that this document was opened/)
  })
})

// ── 6. Source guidance survives verbatim ─────────────────────────────────────────────────────
describe('the source’s own remediation guidance is preserved verbatim, over generic text', () => {
  const GUIDANCE = 'Replace the decorative border image with a CSS background so it needs no description.'

  it('recommended_action / remediation reach the finding card and the HTML output', () => {
    const m = buildFileReportModel({
      file: 'a.docx', mode: 'reviewer',
      rows: [{ id: '1.1.1', name: 'Non-text', plain: 'Alt text', level: 'A', outcome: 'FAIL', count: 2,
        fileIssues: [{ detail: 'Border image has no description', recommended_action: GUIDANCE },
          { detail: 'Logo has no description', remediation: 'Set the logo alt text to the organisation name.' }] }],
    })
    const cards = blocksOf(m, 'findingCard')
    expect(cards.map((c) => c.recommendedAction)).toEqual([GUIDANCE, 'Set the logo alt text to the organisation name.'])
    // Verbatim, and FIRST — ahead of the criterion's generic steps.
    expect(cards[0].steps[0]).toBe(GUIDANCE)
    const html = reportHtmlFromModel(m)
    expect(html).toContain('Recommended action (from the assessment)')
    expect(html).toContain(GUIDANCE)
    expect(html).toContain('Set the logo alt text to the organisation name.')
  })

  it('facts findings carry recommendedAction through to the card and the HTML', () => {
    const m = model(facts({ findings: [finding({ recommendedAction: GUIDANCE })] }))
    expect(blocksOf(m, 'findingCard')[0].recommendedAction).toBe(GUIDANCE)
    expect(reportHtmlFromModel(m)).toContain(GUIDANCE)
    expect(findingCardsFromFacts(facts({ findings: [finding({ recommendedAction: GUIDANCE })] }))[0].steps[0]).toBe(GUIDANCE)
  })
})

// ── 7. Unverified saved changes are first class ──────────────────────────────────────────────
describe('applied-but-unverified AI edits are change cards, marked and counted apart', () => {
  const mixed = () => facts({
    savedChanges: [
      savedChange(),
      savedChange({
        id: unverifiedId('SC_1_4_3', '0123456789abcdef'), ruleId: 'SC_1_4_3', sc: '1.4.3',
        seq: null, locator: 'body/p[4]', before: '#999999', after: '#595959',
        verification: 'not_verified',
        verificationDetail: 'Applied to the corrected copy; no re-scan result has been recorded.',
        source: 'unverified_changes.saved_changes',
      }),
    ],
    savedChangesTotal: 2,
    accounting: { ...facts().accounting, savedChangesVerified: 1, savedChangesUnverified: 1,
      humanReviews: { pending: 2, accepted: 0, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 } },
  })

  it('both appear as cards; the unverified one is marked "AI applied · not verified"', () => {
    const m = model(mixed())
    const cards = blocksOf(m, 'changeCard')
    expect(cards).toHaveLength(2)
    expect(cards.map((c) => c.id)).toEqual([verifiedId('SC_1_1_1', 0), unverifiedId('SC_1_4_3', '0123456789abcdef')])
    expect(cards.map((c) => c.verification)).toEqual(['verified', 'not_verified'])
    expect(reportHtmlFromModel(m)).toContain('AI applied · not verified')
  })

  it('verified and unverified are counted separately everywhere they are stated', () => {
    const m = model(mixed())
    expect(m.savedChanges).toMatchObject({ verified: 1, unverified: 1, total: 2 })
    expect(basis(m, 'editsSaved')).toMatch(/1 verified by re-scan; 1 applied but NOT verified/)
    const stage = blocksOf(m, 'stageStrip')[0].items.find((s) => s.key === 'technicalChecks')
    expect(stage.detail).toMatch(/1 saved change verified by re-scan; 1 applied but not verified/)
    expect(stage.status).toBe('pending')
    expect(blocksOf(m, 'callout')[0].text).toMatch(/Saved changes: 1 verified by re-scan, 1 applied by AI but not verified by a re-scan\./)
    const appendix = m.blocks.find((b) => b.id === 'appendix-changes')
    expect(appendix.rows.map((r) => r[4])).toEqual(['Verified by re-scan', 'AI applied · not verified'])
  })

  it('an unverified change is never described as verified, and the id is the server’s', () => {
    const m = model(mixed())
    const card = blocksOf(m, 'changeCard')[1]
    expect(card.technical.status).toBe('pending')
    expect(card.id).toMatch(/::u[0-9a-f]{16}$/)
    expect(card.seq).toBeNull()
    expect(card.location.label).toContain('body/p[4]')
  })
})

// ── 7b. The unverified list could not be read ────────────────────────────────────────────────
describe('changes awaiting review that could not be read are UNKNOWN, never "none outstanding"', () => {
  // savedChangesUnverifiedSource: 'unavailable' is the quiet failure: the applied-but-unverified
  // records are the ones a human still has to look at, so an unreadable list looks exactly like a
  // finished review. Everything it feeds must go to "not recorded", and ready must fall.
  const unreadable = (over = {}) => facts({
    savedChangesUnverifiedSource: 'unavailable',
    savedChangesComplete: false,
    savedChanges: [savedChange()],          // only the VERIFIED record could be read
    savedChangesTotal: null,
    findings: [], assessment: { ...facts().assessment, findingsTotal: 0 },
    accounting: {
      ...facts().accounting, findingsTotal: 0, findingsOpen: 0,
      savedChangesVerified: 1, savedChangesUnverified: 0,
      humanReviews: { pending: 0, accepted: 1, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 },
    },
    reviews: {
      [verifiedId('SC_1_1_1', 0)]: {
        verdict: 'accepted', reviewer: 'qa@example.com', at: '2026-09-17T10:00:00Z', note: null,
        edited_value: null, artifact_sha256: SHA_COR, change_digest: 'd'.repeat(64),
        stale: false, staleReason: null, current_change_digest: 'd'.repeat(64),
      },
    },
    ...over,
  })
  const rows = [{ id: '1.1.1', name: 'Non-text', plain: 'Alt text', level: 'A', outcome: 'FIXED', count: 0 }]

  it('is never "No outstanding items", even though every record it CAN see is clean', () => {
    const m = model(unreadable(), { rows })
    // Everything visible says done: the one readable change is verified and accepted by a reviewer
    // against the current artifact, the rows are clean, there are no open findings.
    expect(m.technicalReady).toBe(true)
    expect(m.humanConfirmation).toMatchObject({ confirmed: 1, outstanding: 0 })
    // …and the report still may not say so.
    expect(m.ready).toBe(false)
    expect(text(m)).not.toMatch(READY)
  })

  it('says the records could not be read, and that absence is not evidence', () => {
    const m = model(unreadable(), { rows, mode: 'reviewer' })
    const callout = blocksOf(m, 'callout')[0].text
    expect(callout).toMatch(/applied-but-unverified records COULD NOT BE READ/)
    expect(callout).toMatch(/Human confirmation: UNKNOWN/)
    expect(text(m)).toMatch(/could not be read, so this report cannot say how many are outstanding/)
    expect(text(m)).toMatch(/Absence from the list below is not evidence that there are none/)
  })

  it('reports no total and no pending count rather than a floor', () => {
    const m = model(unreadable(), { rows })
    expect(decision(m).editsSaved).toBeNull()
    expect(decision(m).humanChecksPending).toBeNull()
    expect(basis(m, 'editsSaved')).toMatch(/The applied-but-unverified records could not be read, so the total is not recorded/)
    const stage = blocksOf(m, 'stageStrip')[0].items
    expect(stage.find((x) => x.key === 'humanConfirmation')).toMatchObject({ status: 'unknown', value: null })
    expect(stage.find((x) => x.key === 'technicalChecks').status).toBe('unknown')
  })

  it('with NO readable change at all, it does not read as "no saved changes are recorded"', () => {
    const m = model(unreadable({ savedChanges: [], reviews: {} }), { rows, mode: 'reviewer' })
    expect(text(m)).not.toMatch(/No saved changes are recorded for this file/)
    expect(text(m)).toMatch(/could not be read, so this report cannot say how many are outstanding/)
    expect(m.ready).toBe(false)
  })

  it("'ok' is the normal case and none of the above fires", () => {
    const m = model(facts({
      savedChangesUnverifiedSource: 'ok', findings: [],
      accounting: { ...facts().accounting, findingsTotal: 0, findingsOpen: 0 },
      reviews: {
        [verifiedId('SC_1_1_1', 0)]: {
          verdict: 'accepted', reviewer: 'qa', at: '2026-09-17T10:00:00Z', artifact_sha256: SHA_COR,
          change_digest: 'd'.repeat(64), stale: false, current_change_digest: 'd'.repeat(64),
        },
      },
    }), { rows })
    expect(m.ready).toBe(true)
    expect(text(m)).not.toMatch(/COULD NOT BE READ/)
  })
})

// ── 5b. findingsOpen is null whenever the assessment did not complete ────────────────────────
describe('a document that was not assessed reports no open-finding count, not zero', () => {
  for (const state of ['error', 'not_assessed', 'partial']) {
    it(`state '${state}' with findingsOpen null never reads as zero findings`, () => {
      const m = model(facts({
        assessment: { ...facts().assessment, state, findingsTotal: 0, findingsComplete: false, stateReason: 'the analyser did not produce a result' },
        findings: [], savedChanges: [],
        accounting: {
          ...facts().accounting, findingsTotal: null, findingsOpen: null,
          accountingReason: `the assessment state is ${state}, so no open-finding count can be stated`,
          savedChangesVerified: 0, savedChangesUnverified: 0,
          humanReviews: { pending: 0, accepted: 0, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 },
        },
      }), { rows: [{ id: '1.1.1', name: 'Non-text', plain: 'Alt text', level: 'A', outcome: 'UNCHECKED', count: 0 }] })
      expect(decision(m).findingsRemaining).toBeNull()
      expect(decision(m).findingsRemaining).not.toBe(0)
      expect(basis(m, 'findingsRemaining')).toMatch(new RegExp(`the assessment state is "${state}"`))
      expect(basis(m, 'findingsRemaining')).toMatch(/Zero is not the answer/)
      expect(text(m)).not.toMatch(READY)
    })
  }
})

// ── 8. Summary mode is one page ──────────────────────────────────────────────────────────────
describe('summary mode is a one-page decision overview', () => {
  const summary = () => model(facts(), { mode: 'summary' })

  it('carries the counts, what is outstanding, the next action and scope/comparison — and stops', () => {
    const m = summary()
    const kinds = m.blocks.map((b) => b.k)
    expect(kinds.filter((k) => k === 'decisionSummary')).toHaveLength(1)
    expect(kinds.filter((k) => k === 'comparison')).toHaveLength(1)
    expect(blocksOf(m, 'callout')[0].text).toMatch(/Next step:/)
    expect(text(m)).toMatch(/Scope of this assessment:/)
  })

  it('has exactly ONE identity table and no second one', () => {
    const m = summary()
    const identity = m.blocks.filter((b) => b.k === 'table' && (b.role === 'identity' || /identity/i.test(b.caption || '')))
    expect(identity).toHaveLength(1)
    expect(identity[0].role).toBe('identity')
    // Identity does not also appear in the cover meta — that is the duplication, the other way round.
    expect(JSON.stringify(m.cover.meta)).not.toContain(SHA_COR)
    expect(JSON.stringify(m.cover.meta)).not.toContain(SHA_SRC)
  })

  it('drops methodology, disclaimers, the stage strip and every per-criterion table', () => {
    const m = summary()
    const headings = m.blocks.filter((b) => b.k === 'heading').map((b) => b.text)
    expect(headings).not.toContain('What this report is, and is not')
    expect(headings).not.toContain('Manual verification guide')
    expect(headings).not.toContain('Checklist by area')
    expect(headings).not.toContain('Full WCAG coverage')
    expect(headings).not.toContain('Criteria outcomes')
    expect(m.blocks.filter((b) => b.k === 'stageStrip')).toHaveLength(0)
    expect(m.blocks.filter((b) => b.k === 'changeCard')).toHaveLength(0)
    expect(m.blocks.filter((b) => b.k === 'findingCard')).toHaveLength(0)
    expect(m.blocks.filter((b) => b.k === 'appendixTable')).toHaveLength(0)
    expect(text(m)).not.toMatch(/not a conformance determination, a certification or legal advice/)
    expect(text(m)).not.toMatch(/Marking a printed or downloaded copy/)
  })

  it('fits a page: at most three outline roots and a small, BOUNDED block list', () => {
    const m = summary()
    const roots = m.blocks.filter((b) => b.k === 'heading' && (b.level ?? 1) === 1)
    expect(roots.map((b) => b.text)).toEqual(['Decision summary', 'Document identity', 'Since the previous assessment'])
    expect(roots.length).toBeLessThanOrEqual(3)          // was 7 in the parent's two-page render
    expect(m.blocks.length).toBeLessThanOrEqual(8)
    // …and it stays bounded as the document gets worse: nothing in summary grows with the data.
    const big = model(facts({
      findings: Array.from({ length: 80 }, (_, i) => finding({ id: fid(i), detail: `Finding ${i}` })),
      savedChanges: Array.from({ length: 60 }, (_, i) => savedChange({ id: verifiedId('SC_1_1_1', i), seq: i })),
      accounting: { ...facts().accounting, findingsOpen: 80, savedChangesVerified: 60 },
    }), { mode: 'summary' })
    expect(big.blocks.length).toBeLessThanOrEqual(8)
    expect(big.blocks.filter((b) => b.k === 'heading' && (b.level ?? 1) === 1)).toHaveLength(3)
  })

  it('prose names the file, identity keeps the full path — the path is never repeated into prose', () => {
    // A 140-character SharePoint path repeated in every sentence costs the one-page summary two
    // lines per mention. It appears IN FULL exactly where it is identity, and nowhere else.
    const long = 'Shared Documents/Clinical governance/2026/Q3/Patient services and community outreach accessibility review — final approved version.pdf'
    const m = buildFileReportModel({
      file: long, targetLevel: 'AA', mode: 'summary', rows: [],
      facts: { ...facts(), identity: { ...facts().identity, file: long } },
    })
    const callout = blocksOf(m, 'callout')[0].text
    expect(callout).toContain('Patient services and community outreach accessibility review — final approved version.pdf')
    expect(callout).not.toContain('Shared Documents/Clinical governance')
    // …and the full path is still on the page, in the block that is the authority for identity.
    const identity = m.blocks.find((b) => b.role === 'identity')
    expect(identity.rows.find((r) => r[0] === 'Document')[1]).toBe(long)
    expect(m.cover.subtitle).toBe(long)
  })

  it('full mode still keeps every detail', () => {
    const m = model(facts(), { mode: 'full' })
    const headings = m.blocks.filter((b) => b.k === 'heading').map((b) => b.text)
    expect(headings).toContain('What this report is, and is not')
    expect(headings).toContain('Complete evidence appendix')
    expect(m.blocks.filter((b) => b.k === 'stageStrip')).toHaveLength(1)
    expect(m.blocks.filter((b) => b.k === 'changeCard')).toHaveLength(1)
  })
})

// ── 9. Clipping disclosure ───────────────────────────────────────────────────────────────────
describe('a value the store clipped is disclosed as clipped, and not pointed at a fuller copy', () => {
  it('valueClipped says the store did it and that the text was not kept', () => {
    const m = model(facts({ savedChanges: [savedChange({ valueClipped: true, after: 'y'.repeat(2000) })] }))
    const card = blocksOf(m, 'changeCard')[0]
    expect(card.valueClipped).toBe(true)
    expect(card.valueMaxChars).toBe(2000)
    const html = reportHtmlFromModel(m)
    expect(html).toMatch(/clipped by the store to 2,000 characters when it was recorded/)
    expect(html).toMatch(/untruncated text was not kept/)
    // There is nowhere fuller to send the reader: the store kept no untruncated copy.
    expect(html).not.toMatch(/clipped by the store[^<]*Full evidence report/)
    expect(m.blocks.find((b) => b.id === 'appendix-changes').rows[0][7]).toMatch(/Clipped by the store at 2000 characters/)
  })

  it('a value the report merely shortened for a card is a different statement', () => {
    const m = model(facts({ savedChanges: [savedChange({ after: 'z'.repeat(1200), valueClipped: false })] }), { mode: 'reviewer' })
    const card = blocksOf(m, 'changeCard')[0]
    expect(card.afterTruncated).toBe(true)      // this report shows less …
    expect(card.valueClipped).toBe(false)       // … but the record itself is complete
    expect(card.fullRef).toContain('Full evidence report')
  })
})

// ── 10. Comparison ───────────────────────────────────────────────────────────────────────────
describe('comparison comes only from a real comparable snapshot', () => {
  const prevFindings = [
    { id: fid(1), ruleId: 'SC_1_1_1', sc: '1.1.1', detail: 'Image has no description', location: { label: 'Page 1', page: 1 } },
    { id: fid(9), ruleId: 'SC_2_4_2', sc: '2.4.2', detail: 'No document title', location: { label: 'Document', page: null } },
  ]
  const previous = { scanId: 'scan-0', generatedAt: '2026-08-01T00:00:00Z', sha256: SHA_OLD, file: FILE, scopeDigest: SCOPE, scanScope: 'agreed-estate', findings: prevFindings }

  it('matches finding by finding and preserves ids on both sides', () => {
    const c = buildComparisonFromFacts(facts({ previous }))
    expect(c.status).toBe('compared')
    expect(c.resolved.map((x) => x.id)).toEqual([fid(9)])
    expect(c.introduced.map((x) => x.id)).toEqual([fid(2)])
    expect(c.persisting).toBe(1)
    expect(c.previous).toMatchObject({ scanId: 'scan-0', sha256: SHA_OLD })
  })

  it('a different scope or an unrecorded scope is UNKNOWN', () => {
    expect(buildComparisonFromFacts(facts({ previous: { ...previous, scopeDigest: 'x'.repeat(64) } })).status).toBe('unknown')
    // Audit C3, reproduced on the real store: the SERVER matched this baseline by the provider's
    // own file id, so a different name is a RENAME of this document, not another one. Rejecting
    // it here was the defect — the comparison is made.
    expect(buildComparisonFromFacts(facts({ previous: { ...previous, file: 'other.pdf' } })).status).toBe('compared')
    expect(buildComparisonFromFacts(facts({
      previous: { ...previous, scopeDigest: null, scanScope: null },
    })).status).toBe('unknown')
  })

  it('no snapshot gives status unknown with the server’s own reason, and no counts', () => {
    const m = model(facts())
    const c = blocksOf(m, 'comparison')[0]
    expect(c.status).toBe('unknown')
    expect(c.reason).toBe('No earlier assessment of this document with the same scope is recorded.')
    expect(c.persisting).toBeNull()
    expect(c.resolved).toEqual([])
    // No aggregate subtraction anywhere: nothing claims findings were resolved since last time.
    expect(text(m)).not.toMatch(/fewer findings than|down from \d+/i)
  })

  it('a previous snapshot with no itemised findings cannot be compared', () => {
    expect(buildComparisonFromFacts(facts({ previous: { ...previous, findings: null } })).status).toBe('unknown')
  })
})

// ── identity, digest and the scan level ──────────────────────────────────────────────────────
describe('identity travels with the model', () => {
  it('factsDigest, currentArtifact and the checksum kind are carried through', () => {
    const m = model(facts())
    expect(m.identity.factsDigest).toBe('f'.repeat(64))
    expect(m.factsDigest).toBe('f'.repeat(64))
    expect(m.identity.currentArtifact).toEqual({ kind: 'corrected', sha256: SHA_COR })
    expect(m.identity.sourceChecksumKind).toBe('sha256')
    const table = m.blocks.find((b) => b.role === 'identity')
    expect(JSON.stringify(table.rows)).toContain('Corrected copy')
  })

  it('a non-sha256 source checksum is labelled with its kind, never called a SHA-256', () => {
    const m = model(facts({
      identity: { ...facts().identity, sourceSha256: null, sourceChecksum: 'abc123', sourceChecksumKind: 'quickxorhash' },
    }))
    const table = m.blocks.find((b) => b.role === 'identity')
    const row = table.rows.find((r) => r[0] === 'Source checksum')
    expect(row[1]).toBe('abc123 (quickxorhash)')
  })

  it('the scan model carries the scan-level digest and counts assessments, not catalog rows', () => {
    const scanFacts = {
      factsVersion: 1, factsDigest: '9'.repeat(64), generatedAt: '2026-09-17T12:00:00Z',
      files: [
        { file: 'a.pdf', assessment: { state: 'assessed' }, score: 70, findingsOpen: 1, savedChangesVerified: 0, savedChangesUnverified: 0, humanReviews: { pending: 0, accepted: 0, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 }, currentArtifact: { kind: 'source', sha256: SHA_SRC } },
        { file: 'b.pdf', assessment: { state: 'not_assessed' }, score: null, findingsOpen: null, savedChangesVerified: 0, savedChangesUnverified: 0, humanReviews: { pending: 0, accepted: 0, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 }, currentArtifact: { kind: 'unknown', sha256: null } },
      ],
      filesTotal: 2, offset: 0, limit: 100, complete: true,
      accounting: { findingsTotal: 1, findingsOpen: 1, findingsResolvedVerified: null, resolutionLedger: 'none', savedChangesVerified: 0, savedChangesUnverified: 0, humanReviews: { pending: 0, accepted: 0, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 } },
      previous: null, previousReason: 'No comparable earlier estate snapshot is recorded.',
      limits: { valueMaxChars: 2000, savedChangesLimit: 500 },
    }
    const files = [{ file: 'a.pdf', score: 70, status: 'analysed', issues: [{ wcag: 'SC_1_1_1', severity: 'SERIOUS', detail: 'x' }] },
      { file: 'b.pdf', score: null, status: 'pending', issues: [] }]
    const data = aggregateScanReport({ scanId: 'scan-1', files, traces: [], facts: scanFacts })
    const m = buildScanReportModel({ ...data, mode: 'summary' })
    expect(m.identity.factsDigest).toBe('9'.repeat(64))
    expect(decision(m).documentsAssessed).toBe(1)          // not 2, and not the file-list length
    expect(basis(m, 'documentsAssessed')).toMatch(/1 not assessed, errored or only partly assessed/)
    expect(decision(m).findingsVerifiedResolved).toBeNull()
    expect(blocksOf(m, 'comparison')[0]).toMatchObject({ status: 'unknown', reason: 'No comparable earlier estate snapshot is recorded.' })
  })
})
