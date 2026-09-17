// reportRenderClient — the one path every PDF download now takes (POST /scans/{sid}/report-render).
// Asserts behaviour, not source text: what is sent, what is downloaded, and what happens when the
// server is missing (HTML of the same model, said out loud) or refuses (no document at all).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const post = vi.fn()
const downloadModelHtml = vi.fn(async () => {})
const jspdfLoaded = vi.fn()

vi.mock('./api.js', () => ({
  postReportRender: (...a) => post(...a),
  SESSION_EXPIRED: 'Your session expired',
}))
vi.mock('./htmlReport.js', () => ({ downloadModelHtml: (...a) => downloadModelHtml(...a) }))
// Constructing a jsPDF document is what would mean an untagged PDF was produced.
vi.mock('jspdf', () => ({ jsPDF: function () { jspdfLoaded() }, AcroFormCheckBox: function () {} }))

const { renderReportPdf, reportPdfFilename, REPORT_MODES, HTML_FALLBACK_MESSAGE } = await import('./reportRenderClient.js')

const MODEL = { docTitle: 'Report', kind: 'file', mode: 'reviewer', blocks: [{ k: 'heading', text: 'Decision summary' }] }
const pdfResponse = () => ({ ok: true, status: 200, headers: new Headers(), blob: async () => new Blob(['%PDF-1.7'], { type: 'application/pdf' }) })
const errResponse = (status, detail, headers = {}) => ({ ok: false, status, headers: new Headers(headers), json: async () => ({ detail }) })

let clicks
beforeEach(() => {
  post.mockReset(); downloadModelHtml.mockClear(); jspdfLoaded.mockClear()
  clicks = []
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:x')
  globalThis.URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () { clicks.push(this.download) })
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})
afterEach(() => vi.restoreAllMocks())

describe('renderReportPdf', () => {
  it('uses the exact UI labels for the three modes', () => {
    expect(REPORT_MODES).toEqual({ summary: 'Summary', reviewer: 'Reviewer packet', full: 'Full evidence' })
  })

  it('posts kind/file/mode/model and downloads the returned PDF with the mode in its name', async () => {
    post.mockResolvedValue(pdfResponse())
    const res = await renderReportPdf({ scanId: 's1', kind: 'file', file: 'dir/Quarterly report.docx', mode: 'reviewer', model: MODEL })
    expect(post).toHaveBeenCalledTimes(1)
    const [sid, body] = post.mock.calls[0]
    expect(sid).toBe('s1')
    expect(body).toMatchObject({ kind: 'file', file: 'dir/Quarterly report.docx', mode: 'reviewer' })
    expect(body.model.blocks).toEqual(MODEL.blocks)
    expect(res).toEqual({ ok: true, filename: 'accessibility-file-reviewer-dir-Quarterly-report.docx.pdf' })
    expect(clicks).toEqual([res.filename])
    expect(downloadModelHtml).not.toHaveBeenCalled()
  })

  it('in SIM/demo mode (no server) downloads the HTML of the same model and says so — never jsPDF', async () => {
    post.mockResolvedValue(null)
    const res = await renderReportPdf({ scanId: 's1', kind: 'scan', mode: 'summary', model: MODEL })
    expect(res).toMatchObject({ ok: false, fallback: 'html', message: HTML_FALLBACK_MESSAGE })
    expect(downloadModelHtml).toHaveBeenCalledWith(MODEL)
    expect(jspdfLoaded).not.toHaveBeenCalled()
    expect(console.warn).toHaveBeenCalled()
  })

  it.each([
    ['network failure', () => post.mockRejectedValue(new TypeError('Failed to fetch'))],
    ['server error', () => post.mockResolvedValue(errResponse(503, 'renderer unavailable'))],
  ])('falls back to HTML on %s', async (_, arrange) => {
    arrange()
    const res = await renderReportPdf({ scanId: 's1', kind: 'remediation', mode: 'full', model: MODEL })
    expect(res.fallback).toBe('html')
    expect(downloadModelHtml).toHaveBeenCalledTimes(1)
    expect(clicks).toEqual([])
  })

  it.each([
    [404, /not available to your account/],
    [413, /too large/],
    [422, /not accepted.*unsupported block kind/],
  ])('a %i refusal produces no document and a message', async (status, text) => {
    post.mockResolvedValue(errResponse(status, 'block 3: unsupported block kind (script)'))
    const res = await renderReportPdf({ scanId: 's1', kind: 'file', file: 'a.pdf', mode: 'full', model: MODEL })
    expect(res).toMatchObject({ ok: false, fallback: 'none', status })
    expect(res.message).toMatch(text)
    expect(downloadModelHtml).not.toHaveBeenCalled()
    expect(clicks).toEqual([])
  })

  it('a gate 401 raises the app-wide session-expired event', async () => {
    const seen = vi.fn()
    window.addEventListener('acp:session-expired', seen)
    post.mockResolvedValue(errResponse(401, 'Not authenticated', { 'X-Acp-Auth': 'session' }))
    const res = await renderReportPdf({ scanId: 's1', kind: 'scan', mode: 'summary', model: MODEL })
    window.removeEventListener('acp:session-expired', seen)
    expect(res).toMatchObject({ ok: false, fallback: 'none', status: 401 })
    expect(seen).toHaveBeenCalledTimes(1)
  })

  it('rejects unknown modes and kinds without calling the server', async () => {
    expect((await renderReportPdf({ scanId: 's', kind: 'file', mode: 'everything', model: MODEL })).ok).toBe(false)
    expect((await renderReportPdf({ scanId: 's', kind: 'estate', mode: 'full', model: MODEL })).ok).toBe(false)
    expect(post).not.toHaveBeenCalled()
  })

  it('names files safely', () => {
    expect(reportPdfFilename({ kind: 'scan', mode: 'summary', scanId: 'abc/../x', file: null })).toBe('accessibility-scan-summary-abc-..-x.pdf')
  })
})

describe('pdfReport exports delegate to the server renderer', () => {
  it('exportFileCertification / exportRemediationReport build the shared model and render it on the server', async () => {
    post.mockResolvedValue(pdfResponse())
    const { exportFileCertification, exportRemediationReport } = await import('./pdfReport.js')
    const file = await exportFileCertification({
      scanId: 's9', file: 'a.docx', targetLevel: 'AA', score: 80, rows: [], diffs: [],
      date: 'Sep 17, 2026', timestamp: 'Sep 17, 2026 10:00',
    })
    expect(file.ok).toBe(true)
    let [, body] = post.mock.calls[0]
    expect(body).toMatchObject({ kind: 'file', file: 'a.docx', mode: 'full' })
    expect(Array.isArray(body.model.blocks)).toBe(true)
    expect(body.model.blocks.length).toBeGreaterThan(0)

    const rem = await exportRemediationReport({ scanId: 's9', files: [], diffsByFile: {}, appliedFixes: [], mode: 'reviewer' })
    expect(rem.ok).toBe(true);
    [, body] = post.mock.calls[1]
    expect(body).toMatchObject({ kind: 'remediation', file: null, mode: 'reviewer' })
    expect(jspdfLoaded).not.toHaveBeenCalled()
  })
})
