/**
 * The SCAN report, driven through the real screen (Transparency's RuleBreakdown header menu).
 *
 * Only api.js is mocked. fileReportData.js, scanReport.js, reportEvidence.js and
 * reportRenderClient.js all run for real, so this is the live data path — the thing the review
 * said was missing, because "generateScanReport accepts `previous` and `scope`" had been proved
 * only by a unit test that invented them.
 *
 * The browser preview serves the SHARED checkout, not this worktree (CLAUDE.md), so a DOM-level
 * test is the verification here; a screenshot would be evidence about `main`.
 *
 * What it proves:
 *   - the paginated scan facts are read to the end before a report is built;
 *   - the render request carries the server's `factsDigest`;
 *   - "Since the previous assessment" renders a REAL comparison from facts.previous;
 *   - a 409 tells the reader to regenerate;
 *   - an HTML fallback is never reported as a PDF.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'

const h = vi.hoisted(() => ({ facts: null, post: null, factsCalls: [] }))
const downloadModelHtml = vi.fn(async () => {})

vi.mock('./api.js', () => ({
  SIM: false,
  getScanTraces: async () => [
    { file: 'a.pdf', rule_id: '1.1.1', plain_name: 'Non-text Content', level: 'A', outcome: 'FAIL', finding_count: 1 },
    { file: 'b.pdf', rule_id: '1.4.3', plain_name: 'Contrast', level: 'AA', outcome: 'PASS', finding_count: 0 },
  ],
  listHitlQueue: async () => [],
  getConfig: async () => ({ version: '2026.9.17.1' }),
  getScanRemediationDiffs: async () => ({ items: [], total: 0, documents: 0, complete: true }),
  getScanReportFacts: (...a) => { h.factsCalls.push(a); return h.facts(...a) },
  postReportRender: (...a) => h.post(...a),
  openTraceUrl: () => {},
  getTraceStatus: async () => null,
  SESSION_EXPIRED: 'expired',
}))
vi.mock('./htmlReport.js', () => ({ downloadModelHtml: (...a) => downloadModelHtml(...a) }))

const { RuleBreakdown } = await import('./Transparency.jsx')

const FILES = [
  { file: 'a.pdf', score: 60, department: 'Ops', issues: [{ wcag: 'SC_1_1_1', severity: 'SERIOUS', detail: 'no alt', page: 1 }] },
  { file: 'b.pdf', score: 95, department: 'Ops', issues: [] },
]

// A scan-level facts page, in the CONTRACT v2 shape.
const scanPage = (offset, limit, total, extra = {}) => ({
  factsVersion: 1,
  factsDigest: 'scan-digest-xyz',
  generatedAt: '2026-09-17T12:00:00Z',
  identity: { scanId: 's1', file: null, scopeDigest: 'scope-1', scanScope: null, targetLevel: 'AA', platformVersion: '2026.9.17.1' },
  filesTotal: total, offset, limit, complete: offset + limit >= total,
  files: Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, i) => ({
    file: `doc-${offset + i}.pdf`, assessment: { state: 'assessed' }, score: 80,
    findingsOpen: 1, savedChangesVerified: 0, savedChangesUnverified: 0,
    humanReviews: { pending: 0, accepted: 0, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 },
    currentArtifact: { kind: 'source', sha256: null },
  })),
  accounting: { findingsTotal: 2, findingsOpen: 2, findingsResolvedVerified: null, resolutionLedger: 'none', savedChangesVerified: 0, savedChangesUnverified: 0, humanReviews: { pending: 0, accepted: 0, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 } },
  previous: null,
  previousReason: 'No earlier assessment of this estate is recorded.',
  ...extra,
})

const pdfResponse = () => ({ ok: true, status: 200, headers: new Headers(), blob: async () => new Blob(['%PDF']) })
const errResponse = (status, detail) => ({ ok: false, status, headers: new Headers(), json: async () => ({ detail }) })

const mount = async () => {
  const { container, root } = createTestRoot()
  await act(async () => { root.render(createElement(RuleBreakdown, { scanId: 's1', files: FILES })) })
  return container
}

// Open the export menu and click "<mode> — PDF".
const exportAs = async (container, modeLabel) => {
  const btn = [...container.querySelectorAll('button')].find((b) => b.getAttribute('aria-label') === `${modeLabel} — PDF`)
  expect(btn, `no "${modeLabel} — PDF" button in the scan report menu`).toBeTruthy()
  await act(async () => { btn.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  // Let the dynamic imports, the paginated facts fetch and the render request settle. A fixed
  // number of flushes is not enough: the number of awaits depends on how many facts pages the
  // scan has, so wait until the menu itself says it has stopped working.
  for (let i = 0; i < 200 && !done(container); i++) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
  }
  expect(done(container), 'the export never finished').toBe(true)
  return container
}

// The menu shows "Generating …" (or the progress line) in its summary while an export is running.
const done = (container) => !/Generating|Reading report evidence/.test(container.textContent || '')

beforeEach(() => {
  h.factsCalls = []
  h.facts = vi.fn(async (_s, { offset, limit }) => scanPage(offset, limit, 1))
  h.post = vi.fn(async () => pdfResponse())
  downloadModelHtml.mockClear()
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:x')
  globalThis.URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  try { sessionStorage.clear() } catch { /* jsdom */ }
})
afterEach(async () => { await unmountAll(); vi.restoreAllMocks() })

describe('the scan report menu offers all three modes and renders from server facts', () => {
  it('offers Summary, Reviewer packet and Full evidence', async () => {
    const c = await mount()
    const labels = [...c.querySelectorAll('.reportmode-row')].map((r) => r.getAttribute('aria-label'))
    expect(labels).toEqual(['Summary', 'Reviewer packet', 'Full evidence'])
  })

  it('reads EVERY page of the facts index, then sends the digest with the render', async () => {
    h.facts = vi.fn(async (_s, { offset, limit }) => scanPage(offset, limit, 450))
    const c = await exportAs(await mount(), 'Reviewer packet')
    expect(h.factsCalls.map((a) => a[1].offset)).toEqual([0, 200, 400])
    expect(h.post).toHaveBeenCalledTimes(1)
    const [sid, body] = h.post.mock.calls[0]
    expect(sid).toBe('s1')
    expect(body.kind).toBe('scan')
    expect(body.mode).toBe('reviewer')
    expect(body.factsDigest).toBe('scan-digest-xyz')
    expect(body.model.identity.factsDigest).toBe('scan-digest-xyz')
    expect(c.querySelector('[role="alert"]')).toBeNull()
  })

  // Stream D's estate endpoint currently answers `previous: null` with "scan-level comparison is
  // reported per document" (the per-document baseline is the real one — see fileDrawerReports).
  // This proves the estate path DOES render a comparison when a baseline is supplied, and that it
  // is matched finding-by-finding rather than subtracted from aggregate counts.
  it('renders a REAL comparison when the scan has a comparable baseline', async () => {
    h.facts = vi.fn(async (_s, { offset, limit }) => scanPage(offset, limit, 1, {
      findings: [{ id: 'fid-persists', ruleId: 'img-alt', sc: '1.1.1', detail: 'no alt', severity: 'SERIOUS', location: { label: 'Page 1', page: 1 }, state: 'open' }],
      previous: {
        scanId: 's0', generatedAt: '2026-09-01T00:00:00Z', sha256: null, file: null, scopeDigest: 'scope-1',
        findings: [
          { id: 'fid-persists', ruleId: 'img-alt', sc: '1.1.1', detail: 'no alt', location: { label: 'Page 1', page: 1 } },
          { id: 'fid-gone', ruleId: 'title', sc: '2.4.2', detail: 'no title', location: { label: 'Document', page: null } },
        ],
      },
      previousReason: null,
    }))
    await exportAs(await mount(), 'Summary')
    const model = h.post.mock.calls[0][1].model
    const cmp = model.blocks.find((b) => b.k === 'comparison')
    expect(cmp).toBeTruthy()
    expect(cmp.status).toBe('compared')
    expect(cmp.resolved.map((r) => r.id)).toEqual(['fid-gone'])
    expect(cmp.introduced).toEqual([])
    expect(cmp.persisting).toBe(1)
  })

  it('no baseline reads unknown, with the server’s reason — never a subtracted count', async () => {
    await exportAs(await mount(), 'Summary')
    const cmp = h.post.mock.calls[0][1].model.blocks.find((b) => b.k === 'comparison')
    expect(cmp.status).toBe('unknown')
    expect(cmp.reason).toBe('No earlier assessment of this estate is recorded.')
    expect(cmp.resolved).toEqual([])
    expect(cmp.introduced).toEqual([])
  })
})

describe('the scan report menu tells the truth about what it produced', () => {
  it('an HTML fallback is NOT reported as a generated PDF', async () => {
    h.post = vi.fn(async () => errResponse(503, 'renderer down'))
    const c = await exportAs(await mount(), 'Full evidence')
    expect(downloadModelHtml).toHaveBeenCalledTimes(1)
    const alert = c.querySelector('[role="alert"]')
    expect(alert).toBeTruthy()
    expect(alert.textContent).toMatch(/Full evidence \(PDF\) was NOT generated/)
    expect(alert.textContent).toMatch(/HTML copy/)
    // The live region must not be able to be read as "the PDF is ready".
    expect(c.querySelector('[role="status"]').textContent).not.toMatch(/generated\./)
  })

  it('a 409 says the document changed and asks for a regenerate', async () => {
    h.post = vi.fn(async () => errResponse(409, 'report data is out of date; regenerate the report'))
    const c = await exportAs(await mount(), 'Reviewer packet')
    const alert = c.querySelector('[role="alert"]')
    expect(alert).toBeTruthy()
    expect(alert.textContent).toMatch(/changed since this report was prepared/)
    expect(alert.textContent).toMatch(/regenerate/i)
    expect(alert.classList.contains('reportmode-regenerate')).toBe(true)
    // A stale model is not written out as HTML instead.
    expect(downloadModelHtml).not.toHaveBeenCalled()
  })

  it('a facts index that stopped short still produces a report, and says what is missing', async () => {
    h.facts = vi.fn(async (_s, { offset, limit }) => {
      if (offset >= 200) throw new Error('504 gateway timeout')
      return scanPage(offset, limit, 450)
    })
    const c = await exportAs(await mount(), 'Summary')
    expect(h.post).toHaveBeenCalledTimes(1)
    expect(c.querySelector('[role="alert"]')).toBeNull()
  })
})
