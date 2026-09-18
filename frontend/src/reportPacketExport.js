// Per-file report packets for a whole scan, as ZIP part(s) with a complete master index.
//
// WHY. One Full evidence PDF for a large scan is refused (413: 20,000 blocks / 16 MB) and arrives
// as HTML, and the scan report's own index was built from the on-screen list. A reviewer of a
// 4,000-document estate needs ONE document's evidence at a time, plus a list that accounts for
// every document in the scan — including the ones that could not be exported, and why.
//
// HOW, per document, in bounded parallel (PACKET_CONCURRENCY):
//   1. the server's scan index (fileReportData.loadScanReportFacts — read to the end, digest-
//      coherent across pages, or stated incomplete with the exact reason);
//   2. a FRESH per-file facts read, and the file report model built exactly as the document drawer
//      builds it (buildFileReportData + computeCoverageRows + buildFileReportModel);
//   3. the fresh per-file factsDigest compared with the index row's (contract 3a: equal for
//      unchanged evidence). A mismatch means the evidence moved during the export: that document
//      is marked "changed during export" and NOT packed — an index row from one moment beside a
//      packet from another is exactly the mixed record this must never produce;
//   4. the server renders it (renderReportBlob), re-verifying that digest itself (409 → changed);
//      when the PDF service is unavailable the SAME model is packed as HTML and labelled HTML.
// Cancel (AbortSignal) stops at once; a cancelled export says exactly which documents are in it
// and which are not, and never calls itself complete.
//
// MEMORY. Packets go into the current ZIP part; a part is generated, downloaded and released when
// it reaches PART_MAX_FILES / PART_MAX_BYTES (reportPacketArchive.js explains the numbers), so the
// tab never holds the whole estate at once. The master index (index.html + index.csv) goes into
// the LAST part and is also returned for download on its own.
import {
  assignArchiveStems, packetPath, indexCsv, indexHtml, completeness, summaryCounts, finalCheckLabel,
  PART_MAX_BYTES, PART_MAX_FILES,
} from './reportPacketArchive.js'

export const PACKET_CONCURRENCY = 3
export const PACKET_FORMAT_LABEL = 'Per-file packets (ZIP)'

const MODE_LABEL = { summary: 'Summary', reviewer: 'Reviewer packet', full: 'Full evidence' }
const slug = (s) => String(s || '').replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^[-.]+|[-.]+$/g, '').slice(0, 60) || 'scan'

export const CHANGED_REASON = 'the evidence for this document changed while the export was running'
export const UNANALYSABLE_REASON = 'the document could not be analysed, so it has no report (the document drawer offers none either)'
export const NO_RECORD_REASON = "the scan's record for this document could not be read"

const errText = (e) => (e && (e.message || String(e))) || 'unexpected error'

/**
 * Wire the document drawer's report inputs for ONE export: the scan-wide reads (rule catalogue,
 * target level, remediation capability, config, decisions) happen once; the per-document ones
 * (remediation state, dispositions, facts, reviews, previews) per document — the same calls, with
 * the same arguments, that FileDrawer.jsx makes before it calls buildFileReportData.
 *
 * `api` supplies the transport functions (api.js in the app, recorded responses in tests).
 * `computeCoverageRows` is FileDrawer's own export, passed in so this module does not re-derive it.
 */
export function makeFileDataBuilder({ scanId, api, computeCoverageRows, buildFileReportData, isDispositionable, normalizeDisposition, scOf, inScope, capabilityFallback = null, aiEnabled = true }) {
  const once = (fn) => { let p = null; return () => (p ||= Promise.resolve().then(fn)) }
  const rulesP = once(() => (api.getRules ? api.getRules().catch(() => null) : null))
  const targetP = once(async () => {
    try {
      const r = api.getRubric ? await api.getRubric() : null
      const m = (r?.target || '').match(/\b(AAA|AA|A)\s*$/)
      return m ? m[1] : 'AA'
    } catch { return 'AA' }
  })
  const capP = once(async () => {
    try { const r = api.getCapability ? await api.getCapability() : null; return r?.capability || capabilityFallback } catch { return capabilityFallback }
  })
  const cfgP = once(() => (api.getConfig ? api.getConfig() : null))
  const decisionsP = once(() => (api.getDecisions ? api.getDecisions(scanId) : null))
  return async ({ file, mode }) => {
    const [catalogRules, targetLevel, cap, remRows, dispRows] = await Promise.all([
      rulesP(), targetP(), capP(),
      api.getFileRemediationState ? api.getFileRemediationState(scanId, file.file).catch(() => null) : null,
      api.listDispositions ? api.listDispositions(scanId, file.file).catch(() => null) : null,
    ])
    const remediatedRuleIds = new Set((remRows || []).filter((r) => r.state === 'complete').map((r) => r.rule_id))
    const dispositions = {}
    ;(Array.isArray(dispRows) ? dispRows : []).forEach((d) => { const n = normalizeDisposition(d); if (n && d.sc) dispositions[d.sc] = n })
    // FileDrawer: isRemediated || every in-scope failing criterion has a completed remediation row.
    const issues = (file.issues || []).filter((i) => inScope(i))
    const allFailingFixed = issues.length > 0 && issues.every((i) => remediatedRuleIds.has(scOf(i.wcag)))
    const effectiveRemediated = !!(file.acp_stamped || file.remediated_at || file.drive_write_url) || allFailingFixed
    const rows = computeCoverageRows(file, { catalogRules, targetLevel, remediatedRuleIds, effectiveRemediated, aiEnabled, cap })
    return buildFileReportData({
      file, scanId, mode, targetLevel, dispositions, isDispositionable, normalizeDisposition, rows,
      deps: {
        getConfig: () => cfgP(), getDecisions: () => decisionsP(),
        getFileRemediationDiffs: api.getFileRemediationDiffs, getFileReportFacts: api.getFileReportFacts,
        getFileArtifactPage: api.getFileArtifactPage, getFilePage: api.getFilePage,
        getScan: api.getScan, getScanDiff: api.getScanDiff,
        ...(api.loadReviews ? { loadReviews: api.loadReviews } : {}),
      },
    })
  }
}

// ── The final fresh check (contract 7) ────────────────────────────────────────────────────────
// Every packet is checked against a fresh per-file read when it is made, and a PDF again by the
// render route — but those checks happen at different moments, one document at a time. Evidence
// that moves AFTER a document is packed (a reviewer decision, a new corrected copy, a file added to
// or removed from the scan) is seen by none of them. So, once every worker has finished and before
// the master index is written, the scan index is read ONE more time: offset 0, limit 1, and no
// `digest` — the request the server always rebuilds from the store — and its full-index factsDigest
// is compared with the snapshot's. Once. No polling, no retry: a bounded wait, then a stated result.
export const FINAL_CHECK_TIMEOUT_MS = 20000

const STOPPED = Symbol('stopped')

/**
 * Resolves (never rejects) {
 *   status: 'verified' | 'changed' | 'failed' | 'not_performed',
 *   reason,                     // null when verified
 *   snapshotDigest, digest,     // digest = what the final read returned (null when none)
 *   snapshotFilesTotal, filesTotal,
 *   startedAt, checkedAt,       // ISO; checkedAt null when the read was never made
 *   timeoutMs,
 * }
 */
export async function finalDigestCheck({
  scanId, snapshotDigest, snapshotFilesTotal = null, getScanReportFacts, signal = null,
  timeoutMs = FINAL_CHECK_TIMEOUT_MS, now = () => new Date(),
}) {
  const base = {
    snapshotDigest: snapshotDigest ?? null, digest: null,
    snapshotFilesTotal: Number.isFinite(snapshotFilesTotal) ? snapshotFilesTotal : null, filesTotal: null,
    startedAt: now().toISOString(), checkedAt: null, timeoutMs,
  }
  const notPerformed = (reason) => ({ ...base, status: 'not_performed', reason })
  if (signal?.aborted) return notPerformed('the export was cancelled, so the final check was not run')
  if (typeof getScanReportFacts !== 'function') return notPerformed('this export had no way to re-read the scan index, so the final check was not run')
  if (!snapshotDigest) return notPerformed('the scan index snapshot named no evidence digest, so there was nothing to compare the current evidence with')

  const ctl = new AbortController()
  let why = null
  const onCancel = () => { why ||= 'cancelled'; ctl.abort() }
  signal?.addEventListener?.('abort', onCancel, { once: true })
  const timer = setTimeout(() => { why ||= 'timeout'; ctl.abort() }, timeoutMs)
  const stopped = new Promise((resolve) => ctl.signal.addEventListener('abort', () => resolve(STOPPED), { once: true }))
  const secs = `${Math.round(timeoutMs / 100) / 10} s`
  const whyStopped = () => (why === 'cancelled'
    ? notPerformed('the export was cancelled while the final check was running, so it was abandoned before it answered')
    : { ...base, status: 'failed', checkedAt: now().toISOString(), reason: `the scan index did not answer within ${secs}, so the read was abandoned` })
  let res
  try {
    res = await Promise.race([
      Promise.resolve().then(() => getScanReportFacts(scanId, { offset: 0, limit: 1, signal: ctl.signal })),
      stopped,
    ])
  } catch (e) {
    if (why) return whyStopped()
    return { ...base, status: 'failed', checkedAt: now().toISOString(), reason: `the scan index could not be re-read (${errText(e)})` }
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener?.('abort', onCancel)
  }
  if (res === STOPPED) return whyStopped()
  const checkedAt = now().toISOString()
  if (!res || typeof res !== 'object') return { ...base, status: 'failed', checkedAt, reason: 'the server returned no scan index to compare' }
  const digest = typeof res.factsDigest === 'string' && res.factsDigest ? res.factsDigest : null
  const filesTotal = Number.isFinite(res.filesTotal) ? res.filesTotal : Number.isFinite(res.snapshot?.filesTotal) ? res.snapshot.filesTotal : null
  if (!digest) return { ...base, filesTotal, status: 'failed', checkedAt, reason: "the server's answer named no evidence digest, so it could not be compared with the snapshot" }
  if (digest === base.snapshotDigest) return { ...base, digest, filesTotal, status: 'verified', checkedAt, reason: null }
  const count = filesTotal != null && base.snapshotFilesTotal != null && filesTotal !== base.snapshotFilesTotal
    ? `The scan index now lists ${filesTotal} documents; the snapshot listed ${base.snapshotFilesTotal}. `
    : ''
  // Both digests are fields of the result; the reason says what they cannot.
  return { ...base, digest, filesTotal, status: 'changed', checkedAt, reason: `${count}This check does not say which documents changed.` }
}

// A tiny FIFO mutex: part finalisation is async and must not interleave.
function serial() {
  let tail = Promise.resolve()
  return (fn) => { const run = tail.then(fn, fn); tail = run.catch(() => {}); return run }
}

/**
 * Export every document in the scan index as its own packet.
 *
 * deps:
 *   loadIndex({ onProgress })     → loadScanReportFacts result ({facts, files, complete, incompleteReason, factsError})
 *   loadScanFiles()               → the scan's file records (getScan(scanId).files), used for names
 *                                   the caller's `files` does not hold
 *   buildFileData({ file, mode }) → buildFileReportData result (see makeFileDataBuilder)
 *   buildModel(d)                 → reportModel.buildFileReportModel
 *   renderBlob(args)              → reportRenderClient.renderReportBlob
 *   download(blob, filename)      → hand a finished part to the browser
 *   JSZip                         → the jszip constructor
 *   appLink(fileName)             → { href: absolute URL | null, note } for "Open in ACP"
 *   statusOf(fileRecord)          → docStatus.statusOf
 *   getScanReportFacts(sid, opts) → api.getScanReportFacts; used ONCE, for the final fresh check
 *                                   (contract 7). Absent → the check is 'not_performed' and the
 *                                   export can never call itself complete.
 *
 * Resolves {
 *   ok, complete, cancelled, incomplete, snapshotOnly, state, message, finalCheck,
 *   rows, parts: [{ n, filename, packets }], indexHtml, indexCsv, downloads: [{ label, blob, filename }]
 * }
 */
export async function exportScanPackets({
  scanId, mode = 'full', files = [], signal = null, onProgress = null, deps,
  concurrency = PACKET_CONCURRENCY, partMaxFiles = PART_MAX_FILES, partMaxBytes = PART_MAX_BYTES,
  now = () => new Date(), finalCheckTimeoutMs = FINAL_CHECK_TIMEOUT_MS,
} = {}) {
  const modeLabel = MODE_LABEL[mode] || mode
  const report = (p) => { try { onProgress?.(p) } catch { /* progress is advisory */ } }
  const aborted = () => !!signal?.aborted
  const cancelledBeforeStart = () => ({
    ok: false, complete: false, cancelled: true, rows: [], parts: [], downloads: [],
    message: 'The export was cancelled before any packet was made. Nothing was downloaded.',
  })

  // 1. The index — every document the SERVER lists, not the on-screen list.
  report({ phase: 'index', done: 0, total: null })
  let idx
  try {
    idx = await deps.loadIndex({ onProgress: (p) => report({ phase: 'index', done: p?.loaded ?? 0, total: p?.total ?? null }) })
  } catch (e) { idx = { facts: null, factsError: errText(e) } }
  if (aborted()) return cancelledBeforeStart()
  if (!idx?.facts || !Array.isArray(idx.files)) {
    return {
      ok: false, complete: false, cancelled: false, rows: [], parts: [], downloads: [],
      message: `No packets were made: the scan's report index could not be read (${idx?.factsError || idx?.incompleteReason || 'no index returned'}).`,
    }
  }
  const indexDigest = idx.facts.factsDigest ?? null
  const snapshot = idx.facts.snapshot && typeof idx.facts.snapshot === 'object' ? idx.facts.snapshot : null
  const platformVersion = idx.facts.identity?.platformVersion ?? null
  const exportedAt = now().toISOString()
  const indexReadAt = exportedAt
  const snapshotFilesTotal = Number.isFinite(snapshot?.filesTotal) ? snapshot.filesTotal : Number.isFinite(idx.filesTotal) ? idx.filesTotal : null

  // 2. One row per index entry, then the on-screen documents the index does not list.
  const rows = []
  const seen = new Set()
  for (const r of idx.files) {
    if (!r || typeof r.file !== 'string' || seen.has(r.file)) continue
    seen.add(r.file)
    rows.push({
      file: r.file, status: 'pending', format: null, reason: null, part: null, archivePath: null,
      indexRow: r, scanId, indexDigest,
      assessmentState: r.assessment?.state ?? null,
      findingsTotal: Number.isFinite(r.findingsTotal) ? r.findingsTotal : null,
      findingsOpen: Number.isFinite(r.findingsOpen) ? r.findingsOpen : null,
      changesVerified: Number.isFinite(r.savedChangesVerified) ? r.savedChangesVerified : null,
      changesUnverified: Number.isFinite(r.savedChangesUnverified) ? r.savedChangesUnverified : null,
      reviews: r.humanReviews ?? null,
      comparison: r.comparison ?? null,
      factsDigest: r.factsDigest ?? null,
      indexRowDigest: r.factsDigest ?? null,
      sourceChecksum: null, sourceChecksumKind: null, sourceSha256: null, correctedSha256: null,
      generatedAt: null, platformVersion,
    })
  }
  const indexed = rows.length
  for (const f of files || []) {
    const name = f?.file
    if (typeof name !== 'string' || !name || seen.has(name)) continue
    seen.add(name)
    // Listed on screen but absent from the index that was read. When the index stopped early the
    // document may well be in the part that was NOT read — which is a different statement.
    const reason = idx.complete === true ? null : 'the scan index stopped before it was read to the end, so this document may be in the part not read'
    rows.push({ file: name, status: 'notInIndex', format: null, reason, part: null, archivePath: null, scanId, indexDigest, platformVersion })
  }
  const stems = assignArchiveStems(rows.map((r) => r.file))
  for (const r of rows) {
    const link = deps.appLink ? deps.appLink(r.file) : null
    r.appHref = link?.href || null
    r.appHrefNote = link?.href ? null : (link?.note || 'link not available')
  }

  // 3. File records: the caller's list first, the scan read only for names it does not hold.
  const byName = new Map((files || []).filter((f) => f?.file).map((f) => [f.file, f]))
  let scanFilesP = null
  const recordOf = async (name) => {
    if (byName.has(name)) return byName.get(name)
    if (!deps.loadScanFiles) return null
    scanFilesP ||= Promise.resolve().then(deps.loadScanFiles).then((list) => {
      for (const f of list || []) if (f?.file && !byName.has(f.file)) byName.set(f.file, f)
    }).catch(() => {})
    await scanFilesP
    return byName.get(name) || null
  }

  // 4. Parts.
  const parts = []
  let zip = null
  let partPackets = 0
  let partBytes = 0
  let partN = 0
  const lock = serial()
  const freshZip = () => { zip = new deps.JSZip(); partPackets = 0; partBytes = 0; partN += 1 }
  const partName = (n, final) => `accessibility-packets-${mode}-${slug(scanId)}${final && n === 1 ? '' : `-part-${n}`}.zip`
  const closePart = async (final) => {
    const n = partN
    if (!final) {
      zip.file(`README-part-${n}.txt`, `Part ${n} of a per-file packet export for scan ${scanId} (${modeLabel}).\r\n`
        + 'The master index (index.html and index.csv) is in the LAST part. Extract every part into the same folder so its links resolve.\r\n'
        + `The packets in this part reflect the evidence snapshot ${indexDigest || '(digest not recorded)'}${snapshot?.builtAt ? ` built ${snapshot.builtAt}` : ''}. `
        + 'This part was made before the final check; the master index in the last part states whether that snapshot was still current when the export finished.\r\n')
    }
    report({ phase: 'zipping', part: n })
    const blob = await zip.generateAsync({ type: 'blob', compression: 'DEFLATE', compressionOptions: { level: 6 } })
    const filename = partName(n, final)
    parts.push({ n, filename, packets: partPackets, bytes: blob.size })
    deps.download(blob, filename)
    zip = null                                       // release this part before the next
  }
  const addPacket = (row, blob, format) => lock(async () => {
    if (!zip) freshZip()
    const path = packetPath(stems.get(row.file), format)
    // PDFs are already compressed; storing them saves CPU for no size cost.
    zip.file(path, blob, format === 'pdf' ? { compression: 'STORE' } : {})
    partPackets += 1
    partBytes += blob.size || 0
    row.part = partN
    row.archivePath = path
    if (partPackets >= partMaxFiles || partBytes >= partMaxBytes) await closePart(false)
  })

  // 5. The documents, in bounded parallel.
  const queue = rows.filter((r) => r.status === 'pending')
  const total = queue.length
  let done = 0
  const tally = () => summaryCounts(rows)
  const progress = () => report({ phase: 'packets', done, total, part: partN || 1, counts: tally() })
  progress()

  const one = async (row) => {
    const rec = await recordOf(row.file)
    if (aborted()) return
    if (!rec) { row.status = 'failed'; row.reason = NO_RECORD_REASON; return }
    if (deps.statusOf && deps.statusOf(rec) === 'unanalysable') { row.status = 'failed'; row.reason = UNANALYSABLE_REASON; return }
    let d
    try { d = await deps.buildFileData({ file: rec, mode }) } catch (e) {
      if (aborted()) return
      row.status = 'failed'; row.reason = `the report could not be assembled (${errText(e)})`; return
    }
    if (aborted()) return
    // When THIS document's evidence was read (and compared with its snapshot row, below).
    row.factsReadAt = now().toISOString()
    const facts = d?.facts
    if (!facts) { row.status = 'failed'; row.reason = `the report evidence could not be read (${d?.factsError || 'no facts returned'})`; return }
    const id = facts.identity || {}
    Object.assign(row, {
      factsDigest: facts.factsDigest ?? null,
      sourceChecksum: id.sourceChecksum ?? null, sourceChecksumKind: id.sourceChecksumKind ?? null,
      sourceSha256: id.sourceSha256 ?? null, correctedSha256: id.correctedSha256 ?? null,
      generatedAt: facts.generatedAt ?? null, platformVersion: id.platformVersion ?? platformVersion,
    })
    if (!row.indexRowDigest) { row.status = 'failed'; row.reason = 'the scan index row carries no facts digest, so this packet cannot be tied to the index'; return }
    if (facts.factsDigest !== row.indexRowDigest) {
      row.status = 'changed'
      row.reason = `${CHANGED_REASON} (index digest ${String(row.indexRowDigest).slice(0, 12)}…, current ${String(facts.factsDigest).slice(0, 12)}…)`
      return
    }
    let model
    try { model = deps.buildModel(d) } catch (e) { row.status = 'failed'; row.reason = `the report model could not be built (${errText(e)})`; return }
    const res = await deps.renderBlob({ scanId, kind: 'file', file: row.file, mode, model, factsDigest: facts.factsDigest, signal })
    if (res?.aborted || aborted()) return
    if (res?.ok && res.blob) { await addPacket(row, res.blob, 'pdf'); row.status = 'included'; row.format = 'pdf'; return }
    if (res?.format === 'html' && res.htmlBlob) {
      await addPacket(row, res.htmlBlob, 'html')
      row.status = 'included'; row.format = 'html'; row.reason = res.message || 'the PDF could not be produced'
      return
    }
    if (res?.status === 409) { row.status = 'changed'; row.reason = CHANGED_REASON; return }
    row.status = 'failed'
    row.reason = `${res?.status ? `HTTP ${res.status}: ` : ''}${res?.message || 'the report could not be rendered'}`
  }

  let next = 0
  const worker = async () => {
    while (!aborted() && next < queue.length) {
      const row = queue[next++]
      try { await one(row) } catch (e) {
        if (!aborted()) { row.status = 'failed'; row.reason = `unexpected error (${errText(e)})` }
      }
      if (row.status !== 'pending') { done += 1; progress() }
    }
  }
  await Promise.all(Array.from({ length: Math.max(1, Math.min(concurrency, queue.length || 1)) }, worker))

  const cancelled = aborted()
  for (const r of rows) if (r.status === 'pending') { r.status = 'cancelled'; r.reason = 'cancelled before this document was exported' }
  // A packet that raced the cancel into the ZIP is still in it, and says so (included).

  // 6. The final fresh check (contract 7): once, bounded, after every worker and before the index.
  //    Parts already downloaded cannot be recalled; the index names them.
  const partsBeforeFinalCheck = parts.map((p) => p.n)
  if (!cancelled) report({ phase: 'final-check' })
  const finalCheck = {
    ...await finalDigestCheck({
      scanId, snapshotDigest: indexDigest, snapshotFilesTotal, getScanReportFacts: deps.getScanReportFacts,
      signal, timeoutMs: finalCheckTimeoutMs, now,
    }),
    partsBeforeFinalCheck,
  }

  // 7. The master index — last part, and on its own.
  const header = {
    scanId, modeLabel, indexDigest, snapshotBuiltAt: snapshot?.builtAt ?? null, indexReadAt,
    filesTotal: Number.isFinite(idx.filesTotal) ? idx.filesTotal : indexed, exportedAt, platformVersion,
    indexComplete: idx.complete === true, indexIncompleteReason: idx.incompleteReason || idx.factsError || null,
    cancelled, partsTotal: partN || 1, finalCheck,
  }
  await lock(async () => {
    if (!zip) freshZip()
    header.partsTotal = partN
    zip.file('index.html', indexHtml({ rows, header }))
    zip.file('index.csv', indexCsv(rows, { header }))
    await closePart(true)
  })
  const verdict = completeness({ rows, ...header })
  const csv = indexCsv(rows, { header })
  const finalHtml = indexHtml({ rows, header })
  const c = verdict.counts
  const partsNote = parts.length > 1 ? ` in ${parts.length} ZIP parts (the master index is in the last)` : ''
  const htmlNote = c.html ? `, ${c.html} of them as HTML because the PDF could not be produced` : ''
  const complete = verdict.complete && !cancelled
  const snapshotOnly = !cancelled && verdict.packetsComplete && !verdict.complete
  const message = cancelled
    ? `Cancelled. ${c.included} of ${c.total} documents were exported${partsNote}; ${c.cancelled} were not (listed as skipped:cancelled in the index). This archive is INCOMPLETE. Final check not performed: ${finalCheck.reason}.`
    : complete
      ? `${c.included} of ${c.total} documents exported${partsNote}${htmlNote}; verified current at ${finalCheck.checkedAt} (the scan's evidence digest was unchanged from the snapshot).`
      : snapshotOnly
        ? `All ${c.total} documents exported${partsNote}${htmlNote}, but this archive is a SNAPSHOT ONLY (${finalCheckLabel(finalCheck)}): ${verdict.reasons.join(' ')}`
        : `Exported ${c.included} of ${c.total} documents${partsNote}. INCOMPLETE: ${verdict.reasons.join(' ')}`
  return {
    ok: complete, complete, cancelled,
    incomplete: !cancelled && !complete, snapshotOnly, state: cancelled ? 'cancelled' : verdict.state,
    finalCheck, message,
    rows, parts, indexHtml: finalHtml, indexCsv: csv,
    downloads: [
      { label: 'Master index (HTML)', blob: new Blob([finalHtml], { type: 'text/html;charset=utf-8' }), filename: `accessibility-packets-${mode}-${slug(scanId)}-index.html` },
      { label: 'Master index (CSV)', blob: new Blob([csv], { type: 'text/csv;charset=utf-8' }), filename: `accessibility-packets-${mode}-${slug(scanId)}-index.csv` },
    ],
  }
}

// A scan with this many documents is steered from one Full evidence PDF to per-file packets. The
// renderer refuses a model above 20,000 blocks or 16 MB (api/report_render.py); a scan's Full
// evidence carries every document's index row and every open finding, and the audit measured a
// 3,000-document Full evidence at 7 MB / 57 s — so from a few hundred documents the single PDF is
// slow at best and refused at worst. The note says "may"; it is a steer, not a prediction.
export const PACKETS_STEER_AT = 250
export const packetsSteerNote = (n) => (Number.isFinite(n) && n >= PACKETS_STEER_AT
  ? `This scan has ${n.toLocaleString()} documents. One Full evidence PDF may be too large to render and would then arrive as HTML; "${PACKET_FORMAT_LABEL}" gives each document its own PDF plus a master index of every document.`
  : null)

/**
 * The menu entry (ReportModeMenu format) for a scan. The heavy modules load only when it is used.
 * `getFiles()` returns the caller's current file records (used for lookups, and to list on-screen
 * documents the index does not hold); the export itself always covers the SERVER's scan index.
 */
export function packetZipFormat({ scanId, getFiles = () => [], aiEnabled = true }) {
  return {
    key: 'zip',
    label: PACKET_FORMAT_LABEL,
    title: 'One report per document in this scan, as ZIP part(s), with a master index (index.html and index.csv) that accounts for every document.',
    cancellable: true,
    progressText: packetProgressText,
    run: async (mode, { signal, onProgress } = {}) => {
      const { runScanPacketExport } = await import('./reportPacketLive.js')
      return runScanPacketExport({ scanId, mode, files: getFiles() || [], signal, onProgress, aiEnabled })
    },
  }
}

// Human progress line for the live region.
export function packetProgressText(p) {
  if (!p) return null
  if (p.phase === 'index') return `Reading the scan index… ${p.done ?? 0}${p.total != null ? ` of ${p.total}` : ''} document(s)`
  if (p.phase === 'zipping') return `Packing ZIP part ${p.part}…`
  if (p.phase === 'final-check') return 'Checking once that the evidence has not changed since the export began…'
  if (p.phase === 'packets') {
    const c = p.counts || {}
    const extra = [c.html ? `${c.html} as HTML` : null, c.failed ? `${c.failed} failed` : null, c.changed ? `${c.changed} changed` : null].filter(Boolean)
    return `Per-file packets: ${p.done} of ${p.total} document(s) processed${extra.length ? ` (${extra.join(', ')})` : ''} · part ${p.part}`
  }
  return null
}
