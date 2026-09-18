// Scan-level (estate) report: data gathering, client-side aggregation, and the renderer-agnostic
// report MODEL (kind 'scan'). This module fetches the scan-scoped endpoints, rolls them up the SAME
// way the on-screen panels do — per-rule outcomes exactly like Transparency's RuleBreakdown,
// per-document routing via sim's recommendationSummary — and turns the result into contract blocks
// that the server renderer (reportRenderClient.js) and htmlReport.js lay out.
//
// Evidence truth (the reason this module was rewritten):
//   * CLASSIFICATION is not EXECUTION. recommendationSummary's 'auto' bucket says a document is
//     ELIGIBLE for automatic fixing. It was once exported as `routing.fixed` and printed as
//     "Auto-fixed" — a recommendation reported as work done. Executed edits come only from saved
//     records: file_records.remediated_at (a corrected copy exists) and remediation_diff.
//   * VERIFIED is remediation_diff. Those rows are written only for fixes that cleared the post-fix
//     re-scan (api/store.py list_remediation_diffs), so eligible ≠ saved ≠ verified.
//   * Missing evidence is null ("Not recorded"), never 0. A failed fetch is not an empty queue.
//   * "No blocking findings" is a real count of documents, never an estimated confidence %, and
//     never a claim that a document conforms.
import { getScanTraces, listHitlQueue, getConfig, getScanRemediationDiffs } from './api.js'
import { statusOf, statusCounts as countStatuses, avgScore as avgOf, analysedCount } from './docStatus.js'
import { WCAG } from './wcagCatalog.js'
import { recommendationSummary } from './sim.js'
import { fixSteps, hasGuidance, appName } from './remediationGuide.js'
import {
  fileIssuesOf, rowsFromFindings, buildFindingCards, rankFindingCards, buildComparison,
  buildComparisonFromFacts, boundList, changeIdOf, scOfValue, criterionName,
} from './reportEvidence.js'
import { MODE_LABEL } from './reportModel.js'
import { fileEvidenceHref } from './evidenceLink.js'

// Normalize a filename to one of the native-app formats the remediation guide keys on.
const CHECKLIST_CAP = 60
const fmtOfName = (name) => {
  const ext = String(name || '').split('.').pop().toLowerCase()
  if (/^html?$/.test(ext)) return 'html'
  if (['docx', 'xlsx', 'pptx', 'pdf'].includes(ext)) return ext
  return null
}

const LEVEL_RANK = { A: 1, AA: 2, AAA: 3 }
// Same per-scan target the Assess runner persisted (Transparency reads this too).
const assessLevel = (scanId) => {
  try { return JSON.parse(sessionStorage.getItem(`acp-assess-${scanId || 'none'}`) || 'null')?.level || 'AA' } catch { return 'AA' }
}
const deptOf = (f) => f.department || f.dept || 'Unassigned'
const isAdvisory = (i) => String(i?.severity || '').toUpperCase() === 'REVIEW'

// Legacy jsPDF appendix bound — disclosed via appendixTotal. The MODEL lists every file.
const APPENDIX_CAP = 150
const MODES = ['summary', 'reviewer', 'full']
const REVIEWER_FINDING_CAP = 50
const NR = 'Not recorded'
const orNR = (v) => (v == null || v === '' ? NR : String(v))
// A document-name cell that opens that document's evidence view in ACP (evidenceLink contract 2).
// The scan report's cards carry client-side ids, so it cannot name an exact finding; it CAN name
// the document, and that is the link it offers — never a finding-level link it cannot back.
// Relative here; every renderer absolutizes it against a trusted origin or prints text only.
export const docCell = (file, scanId) => {
  const href = scanId && file ? fileEvidenceHref({ scanId, file }) : null
  return href ? { text: file, href } : file
}

// getScanRemediationDiffs(scanId, true) answers { items, total, documents, loaded, complete } from a
// real server, and a bare array in SIM — or [] when the request FAILED (api.js swallows errors).
// So an empty array is "not recorded", not "zero verified fixes".
export function normaliseDiffSummary(raw) {
  if (raw && !Array.isArray(raw) && typeof raw === 'object' && Array.isArray(raw.items)) {
    const total = Number.isFinite(raw.total) ? raw.total : null
    return {
      items: raw.items,
      total,
      documents: Number.isFinite(raw.documents) ? raw.documents : null,
      complete: raw.complete === true || (total != null && raw.items.length === total),
    }
  }
  if (Array.isArray(raw) && raw.length) {
    return { items: raw, total: raw.length, documents: new Set(raw.map((d) => d.file)).size, complete: true }
  }
  return null
}

const STATUS_TXT = {
  certifiable: 'No blocking findings', issues: 'Open findings', clean: 'No findings',
  'not-assessed': 'Not assessed', uncertain: 'Uncertain', unanalysable: 'Could not be analysed',
}
const ROUTE_TXT = {
  auto: 'Eligible for automatic fixing', assisted: 'Recommended: AI-assisted review', review: 'Recommended: human review',
  manual: 'Recommended: manual remediation', archive: 'Recommended: archive', keep: 'Recommended: keep as is',
}

// Pure aggregation of fetched inputs into the report data (legacy fields + evidence fields).
export function aggregateScanReport({ scanId = null, files = [], traces = [], hitlItems = null, cfg = null,
  diffSummary = null, targetLevel = 'AA', org = 'your organisation', now = new Date(), previous = null, scope,
  facts = null } = {}) {
  const rows = Array.isArray(traces) ? traces : []

  // ── Per-rule rollup (identical to RuleBreakdown) ──────────────────────────
  const byRule = {}
  rows.forEach((r) => {
    const k = r.rule_id
    if (!byRule[k]) byRule[k] = { id: r.rule_id, name: r.plain_name || r.rule_name || r.rule_id, level: r.level, pass: 0, fail: 0, skip: 0, findings: 0 }
    const o = String(r.outcome || '').toUpperCase()
    if (o === 'PASS') byRule[k].pass++
    else if (o === 'FAIL') byRule[k].fail++
    else byRule[k].skip++
    byRule[k].findings += r.finding_count || 0
  })
  const rules = Object.values(byRule).sort((a, b) => b.fail - a.fail || String(a.id).localeCompare(String(b.id)))
  const failingAll = rules.filter((r) => r.fail > 0)
  const topFailing = failingAll.slice(0, 8)

  // In-scope / automated coverage counts (same scoping as RuleBreakdown's header).
  const targetRank = LEVEL_RANK[targetLevel] || 2
  const inScope = WCAG.filter((c) => c.docApplies !== false && (LEVEL_RANK[c.level] || 3) <= targetRank)
  const inScopeIds = new Set(inScope.map((c) => c.sc))
  const criteriaAutomated = rules.filter((r) => inScopeIds.has(r.id) || !WCAG.some((c) => c.sc === r.id)).length

  // ── Failure heatmap: top failing criteria × department (top depts by failures) ──
  const deptByFile = {}
  files.forEach((f) => { deptByFile[f.file] = deptOf(f) })
  const topIds = new Set(topFailing.map((r) => r.id))
  const cell = {}
  const deptFail = {}
  rows.forEach((r) => {
    if (String(r.outcome || '').toUpperCase() !== 'FAIL' || !topIds.has(r.rule_id)) return
    const dpt = deptByFile[r.file] || 'Unassigned'
    cell[`${r.rule_id}::${dpt}`] = (cell[`${r.rule_id}::${dpt}`] || 0) + 1
    deptFail[dpt] = (deptFail[dpt] || 0) + 1
  })
  const allFailDepts = Object.keys(deptFail).sort((a, b) => deptFail[b] - deptFail[a])
  const heatDepts = allFailDepts.slice(0, 6)
  const heatmap = {
    depts: heatDepts,
    moreDepts: Math.max(0, allFailDepts.length - heatDepts.length),
    rows: topFailing.map((r) => ({ id: r.id, name: r.name, cells: heatDepts.map((dpt) => cell[`${r.id}::${dpt}`] || 0) })),
  }

  // ── Manual remediation checklist: every FAILING criterion × the file format(s) it fails in,
  //    paired with the native-app menu path to fix it by hand on Mac and Windows. Driven entirely
  //    by real FAIL traces — no criterion appears that the scan did not flag.
  const fileFmt = {}
  files.forEach((f) => { fileFmt[f.file] = fmtOfName(f.file) || (String(f.type || '').toLowerCase() || null) })
  const clMap = {}
  rows.forEach((r) => {
    if (String(r.outcome || '').toUpperCase() !== 'FAIL') return
    const fmt = fileFmt[r.file]
    if (!fmt) return
    const sc = r.rule_id
    const key = `${sc}::${fmt}`
    if (!clMap[key]) clMap[key] = { sc, name: byRule[sc]?.name || r.plain_name || sc, level: byRule[sc]?.level || r.level || null, fmt, docs: 0 }
    clMap[key].docs++
  })
  const manualChecklistAll = Object.values(clMap)
    .map((it) => { const s = fixSteps(it.sc, it.fmt); return { ...it, app: appName(it.fmt), where: s.where, mac: s.mac, win: s.win, specific: hasGuidance(it.sc) } })
    .sort((a, b) => (b.specific - a.specific) || (b.docs - a.docs) || String(a.sc).localeCompare(String(b.sc)))
  const manualChecklist = manualChecklistAll
  const checklistTruncated = manualChecklist.length > CHECKLIST_CAP

  // ── Document-level rollups ────────────────────────────────────────────────
  const totalFiles = files.length
  const conformantN = files.filter((f) => statusOf(f) === 'certifiable').length
  const conformantPct = totalFiles ? Math.round((conformantN / totalFiles) * 100) : 0
  const avgScore = avgOf(files)
  const statusCounts = countStatuses(files)

  const groupBy = (fn) => files.reduce((m, f) => { const k = fn(f); if (k != null) (m[k] = m[k] || []).push(f); return m }, {})
  const rollup = (groups) => Object.entries(groups).map(([label, fs]) => ({
    label,
    docs: fs.length,
    avg: avgOf(fs),
    conformant: fs.filter((f) => statusOf(f) === 'certifiable').length,
    findings: fs.reduce((a, f) => a + (f.issues || []).length, 0),
  })).sort((a, b) => b.docs - a.docs)
  const byDept = rollup(groupBy(deptOf))
  const bySource = files.some((f) => f.sourceName) ? rollup(groupBy((f) => f.sourceName || 'Unknown')) : []

  // ── Recommended routes — CLASSIFICATION, not work performed ───────────────
  const rec = recommendationSummary(files)
  const bucketN = (a) => rec.buckets.find((b) => b.action === a)?.n || 0
  const routing = {
    eligibleAuto: bucketN('auto'),
    humanRouted: bucketN('assisted') + bucketN('review') + bucketN('manual'),
    deferred: bucketN('archive') + bucketN('keep'),
    buckets: rec.buckets,
  }
  // No savedMin: it was manualMin - remediateMin, both invented per-finding constants.
  const effort = { remediateMin: rec.remediateMin, autoPct: rec.autoPct, remediableDocs: rec.remediableDocs }

  // ── Review queue status (null when the queue could not be read) ───────────
  let hitl = null
  if (Array.isArray(hitlItems)) {
    const hc = { pending: 0, approved: 0, rejected: 0, skipped: 0 }
    hitlItems.forEach((it) => { if (hc[it.status] != null) hc[it.status]++ })
    hitl = { ...hc, total: hitlItems.length }
  }

  // ── Executed and verified work — from saved records only ──────────────────
  const remediatedKnown = files.some((f) => Object.prototype.hasOwnProperty.call(f, 'remediated_at'))
  const remediatedFiles = files.filter((f) => f.remediated_at)
  const verified = normaliseDiffSummary(diffSummary)
  const verifiedByFile = {}
  if (verified) verified.items.forEach((d) => { verifiedByFile[d.file] = (verifiedByFile[d.file] || 0) + 1 })
  const publishedKnown = files.some((f) => Object.prototype.hasOwnProperty.call(f, 'published_at'))
  const execution = {
    documentsWithSavedEdits: remediatedKnown ? remediatedFiles.length : null,
    verifiedFixes: verified ? verified.total : null,
    verifiedDocuments: verified ? verified.documents : null,
    verifiedComplete: verified ? verified.complete : null,
    verifiedItems: verified ? verified.items : null,
    publishedDocuments: publishedKnown ? files.filter((f) => f.published_at).length : null,
  }

  // ── Master index: every file, worst-scoring first ─────────────────────────
  const index = files
    .map((f) => {
      const issues = f.issues || []
      return {
        file: f.file, dept: deptOf(f), score: f.score ?? null, status: statusOf(f), action: f.rec?.action || null,
        findings: issues.filter((i) => !isAdvisory(i)).length, advisory: issues.filter(isAdvisory).length,
        remediatedAt: remediatedKnown ? (f.remediated_at || null) : undefined,
        verifiedFixes: verified ? (verifiedByFile[f.file] || 0) : null,
        publishedAt: publishedKnown ? (f.published_at || null) : undefined,
      }
    })
    .sort((a, b) => (a.score == null ? 999 : a.score) - (b.score == null ? 999 : b.score) || String(a.file).localeCompare(String(b.file)))
  const appendix = index.slice(0, APPENDIX_CAP)

  return {
    scanId, org, targetLevel,
    platformVersion: cfg?.version ?? null,
    generatedAt: now.toISOString(),
    date: now.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' }),
    timestamp: now.toLocaleString('en-US', { year: 'numeric', month: 'long', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' }),
    totalFiles, analysedFiles: analysedCount(files), conformantN, conformantPct, avgScore, statusCounts,
    criteriaAutomated, inScopeCount: inScope.length,
    topFailing, failingAll, heatmap, byDept, bySource, routing, effort, hitl, execution,
    appendix, appendixTotal: index.length, index,
    manualChecklist: manualChecklist.slice(0, CHECKLIST_CAP), checklistTruncated,
    checklistTotal: manualChecklist.length, manualChecklistAll,
    files, previous, scope,
    facts: facts && typeof facts === 'object' ? facts : null,
  }
}

// Pure: aggregated scan data → contract report model (kind 'scan').
export function buildScanReportModel(data = {}) {
  const mode = MODES.includes(data.mode) ? data.mode : 'summary'
  const atLeast = (m) => MODES.indexOf(mode) >= MODES.indexOf(m)
  const files = data.files || []
  const ex = data.execution || {}
  // Scan-level report facts (stream D). When present they are the authority for the counts below,
  // exactly as at file level: the client re-shapes, it does not re-derive.
  const facts = data.facts && typeof data.facts === 'object' ? data.facts : null
  const acc = facts?.accounting || null
  const factsFiles = facts && Array.isArray(facts.files) ? facts.files : null
  const indexState = factsIndexState(facts, data)
  const level = data.targetLevel || 'AA'
  const total = data.totalFiles ?? files.length
  const analysed = data.analysedFiles ?? null
  const blocks = []
  const H = (text, lvl = 1) => blocks.push({ k: 'heading', text, level: lvl })
  const T = (text, o) => blocks.push({ k: 'text', text, o: o || {} })
  const scanIdForLinks = data.scanId ?? facts?.identity?.scanId ?? null

  // Remaining findings: only on documents WITHOUT a saved corrected copy. A remediated document's
  // issue list is the pre-remediation assessment, so counting it would report fixed work as open.
  const openFiles = files.filter((f) => !f.remediated_at)
  const awaitingReassess = files.filter((f) => f.remediated_at && (f.issues || []).some((i) => !isAdvisory(i)))
  const findingsRemaining = files.length || total === 0
    ? openFiles.reduce((n, f) => n + (f.issues || []).filter((i) => !isAdvisory(i)).length, 0)
    : null
  const hitl = data.hitl || null
  const notAnalysable = data.statusCounts ? (data.statusCounts['not-assessed'] || 0) + (data.statusCounts.unanalysable || 0) : null
  const eligible = data.routing?.eligibleAuto ?? null
  // "Documents assessed" is a count of ASSESSMENTS, from each file's recorded assessment state —
  // not of catalog rows, and not of documents that merely appear in the file list.
  // The server totals the assessment states itself; files[] is the fallback for a paginated page.
  const totals = facts?.totals || null
  const assessedN = totals && Number.isFinite(totals.assessed)
    ? totals.assessed + (Number.isFinite(totals.partial) ? totals.partial : 0)
    : factsFiles
      ? factsFiles.filter((f) => f?.assessment?.state === 'assessed' || f?.assessment?.state === 'partial').length
      : analysed
  const notAssessedN = totals && Number.isFinite(totals.documents) ? totals.documents - assessedN
    : factsFiles ? factsFiles.length - assessedN : null
  const ledger = acc?.resolutionLedger ?? null
  const findingsResolved = ledger === 'per_finding' && Number.isFinite(acc?.findingsResolvedVerified)
    ? acc.findingsResolvedVerified : null

  H('Decision summary')
  const outstanding = [
    findingsRemaining ? `${findingsRemaining} open finding${findingsRemaining === 1 ? '' : 's'}` : null,
    awaitingReassess.length ? `${awaitingReassess.length} corrected document${awaitingReassess.length === 1 ? '' : 's'} awaiting re-assessment` : null,
    hitl?.pending ? `${hitl.pending} review item${hitl.pending === 1 ? '' : 's'} pending` : null,
    notAnalysable ? `${notAnalysable} document${notAnalysable === 1 ? '' : 's'} not assessed or not analysable` : null,
  ].filter(Boolean)
  blocks.push({
    k: 'callout',
    text: `${data.conformantN ?? 0} of ${total} documents came back with no blocking findings among the WCAG 2.1 Level ${level} criteria ACP checked.${outstanding.length ? ` Outstanding: ${outstanding.join('; ')}.` : ''} ${eligible != null ? `${eligible} document${eligible === 1 ? ' is' : 's are'} eligible for automatic fixing — a recommendation, not work performed.` : ''}`.trim(),
    o: { color: outstanding.length ? AMBER_HEX : GREEN_HEX },
  })
  blocks.push({
    k: 'decisionSummary',
    caption: 'Estate decision evidence',
    items: [
      { key: 'documentsAssessed', label: 'Documents assessed', value: assessedN,
        detail: facts
          ? `${totals?.documents ?? total} document${(totals?.documents ?? total) === 1 ? '' : 's'} in this scan; ${notAssessedN} not assessed, errored or only partly assessed — an empty finding list for those is not "no findings"`
          : `${total} document${total === 1 ? '' : 's'} in this scan` },
      { key: 'editsSaved', label: 'Documents with saved edits', value: ex.documentsWithSavedEdits ?? null, detail: ex.documentsWithSavedEdits == null ? 'Saved-copy status was not included in the file list' : 'Documents with a saved corrected copy' },
      // FINDINGS resolved, which is not the same number as CHANGES verified. Without a per-finding
      // ledger this is "Not recorded"; the verified-change count is stated in the basis instead.
      { key: 'findingsVerifiedResolved', label: 'Findings verified resolved', value: findingsResolved,
        detail: findingsResolved != null
          ? `Counted finding by finding from the resolution ledger`
          : `NOT RECORDED — ${acc?.accountingReason || 'no per-finding ledger links a saved change to the finding it resolved'}. ${ex.verifiedFixes == null ? 'Remediation records could not be loaded' : `${ex.verifiedFixes} saved change(s) across ${orNR(ex.verifiedDocuments)} document(s) cleared the re-scan`}, which is a change count, not a finding count.` },
      { key: 'findingsRemaining', label: 'Findings remaining', value: findingsRemaining,
        detail: awaitingReassess.length ? `On documents without a corrected copy; ${awaitingReassess.length} corrected document(s) await re-assessment and are not counted` : 'Blocking findings on documents without a corrected copy' },
      { key: 'humanChecksPending', label: 'Human checks pending', value: hitl ? hitl.pending : null, detail: hitl ? `${hitl.total} review item(s) in total` : 'The review queue could not be read' },
      { key: 'checksNotPerformed', label: 'Documents not checked', value: notAnalysable, detail: 'Not assessed yet, or could not be analysed' },
    ],
  })
  const decided = hitl ? hitl.approved + hitl.rejected + hitl.skipped : null
  blocks.push({
    k: 'stageStrip',
    items: [
      { key: 'suggestions', label: 'Suggestions', value: hitl ? hitl.total : null, status: hitl == null ? 'unknown' : hitl.total ? 'done' : 'not_started', detail: hitl == null ? 'Review queue not loaded' : `${hitl.total} suggestion(s) queued for review; ${eligible ?? NR} document(s) eligible for automatic fixing` },
      { key: 'savedEdits', label: 'Saved edits', value: ex.documentsWithSavedEdits ?? null, status: ex.documentsWithSavedEdits == null ? 'unknown' : ex.documentsWithSavedEdits ? 'done' : 'not_started', detail: 'Documents with a saved corrected copy' },
      { key: 'technicalChecks', label: 'Technical re-checks', value: ex.verifiedFixes ?? null, status: ex.verifiedFixes == null ? 'unknown' : awaitingReassess.length ? 'pending' : ex.verifiedFixes ? 'done' : 'not_started', detail: ex.verifiedFixes == null ? NR : `${ex.verifiedFixes} fix record(s) cleared the re-scan${ex.verifiedComplete === false ? ' (partial list)' : ''}` },
      { key: 'humanConfirmation', label: 'Human confirmation', value: decided, status: hitl == null ? 'unknown' : hitl.pending ? 'pending' : hitl.total ? 'done' : 'not_started', detail: hitl == null ? NR : `${hitl.approved} approved, ${hitl.rejected} rejected, ${hitl.skipped} skipped, ${hitl.pending} pending` },
      { key: 'publication', label: 'Publication', value: ex.publishedDocuments ?? null, status: ex.publishedDocuments == null ? 'unknown' : ex.publishedDocuments ? 'done' : 'not_started', detail: ex.publishedDocuments == null ? 'Publication status not recorded in this report' : 'Documents published' },
    ],
  })
  // S2: the server index can stop short (a page failed, the page budget ran out, the evidence
  // changed while paging). Say so where the reader decides, not only in the appendix.
  if (indexState.partial) {
    T(`The per-document evidence index in this report is PARTIAL: ${indexState.loaded} of ${indexState.total ?? 'an unknown number of'} documents were loaded. ${indexState.reason || 'The reason was not recorded.'} Scan-wide counts the server computed (documents assessed, findings verified resolved, the estate comparison) cover every document; per-document rows cover only the documents loaded.`, { bold: true, color: AMBER_HEX })
  }
  T(`Average score across scored documents: ${data.avgScore != null ? `${data.avgScore}/100` : NR} — a secondary indicator.`, { size: 9, color: MUTED_HEX })

  H('What this report covers')
  blocks.push({
    k: 'table',
    headers: ['Field', 'Value'],
    caption: 'Report scope and identity',
    rows: [
      ['Organisation', orNR(data.org)],
      ['Assessment (scan) id', orNR(data.scanId)],
      ['Target', `WCAG 2.1 Level ${level}`],
      ['Documents in scan', String(total)],
      ['Criteria with automated checks in scope', `${orNR(data.criteriaAutomated)} of ${orNR(data.inScopeCount)}`],
      ['Report generated', orNR(data.generatedAt || data.timestamp)],
      ['Platform version', orNR(data.platformVersion)],
      ['Report mode', MODE_LABEL[mode]],
    ],
  })
  H('Documents by outcome', 2)
  blocks.push({
    k: 'table',
    headers: ['Outcome', 'Documents'],
    caption: 'Documents by outcome',
    rows: Object.entries(data.statusCounts || {}).filter(([, n]) => n > 0).map(([k, n]) => [STATUS_TXT[k] || k, String(n)]),
  })

  H('Since the previous assessment')
  const estate = facts && facts.comparison && typeof facts.comparison === 'object' && facts.comparison.totals
    ? facts.comparison : null
  if (estate) {
    // C4: the server's REAL estate comparison — every document compared finding by finding with
    // its own most recent earlier assessment, then totalled. Nothing is inferred from scores.
    estateComparisonBlocks(estate, { factsFiles, mode, atLeast, T, H, blocks, scanId: scanIdForLinks })
  } else {
    let cmp
    if (facts) {
      // Only a real comparable snapshot the server supplied. Aggregate counts are never subtracted.
      cmp = buildComparisonFromFacts(facts)
    } else {
      const current = openFiles.flatMap((f) => fileIssuesOf(f).filter((i) => i.severity !== 'REVIEW'))
      cmp = buildComparison(data.previous, { file: null, scope: data.scope === undefined ? null : data.scope, findings: current })
      if (!data.previous) cmp.reason = typeof data.previousReason === 'string' && data.previousReason ? data.previousReason : 'No comparable earlier estate snapshot was supplied, so no change is reported. Aggregate counts are never subtracted to imply one.'
    }
    blocks.push(cmp)
  }

  if (atLeast('reviewer')) {
    H('Remaining work across documents')
    const cards = rankFindingCards(openFiles.flatMap((f) => buildFindingCards({
      file: f.file, rows: rowsFromFindings(f), assignee: f.assignee ?? f.owner ?? null,
    }).map((c) => ({ ...c, title: `${f.file} — ${c.title}`, file: f.file }))))
    if (awaitingReassess.length) T(`${awaitingReassess.length} document(s) with a saved corrected copy are not listed here until they are re-assessed.`, { size: 9, color: MUTED_HEX })
    if (cards.length) {
      const b = boundList(cards, mode === 'full' ? null : REVIEWER_FINDING_CAP)
      b.shown.forEach((c) => blocks.push(c))
      if (b.omitted) T(`${b.omitted} more finding${b.omitted === 1 ? '' : 's'} (of ${b.total}) are listed in the Full evidence report.`, { bold: true })
    } else {
      T('No open findings are recorded on documents without a corrected copy.', { color: MUTED_HEX })
    }

    H('Most frequent failing criteria')
    const failing = mode === 'full' ? (data.failingAll || data.topFailing || []) : (data.topFailing || [])
    blocks.push({
      k: 'table',
      headers: ['WCAG', 'Criterion', 'Level', 'Documents failing', 'Findings'],
      caption: 'Most frequent failing criteria',
      rows: failing.map((r) => [r.id, r.name, r.level || NR, String(r.fail), String(r.findings)]),
    })
    const failingTotal = (data.failingAll || []).length
    if (mode !== 'full' && failingTotal > failing.length) T(`Showing ${failing.length} of ${failingTotal} failing criteria; all are in the Full evidence report.`, { size: 9, color: MUTED_HEX })

    if (data.heatmap?.depts?.length) {
      H('Failures by department', 2)
      blocks.push({
        k: 'table',
        headers: ['Criterion', ...data.heatmap.depts],
        caption: 'Failing documents per criterion and department',
        rows: data.heatmap.rows.map((r) => [`${r.id} ${r.name}`, ...r.cells.map(String)]),
      })
      if (data.heatmap.moreDepts) T(`${data.heatmap.moreDepts} further department(s) with failures are not shown in this grid; the master index lists every document.`, { size: 9, color: MUTED_HEX })
    }
    if ((data.byDept || []).length) {
      H('By department', 2)
      blocks.push({
        k: 'table',
        headers: ['Department', 'Documents', 'No blocking findings', 'Findings', 'Average score'],
        caption: 'Outcomes by department',
        rows: data.byDept.map((g) => [g.label, String(g.docs), String(g.conformant), String(g.findings), g.avg != null ? String(g.avg) : NR]),
      })
    }
    if ((data.bySource || []).length) {
      H('By source', 2)
      blocks.push({
        k: 'table',
        headers: ['Source', 'Documents', 'No blocking findings', 'Findings', 'Average score'],
        caption: 'Outcomes by source',
        rows: data.bySource.map((g) => [g.label, String(g.docs), String(g.conformant), String(g.findings), g.avg != null ? String(g.avg) : NR]),
      })
    }

    H('Recommended routes')
    T('The classifier’s recommendation for each document. These are recommendations, not work performed — saved and verified edits are counted in the decision summary.', { size: 9, color: MUTED_HEX })
    blocks.push({
      k: 'table',
      headers: ['Recommendation', 'Documents'],
      caption: 'Recommended remediation routes',
      rows: (data.routing?.buckets || []).map((b) => [ROUTE_TXT[b.action] || b.action, String(b.n)]),
    })

    H('Manual remediation checklist')
    const all = data.manualChecklistAll || data.manualChecklist || []
    const shown = mode === 'full' ? all : all.slice(0, CHECKLIST_CAP)
    blocks.push({
      k: 'table',
      headers: ['WCAG', 'Criterion', 'Format', 'Documents', 'Where', 'macOS', 'Windows'],
      caption: 'Manual remediation checklist, most actionable first',
      rows: shown.map((it) => [it.sc, it.name, `${it.fmt} (${it.app})`, String(it.docs), it.where || '', it.mac || '', it.win || '']),
    })
    const clTotal = data.checklistTotal ?? all.length
    if (shown.length < clTotal) T(`Showing ${shown.length} of ${clTotal} checklist rows, most actionable first; every row is in the Full evidence report.`, { bold: true })
  }

  if (atLeast('full')) {
    blocks.push({ k: 'pageBreak' })
    H('Complete evidence appendix')
    const index = data.index || data.appendix || []
    if (factsFiles) {
      // S2: the master index comes from the SERVER's per-document index — every document in the
      // scan, each with its own assessment state, findings, verification, reviews and comparison —
      // not from the list this screen happened to hold. When the index stopped short, the table
      // says how many of how many and why.
      const screen = new Map(index.map((r) => [r.file, r]))
      blocks.push({
        k: 'appendixTable',
        id: 'appendix-documents',
        complete: !indexState.partial,
        totalRecords: indexState.total,
        limitNote: indexState.partial ? (indexState.reason || 'the pages of the server index that could be read') : null,
        source: 'server-index',
        headers: ['Document', 'Assessment', 'Findings', 'Open', 'Verified resolved', 'Saved changes (verified / not verified)', 'Reviewer decisions pending', 'Since previous assessment', 'Approvals needing recheck', 'Department', 'Recommendation'],
        caption: 'Master index of every document (server evidence index)',
        rows: factsFiles.map((f) => {
          const s = screen.get(f.file) || {}
          return [
            docCell(f.file, scanIdForLinks),
            `${ASSESS_TXT[f.assessment?.state] || orNR(f.assessment?.state)}${f.assessment?.state && f.assessment.state !== 'assessed' && f.assessment.stateReason ? ` — ${f.assessment.stateReason}` : ''}`,
            orNR(f.findingsTotal), orNR(f.findingsOpen), orNR(f.findingsResolvedVerified),
            `${orNR(f.savedChangesVerified)} / ${orNR(f.savedChangesUnverified)}${f.savedChangesComplete === false ? ' (list incomplete)' : ''}`,
            orNR(f.humanReviews?.pending),
            rowComparisonText(f.comparison),
            orNR(f.approvalsRecheckRequired),
            s.dept || NR,
            s.action ? (ROUTE_TXT[s.action] || s.action) : NR,
          ]
        }),
      })
    }
    if (!factsFiles) blocks.push({
      k: 'appendixTable',
      id: 'appendix-documents',
      complete: index.length === total,
      totalRecords: total,
      limitNote: 'the documents included in the file list',
      headers: ['Document', 'Department', 'Outcome', 'Score', 'Blocking findings', 'Advisory', 'Corrected copy saved', 'Verified fixes', 'Published', 'Recommendation'],
      caption: 'Master index of every document',
      rows: index.map((r) => [
        r.file, r.dept, STATUS_TXT[r.status] || r.status, r.score != null ? String(r.score) : NR,
        String(r.findings ?? NR), String(r.advisory ?? NR),
        r.remediatedAt === undefined ? NR : r.remediatedAt ? `Yes · ${r.remediatedAt}` : 'No',
        r.verifiedFixes == null ? NR : `${r.verifiedFixes}${ex.verifiedComplete === false ? ' (partial)' : ''}`,
        r.publishedAt === undefined ? NR : r.publishedAt ? `Yes · ${r.publishedAt}` : 'No',
        r.action ? (ROUTE_TXT[r.action] || r.action) : NR,
      ]),
    })
    const items = ex.verifiedItems
    blocks.push({
      k: 'appendixTable',
      id: 'appendix-verified-fixes',
      complete: items ? ex.verifiedComplete !== false : false,
      totalRecords: ex.verifiedFixes ?? null,
      limitNote: items ? 'the page of remediation records the server returned' : 'remediation records could not be loaded',
      headers: ['Record id', 'Document', 'Criterion', 'Reason', 'Before', 'After'],
      caption: 'Verified fix records (full before and after)',
      rows: (items || []).map((d, i) => {
        const sc = scOfValue(d.rule_id)
        return [changeIdOf(d.file, d, i), d.file, `${sc || d.rule_id}${criterionName(sc) ? ` · ${criterionName(sc)}` : ''}`,
          d.note || 'Reason not recorded', d.before == null ? NR : String(d.before), d.after == null ? NR : String(d.after)]
      }),
    })
  }

  H('What this report is, and is not')
  T(`A record of what ACP detected, changed and re-verified across this scan against the WCAG 2.1 Level ${level} criteria in scope. It is not a conformance determination, a certification or legal advice; it can support an ADA, Section 508 or EN 301 549 / European Accessibility Act review as evidence alongside a qualified human evaluation.`, { size: 9, color: MUTED_HEX })

  const slugOrg = String(data.org || 'estate').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '') || 'estate'
  return {
    docTitle: `Accessibility Assessment Report — ${data.org || 'Document estate'}`,
    filename: `mova-${slugOrg}-scan-${mode === 'full' ? 'evidence' : mode}-report`,
    lang: 'en-US',
    mode,
    kind: 'scan',
    identity: {
      scanId: data.scanId ?? facts?.identity?.scanId ?? null, file: null,
      sourceChecksum: null, sourceChecksumKind: null, sourceSha256: null, correctedSha256: null,
      currentArtifact: null, artifactVersion: null,
      generatedAt: data.generatedAt || new Date().toISOString(), platformVersion: data.platformVersion ?? null, targetLevel: level,
      // The server render route recomputes this and answers 409 when the facts have moved on.
      factsDigest: data.identity?.factsDigest ?? facts?.factsDigest ?? data.factsDigest ?? null,
    },
    factsDigest: data.identity?.factsDigest ?? facts?.factsDigest ?? data.factsDigest ?? null,
    targetLevel: level,
    footerVersion: data.platformVersion ?? null,
    footerGenerated: data.timestamp || data.generatedAt || null,
    cover: {
      title: 'Accessibility Assessment Report',
      subtitle: `${data.org || 'Document estate'} · WCAG 2.1 Level ${level}`,
      meta: [`${MODE_LABEL[mode]} · generated ${data.timestamp || data.generatedAt || ''}`.trim(), data.scanId ? `Scan ${data.scanId}` : null].filter(Boolean),
    },
    blocks,
  }
}

// Palette hexes (kept local so this module does not depend on the file model's colour exports).
const AMBER_HEX = '#854F0B', GREEN_HEX = '#3B6D11', MUTED_HEX = '#6B6670'

const ASSESS_TXT = { assessed: 'Assessed', partial: 'Partly assessed', error: 'Could not be analysed', not_assessed: 'Not assessed' }
const REVIEWER_COMPARISON_ROWS = 50
const n0 = (v) => (Number.isFinite(v) ? v : null)

// Whether the server's per-document index reached this report whole (S2). The caller's own
// verdict (loadScanReportFacts → factsIndexComplete / factsIncompleteReason) wins; without one the
// rows are counted against the server's filesTotal. Never "complete" by default.
export function factsIndexState(facts, data = {}) {
  const rows = facts && Array.isArray(facts.files) ? facts.files.length : null
  const total = facts ? (n0(facts.snapshot?.filesTotal) ?? n0(facts.filesTotal)) : null
  const reason = typeof data.factsIncompleteReason === 'string' && data.factsIncompleteReason.trim() ? data.factsIncompleteReason.trim() : null
  if (!facts) return { partial: false, loaded: null, total: null, reason: null, known: false }
  let partial
  if (data.factsIndexComplete === false) partial = true
  else if (data.factsIndexComplete === true) partial = total != null && rows != null && rows < total
  else partial = total == null || rows == null || rows < total
  return { partial, loaded: rows, total, reason: partial ? (reason || (total == null ? 'The server did not state how many documents the index holds.' : null)) : null, known: true }
}

// One document's comparison, in words, for the master index. Every status is a different fact.
export function rowComparisonText(c) {
  if (!c || typeof c !== 'object') return NR
  if (c.status === 'compared') {
    const parts = [`${orNR(c.introduced)} new`, `${orNR(c.resolved)} no longer reported`, `${orNR(c.persisting)} still present`]
    if (c.reopened != null) parts.push(`${c.reopened} reopened`)
    const nc = c.notComparable?.current
    if (nc) parts.push(`${nc} not matchable (no location)`)
    return `${parts.join(', ')}${c.renamed ? ` · renamed (was ${c.baseline?.file || 'another name'})` : ''}`
  }
  if (c.status === 'no_baseline') return c.reasonCode === 'no_earlier_assessment' ? 'No earlier assessment (new to the record)' : `No comparable earlier assessment — ${orNR(c.reason)}`
  if (c.status === 'baseline_unusable') return `Earlier assessment not usable — ${orNR(c.reason)}`
  if (c.status === 'not_comparable') return `Not compared — ${orNR(c.reason)}`
  return NR
}

// C4: the estate comparison as blocks every renderer lays out truthfully. A `text` block carries
// the whole answer in words, and the Reviewer packet / Full
// evidence add the table and the per-document rows. It is deliberately NOT a `comparison` block:
// that block's renderers count their `resolved`/`introduced` LISTS, and an estate's findings are
// not listed here — a list of zero items would print "Newly reported: 0" over a real count.
// The headline sentence is BOLD on purpose. Under page pressure the one-page summary drops plain
// text and keeps bold (report_render.summary_blocks, trim >= 2), and a Summary that silently
// loses "N newly reported since the previous assessment" hides the new problems it exists to show.
function estateComparisonBlocks(cmp, { factsFiles, mode, atLeast, T, blocks, scanId = null }) {
  const t = cmp.totals || {}
  const docs = [
    `${orNR(t.filesCompared)} compared with their own most recent earlier assessment`,
    t.filesNew ? `${t.filesNew} with no earlier assessment (new to the record)` : null,
    (t.filesNoBaseline || 0) - (t.filesNew || 0) > 0 ? `${t.filesNoBaseline - (t.filesNew || 0)} with no comparable earlier assessment` : null,
    t.filesBaselineUnusable ? `${t.filesBaselineUnusable} whose earlier assessment did not finish or used a different rubric or scope, so it is not a baseline` : null,
    t.filesNotComparable ? `${t.filesNotComparable} not fully assessed this time, so not compared` : null,
  ].filter(Boolean)
  if (cmp.status !== 'compared') {
    T(`Change since the previous assessment: not compared. ${cmp.reason || ''}`.trim(), { bold: true, color: AMBER_HEX })
    T(`Documents: ${docs.join('; ')}.`, { size: 9, color: MUTED_HEX })
  } else {
    const reopened = t.reopened != null ? `${t.reopened} reopened (reported again after being recorded as resolved)`
      : `reopened not recorded for ${t.filesReopenedUndetermined} document(s)${t.reopenedDetermined ? ` (${t.reopenedDetermined} reopened where it is recorded)` : ''}`
    T(`Across the documents compared: ${orNR(t.introduced)} newly reported finding(s), ${orNR(t.resolved)} no longer reported, ${orNR(t.persisting)} still present; ${reopened}.${t.notComparable ? ` ${t.notComparable} current finding(s) have no detector location, so they cannot be matched one by one and are counted as neither new nor resolved.` : ''} "No longer reported" means absent from the newer assessment — it is not a verified fix.`, { bold: true })
    T(`Documents: ${docs.join('; ')}.`, { size: 9, color: MUTED_HEX })
  }
  ;(cmp.notes || []).forEach((note) => T(note, { size: 9, color: MUTED_HEX }))
  if (!atLeast('reviewer')) return
  blocks.push({
    k: 'table',
    headers: ['Measure', 'Count'],
    caption: 'Change since each document’s previous assessment',
    rows: [
      ['Newly reported findings', orNR(t.introduced)],
      ['No longer reported', orNR(t.resolved)],
      ['Still present', orNR(t.persisting)],
      ['Reopened', t.reopened != null ? String(t.reopened) : `Not recorded (${orNR(t.filesReopenedUndetermined)} document(s) have no earlier per-finding ledger)`],
      ['Findings not matchable one by one (no location)', orNR(t.notComparable)],
      ['Documents compared', orNR(t.filesCompared)],
      ['Documents renamed since then (matched by provider id)', orNR(t.filesRenamed)],
      ['Documents with no earlier assessment', orNR(t.filesNew)],
      ['Documents with no comparable earlier assessment', orNR(t.filesNoBaseline != null && t.filesNew != null ? t.filesNoBaseline - t.filesNew : null)],
      ['Documents whose earlier assessment is not a usable baseline', orNR(t.filesBaselineUnusable)],
      ['Documents not fully assessed this time', orNR(t.filesNotComparable)],
    ],
  })
  if (!factsFiles) return
  const moved = factsFiles.filter((f) => {
    const c = f.comparison || {}
    return (c.status === 'compared' && ((c.introduced || 0) + (c.resolved || 0) + (c.reopened || 0) > 0))
      || c.status === 'baseline_unusable' || c.status === 'not_comparable'
  })
  const shown = mode === 'full' ? moved : moved.slice(0, REVIEWER_COMPARISON_ROWS)
  blocks.push({
    k: 'table',
    headers: ['Document', 'Since previous assessment', 'Previous assessment'],
    caption: 'Documents that changed, or could not be compared',
    rows: shown.map((f) => [docCell(f.file, scanId), rowComparisonText(f.comparison),
      f.comparison?.baseline ? `${orNR(f.comparison.baseline.scanId)} · ${orNR(f.comparison.baseline.generatedAt)}` : NR]),
  })
  if (shown.length < moved.length) T(`Showing ${shown.length} of ${moved.length} documents that changed or could not be compared; every one is in the Full evidence report.`, { bold: true })
}

// Gather every scan-scoped input, aggregate, build the model, then render the PDF server-side.
// Both the Overview toolbar and the Assess/Transparency RuleBreakdown header call this.
// `factsIndexComplete` / `factsIncompleteReason` are what Overview.jsx and Transparency.jsx pass
// from loadScanReportFacts (`got.complete`, `got.incompleteReason || got.factsError`). They were
// accepted and dropped (audit S2); the model now states them.
export async function generateScanReport({ scanId, files = [], org = 'your organisation', mode = 'summary', previous = null, scope, facts = null,
  factsIndexComplete = null, factsIncompleteReason = null, previousReason = null } = {}) {
  const [rowsRaw, hitlRaw, cfg, diffRaw] = await Promise.all([
    getScanTraces(scanId).catch(() => []),
    // null, not [] — a queue that could not be read is "not recorded", not "nothing pending".
    listHitlQueue(scanId).catch(() => null),
    getConfig().catch(() => null),
    getScanRemediationDiffs(scanId, true).catch(() => null),
  ])
  const data = aggregateScanReport({
    scanId, files, traces: rowsRaw, hitlItems: hitlRaw, cfg, diffSummary: diffRaw,
    targetLevel: assessLevel(scanId), org, previous, scope, facts,
  })
  const model = buildScanReportModel({ ...data, mode, factsIndexComplete, factsIncompleteReason, previousReason })
  const { renderReportPdf } = await import('./reportRenderClient.js')
  // The renderer's OWN outcome is returned, not discarded. It answers {format:'html'} when the
  // server PDF is unavailable, and a caller that only sees the model reports "PDF complete" for a
  // download that was an HTML fallback.
  const render = await renderReportPdf({ scanId, kind: 'scan', file: null, mode: model.mode, model })
  return { ...(render && typeof render === 'object' ? render : {}), model }
}
