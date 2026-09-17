/**
 * buildFileReportData — the live payload behind every document report mode.
 */
import { describe, it, expect, vi } from 'vitest'

vi.mock('./api.js', () => ({ SIM: false, fetchChangeReviews: async () => ({}), putChangeReview: async () => ({}) }))
const { buildFileReportData, collectPreviews, findPrevious, PREVIEW_MAX } = await import('./fileReportData.js')

const SHA = 'a'.repeat(64)
const file = {
  file: 'guide.pdf', score: 70, engine: 'pdf',
  issues: [
    { wcag: 'SC_1_1_1', rule_id: 'img-alt', severity: 'SERIOUS', detail: 'Image 1 has no alt', page: 2 },
    { wcag: 'SC_1_1_1', rule_id: 'img-alt', severity: 'SERIOUS', detail: 'Image 2 has no alt', page: 5 },
    { wcag: 'SC_1_1_1', rule_id: 'img-alt', severity: 'SERIOUS', detail: 'Image 3 has no alt', page: 5 },
    { wcag: 'SC_2_4_2', rule_id: 'title', severity: 'MODERATE', detail: 'No document title' },
  ],
}
const rows = [
  { id: '1.1.1', name: 'Non-text Content', outcome: 'FIXED', count: 3 },
  { id: '2.4.2', name: 'Page Titled', outcome: 'FAIL', count: 1 },
  { id: '3.1.1', name: 'Language of Page', outcome: 'UNCHECKED', count: 0 },
]
// 30 diffs — more than any display cap — with before/after longer than a card would show.
const diffs = Array.from({ length: 30 }, (_, i) => ({
  rule_id: '1.1.1', seq: i, before: `before ${i} `.repeat(80), after: `after ${i} `.repeat(80), note: 'n', verified: true,
}))
const png = new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' })

const deps = (over = {}) => ({
  getConfig: async () => ({ version: '2026.9.17.1' }),
  getFileRemediationDiffs: vi.fn(async () => diffs),
  getDecisions: async () => ({ 'guide.pdf': { assignee: 'owner@example.com', triage: 'inscope' } }),
  getFilePage: vi.fn(async () => png),
  getScan: async () => ({ run: {}, files: [] }),
  getScanDiff: async () => ({ no_baseline: true }),
  loadReviews: async () => ({ ok: true, sim: false, artifact: { sourceSha256: null, correctedSha256: SHA, currentSha256: SHA },
    reviews: { 'guide.pdf::1.1.1::0': { verdict: 'accepted', reviewer: 'r', at: '2026-09-17T00:00:00Z', artifact_sha256: SHA, stale: false } } }),
  ...over,
})

describe('buildFileReportData', () => {
  it('carries every finding, the full diff list, identity, reviews and assignee', async () => {
    const dp = deps()
    const d = await buildFileReportData({ file, scanId: 's1', mode: 'reviewer', rows, targetLevel: 'AA', deps: dp, now: new Date('2026-09-17T12:00:00Z') })
    // strict: a transport failure must reject, not come back as an empty list
    expect(dp.getFileRemediationDiffs).toHaveBeenCalledWith('s1', 'guide.pdf', { strict: true })
    expect(d.mode).toBe('reviewer')
    const all = d.rows.flatMap((r) => r.fileIssues)
    expect(all).toHaveLength(file.issues.length)
    expect(new Set(all.map((i) => i.id)).size).toBe(file.issues.length)
    const r111 = d.rows.find((r) => r.id === '1.1.1')
    expect(r111.fileIssues.map((i) => i.detail)).toEqual(['Image 1 has no alt', 'Image 2 has no alt', 'Image 3 has no alt'])
    expect(r111.fileIssues[1].location).toMatchObject({ page: 5 })
    expect(r111.fileIssues[0]).toHaveProperty('severity', 'SERIOUS')
    expect(r111.verified).toBe(true)
    expect(d.rows.find((r) => r.id === '2.4.2').verified).toBe(false)
    expect(d.rows.find((r) => r.id === '3.1.1').fileIssues).toEqual([])
    expect(d.diffs).toHaveLength(30)
    expect(d.diffs[29].before).toBe(diffs[29].before)
    expect(d.diffsComplete).toBe(true)
    expect(d.diffsTotal).toBe(30)
    expect(d.identity).toEqual({
      scanId: 's1', file: 'guide.pdf', sourceSha256: null, sourceChecksum: null, sourceChecksumKind: null,
      correctedSha256: SHA, artifactVersion: SHA,
      currentArtifact: { kind: 'unknown', sha256: SHA },
      factsDigest: null, remediatedAt: null,
      generatedAt: '2026-09-17T12:00:00.000Z', platformVersion: '2026.9.17.1', targetLevel: 'AA',
    })
    expect(d.reviews['guide.pdf::1.1.1::0']).toMatchObject({ verdict: 'accepted', stale: false })
    expect(d.artifact.currentSha256).toBe(SHA)
    expect(d.assignee).toBe('owner@example.com')
    expect(d.locationHref).toBeNull()
  })

  it('a failed diff read is incomplete and unknown, not zero changes', async () => {
    const d = await buildFileReportData({ file, scanId: 's1', mode: 'full', rows, targetLevel: 'AA',
      deps: deps({ getFileRemediationDiffs: async () => { throw new Error('503') } }) })
    expect(d.diffsComplete).toBe(false)
    expect(d.diffsTotal).toBeNull()
    expect(d.diffsError).toBe('503')
    expect(d.rows.every((r) => r.verified === false)).toBe(true)
  })

  it('no recorded reviews is null only when they could not be read', async () => {
    const d = await buildFileReportData({ file, scanId: 's1', mode: 'full', rows, targetLevel: 'AA',
      deps: deps({ loadReviews: async () => ({ ok: false, artifact: { currentSha256: null }, reviews: {}, error: 'down' }) }) })
    expect(d.reviews).toBeNull()
    expect(d.reviewsError).toBe('down')
    expect(d.identity.artifactVersion).toBeNull()
  })
})

describe('collectPreviews', () => {
  it('renders the pages findings point at — not page one only — and names what is left out', async () => {
    const many = { file: 'big.pdf', issues: Array.from({ length: PREVIEW_MAX + 3 }, (_, i) => ({ page: i + 3 })) }
    const getFilePage = vi.fn(async () => png)
    const { previews, status } = await collectPreviews({ scanId: 's', file: many, diffs: [], getFilePage })
    expect(getFilePage.mock.calls.map((c) => c[2])).toEqual([3, 4, 5, 6, 7, 8, 9, 10])
    expect(Object.keys(previews).map(Number)).toEqual([3, 4, 5, 6, 7, 8, 9, 10])
    // Every entry says which VERSION it is. Here no digest is recorded, so the honest answer is
    // "not verified" — never "after the edit".
    expect(previews[3].src).toMatch(/^data:image\/png;base64,/)
    expect(previews[3].provenance).toBe('unverified')
    expect(previews[3].caption).toBe('Document preview — version not verified — page 3')
    expect(status.notIncluded).toEqual([11, 12, 13])
    expect(status.reason).toMatch(/3 more referenced pages are not included \(pages 11, 12, 13\)/)
  })

  it('a page that could not be rendered is unavailable, not silently dropped', async () => {
    const { status } = await collectPreviews({ scanId: 's', file, diffs: [], getFilePage: async (s, f, p) => (p === 2 ? png : null) })
    expect(status.included).toEqual([2])
    expect(status.unavailable).toEqual([5])
  })

  it('non-PDF documents say previews are not rendered', async () => {
    const { previews, status } = await collectPreviews({ scanId: 's', file: { file: 'a.docx', issues: [{ page: 1 }] }, diffs: [], getFilePage: vi.fn() })
    expect(previews).toEqual({})
    expect(status.reason).toMatch(/only rendered for PDF/)
  })
})

describe('findPrevious', () => {
  const run = (id, scope = { a: 1 }) => ({ id, scan_scope: scope, rubric_hash: 'h1', completed_at: `${id}-at` })
  const rec = (drive, issues) => ({ file: 'guide.pdf', drive_file_id: drive, issues, corrected_sha256: null })
  const scans = (cur, prev) => async (id) => (id === 'cur' ? cur : prev)

  it('compares only an earlier snapshot of the same source file, with its findings', async () => {
    const r = await findPrevious({ scanId: 'cur', fileName: 'guide.pdf', targetLevel: 'AA',
      getScan: scans({ run: run('cur'), files: [rec('D1', file.issues.slice(0, 1))] }, { run: run('old'), files: [rec('D1', file.issues)] }),
      getScanDiff: async () => ({ prev_id: 'old' }) })
    expect(r.previous).toMatchObject({ scanId: 'old', generatedAt: 'old-at', file: 'guide.pdf' })
    expect(r.previous.findings).toHaveLength(4)
    expect(r.previous.scope).toEqual(r.scope)
    expect(r.scope).toEqual({ scanScope: { a: 1 }, rubricHash: 'h1', targetLevel: 'AA' })
    expect(r.currentFindings).toHaveLength(1)
    expect(r.previous.findings[0].id).toBe(r.currentFindings[0].id)
  })

  it('a same-named file from a different source is not a previous snapshot', async () => {
    const r = await findPrevious({ scanId: 'cur', fileName: 'guide.pdf', targetLevel: 'AA',
      getScan: scans({ run: run('cur'), files: [rec('D1', [])] }, { run: run('old'), files: [rec('D2', [])] }),
      getScanDiff: async () => ({ prev_id: 'old' }) })
    expect(r.previous).toBeNull()
    expect(r.previousReason).toMatch(/cannot be shown to be the same source file/)
  })

  it('no baseline → null with a reason', async () => {
    const r = await findPrevious({ scanId: 'cur', fileName: 'guide.pdf', targetLevel: 'AA',
      getScan: scans({ run: run('cur'), files: [] }), getScanDiff: async () => ({ no_baseline: true }) })
    expect(r.previous).toBeNull()
    expect(r.previousReason).toMatch(/No earlier assessment/)
  })

  it('an unrecorded scope is null so the comparison cannot claim like-for-like', async () => {
    const r = await findPrevious({ scanId: 'cur', fileName: 'guide.pdf', targetLevel: 'AA',
      getScan: scans({ run: { ...run('cur'), scan_scope: null }, files: [rec('D1', [])] }, { run: run('old'), files: [rec('D1', [])] }),
      getScanDiff: async () => ({ prev_id: 'old' }) })
    expect(r.scope).toBeNull()
  })
})
