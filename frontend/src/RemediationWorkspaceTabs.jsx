import { useEffect, useRef, useState } from 'react'
import RemediationPlanDialog from './RemediationPlanDialog.jsx'
import useConfirmedRemediationActivity from './useConfirmedRemediationActivity.js'
const MODES = ['live', 'review', 'waterfall']
const shownPlans = new Set()
const acceptedPlans = new Set()
// This records presentation history only. It never supplies execution identity,
// accepts a new plan, or changes the server's permission to start work.
function planWasAccepted(identity) {
  if (acceptedPlans.has(identity)) return true
  try { return localStorage.getItem(`acp.remediation.plan.accepted:${identity}`) === 'yes' } catch { return false }
}
function rememberAcceptedPlan(identity) {
  if (!identity) return
  acceptedPlans.add(identity)
  try { localStorage.setItem(`acp.remediation.plan.accepted:${identity}`, 'yes') } catch { /* memory fallback */ }
}
function planWasShown(runId) {
  if (shownPlans.has(runId)) return true
  try { return localStorage.getItem(`acp.remediation.plan.shown:${runId}`) === 'yes' } catch { return false }
}
function rememberPlan(runId) {
  if (!runId) return
  shownPlans.add(runId)
  try { localStorage.setItem(`acp.remediation.plan.shown:${runId}`, 'yes') } catch { /* memory fallback */ }
}

function modeFromLocation() {
  try {
    const mode = new URLSearchParams(window.location.search).get('mode')
    return ['plan', 'modes', ...MODES].includes(mode) ? mode : null
  } catch { return null }
}

export default function RemediationWorkspaceTabs({ runId, reviewCount = 0, snapshot = null,
  plan, review, live, waterfall, reviewOptional = false, workspaceRequest = null, planAccepted = false, assessmentReady = false, assessmentIdentity }) {
  const planIdentity = assessmentIdentity || runId
  const previouslyAccepted = planAccepted || planWasAccepted(planIdentity)
  // Live is the default workspace. Legacy Plan links open the required planning dialog.
  const [chosen, setChosen] = useState(() => modeFromLocation())
  const tabs = useRef([])
  const panels = useRef({})
  const pendingFocus = useRef(null)
  const cancelPanelFocus = () => {
    if (pendingFocus.current) cancelAnimationFrame(pendingFocus.current.id)
    pendingFocus.current = null
  }
  const lastWorkspaceRequest = useRef(workspaceRequest)
  const activeWork = useConfirmedRemediationActivity(snapshot)
  const lastPanel = useRef('live')
  const introduction = useRef(null)
  if (MODES.includes(chosen)) lastPanel.current = chosen
  const mode = MODES.includes(chosen) ? chosen : lastPanel.current

  useEffect(() => {
    cancelPanelFocus()
    const locationMode = modeFromLocation()
    const planning = locationMode === 'plan' || locationMode === 'modes'
    if (planAccepted) rememberAcceptedPlan(planIdentity)
    const introductionKey = `${planIdentity}:${assessmentReady}:${previouslyAccepted}`
    if (introduction.current === introductionKey) return cancelPanelFocus
    introduction.current = introductionKey
    const shouldIntroduce = assessmentReady && runId && !previouslyAccepted && !planWasShown(planIdentity)
    setChosen(previouslyAccepted && planning ? 'live'
      : shouldIntroduce ? 'plan' : assessmentReady && planning && planWasShown(planIdentity) ? 'live' : locationMode)
    if (previouslyAccepted && planning) {
      try {
        const url = new URL(window.location.href)
        url.searchParams.set('mode', 'live')
        history.replaceState({}, '', url)
      } catch { /* navigation state is progressive enhancement */ }
    }
    if (shouldIntroduce || (assessmentReady && planning)) rememberPlan(planIdentity)
    return cancelPanelFocus
  }, [runId, planIdentity, assessmentReady, planAccepted, previouslyAccepted])

  useEffect(() => {
    const restore = () => {
      cancelPanelFocus()
      const next = modeFromLocation()
      setChosen((previouslyAccepted || (assessmentReady && planWasShown(planIdentity))) && ['plan', 'modes'].includes(next) ? 'live' : next)
    }
    window.addEventListener('popstate', restore)
    return () => window.removeEventListener('popstate', restore)
  }, [runId, planIdentity, assessmentReady, previouslyAccepted])

  const select = (next, { focusPanel = false } = {}) => {
    cancelPanelFocus()
    if (next === 'plan' || next === 'modes') rememberPlan(planIdentity)
    setChosen(next)
    try {
      const url = new URL(window.location.href)
      url.searchParams.set('tab', 'remediate')
      url.searchParams.set('mode', next)
      history.pushState({}, '', url)
    } catch { /* navigation state is progressive enhancement */ }
    if (focusPanel) {
      const request = { id: null }
      pendingFocus.current = request
      request.id = requestAnimationFrame(() => {
        if (pendingFocus.current !== request) return
        pendingFocus.current = null
        const panel = panels.current[next]
        if (panel?.isConnected && !panel.hidden) panel.focus()
      })
    }
  }

  // Accepted launches and explicit header actions reveal their destination. Background
  // snapshots never interrupt a chosen panel or discard in-progress selections.
  useEffect(() => {
    if (lastWorkspaceRequest.current === workspaceRequest) return
    lastWorkspaceRequest.current = workspaceRequest
    // `focusPanel: false` is for a request that places focus itself (opening one review item
    // focuses that row); focusing the panel a frame later would take it straight back.
    if (['plan', ...MODES].includes(workspaceRequest?.mode) && !(planAccepted && workspaceRequest.mode === 'plan')) select(workspaceRequest.mode, { focusPanel: workspaceRequest.focusPanel !== false })
  }, [workspaceRequest, planAccepted])

  const onKeyDown = (event, index) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? MODES.length - 1
      : (index + (event.key === 'ArrowRight' ? 1 : -1) + MODES.length) % MODES.length
    select(MODES[nextIndex])
    tabs.current[nextIndex]?.focus()
  }

  return <>
    <div className="rem-workspace-tabs" role="tablist" aria-label="Remediation workspace">
      {MODES.map((value, index) => <button key={value}
        ref={(node) => { tabs.current[index] = node }} type="button" role="tab"
        id={`rem-mode-${value}`} aria-controls={`rem-panel-${value}`} aria-selected={mode === value}
        tabIndex={mode === value ? 0 : -1} onKeyDown={(event) => onKeyDown(event, index)}
        onClick={() => select(value)}>
        {value === 'live' ? 'Live activity' : value === 'waterfall' ? 'AI activity' : 'Needs your review'}
        {value === 'review' && snapshot?.batch_id && (snapshot.scan_id || snapshot.run_id) === runId && <span>{reviewCount.toLocaleString()}</span>}
        {value === 'live' && activeWork && <span className="rem-mode-live-dot" aria-label="active">●</span>}
      </button>)}
    </div>
    {!planAccepted && <button type="button" className="ghost" onClick={() => select('plan')}>Remediation plan</button>}
    <RemediationPlanDialog open={!planAccepted && (chosen === 'plan' || chosen === 'modes')} onClose={() => select(mode)}>
      {plan}
    </RemediationPlanDialog>
    <div ref={node => { panels.current.review = node }} id="rem-panel-review" role="tabpanel" tabIndex={-1} aria-labelledby="rem-mode-review"
      hidden={mode !== 'review'}>{reviewOptional && <p>Review is optional for publishing. Suggestions requiring approval stay unapplied and appear in the remaining-work checklist.</p>}{review}</div>
    <div ref={node => { panels.current.live = node }} id="rem-panel-live" role="tabpanel" tabIndex={-1} aria-labelledby="rem-mode-live"
      hidden={mode !== 'live'}>
      <h2 className="sr-only">Live Processing</h2>
      {live}
    </div>
    <div ref={node => { panels.current.waterfall = node }} id="rem-panel-waterfall" role="tabpanel" tabIndex={-1} aria-labelledby="rem-mode-waterfall" hidden={mode !== 'waterfall'}>{waterfall}</div>
  </>
}
