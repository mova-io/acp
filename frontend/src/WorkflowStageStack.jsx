import { useEffect, useMemo, useState } from 'react'
import CompletedStageDetails, { liveDiscoverProgress } from './CompletedStageDetails.jsx'
import { assessmentStageActivity } from './assessmentStageActivity.js'
import DiscoverRunProgress from './DiscoverRunProgress.jsx'
import LiveHeartbeatBars from './LiveHeartbeatBars.jsx'
import WorkflowStageActivityCard from './WorkflowStageActivityCard.jsx'
import { releaseBatchProgress } from './releaseBatchProgress.js'
import { canonicalStageCardModel, canonicalWorkflowStages, currentCanonicalStage,
  stageNeedsAttention, alignRemediationAssessment, omittedAssessmentGroups } from './canonicalStageCard.js'

const STAGES = ['discover', 'assess', 'remediate', 'release']
const destination = { discover: 'discover', assess: 'assess', remediate: 'remediate', release: 'publish' }
const terminal = (state) => ['processing_complete', 'succeeded', 'failed', 'cancelled', 'superseded', 'integrity_failed'].includes(state)
const completed = (state) => ['processing_complete', 'succeeded'].includes(state)

function storageKey(lineage) {
  const workflow = lineage?.workflow_id || lineage?.scan_id || 'workflow'
  return `acp:workflow-stage-stack:${workflow}:${lineage?.workflow_revision ?? 'unknown'}`
}

function primaryOutcome(model) {
  if (model.stage === 'discover') return `${model.domain?.total ?? '—'} ${model.domain?.unit || 'inventory documents'}`
  if (model.stage === 'assess') {
    const assessed = model.domain?.buckets.find(([label]) => label === 'Assessed')?.[1]
    return `${assessed ?? '—'} assessed of ${model.domain?.total ?? '—'} eligible documents`
  }
  if (model.stage === 'remediate' && model.domain) return `${model.domain.total ?? '—'} assessed findings`
  return `${model.domain?.accounted ?? model.accounted ?? '—'} of ${model.domain?.total ?? model.total ?? '—'} ${model.domain?.unit || model.unit}`
}

function unresolvedRemediation(snapshot) {
  if (snapshot?.stage !== 'remediate') return false
  const total = snapshot.domain_reconciliation?.total
  const verified = snapshot.domain_reconciliation?.buckets?.resolved_verified
  return Number.isSafeInteger(total) && Number.isSafeInteger(verified) && verified < total
}

/** The sole outer shell for all four workflow stages. Detail nodes stay mounted under `hidden`
 * so disclosure changes do not end live subscriptions or reset rolling heartbeat history. */
export default function WorkflowStageStack({ lineage, onNavigate, receivedAt = null,
  activeTab = null, stageDetails = {}, stageAfter = {}, defaultCollapsed = false, progressHostId = null, activeStage = null, assessmentActivity = null, assessmentFindings = null, discoveryScope = null, releaseReviewWorkspace = null }) {
  const snapshots = useMemo(() => canonicalWorkflowStages(lineage), [lineage])
  const current = useMemo(() => currentCanonicalStage(lineage), [lineage])
  const key = storageKey(lineage)
  const [overrides, setOverrides] = useState({})

  useEffect(() => { setOverrides({}) }, [key, activeStage, activeTab, defaultCollapsed])

  if (!snapshots.length) return null
  const assessStage = snapshots.find(snapshot => snapshot.stage === 'assess')
  const byStage = new Map(snapshots.map((snapshot) => {
    const sameAssessment = assessmentFindings?.scanId === lineage?.scan_id
      && (!snapshot.input_manifest_id || !assessStage?.output_manifest_id || snapshot.input_manifest_id === assessStage.output_manifest_id)
    const scoped = ['discover', 'assess'].includes(snapshot.stage) && discoveryScope?.scanId === lineage?.scan_id && discoveryScope?.scope
      ? { ...snapshot, scope: discoveryScope.scope } : snapshot
    const aligned = alignRemediationAssessment(scoped, sameAssessment ? assessmentFindings?.total : null)
    const omitted = sameAssessment && snapshot.stage === 'remediate'
      ? omittedAssessmentGroups(snapshot, assessStage?.assessment_summary, assessmentFindings?.rows) : []
    const omittedTotal = omitted.reduce((sum, group) => sum + group.count, 0)
    return [snapshot.stage, omittedTotal > 0 && omittedTotal === aligned.domain_reconciliation?.buckets?.not_in_remediation_breakdown
      ? { ...aligned, omitted_assessment_groups: omitted } : aligned]
  }))

  return (
    <section className="workflow-stage-stack" aria-label="Workflow stages"
      data-current-stage={current?.stage || ''}>
      {STAGES.filter((stage) => byStage.has(stage)).map((stage) => {
        const snapshot = byStage.get(stage)
        const isCurrent = Boolean(snapshot && stage === current?.stage
          && snapshot.execution_id === current?.execution_id)
        const model = snapshot ? canonicalStageCardModel(snapshot, { isCurrent }) : null
        const batch = releaseBatchProgress(snapshot)
        const attention = Boolean(snapshot && (stageNeedsAttention(snapshot) || !model.integrityOk || batch?.state === 'failed'))
        const assessment = assessmentStageActivity(snapshot, assessmentActivity, lineage?.scan_id)
        const displayState = assessment?.state || batch?.state || snapshot.state
        const isCompleted = completed(displayState)
        // Only running work on its own tab opens automatically. Other tabs retain
        // live status in the collapsed header; users can still open details.
        const unresolved = isCurrent && unresolvedRemediation(snapshot)
        const defaultOpen = !defaultCollapsed && activeStage === stage
          && (['processing', 'running'].includes(displayState) || unresolved)
        const overrideKey = `${stage}:${snapshot.execution_id}:${isCompleted ? 'complete' : terminal(displayState) ? 'stopped' : 'live'}`
        const open = overrides[overrideKey] ?? defaultOpen
        const detail = isCurrent ? stageDetails[stage] : null
        const bodyId = `workflow-stage-${stage}`
        return (
          <div className={`workflow-stage-stack__item${attention ? ' needs-attention' : ''}${isCurrent ? ' is-current' : ''}`}
               data-stage={stage} data-current={isCurrent ? 'true' : 'false'}
               key={`${stage}:${snapshot?.execution_id || snapshot?.revision || 'locked'}`}>
            <button type="button" className="workflow-stage-stack__summary"
                    aria-expanded={open} aria-controls={bodyId}
                    onClick={() => setOverrides((value) => ({ ...value, [overrideKey]: !open }))}>
              <span className="workflow-stage-stack__check" aria-hidden="true">
                {attention ? '!' : displayState === 'succeeded' ? '✓' : '•'}
              </span>
              <span className="workflow-stage-stack__label"><b>{model.stageLabel}</b>
                <span className="workflow-stage-stack__state"> · {assessment?.label || batch?.label || model.stateLabel}</span>
              </span>
              <span className="workflow-stage-stack__meta">
                <span className="muted workflow-stage-stack__count">{assessment?.count || batch?.summary || primaryOutcome(model)}</span>
                {!open && !terminal(displayState) && <LiveHeartbeatBars measuredAt={receivedAt} stage={stage}
                  historyKey={`${snapshot.workflow_id || lineage?.workflow_id || 'workflow'}:${snapshot.execution_id || stage}`}
                  showText />}
                {isCurrent && <span className="workflow-stage-stack__ownership">Current</span>}
              </span>
              <span className="workflow-stage-stack__affordance" aria-hidden="true">{open ? '−' : '+'}</span>
            </button>
            <div id={bodyId} className="workflow-stage-stack__body" hidden={!open}>
              {unresolved && isCompleted && <p role="status" className="workflow-stage-stack__remaining">
                <b>Processing finished; unresolved work remains.</b> Verified fixes and remaining dispositions are shown separately below.
              </p>}
              {detail && !(stage === 'remediate' && progressHostId) ? <div className="workflow-stage-stack__live-detail" data-detail-owner="current">{detail}</div>
                : isCompleted && ['discover', 'assess'].includes(stage)
                  ? <CompletedStageDetails snapshot={snapshot} />
                  : stage === 'discover' && !terminal(snapshot.state)
                    ? <DiscoverRunProgress progress={liveDiscoverProgress(snapshot)} busy
                        source={snapshot?.source || null} scope={snapshot?.scope || null}
                        freshness="live"
                        onReview={onNavigate ? () => onNavigate(destination[stage]) : null} />
                  : <WorkflowStageActivityCard snapshot={snapshot} receivedAt={receivedAt} reviewWorkspace={stage === 'release' ? releaseReviewWorkspace : null}
                      progressHostId={stage === 'remediate' ? progressHostId : null} progressScanId={lineage?.scan_id}
                      onOpen={onNavigate ? () => onNavigate(destination[stage]) : null} />}
            </div>
            {stageAfter[stage] && <div data-stage-after={stage}>{stageAfter[stage]}</div>}
          </div>
        )
      })}
    </section>
  )
}
