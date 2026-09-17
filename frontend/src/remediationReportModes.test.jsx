/**
 * The remediation report now has the same three modes as the other two report kinds, and its
 * evidence includes the changes the AI applied that nothing re-scanned.
 *
 * Before this, Remediate offered one unlabelled "Remediation report (PDF)" button — no Summary,
 * no Reviewer packet, no Full evidence — and it was fed from `remediation_diff`, which holds only
 * fixes that CLEARED the re-scan. So the one report a reviewer would take into a sign-off meeting
 * was the one that omitted every change still needing their judgement.
 *
 * This file is the DATA half — gatherRemediationEvidence driven with api.js mocked, which is the
 * live path (nothing here mocks the builders). The menu itself is in remediationReportMenu.test.jsx,
 * in its own file because mounting the whole Remediate page needs api.js NOT mocked.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const h = vi.hoisted(() => ({ scanFacts: null, fileFacts: null, scanDiffs: null }))

vi.mock('./api.js', () => ({
  SIM: false,
  getScanReportFacts: (...a) => h.scanFacts(...a),
  getFileReportFacts: (...a) => h.fileFacts(...a),
  getScanRemediationDiffs: (...a) => h.scanDiffs(...a),
  fetchChangeReviews: async () => ({ artifact: {}, reviews: {} }),
  putChangeReview: async () => ({}),
}))

const { gatherRemediationEvidence } = await import('./remediationReportData.js')

const SHA = 'c'.repeat(64)
const scanPage = (files, over = {}) => ({
  factsVersion: 1, factsDigest: 'scan-digest-rem', filesTotal: files.length, offset: 0, limit: 200,
  complete: true, files, previous: null, previousReason: 'none', ...over,
})
const fileFacts = (file, savedChanges) => ({
  factsVersion: 1, factsDigest: `d-${file}`,
  identity: { scanId: 's1', file, currentArtifact: { kind: 'corrected', sha256: SHA }, correctedSha256: SHA },
  assessment: { state: 'assessed' },
  findings: [],
  savedChanges, savedChangesComplete: true, savedChangesTotal: savedChanges.length, savedChangesLimit: 500,
  reviews: {},
  previous: null, previousReason: 'none',
  limits: { valueMaxChars: 2000, savedChangesLimit: 500 },
})

const VERIFIED = { id: 'a.docx::1.1.1::0', ruleId: '1.1.1', sc: '1.1.1', seq: 0, before: '', after: 'alt', note: null, verification: 'verified', verificationDetail: 'cleared', artifactSha256: SHA, valueClipped: false, changeDigest: 'cd0', findingIds: null, source: 'remediation_diff' }
const UNVERIFIED = { id: 'a.docx::1.3.1::uaaaabbbbccccdddd', ruleId: '1.3.1', sc: '1.3.1', seq: null, locator: 'p#2', before: 'x', after: 'y', note: null, verification: 'not_verified', verificationDetail: 'no re-scan', artifactSha256: SHA, valueClipped: true, changeDigest: 'cdu', findingIds: null, source: 'unverified_changes' }

beforeEach(() => {
  h.scanFacts = vi.fn(async () => scanPage([
    { file: 'a.docx', assessment: { state: 'assessed' }, savedChangesVerified: 1, savedChangesUnverified: 1, currentArtifact: { kind: 'corrected', sha256: SHA } },
    { file: 'b.pdf', assessment: { state: 'assessed' }, savedChangesVerified: 0, savedChangesUnverified: 0, currentArtifact: { kind: 'source', sha256: null } },
  ]))
  h.fileFacts = vi.fn(async (_s, file) => fileFacts(file, [VERIFIED, UNVERIFIED]))
  h.scanDiffs = vi.fn(async () => ({ items: [], total: 0, documents: 0, complete: true }))
})

describe('the remediation report’s evidence', () => {
  it('includes applied-but-unverified changes and counts both kinds', async () => {
    const ev = await gatherRemediationEvidence('s1')
    expect(h.fileFacts.mock.calls.map((c) => c[1])).toEqual(['a.docx'])   // b.pdf has no changes
    expect(ev.diffsByFile['a.docx'].map((d) => d.id))
      .toEqual(['a.docx::1.1.1::0', 'a.docx::1.3.1::uaaaabbbbccccdddd'])
    expect(ev.savedChangesVerified).toBe(1)
    expect(ev.savedChangesUnverified).toBe(1)
    expect(ev.diffsComplete).toBe(true)
    expect(ev.diffsTotal).toBe(2)
    expect(ev.factsDigest).toBe('scan-digest-rem')
    expect(ev.source).toBe('report-facts')
    expect(ev.evidenceNotes).toBeNull()
  })

  it('a document whose evidence could not be read is named, and decisions are "not loaded"', async () => {
    h.fileFacts = vi.fn(async () => { throw new Error('503') })
    const ev = await gatherRemediationEvidence('s1')
    expect(ev.diffsComplete).toBe(false)
    expect(ev.diffsTotal).toBeNull()
    expect(ev.reviewsByFile).toBeNull()
    expect(ev.evidenceNotes.join(' ')).toMatch(/saved changes of 1 document\(s\) could not be read: a\.docx/)
  })

  it('without the facts endpoint it says the unverified changes are missing — not that there are none', async () => {
    h.scanFacts = vi.fn(async () => { throw new Error('404') })
    h.scanDiffs = vi.fn(async () => ({ items: [{ file: 'a.docx', rule_id: '1.1.1', seq: 0, before: '', after: 'alt' }], total: 1, documents: 1, complete: true }))
    const ev = await gatherRemediationEvidence('s1')
    expect(ev.source).toBe('remediation-diffs')
    expect(ev.savedChangesUnverified).toBeNull()
    expect(ev.evidenceNotes.join(' ')).toMatch(/only changes a re-scan verified/)
    expect(ev.evidenceNotes.join(' ')).toMatch(/their number is not known/)
  })

  it('reports progress while it reads the per-document index', async () => {
    const seen = []
    await gatherRemediationEvidence('s1', { onProgress: (p) => seen.push(p.loaded) })
    expect(seen.length).toBeGreaterThan(0)
    expect(seen[seen.length - 1]).toBe(2)
  })
})
