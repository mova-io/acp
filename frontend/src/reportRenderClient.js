// Client for the accessible SERVER report renderer (POST /scans/{sid}/report-render,
// api/routes/report_render.py → api/report_render.py).
//
// Why this exists: the browser PDF path (jsPDF, pdfReport.js) produced untagged PDFs with no
// bookmarks and a WinAnsi font that silently dropped ✓ and →. Every PDF download now goes through
// the server, which renders the same report MODEL into a tagged PDF with embedded fonts.
//
// There is deliberately NO jsPDF fallback. When the server cannot be used (SIM/demo mode, the API
// is unreachable, or it fails with a 5xx) the report is downloaded as the accessible HTML export
// built from the same model, and the caller gets a message saying so. A request the server
// REFUSES (401/403/404/413/422) is not retried as HTML: that answer is about the request — whose
// scan, how large — and quietly producing a document anyway would hide it.

import { postReportRender, SESSION_EXPIRED } from './api.js'

export const REPORT_MODES = Object.freeze({ summary: 'Summary', reviewer: 'Reviewer packet', full: 'Full evidence' })
export const REPORT_KINDS = Object.freeze(['file', 'scan', 'remediation'])

export const HTML_FALLBACK_MESSAGE = 'The PDF service is not available here, so the report was downloaded as an accessible HTML file instead. Open it in a browser and print or save it as needed.'

const slug = (s) => String(s || '').replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^[-.]+|[-.]+$/g, '').slice(0, 80) || 'report'

export function reportPdfFilename({ kind, mode, scanId, file }) {
  return `accessibility-${kind}-${mode}-${slug(file || scanId)}.pdf`
}

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 60000)
}

async function detailOf(res) {
  try {
    const body = await res.json()
    const d = body && body.detail
    if (typeof d === 'string') return d
    if (d && typeof d === 'object' && typeof d.message === 'string') return d.message
  } catch { /* not JSON */ }
  return null
}

const REFUSED = {
  401: 'Sign in again to download this report.',
  403: 'Your account cannot download this report.',
  404: 'This report is not available to your account — the scan or file was not found.',
  413: 'This report is too large to render as a PDF. Try the Summary or Reviewer packet mode.',
  422: 'The report could not be rendered because its content was not accepted.',
}

async function htmlFallback(model, reason) {
  try {
    const { downloadModelHtml } = await import('./htmlReport.js')
    await downloadModelHtml(model)
  } catch (e) {
    return { ok: false, fallback: 'none', message: `The PDF service is not available and the HTML export failed: ${e?.message || e}` }
  }
  // eslint-disable-next-line no-console
  console.warn(`[report] server PDF unavailable (${reason}); downloaded the HTML export instead`)
  return { ok: false, fallback: 'html', message: HTML_FALLBACK_MESSAGE }
}

// Render `model` on the server and download the PDF.
// Resolves { ok: true, filename } | { ok: false, fallback: 'html' | 'none', message, status? }.
export async function renderReportPdf({ scanId, kind, file = null, mode = 'reviewer', model, filename = null } = {}) {
  if (!model || typeof model !== 'object') return { ok: false, fallback: 'none', message: 'No report content to render.' }
  if (!REPORT_KINDS.includes(kind)) return { ok: false, fallback: 'none', message: `Unknown report kind: ${kind}` }
  if (!REPORT_MODES[mode]) return { ok: false, fallback: 'none', message: `Unknown report mode: ${mode}` }
  if (!scanId) return htmlFallback(model, 'no scan id')

  let res
  try {
    res = await postReportRender(scanId, { kind, file: file ?? null, mode, model: { ...model, mode, kind } })
  } catch (e) {
    return htmlFallback(model, `request failed: ${e?.message || e}`)
  }
  if (!res) return htmlFallback(model, 'demo mode has no report server')

  if (res.ok) {
    const name = filename || reportPdfFilename({ kind, mode, scanId, file })
    downloadBlob(await res.blob(), name)
    return { ok: true, filename: name }
  }
  if (res.status >= 500) return htmlFallback(model, `server answered ${res.status}`)
  if (res.status === 401 && res.headers?.get?.('X-Acp-Auth') === 'session') {
    window.dispatchEvent(new CustomEvent('acp:session-expired', { detail: { reason: SESSION_EXPIRED } }))
  }
  const detail = await detailOf(res)
  const base = REFUSED[res.status] || `The report could not be rendered (HTTP ${res.status}).`
  return { ok: false, fallback: 'none', status: res.status, message: detail && res.status !== 404 ? `${base} ${detail}` : base }
}
