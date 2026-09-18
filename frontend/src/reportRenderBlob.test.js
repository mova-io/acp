// Contract 5 — renderReportBlob renders WITHOUT downloading. The packet ZIP needs the bytes of many
// reports and must never trigger a click per file; an HTML fallback must be labelled as HTML.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const post = vi.fn()
const reportHtmlFromModel = vi.fn((m) => `<!doctype html><title>${m.docTitle}</title>`)

vi.mock('./api.js', () => ({
  postReportRender: (...a) => post(...a),
  SESSION_EXPIRED: 'Your session expired',
}))
vi.mock('./htmlReport.js', () => ({
  reportHtmlFromModel: (...a) => reportHtmlFromModel(...a),
  downloadModelHtml: vi.fn(async () => {}),
}))

const { renderReportBlob, renderReportPdf, CANCELLED_MESSAGE } = await import('./reportRenderClient.js')

const MODEL = { docTitle: 'Report', blocks: [{ k: 'heading', text: 'Decision summary' }] }
const pdfResponse = () => ({ ok: true, status: 200, headers: new Headers(), blob: async () => new Blob(['%PDF-1.7'], { type: 'application/pdf' }) })
const errResponse = (status, detail) => ({ ok: false, status, headers: new Headers(), json: async () => ({ detail }) })
const D = 'a'.repeat(64)

let clicks
beforeEach(() => {
  post.mockReset(); reportHtmlFromModel.mockClear()
  clicks = []
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:x')
  globalThis.URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () { clicks.push(this.download) })
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})
afterEach(() => vi.restoreAllMocks())

describe('renderReportBlob', () => {
  it('returns the PDF bytes and a filename, and downloads nothing', async () => {
    post.mockResolvedValue(pdfResponse())
    const res = await renderReportBlob({ scanId: 's1', kind: 'file', file: 'a/b.pdf', mode: 'full', model: MODEL, factsDigest: D })
    expect(res.ok).toBe(true)
    expect(res.format).toBe('pdf')
    expect(res.blob).toBeInstanceOf(Blob)
    expect(await res.blob.text()).toBe('%PDF-1.7')
    expect(res.filename).toMatch(/\.pdf$/)
    expect(post.mock.calls[0][1]).toMatchObject({ kind: 'file', file: 'a/b.pdf', mode: 'full', factsDigest: D })
    expect(clicks).toEqual([])
  })

  it.each([
    ['no server (demo mode)', () => post.mockResolvedValue(null), null],
    ['network failure', () => post.mockRejectedValue(new TypeError('Failed to fetch')), null],
    ['a 503', () => post.mockResolvedValue(errResponse(503, 'down')), 503],
    ['a 413', () => post.mockResolvedValue(errResponse(413, 'too large')), 413],
  ])('falls back to an HTML blob LABELLED html on %s — never called a PDF', async (_, arrange, status) => {
    arrange()
    const res = await renderReportBlob({ scanId: 's1', kind: 'file', file: 'a.docx', mode: 'reviewer', model: MODEL, factsDigest: D })
    expect(res.ok).toBe(false)
    expect(res.format).toBe('html')
    expect(res.fallback).toBe('html')
    expect(res.status).toBe(status)
    expect(res.htmlBlob.type).toMatch(/text\/html/)
    expect(await res.htmlBlob.text()).toContain('<title>Report</title>')
    expect(res.htmlFilename).toMatch(/\.html$/)
    expect(res.message).toMatch(/HTML instead of PDF/)
    expect(clicks).toEqual([])
  })

  it.each([401, 403, 404, 409, 422])('a %i refusal produces NO document of either kind', async (status) => {
    post.mockResolvedValue(errResponse(status, 'nope'))
    const res = await renderReportBlob({ scanId: 's1', kind: 'file', file: 'a.docx', mode: 'full', model: MODEL, factsDigest: D })
    expect(res).toMatchObject({ ok: false, status, fallback: 'none' })
    expect(res.htmlBlob).toBeUndefined()
    expect(res.format).toBeUndefined()
    if (status === 409) expect(res.regenerate).toBe(true)
    expect(reportHtmlFromModel).not.toHaveBeenCalled()
  })

  it('an already-aborted signal never reaches the server', async () => {
    const ac = new AbortController(); ac.abort()
    const res = await renderReportBlob({ scanId: 's1', kind: 'file', file: 'a.docx', mode: 'full', model: MODEL, factsDigest: D, signal: ac.signal })
    expect(res).toMatchObject({ ok: false, aborted: true, message: CANCELLED_MESSAGE })
    expect(post).not.toHaveBeenCalled()
  })

  it('aborting mid-request settles at once as cancelled, with no fallback document', async () => {
    let release
    post.mockImplementation(() => new Promise((r) => { release = r }))
    const ac = new AbortController()
    const pending = renderReportBlob({ scanId: 's1', kind: 'file', file: 'a.docx', mode: 'full', model: MODEL, factsDigest: D, signal: ac.signal })
    await Promise.resolve()
    ac.abort()
    const res = await pending
    expect(res).toMatchObject({ ok: false, aborted: true, fallback: 'none' })
    expect(res.htmlBlob).toBeUndefined()
    expect(post.mock.calls[0][2]).toEqual({ signal: ac.signal })   // passed through to the transport
    release(pdfResponse())
  })

  it('htmlFallback:false reports the fallback without building one', async () => {
    post.mockResolvedValue(errResponse(502, 'bad gateway'))
    const res = await renderReportBlob({ scanId: 's1', kind: 'scan', mode: 'summary', model: MODEL, factsDigest: D, htmlFallback: false })
    expect(res).toMatchObject({ ok: false, fallback: 'html', status: 502 })
    expect(res.htmlBlob).toBeUndefined()
    expect(reportHtmlFromModel).not.toHaveBeenCalled()
  })

  it('renderReportPdf is built on it and still downloads exactly one PDF', async () => {
    post.mockResolvedValue(pdfResponse())
    const res = await renderReportPdf({ scanId: 's1', kind: 'file', file: 'x.pdf', mode: 'summary', model: MODEL, factsDigest: D })
    expect(res).toEqual({ ok: true, filename: 'accessibility-file-summary-x.pdf.pdf' })
    expect(clicks).toEqual([res.filename])
  })
})
