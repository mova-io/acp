// Live inputs for a single-document report (FileDrawer → reportModel.buildFileReportModel).
//
// Everything here is read from a record, and every gap is stated rather than filled:
//   - diffs: the FULL remediation_diff list for the file; a failed read is diffsComplete=false,
//     diffsTotal=null (not "0 changes").
//   - reviews/artifact: the versioned reviewer decisions (changeReview.js).
//   - previews: page renders for pages that findings or changes point at — bounded, with the
//     pages left out named in previewStatus (never "page one only"). Each entry says WHICH VERSION
//     it is (original / corrected / version-not-verified); nothing here is ever captioned "after".
//   - previous: the server's recorded baseline (facts.previous) — an earlier snapshot of the SAME
//     document identity and scope with per-finding records — or null with previousReason.
//   - assignee: the file's recorded assignment, or null.
// I/O goes through `deps` so the builder is testable without a server.
import { attachFileIssues, fileIssuesOf, fmtOfFile, scOfValue } from './reportEvidence.js'
import { loadChangeReviews, reviewsForReport } from './changeReview.js'

export const PREVIEW_MAX = 8

// ── Server report FACTS ──────────────────────────────────────────────────────────────────────
// api/report_facts.py owns identity, the finding list, the saved-change list (verified AND
// applied-but-unverified), the recorded decisions, and the accounting rules. The report is built
// from THOSE; `factsDigest` binds the rendered PDF to them, and POST /report-render answers 409
// when they have moved on. A facts read that fails is reported as a gap — never replaced by the
// client's own rollup, which is how a report comes to describe evidence nobody checked.
export const FACTS_UNAVAILABLE =
  'The server report evidence could not be read, so this report was built from the data already on screen. Identity, per-finding resolution and the saved-change list may be incomplete.'
export const FACTS_SIM =
  'Demo mode has no report server, so this report is built from the demo data on screen.'
// A wiring mistake is NOT a transport failure, and must not be reported as one. "The evidence
// could not be read" sends the reader to check their connection, the scan and their permissions;
// an undefined dependency is a bug in this app and nothing they do will change it. Found by the
// integrator: an api.js mock missing one export read on screen as "Saved changes could not be
// loaded", which is the diagnosis that stops anybody looking at the wiring.
export const FACTS_INTERNAL =
  'The report evidence could not be requested because of a problem in this application, not with your document or your connection. Please report this.'
// A missing or undefined dependency surfaces as a TypeError ("… is not a function"), or — under a
// module mock with an incomplete factory — as an error naming the missing export.
const isProgrammingError = (e) => e instanceof TypeError
  || /is not a function|No "[^"]+" export is defined/.test(String(e?.message || ''))

export async function loadFileReportFacts(scanId, fileName, { getFileReportFacts } = {}) {
  if (typeof getFileReportFacts !== 'function') return { facts: null, factsError: FACTS_INTERNAL, factsInternal: true }
  if (!scanId || !fileName) return { facts: null, factsError: 'No assessment is selected, so the server report evidence was not read.' }
  try {
    const facts = await getFileReportFacts(scanId, fileName)
    if (!facts || typeof facts !== 'object') return { facts: null, factsError: FACTS_SIM }
    return { facts, factsError: null }
  } catch (e) {
    if (isProgrammingError(e)) return { facts: null, factsError: `${FACTS_INTERNAL} (${e?.message || e})`, factsInternal: true }
    return { facts: null, factsError: `${FACTS_UNAVAILABLE} (${e?.message || e})` }
  }
}

// GET /scans/{sid}/report-facts is paginated over the per-file index. A big scan must come back
// with EVERY file or with an exact statement of what is missing and why — a silent cap on page one
// is how a 4,000-document estate reports the first 200 as if they were all of it.
export const FACTS_PAGE_LIMIT = 200
export const FACTS_MAX_PAGES = 40          // 8,000 files; beyond that we say so rather than loop

export async function loadScanReportFacts(scanId, { getScanReportFacts, limit = FACTS_PAGE_LIMIT, maxPages = FACTS_MAX_PAGES, onProgress = null } = {}) {
  const gap = (reason) => ({ facts: null, files: [], filesTotal: null, complete: false, pages: 0, factsError: reason, incompleteReason: reason })
  if (typeof getScanReportFacts !== 'function') return gap(FACTS_INTERNAL)
  if (!scanId) return gap('No assessment is selected, so the server report evidence was not read.')
  let first
  try { first = await getScanReportFacts(scanId, { offset: 0, limit }) } catch (e) {
    return gap(`${isProgrammingError(e) ? FACTS_INTERNAL : FACTS_UNAVAILABLE} (${e?.message || e})`)
  }
  if (!first || typeof first !== 'object') return gap(FACTS_SIM)

  const files = Array.isArray(first.files) ? [...first.files] : []
  const filesTotal = Number.isFinite(first.filesTotal) ? first.filesTotal : null
  let complete = first.complete === true || (filesTotal != null && files.length >= filesTotal)
  let pages = 1
  let incompleteReason = null
  onProgress?.({ loaded: files.length, total: filesTotal, complete })

  while (!complete && pages < maxPages) {
    let next
    try { next = await getScanReportFacts(scanId, { offset: files.length, limit }) } catch (e) {
      incompleteReason = `The per-document index stopped after ${files.length}${filesTotal != null ? ` of ${filesTotal}` : ''} documents because the next page could not be read (${e?.message || e}).`
      break
    }
    const batch = Array.isArray(next?.files) ? next.files : []
    if (!batch.length) {
      // No progress and not complete: stop rather than spin, and say which it was.
      if (next?.complete === true) complete = true
      else incompleteReason = `The per-document index stopped after ${files.length}${filesTotal != null ? ` of ${filesTotal}` : ''} documents because the server returned an empty page.`
      pages++
      break
    }
    files.push(...batch)
    pages++
    complete = next.complete === true || (filesTotal != null && files.length >= filesTotal)
    onProgress?.({ loaded: files.length, total: filesTotal, complete })
  }
  if (!complete && !incompleteReason && pages >= maxPages) {
    incompleteReason = `The per-document index stopped at ${files.length}${filesTotal != null ? ` of ${filesTotal}` : ''} documents after ${maxPages} pages. The remaining documents are not included in this report.`
  }
  onProgress?.({ loaded: files.length, total: filesTotal, complete })
  return { facts: { ...first, files }, files, filesTotal, complete, pages, factsError: null, incompleteReason }
}
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

// Pages referenced by findings and by changes, in first-seen order. Server facts are read too,
// because they carry findings and saved changes the on-screen record may not have.
export function referencedPages(file, diffs = [], facts = null) {
  const pages = []
  const add = (p) => { const n = posInt(p); if (n != null && !pages.includes(n)) pages.push(n) }
  ;(file?.issues || []).forEach((i) => add(i?.page ?? i?.location?.page))
  ;(diffs || []).forEach((d) => add(d?.page ?? d?.location?.page))
  ;(facts?.findings || []).forEach((f) => add(f?.location?.page))
  ;(facts?.savedChanges || []).forEach((c) => add(c?.location?.page ?? c?.page))
  return pages
}

// ── Preview PROVENANCE ───────────────────────────────────────────────────────────────────────
// /scans/{sid}/files/{file}/page/{page} prefers the ORIGINAL bytes but falls back to the
// remediated blob (api: _source_bytes_for_render). So an image from it is a picture of one of two
// documents and the caller cannot tell which — it can be shown, but it cannot be captioned
// "before" or "after", and captioning it "after the edit" is a claim the bytes do not support.
//
// The exact-bytes route renders ONLY bytes whose sha256 equals the one in the path, so a preview
// fetched with the source sha IS the original and one fetched with the corrected sha IS the
// corrected copy. Where a digest is not recorded we fall back to the ambiguous route and say so.
export const PREVIEW_UNVERIFIED_CAPTION = 'Document preview — version not verified'
export const PREVIEW_UNVERIFIED_NOTE =
  'This preview comes from the document-preview endpoint, which may serve either the original or the corrected copy, so the version shown is not verified. It is not evidence of what changed.'
export const PREVIEW_NONE = 'Visual preview not available'

const isDataImage = (url) => typeof url === 'string' && /^data:image\/(png|jpeg);base64,/.test(url)
const short = (sha) => (sha ? String(sha).slice(0, 12) : null)

const previewEntry = ({ src, provenance, sha256, page, shaConfirmed = null }) => ({
  src,
  provenance,                       // 'original' | 'corrected' | 'unverified'
  sha256: sha256 || null,
  // Did the SERVER name the digest it rendered (X-ACP-Artifact-Sha256), and did it match what we
  // asked for? true = the caption's sha is confirmed against the bytes shown; false = the header
  // was not readable, so the label rests on the route's own guarantee alone; null = not applicable.
  shaConfirmed,
  page,
  verified: provenance !== 'unverified',
  caption: provenance === 'original' ? `Original document, page ${page} (sha ${short(sha256)})`
    : provenance === 'corrected' ? `Corrected copy, page ${page} (sha ${short(sha256)})`
      : `${PREVIEW_UNVERIFIED_CAPTION} — page ${page}`,
  alt: provenance === 'original' ? `Page ${page} of the original document`
    : provenance === 'corrected' ? `Page ${page} of the corrected copy`
      : `Page ${page} of this document; which version is shown is not verified`,
  note: provenance === 'unverified' ? PREVIEW_UNVERIFIED_NOTE : null,
})

/**
 * Resolves { previews, previewPairs, status }.
 *   previews[page]      the preferred single preview for that page (an ENTRY OBJECT, never a bare
 *                       data URL — a bare URL carries no provenance and is what let a source
 *                       render be captioned "after the edit").
 *   previewPairs[page]  { original, corrected } where each is known, so a before/after can show
 *                       both and name which is which.
 */
export async function collectPreviews({ scanId, file, diffs, facts = null, getFilePage, getFileArtifactPage, max = PREVIEW_MAX }) {
  const pages = referencedPages(file, diffs, facts)
  const status = {
    referenced: pages, included: [], unavailable: [], notIncluded: [], limit: max, reason: null,
    original: [], corrected: [], unverified: [], provenanceNote: null,
  }
  const empty = { previews: {}, previewPairs: {}, status }
  if (!pages.length) { status.reason = 'No finding or change records a page number, so no page previews are included.'; return empty }
  if (fmtOfFile(file?.file) !== 'pdf' || !scanId) {
    status.unavailable = pages
    status.reason = 'Page previews are only rendered for PDF documents.'
    return empty
  }
  if (!getFilePage && !getFileArtifactPage) {
    status.unavailable = pages
    status.reason = 'No page-preview service was available, so no page previews are included.'
    return empty
  }
  const wanted = pages.slice(0, max)
  status.notIncluded = pages.slice(max)

  const id = facts?.identity || {}
  const sourceSha = typeof id.sourceSha256 === 'string' && id.sourceSha256 ? id.sourceSha256 : null
  const correctedSha = typeof id.correctedSha256 === 'string' && id.correctedSha256 ? id.correctedSha256 : null
  // Fetch page `page` of the exact bytes whose sha256 is `sha`, and CHECK the digest the server
  // says it rendered (X-ACP-Artifact-Sha256) against the one we asked for. A caption that names a
  // digest is a claim about the bytes on screen; a mismatch means the image is of something else,
  // and a labelled picture of the wrong document is worse than no picture.
  const mismatched = []
  const exact = async (sha, page) => {
    if (!sha || !getFileArtifactPage) return null
    try {
      const got = await getFileArtifactPage(scanId, file.file, sha, page)
      if (!got) return null
      const blob = got instanceof Blob ? got : got.blob
      const served = got instanceof Blob ? null : (got.sha256 ?? null)
      if (served && served !== sha) { mismatched.push({ page, asked: sha, served }); return null }
      const url = await blobToDataUrl(blob)
      return isDataImage(url) ? { url, shaConfirmed: served === sha } : null
    } catch { return null }
  }
  const ambiguous = async (page) => {
    if (!getFilePage) return null
    try {
      const url = await blobToDataUrl(await getFilePage(scanId, file.file, page))
      return isDataImage(url) ? url : null
    } catch { return null }
  }

  const previews = {}
  const previewPairs = {}
  await Promise.all(wanted.map(async (page) => {
    const [orig, corr] = await Promise.all([
      exact(sourceSha, page),
      correctedSha && correctedSha !== sourceSha ? exact(correctedSha, page) : Promise.resolve(null),
    ])
    const original = orig ? previewEntry({ src: orig.url, provenance: 'original', sha256: sourceSha, page, shaConfirmed: orig.shaConfirmed }) : null
    const corrected = corr ? previewEntry({ src: corr.url, provenance: 'corrected', sha256: correctedSha, page, shaConfirmed: corr.shaConfirmed }) : null
    if (original || corrected) {
      previewPairs[page] = { original, corrected }
      // Prefer the corrected copy as the single preview when one exists: it is the version a
      // reviewer is being asked to confirm. Both are kept in previewPairs.
      previews[page] = corrected || original
      status.included.push(page)
      if (original) status.original.push(page)
      if (corrected) status.corrected.push(page)
      return
    }
    const anyUrl = await ambiguous(page)
    if (anyUrl) {
      previews[page] = previewEntry({ src: anyUrl, provenance: 'unverified', sha256: null, page })
      previewPairs[page] = { original: null, corrected: null, unverified: previews[page] }
      status.included.push(page)
      status.unverified.push(page)
    } else {
      status.unavailable.push(page)
    }
  }))
  ;['included', 'unavailable', 'original', 'corrected', 'unverified'].forEach((k) => status[k].sort((a, b) => a - b))

  status.shaMismatch = mismatched
  status.shaConfirmed = Object.values(previews).every((p) => p.provenance === 'unverified' || p.shaConfirmed === true)

  const notes = []
  if (mismatched.length) {
    notes.push(`The preview service returned a different document version than the one requested for page${mismatched.length === 1 ? '' : 's'} ${mismatched.map((m) => m.page).join(', ')}, so those images are not shown at all — a picture labelled with a checksum it does not have is worse than no picture.`)
  }
  if (Object.values(previews).some((p) => p.provenance !== 'unverified' && p.shaConfirmed === false)) {
    notes.push('The preview service did not name the version it rendered, so these labels rest on the request alone and were not confirmed against the image returned.')
  }
  if (status.unverified.length) notes.push(`${status.unverified.length} preview${status.unverified.length === 1 ? '' : 's'} could not be tied to a recorded document version (page${status.unverified.length === 1 ? '' : 's'} ${status.unverified.join(', ')}); ${PREVIEW_UNVERIFIED_NOTE}`)
  // The exact-bytes route is addressed BY SHA-256. A source recorded under a different checksum
  // (Drive hands back md5, SharePoint a quickXorHash) cannot be asked for by digest at all, so no
  // preview of this document can be shown as "the original" — which is a fact about the record,
  // not about the document, and reads as neither if it is left unsaid.
  if (!sourceSha) {
    notes.push(id.sourceChecksum && id.sourceChecksumKind && id.sourceChecksumKind !== 'sha256'
      ? `The original document's checksum is recorded as ${id.sourceChecksumKind}, not sha-256, so its exact bytes cannot be requested and no preview is shown as the original.`
      : 'No sha-256 checksum is recorded for the original document, so no preview is shown as the original.')
  }
  if (!correctedSha) notes.push('No corrected-copy checksum is recorded for this document, so no preview is shown as the corrected version.')
  else if (!status.corrected.length && status.included.length) notes.push('The corrected copy could not be rendered for the referenced pages, so no preview is shown as the corrected version.')
  if (status.unavailable.length) notes.push(`No preview could be produced for page${status.unavailable.length === 1 ? '' : 's'} ${status.unavailable.join(', ')}.`)
  status.provenanceNote = notes.length ? notes.join(' ') : null

  const reasons = []
  if (status.notIncluded.length) {
    reasons.push(`Previews are included for the first ${max} referenced pages; ${status.notIncluded.length} more referenced page${status.notIncluded.length === 1 ? ' is' : 's are'} not included (pages ${status.notIncluded.join(', ')}).`)
  }
  if (status.provenanceNote) reasons.push(status.provenanceNote)
  status.reason = reasons.length ? reasons.join(' ') : null
  return { previews, previewPairs, status }
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

// A server saved-change record in the shape the report builders already read, with the fields the
// facts endpoint adds kept alongside. `id` is the SERVER's id and is used verbatim — the client's
// `${file}::${ruleId}::${seq}` cannot reproduce the `u{16hex}` id of an applied-but-unverified
// change (its seq is null), and a decision recorded against a made-up id is recorded against
// nothing.
export function savedChangeToDiff(c, fileName) {
  return {
    id: c.id,
    file: fileName,
    rule_id: c.ruleId ?? null,
    sc: c.sc ?? null,
    seq: c.seq ?? null,
    locator: c.locator ?? null,
    before: c.before ?? null,
    after: c.after ?? null,
    note: c.note ?? null,
    // `verified` is what the existing builders read; keep the server's own words next to it.
    verified: c.verification === 'verified' ? true : c.verification === 'not_verified' ? false : null,
    verification: c.verification ?? 'unknown',
    verificationDetail: c.verificationDetail ?? null,
    artifactSha256: c.artifactSha256 ?? null,
    valueClipped: c.valueClipped === true,
    changeDigest: c.changeDigest ?? null,
    findingIds: Array.isArray(c.findingIds) ? c.findingIds : null,
    source: c.source ?? null,
    location: c.location ?? null,
    page: c.location?.page ?? null,
  }
}

// The scope IDENTITY both sides of a comparison are measured against. The server's `scopeDigest`
// is preferred because it is one canonical value computed the same way for both snapshots;
// scanScope+rubricHash is the older pair. Neither recorded => null, which the comparison builder
// reads as "not comparable" and says so — it never assumes two snapshots used the same scope.
export function scopeIdentity(src, targetLevel) {
  if (!src || typeof src !== 'object') return null
  if (src.scopeDigest != null) return { scopeDigest: src.scopeDigest, targetLevel: src.targetLevel ?? targetLevel }
  if (src.scanScope != null && src.rubricHash) return { scanScope: src.scanScope, rubricHash: src.rubricHash, targetLevel: src.targetLevel ?? targetLevel }
  if (src.scope != null && typeof src.scope === 'object') return src.scope
  return null
}

// previous / previousReason / scope / currentFindings, entirely from the server's facts.
export function comparisonFromFacts(facts, targetLevel) {
  const scope = scopeIdentity(facts?.identity ?? facts, targetLevel)
  const currentFindings = Array.isArray(facts?.findings) ? facts.findings : null
  const prev = facts?.previous && typeof facts.previous === 'object' ? facts.previous : null
  if (!prev) {
    return {
      previous: null,
      previousReason: (typeof facts?.previousReason === 'string' && facts.previousReason.trim())
        || 'No comparable earlier assessment of this document is recorded, so no change since a previous assessment is reported.',
      scope, currentFindings,
    }
  }
  return {
    previous: { ...prev, file: prev.file ?? facts?.identity?.file ?? null, scope: scopeIdentity(prev, targetLevel) },
    previousReason: null,
    scope, currentFindings,
  }
}

// The ESTATE-level comparison input. Deliberately stricter than the per-document one.
//
// buildComparison matches findings BY ID. The server's finding ids are
// sha256("finding-report-v1"|scanId|file|ruleId|instanceKey)[:32]; the client's fallback ids are
// content hashes of a different shape entirely (reportEvidence.fileIssuesOf). Hand it a
// server-sourced baseline and a client-derived current list and NOTHING matches — every earlier
// finding reads "resolved" and every current one reads "introduced", which is a report saying the
// estate was fixed and re-broken in one week. So the baseline is only passed through when the
// CURRENT findings come from the same source; otherwise the comparison reads unknown, with the
// reason said out loud.
export function scanComparisonFromFacts(facts, targetLevel) {
  const scope = scopeIdentity(facts?.identity ?? facts, targetLevel)
  const prev = facts?.previous && typeof facts.previous === 'object' ? facts.previous : null
  const currentFindings = Array.isArray(facts?.findings) ? facts.findings : null
  const none = (reason) => ({ previous: null, previousReason: reason, scope, currentFindings: null })
  if (!facts) return none('The server report evidence could not be read, so no change since a previous assessment is reported.')
  if (!prev) {
    return none((typeof facts.previousReason === 'string' && facts.previousReason.trim())
      || 'No comparable earlier assessment of this estate is recorded, so no change since a previous assessment is reported.')
  }
  if (!Array.isArray(prev.findings) || !currentFindings) {
    return none('An earlier assessment is recorded, but the individual findings of both assessments are not available at estate level, so no finding-by-finding comparison is reported. Aggregate counts are never subtracted to imply one.')
  }
  return {
    previous: { ...prev, file: null, scope: scopeIdentity(prev, targetLevel) },
    previousReason: null,
    scope, currentFindings,
  }
}

// Why a saved-change list is not complete, in the terms a reviewer needs.
//
// `savedChangesUnverifiedSource: 'unavailable'` is the one that matters: the changes the AI
// APPLIED AND NOTHING RE-SCANNED could not be read. Those are the only ones that need a human, so
// a panel that renders the remaining (verified) records as if the list were whole shows a reviewer
// a complete-looking page of work that needs nothing from them. It has to say what is missing.
export const UNVERIFIED_UNAVAILABLE =
  'The changes awaiting review could not be read, so this list is incomplete. The changes ACP applied and has not re-checked are exactly the ones that need a person, and none of them are shown here. Do not treat this as a complete list.'

export function savedChangesGap(facts, shown, total) {
  if (facts?.savedChangesUnverifiedSource === 'unavailable') return UNVERIFIED_UNAVAILABLE
  return `The saved-change list is partial: ${shown}${total != null ? ` of ${total}` : ''} record(s) were returned (server limit ${facts?.savedChangesLimit ?? facts?.limits?.savedChangesLimit ?? 'not recorded'}).`
}

const artifactFromFacts = (facts) => {
  const id = facts?.identity
  if (!id) return null
  return {
    sourceSha256: id.sourceSha256 ?? null,
    sourceChecksum: id.sourceChecksum ?? null,
    sourceChecksumKind: id.sourceChecksumKind ?? null,
    correctedSha256: id.correctedSha256 ?? null,
    currentSha256: id.currentArtifact?.sha256 ?? null,
    identityKind: id.currentArtifact?.kind ?? null,
  }
}

/**
 * deps: { getConfig, getFileRemediationDiffs, getDecisions, getFilePage, getFileArtifactPage,
 *         getFileReportFacts, getScan, getScanDiff, loadReviews (optional override) }
 */
export async function buildFileReportData({ file, scanId, mode, rows, targetLevel, dispositions = {}, isDispositionable, normalizeDisposition, deps = {}, now = new Date() }) {
  const fileName = file.file
  const cfgP = deps.getConfig ? deps.getConfig().catch(() => null) : Promise.resolve(null)

  // Facts FIRST: the previews need the recorded digests to know which version they are showing,
  // and the render request needs factsDigest to be refused when the document has moved on.
  const { facts, factsError } = await loadFileReportFacts(scanId, fileName, deps)

  let diffs = []
  let diffsComplete = false
  let diffsTotal = null
  let diffsError = null
  if (facts && Array.isArray(facts.savedChanges)) {
    // Server facts include BOTH verified changes and applied-but-unverified ones. The unverified
    // ones are exactly the records a human has to look at, and the remediation_diff route does not
    // return them at all.
    diffs = facts.savedChanges.map((c) => savedChangeToDiff(c, fileName))
    diffsComplete = facts.savedChangesComplete === true
    diffsTotal = Number.isFinite(facts.savedChangesTotal) ? facts.savedChangesTotal : (diffsComplete ? diffs.length : null)
    if (!diffsComplete) diffsError = savedChangesGap(facts, diffs.length, diffsTotal)
  } else if (scanId && deps.getFileRemediationDiffs) {
    try {
      const got = await deps.getFileRemediationDiffs(scanId, fileName, { strict: true })
      diffs = Array.isArray(got) ? got : []
      diffsComplete = Array.isArray(got)
      diffsTotal = diffsComplete ? diffs.length : null
    } catch (e) { diffsError = e?.message || 'Saved changes could not be read.' }
  } else if (!scanId) {
    diffsError = 'No assessment is selected, so saved changes were not read.'
  }
  const [cfg, reviewsRes, decisions, localPrev, pv] = await Promise.all([
    cfgP,
    (deps.loadReviews || loadChangeReviews)(scanId, fileName),
    scanId && deps.getDecisions ? deps.getDecisions(scanId).catch(() => null) : Promise.resolve(null),
    // The server's facts carry the baseline; findPrevious is the fallback when they could not be
    // read, so a comparison is still attempted rather than silently dropped.
    facts ? Promise.resolve(null) : findPrevious({ scanId, fileName, targetLevel, getScan: deps.getScan, getScanDiff: deps.getScanDiff }),
    collectPreviews({ scanId, file, diffs, facts, getFilePage: deps.getFilePage, getFileArtifactPage: deps.getFileArtifactPage }),
  ])
  const prevRes = facts ? comparisonFromFacts(facts, targetLevel) : localPrev
  const verifiedScs = new Set(diffs.filter((d) => d.verified === true).map((d) => scOfValue(d.rule_id) || d.rule_id))
  const withIssues = attachFileIssues(rows, file, { locationHref: null }).map((r) => {
    const out = { ...r, verified: verifiedScs.has(r.id) }
    const d = isDispositionable && normalizeDisposition && isDispositionable(r.outcome) ? normalizeDisposition(dispositions[r.id]) : null
    if (d) out.disposition = d
    return out
  })
  // The server's own identity wins where it is recorded; the change-review route's artifact is the
  // fallback for the facts-unavailable path.
  const artifact = artifactFromFacts(facts) || reviewsRes?.artifact || { sourceSha256: null, correctedSha256: null, currentSha256: null }
  // Facts carry the decisions the server re-evaluated (stale/staleReason bound to the CURRENT
  // artifact and change digest). Those are preferred over the client's own staleness reasoning.
  const factsReviews = facts && facts.reviews && typeof facts.reviews === 'object' ? facts.reviews : null
  const assigneeRaw = decisions && decisions[fileName] ? decisions[fileName].assignee : null
  return {
    mode,
    file: fileName, score: file.score, targetLevel, rows: withIssues,
    diffs, diffsComplete, diffsTotal, diffsError, diffValueCap: facts?.limits?.valueMaxChars ?? DIFF_VALUE_STORE_CAP,
    // The whole server facts object, for the builders that read it directly.
    facts, factsError, factsDigest: facts?.factsDigest ?? null,
    savedChanges: facts?.savedChanges ?? null,
    accounting: facts?.accounting ?? null,
    assessment: facts?.assessment ?? null,
    reviews: factsReviews || (reviewsResOk(reviewsRes) ? reviewsForReport(reviewsRes.reviews, artifact) : null),
    reviewsError: factsReviews ? null : (reviewsRes?.ok ? null : (reviewsRes?.error || null)),
    reviewsSim: !!reviewsRes?.sim,
    artifact,
    identity: {
      scanId: scanId || null, file: fileName,
      sourceSha256: artifact.sourceSha256 ?? null,
      sourceChecksum: artifact.sourceChecksum ?? null,
      sourceChecksumKind: artifact.sourceChecksumKind ?? null,
      correctedSha256: artifact.correctedSha256 ?? file.corrected_sha256 ?? null,
      currentArtifact: facts?.identity?.currentArtifact
        ?? { kind: artifact.identityKind ?? 'unknown', sha256: artifact.currentSha256 ?? null },
      artifactVersion: artifact.currentSha256 ?? null,
      // Binds this report to the evidence it was built from. The render route recomputes it and
      // answers 409 when the document has changed since — see reportRenderClient.STALE_FACTS_MESSAGE.
      factsDigest: facts?.factsDigest ?? null,
      remediatedAt: facts?.identity?.remediatedAt ?? null,
      generatedAt: now.toISOString(),
      platformVersion: facts?.identity?.platformVersion ?? cfg?.version ?? null,
      targetLevel,
    },
    previews: pv.previews, previewPairs: pv.previewPairs, previewStatus: pv.status,
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
