// Gathers the remediation report's evidence: every saved change (with the server's own total, so
// a truncated list says it is partial) and the reviewer decisions recorded against each document.
//
// Two failures are kept distinct from "nothing there", because the report states both as facts:
//   * a change list that could not be read is UNKNOWN (diffsComplete false, diffsTotal null),
//     never an empty list;
//   * if any document's decisions could not be read, decisions are reported as "not loaded" for
//     the whole report (reviewsByFile null) rather than as "no decision recorded".
import { getScanRemediationDiffs } from './api.js'
import { normaliseDiffSummary } from './scanReport.js'
import { loadChangeReviews, reviewsForReport } from './changeReview.js'

const REVIEW_BATCH = 6

export async function gatherRemediationEvidence(scanId) {
  // With include_summary the server answers {items, total, complete}; a bare array is the client's
  // best-effort error value.
  const raw = await getScanRemediationDiffs(scanId, true).catch(() => null)
  const page = normaliseDiffSummary(raw)
  const diffsByFile = {}
  ;(page ? page.items : []).forEach((d) => { (diffsByFile[d.file] = diffsByFile[d.file] || []).push(d) })

  const files = Object.keys(diffsByFile)
  const reviewsByFile = {}, currentShaByFile = {}
  let reviewsOk = true
  for (let i = 0; i < files.length; i += REVIEW_BATCH) {
    const batch = files.slice(i, i + REVIEW_BATCH)
    const loaded = await Promise.all(batch.map((f) => loadChangeReviews(scanId, f)))
    loaded.forEach((r, k) => {
      if (!r.ok) { reviewsOk = false; return }
      reviewsByFile[batch[k]] = reviewsForReport(r.reviews, r.artifact)
      currentShaByFile[batch[k]] = r.artifact?.currentSha256 ?? null
    })
  }
  return {
    diffsByFile,
    diffsComplete: page ? page.complete : false,
    diffsTotal: page ? page.total : null,
    reviewsByFile: reviewsOk ? reviewsByFile : null,
    currentShaByFile: reviewsOk ? currentShaByFile : null,
  }
}
