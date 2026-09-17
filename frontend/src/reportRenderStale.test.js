/**
 * Stale-content binding: the render request carries `factsDigest`, and a 409 is surfaced as an
 * instruction to regenerate — never as an HTML copy of the stale report, and never silently.
 *
 * The defect this covers: the server stamps its own authoritative identity (scan id, file, source
 * and corrected sha) onto whatever model the client posts. Without a digest to compare, a model
 * built from evidence that has since been re-remediated gets re-stamped as current — an old report
 * describing itself as a picture of the document as it is now.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const post = vi.fn()
const downloadModelHtml = vi.fn(async () => {})

vi.mock('./api.js', () => ({ postReportRender: (...a) => post(...a), SESSION_EXPIRED: 'expired' }))
vi.mock('./htmlReport.js', () => ({ downloadModelHtml: (...a) => downloadModelHtml(...a) }))

const { renderReportPdf, STALE_FACTS_MESSAGE } = await import('./reportRenderClient.js')

const model = (identity = {}) => ({
  docTitle: 'Report', kind: 'file', mode: 'reviewer',
  identity: { scanId: 's1', file: 'a.pdf', ...identity },
  blocks: [{ k: 'heading', text: 'Decision summary' }],
})
const pdfResponse = () => ({ ok: true, status: 200, headers: new Headers(), blob: async () => new Blob(['%PDF']) })
const errResponse = (status, detail) => ({ ok: false, status, headers: new Headers(), json: async () => ({ detail }) })

beforeEach(() => {
  post.mockReset(); downloadModelHtml.mockClear()
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:x')
  globalThis.URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})
afterEach(() => vi.restoreAllMocks())

describe('factsDigest travels with every render request', () => {
  it('sends the digest carried on the model identity', async () => {
    post.mockResolvedValue(pdfResponse())
    await renderReportPdf({ scanId: 's1', kind: 'file', file: 'a.pdf', mode: 'reviewer', model: model({ factsDigest: 'abc' }) })
    expect(post.mock.calls[0][1].factsDigest).toBe('abc')
  })

  it.each(['file', 'scan', 'remediation'])('binds the %s report too', async (kind) => {
    post.mockResolvedValue(pdfResponse())
    await renderReportPdf({ scanId: 's1', kind, file: kind === 'file' ? 'a.pdf' : null, mode: 'summary', model: model({ factsDigest: 'kind-digest' }) })
    expect(post.mock.calls[0][1]).toMatchObject({ kind, factsDigest: 'kind-digest' })
  })

  it('an explicit digest wins over the model’s', async () => {
    post.mockResolvedValue(pdfResponse())
    await renderReportPdf({ scanId: 's1', kind: 'scan', mode: 'full', model: model({ factsDigest: 'from-model' }), factsDigest: 'explicit' })
    expect(post.mock.calls[0][1].factsDigest).toBe('explicit')
  })

  it('sends null rather than a made-up digest when the facts could not be read', async () => {
    post.mockResolvedValue(pdfResponse())
    await renderReportPdf({ scanId: 's1', kind: 'file', file: 'a.pdf', mode: 'summary', model: model() })
    expect(post.mock.calls[0][1].factsDigest).toBeNull()
  })
})

describe('409 — the document changed since this report was prepared', () => {
  it('says regenerate, in words, and flags it as such', async () => {
    post.mockResolvedValue(errResponse(409, 'report data is out of date; regenerate the report'))
    const res = await renderReportPdf({ scanId: 's1', kind: 'file', file: 'a.pdf', mode: 'reviewer', model: model({ factsDigest: 'stale' }) })
    expect(res.ok).toBe(false)
    expect(res.status).toBe(409)
    expect(res.regenerate).toBe(true)
    expect(res.message).toBe(STALE_FACTS_MESSAGE)
    expect(res.message).toMatch(/changed since this report was prepared/)
    expect(res.message).toMatch(/regenerate/i)
  })

  it('does NOT write the stale model out as HTML instead', async () => {
    post.mockResolvedValue(errResponse(409, 'report data is out of date; regenerate the report'))
    const res = await renderReportPdf({ scanId: 's1', kind: 'scan', mode: 'full', model: model({ factsDigest: 'stale' }) })
    // Delivering the same stale evidence in another file format is exactly what the refusal is
    // there to prevent, and the HTML copy would carry nothing on its face to say so.
    expect(downloadModelHtml).not.toHaveBeenCalled()
    expect(res.fallback).toBe('none')
  })
})
