/**
 * S3 — the remediation report at estate size: an OVERALL Reviewer-packet bound, and no document
 * stranded by the per-document itemisation limit.
 *
 * Measured by the audit: at 300 documents the Reviewer packet held 1,500 cards (the same as Full
 * evidence), and documents beyond the 250 itemised were named ten at a time, "…", and then lost.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const N_DOCS = 262
const h = vi.hoisted(() => ({ index: [], failing: new Set(), calls: 0 }))
vi.mock('./api.js', async (importOriginal) => ({
  ...(await importOriginal()),
  SIM: false,
  getScanReportFacts: vi.fn(async (_sid, { offset = 0, limit = 200 } = {}) => ({
    factsVersion: 1, kind: 'scan', factsDigest: 'd'.repeat(64),
    totals: { savedChangesVerified: h.index.length * 2, savedChangesUnverified: h.index.length },
    files: h.index.slice(offset, offset + limit), filesTotal: h.index.length, offset, limit,
    complete: offset + limit >= h.index.length,
    snapshot: { factsDigest: 'd'.repeat(64), filesTotal: h.index.length, builtAt: 'x' },
  })),
  getFileReportFacts: vi.fn(async (_sid, file) => {
    h.calls++
    if (h.failing.has(file)) throw new Error('HTTP 503')
    const change = (seq, verification) => ({
      id: verification === 'verified' ? `${file}::1.1.1::${seq}` : `${file}::1.1.1::u${String(seq).padStart(16, '0')}`,
      ruleId: '1.1.1', sc: '1.1.1', seq: verification === 'verified' ? seq : null, before: '', after: `alt ${seq}`,
      verification, location: null,
    })
    return {
      identity: { file, currentArtifact: { kind: 'corrected', sha256: 'c'.repeat(64) } },
      savedChanges: [change(0, 'verified'), change(1, 'verified'), change(2, 'not_verified')],
      savedChangesComplete: true, savedChangesTotal: 3, reviews: {},
    }
  }),
}))
const { gatherRemediationEvidence, FILE_FACTS_MAX } = await import('./remediationReportData.js')
const { buildRemediationModel, remediationReportModel, REMEDIATION_REVIEWER_TOTAL_CAP } = await import('./reportModel.js')

const name = (n) => `docs/d${String(n).padStart(4, '0')}.docx`
beforeEach(() => {
  h.index = Array.from({ length: N_DOCS }, (_, n) => ({
    file: name(n), savedChangesVerified: 2, savedChangesUnverified: n === 261 ? null : 1, humanReviews: { pending: 3 },
  }))
  h.failing = new Set([name(3)])
  h.calls = 0
})

const build = async (mode) => {
  const ev = await gatherRemediationEvidence('s1')
  const files = h.index.map((r) => ({ file: r.file, remediated_at: '2026-09-01' }))
  const m = buildRemediationModel({ ...ev, files, scanId: 's1' })
  return { ev, m, model: remediationReportModel(m, { mode, reviewsByFile: ev.reviewsByFile, currentShaByFile: ev.currentShaByFile }) }
}

describe('S3', () => {
  it('lists EVERY document it did not itemise, with the index’s own counts, and never a 0 for an unknown', async () => {
    const { ev, model } = await build('full')
    expect(h.calls).toBe(FILE_FACTS_MAX)
    const unitemised = ev.unitemisedDocuments.map((u) => u.file)
    expect(unitemised).toHaveLength(N_DOCS - FILE_FACTS_MAX + 1)       // 12 beyond the limit + 1 unreadable
    expect(unitemised).toContain(name(3))
    expect(unitemised).toContain(name(261))
    const table = model.blocks.find((b) => b.id === 'appendix-remediation-unitemised')
    expect(table.rows.map((r) => r[0]).sort()).toEqual([...unitemised].sort())
    const last = table.rows.find((r) => r[0] === name(261))
    expect(last[3]).toBe('Not recorded')                                   // null stays null
    expect(table.rows.find((r) => r[0] === name(3))[1]).toMatch(/could not be read \(HTTP 503\)/)
    // the note names a few and says where the rest are — no bare "…"
    const note = ev.evidenceNotes.find((x) => /not itemised/.test(x))
    expect(note).toMatch(/and 2 more/)
    expect(note).toMatch(/Documents not itemised/)
  })

  it('uses the server’s totals over every document; itemised-only counts are never presented as totals', async () => {
    const { ev, model } = await build('full')
    expect(ev.savedChangesVerified).toBe(N_DOCS * 2)
    const d = Object.fromEntries(model.blocks.find((b) => b.k === 'decisionSummary').items.map((i) => [i.key, i]))
    expect(d.editsSaved.value).toBe(N_DOCS * 3)
    // one document's facts failed, so decisions as a whole are "not loaded" — not a count
    expect(d.humanChecksPending.value).toBeNull()
    expect(d.humanChecksPending.detail).toBe('Reviewer decisions were not loaded')
  })

  it('with every itemised document read, the awaiting count still covers only the itemised part and says so', async () => {
    h.failing = new Set()
    const { model } = await build('full')
    const d = Object.fromEntries(model.blocks.find((b) => b.k === 'decisionSummary').items.map((i) => [i.key, i]))
    expect(d.humanChecksPending.value).toBeNull()
    expect(d.humanChecksPending.detail).toMatch(/of the 750 itemised changes await a decision/)
  })

  it('a document that was not itemised never reads "no per-change record was stored"', async () => {
    const { model } = await build('reviewer')
    const i = model.blocks.findIndex((b) => b.k === 'heading' && b.text === 'd0003.docx')
    expect(model.blocks[i + 2].text).toMatch(/^Not itemised in this report — its evidence could not be read/)
  })

  it('bounds the Reviewer packet OVERALL and says so; Full evidence has every card', async () => {
    const reviewer = (await build('reviewer')).model
    const full = (await build('full')).model
    const cards = (m) => m.blocks.filter((b) => b.k === 'changeCard').length
    expect(cards(reviewer)).toBe(REMEDIATION_REVIEWER_TOTAL_CAP)
    expect(cards(full)).toBe((FILE_FACTS_MAX - 1) * 3)
    const texts = reviewer.blocks.filter((b) => b.k === 'text').map((b) => b.text).join('\n')
    expect(texts).toMatch(new RegExp(`at most ${REMEDIATION_REVIEWER_TOTAL_CAP} change cards in total`))
  })
})
