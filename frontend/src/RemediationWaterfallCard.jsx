import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import Drawer from './Drawer.jsx'
import RemediationAssessmentProgress from './RemediationAssessmentProgress.jsx'
import WaterfallVisualDrawer from './WaterfallVisualDrawer.jsx'
import WaterfallDrawerOverview from './WaterfallDrawerOverview.jsx'
import WaterfallDrawerChanges from './WaterfallDrawerChanges.jsx'
import RemediationRunInsights from './RemediationRunInsights.jsx'
import { getFindingDispositions } from './api.js'
import { authEpoch } from './apiIdentity.js'
import useWaterfallActivity from './useWaterfallActivity.js'
import WaterfallCount from './WaterfallCount.jsx'
import useWaterfallMotion from './useWaterfallMotion.js'
import RemediationThroughput from './RemediationThroughput.jsx'
import RemediationWaterfallGraph from './RemediationWaterfallGraph.jsx'
import RemediationAttemptStory from './RemediationAttemptStory.jsx'
import RemediationCompletionSummary from './RemediationCompletionSummary.jsx'
import CloudAIActivity from './CloudAIActivity.jsx'
import WaterfallRunNotice from './WaterfallRunNotice.jsx'
import './remediation-waterfall-card.css'

const OUTCOMES = [
  ['resolved_verified', 'Fixed and checked', 'resolved_verified', 'Applied changes with qualifying verification evidence.'],
  ['awaiting_review', 'Awaiting your review', 'awaiting_review', 'Findings awaiting a person’s decision. These are not verified fixes.'],
  ['approved_pending_verification', 'Approved, awaiting completion', 'approved_pending_verification', 'Approval recorded; application or verification is still outstanding.'],
  ['unchanged_no_fix', 'No eligible fix', 'unchanged_no_fix', 'Still needs work: no eligible correction was applied.'],
  ['failed', 'Remediation failed', 'remediation_failed', 'Still needs work: remediation did not complete successfully.'],
  ['excluded', 'Excluded by policy', 'excluded_by_policy', 'Excluded from correction under the accepted run policy.'],
  ['superseded', 'Superseded by reassessment', 'superseded_by_reassessment', 'A newer assessment replaced these findings.'],
]
// Mutually exclusive result buckets for the fixed assessment baseline. The detailed
// disposition rows below remain available for drilldown; this summary deliberately
// keeps “still needs work” together so a person can read the result at a glance.
export const RESULT_BUCKETS = [
  ['fixed', 'Fixed and checked', 'Applied changes with qualifying verification evidence.'],
  ['awaiting_review', 'Suggestions awaiting review', 'Usable suggestions that still need a person’s decision.'],
  ['approved_pending', 'Approved, awaiting completion', 'Approved work that is still being applied or checked.'],
  ['needs_work', 'Still needs work', 'Rejected, failed, unsupported, or otherwise unresolved findings.'],
  ['processing', 'Still processing', 'Findings with active work in progress.'],
  ['unavailable', 'Outcome unavailable', 'Records that cannot yet be reconciled to the fixed baseline.'],
]
export function resultBuckets(reconciliation) {
  const rec = reconciliation || {}
  const unresolved = ['unchanged_no_fix', 'failed', 'excluded', 'superseded']
    .reduce((sum, key) => sum + (count(rec[key]) ? rec[key] : 0), 0)
  return RESULT_BUCKETS.map(([key, label, detail]) => [key, label, detail, {
    fixed: rec.resolved_verified,
    awaiting_review: rec.awaiting_review,
    approved_pending: rec.approved_pending_verification,
    needs_work: unresolved,
    processing: rec.processing,
    unavailable: rec.unavailable,
  }[key]])
}
const money = value => Number.isSafeInteger(value) ? new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 6,
}).format(value / 1000000) : 'Unavailable'
const count = value => Number.isSafeInteger(value) && value >= 0

// Retired vertical presentation, retained for a reversible layout change.
function Stage({ title, detail, children, stamp, paused, identity, onClick, selected, active = false, activeLabel = 'Checking corrected documents' }) {
  const previous = useRef(null)
  const [pulse, setPulse] = useState(0)
  useEffect(() => {
    const before = previous.current
    previous.current = { stamp, identity }
    setPulse(0)
    if (paused || before?.identity !== identity || before.stamp == null || stamp == null || before.stamp === stamp) return undefined
    setPulse(value => value + 1)
    const timer = setTimeout(() => setPulse(0), 1600)
    return () => clearTimeout(timer)
  }, [stamp, paused, identity])
  return <li className={`wf-stage${active ? ' wf-stage-active' : ''}`}><button type="button" className={`wf-stage-button${selected ? ' wf-selected' : ''}`} onClick={onClick} aria-pressed={selected}>
    {pulse > 0 && !paused && <span key={pulse} className="wf-stage-flare" aria-hidden="true" />}
    <strong>{title}</strong>{active && <span className="wf-working"><i aria-hidden="true" />{activeLabel}</span>}<span className="wf-secondary">{detail}</span>{children}
  </button><span className="wf-connector" aria-hidden="true">↓</span></li>
}

export default function RemediationWaterfallCard({ snapshot, paused = false, activity = null, assessmentContext = null, streamlined = false }) {
  const scanId = snapshot.scan_id || snapshot.run_id
  const batchId = snapshot.batch_id
  const identity = `${authEpoch()}:${scanId}:${batchId}`
  const fetched = useWaterfallActivity(activity ? null : scanId, batchId, paused)
  const state = activity || fetched
  const data = state.view
  const rec = snapshot.finding_reconciliation || {}
  const exact = rec.exact === true && !rec.violations?.length && !snapshot.integrity?.affected?.includes('finding_reconciliation')
    && count(rec.assessed) && OUTCOMES.every(([key]) => count(rec[key]))
    && OUTCOMES.reduce((sum, [key]) => sum + rec[key], 0) === rec.assessed
  const [selection, setSelection] = useState('rules')
  const [selectionScope, setSelectionScope] = useState(identity)
  const [selectedModel, setSelectedModel] = useState(null)
  const selectedRole = selectedModel?.stage || selection
  const [stageDrawer, setStageDrawer] = useState(false)
  const closeStageDrawer = useCallback(() => setStageDrawer(false), [])
  const openStage = (stage, model = null) => {
    setSelectionScope(identity)
    setSelection(model?.id || stage)
    setSelectedModel(model ? { ...model, id: model.id || stage } : null)
    setStageDrawer(true)
  }
  const storyLive = !snapshot.terminal && (snapshot.state === 'running' || (snapshot.state === 'needs_attention' && snapshot.also?.includes('running')))
  const [motionPaused, setMotionPaused] = useState(false)
  const motion = useWaterfallMotion(snapshot, data, { paused: paused || motionPaused, error: state.error, selected: selectedRole })
  const visualsPaused = paused || motionPaused || motion.hidden
  const [drawer, setDrawer] = useState(null)
  const requestId = useRef(0)
  const close = useCallback(() => { requestId.current += 1; setDrawer(null) }, [])
  useEffect(() => { setSelection('rules'); setSelectedModel(null); setStageDrawer(false); close() }, [identity, close])
  useEffect(() => () => { requestId.current += 1 }, [])
  const openOutcome = async ([key, label, disposition, detail]) => {
    const id = ++requestId.current
    const epoch = authEpoch()
    setDrawer({ identity, label, detail, loading: true })
    try {
      const result = await getFindingDispositions(scanId, disposition)
      if (id !== requestId.current || epoch !== authEpoch()) return
      if (!result.available || result.batch_id !== batchId) throw new Error('The run changed. Refresh the card to see matching evidence.')
      if (result.items.length !== rec[key]) throw new Error('These findings changed while you opened them. Refresh the card for matching totals.')
      setDrawer({ identity, label, detail, items: result.items, loading: false })
    } catch (error) {
      if (id === requestId.current && epoch === authEpoch()) setDrawer({ identity, label, detail, error: error.message })
    }
  }
  const displayCount = value => <WaterfallCount value={value} identity={`${identity}:${exact ? 'findings' : 'changes'}`} paused={visualsPaused || state.error} />
  const stages = data?.stages || []
  const reviewUrl = new URL(typeof window !== 'undefined' ? window.location.href : 'http://localhost/')
  reviewUrl.searchParams.set('tab', 'remediate')
  reviewUrl.searchParams.set('mode', 'review')
  const releaseUrl = new URL(reviewUrl)
  releaseUrl.searchParams.set('tab', 'publish')
  releaseUrl.searchParams.delete('mode')
  const outcomesRef = useRef(null)
  const spending = data?.spending
  const selectedStage = selectedModel?.attemptIds ? null : stages.find(stage => stage.tier === (selectedRole === 'first' ? 1 : selectedRole === 'next' ? 2 : null))
  const descriptions = {
    rules: 'Supported rule-based changes follow the approval settings accepted for this run. Verified changes below include all origins; a rule-only finding split is not yet available.',
    first: 'The first configured model handles drafting and, if requested, review work. Counts describe recorded operations and charge states, not usable suggestions or fixed findings.',
    next: 'The next configured model can draft after an unusable response or review a draft when your plan permits. These counts include both purposes. Open saved history below to see which work it performed.',
    unknown: 'The purpose or position of this recorded AI work is unavailable. Open its saved evidence for known facts.',
    review: 'This recorded reviewer checks a suggestion. Its verdict is not a verified correction, and approval follows the authorization saved for this run.',
    approval: 'Approval follows the authorization saved for this run. Saved approval and review evidence remains distinct from verification.',
    verify: 'Approved changes must be applied and pass the existing verification checks. Document processing and provider responses do not count as fixed findings.',
  }
  const selectedDescription = selectedModel?.purpose === 'generation'
    ? 'This is the model saved for this generation step. Attempts and outcomes are shown only when recorded.'
    : selectedModel?.purpose === 'draft' ? 'Recorded draft attempts for this model do not establish a usable suggestion or a verified fix.'
    : selectedModel?.purpose === 'fallback' ? 'Recorded fallback attempts for this model. Its position is shown only when preserved in the saved run.'
    : descriptions[selectedRole]
  const stageEvidence = <><h4>{({ rules: 'Rules lead the way', first: 'What the first AI did', next: 'What the next AI did', unknown: 'Recorded AI evidence', review: 'What the AI reviewer did', approval: 'Your decision matters', verify: 'Evidence of completion' })[selectedRole]}</h4><p>{selectedDescription}</p>{selectedStage?.models?.length > 0 && <ul>{selectedStage.models.map(model => <li key={`${model.provider}:${model.model}`}>{model.provider} · {model.model}</li>)}</ul>}{selectedStage && <><dl key={selectedStage.tier} className="wf-spending">{[['reserved', 'Reserved, not dispatched'], ['active', 'Dispatched, awaiting charge'], ['settled', 'Charge recorded'], ['released', 'Released without charge'], ['uncertain', 'Charge uncertain'], ['breached', 'Charge exceeded reservation']].map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{displayCount(selectedStage[key])}</dd></div>)}</dl><p className="wf-secondary">{money(selectedStage.spent_units)} settled · {money(selectedStage.held_units)} reserved for this step.</p><p className="wf-secondary">These are attempt states. Recorded operations deduplicate admission retries.</p></>}
      <section><h4>What each AI step added</h4><p>{data?.contribution_reason || 'AI step breakdown unavailable for this run. Calls cannot yet be joined to usable suggestions.'}</p><p className="wf-secondary">Optional AI reviews check a suggestion; they do not add another finding. Open saved model history below to see recorded reviews.</p></section>
      <section><h4>Models behind current proposals</h4>{data?.models?.length ? <><ul className="wf-models">{data.models.map(model => <li key={`${model.provider}:${model.model}`}><strong>{model.provider} · {model.model}</strong><span>{model.linked_calls} recorded call{model.linked_calls === 1 ? '' : 's'} linked to current proposals</span><span>Recorded call cost: {typeof model.recorded_cost_usd === 'number' ? money(Math.round(model.recorded_cost_usd * 1000000)) : 'Unavailable'}</span></li>)}</ul><p className="wf-secondary">These models produced the current proposals for this scan. Proposals and their recorded call costs may come from other runs. This is not a model breakdown for the selected run; do not add these costs to its charges below.</p></> : <p>No provider/model identity is linked to current proposals for this view. Historical run attribution is unavailable.</p>}</section>
      <section><h4>Spending for this run</h4><dl className="wf-spending">{[['spent_units', 'Settled provider charges'], ['held_units', 'Reserved · may still be charged'], ['available_units', 'Remaining allowance'], ['cap_units', 'Approved spending limit']].map(([key, label]) => <div key={key}><dt>{label}</dt><dd><WaterfallCount value={spending?.[key]} identity={identity} paused={visualsPaused || state.error} format={money} /></dd></div>)}</dl>{spending?.unknown_charges > 0 && <p className="wf-note">{spending.unknown_charges} charge(s) unknown. Their reservations remain held.</p>}{spending?.blocked && <p className="wf-note">Further AI spending is blocked pending reconciliation.</p>}<p className="wf-secondary">Provider charges only. Infrastructure costs are separate.</p></section></>
  return <section className={`wf-card${visualsPaused ? ' wf-paused' : ''}`} aria-label="Live remediation waterfall">
    {!streamlined && <header className="wf-header"><div><span className="wf-eyebrow">Selected run</span><h3>{snapshot.terminal ? 'Your run, recorded' : 'Follow your remediation'}</h3></div><div className="wf-header-status"><span className="wf-tag">Approval follows saved run authorization</span><RemediationThroughput mini data={snapshot.throughput} identity={identity} paused={visualsPaused || state.error} /></div></header>}
    <div className="wf-motion-status"><span>{motion.documents > 0 ? <><i className="wf-processing-dot" aria-hidden="true" />{motion.documents} documents processing · counts update as results arrive</> : snapshot.terminal ? 'Recorded run results' : 'Motion follows confirmed activity'}</span>{!snapshot.terminal && <button type="button" aria-pressed={motionPaused} disabled={paused} onClick={() => setMotionPaused(value => !value)}>{motionPaused ? 'Resume animation' : 'Pause animation'}</button>}</div>
    <CloudAIActivity scanId={scanId} batchId={batchId} view={data} verifiedFindings={exact ? rec.resolved_verified : null} aiEnabled={data?.ai_enabled} live={storyLive} paused={visualsPaused || state.error} />
    {(!snapshot.terminal || state.error) && <WaterfallRunNotice snapshot={snapshot} view={data} error={state.error} paused={visualsPaused} />}
    {!streamlined && <RemediationAssessmentProgress snapshot={snapshot} assessmentContext={assessmentContext} identity={identity} paused={visualsPaused || state.error} />}
    <div className="wf-layout wf-layout-graph">
      <RemediationWaterfallGraph stages={stages} aiEnabled={data?.ai_enabled}
        selection={selectionScope === identity ? selection : 'rules'} onSelect={openStage} motion={motion}
        snapshot={snapshot} viewAvailable={data?.available} runGraph={data?.run_graph}
        paused={visualsPaused} error={state.error} identity={identity}
        reviewCount={snapshot.review?.items} verifiedCount={snapshot.fixes?.verified} />
      <div className="wf-activity-action"><span>{data?.generated_at ? `Updated ${new Date(data.generated_at).toLocaleTimeString()}` : 'Saved activity details'}</span><button type="button" className="ghost" onClick={() => { setSelectionScope(identity); setSelectedModel(null); setSelection('rules'); setStageDrawer(true) }}>View activity</button></div>
    </div>
    {!streamlined && <RemediationCompletionSummary snapshot={snapshot} view={data} exact={exact} reviewHref={`${reviewUrl.pathname}${reviewUrl.search}`} releaseHref={`${releaseUrl.pathname}${releaseUrl.search}`} />}
    <footer className="wf-footer"><span>{state.error ? data ? 'Refresh delayed · showing the last recorded AI activity' : 'AI activity unavailable · retrying' : visualsPaused ? 'Animation paused · recorded totals remain available' : 'Updates follow recorded activity'}</span><span>{data?.generated_at ? `AI snapshot ${new Date(data.generated_at).toLocaleTimeString()}` : data?.available === false ? 'No managed waterfall records for this run' : 'Waiting for AI activity records'}</span></footer>
    {stageDrawer && selectionScope === identity && createPortal(<WaterfallVisualDrawer identity={identity}
      stageTitle={selectedModel?.stepId === 'fallback_1' ? 'First fallback' : selectedModel?.stepId === 'fallback_2' ? 'Second fallback' : ({ rules: 'Rule-based changes', first: 'Primary model', next: 'Recorded fallback', review: 'AI review', approval: 'Approval', verify: 'Verification', unknown: 'Recorded AI work' })[selectedRole]}
      provider={selectedModel?.provider} model={selectedModel?.model} status={data?.ai_enabled === false && !['rules', 'approval', 'verify'].includes(selectedRole) ? 'AI off for this run' : snapshot.terminal ? 'Overall run: finished' : 'Overall run: in progress'}
      stageKind={selectedModel?.stepId === 'fallback_1' ? 'fallback1' : selectedModel?.stepId === 'fallback_2' ? 'fallback2' : ({rules:'rules',review:'review',approval:'approval',verify:'verification'})[selectedRole] || 'model'}
      breadcrumb={`Run › ${({primary:'Primary model',fallback_1:'First fallback',fallback_2:'Second fallback',rules:'Rules',first:'Initial AI',next:'Recorded fallback',review:'AI review',approval:'Approval',verify:'Verification',unknown:'Recorded AI'})[selectedModel?.stepId || selectedRole] || 'Recorded stage'}`} onClose={closeStageDrawer}
      overview={({selectTab}) => <WaterfallDrawerOverview aiEnabled={data?.ai_enabled} scanId={scanId} batchId={batchId} identity={identity} selectedModel={selectedModel} role={selectedRole} view={data} onSelectStage={openStage} description={selectedDescription} snapshot={snapshot} live={storyLive} paused={visualsPaused || state.error} selectTab={selectTab} />}
      changes={<WaterfallDrawerChanges key={identity} scanId={scanId} batchId={batchId} live={storyLive} paused={paused || motion.hidden} modelFilter={selectedModel} />}
      attempts={<RemediationAttemptStory scanId={scanId} batchId={batchId} modelFilter={selectedModel?.model || selectedModel?.attemptIds ? selectedModel : null} defaultOpen={true} live={storyLive} paused={paused || motion.hidden} reviewHref={`${reviewUrl.pathname}${reviewUrl.search}${reviewUrl.hash}`} />}
      evidence={<><div className="wf-detail">{stageEvidence}</div><RemediationRunInsights scanId={scanId} batchId={batchId} inlineDrilldown /></>}
    />, document.body)}
    {drawer?.identity === identity && createPortal(<Drawer title={drawer.label} subtitle={drawer.detail} onClose={close}><div className="wf-drawer-content">{drawer.loading && <p role="status">Loading findings…</p>}{drawer.error && <p role="alert">{drawer.error}</p>}{drawer.items && <><p>{drawer.items.length} findings in this outcome.</p>{drawer.items.length === 0 && <p>No findings in this outcome.</p>}<ul>{drawer.items.map(item => <li key={item.finding_id}><strong>{item.file}</strong><span>WCAG {item.rule_id} · {item.instance_key}</span>{item.verified_at && <span>Verified {new Date(item.verified_at).toLocaleString()}</span>}</li>)}</ul></>}</div></Drawer>, document.body)}
  </section>
}


// Retired 2026-09-09 at the user's request. Kept for reuse; deliberately not rendered.
export function RetiredFindingOutcomes({ outcomesRef, rec, exact, openOutcome, displayCount = value => value }) {
  return (
    <section ref={outcomesRef} tabIndex={-1} className="wf-outcomes" aria-label="Finding outcomes">
      <div className="wf-section-head"><h4>Where your findings stand</h4><span>{count(rec.assessed) ? `${rec.assessed.toLocaleString()} assessed findings` : 'Finding baseline unavailable'}</span></div>
      {exact ? <><div className="wf-results-summary"><div className="wf-section-head"><h4>Results against the assessed baseline</h4><span>{rec.assessed.toLocaleString()} findings · fixed baseline</span></div><div className="wf-outcome-bar" aria-hidden="true">{resultBuckets(rec).map(([key,,, value], index) => <span key={key} className={`wf-tone-${index}`} style={{ width: `${rec.assessed ? (count(value) ? value : 0) / rec.assessed * 100 : 0}%` }} />)}</div><div className="wf-results-legend">{resultBuckets(rec).map(([key, label, detail, value], index) => <div key={key}><i className={`wf-tone-${index}`} aria-hidden="true" /><span><strong>{label}</strong><small>{count(value) ? value.toLocaleString() : 'Unavailable'}</small><em>{detail}</em></span></div>)}</div><details><summary>View result counts as a table</summary><table><caption className="sr-only">Results against the assessed baseline</caption><thead><tr><th scope="col">Result</th><th scope="col">Findings</th><th scope="col">Meaning</th></tr></thead><tbody>{resultBuckets(rec).map(([key, label, detail, value]) => <tr key={key}><th scope="row">{label}</th><td>{count(value) ? value.toLocaleString() : 'Unavailable'}</td><td>{detail}</td></tr>)}</tbody></table></details></div><div className="wf-outcome-bar" aria-hidden="true">{OUTCOMES.map(([key], index) => <span key={key} className={`wf-tone-${index}`} style={{ width: `${rec.assessed ? rec[key] / rec.assessed * 100 : 0}%` }} />)}</div>
        <div className="wf-legend">{OUTCOMES.map((row, index) => <button key={row[0]} type="button" onClick={() => openOutcome(row)}><i className={`wf-tone-${index}`} aria-hidden="true" />{row[1]} {displayCount(rec[row[0]])}<span className="sr-only">. View finding details.</span></button>)}</div>
        <details className="wf-outcome-table"><summary>View outcomes as a table</summary><table><thead><tr><th>Outcome</th><th>Findings</th></tr></thead><tbody>{OUTCOMES.map(row => <tr key={row[0]}><th><button type="button" className="linklike" onClick={() => openOutcome(row)}>{row[1]}</button></th><td>{rec[row[0]].toLocaleString()}</td></tr>)}</tbody></table></details>
      </> : <details className="wf-outcome-unavailable"><summary>Finding outcome totals unavailable · why?</summary><p>Finding outcomes are not fully reconciled yet. Verified changes and review items above use separate units; a complete finding bar is unavailable.</p></details>}
    </section>
  )
}
