// Client for the accessible SERVER report renderer (POST /scans/{sid}/report-render,
// api/routes/report_render.py → api/report_render.py).
//
// Why this exists: the browser PDF path (jsPDF, pdfReport.js) produced untagged PDFs with no
// bookmarks and a WinAnsi font that silently dropped ✓ and →. Every report PDF now goes through
// the server, which renders the same report MODEL into a tagged PDF with embedded fonts. The one
// exception is the Overview's quarterly governance summary (pdfReport.exportGovernanceReport),
// which is still drawn in the browser and is labelled as untagged in the menu and on its first page.
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

// The document changed between "facts fetched" and "render requested": the server recomputed the
// facts digest and refused to stamp its own authoritative identity onto evidence that is no longer
// current. This one is not an error to log — it is an instruction to the reader, so it is worded
// as the action and flagged `regenerate` so the UI can offer it.
export const STALE_FACTS_MESSAGE =
  'The document changed since this report was prepared — regenerate the report so it describes the current version.'

const REFUSED = {
  401: 'Sign in again to download this report.',
  403: 'Your account cannot download this report.',
  404: 'This report is not available to your account — the scan or file was not found.',
  409: STALE_FACTS_MESSAGE,
  413: 'This report is too large to render as a PDF. Try the Summary or Reviewer packet mode.',
  422: 'The report could not be rendered because its content was not accepted.',
}
// Missing facts are reported, never worked around: without a digest the server cannot tell whether
// the model describes the document as it is now, which is the whole point of the 409 above.
export const NO_FACTS_MESSAGE =
  'The report evidence could not be read from the server, so no report was rendered. Reload the document and try again.'

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

export const CANCELLED_MESSAGE = 'The report was cancelled before it finished rendering; nothing was produced.'
export const SIZE_LIMIT_HTML_MESSAGE =
  'The report exceeds the PDF size limit. An HTML copy containing the same evidence was downloaded instead.'
export const SCAN_TOO_LARGE_STEER =
  'For a scan this large, use "Per-file packets (ZIP)": each document gets its own PDF, with a master index of every document.'

const abortError = () => Object.assign(new Error('aborted'), { name: 'AbortError' })
const isAbort = (e) => e?.name === 'AbortError'
// Settle with `promise`, or reject the moment `signal` aborts — so a cancelled export stops
// waiting even when the transport underneath does not honour the signal itself.
function abortable(promise, signal) {
  if (!signal) return promise
  if (signal.aborted) return Promise.reject(abortError())
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(abortError())
    signal.addEventListener('abort', onAbort, { once: true })
    promise.then(
      (v) => { signal.removeEventListener('abort', onAbort); resolve(v) },
      (e) => { signal.removeEventListener('abort', onAbort); reject(e) },
    )
  })
}

// One server round-trip, classified. Nothing here downloads anything:
//   { outcome: 'pdf', blob }
//   { outcome: 'fallback', reason, status }     — no server / unreachable / 5xx / 413
//   { outcome: 'refused', status, message, regenerate? }
//   { outcome: 'aborted' }
async function renderOnServer({ scanId, kind, file, mode, model, factsDigest, signal }) {
  if (!scanId) return { outcome: 'fallback', reason: 'no scan id', status: null }
  // The digest travels on the MODEL (identity.factsDigest) so every builder — file, scan,
  // remediation — binds its render without each caller having to remember to thread it.
  const digest = factsDigest !== undefined && factsDigest !== null ? factsDigest
    : (typeof model?.identity?.factsDigest === 'string' ? model.identity.factsDigest : null)
  if (signal?.aborted) return { outcome: 'aborted' }
  let res
  try {
    res = await abortable(postReportRender(scanId, { kind, file: file ?? null, mode, factsDigest: digest, model: { ...model, mode, kind } }, signal ? { signal } : undefined), signal)
  } catch (e) {
    if (isAbort(e) || signal?.aborted) return { outcome: 'aborted' }
    return { outcome: 'fallback', reason: `request failed: ${e?.message || e}`, status: null }
  }
  if (!res) return { outcome: 'fallback', reason: 'demo mode has no report server', status: null }
  if (res.ok) {
    try { return { outcome: 'pdf', blob: await abortable(res.blob(), signal) } } catch (e) {
      if (isAbort(e) || signal?.aborted) return { outcome: 'aborted' }
      return { outcome: 'fallback', reason: `the PDF could not be read: ${e?.message || e}`, status: res.status }
    }
  }
  // Keep the complete model accessible when the PDF's resource limit is reached. Permission
  // refusals and stale-evidence refusals still never produce a fallback.
  if (res.status === 413) return { outcome: 'fallback', reason: 'PDF size limit exceeded', status: 413 }
  if (res.status >= 500) return { outcome: 'fallback', reason: `server answered ${res.status}`, status: res.status }
  if (res.status === 401 && res.headers?.get?.('X-Acp-Auth') === 'session') {
    window.dispatchEvent(new CustomEvent('acp:session-expired', { detail: { reason: SESSION_EXPIRED } }))
  }
  const detail = await detailOf(res)
  const base = REFUSED[res.status] || `The report could not be rendered (HTTP ${res.status}).`
  // 409 is NOT an HTML fallback. The model describes evidence the server has already said is out
  // of date; writing it into an HTML file instead would deliver exactly the stale document the
  // refusal exists to prevent, with nothing on its face to say so.
  const quiet = res.status === 404 || res.status === 409
  return {
    outcome: 'refused', status: res.status,
    ...(res.status === 409 ? { regenerate: true } : {}),
    message: detail && !quiet ? `${base} ${detail}` : base,
  }
}

const invalidRequest = ({ model, kind, mode }) => {
  if (!model || typeof model !== 'object') return 'No report content to render.'
  if (!REPORT_KINDS.includes(kind)) return `Unknown report kind: ${kind}`
  if (!REPORT_MODES[mode]) return `Unknown report mode: ${mode}`
  return null
}

export const reportHtmlFilename = ({ kind, mode, scanId, file }) =>
  reportPdfFilename({ kind, mode, scanId, file }).replace(/\.pdf$/, '.html')

/**
 * Contract 5 — render WITHOUT downloading. For callers that assemble several reports (the per-file
 * packet ZIP) and need the bytes, not a click.
 *
 * Resolves
 *   { ok: true,  format: 'pdf', blob, filename }
 *   { ok: false, status, message, regenerate?, fallback: 'html'|'none', aborted?,
 *     format?: 'html', htmlBlob?, htmlFilename? }
 *
 * When the server cannot produce the PDF (no server, unreachable, 5xx, 413) and `htmlFallback` is
 * on, the SAME model is returned as an accessible HTML blob, labelled `format: 'html'` — never
 * called a PDF. A refusal (401/403/404/409/422) and a cancellation produce no document at all.
 * `buildHtml(model)` may be supplied to control how that HTML is written (default:
 * htmlReport.reportHtmlFromModel).
 */
export async function renderReportBlob({ scanId, kind, file = null, mode = 'reviewer', model, factsDigest = undefined, signal = null, htmlFallback: wantHtml = true, buildHtml = null, filename = null } = {}) {
  const bad = invalidRequest({ model, kind, mode })
  if (bad) return { ok: false, status: null, fallback: 'none', message: bad }
  const got = await renderOnServer({ scanId, kind, file, mode, model, factsDigest, signal })
  if (got.outcome === 'pdf') {
    return { ok: true, format: 'pdf', blob: got.blob, filename: filename || reportPdfFilename({ kind, mode, scanId, file }) }
  }
  if (got.outcome === 'aborted') return { ok: false, status: null, aborted: true, fallback: 'none', message: CANCELLED_MESSAGE }
  if (got.outcome === 'refused') {
    const { outcome, ...rest } = got
    return { ok: false, fallback: 'none', ...rest }
  }
  // fallback
  const cause = got.status === 413 ? 'The report exceeds the PDF size limit' : 'The PDF service was not available'
  const why = `${cause}, so the report was produced as accessible HTML instead of PDF.`
  if (!wantHtml) return { ok: false, status: got.status, fallback: 'html', reason: got.reason, message: why }
  if (signal?.aborted) return { ok: false, status: null, aborted: true, fallback: 'none', message: CANCELLED_MESSAGE }
  try {
    const html = buildHtml ? await buildHtml(model) : (await import('./htmlReport.js')).reportHtmlFromModel(model)
    if (typeof html !== 'string' || !html) throw new Error('the HTML export produced nothing')
    return {
      ok: false, status: got.status, fallback: 'html', reason: got.reason, message: why,
      format: 'html', htmlBlob: new Blob([html], { type: 'text/html;charset=utf-8' }),
      htmlFilename: reportHtmlFilename({ kind, mode, scanId, file }),
    }
  } catch (e) {
    return { ok: false, status: got.status, fallback: 'none', reason: got.reason, message: `${cause}, and the HTML copy could not be built either: ${e?.message || e}` }
  }
}

// Render `model` on the server and download the PDF. Built on renderReportBlob.
// Resolves { ok: true, filename } | { ok: false, fallback: 'html' | 'none', message, status? }.
export async function renderReportPdf({ scanId, kind, file = null, mode = 'reviewer', model, filename = null, factsDigest = undefined, signal = null } = {}) {
  const res = await renderReportBlob({ scanId, kind, file, mode, model, factsDigest, signal, filename, htmlFallback: false })
  if (res.ok) {
    downloadBlob(res.blob, res.filename)
    return { ok: true, filename: res.filename }
  }
  if (res.fallback === 'html') {
    // The single-report path keeps the established download: the HTML export of the SAME model,
    // with the mova logo, and a message that says a PDF was NOT produced.
    const fallback = await htmlFallback(model, res.reason || 'PDF unavailable')
    if (fallback.fallback === 'html' && res.status === 413) {
      fallback.message = kind === 'scan' ? `${SIZE_LIMIT_HTML_MESSAGE} ${SCAN_TOO_LARGE_STEER}` : SIZE_LIMIT_HTML_MESSAGE
    }
    return fallback
  }
  const { reason, format, htmlBlob, htmlFilename, ...rest } = res
  return rest
}
