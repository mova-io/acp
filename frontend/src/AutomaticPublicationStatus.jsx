import './remediation-automation-status.css'
import WorkflowOutcomeTiles from './WorkflowOutcomeTiles.jsx'
import { releaseBatchDomain, releaseBatchProgress } from './releaseBatchProgress.js'

/** Read-only saved-plan presentation; never authorizes or enqueues publication. */
// outOfDateFiles: files whose delivered copy is an earlier version (GET release publication.out_of_date).
// They are undelivered for the plan, but they are not "awaiting confirmation": nothing is in flight for them.
export default function AutomaticPublicationStatus({ authorization, pending = false, error = '', destinationLabel, onOpen, compact = false, collapsed = false, onFilter, outOfDateFiles = [] }) {
  const raw = authorization?.batch_progress
  const batch = raw?.authorization_id === authorization?.id ? releaseBatchProgress({stage:'release',release_batch_progress:raw}) : null
  const domain = releaseBatchDomain(batch)
  const active = authorization?.id
  const complete = batch?.available === true && batch.remaining === 0 && batch.status === 'completed'
  const outOfDate = batch?.available === true ? Math.min(batch.remaining,
    new Set(outOfDateFiles.filter(file => authorization?.files?.includes(file))).size) : 0
  const awaiting = batch?.available === true ? batch.remaining - outOfDate : 0
  const heading = pending ? 'Checking automatic publication' : active ? 'Automatic publication' : 'Publication'
  const Wrapper = collapsed ? 'details' : 'div'
  return <section className="panel" aria-label="Automatic publication status" data-scope-id={batch?.scope_id} data-snapshot-revision={batch?.revision}>
    <Wrapper className={collapsed ? 'automatic-publication-disclosure' : undefined}>
    {collapsed ? <summary><b>{heading}</b>{(authorization?.destination_label || destinationLabel) && <span> · {authorization?.destination_label || destinationLabel}</span>}{batch?.available === true && <span> · {batch.delivered.toLocaleString()} of {batch.total.toLocaleString()} delivered</span>}</summary> : <h3>{heading}</h3>}
    <p role="status">{pending ? 'ACP is checking the saved publishing permission. Wait before starting another delivery.' : active
      ? complete ? 'All authorized copies are confirmed at the destination.'
        : authorization.status === 'stopped' ? 'Automatic publishing is stopped. Existing delivery results remain available.'
          : authorization.requires_reconnect ? 'Delivery is paused. Reconnect Microsoft or Google Drive to continue the saved release.'
            : authorization.needs_attention || ['blocked', 'failed'].includes(authorization.status) ? 'Some copies could not be delivered. Published copies remain available; the remaining copies need recovery.'
              : 'Automatic publishing is on. No Publish click is needed for covered copies.'
      : error ? 'Automatic publication status is unavailable. ACP must confirm the saved permission before another delivery.' : 'Automatic publishing is off. Choose copies and confirm publication in Release.'}</p>
    {active && <p>{authorization.requires_reconnect ? 'Reconnect the destination in Sources, then return to Release to continue. ACP checks existing delivery receipts first; no new scan or approval is needed.' : authorization.needs_attention || ['blocked', 'failed'].includes(authorization.status) ? 'Delivery needs recovery. Check the specific issue below.' : 'ACP delivers eligible saved copies after processing and release checks.'}</p>}
    {(authorization?.destination_label || destinationLabel) && <p><b>Destination:</b> {authorization?.destination_label || destinationLabel}</p>}
    {batch?.available === true && <p><b>{batch.delivered.toLocaleString()} of {batch.total.toLocaleString()} authorized copies delivered</b>{outOfDate > 0 && ` · ${outOfDate.toLocaleString()} published ${outOfDate === 1 ? 'copy' : 'copies'} out of date`}{(awaiting > 0 || outOfDate === 0) && ` · ${awaiting.toLocaleString()} awaiting confirmation`}</p>}
    {active && batch?.available !== true && <p>Confirmed delivery totals are unavailable; this release is not shown as complete.</p>}
    {error && <p>{error}</p>}
    {onOpen && <button type="button" className="linklike" onClick={onOpen}>Open Release →</button>}
    </Wrapper>
    {!compact && domain && <WorkflowOutcomeTiles stage="release" domain={domain} executionId={batch.scope_id} scopeLabel={`${batch.total.toLocaleString()} authorized files · entire saved plan`} onFilter={onFilter} />}
    {!compact && batch?.available === true && !domain && <p>Delivery classifications are being reconciled. Outstanding copies are not assumed to be queued.</p>}
  </section>
}
