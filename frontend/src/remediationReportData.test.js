/**
 * gatherRemediationEvidence — the remediation report must not turn a failed read into "nothing".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const h = vi.hoisted(() => ({ diffs: null, reviews: {} }))
vi.mock('./api.js', async (importOriginal) => ({
  ...(await importOriginal()),
  SIM: false,
  getScanRemediationDiffs: vi.fn(async () => h.diffs),
  fetchChangeReviews: vi.fn(async (_sid, file) => {
    const r = h.reviews[file]
    if (r instanceof Error) throw r
    return r
  }),
}))
const { gatherRemediationEvidence } = await import('./remediationReportData.js')
const { remediationReportModel, buildRemediationModel } = await import('./reportModel.js')

const SHA = 'b'.repeat(64)
const diff = (file, seq) => ({ file, rule_id: '1.1.1', seq, before: 'x', after: 'y', note: null, verified: true })

beforeEach(() => { h.diffs = null; h.reviews = {} })

describe('gatherRemediationEvidence', () => {
  it('a failed change read is unknown, not an empty list', async () => {
    h.diffs = []           // api.js resolves [] on any transport error
    const ev = await gatherRemediationEvidence('s1')
    expect(ev.diffsComplete).toBe(false)
    expect(ev.diffsTotal).toBeNull()
  })

  it('keeps the server total so a truncated page reads as partial', async () => {
    h.diffs = { items: [diff('a.docx', 0), diff('a.docx', 1)], total: 5, documents: 1, complete: false }
    h.reviews['a.docx'] = { artifact: { currentSha256: SHA }, reviews: {} }
    const ev = await gatherRemediationEvidence('s1')
    expect(ev.diffsByFile['a.docx']).toHaveLength(2)
    expect(ev.diffsComplete).toBe(false)
    expect(ev.diffsTotal).toBe(5)
  })

  it('binds recorded decisions per document and reports the current version', async () => {
    h.diffs = { items: [diff('a.docx', 0), diff('b.pdf', 0)], total: 2, documents: 2, complete: true }
    h.reviews['a.docx'] = {
      artifact: { currentSha256: SHA, correctedSha256: SHA },
      reviews: { 'a.docx::1.1.1::0': { verdict: 'accepted', reviewer: 'r@example.com', at: '2026-09-17T00:00:00Z', artifact_sha256: SHA } },
    }
    h.reviews['b.pdf'] = { artifact: { currentSha256: SHA }, reviews: {} }
    const ev = await gatherRemediationEvidence('s1')
    expect(ev.reviewsByFile['a.docx']['a.docx::1.1.1::0'].verdict).toBe('accepted')
    expect(ev.reviewsByFile['a.docx']['a.docx::1.1.1::0'].stale).toBe(false)
    expect(ev.reviewsByFile['b.pdf']).toEqual({})
    expect(ev.currentShaByFile).toEqual({ 'a.docx': SHA, 'b.pdf': SHA })
  })

  it('one unreadable document makes decisions "not loaded", never "none recorded"', async () => {
    h.diffs = { items: [diff('a.docx', 0), diff('b.pdf', 0)], total: 2, documents: 2, complete: true }
    h.reviews['a.docx'] = { artifact: { currentSha256: SHA }, reviews: {} }
    h.reviews['b.pdf'] = new Error('503')
    const ev = await gatherRemediationEvidence('s1')
    expect(ev.reviewsByFile).toBeNull()

    const m = buildRemediationModel({ files: [{ file: 'a.docx' }, { file: 'b.pdf' }], ...ev })
    const model = remediationReportModel(m, { mode: 'reviewer', reviewsByFile: ev.reviewsByFile })
    const cards = model.blocks.filter((b) => b.k === 'changeCard')
    expect(cards).toHaveLength(2)
    cards.forEach((c) => expect(c.human.loaded).toBe(false))
    const pending = model.blocks.find((b) => b.k === 'decisionSummary').items.find((i) => i.key === 'humanChecksPending')
    expect(pending.value).toBeNull()
  })
})
