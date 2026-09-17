import { useState, useEffect } from 'react'
import { openTraceUrl, getTraceStatus, getScanTraces, getScanReportFacts } from './api.js'
import SegmentDrawer from './SegmentDrawer.jsx'
import ReportModeMenu from './ReportModeMenu.jsx'
import FileDrawer from './FileDrawer.jsx'
import TracePanel from './TracePanel.jsx'
import SessionPanel from './SessionPanel.jsx'
import { WCAG } from './wcagCatalog.js'
import { allRules } from './rules/index.js'
import { DOCUMENTS_20 } from './documents20.js'
import { RULE_DETAILS, RULES_BY_SC, TRACKED_17 } from './ruleDetails.js'
import { OUT_OF_SCOPE_SCS, DENOMINATOR, outOfScopeNote, criteriaFor, CORE_SCS, SCOPE_SIZE, SCOPE_LABEL } from './activeScope.js'

// SCs a rule module exists for — distinguishes "check built, no data in this scan"
// from "no automated check exists yet".
const BUILT_SCS = new Set(allRules.map((r) => r.meta.id))

const LEVEL_RANK = { A: 1, AA: 2, AAA: 3 }
// The conformance level the user picked in the Assess runner — persisted per scan
// (AssessRunner's sessionStorage contract; this panel only renders once an
// assessment is 'done', so the key is always fresh by the time we read it).
const assessLevel = (scanId) => {
  try { return JSON.parse(sessionStorage.getItem(`acp-assess-${scanId || 'none'}`) || 'null')?.level || 'AA' } catch { return 'AA' }
}

// "📊 View trace" link. File-centric tracing (backend: lf.file_trace) — every file has
// its OWN Langfuse trace (Discover/Assess/Remediate as spans inside it), grouped into a
// session keyed by scan_id:
//   kind='file'    (scanId + file) — that ONE file's full lifecycle trace.
//   kind='session' (scanId only)   — the whole scan's session (every file's trace) — the
//                                     "view this scan" replacement for the old single
//                                     scan/assess/remediate trace.
// Routes through the backend redirect endpoint, which ensures a file trace exists before
// sending you to Langfuse (sessions never need this — they don't 404 when empty).
// Renders nothing when Langfuse isn't configured.
export function TraceChip({ scanId, kind = 'session', file = null, label = 'View trace', title, refreshKey = 0 }) {
  const url = openTraceUrl(scanId, kind, file)
  const [available, setAvailable] = useState(null)
  // Traces render IN-APP rather than sending the user out to Langfuse — its page hangs for
  // logged-out users on our self-hosted v3 and can't be iframed. A per-FILE chip opens the file
  // TracePanel; a whole-scan SESSION chip opens the SessionPanel (the aggregate that used to hang
  // Langfuse itself). Both report their own state, so neither is gated on the availability probe.
  const inAppFile = kind === 'file' && !!file
  const inAppSession = kind === 'session' && !!scanId
  const inApp = inAppFile || inAppSession
  const [panelOpen, setPanelOpen] = useState(false)
  useEffect(() => {
    if (!url || inApp) return   // in-app chips render unconditionally; the panel reports state
    let cancelled = false
    // refreshKey re-checks availability after an event that may have CREATED the
    // trace (e.g. remediate-now just wrote a Remediate span) — with retries to ride
    // out Langfuse's async ingestion, instead of the chip staying greyed until the
    // drawer is closed and reopened.
    const attempt = (retries) => getTraceStatus(scanId, kind, file)
      .then((r) => {
        if (cancelled) return
        if (r?.available) { setAvailable(true); return }
        if (retries > 0) setTimeout(() => { if (!cancelled) attempt(retries - 1) }, 2500)
        else setAvailable(false)
      })
      .catch(() => { if (!cancelled) setAvailable(false) })
    attempt(refreshKey > 0 ? 3 : 0)
    return () => { cancelled = true }
  }, [scanId, kind, file, url, refreshKey])

  if (!url) return null
  // In-app file traces show the button whenever tracing is configured (url != null) and open
  // the panel, which reports its own not-configured / pending / ok state. We deliberately do NOT
  // gate this on the availability probe: a file trace often doesn't exist until Assess runs, and
  // the panel's synchronous rebuild + honest "still recording" beats a permanently greyed chip.
  if (inApp) return (
    <>
      <button type="button" className="tracechip" onClick={() => setPanelOpen(true)}
              title={title || (inAppSession
                ? 'View this scan’s traces inside AccessOps (no Langfuse login)'
                : 'View this document’s trace inside AccessOps (no Langfuse login)')}>
        📊 {label}
      </button>
      {panelOpen && (inAppSession
        ? <SessionPanel scanId={scanId} onClose={() => setPanelOpen(false)} />
        : <TracePanel scanId={scanId} file={file} onClose={() => setPanelOpen(false)} />)}
    </>
  )
  if (available === false) return (
    <span className="tracechip tracechip--unavailable"
          title="Trace not available — this scan predates observability wiring">
      📊 {label}
    </span>
  )
  return (
    <a className="tracechip" href={url} target="_blank" rel="noopener noreferrer"
       title={title || "Open this step's trace in Langfuse (observability)"}>
      📊 {label}
    </a>
  )
}

// WCAG failure heatmap: top failing rules × department, so a reviewer sees at a glance
// WHERE the biggest problems concentrate, not just which rules fail most overall.
// Color intensity = fail count (darker = worse), same orange family as rb-fail.
function FailureHeatmap({ rows, files, topRules, onCellClick }) {
  const fileByName = {}
  ;(files || []).forEach((f) => { fileByName[f.file] = f })
  const deptOf = {}
  ;(files || []).forEach((f) => { deptOf[f.file] = f.department || f.dept || 'Unassigned' })
  if (!Object.keys(deptOf).length) return null   // no department data available — skip silently

  const ruleIds = new Set(topRules.map((r) => r.id))
  const depts = new Set()
  const cell = {}       // `${ruleId}::${dept}` -> fail count
  const cellFiles = {}  // `${ruleId}::${dept}` -> failing file objects
  rows.forEach((r) => {
    if (!ruleIds.has(r.rule_id) || String(r.outcome || '').toUpperCase() !== 'FAIL') return
    const d = deptOf[r.file] || 'Unassigned'
    depts.add(d)
    const k = `${r.rule_id}::${d}`
    cell[k] = (cell[k] || 0) + 1
    ;(cellFiles[k] ||= []).push(fileByName[r.file] || { file: r.file, score: null })
  })
  if (!depts.size) return null
  const deptList = [...depts].sort()
  const max = Math.max(1, ...Object.values(cell))

  return (
    <div className="heatmap" role="table" aria-label="WCAG failures by rule and department">
      <div className="heatrow heathdr" role="row">
        <span className="heatlabel" role="columnheader">Rule</span>
        {deptList.map((d) => <span className="heatcol" role="columnheader" key={d}>{d}</span>)}
      </div>
      {topRules.map((r) => (
        <div className="heatrow" role="row" key={r.id}>
          <span className="heatlabel" role="rowheader" title={r.name}><b>{r.id}</b> {r.name}</span>
          {deptList.map((d) => {
            const k = `${r.id}::${d}`
            const n = cell[k] || 0
            const alpha = n ? 0.15 + 0.85 * (n / max) : 0
            return (
              <button type="button" className="heatcell" role="cell" key={d} disabled={!n}
                    style={{ background: n ? `rgba(201,116,43,${alpha.toFixed(2)})` : 'transparent',
                             cursor: n ? 'pointer' : 'default' }}
                    title={n ? `${r.id} × ${d}: ${n} failing document${n === 1 ? '' : 's'} — click to view` : `${r.id} × ${d}: no failures`}
                    onClick={() => n && onCellClick(r, d, cellFiles[k])}>
                {n || ''}
              </button>
            )
          })}
        </div>
      ))}
    </div>
  )
}

// What ACP actually checks for a criterion, and what it does about a failure — the answer to
// "are we checking H1 and H2?", which the product could not previously give.
//
// The prose is GENERATED from docs/rules/*.md (scripts/gen_rule_details.py, CI-guarded), so it
// is the same text an engineer maintains beside the detector rather than a marketing paraphrase
// that drifts the first time a rule changes.
//
// `remediation` is null for the six rules whose doc has no "Fix mode rationale" section, and
// those render NOTHING rather than a plausible sentence. A panel that fabricates the fix story
// for a sixth of the catalog is worse than one that stays quiet: the reader cannot tell which
// half is real, so neither half can be trusted.
function RuleDetail({ sc }) {
  const ids = RULES_BY_SC[sc] || []
  if (!ids.length) return null
  return (
    <details className="ruledetail">
      <summary>what we check</summary>
      {ids.map((id) => {
        const d = RULE_DETAILS[id]
        if (!d) return null
        return (
          <div className="rd-rule" key={id}>
            <div className="rd-hd"><b>{id}</b> <span className="muted">· {d.title}</span>
              <span className="lvlpill">{d.severity}</span></div>
            <p className="rd-checks"><b>Assess:</b> {d.checks}</p>
            {d.remediation
              ? <p className="rd-fix"><b>Remediate ({d.fixMode}):</b> {d.remediation}</p>
              : <p className="rd-fix muted"><b>Remediate ({d.fixMode}):</b> not documented for this rule yet</p>}
          </div>
        )
      })}
    </details>
  )
}


// "By WCAG criterion" breakdown — aggregates the authoritative per-rule outcomes
// (PASS / FAIL / SKIP) the scanner recorded into scan_rule_traces, so users see exactly
// what each check did across the estate instead of only the summary tiles.

export function RuleBreakdown({ scanId, files }) {
  const [rows, setRows] = useState(null)
  // Default view = the criteria this engagement agreed to assess (14), not everything ACP
  // certifies against (the 20-check document core). `showAllCore` widens it back to the 20 and
  // is the panel's ONE expansion control: it also reveals the footnote saying what the assessed
  // conformance level and the document filter are leaving out, because those are the same
  // question ("what is not on this list, and why") asked about a wider ring.
  const [showAllCore, setShowAllCore] = useState(false)
  const [seg, setSeg] = useState(null)
  const [sel, setSel] = useState(null)
  // The 17 Mova iO tracks (column G), or all 20 document-core when widened. Defined once in
  // api/assessment_policy.py:MOVA_TRACKED and generated in — this file must never retype it,
  // because it is the fifth total this product quotes and the sixth would be unrecoverable.
  //
  // The agreed scope (14) is NOT a third view. It is what the backend GATES on, so it is already
  // reported by outOfScopeNote below; adding a third toggle state would ask the reader to hold
  // three denominators at once to answer one question.
  const criteria = showAllCore ? criteriaFor(true) : TRACKED_17
  // DENOMINATOR.scope names the AGREED scope (14). The narrowed view is the tracked 17 now, so
  // reusing it would print "of 17 criteria in the agreed scope" — a label describing one set
  // above a count of another, which is the reconcilability problem in miniature.
  const den = showAllCore
    ? DENOMINATOR.core
    : { total: criteria.size, noun: 'criteria tracked for this engagement',
        question: `what do we track? — the ${criteria.size} criteria Mova iO follows, of the ${CORE_SCS.size} document core` }
  const [hideNA, setHideNA] = useState(true)   // default to hiding the N/A rows; the toggle reveals them
  // Same server-facts path as Overview's scan report: the facts index is read to the end, the
  // recorded baseline supplies "since the previous assessment", and the renderer's own outcome is
  // returned so an HTML fallback is never reported as a PDF.
  const [factsProgress, setFactsProgress] = useState(null)
  const runScanReport = async (mode) => {
    const [{ loadScanReportFacts, scanComparisonFromFacts }, { generateScanReport }] = await Promise.all([
      import('./fileReportData.js'), import('./scanReport.js'),
    ])
    setFactsProgress({ loaded: 0, total: null, complete: false })
    try {
      const got = await loadScanReportFacts(scanId, { getScanReportFacts, onProgress: setFactsProgress })
      const cmp = scanComparisonFromFacts(got.facts, targetLevel)
      return await generateScanReport({
        scanId, files: files || [], mode,
        facts: got.facts, factsDigest: got.facts?.factsDigest ?? null,
        factsIndexComplete: got.complete, factsIncompleteReason: got.incompleteReason || got.factsError || null,
        previous: cmp.previous, previousReason: cmp.previousReason, scope: cmp.scope,
      })
    } finally { setFactsProgress(null) }
  }
  useEffect(() => {
    if (!scanId) { setRows(null); return }
    let cancelled = false
    getScanTraces(scanId)
      .then((r) => { if (!cancelled) setRows(Array.isArray(r) ? r : []) })
      .catch(() => { if (!cancelled) setRows([]) })
    return () => { cancelled = true }
  }, [scanId])
  if (rows && rows.length === 0) return null      // discover-only scan — no per-rule data

  const byRule = {}
  ;(rows || []).forEach((r) => {
    const k = r.rule_id
    if (!byRule[k]) byRule[k] = { id: r.rule_id, name: r.plain_name || r.rule_name, level: r.level, pass: 0, fail: 0, skip: 0, findings: 0 }
    const o = String(r.outcome || '').toUpperCase()
    if (o === 'PASS') byRule[k].pass++
    else if (o === 'FAIL') byRule[k].fail++
    else byRule[k].skip++
    byRule[k].findings += r.finding_count || 0
  })
  const rulesAll = Object.values(byRule).sort((a, b) => b.fail - a.fail || a.id.localeCompare(b.id))
  const targetLevel = assessLevel(scanId)
  const targetRank = LEVEL_RANK[targetLevel] || 2
  // Show only rules at/below the assessed level (no AAA above an AA target — e.g. 1.4.9), and,
  // by default, only the criteria in the agreed scope. A rule with no known level is kept.
  const rules = rulesAll.filter((r) =>
    (LEVEL_RANK[r.level] || 1) <= targetRank && criteria.has(r.id))
  if (!rules.length) return null
  // What the default narrowing costs, counted BEFORE it is applied. The backend gates on the
  // same scope inside `_rule_outcome`, so on a scan the operator actually ran scoped these are
  // all NOT_EVALUATED and this is 0. On an unscoped scan they are real recorded findings, and
  // narrowing the view would otherwise make them disappear with no account of themselves — the
  // same trust problem as a total that cannot be reconciled on screen (#77, #84). Reported in
  // the note below whether or not it flatters the score.
  //
  // WHICH set that is changed when the default became the tracked 17. It must be the criteria
  // THIS VIEW hides — the 3 untracked — not the 6 outside the agreed scope. Reporting the 6 while
  // rendering 17 rows would put a note on screen that cannot be reconciled against the rows above
  // it, which is the exact defect this panel shipped with (a "20 of 20" header above six rows)
  // and the reason ruleBreakdownScope14.test.jsx renders rather than reads the source.
  const untrackedScs = [...CORE_SCS].filter((sc) => !criteria.has(sc))
  const outOfScopeFindings = rulesAll
    .filter((r) => (LEVEL_RANK[r.level] || 1) <= targetRank && untrackedScs.includes(r.id))
    .reduce((n, r) => n + (r.findings || r.fail), 0)
  // A row is "N/A" when the criterion doesn't apply to this file type — the engine ran it but it
  // can't fire (no pass, no fail, only skip). The Hide-N/A toggle collapses those to the ones that
  // actually produced a result.
  const naCount = rules.filter((r) => r.pass === 0 && r.fail === 0).length
  const shown = hideNA ? rules.filter((r) => r.pass > 0 || r.fail > 0) : rules
  const fileCount = new Set((rows || []).map((r) => r.file)).size
  const failingRules = rules.filter((r) => r.fail > 0).slice(0, 8)

  // Checklist parity: every in-scope success criterion appears, not just the ones
  // the scanner automates. Scope = the assessed conformance level + the active criteria list
  // (the agreed scope by default, the whole document core when expanded).
  const inScope = WCAG.filter((c) => c.docApplies !== false && (LEVEL_RANK[c.level] || 3) <= targetRank && criteria.has(c.sc))
  const hiddenAboveLevel = WCAG.filter((c) => c.docApplies !== false && (LEVEL_RANK[c.level] || 3) > targetRank).length
  const hiddenWebOnly = WCAG.filter((c) => c.docApplies === false).length
  const evaluatedIds = new Set(rules.map((r) => r.id))
  // `rules` is already scoped to the assessed level (and the 20-core by default), so an
  // above-target criterion the scanner happens to check (e.g. 1.4.9 / 3.1.4 at an AA target)
  // never appears as a row here.
  const inScopeIds = new Set(inScope.map((c) => c.sc))
  const evaluatedInScope = rules.filter((r) => inScopeIds.has(r.id) || !WCAG.some((c) => c.sc === r.id)).length
  const unevaluated = inScope
    .filter((c) => !evaluatedIds.has(c.sc))
    .map((c) => ({
      ...c,
      isHuman: /Human/i.test(c.approach || '') || c.source === 'MDK HITL',
      isBuilt: BUILT_SCS.has(c.sc),
    }))
  const humanCount = unevaluated.filter((c) => c.isHuman).length
  const builtNoData = unevaluated.filter((c) => !c.isHuman && c.isBuilt).length

  // The header count is the number of rows actually rendered, and every criterion in the active
  // denominator that is NOT rendered is accounted for by a visible control or section:
  //
  //     rows shown  +  N/A hidden (the chip)  +  not evaluated (the section below)  =  inScope
  //
  // Stated as an identity because the failure it prevents is specific: the header used to read
  // "20 of 20 … automated" while as few as six rows were on screen, which is the same defect as
  // a dashboard total nobody can reconcile against the rows beneath it (#77, #84). The tooltip
  // spells the arithmetic out; the three visible numbers let a reader do it themselves.
  const naHidden = hideNA ? naCount : 0
  const reconciliation = `${shown.length} shown + ${naHidden} hidden as N/A + ${unevaluated.length} `
    + `not evaluated = ${inScope.length} ${den.noun} · ${evaluatedInScope} automated in this scan`
    + ` · denominator: ${den.question}`

  return (
    <section className="panel rulebreak">
      <div className="rubrichdr">
        <h2 style={{ margin: 0 }}>By WCAG criterion <span className="muted">· what each check found across {fileCount.toLocaleString()} documents</span></h2>
        <span className="muted" style={{ fontSize: 12 }} title={reconciliation}>Showing {shown.length} of {inScope.length} {den.noun} · target {targetLevel}
          <button className={`ghost small${hideNA ? " on" : ""}`} style={{ marginLeft: 8, fontSize: 11 }}
                  onClick={() => setHideNA((v) => !v)}
                  title="Hide the criteria that don't apply to this file type (N/A) \u2014 show only the ones with a pass or fail result">
            {hideNA ? `\u2713 Hiding ${naCount} N/A` : `Hide N/A (${naCount})`}
          </button>
          <span style={{ marginLeft: 8, fontSize: 11 }}>
            <ReportModeMenu label="Export scan report" disabled={!scanId}
                            progress={factsProgress && `Reading report evidence\u2026 ${factsProgress.loaded}${factsProgress.total != null ? ` of ${factsProgress.total}` : ''} document(s)`}
                            formats={[{ key: 'pdf', label: 'PDF', run: runScanReport }]} />
          </span></span>
      </div>
      <div className="rulerows">
        {shown.map((r) => {
          const total = r.pass + r.fail + r.skip || 1
          return (
            <div className="rulerow" key={r.id}>
              <div className="rulemeta"><b>{r.id}</b> <span className="lvlpill">{r.level}</span> <span>{r.name}</span></div>
              {/* OUTSIDE .rulemeta, deliberately. That element identifies the row's criterion, and
                  `.rulemeta b` is how callers read it — RuleDetail emits a <b> per rule doc, so
                  nesting it here made one row report 1 criterion plus 6 rule ids. Caught by
                  ruleBreakdownScope14.test.jsx counting 125 ids where 17 were expected; any other
                  consumer of that selector would have broken the same way, silently. */}
              <RuleDetail sc={r.id} />
              <div className="rulebar" title={`${r.pass} pass · ${r.fail} document(s) failed${r.findings > r.fail ? ` · ${r.findings} findings` : ''} · ${r.skip} N/A`}>
                <i className="rb-pass" style={{ width: `${(r.pass / total) * 100}%` }} />
                <i className="rb-fail" style={{ width: `${(r.fail / total) * 100}%` }} />
                <i className="rb-skip" style={{ width: `${(r.skip / total) * 100}%` }} />
              </div>
              <div className="rulecounts">
                <span className="rc-pass">{r.pass.toLocaleString()} pass</span>
                {r.fail ? (() => {
                  // Show the FINDING count, not the document count — one criterion can fail with
                  // several findings (e.g. two images missing alt), and those all count as blocking
                  // issues. `finding_count` is the per-rule finding total the scanner recorded.
                  const found = r.findings || r.fail
                  return <span className="rc-fail" title={`${r.fail.toLocaleString()} document(s) failed`}>{found.toLocaleString()} finding{found === 1 ? '' : 's'}</span>
                })() : null}
                {r.skip ? <span className="rc-skip">{r.skip.toLocaleString()} N/A</span> : null}
              </div>
            </div>
          )
        })}
      </div>
      {unevaluated.length > 0 && (
        <>
          <h3 className="rulesubhdr">Not evaluated in this scan <span className="muted">· {humanCount} need human / AT validation{unevaluated.length - humanCount - builtNoData > 0 ? ` · ${unevaluated.length - humanCount - builtNoData} automatable, not yet built` : ''}{builtNoData > 0 ? ` · ${builtNoData} built, no data this scan` : ''}</span></h3>
          <div className="rulerows">
            {unevaluated.map((c) => (
              <div className="rulerow rulerow--manual" key={c.sc}>
                <div className="rulemeta"><b>{c.sc}</b> <span className="lvlpill">{c.level}</span> <span title={c.name}>{c.name}</span></div>
                <div className="rulereq" title={c.req}>{c.req}</div>
                <span className={c.isHuman ? 'covchip covchip--human' : c.isBuilt ? 'covchip covchip--nodata' : 'covchip covchip--gap'}
                      title={c.isHuman ? 'Requires manual or assistive-technology validation — routed to the HITL queue, cannot be fully automated' : c.isBuilt ? 'An automated check exists but this scan recorded no result for it' : `Deterministic automation is possible (${c.approach}) but no check is built yet`}>
                  {c.isHuman ? 'Human / AT review' : c.isBuilt ? 'Built · no data' : 'Automatable · not built'}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
      {/* The scope expansion. Says how many criteria the default view leaves out, which ones, and
          why — a criterion that vanishes with no explanation is the same trust problem as an
          unreconcilable total, so the note renders whether or not the hidden criteria are
          carrying findings. `outOfScopeFindings` is 0 on a scan the operator ran scoped (the
          backend already reads those pairs as NOT_EVALUATED) and non-zero on an unscoped one,
          where the narrowing really is holding back recorded failures. */}
      {!showAllCore && (
        <p className="muted" style={{ fontSize: 11.5, margin: '10px 0 0' }}>
          {untrackedScs.length} of the {CORE_SCS.size} document-core criteria
          ({untrackedScs.sort().join(', ')}) are not tracked for this engagement and are not
          counted above — resize, reflow and text spacing are viewer behaviours rather than
          properties of a static document
          {outOfScopeFindings > 0
            ? `, including ${outOfScopeFindings} recorded finding${outOfScopeFindings === 1 ? '' : 's'}`
            : ''}.
        </p>
      )}
      {/* The agreed scope is a DIFFERENT fact from what this table shows, and it is the one the
          backend gates on — so it is stated separately rather than folded into the line above.
          Collapsing the two would make one number answer two questions. */}
      {!showAllCore && OUT_OF_SCOPE_SCS.size > 0 && (
        <p className="muted" style={{ fontSize: 11.5, margin: '4px 0 0' }}>
          Assessment itself is gated by the {SCOPE_LABEL}: {SCOPE_SIZE} of these criteria.
          {' '}{outOfScopeNote(0)}
        </p>
      )}
      <button className="ghost small" style={{ marginTop: 8 }} onClick={() => setShowAllCore((v) => !v)}
              title={showAllCore
                ? `Narrow back to the ${TRACKED_17.size} criteria tracked for this engagement`
                : `Widen to all ${DOCUMENTS_20.size} document-core criteria — the list ACP certifies against. The ${DOCUMENTS_20.size - TRACKED_17.size} extra (resize, reflow, text spacing) are viewer behaviours rather than properties of a static document, which is why they are not tracked`}>
        {showAllCore
          ? `Show only the ${TRACKED_17.size} tracked criteria`
          : `Show all ${DOCUMENTS_20.size} document-core criteria (+${DOCUMENTS_20.size - TRACKED_17.size} not tracked)`}
      </button>
      {showAllCore && (hiddenAboveLevel > 0 || hiddenWebOnly > 0) && (
        <p className="muted" style={{ fontSize: 11.5, margin: '8px 0 0' }}>
          Scoped to your assessment: {hiddenAboveLevel > 0 ? `${hiddenAboveLevel} criteria above Level ${targetLevel}` : ''}{hiddenAboveLevel > 0 && hiddenWebOnly > 0 ? ' and ' : ''}{hiddenWebOnly > 0 ? `${hiddenWebOnly} web-app-only criteria (not applicable to documents)` : ''} are not shown.
        </p>
      )}
      {failingRules.length > 0 && (
        <>
          <h3 className="heatmaptitle">Where failures concentrate <span className="muted">· top {failingRules.length} failing criteria by department, within the {inScope.length} {den.noun}</span></h3>
          <FailureHeatmap rows={rows || []} files={files} topRules={failingRules}
                           onCellClick={(rule, dept, cellFiles) => setSeg({
                             title: `${rule.id} · ${dept}`,
                             subtitle: `${cellFiles.length} document${cellFiles.length === 1 ? '' : 's'} failing "${rule.name}"`,
                             files: cellFiles,
                           })} />
        </>
      )}
      {seg && <SegmentDrawer title={seg.title} subtitle={seg.subtitle} files={seg.files} onClose={() => setSeg(null)} onPickFile={(f) => { setSeg(null); setSel(f) }} />}
      {sel && <FileDrawer file={sel} scanId={scanId} onClose={() => setSel(null)} />}
    </section>
  )
}
