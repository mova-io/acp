import WorkflowOutcomeTiles, { outcomeTileModel } from './WorkflowOutcomeTiles.jsx'
import ProgressQueueDrawer from './ProgressQueueDrawer.jsx'
import { getStageProgressQueue } from './api.js'
import { useEffect, useState } from 'react'
import LiveHeartbeatBars from './LiveHeartbeatBars.jsx'
import { canonicalStageCardModel, alignRemediationAssessment } from './canonicalStageCard.js'
import { releaseBatchProgress, releaseBatchDomain } from './releaseBatchProgress.js'
import { requestOpenReviewItem } from './openReviewItem.js'

// The snapshot's unresolved findings (contract C2) that name a review item, attached to the file
// rows of the Unresolved findings queue so each can be opened directly. Findings without a
// review_item_id have nothing to open and are left to the file row's own count.
function withReviewItems(files, snapshot, bucket) {
  const findings = bucket === 'attention' && Array.isArray(snapshot?.domain_reconciliation?.unresolved_findings)
    ? snapshot.domain_reconciliation.unresolved_findings.filter(f => f?.review_item_id != null) : []
  if (!findings.length) return files
  return files.map(row => {
    const items = findings.filter(f => f.file === row.file)
      .map(f => ({ itemId: String(f.review_item_id), criterion: f.rule_id || null, name: f.rule_name || null }))
    return items.length ? { ...row, reviewItems: items } : row
  })
}

const terminal = (state) => ['processing_complete', 'succeeded', 'failed', 'cancelled', 'superseded', 'integrity_failed'].includes(state)
const shown = (value) => value == null ? '—' : Number(value).toLocaleString()

export default function WorkflowStageActivityCard({ snapshot, receivedAt, onOpen, progressHostId = null, progressScanId, onOutcomeFilter, reviewWorkspace = null }) {
  snapshot = alignRemediationAssessment(snapshot, null)
  const model = canonicalStageCardModel(snapshot)
  const [selection, setSelection] = useState(null)
  const [queueView, setQueueView] = useState({ files: [], loading: false, error: null })
  const executionId = model?.executionId
  const queue = selection?.executionId === executionId ? selection.key : null
  const queueIdentity = queue ? `${executionId}:${queue}` : null
  const batchDomain = releaseBatchDomain(releaseBatchProgress(snapshot))
  const primaryDomain = model?.stage === 'release' && releaseBatchProgress(snapshot) ? batchDomain : snapshot?.domain_reconciliation
  const queueModel = outcomeTileModel(model?.stage, primaryDomain)
  const tile = queueModel?.tiles.find(item => item.key === queue)
  const expected = tile?.value
  useEffect(() => { setSelection(null) }, [executionId])
  useEffect(() => {
    if (!queue || !executionId) return undefined
    let active = true
    setQueueView(previous => previous.identity === queueIdentity ? { ...previous, error: null } : { identity: queueIdentity, files: [], loading: true, error: null })
    getStageProgressQueue(executionId, queue).then(result => {
      if (!active) return
      if (!result.available || result.execution_id !== executionId || result.bucket !== queue) {
        setQueueView({ identity: queueIdentity, files: [], loading: false, error: result.reason || 'Recorded queue membership is unavailable.' })
      } else if (expected != null && result.count !== expected) {
        setQueueView({ identity: queueIdentity, files: [], loading: false, error: 'This queue changed while loading. Live counts will refresh automatically.' })
      } else setQueueView({ identity: queueIdentity, files: result.files || [], loading: false, error: null })
    }).catch(error => {
      if (active) setQueueView({ identity: queueIdentity, files: [], loading: false, error: error.message || 'Could not load this queue.' })
    })
    return () => { active = false }
  }, [queue, queueIdentity, executionId, expected, snapshot?.generated_at])
  if (!model) return null
  const domain = model.domain
  const batch = releaseBatchProgress(snapshot)
  const done = domain?.accounted ?? model.accounted
  const total = domain?.total ?? model.total
  const pct = typeof done === 'number' && typeof total === 'number' && total > 0
    ? Math.max(0, Math.min(100, Math.round(done / total * 100))) : 0
  const findingAccounting = model.stage === 'remediate' && !!domain
  const missingOutcomes = findingAccounting && Number.isSafeInteger(total) && Number.isSafeInteger(done) && total > done ? total - done : 0
  const balanced = findingAccounting && Number.isSafeInteger(total) && Number.isSafeInteger(done)
    && done >= 0 && done <= total && domain.buckets.every(([, value]) => Number.isSafeInteger(value) && value >= 0)
    && domain.buckets.reduce((sum, [, value]) => sum + value, 0) === total
  const live = !terminal(batch?.state || snapshot.state)
  return <section className={`workflow-sse-card stage-${model.stage}`} aria-label={`${model.stageLabel} activity`}>
    <header className="workflow-sse-card__header">
      <div><strong>{model.stageLabel} · {batch?.label || model.stateLabel}</strong>
        <span>Workflow revision {model.workflowRevision ?? '—'}</span></div>
      {live && <LiveHeartbeatBars measuredAt={receivedAt} stage={model.stage}
        historyKey={`${snapshot.workflow_id || 'workflow'}:${model.executionId || model.stage}`} showText />}
      {onOpen && <button type="button" className="linklike" onClick={onOpen}>Open details →</button>}
    </header>
    <p className="workflow-sse-card__outcome">{batch ? <b>{batch.summary}</b> : findingAccounting
      ? <><b>{shown(total)} assessed findings</b> · Outcome breakdown</>
      : <><b>{shown(done)} of {shown(total)}</b> {domain?.unit || model.unit}</>}</p>
    {batch && <details className="workflow-sse-card__outcome-details"><summary>Latest delivery request</summary><p className="workflow-sse-card__notice">{batch.available === false
      ? 'The approved batch could not be confirmed. '
      : `${shown(batch.remaining)} authorized files awaiting confirmed delivery. `}Latest delivery request: {shown(done)} of {shown(total)} requested documents accounted for. Request completion does not mean the entire batch is delivered.</p></details>}
    {findingAccounting && <p className="workflow-sse-card__notice">{live ? 'Verified totals update as document work progresses.' : 'Automatic document work has stopped; remaining findings still need review or remediation.'} Outcomes show what happened to the findings. Document categories show how they can be remediated; individual bucket counts can differ.</p>}
    {model.stage === 'remediate' && progressHostId && <div id={progressHostId} data-scan-id={progressScanId} data-batch-id={model.executionId} aria-label="Document progress summary" />}
    {['remediate', 'release'].includes(model.stage) ? <>
      {(!batch || batchDomain) && <WorkflowOutcomeTiles queueMode={!batch} stage={model.stage} domain={primaryDomain}
        scopeLabel={batch ? `${shown(batch.total)} authorized files · entire saved plan` : undefined}
        reviewWorkspace={model.stage === 'release' ? reviewWorkspace : null}
        reviewHostId={model.stage === 'remediate' && progressHostId ? `${progressHostId}-review` : null} reviewScanId={progressScanId}
        baseline={batch ? null : snapshot.progress_baseline} executionId={batch?.scope_id || model.executionId} onFilter={batch ? (onOutcomeFilter ? bucket => onOutcomeFilter(bucket) : undefined) : bucket => { setSelection({ key: bucket, executionId }); onOutcomeFilter?.(bucket) }} />}
      {batch && !batchDomain && <p className="workflow-sse-card__notice" role="status">Delivery classifications are unavailable. Confirmed deliveries remain visible above; outstanding copies are not assumed to be queued.</p>}
      {model.stage === 'remediate' && progressHostId && <div id={`${progressHostId}-automation`} data-scan-id={progressScanId} data-batch-id={model.executionId} />}
      <details className="workflow-sse-card__outcome-details">
        <summary>Outcome details</summary>
        {domain?.buckets?.length > 0 && <dl className="workflow-sse-card__metrics">
          {domain.buckets.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{shown(value)}</dd></div>)}
        </dl>}
        {balanced && <p className="workflow-sse-card__notice">
          {domain.buckets.filter(([, value]) => value > 0).map(([label, value]) => `${shown(value)} ${label.toLowerCase()}`).join(' + ') || '0'} = {shown(total)} assessed findings.
          <br />{shown(done)} with recorded outcomes + {shown(missingOutcomes)} awaiting an outcome = {shown(total)} assessed findings.
        </p>}
      </details>
    </> : <>
      <div className="workflow-sse-card__track" aria-label={`${pct}% complete`} role="progressbar"
        aria-valuemin="0" aria-valuemax="100" aria-valuenow={pct}><span style={{ width: `${pct}%` }} /></div>
      {domain?.buckets?.length > 0 && <dl className="workflow-sse-card__metrics">
        {domain.buckets.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{shown(value)}</dd></div>)}
      </dl>}
    </>}
    {snapshot.omitted_assessment_groups?.length > 0 && <details>
      <summary>Findings not in the remediation breakdown — by file and SC</summary>
      <ul>{snapshot.omitted_assessment_groups.map(({ file, sc, count }) => <li key={`${file}:${sc}`}>
        <b>{file}</b> · SC {sc || 'not recorded'} · {shown(count)} {count === 1 ? 'finding' : 'findings'}
      </li>)}</ul>
    </details>}
    {!model.integrityOk && <p className="workflow-sse-card__notice"><b>Accounting is reconciling.</b> {missingOutcomes > 0 ? `${missingOutcomes} assessed finding${missingOutcomes === 1 ? '' : 's'} still lack a recorded outcome. This is not a count of fixes completed.` : 'Durable totals remain visible while ACP verifies this snapshot.'}</p>}
    {queue && <ProgressQueueDrawer title={tile?.label || 'File queue'}
      scopeLabel={model.stage === 'remediate' ? `${expected ?? '—'} findings · this remediation run` : `${expected ?? '—'} requested files · this release`}
      {...(queueView.identity === queueIdentity ? { ...queueView, files: withReviewItems(queueView.files, snapshot, queue) } : { files: [], loading: true })} onClose={() => setSelection(null)}
      onOpenItem={model.stage === 'remediate' ? item => {
        setSelection(null)
        onOpen?.()
        requestOpenReviewItem({ itemId: item.itemId, scanId: snapshot?.scan_id || progressScanId || null })
      } : undefined} />}
  </section>
}
