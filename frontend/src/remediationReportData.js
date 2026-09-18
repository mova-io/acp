// Gathers the remediation report's evidence from the SERVER's report facts: every saved change —
// verified AND applied-but-unverified — the reviewer decisions recorded against each, and the
// scan-level `factsDigest` that binds the rendered report to the evidence it was built from.
//
// Why it is not the remediation-diff list any more. `remediation_diff` rows are only written for
// fixes that CLEARED the post-fix re-scan, so a report built from them shows a reviewer everything
// except the changes that still need a human — the ones the AI wrote into the corrected copy and
// nothing has re-checked. Those are now included and are marked "AI applied · not verified".
//
// Three failures are kept distinct from "nothing there", because the report states them as facts:
//   * a change list that could not be read is UNKNOWN (diffsComplete false, diffsTotal null),
//     never an empty list;
//   * a per-document index that stopped short is reported with what is missing and why, never
//     silently capped;
//   * if a document's decisions could not be read, decisions are reported as "not loaded" for the
//     whole report (reviewsByFile null) rather than as "no decision recorded".
import { getScanRemediationDiffs, getScanReportFacts, getFileReportFacts } from './api.js'
import { normaliseDiffSummary } from './scanReport.js'
import { loadChangeReviews, reviewsForReport } from './changeReview.js'
import { loadScanReportFacts, savedChangeToDiff } from './fileReportData.js'

const REVIEW_BATCH = 6
// Per-document facts are one request each. Beyond this the report says which documents it covers
// and which it does not, rather than issuing thousands of requests or quietly stopping.
export const FILE_FACTS_MAX = 250

const batched = async (items, size, fn) => {
  const out = []
  for (let i = 0; i < items.length; i += size) {
    // eslint-disable-next-line no-await-in-loop
    out.push(...await Promise.all(items.slice(i, i + size).map(fn)))
  }
  return out
}

// The documents the scan index says hold saved changes, worst-first is not meaningful here so the
// server's own order is kept.
// A count of `null` means NOT RECORDED, and a document whose change count is unknown is exactly
// the one that must be opened rather than assumed empty — so unknown is included, not filtered out.
const filesWithChanges = (index) => (index || [])
  .filter((f) => f?.savedChangesVerified == null || f?.savedChangesUnverified == null
    || (f.savedChangesVerified + f.savedChangesUnverified) > 0)
  .map((f) => f?.file)
  .filter((f) => typeof f === 'string' && f)

export async function gatherRemediationEvidence(scanId, { onProgress = null } = {}) {
  const scan = await loadScanReportFacts(scanId, { getScanReportFacts, onProgress })
  if (scan.facts) return fromFacts(scanId, scan)
  return fromDiffs(scanId, scan.factsError)
}

async function fromFacts(scanId, scan) {
  const wanted = filesWithChanges(scan.files)
  const covered = wanted.slice(0, FILE_FACTS_MAX)
  const omitted = wanted.slice(FILE_FACTS_MAX)

  const results = await batched(covered, REVIEW_BATCH, async (file) => {
    try { return { file, facts: await getFileReportFacts(scanId, file), error: null } } catch (e) {
      return { file, facts: null, error: e?.message || 'the document evidence could not be read' }
    }
  })

  const diffsByFile = {}
  const reviewsByFile = {}
  const currentShaByFile = {}
  const failed = []
  const unreadableUnverified = []
  let total = 0
  let verified = 0
  let unverified = 0
  let allComplete = true

  results.forEach(({ file, facts, error }) => {
    if (!facts || !Array.isArray(facts.savedChanges)) { failed.push(file); if (error) allComplete = false; return }
    const rows = facts.savedChanges.map((c) => savedChangeToDiff(c, file))
    diffsByFile[file] = rows
    total += rows.length
    rows.forEach((r) => { if (r.verification === 'verified') verified++; else unverified++ })
    if (facts.savedChangesComplete === false) {
      allComplete = false
      if (facts.savedChangesUnverifiedSource === 'unavailable') unreadableUnverified.push(file)
    }
    reviewsByFile[file] = facts.reviews && typeof facts.reviews === 'object' ? facts.reviews : {}
    currentShaByFile[file] = facts.identity?.currentArtifact?.sha256 ?? null
  })

  // S3: a document beyond FILE_FACTS_MAX, or one whose facts could not be read, is NOT itemised —
  // but it is never stranded. Every one is returned (with the server index's own counts for it,
  // null where the index does not record them) and listed in the Full evidence appendix; the notes
  // name the first few and say where the rest are.
  const indexByFile = new Map((scan.files || []).map((f) => [f?.file, f]))
  const countsOf = (file) => {
    const row = indexByFile.get(file) || {}
    return {
      savedChangesVerified: Number.isFinite(row.savedChangesVerified) ? row.savedChangesVerified : null,
      savedChangesUnverified: Number.isFinite(row.savedChangesUnverified) ? row.savedChangesUnverified : null,
      decisionsPending: Number.isFinite(row.humanReviews?.pending) ? row.humanReviews.pending : null,
    }
  }
  const errors = new Map(results.filter((r) => !r.facts).map((r) => [r.file, r.error]))
  const unitemisedDocuments = [
    ...omitted.map((file) => ({ file, reason: `beyond the ${FILE_FACTS_MAX}-document itemisation limit of this report`, ...countsOf(file) })),
    ...failed.map((file) => ({ file, reason: `its evidence could not be read${errors.get(file) ? ` (${errors.get(file)})` : ''}`, ...countsOf(file) })),
  ]
  const named = (list) => `${list.slice(0, 10).join(', ')}${list.length > 10 ? `, and ${list.length - 10} more` : ''}`
  const notes = []
  if (omitted.length) notes.push(`${omitted.length} document(s) with saved changes are not itemised in this report (it itemises the first ${FILE_FACTS_MAX}): ${named(omitted)}. Every one is listed, with the server's counts for it, in the Full evidence appendix "Documents not itemised".`)
  if (failed.length) notes.push(`The saved changes of ${failed.length} document(s) could not be read: ${named(failed)}. Every one is listed in the Full evidence appendix "Documents not itemised".`)
  if (unreadableUnverified.length) notes.push(`For ${unreadableUnverified.length} document(s) the changes AWAITING REVIEW could not be read, so none of them are listed: ${named(unreadableUnverified)}. Those are the changes that need a person, so this report is not a complete account of the work outstanding.`)
  if (scan.incompleteReason) notes.push(scan.incompleteReason)

  const complete = allComplete && !omitted.length && !failed.length && scan.complete === true
  // The server totals its own index over EVERY document, whatever this report itemised; those are
  // the counts to state. Without them a partial gather's count is unknown — never the part read.
  const totals = scan.facts?.totals || {}
  const serverVerified = Number.isFinite(totals.savedChangesVerified) ? totals.savedChangesVerified : null
  const serverUnverified = Number.isFinite(totals.savedChangesUnverified) ? totals.savedChangesUnverified : null
  return {
    diffsByFile,
    diffsComplete: complete,
    // A partial gather reports the count it actually holds as unknown rather than as the total.
    diffsTotal: complete ? total : null,
    savedChangesVerified: serverVerified ?? (complete ? verified : null),
    savedChangesUnverified: unreadableUnverified.length ? null : (serverUnverified ?? (complete ? unverified : null)),
    itemisedChanges: { total, verified, unverified, documents: results.length - failed.length },
    unitemisedDocuments: unitemisedDocuments.length ? unitemisedDocuments : null,
    unreadableUnverified: unreadableUnverified.length ? unreadableUnverified : null,
    // The decisions are only trustworthy as a whole when every document answered.
    reviewsByFile: failed.length ? null : reviewsByFile,
    currentShaByFile: failed.length ? null : currentShaByFile,
    facts: scan.facts,
    factsDigest: scan.facts?.factsDigest ?? null,
    filesIndexComplete: scan.complete,
    evidenceNotes: notes.length ? notes : null,
    source: 'report-facts',
  }
}

// Fallback for demo mode and for a facts endpoint that could not be reached. Verified changes
// only — which is exactly what the note says, so the gap is on the page rather than implied.
async function fromDiffs(scanId, factsError) {
  const raw = await getScanRemediationDiffs(scanId, true).catch(() => null)
  const page = normaliseDiffSummary(raw)
  const diffsByFile = {}
  ;(page ? page.items : []).forEach((d) => { (diffsByFile[d.file] = diffsByFile[d.file] || []).push(d) })

  const files = Object.keys(diffsByFile)
  const reviewsByFile = {}
  const currentShaByFile = {}
  let reviewsOk = true
  const loaded = await batched(files, REVIEW_BATCH, (f) => loadChangeReviews(scanId, f))
  loaded.forEach((r, k) => {
    if (!r.ok) { reviewsOk = false; return }
    reviewsByFile[files[k]] = reviewsForReport(r.reviews, r.artifact)
    currentShaByFile[files[k]] = r.artifact?.currentSha256 ?? null
  })
  return {
    diffsByFile,
    // Completeness OF THE VERIFIED LIST — which is all this path holds. The note below says that
    // unverified changes are missing entirely, so the two are not confused.
    diffsComplete: page ? page.complete : false,
    diffsTotal: page ? page.total : null,
    savedChangesVerified: page ? page.total : null,
    savedChangesUnverified: null,
    reviewsByFile: reviewsOk ? reviewsByFile : null,
    currentShaByFile: reviewsOk ? currentShaByFile : null,
    facts: null,
    factsDigest: null,
    filesIndexComplete: false,
    evidenceNotes: [
      factsError || 'The server report evidence could not be read.',
      'This report lists only changes a re-scan verified. Changes the AI applied but nothing re-checked are not included, and their number is not known.',
    ],
    source: 'remediation-diffs',
  }
}
