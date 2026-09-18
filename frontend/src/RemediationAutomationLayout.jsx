import { useLayoutEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import InfoTip from './InfoTip.jsx'
import RemediationAutomationStatus from './RemediationAutomationStatus.jsx'
import AutomaticPublicationStatus from './AutomaticPublicationStatus.jsx'
import './workflow-outcome-tiles.css'
import './remediation-automation-status.css'

// Current-run portals share the existing document-progress host identity. They
// never contribute review ITEM counts to the canonical finding partition.
export default function RemediationAutomationLayout({ progressHostId, scanId, batchId, policy, error, saving, reviewCount=0, statusCheckCount=0, onOpenReview, onRetry, authorization, publicationPending, publicationError, destinationLabel }) {
  const [hosts,setHosts]=useState({review:null,automation:null})
  useLayoutEffect(()=>{
    const find=suffix=>{
      const node=progressHostId ? document.getElementById(`${progressHostId}-${suffix}`) : null
      return node?.dataset.scanId===scanId && node?.dataset.batchId===batchId ? node : null
    }
    const refresh=()=>{
      const review=find('review'),automation=find('automation')
      setHosts(previous=>previous.review===review && previous.automation===automation ? previous : {review,automation})
    }
    refresh()
    const observer=new MutationObserver(refresh)
    observer.observe(document.body,{childList:true,subtree:true})
    return ()=>observer.disconnect()
  },[progressHostId,scanId,batchId])
  const enabled=policy?.enabled===true && policy.supported!==false
  const review=<div className="workflow-outcome-tiles__cell tone-amber remediation-review-workspace">
    <button type="button" className="workflow-outcome-tiles__tile" onClick={onOpenReview} disabled={!onOpenReview}>
      <span className="workflow-outcome-tiles__label">Review workspace</span><strong>{reviewCount.toLocaleString()}</strong>
      <span className="workflow-outcome-tiles__definition">{enabled ? 'Review tasks needing your input' : 'Open review tasks'}</span>
      {/* A zero here beside "Unresolved findings 1" read as a contradiction when the open work sat in
          Status checks. Those tasks are not the reviewer's to decide, so they stay out of the count,
          but the tile says they exist rather than leaving the reader to reconcile two numbers. */}
      {statusCheckCount > 0 && <span className="workflow-outcome-tiles__definition">+ {statusCheckCount.toLocaleString()} status check{statusCheckCount === 1 ? '' : 's'} ACP is tracking</span>}
      <span className="workflow-outcome-tiles__definition">Open review tasks →</span>
    </button>
    <InfoTip label="Review workspace">{enabled ? 'Review tasks requiring your input.' : 'Open review tasks available for approval or manual work.'} Status checks ACP is tracking are listed separately. Tasks and findings are different units: one task can cover several findings and a finding can have no task, so this count is never added to the finding totals.</InfoTip>
  </div>
  const settings=<details className="panel remediation-automation-combined" aria-label="Approval and publication settings">
    <summary><b>Automation settings</b> · AI approval {error ? 'unavailable' : saving ? 'saving' : !policy ? 'checking' : enabled ? 'on' : 'individual'} · publication {publicationPending ? 'checking' : authorization?.id ? authorization.status==='stopped' ? 'stopped' : 'automatic' : 'manual'}</summary>
    <RemediationAutomationStatus combined policy={policy} error={error} saving={saving} onRetry={onRetry}/>
    <AutomaticPublicationStatus compact authorization={authorization} pending={publicationPending} error={publicationError} destinationLabel={destinationLabel}/>
  </details>
  return <>
    {hosts.review?.isConnected && hosts.review.dataset.batchId===batchId ? createPortal(review,hosts.review) : <div className="remediation-review-workspace-fallback">{review}</div>}
    {hosts.automation?.isConnected && hosts.automation.dataset.batchId===batchId ? createPortal(settings,hosts.automation) : settings}
  </>
}
