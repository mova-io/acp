// The report model, run against the REAL output of stream D's report-facts endpoints.
//
// `__fixtures__/reportFacts.file.sample.json` and `…scan.sample.json` are captured verbatim from
// `GET /scans/{sid}/files/{file}/report-facts` and `GET /scans/{sid}/report-facts` against the
// store — not hand-written. The hand-written fixtures in reportFactsAccounting.test.js pin the
// rules; this file proves the rules survive contact with the shape the server actually emits,
// including the parts no contract sketch mentioned (`kind`, `totals`, `accountingReason`,
// `targetLevelSource`, an md5 source checksum, a `reviews` entry with no `stale` key).
import { describe, it, expect } from 'vitest'
import fileFacts from './__fixtures__/reportFacts.file.sample.json'
import scanFacts from './__fixtures__/reportFacts.scan.sample.json'
import { buildFileReportModel } from './reportModel.js'
import { buildScanReportModel, aggregateScanReport } from './scanReport.js'
import { reportHtmlFromModel } from './htmlReport.js'

const blocksOf = (m, k) => m.blocks.filter((b) => b.k === k)
const decision = (m) => Object.fromEntries(blocksOf(m, 'decisionSummary')[0].items.map((i) => [i.key, i.value]))
const basis = (m, key) => blocksOf(m, 'decisionSummary')[0].items.find((i) => i.key === key).detail
const build = (mode = 'full') => buildFileReportModel({ file: fileFacts.identity.file, mode, facts: fileFacts, rows: [] })

describe('the real per-file facts document', () => {
  it('is the shape this model was written against', () => {
    // If stream D changes any of these, the tests below stop meaning what they say.
    expect(fileFacts.factsVersion).toBe(1)
    expect(fileFacts.accounting.resolutionLedger).toBe('none')
    expect(fileFacts.accounting.findingsResolvedVerified).toBeNull()
    expect(fileFacts.accounting.savedChangesUnverified).toBe(1)
    expect(fileFacts.savedChanges.map((c) => c.verification)).toEqual(['verified', 'not_verified'])
    expect(fileFacts.savedChanges[1].id).toMatch(/::u[0-9a-f]{16}$/)
    expect(fileFacts.savedChanges[1].seq).toBeNull()
    expect(Object.values(fileFacts.reviews).map((r) => r.verdict)).toContain('edited')
    expect(fileFacts.limits.valueMaxChars).toBe(2000)
  })

  it('credits no finding as resolved, and says the server’s own reason', () => {
    const m = build()
    expect(decision(m).findingsVerifiedResolved).toBeNull()
    expect(decision(m).findingsRemaining).toBeNull()          // accounting.findingsOpen is null here
    expect(basis(m, 'findingsVerifiedResolved')).toContain(fileFacts.accounting.accountingReason)
    expect(m.findingsAccounting).toMatchObject({ ledger: 'none', attributed: false })
  })

  it('counts verified and unverified saved changes apart, and shows both as cards', () => {
    const m = build()
    const cards = blocksOf(m, 'changeCard')
    expect(cards.map((c) => c.id)).toEqual(fileFacts.savedChanges.map((c) => c.id))
    expect(cards.map((c) => c.verification)).toEqual(['verified', 'not_verified'])
    expect(m.savedChanges).toMatchObject({ verified: 1, unverified: 1, total: 2 })
    expect(basis(m, 'editsSaved')).toMatch(/1 verified by re-scan; 1 applied but NOT verified/)
    expect(reportHtmlFromModel(m)).toContain('AI applied · not verified')
  })

  it("the store's 'edited' verdict reads as a correction requested, never as confirmation", () => {
    const m = build()
    const reviewed = blocksOf(m, 'changeCard').find((c) => c.human.status !== 'pending')
    expect(reviewed.human.status).toBe('correction_requested')
    expect(reviewed.human.confirmed).toBe(false)
    expect(reviewed.human.editedValue).toBe('The north entrance of the Riverside campus')
    expect(m.humanConfirmation).toMatchObject({ correctionRequested: 1, confirmed: 0 })
    const html = reportHtmlFromModel(m)
    expect(html).not.toMatch(/Accepted with edits/)
    expect(html).toContain('Correction requested — not applied')
    // The proposed value is shown as a proposal, and the document is not described as changed.
    expect(html).toMatch(/has <strong>not<\/strong> been applied/)
  })

  it('never says "No outstanding items" for this document', () => {
    const m = build()
    expect(m.ready).toBe(false)
    expect(JSON.stringify(m.blocks)).not.toMatch(/No outstanding items for/)
    const callout = blocksOf(m, 'callout')[0].text
    expect(callout).toMatch(/Technical checks: /)
    expect(callout).toMatch(/Saved changes: 1 verified by re-scan, 1 applied by AI but not verified/)
    expect(callout).toMatch(/correction requested \(proposed only — not applied\)/)
  })

  it('never claims the document was not evaluated when the facts say it was assessed', () => {
    // This build passes no coverage rows (the drawer supplies them separately). The report may not
    // speak about criteria — but "no in-scope criteria were evaluated" is a claim about the
    // ASSESSMENT, and the facts say it ran.
    const callout = blocksOf(build(), 'callout')[0].text
    expect(callout).not.toMatch(/no in-scope criteria were evaluated/)
    expect(callout).toMatch(/the per-criterion coverage table is not part of this report/)
    expect(basis(build(), 'documentsAssessed')).not.toMatch(/0 in-scope criteria/)
  })

  it('carries the server identity through, including a non-SHA-256 source checksum', () => {
    const m = build()
    expect(m.identity.factsDigest).toBe(fileFacts.factsDigest)
    expect(m.identity.currentArtifact).toEqual(fileFacts.identity.currentArtifact)
    expect(m.identity.sourceChecksumKind).toBe('md5')
    const row = m.blocks.find((b) => b.role === 'identity').rows.find((r) => r[0] === 'Source checksum')
    expect(row[1]).toBe(`${fileFacts.identity.sourceChecksum} (md5)`)
    expect(row[1]).not.toMatch(/SHA-256/)
  })

  it('reports one card per real finding, with that finding’s own state', () => {
    const m = build()
    const open = fileFacts.findings.filter((f) => f.state !== 'resolved_verified')
    expect(blocksOf(m, 'findingCard').map((c) => c.id)).toEqual(open.map((f) => f.id))
    const appendix = m.blocks.find((b) => b.id === 'appendix-findings')
    expect(appendix.rows).toHaveLength(fileFacts.findings.length)
    expect(appendix.headers).toContain('State of this finding')
  })

  it('reports no comparison, with the server’s reason and no invented movement', () => {
    const c = blocksOf(build(), 'comparison')[0]
    expect(c.status).toBe('unknown')
    expect(c.reason).toBe(fileFacts.previousReason)
    expect(c.persisting).toBeNull()
    expect(c.resolved).toEqual([])
    expect(c.introduced).toEqual([])
  })

  it('summary mode of the real document is still the one-page shape', () => {
    const m = build('summary')
    const roots = m.blocks.filter((b) => b.k === 'heading' && (b.level ?? 1) === 1).map((b) => b.text)
    expect(roots).toEqual(['Decision summary', 'Document identity', 'Since the previous assessment'])
    expect(m.blocks.length).toBeLessThanOrEqual(8)
    expect(m.blocks.filter((b) => b.k === 'stageStrip')).toHaveLength(0)
    expect(m.blocks.filter((b) => b.k === 'appendixTable')).toHaveLength(0)
    expect(blocksOf(m, 'callout')[0].text).toMatch(/Next step:/)
  })
})

describe('the real scan-level facts document', () => {
  const scanModel = (mode = 'summary') => {
    const data = aggregateScanReport({ scanId: scanFacts.identity.scanId, files: [], traces: [], facts: scanFacts })
    return buildScanReportModel({ ...data, mode })
  }

  it('is the shape this model was written against', () => {
    expect(scanFacts.totals).toMatchObject({ documents: 3, assessed: 2, error: 1 })
    expect(scanFacts.accounting.resolutionLedger).toBe('none')
    expect(scanFacts.previous).toBeNull()
  })

  it('counts assessments, not documents in the list — the errored one is not assessed', () => {
    const m = scanModel()
    expect(decision(m).documentsAssessed).toBe(2)
    expect(decision(m).documentsAssessed).not.toBe(scanFacts.filesTotal)
    expect(basis(m, 'documentsAssessed')).toMatch(/1 not assessed, errored or only partly assessed/)
    expect(basis(m, 'documentsAssessed')).toMatch(/an empty finding list for those is not "no findings"/)
  })

  it('states no resolved-finding count, and gives the server’s reason', () => {
    const m = scanModel()
    expect(decision(m).findingsVerifiedResolved).toBeNull()
    expect(basis(m, 'findingsVerifiedResolved')).toContain(scanFacts.accounting.accountingReason)
  })

  it('carries the scan-level facts digest the render route will check', () => {
    expect(scanModel().identity.factsDigest).toBe(scanFacts.factsDigest)
  })

  it('reports no estate comparison, with the server’s reason', () => {
    const c = blocksOf(scanModel(), 'comparison')[0]
    expect(c.status).toBe('unknown')
    expect(c.reason).toBe(scanFacts.previousReason)
  })
})
