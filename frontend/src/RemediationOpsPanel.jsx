import ActivityEventDetails from './ActivityEventDetails.jsx'
import { isRemediationActivity, remediationStep } from './processingActivity.js'
import useRemediationFreshness from './useRemediationFreshness.js'
import { useEffect, useLayoutEffect, useRef, useState, useId } from 'react'
import useConfirmedRemediationActivity from './useConfirmedRemediationActivity.js'
import LiveCounter from './LiveCounter.jsx'
import PlannedRemediationWaterfall from './PlannedRemediationWaterfall.jsx'
import RemediationWaterfallCard from './RemediationWaterfallCard.jsx'
import RemediationThroughput from './RemediationThroughput.jsx'
import { counterRows, secondaryRows, freshness, headline, integrityAffects, partitionSums } from './remediationSnapshot.js'
import { attemptStage, milestoneCrossings, retrySeconds } from './remediationLivePanel.js'
import ActivityPulse from './ActivityPulse.jsx'
import { activityGroups } from './remediationEventFeed.js'
import RemainingWorkStatus from './RemainingWorkStatus.jsx'
import RemediationExceptions, { useRemediationExceptions, exceptionCount } from './RemediationExceptions.jsx'
import { getFindingDispositions, getRemediationActivityEvidence, downloadRemediationActivitySavedCopy, downloadRemediationActivityEvidence } from './api.js'
import './remediation-ops-panel.css'
import './remediation-live-detail.css'
import './remediation-reconciliation.css'

const PHASE_TEXT = { pending: 'Pending', active: 'In progress', completed: 'Completed', completed_with_exceptions: 'Completed with exceptions', failed: 'Failed', skipped: 'Skipped' }
const POSITIVE = new Set(['fixesApplied', 'fixesVerified', 'documentsVerified', 'delivered'])
const COMPACT_QUERY = '(max-width: 760px)'

function useCompactLayout() {
  const query = () => typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia(COMPACT_QUERY) : null
  const [compact, setCompact] = useState(() => query()?.matches || false)
  useEffect(() => {
    const media = query()
    if (!media) return undefined
    const change = (event) => setCompact(event.matches)
    setCompact(media.matches)
    media.addEventListener?.('change', change)
    return () => media.removeEventListener?.('change', change)
  }, [])
  return compact
}

function Disclosure({ title, compact, children, defaultExpanded = false }) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  return <details className={`remops-disclosure${compact ? ' remops-disclosure-compact' : ''}`}
    open={!compact || expanded}
    onToggle={(event) => { if (compact) setExpanded(event.currentTarget.open) }}>
    <summary>{title}</summary>
    <div className="remops-disclosure-body">{children}</div>
  </details>
}
function ago(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return null
  if (seconds < 60) return `${Math.round(seconds)}s`
  const minutes = Math.floor(seconds / 60)
  return minutes < 60 ? `${minutes}m ${Math.round(seconds % 60)}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

function FreshnessBadge({ state, updateMode }) {
  const polling = updateMode === 'polling' && state.level === 'reconnecting'
  const label = polling ? 'Updating by polling' : state.label
  return <span className={`remops-fresh remops-fresh-${polling ? 'polling' : state.level}`} role="status" title={state.detail}>
    <span aria-hidden="true">{state.level === 'stalled' ? '■' : polling ? '↻' : state.level === 'live' ? '●' : '◐'}</span>
    {label}{state.ageS !== null && state.level !== 'live' ? ` · last update ${state.ageS}s ago` : ''}
  </span>
}

function Progress({ snapshot, suspect }) {
  const rows = counterRows(snapshot)
  if (!rows) return null
  const total = snapshot.total_documents
  const known = typeof total === 'number' && total > 0 && rows.every((r) => typeof r.value === 'number')
  // "Completed" is intentionally the successful-correction bucket. It must stay separate from
  // review, failure and no-eligible-fix outcomes in the reconciled partition, but it is the wrong
  // numerator for the large RUN-COMPLETENESS headline: a healthy SharePoint batch can inspect
  // dozens of documents, place each in Skipped because there was no approved automatic fix, and
  // leave that headline frozen at 0. Count every terminal outcome here; the six buckets below
  // still say exactly how those documents ended.
  //
  // WHAT IT IS CALLED MATTERS AS MUCH AS WHAT IT COUNTS. This read "N documents processed" and a
  // real run rendered "12 of 70 documents processed" with `completed` at ZERO — all twelve routed
  // to review or skipped. The reasoning above is why the numerator stays; "processed" is what a
  // reader takes for "done", which is the one thing this number does not promise.
  const throughAutomatic = known
    ? rows.filter((row) => !['processing', 'waiting'].includes(row.key))
      .reduce((sum, row) => sum + row.value, 0)
    : null
  return <section className="remops-progress" aria-labelledby="remops-progress-title">
    <div className="remops-progress-head"><strong id="remops-progress-title">{throughAutomatic == null ? 'Document progress unavailable' : <><LiveCounter value={throughAutomatic} /> of {total.toLocaleString()} documents through automatic processing</>}</strong><span className="muted">{snapshot.estimate?.available ? `Estimated ${snapshot.estimate.label || 'range available'}` : 'Estimating after the first results'}</span></div>
    {known && <div className="remops-segments" aria-label={`${total} documents: ${rows.map((r) => `${r.value} ${r.label.toLowerCase()}`).join(', ')}`}>{rows.filter((r) => r.value > 0).map((row) => <span key={row.key} tabIndex="0" role="img" aria-label={`${row.label}: ${row.value}`} className={`remops-segment remops-segment-${row.key}`} style={{ width: `${row.value / total * 100}%` }} data-detail={`${row.label}: ${row.value.toLocaleString()}`} />)}</div>}
    <dl className={`remops-counts${suspect ? ' remops-suspect' : ''}`}>{rows.map((row) => <div key={row.key} title={row.definition}><dt>{row.label}</dt><dd data-testid={`rem-count-${row.key}`}>{row.value == null ? '—' : row.key === 'completed' ? <LiveCounter value={row.value} /> : row.value.toLocaleString()}</dd></div>)}</dl>
    {partitionSums(snapshot) === false && <p className="remops-error">These counters do not add up to the documents in scope. ACP is reconciling them.</p>}
  </section>
}

function Pipeline({ phases = [], attempts = [], moving = false }) {
  if (!phases.length) return null
  return <section className={`remops-pipeline${moving ? ' remops-pipeline-moving' : ''}`}><h3>Active document pipeline</h3><ol aria-label="Remediation phases">{phases.map((phase, index) => { const documents = attempts.filter((attempt) => attemptStage(attempt.phase) === phase.key).slice(0, 2); const phaseState = phase.key === 'preparing' && phase.status === 'completed' ? 'Scope ready' : (PHASE_TEXT[phase.status] || phase.status); return <li key={phase.key} className={`remops-phase remops-phase-${phase.status}`}><span className="remops-phase-mark" aria-hidden="true">{phase.status === 'active' ? '●' : phase.status === 'failed' ? '×' : phase.status.startsWith('completed') ? '✓' : '○'}</span><span className="remops-phase-name">{phase.label}</span><span className="remops-phase-state">{phaseState}{phase.detail ? ` · ${phase.detail}` : ''}</span>{documents.length > 0 && <span className="remops-phase-docs">{documents.map((document) => <span key={`${document.file}-${phase.key}`} title={document.file}>{document.file}</span>)}</span>}{index < phases.length - 1 && <span className="remops-flow" aria-hidden="true">···►</span>}</li> })}</ol></section>
}

function ProgressCue({ snapshot, onViewMonitor }) {
  const age = snapshot.progress?.material_age_s
  const delayedAfter = snapshot.thresholds?.delayed_after_s ?? 60
  const stallAfter = snapshot.thresholds?.stall_after_s ?? 900
  const processing = snapshot.documents?.processing || 0
  if (snapshot.state === 'stalled') return <div className="remops-progress-cue remops-progress-cue-stalled" role="alert"><b>No durable progress is being recorded.</b>{age != null ? ` Last progress was ${ago(age)} ago.` : ''}{onViewMonitor && <button type="button" className="linklike" onClick={onViewMonitor}>Inspect workers and retries →</button>}</div>
  // A current lease/heartbeat is positive evidence that a large document is still running.
  // Give healthy work a five-minute checkpoint window; the one-minute transport threshold is
  // for a quiet or unhealthy run and produced noisy amber warnings during normal PDF work.
  const healthyLease = snapshot.progress?.lease_healthy === true
  const warnAfter = healthyLease ? Math.max(delayedAfter, Math.min(300, stallAfter / 3)) : delayedAfter
  if (processing > 0 && typeof age === 'number' && age > warnAfter) return <div className="remops-progress-cue" role="status"><b>{processing} worker attempt{processing === 1 ? '' : 's'} active; checkpoint delayed.</b> No durable progress for {ago(age)}. ACP continues watching and will flag a stall after {ago(stallAfter)}.{onViewMonitor && <button type="button" className="linklike" onClick={onViewMonitor}>Check Live Operations →</button>}</div>
  return null
}

function Workstream({ attempts = [], generatedAt, compact = false }) {
  if (!attempts.length) return null
  const now = generatedAt ? Date.parse(generatedAt) : null
  const shown = attempts.slice(0, compact ? 2 : 3)
  const hiddenCount = attempts.length - shown.length
  return <section className="remops-work"><h3>In flight now <span>· {attempts.length} document{attempts.length === 1 ? '' : 's'}</span></h3><ul>{shown.map((a) => { const signal = now && a.progress_at ? (now - Date.parse(a.progress_at)) / 1000 : null; const trail = Array.isArray(a.trail) ? a.trail : []; return <li key={`${a.file}-${a.started_at || ''}`}><div className="remops-doc-head"><strong><span aria-hidden="true">●</span> <span className="fname">{a.file}</span></strong><span>{ago(a.elapsed_s) ? `in flight ${ago(a.elapsed_s)}` : ''}</span></div><div className="remops-trail"><span className="remops-done">✓ Opened</span>{trail.map((step, index) => <span className="remops-trail-step" key={`${step.label || step}-${index}`}><span aria-hidden="true">→</span><span className="remops-done">✓ {step.label || step}</span></span>)}<span aria-hidden="true">→</span><span className="remops-active">● {a.phase || 'Processing'}</span>{a.attempt > 1 && <span>attempt {a.attempt}</span>}{ago(signal) && <span>last signal {ago(signal)} ago</span>}</div></li> })}</ul>{hiddenCount > 0 && <p className="muted">and {hiddenCount} more document{hiddenCount === 1 ? '' : 's'} in flight</p>}</section>
}

function RetryNotice({ retryAt, now }) {
  const seconds = retrySeconds(retryAt, now)
  if (seconds === null) return null
  return <div className="remops-retry" role="status"><span aria-hidden="true">↻</span> Temporary issue · {seconds > 0 ? `retry in ${seconds}s` : 'retry due now'}</div>
}

function RecoveryNotice({ recovery = {} }) {
  const reclaimed = Number(recovery.worker_reclaimed) || 0
  const retries = Number(recovery.retry_scheduled) || 0
  if (!reclaimed && !retries) return null
  return <div className="remops-recovery" role="status">
    {reclaimed > 0 && <span><b>{reclaimed} document{reclaimed === 1 ? '' : 's'} safely queued after a worker interruption.</b> No action is needed; ACP will resume the work.</span>}
    {retries > 0 && <span><b>{retries} document{retries === 1 ? '' : 's'} waiting after a processing error.</b> The scheduled retry remains active.</span>}
  </div>
}

function Milestones({ notices, onDismiss }) {
  return notices.length ? <aside className="remops-milestones" aria-label="Completion milestones">{notices.map((notice) => <div key={notice.key}><span><span aria-hidden="true">✓</span> {notice.text}</span><button type="button" aria-label={`Dismiss ${notice.text}`} onClick={() => onDismiss(notice.key)}>×</button></div>)}</aside> : null
}

function Throughput({ snapshot, frozen = false }) {
  const latest = snapshot.throughput || {}
  const latestRef = useRef(latest)
  latestRef.current = latest
  const [data, setData] = useState(latest)
  useEffect(() => setData(latestRef.current), [snapshot.run_id, snapshot.batch_id])
  useEffect(() => {
    if (frozen) return undefined
    const timer = setInterval(() => setData(latestRef.current), 12_000)
    return () => clearInterval(timer)
  }, [frozen])
  const processing = snapshot.documents?.processing || 0
  return <section className="remops-throughput"><h3>Throughput <span>· last 5 minutes</span></h3>{typeof data.documents_per_minute === 'number' ? <><RemediationThroughput data={data} identity={`${snapshot.run_id}:${snapshot.batch_id}`} paused={frozen} /><p>{data.change_percent != null && data.sample_documents >= 5 && <span className="remops-rate"> {data.change_percent >= 0 ? '↑' : '↓'} {Math.abs(data.change_percent)}% over previous 5 minutes</span>}</p></> : <p className="muted">No document was processed in the last five minutes.{processing ? ` ${processing} ${processing === 1 ? 'is' : 'are'} actively processing; rate and ETA will appear after terminal outcomes.` : ' Rate and ETA will appear after terminal outcomes.'}</p>}</section>
}

function Secondary({ snapshot }) {
  const rows = secondaryRows(snapshot)
  return rows.length ? <dl className="remops-secondary">{rows.map((row) => <div key={row.key}><dt>{row.label}</dt><dd>{POSITIVE.has(row.key) ? <LiveCounter value={row.value} /> : row.value.toLocaleString()}</dd></div>)}</dl> : null
}

function FindingReconciliation({ snapshot }) {
  const [drilldown, setDrilldown] = useState(null)
  const [drilldownError, setDrilldownError] = useState(null)
  const [drilldownLoading, setDrilldownLoading] = useState(false)
  const reconciliation = snapshot.finding_reconciliation
  if (!reconciliation) return null
  const assessed = reconciliation.assessed
  const reviewFindings = reconciliation.awaiting_review
  const reviewItems = snapshot.review?.items
  const verifiedChanges = snapshot.fixes?.verified
  const documents = snapshot.documents || {}
  const processedDocuments = ['completed', 'review', 'failed', 'skipped']
    .reduce((sum, key) => sum + (typeof documents[key] === 'number' ? documents[key] : 0), 0)
  const count = (value) => typeof value === 'number' ? value.toLocaleString() : 'Not yet available'
  const inconsistent = snapshot.integrity?.affected?.includes('finding_reconciliation')
    || (reconciliation.violations || []).length > 0
  const exact = reconciliation.exact === true && !inconsistent
  const outcomes = [
    ['Verified resolved', reconciliation.resolved_verified, 'resolved_verified'],
    ['Awaiting human review', reconciliation.awaiting_review, 'awaiting_review'],
    ['Approved, awaiting verification', reconciliation.approved_pending_verification, 'approved_pending_verification'],
    ['Unchanged — no eligible fix', reconciliation.unchanged_no_fix, 'unchanged_no_fix'],
    ['Failed remediation', reconciliation.failed, 'remediation_failed'],
    ['Excluded by policy', reconciliation.excluded, 'excluded_by_policy'],
    ['Superseded by reassessment', reconciliation.superseded, 'superseded_by_reassessment'],
  ]
  const openDrilldown = async (label, disposition) => {
    setDrilldownLoading(true)
    setDrilldownError(null)
    try {
      const result = await getFindingDispositions(snapshot.scan_id || snapshot.run_id, disposition)
      setDrilldown({ label, items: result.items || [], batchId: result.batch_id })
    } catch (error) {
      setDrilldownError(String(error?.message || error))
    } finally {
      setDrilldownLoading(false)
    }
  }
  return <section className="remops-reconciliation" aria-labelledby="remops-reconciliation-title">
    <div><h3 id="remops-reconciliation-title">Assessment → Remediation accounting</h3><p className="muted">The units stay separate so completed processing is not mistaken for resolved findings.</p></div>
    {inconsistent && <p className="remops-accounting-note" role="status"><b>Accounting temporarily inconsistent.</b> ACP is preserving the last durable finding totals while it reconciles this snapshot.</p>}
    {exact ? <>
      <div aria-labelledby="remops-finding-outcomes"><h4 id="remops-finding-outcomes">Finding outcomes</h4><dl>
        <div><dt>Assessment findings</dt><dd>{count(assessed)} findings<span>Finding instances handed into this workflow</span></dd></div>
        {outcomes.map(([label, value, disposition]) => <div key={label}><dt>{label}</dt><dd>{count(value)} findings{value > 0 && <button type="button" className="remops-finding-details" onClick={() => openDrilldown(label, disposition)}>View affected documents<span className="sr-only"> for {label.toLowerCase()}</span></button>}</dd></div>)}
        <div><dt>Findings accounted for</dt><dd>{count(reconciliation.accounted)} / {count(assessed)} findings<span>Every assessed finding has one current disposition</span></dd></div>
      </dl></div>
      <div aria-labelledby="remops-document-outcomes"><h4 id="remops-document-outcomes">Document outcomes</h4><dl>
        <div><dt>Documents processed</dt><dd>{processedDocuments.toLocaleString()} / {typeof snapshot.total_documents === 'number' ? snapshot.total_documents.toLocaleString() : 'Not yet available'} documents<span>Files that reached a terminal remediation outcome</span></dd></div>
        <div><dt>Pending human review</dt><dd>{count(reviewFindings)} findings<span>{typeof reviewItems === 'number' ? `Across ${reviewItems.toLocaleString()} review card${reviewItems === 1 ? '' : 's'}` : 'Review-card count not yet available'}</span></dd></div>
      </dl></div>
      <div aria-labelledby="remops-change-evidence"><h4 id="remops-change-evidence">Change evidence</h4><dl>
        <div><dt>Verified changes</dt><dd>{count(verifiedChanges)} changes<span>Before/after changes that passed re-check</span></dd></div>
      </dl></div>
      {drilldownLoading && <p role="status">Loading finding evidence…</p>}
      {drilldownError && <p role="alert">Finding evidence is unavailable: {drilldownError}</p>}
      {drilldown && !drilldownLoading && <section className="remops-finding-drilldown" aria-labelledby="remops-finding-drilldown-title">
        <div><h4 id="remops-finding-drilldown-title">{drilldown.label}: affected documents</h4><button type="button" onClick={() => setDrilldown(null)}>Close details</button></div>
        <p className="muted">Current remediation batch {drilldown.batchId || 'not available'} · {drilldown.items.length.toLocaleString()} finding{drilldown.items.length === 1 ? '' : 's'}</p>
        {drilldown.items.length ? <table><caption className="sr-only">Finding evidence for {drilldown.label.toLowerCase()}</caption><thead><tr><th scope="col">Document</th><th scope="col">Criterion</th><th scope="col">Instance</th><th scope="col">Evidence</th></tr></thead><tbody>{drilldown.items.map((item) => <tr key={item.finding_id}><th scope="row">{item.file || item.document_id}</th><td>{item.rule_id}</td><td>{item.instance_key}</td><td>{item.fix_evidence_ids?.length ? `${item.fix_evidence_ids.length} linked record${item.fix_evidence_ids.length === 1 ? '' : 's'}` : item.review_item_id ? 'Human review linked' : 'No linked evidence'}</td></tr>)}</tbody></table> : <p>No current findings are in this disposition.</p>}
      </section>}
    </> : <>
      <dl>
        <div><dt>Assessment findings</dt><dd>{count(assessed)}{typeof assessed === 'number' && ' findings'}<span>Finding instances handed into this workflow</span></dd></div>
        <div><dt>Documents processed</dt><dd>{processedDocuments.toLocaleString()} / {typeof snapshot.total_documents === 'number' ? snapshot.total_documents.toLocaleString() : 'Not yet available'} documents<span>Files that reached a terminal remediation outcome</span></dd></div>
        <div><dt>Verified changes</dt><dd>{count(verifiedChanges)}{typeof verifiedChanges === 'number' && ' changes'}<span>Before/after changes that passed re-check</span></dd></div>
        <div><dt>Pending human review</dt><dd>{count(reviewFindings)}{typeof reviewFindings === 'number' && ' findings'}<span>{typeof reviewItems === 'number' ? `Across ${reviewItems.toLocaleString()} review card${reviewItems === 1 ? '' : 's'}` : 'Review-card count not yet available'}</span></dd></div>
        <div><dt>Exact finding disposition</dt><dd>Not yet available<span>A complete finding-disposition ledger is not yet available</span></dd></div>
      </dl>
      {!inconsistent && <p className="remops-accounting-note">These values do not form a subtraction. One finding may require several verified changes, and one review item may group several findings. ACP does not yet track an exact disposition for every finding.</p>}
    </>}
  </section>
}

function ActivityLine({ line = '' }) {
  const match = /^(.*) · (source delivery is (?:unavailable|disabled))$/.exec(line)
  return match ? <>{match[1]} <span className="remops-delivery-tag">{match[2]}</span></> : line
}

export function Activity({ scanId = null, events = [], status = 'ready', terminal = false, compact = false }) {
  const listRef = useRef(null)
  const anchorRef = useRef(null)
  const listId = useId()
  const [grouped, setGrouped] = useState(false)
  const [scroll, setScroll] = useState({top: 0, max: 0})
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [outcome, setOutcome] = useState('all')
  const [zone, setZone] = useState('all')
  const filtered = events.filter(event => (!search.trim() || `${event.documentName || ''} ${event.line || ''} ${event.activityDetails?.criterion || ''} ${event.activityDetails?.location || ''}`.toLowerCase().includes(search.trim().toLowerCase()))
    && (outcome === 'all' || (outcome === 'attention' && ['attention', 'error'].includes(event.tone))
      || (outcome === 'verified' && event.kind === 'remediate.verified')
      || (outcome === 'saved' && ['remediate.fix_applied', 'remediate.delivered', 'remediate.delivery_failed'].includes(event.kind))
      || (outcome === 'ai' && event.kind?.startsWith('remediate.ai_request_')))
    && (zone === 'all' || event.processingZone === zone))
  const filtering = !!search.trim() || outcome !== 'all' || zone !== 'all'
  const groups = grouped ? activityGroups(filtered) : filtered.map(event => ({key: event.key, lead: event, rows: [event]}))
  const rememberScroll = () => {
    const list = listRef.current
    if (!list) return
    const top = list.getBoundingClientRect().top
    const first = [...list.children].find(row => row.getBoundingClientRect().bottom > top)
    anchorRef.current = list.scrollTop > 0 && first ? {key: first.dataset.activityKey, offset: first.getBoundingClientRect().top - top} : null
    setScroll({top: list.scrollTop, max: Math.max(0, list.scrollHeight - list.clientHeight)})
  }
  const restoreScroll = () => {
    const list = listRef.current
    if (!list) return
    const anchor = anchorRef.current
    const row = anchor && [...list.children].find(item => item.dataset.activityKey === anchor.key)
    if (row) list.scrollTop += row.getBoundingClientRect().top - list.getBoundingClientRect().top - anchor.offset
    rememberScroll()
  }
  useLayoutEffect(restoreScroll, [events, grouped, search, outcome, zone])
  useEffect(() => {
    if (typeof ResizeObserver === 'undefined' || !listRef.current) return undefined
    const observer = new ResizeObserver(restoreScroll)
    observer.observe(listRef.current)
    for (const row of listRef.current.children) observer.observe(row)
    return () => observer.disconnect()
  }, [events, grouped, search, outcome, zone])
  const seen = useRef(events.length || status !== 'loading' ? new Set(events.map(event => event.key)) : null)
  const [fresh, setFresh] = useState(new Set())
  const timer = useRef(null)
  useEffect(() => {
    if (!seen.current) {
      if (events.length || status === 'ready') seen.current = new Set(events.map(event => event.key))
      return
    }
    const added = events.filter(event => !seen.current.has(event.key)).map(event => event.key)
    events.forEach(event => seen.current.add(event.key))
    if (!added.length) return
    clearTimeout(timer.current)
    setFresh(new Set(added))
    timer.current = setTimeout(() => setFresh(new Set()), 1500)
  }, [events, status])
  useEffect(() => () => clearTimeout(timer.current), [])
  return <section className="remops-activity">{!compact && <h3>Live activity</h3>}{events.length ? <><div className="remops-history-tools"><span>{filtering ? `${filtered.length.toLocaleString()} of ` : ''}{events.length.toLocaleString()} recorded updates · Newest first</span><button type="button" aria-expanded={filtersOpen} onClick={() => setFiltersOpen(value => !value)}>Filter activity{filtering ? ' •' : ''}</button><button type="button" aria-pressed={grouped} onClick={() => {anchorRef.current = null; setGrouped(value => !value)}}>{grouped ? "Show all activity" : "Group by document"}</button><button type="button" onClick={() => {if (listRef.current) listRef.current.scrollTop = 0; rememberScroll()}}>Latest activity ↑</button></div>{(filtersOpen || filtering) && <div className="remops-history-filters"><label><span className="sr-only">Search activity</span><input type="search" placeholder="Find document, criterion, or activity…" value={search} onChange={event => {anchorRef.current = null; setSearch(event.target.value)}} /></label><label><span className="sr-only">Activity outcome</span><select value={outcome} onChange={event => {anchorRef.current = null; setOutcome(event.target.value)}}><option value="all">All outcomes</option><option value="attention">Needs attention</option><option value="verified">Verified fixes</option><option value="saved">Saved copies</option><option value="ai">AI requests</option></select></label><label><span className="sr-only">AI processing location</span><select value={zone} onChange={event => {anchorRef.current = null; setZone(event.target.value)}}><option value="all">All AI locations</option><option value="local">Local AI</option><option value="cloud">Cloud AI</option><option value="tenant">Tenant AI</option></select></label>{filtering && <button type="button" onClick={() => {anchorRef.current = null; setSearch(''); setOutcome('all'); setZone('all')}}>Clear filters</button>}</div>}{filtering && !filtered.length && <p role="status">No recorded activity matches these filters.</p>}<div className="remops-history-scroll"><ol id={listId} ref={listRef} onScroll={rememberScroll} onToggle={rememberScroll} aria-label="Recent remediation activity" tabIndex={0}>{groups.map((group) => { const event = group.lead; const retry = ['scan.retrying', 'scan.interrupted', 'remediate.delivery_retry_requested', 'remediate.vision_retry_pending'].includes(event.kind); return <li key={group.key} data-activity-key={group.key} data-activity-stage={event.activityStage || undefined} className={`remops-activity-${event.tone}${retry ? ' remops-activity-retry' : ''}${fresh.has(event.key) ? ' remops-activity-fresh' : ''}`}><div className="remops-activity-event"><time dateTime={event.occurredAt || undefined}>{event.occurredAt ? new Date(event.occurredAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : 'Time unavailable'}</time><span aria-hidden="true">{event.kind === 'remediate.accepted' ? <svg data-activity-icon="accepted" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M7 3h10M7 7h10M4 11h5l2 3h2l2-3h5v9H4z" /></svg> : event.kind === 'remediate.fix_applied' || (event.kind === 'remediate.delivery_failed' && event.tone === 'neutral') ? <svg data-activity-icon="saved-copy" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6M8 15l3 3 5-5" /></svg> : event.kind === 'remediate.vision_retry_recovered' ? <svg data-activity-icon="image-description" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 15l5-5 5 5 3-3 5 5M7 8h1" /></svg> : event.kind?.startsWith('remediate.ai_request_') ? <svg data-activity-icon="ai-request" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M4 8h14M14 4l4 4-4 4M20 16H6M10 12l-4 4 4 4" /></svg> : retry ? '↻' : event.tone === 'error' ? '×' : event.tone === 'attention' ? '!' : event.tone === 'success' ? '✓' : <svg data-activity-icon="activity-record" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 12h8M8 16h6" /></svg>}</span><span><ActivityLine line={event.line} /></span></div><ActivityEventDetails event={event} loadEvidence={scanId ? seq => getRemediationActivityEvidence(scanId, seq) : undefined} downloadSavedCopy={scanId ? seq => downloadRemediationActivitySavedCopy(scanId, seq) : undefined} downloadEvidence={scanId ? seq => downloadRemediationActivityEvidence(scanId, seq) : undefined} />{group.rows.length > 1 && <details><summary>{group.rows.length - 1} other updates for this document</summary><ul>{group.rows.filter(row => row !== event).map(row => <li key={row.key}><ActivityLine line={row.line} /></li>)}</ul></details>}</li> })}</ol><input className="remops-history-scrollbar" type="range" min="0" max={Math.max(1, scroll.max)} value={scroll.top} aria-label="Scroll activity history" aria-controls={listId} aria-valuetext={`${Math.round(scroll.top)} of ${Math.round(scroll.max)} pixels`} onChange={event => {if (listRef.current) listRef.current.scrollTop = Number(event.target.value); rememberScroll()}} /></div></> : <p className="muted">{status === 'loading' ? 'Loading saved activity…' : status === 'unavailable' ? 'Saved activity could not be loaded. Updates will retry automatically.' : terminal ? 'No recent remediation activity is recorded for this run.' : 'No recent remediation activity is recorded yet. New updates appear as work is saved.'}</p>}</section>
}

// The stub this replaces summed four numbers into "Needs attention · N" and offered nothing to do
// about any of them — and one of its four, `snapshot.delivery.failures`, is a field the snapshot
// has never carried, so the delivery group silently never rendered at all. Region E is now served
// by its own endpoint, which groups the exceptions BY RESPONSE and decides on the server which of
// them ACP may act on. See RemediationExceptions.jsx.

export default function RemediationOpsPanel({ snapshot = null, connected = false, receivedAt = null, events = [], activityStatus = 'ready', updateMode = 'idle', onViewMonitor = null, compactLayout = null, exceptions = null, assessmentContext = null, streamlined = false, hideActivity = false }) {
  const fresh = useRemediationFreshness({ snapshot, connected, receivedAt })
  const activityConfirmed = useConfirmedRemediationActivity(snapshot)
  const [paused, setPaused] = useState(false)
  const [hidden, setHidden] = useState(() => typeof document !== 'undefined' && document.hidden)
  const [clock, setClock] = useState(() => Date.now())
  const [milestones, setMilestones] = useState([])
  const previousSnapshot = useRef(null)
  const detectedCompact = useCompactLayout()
  const compact = compactLayout == null ? detectedCompact : compactLayout
  useEffect(() => { if (typeof document === 'undefined') return undefined; const change = () => setHidden(document.hidden); document.addEventListener('visibilitychange', change); return () => document.removeEventListener('visibilitychange', change) }, [])
  useEffect(() => { if (paused || hidden || !snapshot?.retry_at) return undefined; const timer = setInterval(() => setClock(Date.now()), 1_000); return () => clearInterval(timer) }, [paused, hidden, snapshot?.retry_at])
  useEffect(() => { const previous = previousSnapshot.current; previousSnapshot.current = snapshot; const crossed = milestoneCrossings(previous, snapshot); if (crossed.length) setMilestones((current) => [...current, ...crossed.filter((next) => !current.some((item) => item.key === next.key))]) }, [snapshot])
  // Above the early return, because hooks are not conditional. It follows the snapshot's own
  // revision rather than opening a second stream — one connection, and the exception set is
  // re-read only when the run has actually moved.
  // `exceptions` as a prop is the injection seam for tests and for a caller that already holds
  // the view; the hook is what a mounted panel uses. Both, rather than either, because the hook
  // cannot be called conditionally and a test cannot run an effect through renderToStaticMarkup.
  const fetchedExceptions = useRemediationExceptions(
    exceptions || streamlined ? null : (snapshot?.run_id || null), snapshot?.revision ?? null)
  const exceptionState = exceptions || fetchedExceptions
  // ONE LIVE REGION FOR THE WHOLE PANEL. PRD §12 asks for "a polite live region" — singular —
  // and two of them do not add up to one: a screen reader interleaves their updates, so the
  // sentence that matters ("two of your twelve retries were refused") arrives inside a stream of
  // headline changes. The exception region therefore reports UP to here rather than owning a
  // region of its own, and a material state change supersedes a stale action outcome.
  const [announcement, setAnnouncement] = useState('')
  const line = headline(snapshot)
  useEffect(() => { setAnnouncement('') }, [line])
  if (!snapshot || snapshot.state === 'draft') return assessmentContext?.scanId ? <PlannedRemediationWaterfall /> : null
  const suspect = snapshot.integrity?.ok === false
  const documentCountsSuspect = integrityAffects(snapshot, 'documents')
  // The count comes from the exception ENDPOINT, which groups by response and knows which rows
  // are actionable. The predicate this replaces read `snapshot.delivery.failures` — a field the
  // snapshot has never carried — so its delivery term was always false.
  const exceptionTotal = exceptionCount(exceptionState.view)
  // Retired presentation remains available to existing callers. Live View opts
  // into one AI graph and one activity list, without parallel progress totals.
  if (streamlined) return <section className={`panel remops${hidden ? ' remops-motion-paused' : ''}`} aria-label="Remediation live view">
    {['failed', 'cancelled', 'paused', 'stalled'].includes(snapshot.state) && <p role="status" className="remops-error">{line}{onViewMonitor && <button type="button" className="linklike" onClick={onViewMonitor}>View in Monitor →</button>}</p>}
    <RemainingWorkStatus snapshot={snapshot} events={events} />
    <RemediationWaterfallCard key={`${snapshot.scan_id || snapshot.run_id}:${snapshot.batch_id || "legacy"}`} snapshot={snapshot} paused={hidden} assessmentContext={assessmentContext} streamlined />
    {!hideActivity && <><div className="remops-actions"><FreshnessBadge state={fresh} updateMode={updateMode} /></div>
    <Activity scanId={snapshot?.scan_id || snapshot?.run_id} key={`${snapshot?.scan_id || snapshot?.run_id}:${snapshot?.batch_id || "legacy"}`} events={events} status={activityStatus} terminal={snapshot.terminal} /></>}
    <p aria-live="polite" className="sr-only" data-testid="rem-ops-announce">{line}</p>
  </section>
  return <section className={`panel remops${paused || hidden ? ' remops-motion-paused' : ''}`} aria-label="Remediation run status">
    <header className="remops-header"><div><span className="remops-eyebrow">{({ failed: 'Remediation failed', cancelled: 'Remediation stopped', paused: 'Remediation paused', stalled: 'Remediation stalled' })[snapshot.state] || (snapshot.terminal ? 'Remediation results' : activityConfirmed ? 'Remediation in progress' : 'Remediation run')}</span><h2>{line}</h2>{snapshot.source?.breadcrumb && <p>{snapshot.source.breadcrumb}</p>}<p className="muted">{snapshot.source?.locked_at ? `Snapshot locked ${new Date(snapshot.source.locked_at).toLocaleString()} · ` : ''}{snapshot.run_id}</p></div><div className="remops-actions"><FreshnessBadge state={fresh} updateMode={updateMode} /><button type="button" className="ghost" aria-pressed={paused} onClick={() => setPaused((value) => !value)}>{paused ? 'Resume visual updates' : 'Pause visual updates'}</button>{onViewMonitor && <button type="button" className="linklike" onClick={onViewMonitor}>View in Monitor →</button>}</div></header>
    {suspect && <div className="remops-integrity" role="status"><b>{documentCountsSuspect ? 'Document status is temporarily inconsistent.' : 'Some supporting totals are catching up.'}</b> ACP cannot currently reconcile {(snapshot.integrity.affected || []).join(', ') || 'one or more values'}. {documentCountsSuspect ? 'Document counts below are the last ACP confirmed.' : 'Live document progress remains available.'}</div>}
    <ActivityPulse events={events} generatedAt={snapshot.generated_at} />
    <Milestones notices={milestones} onDismiss={(key) => setMilestones((current) => current.filter((notice) => notice.key !== key))} />
    <RecoveryNotice recovery={snapshot.recovery} />
    <RetryNotice retryAt={snapshot.retry_at} now={clock} />
    <ProgressCue snapshot={snapshot} onViewMonitor={onViewMonitor} />
    <Progress snapshot={snapshot} suspect={documentCountsSuspect} />
    <RemediationWaterfallCard key={`${snapshot.scan_id || snapshot.run_id}:${snapshot.batch_id || "legacy"}`} snapshot={snapshot} paused={paused || hidden} assessmentContext={assessmentContext} />
    <details className="wf-accounting"><summary>Detailed accounting and finding evidence</summary><FindingReconciliation snapshot={snapshot} /></details>
    {snapshot.phases?.length > 0 && <Disclosure title="Phases" compact={compact}><Pipeline phases={snapshot.phases} attempts={snapshot.active_attempts || []} moving={activityConfirmed && connected && !paused && !hidden && (snapshot.active_attempts || []).length > 0} /></Disclosure>}
    <div className="remops-two"><Workstream attempts={snapshot.active_attempts || []} generatedAt={snapshot.generated_at} compact={compact} /><Throughput snapshot={snapshot} frozen={paused || hidden} /></div>
    <Disclosure title="Fix and delivery totals" compact={compact}><Secondary snapshot={snapshot} /></Disclosure>
    {/* The exception region is ALWAYS offered, unlike the stub it replaces: it names its own
        empty state ("nothing needs a decision or a retry"), which is an answer worth giving on a
        run that is going well, and a heading that only appears once something is wrong is one
        nobody has learned where to look for. The Disclosure's summary is this region's name, so
        the region itself renders no second heading under it. */}
    <div className={`remops-bottom${events.length ? '' : ' remops-bottom-empty'}`}><Disclosure title="Live activity" compact={compact}><Activity scanId={snapshot?.scan_id || snapshot?.run_id} key={`${snapshot?.scan_id || snapshot?.run_id}:${snapshot?.batch_id || "legacy"}`} events={events} status={activityStatus} terminal={snapshot.terminal} compact={compact} /></Disclosure><Disclosure title={`Needs attention${exceptionTotal ? ` · ${exceptionTotal}` : ''}`} compact={compact}><RemediationExceptions view={exceptionState.view} error={exceptionState.error} onReload={exceptionState.reload} runId={snapshot.run_id} onAnnounce={setAnnouncement} heading={null} /></Disclosure></div>
    <p aria-live="polite" className="sr-only" data-testid="rem-ops-announce">{announcement || line}</p>
  </section>
}

export function RemediationActivityPanel({ activity = null, snapshot, rows = [], decisions = {}, events = [], connected = false, receivedAt = null, activityStatus = 'ready', updateMode = 'idle', automatic = false }) {
  const fresh = useRemediationFreshness({snapshot, connected, receivedAt})
  if (!snapshot?.batch_id && !events.length) return null
  return <section className="panel remops" aria-label="Remediation live activity">
    <div className="remops-actions"><FreshnessBadge state={fresh} updateMode={updateMode} /></div>
    <RemediationProcessingNow activity={activity} terminal={snapshot?.terminal} />
    <RemainingWorkStatus snapshot={snapshot} events={events} rows={rows} decisions={decisions} automatic={automatic} />
    <Activity scanId={snapshot?.scan_id || snapshot?.run_id} key={`${snapshot?.scan_id || snapshot?.run_id}:${snapshot?.batch_id || "legacy"}`} events={events} status={activityStatus} terminal={snapshot?.terminal} />
  </section>
}

export function RemediationProcessingNow({ activity, terminal = false }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  if (terminal || !isRemediationActivity(activity) || !activity?.file || !activity?.action ||
      !Number.isFinite(activity.at) || now - activity.at * 1000 > 120000) return null
  return <div className="remops-processing-now" role="status" style={{padding: '12px 16px', margin: '12px 0', background: '#eef5ff', borderRadius: 8, fontFamily: 'var(--font-mono, monospace)'}}>
    <span className="muted">Processing Now: </span><strong>{activity.file}</strong>{' · '}
    <span>{remediationStep(activity)}</span>
  </div>
}
