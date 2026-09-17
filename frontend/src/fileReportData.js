// Live inputs for a single-document report (FileDrawer → reportModel.buildFileReportModel).
//
// Everything here is read from a record, and every gap is stated rather than filled:
//   - diffs: the FULL remediation_diff list for the file; a failed read is diffsComplete=false,
//     diffsTotal=null (not "0 changes").
//   - reviews/artifact: the versioned reviewer decisions (changeReview.js).
//   - previews: page renders for pages that findings or changes point at — bounded, with the
//     pages left out named in previewStatus (never "page one only").
//   - previous: an earlier snapshot of the SAME document (same name AND same source file id) with
//     per-finding records, or null with previousReason.
//   - assignee: the file's recorded assignment, or null.
// I/O goes through `deps` so the builder is testable without a server.
import { attachFileIssues, fileIssuesOf, fmtOfFile, scOfValue } from './reportEvidence.js'
import { loadChangeReviews, reviewsForReport } from './changeReview.js'

export const PREVIEW_MAX = 8
// remediation_diff stores before/after clipped to this many characters (store.record_remediation_diffs).
export const DIFF_VALUE_STORE_CAP = 2000

const blobToDataUrl = (blob) => new Promise((resolve) => {
  if (!blob || typeof FileReader === 'undefined') { resolve(null); return }
  const r = new FileReader()
  r.onload = () => resolve(typeof r.result === 'string' ? r.result : null)
  r.onerror = () => resolve(null)
  r.readAsDataURL(blob)
})

const posInt = (v) => { const n = Number(v); return Number.isInteger(n) && n > 0 ? n : null }

// Pages referenced by findings and by changes, in first-seen order.
export function referencedPages(file, diffs = []) {
  const pages = []
  const add = (p) => { const n = posInt(p); if (n != null && !pages.includes(n)) pages.push(n) }
  ;(file?.issues || []).forEach((i) => add(i?.page ?? i?.location?.page))
  ;(diffs || []).forEach((d) => add(d?.page ?? d?.location?.page))
  return pages
}

export async function collectPreviews({ scanId, file, diffs, getFilePage, max = PREVIEW_MAX }) {
  const pages = referencedPages(file, diffs)
  const status = { referenced: pages, included: [], unavailable: [], notIncluded: [], limit: max, reason: null }
  if (!pages.length) { status.reason = 'No finding or change records a page number, so no page previews are included.'; return { previews: {}, status } }
  if (fmtOfFile(file?.file) !== 'pdf' || !scanId || !getFilePage) {
    status.unavailable = pages
    status.reason = 'Page previews are only rendered for PDF documents.'
    return { previews: {}, status }
  }
  const wanted = pages.slice(0, max)
  status.notIncluded = pages.slice(max)
  const previews = {}
  const results = await Promise.all(wanted.map(async (p) => {
    try { return [p, await blobToDataUrl(await getFilePage(scanId, file.file, p))] } catch { return [p, null] }
  }))
  results.forEach(([p, url]) => {
    if (url && /^data:image\/(png|jpeg);base64,/.test(url)) { previews[p] = url; status.included.push(p) } else status.unavailable.push(p)
  })
  if (status.notIncluded.length) {
    status.reason = `Previews are included for the first ${max} referenced pages; ${status.notIncluded.length} more referenced page${status.notIncluded.length === 1 ? ' is' : 's are'} not included (pages ${status.notIncluded.join(', ')}).`
  }
  return { previews, status }
}

const scopeOf = (run, targetLevel) => (run && run.scan_scope != null && run.rubric_hash
  ? { scanScope: run.scan_scope, rubricHash: run.rubric_hash, targetLevel }
  : null)

// The comparison input. Only an earlier snapshot of the same document identity qualifies.
export async function findPrevious({ scanId, fileName, targetLevel, getScan, getScanDiff }) {
  const none = (reason, extra = {}) => ({ previous: null, previousReason: reason, scope: null, currentFindings: null, ...extra })
  if (!scanId || !getScan) return none('No assessment is selected, so no comparison was made.')
  let cur
  try { cur = await getScan(scanId) } catch { return none('The current assessment record could not be read, so no comparison was made.') }
  const curRun = cur?.run || null
  const curRec = (cur?.files || []).find((f) => f.file === fileName) || null
  const scope = scopeOf(curRun, targetLevel)
  const currentFindings = curRec ? fileIssuesOf(curRec) : null
  const base = { scope, currentFindings }
  let diff
  try { diff = getScanDiff ? await getScanDiff(scanId) : null } catch { diff = null }
  const prevId = diff?.prev_id || null
  if (!prevId) return { ...none('No earlier assessment is recorded to compare against.'), ...base }
  let prev
  try { prev = await getScan(prevId) } catch { return { ...none('The earlier assessment could not be read, so no comparison was made.'), ...base } }
  const prevRec = (prev?.files || []).find((f) => f.file === fileName) || null
  if (!prevRec) return { ...none('The earlier assessment did not include this document.'), ...base }
  if (!curRec || !curRec.drive_file_id || !prevRec.drive_file_id || curRec.drive_file_id !== prevRec.drive_file_id) {
    return { ...none('The earlier assessment has a document with this name, but it cannot be shown to be the same source file, so no comparison was made.'), ...base }
  }
  return {
    previous: {
      scanId: prevId,
      generatedAt: prev?.run?.completed_at || prev?.run?.assessed_at || prev?.run?.started_at || null,
      sha256: prevRec.corrected_sha256 || null,
      file: fileName,
      scope: scopeOf(prev?.run, targetLevel),
      findings: fileIssuesOf(prevRec),
    },
    previousReason: null,
    ...base,
  }
}

/**
 * deps: { getConfig, getFileRemediationDiffs, getDecisions, getFilePage, getScan, getScanDiff,
 *         loadReviews (optional override) }
 */
export async function buildFileReportData({ file, scanId, mode, rows, targetLevel, dispositions = {}, isDispositionable, normalizeDisposition, deps = {}, now = new Date() }) {
  const fileName = file.file
  const cfgP = deps.getConfig ? deps.getConfig().catch(() => null) : Promise.resolve(null)
  let diffs = []
  let diffsComplete = false
  let diffsTotal = null
  let diffsError = null
  if (scanId && deps.getFileRemediationDiffs) {
    try {
      const got = await deps.getFileRemediationDiffs(scanId, fileName, { strict: true })
      diffs = Array.isArray(got) ? got : []
      diffsComplete = Array.isArray(got)
      diffsTotal = diffsComplete ? diffs.length : null
    } catch (e) { diffsError = e?.message || 'Saved changes could not be read.' }
  } else if (!scanId) {
    diffsError = 'No assessment is selected, so saved changes were not read.'
  }
  const [cfg, reviewsRes, decisions, prevRes, pv] = await Promise.all([
    cfgP,
    (deps.loadReviews || loadChangeReviews)(scanId, fileName),
    scanId && deps.getDecisions ? deps.getDecisions(scanId).catch(() => null) : Promise.resolve(null),
    findPrevious({ scanId, fileName, targetLevel, getScan: deps.getScan, getScanDiff: deps.getScanDiff }),
    collectPreviews({ scanId, file, diffs, getFilePage: deps.getFilePage }),
  ])
  const verifiedScs = new Set(diffs.filter((d) => d.verified === true).map((d) => scOfValue(d.rule_id) || d.rule_id))
  const withIssues = attachFileIssues(rows, file, { locationHref: null }).map((r) => {
    const out = { ...r, verified: verifiedScs.has(r.id) }
    const d = isDispositionable && normalizeDisposition && isDispositionable(r.outcome) ? normalizeDisposition(dispositions[r.id]) : null
    if (d) out.disposition = d
    return out
  })
  const artifact = reviewsRes?.artifact || { sourceSha256: null, correctedSha256: null, currentSha256: null }
  const assigneeRaw = decisions && decisions[fileName] ? decisions[fileName].assignee : null
  return {
    mode,
    file: fileName, score: file.score, targetLevel, rows: withIssues,
    diffs, diffsComplete, diffsTotal, diffsError, diffValueCap: DIFF_VALUE_STORE_CAP,
    reviews: reviewsResOk(reviewsRes) ? reviewsForReport(reviewsRes.reviews, artifact) : null,
    reviewsError: reviewsRes?.ok ? null : (reviewsRes?.error || null),
    reviewsSim: !!reviewsRes?.sim,
    artifact,
    identity: {
      scanId: scanId || null, file: fileName,
      sourceSha256: artifact.sourceSha256 ?? null,
      correctedSha256: artifact.correctedSha256 ?? file.corrected_sha256 ?? null,
      artifactVersion: artifact.currentSha256 ?? null,
      generatedAt: now.toISOString(),
      platformVersion: cfg?.version ?? null,
      targetLevel,
    },
    previews: pv.previews, previewStatus: pv.status,
    previous: prevRes.previous, previousReason: prevRes.previousReason,
    scope: prevRes.scope, currentFindings: prevRes.currentFindings,
    assignee: typeof assigneeRaw === 'string' && assigneeRaw.trim() ? assigneeRaw.trim() : null,
    // The app has no URL route to a document or page, so no location link is offered.
    locationHref: null,
    date: now.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' }),
    timestamp: now.toLocaleString('en-US', { year: 'numeric', month: 'long', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' }),
    engine: file.engine, sourceName: file.sourceName, department: file.department || file.dept,
    platformVersion: cfg?.version,
    scanId,
  }
}

const reviewsResOk = (r) => !!(r && r.ok)
